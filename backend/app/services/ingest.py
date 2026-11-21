import json
from uuid import uuid4

from app.db import FIXTURES_DIR, connect
from app.modules.open_close_order_fence import (
    EVENT_QUARANTINED,
    check_order,
    quarantine_snapshot,
)
from app.seed import now_iso, row_hash


def run_batch(fixture_name: str = "ingest_change_batch.json"):
    path = FIXTURES_DIR / fixture_name
    if not path.exists():
        return {"batch_id": None, "accepted": 0, "message": "无夹具"}
    data = json.loads(path.read_text(encoding="utf-8"))
    snaps = data.get("snapshots", [])
    batch_id = f"B-{uuid4().hex[:8]}"
    now = now_iso()
    conn = connect()
    accepted = 0
    skipped = 0
    quarantined = []
    try:
        for s in snaps:
            store = conn.execute("SELECT id FROM stores WHERE id=?", (s["store_id"],)).fetchone()
            if not store:
                skipped += 1
                continue
            ot, ct = s.get("open_time"), s.get("close_time")
            wm = s.get("weekday_mask", "1111100")
            st = s.get("biz_status", "open")
            rh = row_hash(ot, ct, wm, st)
            snap_id = s.get("id") or f"SNAP-{uuid4().hex[:10]}"

            # 失败闭甲·入口分流：非法序快照入档即拦，不进有效维/检测/镜。
            order_ok, reason = check_order(ot, ct)

            cur = conn.execute(
                """INSERT OR IGNORE INTO source_snapshots(
                  id, batch_id, store_id, as_of_ts, open_time, close_time,
                  weekday_mask, biz_status, payload_json, row_hash, applied
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    snap_id,
                    batch_id,
                    s["store_id"],
                    s["as_of_ts"],
                    ot,
                    ct,
                    wm,
                    st,
                    json.dumps(s.get("payload", {}), ensure_ascii=False),
                    rh,
                    1 if order_ok else 0,
                ),
            )
            if cur.rowcount == 0:
                skipped += 1
                continue

            if not order_ok:
                snap_row = conn.execute(
                    "SELECT * FROM source_snapshots WHERE id=?", (snap_id,)
                ).fetchone()
                info = quarantine_snapshot(
                    conn,
                    snap=snap_row,
                    reason=reason,
                    related_run_id=batch_id,
                    event=EVENT_QUARANTINED,
                    detail={"fixture": fixture_name, "as_of_ts": s["as_of_ts"]},
                )
                quarantined.append(info)
                continue

            accepted += 1

        report = {
            "accepted": accepted,
            "skipped": skipped,
            "quarantined": len(quarantined),
            "total": len(snaps),
            "quarantine_snapshot_ids": [q["snapshot_id"] for q in quarantined],
        }
        conn.execute(
            "INSERT INTO ingest_batches(id, created_at, report_json) VALUES (?,?,?)",
            (batch_id, now, json.dumps(report, ensure_ascii=False)),
        )
        conn.commit()
        return {"batch_id": batch_id, **report, "quarantine_detail": quarantined}
    finally:
        conn.close()
