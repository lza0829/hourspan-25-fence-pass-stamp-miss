"""序闸放行戳 · fence_pass_stamp

架构甲（全局唯一口径）：**以放行戳为后续写前置**。

- 合法序开段标成功时，必须与开段同事务写入一条 fence_pass 戳
  （store_id / hist_id / open_time / close_time / passed_at），与 hist 行互证；
  戳落不下来（写失败、键冲突、时刻分叉）即整笔回滚，绝不出现「正式已开、戳空」。
- 非法序绝不写戳；拒收路径（搁置 / 写闸驳回 / 待提交草稿被清）无戳。
- 邻键防串改：hist_id 唯一，戳上 (store_id, open_time, close_time) 必须与
  对应 hist 行逐字一致；核证接口对缺失 / 分叉 / 孤儿 / 重复 / 非法全部点名。
- 后续写闸：某店当前段若已有「已提交草稿」或已有戳，则再入档 / 再开段前
  必须核到该段的有效放行戳；无戳一律拒绝继续（即便 hist 已存在）。
  种子段在建库时即落戳，保证底座自洽。

本模块只读写 `fence_pass_stamps`，不提交事务；提交 / 回滚由调用方（scd.apply、
ingest.run_batch、seed.init_db）统一收口。
"""
from __future__ import annotations

from app.seed import now_iso

STATUS = "active"

# 正确性口径标记：stamp_precondition = 架构甲（放行戳为后续写硬前置）。
ARCHITECTURE = "stamp_precondition"
ARCHITECTURE_LABEL = "甲·以放行戳为后续写前置"

_CREATE_SQL = """CREATE TABLE IF NOT EXISTS fence_pass_stamps(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  store_id TEXT NOT NULL,
  hist_id TEXT NOT NULL,
  open_time TEXT,
  close_time TEXT,
  passed_at TEXT NOT NULL
)"""


def ensure_schema(conn) -> None:
    conn.execute(_CREATE_SQL)
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_fence_pass_hist "
        "ON fence_pass_stamps(hist_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_fence_pass_store "
        "ON fence_pass_stamps(store_id)"
    )


def _order_ok(open_time, close_time):
    # 延迟导入避免与 open_close_order_fence 形成模块级环。
    from app.modules.open_close_order_fence import check_order

    return check_order(open_time, close_time)


def maybe_stamp(conn, store_id: str, hist_id: str, open_time, close_time) -> dict:
    """写一条放行戳（幂等）。返回 {ok, stamp_id, reason, duplicate}。

    只落合法序；同一 hist_id 只允许一条，且键位必须一致，否则判为串改失败，
    由调用方整笔回滚。本函数不提交事务。
    """
    ensure_schema(conn)
    if not store_id or not hist_id:
        return {"ok": False, "reason": "missing_key"}

    ok, reason = _order_ok(open_time, close_time)
    if not ok:
        # 非法序不得打戳。
        return {"ok": False, "reason": f"illegal_order:{reason}"}

    existing = conn.execute(
        "SELECT * FROM fence_pass_stamps WHERE hist_id=?", (hist_id,)
    ).fetchone()
    if existing is not None:
        same = (
            existing["store_id"] == store_id
            and existing["open_time"] == open_time
            and existing["close_time"] == close_time
        )
        if not same:
            # 邻键戳被串改：同 hist_id 却挂了别的门店/时刻。
            return {
                "ok": False,
                "reason": "stamp_key_conflict",
                "stamp_id": existing["id"],
            }
        return {"ok": True, "stamp_id": existing["id"], "duplicate": True}

    cur = conn.execute(
        """INSERT INTO fence_pass_stamps(
             store_id, hist_id, open_time, close_time, passed_at
           ) VALUES (?,?,?,?,?)""",
        (store_id, hist_id, open_time, close_time, now_iso()),
    )
    return {
        "ok": True,
        "stamp_id": cur.lastrowid,
        "duplicate": False,
        "hist_id": hist_id,
        "store_id": store_id,
    }


