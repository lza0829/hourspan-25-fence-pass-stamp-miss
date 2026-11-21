"""夹具二：先开 Type2 草稿，再灌非法序 —— 草稿须清理，正式态保持冲突前。"""


def test_draft_then_illegal_clears_draft_and_keeps_formal_state(client):
    # 0) 记录冲突前正式态
    cur_before = {c["store_id"]: c for c in client.get("/api/current").json()}
    hist_before = client.get("/api/history", params={"store_id": "ST-EAST-01"}).json()
    assert len(hist_before) == 1 and hist_before[0]["is_current"] == 1

    # 1) 先让合法变更检测开跑：产生未提交 Type2 草稿（不 apply）
    r = client.post("/api/ingest/run", json={"fixture": "fence_mixed_batch.json"}).json()
    d = client.post("/api/detect/run", json={"batch_id": r["batch_id"]}).json()
    east_changes = [c for c in d["changes"] if c["store_id"] == "ST-EAST-01"]
    assert east_changes and d["status"] == "open"

    drafts = client.get("/api/fence/drafts", params={"store_id": "ST-EAST-01"}).json()
    assert len(drafts) == 1 and drafts[0]["status"] == "open"
    draft_id = drafts[0]["id"]

    # 2) 灌入针对同一键的非法序快照：入档即拦 + 失败闭甲清草稿
    bad = client.post(
        "/api/ingest/run", json={"fixture": "fence_draft_then_illegal.json"}
    ).json()
    assert bad["accepted"] == 0 and bad["quarantined"] == 1
    bad_snap = bad["quarantine_detail"][0]["snapshot_id"]
    assert bad["quarantine_detail"][0]["draft_cleared"] is True
    assert draft_id in bad["quarantine_detail"][0]["cleared_draft_ids"]

    # 3) 未提交草稿已清理（discarded），无 open 残留
    drafts_after = client.get("/api/fence/drafts", params={"store_id": "ST-EAST-01"}).json()
    assert drafts_after == []
    board = client.get("/api/fence/board").json()
    draft_rows = {x["id"]: x for x in board["草稿"]}
    assert draft_rows[draft_id]["status"] == "discarded"

    # 4) 搁置行带草稿清理位；核证流含 quarantined 与 draft_cleared 两事件
    q = {row["snapshot_id"]: row for row in board["搁置队列"]}
    assert q[bad_snap]["draft_cleared"] == 1
    assert draft_id in q[bad_snap]["cleared_draft_ids_json"]
    events = {p["event"] for p in board["核证流"] if p["snapshot_id"] == bad_snap}
    assert {"quarantined", "draft_cleared"} <= events

    # 5) 正式态保持冲突前：hist 仍只有种子行且当前，镜未动
    hist_after = client.get("/api/history", params={"store_id": "ST-EAST-01"}).json()
    assert hist_after == hist_before
    cur_after = {c["store_id"]: c for c in client.get("/api/current").json()}
    assert cur_after["ST-EAST-01"] == cur_before["ST-EAST-01"]

    # 6) 对原检测跑次 apply：草稿已被清，无可应用项，正式态仍不变
    a = client.post("/api/scd/apply", json={"detect_run_id": d["detect_run_id"]}).json()
    assert a["ok"] is True and a["count"] == 0
    hist_final = client.get("/api/history", params={"store_id": "ST-EAST-01"}).json()
    assert hist_final == hist_before

    # 7) 无双 is_current，镜中无非法开闭，核证全绿
    all_hist = client.get("/api/history").json()
    for sid in {h["store_id"] for h in all_hist}:
        assert len([h for h in all_hist if h["store_id"] == sid and h["is_current"]]) == 1
    proofs = client.post("/api/proofs/rebuild").json()
    assert proofs["failed"] == 0
    final_board = client.get("/api/fence/board").json()
    assert final_board["计数"]["未提交草稿"] == 0
    assert final_board["计数"]["已废弃草稿"] == 1


def test_reingest_same_illegal_snapshot_is_idempotent(client):
    client.post("/api/ingest/run", json={"fixture": "fence_draft_then_illegal.json"})
    again = client.post(
        "/api/ingest/run", json={"fixture": "fence_draft_then_illegal.json"}
    ).json()
    # 同快照 INSERT OR IGNORE 跳过，不重复搁置
    assert again["quarantined"] == 0
    board = client.get("/api/fence/board").json()
    rows = [q for q in board["搁置队列"] if q["snapshot_id"] == "SNAP-FENCE-DRAFT-ILLEGAL-EAST"]
    assert len(rows) == 1
