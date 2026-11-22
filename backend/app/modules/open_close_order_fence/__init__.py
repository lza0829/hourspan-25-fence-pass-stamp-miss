"""序栅栏 · open_close_order_fence

规则：日内时刻快照的 close_time 与 open_time 均非空时，必须满足
close_time > open_time（同一自然日内的营业序）。close_time <= open_time
即非法序（含相等的零时长），不得进有效维（dim_store_hours_hist /
dim_store_hours_current），亦不得参与变更检测。

落库形态（择一·甲）：
  1. source_snapshots.applied=0 旁路（快照仍留贴源表，可追溯）；
  2. open_close_quarantine 搁置：快照 id + 原因 + 关联 run_id + 原始载荷
     + 草稿清理位（draft_cleared / cleared_draft_ids_json）；
  3. fence_proofs 驳回审计（只追加）。
禁止静默对调开闭入维；禁止只打日志仍写 hist。

失败闭（择一·甲）：入口分流——入档即判定，命中非法序立即清理本键
（store_id）未提交 Type2 草稿（scd_drafts.status='open'），并在搁置行
与核证流同时落草稿清理位。detect / scd.apply 入口复检兜底：非法序
一律剔除并补登搁置，绝不放行写闸。

协同点：ingest（入档分流）→ detect（仅吃 applied=1，产出草稿）→
scd（历史维写闸复检，提交/废弃草稿）→ fence_board（只读核证接口）。
"""
from __future__ import annotations

import json
from datetime import datetime

from app.seed import now_iso

REASON_BAD_ORDER = "close_le_open"
REASON_BAD_FORMAT = "time_format_invalid"
EVENT_QUARANTINED = "quarantined"
EVENT_DRAFT_CLEARED = "draft_cleared"
EVENT_APPLY_GUARD = "apply_guard_reject"
EVENT_DETECT_SKIP = "detect_skip"

DRAFT_OPEN = "open"
DRAFT_APPLIED = "applied"
DRAFT_DISCARDED = "discarded"


def parse_hhmm(value):
    """日内 HH:MM；无法按日内时刻解析返回 None（空值不在本模块管辖）。"""
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value.strip(), "%H:%M")
    except (ValueError, TypeError):
        return None


def check_order(open_time, close_time) -> tuple[bool, str | None]:
    """返回 (是否合法, 原因)。任一为空则本栅栏不适用，视为合法交下游处理。"""
    if open_time is None or close_time is None:
        return True, None
    ot = parse_hhmm(open_time)
    ct = parse_hhmm(close_time)
    if ot is None or ct is None:
        return False, REASON_BAD_FORMAT
    if ct <= ot:
        return False, REASON_BAD_ORDER
    return True, None


def snapshot_payload(snap) -> dict:
    """留载荷：搁置时保留快照全字段，便于人工核证与回放。"""
    if snap is None:
        return {}
    return {k: snap[k] for k in snap.keys()}


def record_proof(conn, *, snapshot_id, store_id, event, reason=None,
                 related_run_id=None, draft_cleared=0, detail=None) -> int:
    cur = conn.execute(
        """INSERT INTO fence_proofs(
             snapshot_id, store_id, event, reason, related_run_id,
             draft_cleared, detail_json, created_at
           ) VALUES (?,?,?,?,?,?,?,?)""",
        (
            snapshot_id,
            store_id,
            event,
            reason,
            related_run_id,
            1 if draft_cleared else 0,
            json.dumps(detail or {}, ensure_ascii=False),
            now_iso(),
        ),
    )
    return cur.lastrowid


