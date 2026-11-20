"""切边已落、间隙水位必同成败抬升；水位与边 gap 互证，分叉须失败。"""
import json

import pytest
from fastapi.testclient import TestClient

from app.db import connect
from app.main import app
from app.modules import gap_boundary_proof as gbp
from app.services import ingest as ingest_svc


def _wm_rows():
    conn = connect()
    try:
        return {
            r["edge_id"]: dict(r)
            for r in conn.execute("SELECT * FROM gap_watermarks").fetchall()
        }
    finally:
        conn.close()


def test_watermark_lifts_to_edge_gap_on_split(fresh_db):
    res = ingest_svc.run_batch()
    assert res["ok"], res
    assert res["report"]["split"] == 2

    conn = connect()
    try:
        edges = {r["id"]: r for r in gbp.list_boundaries(conn)}
        wms = _wm_rows()
        assert set(wms) == set(edges)
        for edge_id, edge in edges.items():
            wm = wms[edge_id]
            assert wm["visitor_id"] == edge["visitor_id"]
            assert wm["watermark_sec"] == edge["gap_sec"]
            assert wm["visitor_edge_key"] == f"{edge['visitor_id']}:{edge['prev_session_id']}->{edge['next_session_id']}"
            assert wm["trigger_hit_id"] == edge["next_first_hit_id"]
        by_v = {w["visitor_id"]: w["watermark_sec"] for w in wms.values()}
        assert by_v == {"V-1001": 5820, "V-1002": 4110}
    finally:
        conn.close()


def test_certificate_rows_read_watermark_and_edge_both_ways(fresh_db):
    assert ingest_svc.run_batch()["ok"]
    conn = connect()
    try:
        cert = gbp.watermark_certificate(conn)
        assert len(cert) == 2
        for row in cert:
            assert row["ok"], row
            assert row["watermark_sec"] == row["gap_sec"]
            assert row["checks"]["watermark_found"]
            assert row["checks"]["watermark_visitor_matches_edge"]
            assert row["checks"]["edge_key_matches"]
            assert row["checks"]["trigger_matches_edge"]
        by_v = {r["visitor_id"]: r for r in cert}
        assert by_v["V-1001"]["watermark_sec"] == 5820
        assert by_v["V-1001"]["edge_key"].startswith("V-1001:S-V-1001-1->")
        # 按访客过滤
        one = gbp.watermark_certificate(conn, "V-1002")
        assert len(one) == 1 and one[0]["visitor_id"] == "V-1002"
    finally:
        conn.close()


def test_watermark_split_from_edge_fails(fresh_db, monkeypatch):
    """人为让水位值与边 gap 分叉 → 整批回滚，不得切边成功而水位错位。"""
    before_conn = connect()
    try:
        before = {
            t: before_conn.execute(f"SELECT COUNT(*) AS c FROM {t}").fetchone()["c"]
            for t in ("sessions", "hits", "gap_boundaries", "gap_watermarks")
        }
    finally:
        before_conn.close()

    real_lift = gbp.lift_watermark

    def bad_lift(conn, boundary_id, d):
        bad = dict(d)
        bad["gap_sec"] = int(d["gap_sec"]) + 1  # 与边 gap_sec 分叉
        return real_lift(conn, boundary_id, bad)

    monkeypatch.setattr(gbp, "lift_watermark", bad_lift)
    res = ingest_svc.run_batch()
    assert not res["ok"]
    assert "分叉" in res["error"]
    after = _wm_rows()
    assert after == {}
    conn = connect()
    try:
        assert conn.execute("SELECT COUNT(*) AS c FROM gap_boundaries").fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) AS c FROM hits").fetchone()["c"] == before["hits"]
    finally:
        conn.close()


def test_watermark_missing_after_edge_fails(fresh_db, monkeypatch):
    """切边已落而水位未抬 → 终检抛错，整批回滚。"""
    def no_lift(conn, boundary_id, d):
        return None

    monkeypatch.setattr(gbp, "lift_watermark", no_lift)
    res = ingest_svc.run_batch()
    assert not res["ok"]
    assert "水位未抬" in res["error"] or "水位" in res["error"]
    assert _wm_rows() == {}
    conn = connect()
    try:
        assert conn.execute("SELECT COUNT(*) AS c FROM gap_boundaries").fetchone()["c"] == 0
    finally:
        conn.close()


