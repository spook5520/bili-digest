# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import sqlite3

from bili_digest.schema.core_schema import now_sec

_DDL = """
CREATE TABLE IF NOT EXISTS refute (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    derived_id  INTEGER NOT NULL,
    reason      TEXT NOT NULL,
    evidence    TEXT,
    created_at  INTEGER NOT NULL,
    "by"        TEXT NOT NULL DEFAULT 'human'
)
"""
_IDX = [
    "CREATE INDEX IF NOT EXISTS idx_refute_derived ON refute(derived_id)",
]

def _table_exists(con: sqlite3.Connection) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='refute'"
    ).fetchone() is not None

def ensure(con: sqlite3.Connection) -> None:
    if _table_exists(con):
        return
    con.execute(_DDL)
    for q in _IDX:
        con.execute(q)

def record(con: sqlite3.Connection, derived_id: int, reason: str,
           evidence: str | None = None, by: str = "human") -> int:
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("refute.reason 不能为空 —— 判定必须写明为什么")
    ensure(con)
    if con.execute("SELECT 1 FROM derived WHERE id=?", (derived_id,)).fetchone() is None:
        raise ValueError("derived_id=%r 不存在于 derived 表" % (derived_id,))
    cur = con.execute(
        'INSERT INTO refute(derived_id, reason, evidence, created_at, "by") '
        "VALUES(?,?,?,?,?)",
        (derived_id, reason, evidence, now_sec(), by))
    con.commit()
    return int(cur.lastrowid)

def refuted_pairs(con: sqlite3.Connection) -> set[tuple[str, str]]:
    if not _table_exists(con):
        return set()
    return {
        (r[0], r[1]) for r in con.execute(
            "SELECT DISTINCT d.item_uid, d.kind FROM refute r "
            "JOIN derived d ON d.id = r.derived_id")
    }

def for_derived(con: sqlite3.Connection, derived_id: int) -> list[dict]:
    if not _table_exists(con):
        return []
    rows = con.execute(
        'SELECT id, derived_id, reason, evidence, created_at, "by" '
        "FROM refute WHERE derived_id=? ORDER BY id", (derived_id,)).fetchall()
    return [dict(r) for r in rows]

def all_records(con: sqlite3.Connection) -> list[dict]:
    if not _table_exists(con):
        return []
    rows = con.execute(
        'SELECT r.id, r.derived_id, d.item_uid, d.kind, r.reason, '
        'r.evidence, r.created_at, r."by" '
        "FROM refute r LEFT JOIN derived d ON d.id = r.derived_id "
        "ORDER BY r.id").fetchall()
    return [dict(r) for r in rows]

def report(con: sqlite3.Connection) -> int:
    recs = all_records(con)
    print("refute 判定 %d 条" % len(recs))
    for r in recs:
        print("  #%d  derived=%s  %s/%s  by=%s" % (
            r["id"], r["derived_id"], r["item_uid"], r["kind"], r["by"]))
        print("      reason: %s" % r["reason"])
        if r["evidence"]:
            print("      evidence: %s" % r["evidence"])
    print("检索降权命中 %d 个 (item_uid, kind) 组合" % len(refuted_pairs(con)))
    return 0

def main() -> int:
    ap = argparse.ArgumentParser(description="结论反驳记录（检索侧自动降权）")
    ap.add_argument("--db", default=None)
    ap.add_argument("--add", type=int, metavar="DERIVED_ID",
                    help="记一条反驳判定（写入；仅此动作会建表/写库）")
    ap.add_argument("--reason", default="", help="为什么判定它错了（必填）")
    ap.add_argument("--evidence", default=None, help="现场信息，自由文本")
    ap.add_argument("--by", default="human")
    a = ap.parse_args()

    from bili_digest.schema.core_schema import connect
    from bili_digest.paths import DB
    con = connect(a.db or str(DB))
    try:
        if a.add is not None:
            rid = record(con, a.add, a.reason, a.evidence, a.by)
            print("已记录 refute #%d -> derived_id=%s" % (rid, a.add))
            return 0
        return report(con)
    finally:
        con.close()

if __name__ == "__main__":
    raise SystemExit(main())
