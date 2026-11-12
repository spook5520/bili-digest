# -*- coding: utf-8 -*-

from __future__ import annotations

import sqlite3

STAGES = ("fetch", "asr", "distill", "index", "publish")
TYPES = ("claimed", "completed", "failed", "rejected", "aborted")

_DDL = """
CREATE TABLE IF NOT EXISTS task_event (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    item_uid  TEXT NOT NULL,
    stage     TEXT NOT NULL,
    type      TEXT NOT NULL,
    ts        INTEGER NOT NULL,
    detail    TEXT
)
"""
_IDX = [
    "CREATE INDEX IF NOT EXISTS idx_task_event_uid ON task_event(item_uid, ts)",
    "CREATE INDEX IF NOT EXISTS idx_task_event_stage ON task_event(stage, type, ts)",
]

def ensure(con: sqlite3.Connection) -> None:
    con.execute(_DDL)
    for q in _IDX:
        con.execute(q)
    con.commit()

def record(con: sqlite3.Connection, item_uid: str, stage: str, type: str,
           detail: str | None = None, ts: int | None = None,
           commit: bool = True) -> None:
    if stage not in STAGES:
        raise ValueError("未知 stage：%r（可选 %s）" % (stage, "/".join(STAGES)))
    if type not in TYPES:
        raise ValueError("未知 type：%r（可选 %s）" % (type, "/".join(TYPES)))
    if ts is None:
        from bili_digest.retrieve.temporal import now_sec as _now
        ts = int(_now())
    ensure(con)
    con.execute(
        "INSERT INTO task_event(item_uid, stage, type, ts, detail) VALUES(?,?,?,?,?)",
        (item_uid, stage, type, int(ts), detail))
    if commit:
        con.commit()

def latest(con: sqlite3.Connection, item_uid: str) -> dict | None:
    try:
        row = con.execute(
            "SELECT stage, type, ts, detail FROM task_event "
            "WHERE item_uid=? ORDER BY ts DESC, id DESC LIMIT 1",
            (item_uid,)).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row:
        return None
    return {"stage": row[0], "type": row[1], "ts": row[2], "detail": row[3]}

def _table_exists(con: sqlite3.Connection) -> bool:
    try:
        return con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_event'"
        ).fetchone() is not None
    except sqlite3.OperationalError:
        return False

def stuck_by_stage(con: sqlite3.Connection, only_empty: bool = True) -> dict:
    if not _table_exists(con):
        return {"no_event": []}
    if only_empty:
        rows = con.execute("""
            SELECT i.uid FROM item i
            WHERE NOT EXISTS (SELECT 1 FROM content_block b
                              WHERE b.item_uid = i.uid
                                AND b.kind IN ('subtitle','comment','danmaku'))
        """).fetchall()
    else:
        rows = con.execute("SELECT uid FROM item").fetchall()
    out: dict = {}
    for (uid,) in rows:
        if not only_empty and _has_readable_blocks(con, uid):
            key = "in_library"
        else:
            ev = latest(con, uid)
            key = "%s/%s" % (ev["stage"], ev["type"]) if ev else "no_event"
        out.setdefault(key, []).append(uid)
    return out

def _has_readable_blocks(con: sqlite3.Connection, uid: str) -> bool:
    return con.execute(
        "SELECT 1 FROM content_block b WHERE b.item_uid=? "
        "AND b.kind IN ('subtitle','comment','danmaku') LIMIT 1", (uid,)
    ).fetchone() is not None

def report(con: sqlite3.Connection) -> int:
    import sys
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ensure(con)
    buckets = stuck_by_stage(con, only_empty=True)
    total = sum(len(v) for v in buckets.values())
    print("空壳 %d 条，按最后阶段分桶：" % total)
    for k in sorted(buckets, key=lambda x: -len(buckets[x])):
        v = buckets[k]
        print("  %-22s %4d" % (k, len(v)))
        for uid in v[:3]:
            print("      " + uid)
        if len(v) > 3:
            print("      … 另 %d 条" % (len(v) - 3))
    return 0

def main() -> int:
    import argparse
    from bili_digest.schema.core_schema import connect
    from bili_digest.paths import DB
    ap = argparse.ArgumentParser(description="空壳按卡住的阶段分桶")
    ap.add_argument("--db", default=str(DB))
    a = ap.parse_args()
    con = connect(a.db)
    try:
        return report(con)
    finally:
        con.close()

if __name__ == "__main__":
    raise SystemExit(main())
