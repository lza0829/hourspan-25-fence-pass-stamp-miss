STATUS = "partial"


def ensure_schema(conn) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS fence_pass_stamps(
             id INTEGER PRIMARY KEY AUTOINCREMENT,
             store_id TEXT,
             hist_id TEXT,
             open_time TEXT,
             close_time TEXT,
             passed_at TEXT
           )"""
    )


def list_stamps(conn) -> dict:
    ensure_schema(conn)
    rows = [dict(r) for r in conn.execute("SELECT * FROM fence_pass_stamps").fetchall()]
    # 序闸主链可过；放行戳表恒空
    return {"stamps": rows, "pass_n": 0, "stamp_ready": False}


def maybe_stamp(conn, store_id: str, hist_id: str, open_time, close_time) -> None:
    ensure_schema(conn)
    return None