def clear_open_drafts(conn, *, store_id, snapshot_id, reason, related_run_id=None) -> list[str]:
    """失败闭甲：清理本键全部未提交 Type2 草稿，并写驳回审计。

    正式态（hist / current 镜）本就未被 open 草稿触碰，因此无需回滚；
    清草稿即保证非法序命中后正式态保持冲突前。
    """
    rows = conn.execute(
        "SELECT id FROM scd_drafts WHERE store_id=? AND status=?",
        (store_id, DRAFT_OPEN),
    ).fetchall()
    cleared = [r["id"] for r in rows]
    if not cleared:
        return []
    now = now_iso()
    for did in cleared:
        old = conn.execute("SELECT detail_json FROM scd_drafts WHERE id=?", (did,)).fetchone()
        try:
            detail = json.loads(old["detail_json"]) if old and old["detail_json"] else {}
        except json.JSONDecodeError:
            detail = {}
        detail["discard"] = {
            "by_snapshot": snapshot_id,
            "reason": reason,
            "related_run_id": related_run_id,
            "at": now,
        }
        conn.execute(
            "UPDATE scd_drafts SET status=?, detail_json=? WHERE id=?",
            (DRAFT_DISCARDED, json.dumps(detail, ensure_ascii=False), did),
        )
    record_proof(
        conn,
        snapshot_id=snapshot_id,
        store_id=store_id,
        event=EVENT_DRAFT_CLEARED,
        reason=reason,
        related_run_id=related_run_id,
        draft_cleared=1,
        detail={"cleared_draft_ids": cleared},
    )
    return cleared


def quarantine_snapshot(conn, *, snap, reason, related_run_id=None,
                        event=EVENT_QUARANTINED, detail=None) -> dict:
    """落库甲核心：applied=0 旁路 + 搁置表（幂等，同快照同原因只挂一条）
    + 驳回审计；并执行失败闭甲的草稿清理。返回搁置信息。"""
    snap_id = snap["id"]
    store_id = snap["store_id"]
    existing = conn.execute(
        "SELECT id FROM open_close_quarantine WHERE snapshot_id=? AND reason=?",
        (snap_id, reason),
    ).fetchone()
    if existing:
        return {"snapshot_id": snap_id, "quarantine_id": existing["id"], "duplicate": True}

    cleared = clear_open_drafts(
        conn,
        store_id=store_id,
        snapshot_id=snap_id,
        reason=reason,
        related_run_id=related_run_id,
    )
    payload = snapshot_payload(snap)
    cur = conn.execute(
        """INSERT INTO open_close_quarantine(
             snapshot_id, store_id, reason, detail_json, related_run_id,
             draft_cleared, cleared_draft_ids_json, payload_json, created_at
           ) VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            snap_id,
            store_id,
            reason,
            json.dumps(detail or {}, ensure_ascii=False),
            related_run_id,
            1 if cleared else 0,
            json.dumps(cleared, ensure_ascii=False),
            json.dumps(payload, ensure_ascii=False),
            now_iso(),
        ),
    )
    conn.execute(
        "UPDATE source_snapshots SET applied=0 WHERE id=?", (snap_id,)
    )
    record_proof(
        conn,
        snapshot_id=snap_id,
        store_id=store_id,
        event=event,
        reason=reason,
        related_run_id=related_run_id,
        draft_cleared=1 if cleared else 0,
        detail={"quarantine_id": cur.lastrowid, "cleared_draft_ids": cleared, **(detail or {})},
    )
    return {
        "snapshot_id": snap_id,
        "quarantine_id": cur.lastrowid,
        "reason": reason,
        "related_run_id": related_run_id,
        "draft_cleared": bool(cleared),
        "cleared_draft_ids": cleared,
        "duplicate": False,
    }


def is_applied(conn, snapshot_id) -> bool:
    row = conn.execute(
        "SELECT applied FROM source_snapshots WHERE id=?", (snapshot_id,)
    ).fetchone()
    return bool(row and row["applied"] == 1)


def create_draft(conn, *, detect_run_id, store_id, snapshot_id, kind,
                 from_hash, to_hash, detail=None) -> str:
    from uuid import uuid4

    draft_id = f"DRF-{uuid4().hex[:10]}"
    conn.execute(
        """INSERT INTO scd_drafts(
             id, detect_run_id, store_id, snapshot_id, kind, from_hash,
             to_hash, status, created_at, detail_json
           ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            draft_id,
            detect_run_id,
            store_id,
            snapshot_id,
            kind,
            from_hash,
            to_hash,
            DRAFT_OPEN,
            now_iso(),
            json.dumps(detail or {}, ensure_ascii=False),
        ),
    )
    return draft_id