# 兼容旧桩名：write_stamp / pass_stamp 均指向同一实现。
write_stamp = maybe_stamp


def get_stamp(conn, hist_id: str):
    ensure_schema(conn)
    row = conn.execute(
        "SELECT * FROM fence_pass_stamps WHERE hist_id=?", (hist_id,)
    ).fetchone()
    return dict(row) if row is not None else None


def stamp_matches_hist(hist, stamp) -> tuple[bool, str | None]:
    """戳与 hist 行互证：缺失 / 门店分叉 / 开闭时刻分叉。"""
    if stamp is None:
        return False, "stamp_missing"
    if hist is None:
        return False, "hist_missing"
    if stamp["store_id"] != hist["store_id"]:
        return False, "stamp_store_mismatch"
    if stamp["open_time"] != hist["open_time"] or stamp["close_time"] != hist["close_time"]:
        return False, "stamp_time_mismatch"
    return True, None


def has_valid_stamp(conn, hist) -> tuple[bool, str | None]:
    # 开闭任一为空不属于序闸管辖（与 check_order 口径一致）：核证跳过，
    # 后续写不因空时刻段被永久挡死。
    if hist is not None and (hist["open_time"] is None or hist["close_time"] is None):
        return True, None
    return stamp_matches_hist(hist, get_stamp(conn, hist["id"]))


def current_segment(conn, store_id: str):
    return conn.execute(
        """SELECT * FROM dim_store_hours_hist
           WHERE store_id=? AND is_current=1
           ORDER BY valid_from DESC, id DESC LIMIT 1""",
        (store_id,),
    ).fetchone()


def prior_segment_stamp_ok(conn, store_id: str) -> dict:
    """架构甲后续写闸：当前段须有有效放行戳，否则拒绝继续入档 / 开段。

    - 该店尚无当前段（首段）→ 放行（开段时自会打戳）。
    - 当前段存在但无戳 → prior_stamp_missing（即便 hist 已存在也拒绝）。
    - 戳被串改（门店 / 时刻分叉）→ 对应 prior_stamp_*_mismatch。
    """
    ensure_schema(conn)
    cur = current_segment(conn, store_id)
    if cur is None:
        return {"ok": True, "reason": None, "hist_id": None, "first_segment": True}
    ok, why = has_valid_stamp(conn, cur)
    if not ok:
        return {
            "ok": False,
            "reason": f"prior_{why}",
            "hist_id": cur["id"],
            "store_id": store_id,
            "first_segment": False,
        }
    return {
        "ok": True,
        "reason": None,
        "hist_id": cur["id"],
        "store_id": store_id,
        "first_segment": False,
    }


def segment_verdicts(conn) -> list:
    """逐店按当前段给后续写结论，直接区分「删戳后两架构」。

    架构甲：无戳 → 拒绝继续（writable=false），即便 hist 已存在。
    """
    ensure_schema(conn)
    out = []
    store_ids = [
        r["store_id"]
        for r in conn.execute(
            "SELECT DISTINCT store_id FROM dim_store_hours_hist ORDER BY store_id"
        ).fetchall()
    ]
    for store_id in store_ids:
        cur = current_segment(conn, store_id)
        if cur is None:
            out.append(
                {
                    "store_id": store_id,
                    "hist_id": None,
                    "stamp_present": False,
                    "stamp_problem": None,
                    "hist_exists": False,
                    "subsequent_write": "allowed",
                    "reason": None,
                    "first_segment": True,
                }
            )
            continue
        ok, why = has_valid_stamp(conn, cur)
        out.append(
            {
                "store_id": store_id,
                "hist_id": cur["id"],
                "stamp_present": ok,
                "stamp_problem": why,
                "hist_exists": True,
                # 架构甲口径：戳缺即挡写。
                "subsequent_write": "allowed" if ok else "blocked",
                "reason": None if ok else f"prior_{why}",
                "first_segment": False,
            }
        )
    return out


