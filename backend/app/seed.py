import json

from app.db import FIXTURES_DIR, connect
from app.modules import gap_boundary_proof

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS visitors (
  id TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  visitor_id TEXT NOT NULL REFERENCES visitors(id),
  started_at TEXT NOT NULL,
  ended_at TEXT,
  hit_count INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'open'
);

CREATE TABLE IF NOT EXISTS hits (
  id TEXT PRIMARY KEY,
  visitor_id TEXT NOT NULL REFERENCES visitors(id),
  session_id TEXT REFERENCES sessions(id),
  event_ts TEXT NOT NULL,
  page_path TEXT NOT NULL,
  event_name TEXT NOT NULL,
  channel TEXT NOT NULL DEFAULT 'primary',
  UNIQUE(visitor_id, event_ts, page_path, event_name)
);

CREATE TABLE IF NOT EXISTS quarantine (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  visitor_id TEXT,
  reason TEXT NOT NULL,
  member_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingest_batches (
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  report_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS session_proofs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  visitor_id TEXT NOT NULL,
  hit_ids_json TEXT NOT NULL,
  hit_count INTEGER NOT NULL,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  created_at TEXT NOT NULL
);
"""


def init_db():
    conn = connect()
    conn.executescript(SCHEMA)
    gap_boundary_proof.ensure_schema(conn)
    row = conn.execute("SELECT value FROM settings WHERE key='idle_timeout_sec'").fetchone()
    if not row:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES ('idle_timeout_sec', ?)",
            ("1800",),
        )
    n = conn.execute("SELECT COUNT(*) AS c FROM visitors").fetchone()["c"]
    if n == 0:
        _seed_fixtures(conn)
    conn.commit()
    conn.close()


def _seed_fixtures(conn):
    fixture = FIXTURES_DIR / "seed_hits.json"
    if not fixture.exists():
        return
    data = json.loads(fixture.read_text(encoding="utf-8"))
    for v in data.get("visitors", []):
        conn.execute(
            "INSERT INTO visitors(id, label, created_at) VALUES (?,?,?)",
            (v["id"], v["label"], v["created_at"]),
        )
    # 与收流同路径：逐击点挂会话，超隙即切开并同事务落边界
    timeout = idle_timeout_sec(conn)
    for visitor_id, group in _group_hits(data.get("hits", [])).items():
        group = sorted(group, key=lambda h: (h["event_ts"], h["id"]))
        counter = {"n": 0}

        def next_sid():
            counter["n"] += 1
            return f"S-{visitor_id}-{counter['n']}"

        for h in group:
            sid = gap_boundary_proof.attach_hit(
                conn, visitor_id, h, timeout, next_sid
            )
            conn.execute(
                """
                INSERT INTO hits(id, visitor_id, session_id, event_ts, page_path, event_name, channel)
                VALUES (?,?,?,?,?,?,?)
                """,
                (
                    h["id"],
                    visitor_id,
                    sid,
                    h["event_ts"],
                    h["page_path"],
                    h["event_name"],
                    h.get("channel", "primary"),
                ),
            )
            conn.execute(
                """
                UPDATE sessions
                SET hit_count = hit_count + 1,
                    ended_at = CASE
                      WHEN ended_at IS NULL OR ended_at < ? THEN ?
                      ELSE ended_at END
                WHERE id=?
                """,
                (h["event_ts"], h["event_ts"], sid),
            )
        for sess in conn.execute(
            "SELECT * FROM sessions WHERE visitor_id=? ORDER BY started_at",
            (visitor_id,),
        ).fetchall():
            hit_ids = [
                r["id"]
                for r in conn.execute(
                    "SELECT id FROM hits WHERE session_id=? ORDER BY event_ts, id",
                    (sess["id"],),
                ).fetchall()
            ]
            conn.execute(
                """
                INSERT INTO session_proofs(session_id, visitor_id, hit_ids_json, hit_count, started_at, ended_at, created_at)
                VALUES (?,?,?,?,?,?,datetime('now'))
                """,
                (
                    sess["id"],
                    visitor_id,
                    json.dumps(hit_ids, ensure_ascii=False),
                    len(hit_ids),
                    sess["started_at"],
                    sess["ended_at"],
                ),
            )


def _group_hits(hits):
    out = {}
    for h in hits:
        out.setdefault(h["visitor_id"], []).append(h)
    return out


def idle_timeout_sec(conn) -> int:
    row = conn.execute("SELECT value FROM settings WHERE key='idle_timeout_sec'").fetchone()
    return int(row["value"]) if row else 1800
