import hashlib
import json
from datetime import datetime, timezone

from app.db import FIXTURES_DIR, connect

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stores (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  region TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingest_batches (
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  report_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS source_snapshots (
  id TEXT PRIMARY KEY,
  batch_id TEXT NOT NULL,
  store_id TEXT NOT NULL REFERENCES stores(id),
  as_of_ts TEXT NOT NULL,
  open_time TEXT,
  close_time TEXT,
  weekday_mask TEXT NOT NULL DEFAULT '1111100',
  biz_status TEXT NOT NULL DEFAULT 'open',
  payload_json TEXT NOT NULL DEFAULT '{}',
  row_hash TEXT NOT NULL,
  applied INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS detect_runs (
  id TEXT PRIMARY KEY,
  batch_id TEXT,
  created_at TEXT NOT NULL,
  changed_count INTEGER NOT NULL DEFAULT 0,
  report_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS dim_store_hours_hist (
  id TEXT PRIMARY KEY,
  store_id TEXT NOT NULL REFERENCES stores(id),
  open_time TEXT,
  close_time TEXT,
  weekday_mask TEXT NOT NULL,
  biz_status TEXT NOT NULL,
  valid_from TEXT NOT NULL,
  valid_to TEXT,
  is_current INTEGER NOT NULL DEFAULT 1,
  source_snapshot_id TEXT,
  row_hash TEXT NOT NULL,
  closed_by_run TEXT
);

CREATE TABLE IF NOT EXISTS dim_store_hours_current (
  store_id TEXT PRIMARY KEY REFERENCES stores(id),
  open_time TEXT,
  close_time TEXT,
  weekday_mask TEXT NOT NULL,
  biz_status TEXT NOT NULL,
  hist_id TEXT NOT NULL,
  as_of_ts TEXT NOT NULL,
  row_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quarantine (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  reason TEXT NOT NULL,
  member_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scd_proofs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  store_id TEXT NOT NULL,
  hist_id TEXT NOT NULL,
  mirror_hist_id TEXT NOT NULL,
  row_hash TEXT NOT NULL,
  valid INTEGER NOT NULL,
  detail_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

-- 序栅栏（open_close_order_fence）落库形态甲：搁置表 + 快照 applied 旁路 + 草稿 + 核证流
CREATE TABLE IF NOT EXISTS open_close_quarantine (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  snapshot_id TEXT NOT NULL,
  store_id TEXT NOT NULL,
  reason TEXT NOT NULL,
  detail_json TEXT NOT NULL DEFAULT '{}',
  related_run_id TEXT,
  draft_cleared INTEGER NOT NULL DEFAULT 0,
  cleared_draft_ids_json TEXT NOT NULL DEFAULT '[]',
  payload_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scd_drafts (
  id TEXT PRIMARY KEY,
  detect_run_id TEXT NOT NULL,
  store_id TEXT NOT NULL,
  snapshot_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  from_hash TEXT,
  to_hash TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  created_at TEXT NOT NULL,
  applied_at TEXT,
  detail_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS fence_proofs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  snapshot_id TEXT NOT NULL,
  store_id TEXT NOT NULL,
  event TEXT NOT NULL,
  reason TEXT,
  related_run_id TEXT,
  draft_cleared INTEGER NOT NULL DEFAULT 0,
  detail_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

-- 序闸放行戳（架构甲：合法序开段同事务打戳，戳为后续写硬前置）
CREATE TABLE IF NOT EXISTS fence_pass_stamps (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  store_id TEXT NOT NULL,
  hist_id TEXT NOT NULL,
  open_time TEXT,
  close_time TEXT,
  passed_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_fence_pass_hist
  ON fence_pass_stamps(hist_id);
CREATE INDEX IF NOT EXISTS ix_fence_pass_store
  ON fence_pass_stamps(store_id);

CREATE INDEX IF NOT EXISTS idx_hist_store_current
  ON dim_store_hours_hist(store_id, is_current);
CREATE INDEX IF NOT EXISTS idx_snap_store_ts
  ON source_snapshots(store_id, as_of_ts);
CREATE INDEX IF NOT EXISTS idx_ocq_snap
  ON open_close_quarantine(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_drafts_status
  ON scd_drafts(status, store_id);
CREATE INDEX IF NOT EXISTS idx_fence_snap
  ON fence_proofs(snapshot_id);
"""


def _ensure_column(conn, table: str, column: str, ddl_type: str):
    """幂等加列：老库已存在时跳过（CREATE TABLE IF NOT EXISTS 不会补列）。"""
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def row_hash(open_time, close_time, weekday_mask, biz_status) -> str:
    raw = f"{open_time or ''}|{close_time or ''}|{weekday_mask}|{biz_status}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def init_db():
    conn = connect()
    conn.executescript(SCHEMA)
    # 老库迁移：applied 旁路标记（1=可入维，0=被序栅栏搁置）
    _ensure_column(conn, "source_snapshots", "applied", "INTEGER NOT NULL DEFAULT 1")
    _ensure_column(conn, "detect_runs", "status", "TEXT NOT NULL DEFAULT 'applied'")
    if not conn.execute("SELECT 1 FROM settings WHERE key='as_of_mode'").fetchone():
        conn.execute("INSERT INTO settings(key, value) VALUES ('as_of_mode', 'business_ts')")
    if conn.execute("SELECT COUNT(*) AS c FROM stores").fetchone()["c"] == 0:
        _seed(conn)
    # 老库升级：仅对建库种子段补落放行戳（它们在建库时即「已开段」）。
    # 其它历史行若缺戳，按架构甲如实核证标缺，不作静默补戳。
    _backfill_seed_stamps(conn)
    conn.commit()
    conn.close()


def _backfill_seed_stamps(conn):
    from app.modules.fence_pass_stamp import get_stamp, maybe_stamp

    rows = conn.execute(
        """SELECT * FROM dim_store_hours_hist
           WHERE id LIKE 'HIST-SEED-%' AND open_time IS NOT NULL AND close_time IS NOT NULL"""
    ).fetchall()
    for h in rows:
        if get_stamp(conn, h["id"]) is None:
            maybe_stamp(conn, h["store_id"], h["id"], h["open_time"], h["close_time"])


def _seed(conn):
    path = FIXTURES_DIR / "seed_stores.json"
    if not path.exists():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    batch_id = "SEED-BATCH"
    created = now_iso()
    conn.execute(
        "INSERT INTO ingest_batches(id, created_at, report_json) VALUES (?,?,?)",
        (batch_id, created, json.dumps({"seed": True}, ensure_ascii=False)),
    )
    for s in data.get("stores", []):
        conn.execute(
            "INSERT INTO stores(id, name, region) VALUES (?,?,?)",
            (s["id"], s["name"], s["region"]),
        )
        ot, ct = s.get("open_time"), s.get("close_time")
        wm = s.get("weekday_mask", "1111100")
        st = s.get("biz_status", "open")
        rh = row_hash(ot, ct, wm, st)
        snap_id = f"SNAP-SEED-{s['id']}"
        as_of = s.get("as_of_ts", "2026-11-01T00:00:00Z")
        conn.execute(
            """INSERT INTO source_snapshots(
              id, batch_id, store_id, as_of_ts, open_time, close_time,
              weekday_mask, biz_status, payload_json, row_hash
            ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                snap_id,
                batch_id,
                s["id"],
                as_of,
                ot,
                ct,
                wm,
                st,
                json.dumps(s.get("payload", {}), ensure_ascii=False),
                rh,
            ),
        )
        hist_id = f"HIST-SEED-{s['id']}"
        conn.execute(
            """INSERT INTO dim_store_hours_hist(
              id, store_id, open_time, close_time, weekday_mask, biz_status,
              valid_from, valid_to, is_current, source_snapshot_id, row_hash, closed_by_run
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (hist_id, s["id"], ot, ct, wm, st, as_of, None, 1, snap_id, rh, None),
        )
        conn.execute(
            """INSERT INTO dim_store_hours_current(
              store_id, open_time, close_time, weekday_mask, biz_status,
              hist_id, as_of_ts, row_hash
            ) VALUES (?,?,?,?,?,?,?,?)""",
            (s["id"], ot, ct, wm, st, hist_id, as_of, rh),
        )
        conn.execute(
            """INSERT INTO scd_proofs(
              store_id, hist_id, mirror_hist_id, row_hash, valid, detail_json, created_at
            ) VALUES (?,?,?,?,?,?,?)""",
            (
                s["id"],
                hist_id,
                hist_id,
                rh,
                1,
                json.dumps({"seed": True}, ensure_ascii=False),
                created,
            ),
        )
        # 架构甲：种子段即首个已开段，建库时与 hist 同事务补落放行戳，
        # 保证底座后续写闸自洽（当前段有戳，才允许继续入档 / 开段）。
        from app.modules.fence_pass_stamp import maybe_stamp

        maybe_stamp(conn, s["id"], hist_id, ot, ct)