def verify(conn) -> dict:
    """只读核证：全量 hist 行与戳表互证，逐项点名问题。"""
    ensure_schema(conn)
    hist_rows = conn.execute(
        "SELECT * FROM dim_store_hours_hist ORDER BY store_id, valid_from, id"
    ).fetchall()
    stamps = conn.execute("SELECT * FROM fence_pass_stamps ORDER BY id").fetchall()

    by_hist: dict[str, list] = {}
    for s in stamps:
        by_hist.setdefault(s["hist_id"], []).append(s)
    hist_ids = {h["id"] for h in hist_rows}

    missing, time_mismatch, store_mismatch, unattested_current = [], [], [], []
    duplicates, illegal = [], []
    valid_n = 0

    for h in hist_rows:
        # 空时刻段不在序闸管辖内，不参与戳互证（与 check_order 口径一致）。
        if h["open_time"] is None or h["close_time"] is None:
            continue
        rows = by_hist.get(h["id"], [])
        if not rows:
            item = {
                "hist_id": h["id"],
                "store_id": h["store_id"],
                "is_current": h["is_current"],
            }
            missing.append(item)
            if h["is_current"]:
                unattested_current.append(item)
            continue
        if len(rows) > 1:
            duplicates.append({"hist_id": h["id"], "count": len(rows)})
        stamp = rows[0]
        ok, why = stamp_matches_hist(h, stamp)
        if ok:
            valid_n += 1
        else:
            item = {
                "hist_id": h["id"],
                "store_id": h["store_id"],
                "stamp_id": stamp["id"],
                "reason": why,
            }
            if why == "stamp_time_mismatch":
                time_mismatch.append(item)
            elif why == "stamp_store_mismatch":
                store_mismatch.append(item)
            else:
                missing.append(item)
            if h["is_current"]:
                unattested_current.append(item)

    orphans = [
        {"stamp_id": s["id"], "hist_id": s["hist_id"], "store_id": s["store_id"]}
        for s in stamps
        if s["hist_id"] not in hist_ids
    ]
    for s in stamps:
        ok, why = _order_ok(s["open_time"], s["close_time"])
        if not ok:
            illegal.append(
                {
                    "stamp_id": s["id"],
                    "hist_id": s["hist_id"],
                    "store_id": s["store_id"],
                    "reason": why,
                }
            )

    problems = (
        missing
        or time_mismatch
        or store_mismatch
        or orphans
        or duplicates
        or illegal
    )
    return {
        "ok": not problems,
        "architecture": ARCHITECTURE,
        "architecture_label": ARCHITECTURE_LABEL,
        "hist_n": len(hist_rows),
        "stamp_n": len(stamps),
        "valid_n": valid_n,
        "missing": missing,
        "time_mismatch": time_mismatch,
        "store_mismatch": store_mismatch,
        "unattested_current": unattested_current,
        "orphans": orphans,
        "duplicates": duplicates,
        "illegal": illegal,
        "segments": segment_verdicts(conn),
    }


def list_stamps(conn) -> dict:
    """只读核证接口数据：戳表全量 + 汇总 + 互证明细。"""
    ensure_schema(conn)
    rows = [
        dict(r)
        for r in conn.execute("SELECT * FROM fence_pass_stamps ORDER BY id").fetchall()
    ]
    v = verify(conn)
    return {
        "architecture": ARCHITECTURE,
        "architecture_label": ARCHITECTURE_LABEL,
        "rule": (
            "架构甲：合法序开段同事务打戳，戳为后续写硬前置；"
            "非法序无戳；无戳拒绝继续入档/开段（即使 hist 已存在）"
        ),
        "stamps": rows,
        "pass_n": len(rows),
        "stamp_ready": v["ok"],
        "verification": v,
    }
