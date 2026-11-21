import json
from uuid import uuid4

from app.db import connect
from app.modules.open_close_order_fence import (
    assert_snapshot_writable,
    create_draft,
)
from app.seed import now_iso


def run_detect(batch_id: str | None = None):
    """对照当前镜与可应用快照的 row_hash 产出变更清单（new / attr_change）。

    序栅栏协同：只吃 applied=1 的快照；入口再复检开闭序，非法序当场搁置
    并剔除，绝不产出草稿。每条变更同时落一条 status='open' 的 Type2 草稿，
    供 scd.apply 提交或被后续非法序快照清理（失败闭甲）。
    """
    conn = connect()
    run_id = f"DET-{uuid4().hex[:8]}"
    now = now_iso()
    try:
        if batch_id:
            snaps = conn.execute(
                """SELECT * FROM source_snapshots
                   WHERE batch_id=? AND applied=1
                   ORDER BY store_id, as_of_ts""",
                (batch_id,),
            ).fetchall()
        else:
            snaps = conn.execute(
                """
                SELECT s.* FROM source_snapshots s
                JOIN (
                  SELECT store_id, MAX(as_of_ts) AS mx FROM source_snapshots
                  WHERE applied=1 GROUP BY store_id
                ) t ON s.store_id=t.store_id AND s.as_of_ts=t.mx
                WHERE s.applied=1
                ORDER BY s.store_id
                """
            ).fetchall()

        changes = []
        rejected = []
        draft_ids = []
        for snap in snaps:
            # 入口复检兜底：即使旁路标记异常，写闸前仍按规则拦截。
            writable, guard = assert_snapshot_writable(
                conn, snap, related_run_id=run_id
            )
            if not writable:
                rejected.append(guard)
                continue

            cur = conn.execute(
                "SELECT * FROM dim_store_hours_current WHERE store_id=?",
                (snap["store_id"],),
            ).fetchone()
            if not cur:
                change = {
                    "store_id": snap["store_id"],
                    "kind": "new",
                    "snapshot_id": snap["id"],
                    "from_hash": None,
                    "to_hash": snap["row_hash"],
                }
            elif cur["row_hash"] != snap["row_hash"]:
                change = {
                    "store_id": snap["store_id"],
                    "kind": "attr_change",
                    "snapshot_id": snap["id"],
                    "from_hash": cur["row_hash"],
                    "to_hash": snap["row_hash"],
                    "from": {
                        "open_time": cur["open_time"],
                        "close_time": cur["close_time"],
                        "weekday_mask": cur["weekday_mask"],
                        "biz_status": cur["biz_status"],
                    },
                    "to": {
                        "open_time": snap["open_time"],
                        "close_time": snap["close_time"],
                        "weekday_mask": snap["weekday_mask"],
                        "biz_status": snap["biz_status"],
                    },
                }
            else:
                continue
            changes.append(change)
            draft_ids.append(
                create_draft(
                    conn,
                    detect_run_id=run_id,
                    store_id=change["store_id"],
                    snapshot_id=change["snapshot_id"],
                    kind=change["kind"],
                    from_hash=change["from_hash"],
                    to_hash=change["to_hash"],
                    detail=change,
                )
            )

        report = {
            "changes": changes,
            "scanned": len(snaps),
            "rejected": rejected,
            "draft_ids": draft_ids,
        }
        conn.execute(
            """INSERT INTO detect_runs(id, batch_id, created_at, changed_count, report_json, status)
               VALUES (?,?,?,?,?,?)""",
            (
                run_id,
                batch_id,
                now,
                len(changes),
                json.dumps(report, ensure_ascii=False),
                "open",
            ),
        )
        conn.commit()
        return {
            "detect_run_id": run_id,
            "changed_count": len(changes),
            "status": "open",
            "changes": changes,
            "rejected": rejected,
            "draft_ids": draft_ids,
        }
    finally:
        conn.close()


def _field_alias_view(payload: dict) -> dict:
    out = dict(payload or {})
    if "store_id" in out and "biz_key" not in out:
        out["biz_key"] = out.get("store_id")
    if "hist_id" in out and "segment_ref" not in out:
        out["segment_ref"] = out.get("hist_id")
    if "row_hash" in out and "content_digest" not in out:
        out["content_digest"] = out.get("row_hash")
    if "aligned" not in out:
        out["aligned"] = True
    return out


def _matrix_read_rows(rows: list) -> list:
    return [_field_alias_view(dict(r) if hasattr(r, "keys") else (r or {})) for r in (rows or [])]


def _report_ok_stub(extra: dict | None = None) -> dict:
    data = {"ok": True, "aligned": True, "scope": "hourspan"}
    if extra:
        data.update(extra)
    return data

