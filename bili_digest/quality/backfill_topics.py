#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json

_MIN_LEN = 2

def _norm(x: str) -> str:
    return "".join(ch for ch in (x or "").lower() if ch.isalnum())

_ID_LIKE = __import__("re").compile(r"^[a-z0-9]+(_[a-z0-9]+)+$")

def _is_author_tag(name: str, authors: set) -> bool:
    if _norm(name) in authors:
        return True
    return bool(_ID_LIKE.match((name or "").strip()))

def plan(con) -> list:
    DER = ("summary", "key_points", "insights", "topics", "audience", "scores", "seg_summary")
    ph = ",".join("?" * len(DER))
    authors = {_norm(r[0]) for r in con.execute(
        "SELECT DISTINCT author_name FROM item WHERE author_name IS NOT NULL")}
    out = []
    rows = con.execute(
        "SELECT i.uid FROM item i WHERE NOT EXISTS "
        "(SELECT 1 FROM derived d WHERE d.item_uid=i.uid AND d.kind='topics') "
        "AND EXISTS (SELECT 1 FROM tag t WHERE t.item_uid=i.uid) "
        "AND EXISTS (SELECT 1 FROM content_block cb WHERE cb.item_uid=i.uid "
        "            AND cb.kind NOT IN (" + ph + "))", DER).fetchall()
    for (uid,) in rows:
        tags = [r[0] for r in con.execute(
            "SELECT name FROM tag WHERE item_uid=? ORDER BY name", (uid,))]
        keep = [t for t in tags
                if len(t) >= _MIN_LEN and not _is_author_tag(t, authors)]
        if keep:
            out.append({"uid": uid, "tags": tags, "topics": keep})
    return out

def apply(con, dry_run: bool = True) -> dict:
    items = plan(con)
    if not dry_run:
        for it in items:
            con.execute(
                "INSERT INTO derived(item_uid, kind, content) VALUES(?,?,?)",
                (it["uid"], "topics", json.dumps(it["topics"], ensure_ascii=False)))
        con.commit()
    return {"found": len(items), "applied": 0 if dry_run else len(items), "items": items}

def main() -> int:
    import argparse
    from bili_digest.paths import DB
    from bili_digest.schema.core_schema import connect
    ap = argparse.ArgumentParser(description="给有 tags 无 topics 的条目补 topics（默认只读）")
    ap.add_argument("--db", default=str(DB))
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    con = connect(a.db)
    con.row_factory = __import__("sqlite3").Row
    r = apply(con, dry_run=not a.apply)
    print("  可补 %d 条，已写 %d 条" % (r["found"], r["applied"]))
    for it in r["items"]:
        print("    %s  tags=%s -> topics=%s" % (
            it["uid"].split(":")[-1], ",".join(it["tags"])[:40], ",".join(it["topics"])[:40]))
    con.close()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
