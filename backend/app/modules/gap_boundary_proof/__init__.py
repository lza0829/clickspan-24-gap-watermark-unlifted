"""切分边界可核证：超时切开的相邻会话对必落边界，两点-间隙-超时三数可核。

形态选择（两种须择一）：独立边界表 gap_boundaries —— 不写入 next 会话快照列，
也禁止只在报告 JSON 临时拼。
成败路径（两种须择一）：切分与边界同一事务 —— attach_hit 内在落库前先校验
三数关系（gap_sec == 两点之差 且 gap_sec > 当时超时），矛盾即抛错，整批回滚，
切分不得单独成功。

间隙水位物化（同一套选择）：
- 形态：独立间隙水位表 gap_watermarks（每条切边一行不可变快照，主键 edge_id，
  不写入切边行的可空列）。
- 成败：切分成功即在同一事务内「落边 + 抬该访客间隙水位」，缺一回滚；
  抬水位前以落库后的切边行对撞（水位值 == 边 gap_sec、访客 == 边访客、
  边键/触发键 == 边首击），任何分叉抛错，切分不得单独成功。
- 可读互证：水位行保留 edge_id 与 trigger_hit_id，核证时回读切边行，
  水位核证接口与边界核证接口互相印证。
"""
from datetime import datetime, timezone

STATUS = "implemented"

TS_FMT = "%Y-%m-%dT%H:%M:%SZ"

SCHEMA = """
CREATE TABLE IF NOT EXISTS gap_boundaries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  visitor_id TEXT NOT NULL REFERENCES visitors(id),
  prev_session_id TEXT NOT NULL REFERENCES sessions(id),
  next_session_id TEXT NOT NULL REFERENCES sessions(id),
  prev_last_hit_id TEXT NOT NULL,
  next_first_hit_id TEXT NOT NULL,
  prev_last_ts TEXT NOT NULL,
  next_first_ts TEXT NOT NULL,
  gap_sec INTEGER NOT NULL,
  timeout_sec_snapshot INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(prev_session_id, next_session_id)
);

CREATE TABLE IF NOT EXISTS gap_watermarks (
  edge_id INTEGER PRIMARY KEY REFERENCES gap_boundaries(id),
  visitor_id TEXT NOT NULL REFERENCES visitors(id),
  watermark_sec INTEGER NOT NULL,
  visitor_edge_key TEXT NOT NULL,
  trigger_hit_id TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_gap_watermarks_visitor
  ON gap_watermarks(visitor_id);
"""


def ensure_schema(conn):
    conn.executescript(SCHEMA)


def _ts(s: str) -> datetime:
    return datetime.strptime(s, TS_FMT).replace(tzinfo=timezone.utc)


def _now() -> str:
    return datetime.now(timezone.utc).strftime(TS_FMT)


def gap_seconds(a: str, b: str) -> int:
    """b - a 的秒数。"""
    return int((_ts(b) - _ts(a)).total_seconds())


def draft_boundary(
    *, visitor_id, prev_session_id, next_session_id, prev_hit, next_hit, timeout_sec
) -> dict:
    """边界草稿：访客、prev/next 会话、两点、间隙、当时超时快照。"""
    return {
        "visitor_id": visitor_id,
        "prev_session_id": prev_session_id,
        "next_session_id": next_session_id,
        "prev_last_hit_id": prev_hit["id"],
        "next_first_hit_id": next_hit["id"],
        "prev_last_ts": prev_hit["event_ts"],
        "next_first_ts": next_hit["event_ts"],
        "gap_sec": gap_seconds(prev_hit["event_ts"], next_hit["event_ts"]),
        "timeout_sec_snapshot": int(timeout_sec),
    }