def test_watermark_wrong_visitor_fails(fresh_db, monkeypatch):
    """水位挂错访客 → 对撞抛错，整批回滚。"""
    real_lift = gbp.lift_watermark

    def wrong_visitor_lift(conn, boundary_id, d):
        wrong = dict(d)
        wrong["visitor_id"] = "V-9999"
        return real_lift(conn, boundary_id, wrong)

    monkeypatch.setattr(gbp, "lift_watermark", wrong_visitor_lift)
    res = ingest_svc.run_batch()
    assert not res["ok"]
    assert "水位挂错访客" in res["error"] or "切边不存在" in res["error"]
    assert _wm_rows() == {}


def test_certificate_flags_watermark_edge_divergence(fresh_db):
    """直接改库让水位与边分叉：核证页须判失败。"""
    assert ingest_svc.run_batch()["ok"]
    conn = connect()
    try:
        conn.execute(
            "UPDATE gap_watermarks SET watermark_sec=watermark_sec+7 WHERE visitor_id='V-1001'"
        )
        conn.commit()
        rows = gbp.watermark_certificate(conn)
        by_v = {r["visitor_id"]: r for r in rows}
        assert by_v["V-1001"]["ok"] is False
        assert by_v["V-1001"]["checks"]["watermark_equals_edge_gap"] is False
        assert by_v["V-1002"]["ok"] is True
    finally:
        conn.close()


def test_certificate_flags_watermark_wrong_visitor_row(fresh_db):
    """把 V-1001 的水位行改挂到 V-1002 的边：核证须暴露挂错访客。"""
    assert ingest_svc.run_batch()["ok"]
    conn = connect()
    try:
        edge_v1 = conn.execute(
            "SELECT * FROM gap_boundaries WHERE visitor_id='V-1001'"
        ).fetchone()
        edge_v2 = conn.execute(
            "SELECT * FROM gap_boundaries WHERE visitor_id='V-1002'"
        ).fetchone()
        # 把 V-1002 那边上的水位行改成 V-1001 的访客，模拟挂错访客
        conn.execute(
            "UPDATE gap_watermarks SET visitor_id='V-1001' WHERE edge_id=?",
            (edge_v2["id"],),
        )
        conn.commit()
        rows = {r["edge_id"]: r for r in gbp.watermark_certificate(conn)}
        assert rows[edge_v2["id"]]["ok"] is False
        assert rows[edge_v2["id"]]["checks"]["watermark_visitor_matches_edge"] is False
        # V-1001 自己的边仍正位
        assert rows[edge_v1["id"]]["ok"] is True
    finally:
        conn.close()


def test_certificate_flags_missing_watermark_for_edge(fresh_db):
    """边在、水位缺：核证页须标「未抬」。"""
    assert ingest_svc.run_batch()["ok"]
    conn = connect()
    try:
        edge = conn.execute(
            "SELECT * FROM gap_boundaries WHERE visitor_id='V-1001'"
        ).fetchone()
        conn.execute("DELETE FROM gap_watermarks WHERE edge_id=?", (edge["id"],))
        conn.commit()
        rows = {r["edge_id"]: r for r in gbp.watermark_certificate(conn)}
        row = rows[edge["id"]]
        assert row["ok"] is False
        assert row["checks"]["watermark_found"] is False
        assert row["watermark_sec"] is None
    finally:
        conn.close()


