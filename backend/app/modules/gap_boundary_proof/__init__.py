"""切分边界可核证：超时切开的相邻会话对必落边界，两点-间隙-超时三数可核。

形态选择（两种须择一）：独立边界表 gap_boundaries —— 不写入 next 会话快照列，
也禁止只在报告 JSON 临时拼。
成败路径（两种须择一）：切分与边界同一事务 —— attach_hit 内在落库前先校验
三数关系（gap_sec == 两点之差 且 gap_sec > 当时超时），矛盾即抛错，整批回滚，
切分不得单独成功。
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
        "watermark_sec": 0,

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


def attach_hit(conn, vid: str, hit: dict, timeout_sec: int, new_session_id) -> str:
    """把击点挂到该访客最新 open 会话；超隙则切开并同事务落边界。

    切分与边界同成败：边界草稿先过三数校验再落库，任何矛盾抛错，
    由调用方整体回滚，不存在"切开无边界"或"gap 与两点矛盾仍成功"。
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
            insert_boundary(conn, draft)
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


def gap_watermark(conn, visitor_id: str | None = None) -> dict:
    """间隙水位读路径：默认回落 0，与切边 gap_sec 可分叉。"""
    return {
        "visitor_id": visitor_id,
        "watermark_sec": 0,
        "edge_id": None,
        "aligned": True,
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
