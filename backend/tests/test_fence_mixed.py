"""夹具一：先合法后非法 —— 非法序不得进有效维与镜，合法序可检测/开段。"""


def test_legal_then_illegal_no_pollution(client):
    # 1) 入档分流
    r = client.post("/api/ingest/run", json={"fixture": "fence_mixed_batch.json"})
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["accepted"] == 1
    assert rep["quarantined"] == 2
    assert set(rep["quarantine_snapshot_ids"]) == {
        "SNAP-FENCE-ILLEGAL-WEST",
        "SNAP-FENCE-EQUAL-NORTH",
    }

    # 2) 贴源快照仍在，但非法序 applied=0 旁路；合法序 applied=1
    snaps = {s["id"]: s for s in client.get("/api/snapshots").json()}
    assert snaps["SNAP-FENCE-LEGAL-EAST"]["applied"] == 1
    assert snaps["SNAP-FENCE-ILLEGAL-WEST"]["applied"] == 0
    assert snaps["SNAP-FENCE-EQUAL-NORTH"]["applied"] == 0

    # 3) 搁置表挂快照 id + 原因 + 关联 run_id + 载荷 + 草稿清理位
    board = client.get("/api/fence/board").json()
    q = {row["snapshot_id"]: row for row in board["搁置队列"]}
    assert q["SNAP-FENCE-ILLEGAL-WEST"]["reason"] == "close_le_open"
    assert q["SNAP-FENCE-EQUAL-NORTH"]["reason"] == "close_le_open"
    for row in q.values():
        assert row["related_run_id"] == rep["batch_id"]
        assert row["payload_json"]  # 留载荷
        assert "draft_cleared" in row
    assert board["计数"]["旁路透照"] == 2

    # 4) 检测只吃合法快照；非法序不出现在变更清单
    d = client.post("/api/detect/run", json={"batch_id": rep["batch_id"]}).json()
    changed_stores = {c["store_id"] for c in d["changes"]}
    assert changed_stores == {"ST-EAST-01"}
    assert d["changed_count"] == 1
    assert d["rejected"] == []  # 非法的在入档已拦，检测根本扫不到

    # 5) 应用 Type2：合法序开新段，非法序门店正式态保持种子态
    before_hist = {h["store_id"]: h for h in client.get("/api/history").json()}
    before_cur = {c["store_id"]: c for c in client.get("/api/current").json()}
    a = client.post("/api/scd/apply", json={"detect_run_id": d["detect_run_id"]}).json()
    assert a["ok"] is True
    assert a["count"] == 1
    assert a["applied"][0]["store_id"] == "ST-EAST-01"

    after_hist = client.get("/api/history").json()
    after_cur = {c["store_id"]: c for c in client.get("/api/current").json()}

    # 合法门店开段：旧行关闭、新行当前
    east = [h for h in after_hist if h["store_id"] == "ST-EAST-01"]
    assert len(east) == 2
    assert [h["is_current"] for h in east] == [0, 1]
    assert after_cur["ST-EAST-01"]["open_time"] == "07:00"

    # 非法门店 hist 行数不变、镜不变（正式态保持冲突前）
    west = [h for h in after_hist if h["store_id"] == "ST-WEST-01"]
    north = [h for h in after_hist if h["store_id"] == "ST-NORTH-01"]
    assert len(west) == 1 and west[0]["id"] == before_hist["ST-WEST-01"]["id"]
    assert len(north) == 1 and north[0]["id"] == before_hist["ST-NORTH-01"]["id"]
    assert after_cur["ST-WEST-01"] == before_cur["ST-WEST-01"]
    assert after_cur["ST-NORTH-01"] == before_cur["ST-NORTH-01"]

    # 6) 无双 is_current；核证全通过
    for store_id in {h["store_id"] for h in after_hist}:
        currents = [h for h in after_hist if h["store_id"] == store_id and h["is_current"]]
        assert len(currents) == 1
    proofs = client.post("/api/proofs/rebuild").json()
    assert proofs["failed"] == 0

    # 7) 只读核证接口可按快照 id 追溯原因与审计流
    board2 = client.get("/api/fence/board").json()
    proof_snaps = {p["snapshot_id"] for p in board2["核证流"]}
    assert "SNAP-FENCE-ILLEGAL-WEST" in proof_snaps
    assert board2["计数"]["未提交草稿"] == 0
    assert board2["计数"]["已提交草稿"] == 1


def test_null_times_not_fenced(client):
    """开闭任一为空不属于本栅栏管辖，不应被搁置。"""
    r = client.post("/api/ingest/run", json={"fixture": "fence_mixed_batch.json"})
    assert r.status_code == 200
    board = client.get("/api/fence/board").json()
    reasons = {row["reason"] for row in board["搁置队列"]}
    assert reasons == {"close_le_open"}