def test_within_timeout_does_not_touch_watermark(fresh_db, tmp_path, monkeypatch):
    batch = {
        "batch_id": "B-close",
        "hits": [
            {
                "id": "H20",
                "visitor_id": "V-1001",
                "event_ts": "2026-11-17T08:20:00Z",
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
    assert res["ok"] and res["report"]["split"] == 0
    assert _wm_rows() == {}


def test_watermark_readonly_api_serves_certificates(fresh_db):
    with TestClient(app) as client:
        assert client.post("/api/ingest/run").json()["ok"]
        cert = client.get("/api/gap-watermark-certificates").json()
        assert cert["ok"] is True
        assert len(cert["rows"]) == 2
        for r in cert["rows"]:
            assert r["ok"] and r["watermark_sec"] == r["gap_sec"]
            assert r["conclusion"] == "水位=边 gap"
        one = client.get(
            "/api/gap-watermark-certificates", params={"visitor_id": "V-1001"}
        ).json()
        assert one["ok"] and len(one["rows"]) == 1
        wm = client.get("/api/gap-watermark", params={"visitor_id": "V-1001"}).json()
        assert wm["watermark_sec"] == 5820
        assert wm["trigger_hit_id"] == "H6"
        all_wm = client.get("/api/gap-watermark").json()
        assert len(all_wm["watermarks"]) == 2
        # 边界页读路径同时可见切边 gap，与水位值一致
        edges = client.get("/api/gap-boundaries").json()
        assert {e["visitor_id"]: e["gap_sec"] for e in edges} == {
            r["visitor_id"]: r["watermark_sec"] for r in cert["rows"]
        }
        assert client.get("/api/overview").json()["gap_watermarks"] == 2


def test_seed_without_gap_has_no_watermark(fresh_db):
    assert _wm_rows() == {}
    conn = connect()
    try:
        assert gbp.watermark_certificate(conn) == []
        # 读路径不得回落 0 冒充对齐
        empty = gbp.gap_watermark(conn, "V-1001")
        assert empty["watermark_sec"] is None
        assert empty["edge_id"] is None
    finally:
        conn.close()


def test_watermark_api_does_not_fall_back_to_zero(fresh_db):
    with TestClient(app) as client:
        wm = client.get("/api/gap-watermark", params={"visitor_id": "V-1001"}).json()
        assert wm["watermark_sec"] is None
        cert = client.get("/api/gap-watermark-certificates").json()
        assert cert == {"rows": [], "ok": True}


def test_each_edge_of_same_visitor_has_own_watermark(fresh_db, tmp_path, monkeypatch):
    """同一访客连续切开两段：每条边各自一行水位，逐边与 gap 相等。"""
    batch = {
        "batch_id": "B-multi",
        "hits": [
            {
                "id": "H30",
                "visitor_id": "V-1001",
                "event_ts": "2026-11-17T10:00:00Z",
                "page_path": "/a",
                "event_name": "view",
            },
            {
                "id": "H31",
                "visitor_id": "V-1001",
                "event_ts": "2026-11-17T11:10:00Z",
                "page_path": "/b",
                "event_name": "view",
            },
        ],
    }
    (tmp_path / "ingest_batch.json").write_text(
        json.dumps(batch, ensure_ascii=False), encoding="utf-8"
    )
    monkeypatch.setattr(ingest_svc, "FIXTURES_DIR", tmp_path)
    res = ingest_svc.run_batch()
    assert res["ok"] and res["report"]["split"] == 2

    conn = connect()
    try:
        edges = gbp.list_boundaries(conn, visitor_id="V-1001")
        assert len(edges) == 2
        wms = _wm_rows()
        assert len(wms) == 2
        for edge in edges:
            assert int(wms[edge["id"]]["watermark_sec"]) == int(edge["gap_sec"])
        cert = gbp.watermark_certificate(conn, "V-1001")
        assert len(cert) == 2 and all(r["ok"] for r in cert)
        # 第一刀：H3@08:13 → H30@10:00 = 6420s；第二刀：H30 → H31@11:10 = 4200s
        assert {r["watermark_sec"] for r in cert} == {6420, 4200}
        # 访客水位读路径给最新一条，并附全部水位快照
        latest = gbp.gap_watermark(conn, "V-1001")
        assert latest["watermark_sec"] == 4200
        assert len(latest["watermarks"]) == 2
    finally:
        conn.close()


def test_replay_does_not_duplicate_or_rewrite_watermarks(fresh_db):
    assert ingest_svc.run_batch()["ok"]
    before = _wm_rows()
    assert ingest_svc.run_batch()["ok"]
    assert _wm_rows() == before
