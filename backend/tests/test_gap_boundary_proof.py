"""超间隙夹具跑通后，边界三数可核；切分与边界同成败。"""
import json

import pytest
from fastapi.testclient import TestClient

from app.db import connect
from app.main import app
from app.modules import gap_boundary_proof as gbp
from app.services import ingest as ingest_svc


def _counts():
    conn = connect()
    try:
        return {
            t: conn.execute(f"SELECT COUNT(*) AS c FROM {t}").fetchone()["c"]
            for t in ("visitors", "sessions", "hits", "gap_boundaries")
        }
    finally:
        conn.close()


def test_seed_without_gap_has_no_boundary(fresh_db):
    conn = connect()
    try:
        assert conn.execute("SELECT COUNT(*) AS c FROM gap_boundaries").fetchone()["c"] == 0
        # 种子数据访客内最大间隙 120s < 1800s：每访客仍只有一个 open 会话
        rows = conn.execute(
            "SELECT visitor_id, COUNT(*) AS c FROM sessions GROUP BY visitor_id"
        ).fetchall()
        assert {r["visitor_id"]: r["c"] for r in rows} == {"V-1001": 1, "V-1002": 1}
    finally:
        conn.close()


def test_over_gap_fixture_splits_and_lands_boundaries(fresh_db):
    res = ingest_svc.run_batch()
    assert res["ok"], res
    assert res["report"]["written"] == 5
    assert res["report"]["split"] == 2
    assert res["report"]["idle_timeout_sec"] == 1800

    conn = connect()
    try:
        rows = gbp.list_boundaries(conn)
        assert len(rows) == 2
        by_visitor = {r["visitor_id"]: r for r in rows}

        b1 = by_visitor["V-1001"]
        assert b1["prev_session_id"] == "S-V-1001-1"
        assert b1["prev_last_hit_id"] == "H3"
        assert b1["next_first_hit_id"] == "H6"
        assert b1["prev_last_ts"] == "2026-11-17T08:13:00Z"
        assert b1["next_first_ts"] == "2026-11-17T09:50:00Z"
        assert b1["gap_sec"] == 5820
        assert b1["timeout_sec_snapshot"] == 1800

        b2 = by_visitor["V-1002"]
        assert (b2["prev_last_hit_id"], b2["next_first_hit_id"]) == ("H5", "H9")
        assert b2["gap_sec"] == 4110

        for r in rows:
            check = gbp.verify(conn, r)
            assert check["ok"], check

        # 切开的上段已 closed，下段 open 且以首击点起
        prev = conn.execute(
            "SELECT status, ended_at FROM sessions WHERE id=?", (b1["prev_session_id"],)
        ).fetchone()
        assert prev["status"] == "closed"
        assert prev["ended_at"] == "2026-11-17T08:13:00Z"
        nxt = conn.execute(
            "SELECT status, started_at, hit_count FROM sessions WHERE id=?",
            (b1["next_session_id"],),
        ).fetchone()
        assert nxt["status"] == "open"
        assert nxt["started_at"] == "2026-11-17T09:50:00Z"
        assert nxt["hit_count"] == 2

        # 新访客 V-1003 首访建会话：无切开即无边界
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM gap_boundaries WHERE visitor_id='V-1003'"
        ).fetchone()["c"] == 0
    finally:
        conn.close()


def test_replay_is_idempotent_no_duplicate_boundary(fresh_db):
    assert ingest_svc.run_batch()["ok"]
    before = _counts()
    res = ingest_svc.run_batch()
    assert res["ok"]
    assert res["report"]["written"] == 0
    assert res["report"]["split"] == 0
    assert _counts() == before


def test_boundary_and_split_same_fate(fresh_db, monkeypatch):
    """边界落库失败 → 整批回滚：不得切开无边界。"""
    before = _counts()

    def boom(conn, draft):
        raise RuntimeError("边界落库故障")

    monkeypatch.setattr(gbp, "insert_boundary", boom)
    res = ingest_svc.run_batch()
    assert not res["ok"]
    assert "边界落库故障" in res["error"]
    assert _counts() == before


def test_validate_draft_rejects_three_number_contradiction(fresh_db):
    draft = {
        "visitor_id": "V-X",
        "prev_session_id": "S-1",
        "next_session_id": "S-2",
        "prev_last_hit_id": "HA",
        "next_first_hit_id": "HB",
        "prev_last_ts": "2026-11-17T08:00:00Z",
        "next_first_ts": "2026-11-17T09:00:00Z",
        "gap_sec": 3599,  # 与两点之差 3600 矛盾
        "timeout_sec_snapshot": 1800,
    }
    with pytest.raises(ValueError, match="间隙与两点矛盾"):
        gbp.validate_draft(draft)
    draft["gap_sec"] = 3600
    assert gbp.validate_draft(draft) is draft
    draft["timeout_sec_snapshot"] = 3600  # 未严格超过当时超时
    with pytest.raises(ValueError, match="间隙未超当时超时"):
        gbp.validate_draft(draft)


def test_within_timeout_attaches_without_boundary(fresh_db, tmp_path, monkeypatch):
    batch = {
        "batch_id": "B-close",
        "hits": [
            {
                "id": "H20",
                "visitor_id": "V-1001",
                "event_ts": "2026-11-17T08:20:00Z",  # 距 H3 仅 420s < 1800s
                "page_path": "/cart",
                "event_name": "view",
            }
        ],
    }
    (tmp_path / "ingest_batch.json").write_text(
        json.dumps(batch, ensure_ascii=False), encoding="utf-8"
    )
    monkeypatch.setattr(ingest_svc, "FIXTURES_DIR", tmp_path)
    res = ingest_svc.run_batch()
    assert res["ok"]
    assert res["report"]["split"] == 0
    conn = connect()
    try:
        assert conn.execute("SELECT COUNT(*) AS c FROM gap_boundaries").fetchone()["c"] == 0
        hit = conn.execute("SELECT session_id FROM hits WHERE id='H20'").fetchone()
        assert hit["session_id"] == "S-V-1001-1"
    finally:
        conn.close()


def test_readonly_boundary_api_serves_three_numbers(fresh_db):
    with TestClient(app) as client:
        assert client.post("/api/ingest/run").json()["ok"]
        rows = client.get("/api/gap-boundaries").json()
        assert len(rows) == 2
        for r in rows:
            assert r["check"]["ok"]
            assert r["check"]["gap_sec_recomputed"] == r["gap_sec"]
            assert r["gap_sec"] > r["timeout_sec_snapshot"]
        one = client.get(f"/api/gap-boundaries/{rows[0]['id']}").json()
        assert one["check"]["ok"]
        assert client.get("/api/gap-boundaries/9999").status_code == 404
        # 按会话过滤：prev/next 任一命中
        by_sess = client.get(
            "/api/gap-boundaries", params={"session_id": "S-V-1001-1"}
        ).json()
        assert len(by_sess) == 1 and by_sess[0]["visitor_id"] == "V-1001"