def mark_drafts(conn, detect_run_id: str, status: str) -> int:
    now = now_iso()
    cur = conn.execute(
        "UPDATE scd_drafts SET status=?, applied_at=? WHERE detect_run_id=? AND status=?",
        (status, now if status == DRAFT_APPLIED else None, detect_run_id, DRAFT_OPEN),
    )
    return cur.rowcount


def open_drafts(conn, store_id: str | None = None):
    if store_id:
        return conn.execute(
            "SELECT * FROM scd_drafts WHERE status=? AND store_id=? ORDER BY created_at, id",
            (DRAFT_OPEN, store_id),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM scd_drafts WHERE status=? ORDER BY created_at, id",
        (DRAFT_OPEN,),
    ).fetchall()


def assert_snapshot_writable(conn, snap, *, related_run_id) -> tuple[bool, dict | None]:
    """历史维写闸 / 检测入口复检。非法序当场搁置（幂等）并拒绝放行。"""
    ok, reason = check_order(snap["open_time"], snap["close_time"])
    if ok:
        return True, None
    info = quarantine_snapshot(
        conn,
        snap=snap,
        reason=reason,
        related_run_id=related_run_id,
        event=EVENT_APPLY_GUARD,
    )
    return False, info


def board(conn) -> dict:
    """只读核证接口数据：搁置队列（快照 id/原因/关联 run/草稿清理位/载荷）、
    未提交草稿、驳回审计流、放行戳核证、汇总计数。"""
    from app.modules import fence_pass_stamp

    stamp_v = fence_pass_stamp.verify(conn)
    quarantined = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM open_close_quarantine ORDER BY id DESC"
        ).fetchall()
    ]
    drafts = [
        dict(r) for r in conn.execute(
            "SELECT * FROM scd_drafts ORDER BY created_at DESC, id DESC"
        ).fetchall()
    ]
    proofs = [
        dict(r) for r in conn.execute(
            "SELECT * FROM fence_proofs ORDER BY id DESC LIMIT 200"
        ).fetchall()
    ]
    return {
        "规则": "close_time 与 open_time 均非空时须 close_time > open_time；非法序搁置不入维",
        "落库形态": "甲：搁置表 open_close_quarantine + source_snapshots.applied=0 旁路 + fence_proofs 审计",
        "失败闭": "甲：入口分流并清理本键未提交 Type2 草稿（detect/apply 入口复检兜底）",
        "计数": {
            "搁置总数": len(quarantined),
            "未提交草稿": conn.execute(
                "SELECT COUNT(*) AS c FROM scd_drafts WHERE status=?", (DRAFT_OPEN,)
            ).fetchone()["c"],
            "已提交草稿": conn.execute(
                "SELECT COUNT(*) AS c FROM scd_drafts WHERE status=?", (DRAFT_APPLIED,)
            ).fetchone()["c"],
            "已废弃草稿": conn.execute(
                "SELECT COUNT(*) AS c FROM scd_drafts WHERE status=?", (DRAFT_DISCARDED,)
            ).fetchone()["c"],
            "旁路透照": conn.execute(
                "SELECT COUNT(*) AS c FROM source_snapshots WHERE applied=0"
            ).fetchone()["c"],
            "审计条": conn.execute("SELECT COUNT(*) AS c FROM fence_proofs").fetchone()["c"],
            "放行戳": stamp_v["stamp_n"],
            "放行戳待补": len(stamp_v["missing"]),
            "放行戳分叉": len(stamp_v["time_mismatch"]) + len(stamp_v["store_mismatch"]),
            "当前段缺戳": len(stamp_v["unattested_current"]),
        },
        "放行戳核证": stamp_v,
        "搁置队列": quarantined,
        "草稿": drafts,
        "核证流": proofs,
    }


def pass_stamp(conn, store_id: str, hist_id: str, open_time, close_time) -> dict:
    """序闸已过 → 落放行戳（架构甲：戳为后续写硬前置）。"""
    from app.modules import fence_pass_stamp

    return fence_pass_stamp.maybe_stamp(conn, store_id, hist_id, open_time, close_time)


def list_pass_stamps(conn) -> list:
    from app.modules import fence_pass_stamp

    return fence_pass_stamp.list_stamps(conn)["stamps"]
