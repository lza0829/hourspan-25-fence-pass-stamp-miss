"""底座 happy path 回归：原变更夹具全链路不受序栅栏影响。"""


def test_base_pipeline_still_green(client):
    r = client.post("/api/ingest/run", json={"fixture": "ingest_change_batch.json"}).json()
    assert r["accepted"] == 2 and r["quarantined"] == 0

    d = client.post("/api/detect/run", json={"batch_id": r["batch_id"]}).json()
    assert d["changed_count"] == 2 and d["rejected"] == []
    assert len(d["draft_ids"]) == 2

    a = client.post("/api/scd/apply", json={"detect_run_id": d["detect_run_id"]}).json()
    assert a["ok"] is True and a["count"] == 2 and a["rejected"] == []

    board = client.get("/api/fence/board").json()
    assert board["计数"]["搁置总数"] == 0
    assert board["计数"]["未提交草稿"] == 0
    assert board["计数"]["已提交草稿"] == 2

    proofs = client.post("/api/proofs/rebuild").json()
    assert proofs["failed"] == 0

    # 检测跑次收口状态
    runs = {x["id"]: x for x in client.get("/api/detect-runs").json()}
    assert runs[d["detect_run_id"]]["status"] == "applied"


def test_detect_default_mode_skips_bypassed_snapshots(client):
    # 非法序快照即便成了某店最新快照，默认检测也不得扫到
    client.post("/api/ingest/run", json={"fixture": "fence_draft_then_illegal.json"})
    d = client.post("/api/detect/run", json={}).json()
    assert all(c["store_id"] != "ST-EAST-01" for c in d["changes"])
    assert d["rejected"] == []
