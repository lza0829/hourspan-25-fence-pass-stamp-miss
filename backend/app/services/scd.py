import json
from uuid import uuid4

from app.db import connect
from app.modules.open_close_order_fence import (
    DRAFT_APPLIED,
    DRAFT_DISCARDED,
    DRAFT_OPEN,
    assert_snapshot_writable,
    check_order,
    mark_drafts,
)
from app.seed import now_iso


def apply_type2(detect_run_id: str):
    """按检测跑次的未提交草稿关旧行/开新行并刷新当前镜。

    序栅栏协同（历史维写闸）：每条草稿对应的快照在写 hist 前再次复检
    开闭序；非法序当场搁置并废弃该草稿，绝不写 hist / 镜。全部处理完
    才把跑次置为 applied，保证无半截态：成功提交的草稿才对应新 hist 行，
    被拦的草稿留废弃痕迹，正式态保持冲突前。
    """
    conn = connect()
    try:
        run = conn.execute(
            "SELECT * FROM detect_runs WHERE id=?", (detect_run_id,)
        ).fetchone()
        if not run:
            return {"ok": False, "message": "检测跑次不存在"}

        drafts = conn.execute(
            "SELECT * FROM scd_drafts WHERE detect_run_id=? AND status=? ORDER BY id",
            (detect_run_id, DRAFT_OPEN),
        ).fetchall()
        if not drafts:
            return {
                "ok": True,
                "applied": [],
                "count": 0,
                "detect_run_id": detect_run_id,
                "message": "无未提交草稿（可能已应用或已被序栅栏清理）",
            }

        applied = []
        rejected = []
        for draft in drafts:
            store_id = draft["store_id"]
            snap = conn.execute(
                "SELECT * FROM source_snapshots WHERE id=?", (draft["snapshot_id"],)
            ).fetchone()
            if not snap:
                conn.execute(
                    "UPDATE scd_drafts SET status=? WHERE id=?",
                    (DRAFT_DISCARDED, draft["id"]),
                )
                rejected.append({"draft_id": draft["id"], "reason": "snapshot_missing"})
                continue

            # 写闸复检：非法序快照永不进 hist / 镜。
            writable, guard = assert_snapshot_writable(
                conn, snap, related_run_id=detect_run_id
            )
            if not writable:
                conn.execute(
                    "UPDATE scd_drafts SET status=? WHERE id=?",
                    (DRAFT_DISCARDED, draft["id"]),
                )
                rejected.append({"draft_id": draft["id"], **guard})
                continue

            cur_hist = conn.execute(
                "SELECT * FROM dim_store_hours_hist WHERE store_id=? AND is_current=1",
                (store_id,),
            ).fetchone()
            valid_from = snap["as_of_ts"]
            if cur_hist:
                conn.execute(
                    """UPDATE dim_store_hours_hist
                       SET valid_to=?, is_current=0, closed_by_run=?
                       WHERE id=?""",
                    (valid_from, detect_run_id, cur_hist["id"]),
                )
            new_id = f"HIST-{uuid4().hex[:10]}"
            rh = snap["row_hash"]
            conn.execute(
                """INSERT INTO dim_store_hours_hist(
                  id, store_id, open_time, close_time, weekday_mask, biz_status,
                  valid_from, valid_to, is_current, source_snapshot_id, row_hash, closed_by_run
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    new_id,
                    store_id,
                    snap["open_time"],
                    snap["close_time"],
                    snap["weekday_mask"],
                    snap["biz_status"],
                    valid_from,
                    None,
                    1,
                    snap["id"],
                    rh,
                    None,
                ),
            )
            conn.execute(
                """INSERT INTO dim_store_hours_current(
                  store_id, open_time, close_time, weekday_mask, biz_status,
                  hist_id, as_of_ts, row_hash
                ) VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(store_id) DO UPDATE SET
                  open_time=excluded.open_time,
                  close_time=excluded.close_time,
                  weekday_mask=excluded.weekday_mask,
                  biz_status=excluded.biz_status,
                  hist_id=excluded.hist_id,
                  as_of_ts=excluded.as_of_ts,
                  row_hash=excluded.row_hash
                """,
                (
                    store_id,
                    snap["open_time"],
                    snap["close_time"],
                    snap["weekday_mask"],
                    snap["biz_status"],
                    new_id,
                    valid_from,
                    rh,
                ),
            )
            applied.append(
                {
                    "store_id": store_id,
                    "hist_id": new_id,
                    "closed": cur_hist["id"] if cur_hist else None,
                    "draft_id": draft["id"],
                }
            )

        # 提交成功的草稿置 applied（被拦的已是 discarded）；跑次收口。
        mark_drafts(conn, detect_run_id, DRAFT_APPLIED)
        conn.execute(
            "UPDATE detect_runs SET status='applied' WHERE id=?", (detect_run_id,)
        )

        # 提交前不变量自检：每店至多一条 is_current；任一镜指向非法序即整体回滚。
        doubles = conn.execute(
            """SELECT store_id, COUNT(*) AS c FROM dim_store_hours_hist
               WHERE is_current=1 GROUP BY store_id HAVING c > 1"""
        ).fetchall()
        if doubles:
            conn.rollback()
            return {
                "ok": False,
                "message": "双 is_current 不变量被破坏，整趟回滚",
                "stores": [r["store_id"] for r in doubles],
            }
        bad_mirror = conn.execute(
            "SELECT store_id, open_time, close_time FROM dim_store_hours_current"
        ).fetchall()
        for m in bad_mirror:
            ok, reason = check_order(m["open_time"], m["close_time"])
            if not ok:
                conn.rollback()
                return {
                    "ok": False,
                    "message": f"镜出现非法开闭序({reason})，整趟回滚",
                    "store_id": m["store_id"],
                }

        conn.commit()
        return {
            "ok": True,
            "applied": applied,
            "count": len(applied),
            "rejected": rejected,
            "detect_run_id": detect_run_id,
        }
    finally:
        conn.close()


def rebuild_proofs():
    """Attest: each current mirror hist_id must point to is_current=1 hist with same hash."""
    conn = connect()
    now = now_iso()
    try:
        mirrors = conn.execute("SELECT * FROM dim_store_hours_current ORDER BY store_id").fetchall()
        results = []
        for m in mirrors:
            hist = conn.execute(
                "SELECT * FROM dim_store_hours_hist WHERE id=?", (m["hist_id"],)
            ).fetchone()
            valid = 1
            detail = {}
            if not hist:
                valid = 0
                detail["error"] = "mirror_hist_missing"
            elif hist["is_current"] != 1:
                valid = 0
                detail["error"] = "mirror_not_current_hist"
            elif hist["row_hash"] != m["row_hash"]:
                valid = 0
                detail["error"] = "hash_mismatch"
            elif hist["store_id"] != m["store_id"]:
                valid = 0
                detail["error"] = "store_mismatch"
            else:
                detail["ok"] = True
            conn.execute(
                """INSERT INTO scd_proofs(
                  store_id, hist_id, mirror_hist_id, row_hash, valid, detail_json, created_at
                ) VALUES (?,?,?,?,?,?,?)""",
                (
                    m["store_id"],
                    hist["id"] if hist else "",
                    m["hist_id"],
                    m["row_hash"],
                    valid,
                    json.dumps(detail, ensure_ascii=False),
                    now,
                ),
            )
            results.append({"store_id": m["store_id"], "valid": bool(valid), "detail": detail})
        conn.commit()
        return {
            "checked": len(results),
            "failed": sum(1 for r in results if not r["valid"]),
            "results": results,
        }
    finally:
        conn.close()

# fence_pass_not_persisted on successful open path