def validate_draft(d: dict) -> dict:
    """三数关系校验：gap_sec 须等于两点之差，且严格大于当时超时。矛盾即拒。"""
    recomputed = gap_seconds(d["prev_last_ts"], d["next_first_ts"])
    if recomputed != d["gap_sec"]:
        raise ValueError(
            f"间隙与两点矛盾：gap_sec={d['gap_sec']} 而两点之差={recomputed}"
        )
    if d["gap_sec"] <= d["timeout_sec_snapshot"]:
        raise ValueError(
            f"间隙未超当时超时：gap_sec={d['gap_sec']} "
            f"timeout_sec_snapshot={d['timeout_sec_snapshot']}"
        )
    return d


def insert_boundary(conn, d: dict) -> int:
    cur = conn.execute(
        """
        INSERT INTO gap_boundaries(
          visitor_id, prev_session_id, next_session_id,
          prev_last_hit_id, next_first_hit_id,
          prev_last_ts, next_first_ts, gap_sec, timeout_sec_snapshot, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (
            d["visitor_id"],
            d["prev_session_id"],
            d["next_session_id"],
            d["prev_last_hit_id"],
            d["next_first_hit_id"],
            d["prev_last_ts"],
            d["next_first_ts"],
            d["gap_sec"],
            d["timeout_sec_snapshot"],
            _now(),
        ),
    )
    return cur.lastrowid


def edge_key(d: dict) -> str:
    """边键：访客+上一段→下一段，读路径与水位行共用。"""
    return f"{d['visitor_id']}:{d['prev_session_id']}->{d['next_session_id']}"


def insert_watermark(
    conn, *, visitor_id, watermark_sec, edge_id, visitor_edge_key, trigger_hit_id
):
    """低层写：每边一行不可变水位快照，水位值取当次边 gap_sec。"""
    conn.execute(
        """
        INSERT INTO gap_watermarks(
          edge_id, visitor_id, watermark_sec, visitor_edge_key,
          trigger_hit_id, created_at
        ) VALUES (?,?,?,?,?,?)
        """,
        (
            int(edge_id),
            visitor_id,
            int(watermark_sec),
            visitor_edge_key,
            trigger_hit_id,
            _now(),
        ),
    )


def lift_watermark(conn, boundary_id: int, d: dict) -> dict:
    """抬升该访客间隙水位：以刚落库的切边行对撞，水位值=当次 gap_sec。

    对撞项：访客、gap_sec、触发键（下段首击）。任何分叉抛错，由调用方整体
    回滚；写后再回读一次，确保水位与切边一致才返回。
    """
    edge = conn.execute(
        "SELECT * FROM gap_boundaries WHERE id=?", (boundary_id,)
    ).fetchone()
    if edge is None:
        raise ValueError("水位抬升失败：切边不存在")
    if edge["visitor_id"] != d["visitor_id"]:
        raise ValueError(
            f"水位挂错访客：切边访客={edge['visitor_id']} 而水位访客={d['visitor_id']}"
        )
    if int(edge["gap_sec"]) != int(d["gap_sec"]):
        raise ValueError(
            f"水位与切边 gap 分叉：gap_sec={d['gap_sec']} 而切边 gap_sec={edge['gap_sec']}"
        )
    if edge["next_first_hit_id"] != d["next_first_hit_id"]:
        raise ValueError(
            f"水位触发键与切边分叉：trigger={d['next_first_hit_id']} "
            f"而切边下段首击={edge['next_first_hit_id']}"
        )
    key = edge_key(d)
    existing = conn.execute(
        "SELECT * FROM gap_watermarks WHERE edge_id=?", (boundary_id,)
    ).fetchone()
    if existing:
        # 切边行不可变；重放不得覆写已有水位快照
        if (
            existing["visitor_id"] != d["visitor_id"]
            or int(existing["watermark_sec"]) != int(edge["gap_sec"])
            or existing["visitor_edge_key"] != key
            or existing["trigger_hit_id"] != d["next_first_hit_id"]
        ):
            raise ValueError("已有水位快照与切边分叉：拒绝覆写")
        return dict(existing)
    insert_watermark(
        conn,
        visitor_id=d["visitor_id"],
        watermark_sec=int(edge["gap_sec"]),
        edge_id=boundary_id,
        visitor_edge_key=key,
        trigger_hit_id=d["next_first_hit_id"],
    )
    row = conn.execute(
        "SELECT * FROM gap_watermarks WHERE edge_id=?", (boundary_id,)
    ).fetchone()
    if (
        row is None
        or int(row["watermark_sec"]) != int(edge["gap_sec"])
        or row["visitor_id"] != d["visitor_id"]
        or row["visitor_edge_key"] != key
        or row["trigger_hit_id"] != d["next_first_hit_id"]
    ):
        raise ValueError("水位未与切边对齐：写入后回读分叉")
    return dict(row)


def _assert_watermark_aligned(conn, boundary_id: int, d: dict) -> None:
    """独立于 lift 的终检：切边存在则必有同访客、同 gap、同边键的水位行。"""
    edge = conn.execute(
        "SELECT * FROM gap_boundaries WHERE id=?", (boundary_id,)
    ).fetchone()
    wm = conn.execute(
        "SELECT * FROM gap_watermarks WHERE edge_id=?", (boundary_id,)
    ).fetchone()
    if wm is None:
        raise ValueError("切边已落而水位未抬：缺少水位行")
    if edge is None or int(wm["edge_id"]) != int(boundary_id):
        raise ValueError("切边已落而水位未接：水位指向的其他边")
    if wm["visitor_id"] != d["visitor_id"]:
        raise ValueError("水位挂错访客")
    if wm["visitor_edge_key"] != edge_key(d):
        raise ValueError("水位边键与切边分叉")
    if int(wm["watermark_sec"]) != int(edge["gap_sec"]) or int(
        wm["watermark_sec"]
    ) != int(d["gap_sec"]):
        raise ValueError(
            f"水位≠边 gap_sec：水位={wm['watermark_sec']} 边 gap_sec={edge['gap_sec']}"
        )
    if wm["trigger_hit_id"] != d["next_first_hit_id"]:
        raise ValueError("水位触发键与切边分叉")


def attach_hit(conn, vid: str, hit: dict, timeout_sec: int, new_session_id) -> str:
    """把击点挂到该访客最新 open 会话；超隙则切开并同事务落边界并抬水位。

    切分 / 边界 / 水位同成败：边界草稿先过三数校验再落库，落库后立刻以该边
    行对撞抬升该访客水位，任何矛盾抛错，由调用方整体回滚；既不存在"切开无
    边界"，也不存在"切边已落而水位未抬"或"水位≠边 gap_sec 仍成功"。
    new_session_id: 需要新会话时的 id 工厂（无参 callable）。
    """
    existing = conn.execute(
        "SELECT session_id FROM hits WHERE id=?", (hit["id"],)
    ).fetchone()
    if existing:  # 重放幂等：已落击点直接回原会话，不再触发切分
        return existing["session_id"]
    sess = conn.execute(
        """
        SELECT id FROM sessions
        WHERE visitor_id=? AND status='open'
        ORDER BY started_at DESC LIMIT 1
        """,
        (vid,),
    ).fetchone()
    if sess:
        prev_hit = conn.execute(
            """
            SELECT id, event_ts FROM hits
            WHERE session_id=? AND channel='primary'
            ORDER BY event_ts DESC, id DESC LIMIT 1
            """,
            (sess["id"],),
        ).fetchone()
        if prev_hit and gap_seconds(prev_hit["event_ts"], hit["event_ts"]) > timeout_sec:
            next_sid = new_session_id()
            conn.execute(
                "UPDATE sessions SET status='closed' WHERE id=?", (sess["id"],)
            )
            conn.execute(
                """
                INSERT INTO sessions(id, visitor_id, started_at, ended_at, hit_count, status)
                VALUES (?,?,?,?,0,'open')
                """,
                (next_sid, vid, hit["event_ts"], hit["event_ts"]),
            )
            draft = validate_draft(
                draft_boundary(
                    visitor_id=vid,
                    prev_session_id=sess["id"],
                    next_session_id=next_sid,
                    prev_hit=prev_hit,
                    next_hit=hit,
                    timeout_sec=timeout_sec,
                )
            )
            boundary_id = insert_boundary(conn, draft)
            lift_watermark(conn, boundary_id, draft)
            _assert_watermark_aligned(conn, boundary_id, draft)
            return next_sid
        return sess["id"]
    sid = new_session_id()
    conn.execute(
        """
        INSERT INTO sessions(id, visitor_id, started_at, ended_at, hit_count, status)
        VALUES (?,?,?,?,0,'open')
        """,
        (sid, vid, hit["event_ts"], hit["event_ts"]),
    )
    return sid


def list_boundaries(conn, visitor_id: str | None = None, session_id: str | None = None):
    """只读：边界列表，可按访客或会话（prev/next 任一）过滤。"""
    sql = "SELECT * FROM gap_boundaries"
    clauses, args = [], []
    if visitor_id:
        clauses.append("visitor_id=?")
        args.append(visitor_id)
    if session_id:
        clauses.append("(prev_session_id=? OR next_session_id=?)")
        args += [session_id, session_id]
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY id"
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def get_boundary(conn, boundary_id: int):
    row = conn.execute(
        "SELECT * FROM gap_boundaries WHERE id=?", (boundary_id,)
    ).fetchone()
    return dict(row) if row else None


def _watermark_rows(conn, visitor_id: str | None = None) -> list[dict]:
    if visitor_id:
        rows = conn.execute(
            "SELECT * FROM gap_watermarks WHERE visitor_id=? ORDER BY edge_id",
            (visitor_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM gap_watermarks ORDER BY visitor_id, edge_id"
        ).fetchall()
    return [dict(r) for r in rows]


def gap_watermark(conn, visitor_id: str | None = None) -> dict:
    """间隙水位读路径：读独立水位表；无水位行如实返回空，不回落 0 冒充对齐。"""
    rows = _watermark_rows(conn, visitor_id)
    if visitor_id:
        if not rows:
            return {
                "visitor_id": visitor_id,
                "watermark_sec": None,
                "edge_id": None,
                "visitor_edge_key": None,
                "trigger_hit_id": None,
            }
        # 该访客当前水位 = 最新一条切边抬升的水位快照
        row = rows[-1]
        return {**row, "visitor_id": visitor_id, "watermarks": rows}
    return {"watermarks": rows}


def watermark_certificate(conn, visitor_id: str | None = None) -> list[dict]:
    """只读水位核证：每条给访客、边键、gap_sec、水位、结论。

    每一条切边一行：回读其水位快照互证，缺水位即「切边已落、水位未抬」。
    结论为真的条件：切边自身三数可核、水位与该边同访客、同边键、水位值 ==
    边 gap_sec、触发键 == 边下段首击、水位行指向本边。
    """
    edges = list_boundaries(conn, visitor_id=visitor_id)
    wm_by_edge = {int(r["edge_id"]): r for r in _watermark_rows(conn)}
    out = []
    seen: set[int] = set()
    for edge in edges:
        seen.add(int(edge["id"]))
        out.append(_certify_one(conn, edge, wm_by_edge.get(int(edge["id"]))))
    # 无对应切边却存在的水位行（如被挂到别的访客边上）：也须暴露，判不通过
    for edge_id, wm in wm_by_edge.items():
        if edge_id in seen:
            continue
        if visitor_id and wm["visitor_id"] != visitor_id:
            continue
        out.append(
            _certify_one(conn, get_boundary(conn, edge_id), wm, fallback_visitor=wm["visitor_id"])
        )
    out.sort(key=lambda r: ((r["visitor_id"] or ""), (r["edge_id"] or 0)))
    return out


def _certify_one(conn, edge, wm, fallback_visitor=None) -> dict:
    visitor = edge["visitor_id"] if edge else fallback_visitor
    checks = {
        "edge_found": edge is not None,
        "watermark_found": wm is not None,
        # 水位行访客 == 边访客：防「水位挂错访客」
        "watermark_visitor_matches_edge": bool(edge and wm)
        and wm["visitor_id"] == edge["visitor_id"],
        # 水位行指向本边：防「水量挂到别的边」
        "watermark_points_to_edge": bool(edge and wm)
        and int(wm["edge_id"]) == int(edge["id"]),
        "edge_key_matches": bool(edge and wm)
        and wm["visitor_edge_key"] == edge_key(edge),
        "watermark_equals_edge_gap": bool(edge and wm)
        and int(wm["watermark_sec"]) == int(edge["gap_sec"]),
        "trigger_matches_edge": bool(edge and wm)
        and wm["trigger_hit_id"] == edge["next_first_hit_id"],
        "edge_three_numbers_ok": bool(edge) and bool(verify(conn, edge)["ok"]),
    }
    conclusion = all(checks.values())
    return {
        "visitor_id": visitor,
        "edge_id": edge["id"] if edge else (wm["edge_id"] if wm else None),
        "edge_key": edge_key(edge) if edge else (wm["visitor_edge_key"] if wm else None),
        "prev_session_id": edge["prev_session_id"] if edge else None,
        "next_session_id": edge["next_session_id"] if edge else None,
        "trigger_hit_id": edge["next_first_hit_id"] if edge else (
            wm["trigger_hit_id"] if wm else None
        ),
        "gap_sec": edge["gap_sec"] if edge else None,
        "watermark_sec": wm["watermark_sec"] if wm else None,
        "checks": checks,
        "ok": conclusion,
        "conclusion": "水位=边 gap" if conclusion else "水位与边未接或分叉",
    }


def verify(conn, row: dict) -> dict:
    """只读核证：以 hits 表为准重算两点之差，并核两点确为相邻会话的末击/首击。"""
    prev_hit = conn.execute(
        "SELECT id, session_id, event_ts FROM hits WHERE id=?",
        (row["prev_last_hit_id"],),
    ).fetchone()
    next_hit = conn.execute(
        "SELECT id, session_id, event_ts FROM hits WHERE id=?",
        (row["next_first_hit_id"],),
    ).fetchone()
    prev_last = conn.execute(
        """
        SELECT id FROM hits WHERE session_id=? AND channel='primary'
        ORDER BY event_ts DESC, id DESC LIMIT 1
        """,
        (row["prev_session_id"],),
    ).fetchone()
    next_first = conn.execute(
        """
        SELECT id FROM hits WHERE session_id=? AND channel='primary'
        ORDER BY event_ts, id LIMIT 1
        """,
        (row["next_session_id"],),
    ).fetchone()
    recomputed = (
        gap_seconds(prev_hit["event_ts"], next_hit["event_ts"])
        if prev_hit and next_hit
        else None
    )
    checks = {
        "prev_hit_found": prev_hit is not None,
        "next_hit_found": next_hit is not None,
        "prev_hit_in_prev_session": bool(prev_hit)
        and prev_hit["session_id"] == row["prev_session_id"],
        "next_hit_in_next_session": bool(next_hit)
        and next_hit["session_id"] == row["next_session_id"],
        "prev_hit_is_last": bool(prev_last)
        and prev_last["id"] == row["prev_last_hit_id"],
        "next_hit_is_first": bool(next_first)
        and next_first["id"] == row["next_first_hit_id"],
        "gap_sec_recomputed": recomputed,
        "gap_matches_points": recomputed is not None and recomputed == row["gap_sec"],
        "gap_exceeds_timeout": row["gap_sec"] > row["timeout_sec_snapshot"],
    }
    checks["ok"] = all(v for k, v in checks.items() if isinstance(v, bool))
    return checks
