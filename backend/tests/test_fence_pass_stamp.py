"""序闸放行戳（架构甲）核证：合法序开段同事务落戳；非法序无戳；
删戳 / 分叉后后续写拒绝（区分「以 hist 存在为前置」的乙口径）。"""
import sqlite3


def _db():
    # 延迟导入：conftest 的 client 夹具会清掉 app.* 并重导到隔离 DATA_DIR，
    # 模块级 import connect 会绑到旧库。
    from app.db import connect

    return connect()


def _pipeline(client, fixture="fence_mixed_batch.json"):
    r = client.post("/api/ingest/run", json={"fixture": fixture}).json()
    d = client.post("/api/detect/run", json={"batch_id": r["batch_id"]}).json()
    a = client.post("/api/scd/apply", json={"detect_run_id": d["detect_run_id"]}).json()
    return r, d, a


def test_legal_open_writes_stamp_same_transaction(client):
    r, d, a = _pipeline(client)
    assert a["ok"] is True and a["count"] == 1
    east_hist = a["applied"][0]["hist_id"]
    assert a["applied"][0]["stamp_id"] is not None

    conn = _db()
    try:
        stamp = conn.execute(
            "SELECT * FROM fence_pass_stamps WHERE hist_id=?", (east_hist,)
        ).fetchone()
        hist = conn.execute(
            "SELECT * FROM dim_store_hours_hist WHERE id=?", (east_hist,)
        ).fetchone()
        assert stamp is not None
        assert stamp["store_id"] == hist["store_id"] == "ST-EAST-01"
        assert stamp["open_time"] == hist["open_time"] == "07:00"
        assert stamp["close_time"] == hist["close_time"] == "22:00"
        assert stamp["passed_at"]
    finally:
        conn.close()

    # 核证接口：架构甲口径 + 全绿 + 合法开段有戳
    ls = client.get("/api/fence/pass-stamps").json()
    assert ls["architecture"] == "stamp_precondition"
    assert ls["stamp_ready"] is True
    v = client.get("/api/fence/pass-stamps/verify").json()
    assert v["ok"] is True and v["missing"] == []
    assert east_hist in {s["hist_id"] for s in ls["stamps"]}


def test_illegal_order_never_stamped(client):
    _pipeline(client)
    conn = _db()
    try:
        # 非法门店的当前段仍是种子行；其 hist id 不在戳表里（除种子戳外无新戳）
        west = conn.execute(
            "SELECT id FROM dim_store_hours_hist WHERE store_id='ST-WEST-01' AND is_current=1"
        ).fetchone()["id"]
        north = conn.execute(
            "SELECT id FROM dim_store_hours_hist WHERE store_id='ST-NORTH-01' AND is_current=1"
        ).fetchone()["id"]
        snapped = conn.execute(
            "SELECT COUNT(*) AS c FROM fence_pass_stamps WHERE store_id IN ('ST-WEST-01','ST-NORTH-01')"
        ).fetchone()["c"]
        # 只有建库种子戳，且种子戳对应种子 hist（并非本次非法快照）
        assert snapped == 2
        illegal_snap_hist = conn.execute(
            "SELECT COUNT(*) AS c FROM dim_store_hours_hist WHERE source_snapshot_id IN "
            "('SNAP-FENCE-ILLEGAL-WEST','SNAP-FENCE-EQUAL-NORTH')"
        ).fetchone()["c"]
        assert illegal_snap_hist == 0
        assert west and north
    finally:
        conn.close()
    # 核证接口不得报非法戳
    v = client.get("/api/fence/pass-stamps/verify").json()
    assert v["illegal"] == []


def test_apply_rolls_back_when_stamp_write_is_a_noop(client, monkeypatch):
    """半截失败：开段成功但戳没落 → 整笔回滚，正式态保持冲突前。"""
    from app.modules import fence_pass_stamp as fps

    monkeypatch.setattr(fps, "maybe_stamp", lambda *a, **k: {"ok": True, "stamp_id": None})

    before = {h["store_id"]: h for h in client.get("/api/history").json()}
    before_cur = {c["store_id"]: c for c in client.get("/api/current").json()}
    r = client.post("/api/ingest/run", json={"fixture": "fence_mixed_batch.json"}).json()
    d = client.post("/api/detect/run", json={"batch_id": r["batch_id"]}).json()
    a = client.post("/api/scd/apply", json={"detect_run_id": d["detect_run_id"]}).json()
    assert a["ok"] is False and a["code"] == "fence_pass_stamp_missing"

    after = {h["store_id"]: h for h in client.get("/api/history").json()}
    after_cur = {c["store_id"]: c for c in client.get("/api/current").json()}
    assert after == before
    assert after_cur == before_cur
    conn = _db()
    try:
        n = conn.execute("SELECT COUNT(*) AS c FROM fence_pass_stamps").fetchone()["c"]
    finally:
        conn.close()
    assert n == 6  # 仍只有种子戳


def test_apply_rolls_back_when_stamp_time_diverges(client, monkeypatch):
    """戳上开闭时刻与 hist 分叉 → 整笔回滚。"""
    from app.modules import fence_pass_stamp as fps

    def bad_stamp(conn, store_id, hist_id, open_time, close_time):
        conn.execute(
            "INSERT INTO fence_pass_stamps(store_id, hist_id, open_time, close_time, passed_at)"
            " VALUES (?,?,?,?,?)",
            (store_id, hist_id, open_time or "", "23:59", "x"),
        )
        return {"ok": True, "stamp_id": 999}

    monkeypatch.setattr(fps, "maybe_stamp", bad_stamp)
    before = {h["store_id"]: h for h in client.get("/api/history").json()}
    r = client.post("/api/ingest/run", json={"fixture": "fence_mixed_batch.json"}).json()
    d = client.post("/api/detect/run", json={"batch_id": r["batch_id"]}).json()
    a = client.post("/api/scd/apply", json={"detect_run_id": d["detect_run_id"]}).json()
    assert a["ok"] is False and a["code"] == "fence_pass_stamp_missing"
    after = {h["store_id"]: h for h in client.get("/api/history").json()}
    assert after == before


def test_deleted_stamp_blocks_subsequent_write_and_is_reported(client):
    """删戳后：核证点名缺戳；后续入档 / 检测 / 开段一律拒绝（甲口径）。"""
    r, d, a = _pipeline(client)
    assert a["ok"] is True
    east_hist = a["applied"][0]["hist_id"]

    conn = _db()
    try:
        conn.execute("DELETE FROM fence_pass_stamps WHERE hist_id=?", (east_hist,))
        conn.commit()
    finally:
        conn.close()

    # 1) 只读核证：缺戳且标缺戳段，结论不绿
    v = client.get("/api/fence/pass-stamps/verify").json()
    assert v["ok"] is False
    assert east_hist in {m["hist_id"] for m in v["missing"]}
    assert east_hist in {m["hist_id"] for m in v["unattested_current"]}
    seg = {s["store_id"]: s for s in v["segments"]}
    assert seg["ST-EAST-01"]["stamp_present"] is False
    assert seg["ST-EAST-01"]["hist_exists"] is True
    assert seg["ST-EAST-01"]["subsequent_write"] == "blocked"
    assert seg["ST-EAST-01"]["reason"] == "prior_stamp_missing"
    board = client.get("/api/fence/board").json()
    assert board["计数"]["当前段缺戳"] >= 1

    # 2) 后续入档该键：拒绝（不因 hist 尚在而放行）
    import json as _json
    from app.db import FIXTURES_DIR

    new_fixture = FIXTURES_DIR / "_tmp_stamp_gate.json"
    new_fixture.write_text(
        _json.dumps(
            {
                "snapshots": [
                    {
                        "id": "SNAP-STAMP-GATE-EAST",
                        "store_id": "ST-EAST-01",
                        "as_of_ts": "2026-11-25T00:00:00Z",
                        "open_time": "06:00",
                        "close_time": "23:00",
                        "weekday_mask": "1111100",
                        "biz_status": "open",
                        "payload": {},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    try:
        rep = client.post("/api/ingest/run", json={"fixture": "_tmp_stamp_gate.json"}).json()
        assert rep["accepted"] == 0
        assert rep["stamp_rejected"] == 1
        assert rep["stamp_reject_detail"][0]["reason"] == "prior_stamp_missing"
        proof_events = {
            p["event"] for p in client.get("/api/fence/board").json()["核证流"]
            if p["snapshot_id"] == "SNAP-STAMP-GATE-EAST"
        }
        assert "stamp_precondition_reject" in proof_events
    finally:
        new_fixture.unlink(missing_ok=True)

    # 3) 后续检测：即便快照 applied=1 存在，也拒绝开段（不产草稿）
    conn = _db()
    try:
        conn.execute(
            """INSERT OR IGNORE INTO source_snapshots(
                 id, batch_id, store_id, as_of_ts, open_time, close_time,
                 weekday_mask, biz_status, payload_json, row_hash, applied
               ) VALUES ('SNAP-STAMP-DETECT-EAST','B-X','ST-EAST-01','2026-11-26T00:00:00Z',
                 '06:00','23:00','1111100','open','{}','deadbeef',1)"""
        )
        conn.commit()
    finally:
        conn.close()
    d2 = client.post("/api/detect/run", json={}).json()
    assert all(c["store_id"] != "ST-EAST-01" for c in d2["changes"])
    assert any(
        rej.get("reason") == "prior_stamp_missing" for rej in d2["rejected"]
    )

    # 4) 补回戳后恢复：可继续入档 / 检测
    conn = _db()
    try:
        hist = conn.execute(
            "SELECT * FROM dim_store_hours_hist WHERE id=?", (east_hist,)
        ).fetchone()
        conn.execute(
            "INSERT INTO fence_pass_stamps(store_id, hist_id, open_time, close_time, passed_at)"
            " VALUES (?,?,?,?,?)",
            (hist["store_id"], east_hist, hist["open_time"], hist["close_time"], "manual"),
        )
        conn.commit()
    finally:
        conn.close()
    v2 = client.get("/api/fence/pass-stamps/verify").json()
    assert v2["ok"] is True


def test_stamp_tamper_is_flagged_and_blocks(client):
    """戳上时刻被串改 → 核证点名 time_mismatch，后续写拒绝。"""
    r, d, a = _pipeline(client)
    east_hist = a["applied"][0]["hist_id"]
    conn = _db()
    try:
        conn.execute(
            "UPDATE fence_pass_stamps SET close_time='23:59' WHERE hist_id=?", (east_hist,)
        )
        conn.commit()
    finally:
        conn.close()
    v = client.get("/api/fence/pass-stamps/verify").json()
    assert v["ok"] is False
    assert east_hist in {m["hist_id"] for m in v["time_mismatch"]}
    seg = {s["store_id"]: s for s in v["segments"]}
    assert seg["ST-EAST-01"]["subsequent_write"] == "blocked"


def test_neighbor_stamp_not_cross_written(client):
    """邻键戳不被串改：每店戳的 hist_id / 门店 一一对应，不互相覆盖。"""
    r, d, a = _pipeline(client)
    east_hist = a["applied"][0]["hist_id"]
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT * FROM fence_pass_stamps ORDER BY id"
        ).fetchall()
        pairs = {(s["store_id"], s["hist_id"]) for s in rows}
        assert len(pairs) == len(rows)  # hist_id 唯一索引保证
        # 东城戳必须指向东城 hist
        east = conn.execute(
            "SELECT * FROM fence_pass_stamps WHERE hist_id=?", (east_hist,)
        ).fetchone()
        hist = conn.execute(
            "SELECT * FROM dim_store_hours_hist WHERE id=?", (east_hist,)
        ).fetchone()
        assert east["store_id"] == hist["store_id"]
    finally:
        conn.close()


def test_chained_open_writes_new_stamp_and_keeps_old(client):
    """每次合法开段都落各自戳；旧段戳保留，新段戳与 hist 互证。"""
    import json as _json

    from app.db import FIXTURES_DIR

    r, d, a = _pipeline(client)
    first_hist = a["applied"][0]["hist_id"]

    fx = FIXTURES_DIR / "_tmp_chain.json"
    fx.write_text(
        _json.dumps(
            {
                "snapshots": [
                    {
                        "id": "SNAP-CHAIN-EAST",
                        "store_id": "ST-EAST-01",
                        "as_of_ts": "2026-11-27T00:00:00Z",
                        "open_time": "08:00",
                        "close_time": "20:00",
                        "weekday_mask": "1111100",
                        "biz_status": "open",
                        "payload": {},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    try:
        r2 = client.post("/api/ingest/run", json={"fixture": "_tmp_chain.json"}).json()
        assert r2["accepted"] == 1 and r2["stamp_rejected"] == 0
        d2 = client.post("/api/detect/run", json={"batch_id": r2["batch_id"]}).json()
        a2 = client.post("/api/scd/apply", json={"detect_run_id": d2["detect_run_id"]}).json()
    finally:
        fx.unlink(missing_ok=True)

    assert a2["ok"] is True and a2["count"] == 1
    second_hist = a2["applied"][0]["hist_id"]
    assert second_hist != first_hist

    conn = _db()
    try:
        stamps = {
            s["hist_id"]: s
            for s in conn.execute(
                "SELECT * FROM fence_pass_stamps WHERE store_id='ST-EAST-01'"
            ).fetchall()
        }
        assert first_hist in stamps and second_hist in stamps
        assert stamps[second_hist]["open_time"] == "08:00"
        assert stamps[second_hist]["close_time"] == "20:00"
    finally:
        conn.close()
    v = client.get("/api/fence/pass-stamps/verify").json()
    assert v["ok"] is True
    seg = {s["store_id"]: s for s in v["segments"]}
    assert seg["ST-EAST-01"]["hist_id"] == second_hist
    assert seg["ST-EAST-01"]["subsequent_write"] == "allowed"
