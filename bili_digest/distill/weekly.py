# -*- coding: utf-8 -*-

import os as _os
import sys as _sys

ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
from bili_digest import paths

import json as _json
import time as _time

def touched_uids(con) -> set:
    out = set()
    for r in con.execute("SELECT hits_json FROM query_log WHERE hits_json IS NOT NULL"):
        try:
            for h in _json.loads(str(r[0])):
                u = h.get("uid")
                if u:
                    out.add(u)
        except Exception:
            continue
    return out

def never_touched(con, top: int = 8) -> dict:
    seen = touched_uids(con)
    rows = con.execute(
        "SELECT uid, title, saved_at, duration_sec FROM item "
        "WHERE saved_at > 0 ORDER BY saved_at ASC").fetchall()
    now = int(_time.time())
    cold = []
    for r in rows:
        if r[0] in seen:
            continue
        age = (now - int(r[2])) // 86400
        cold.append({"uid": r[0], "title": str(r[1] or ""),
                     "days": age, "duration_sec": r[3]})
    total = len(cold)
    cold.sort(key=lambda x: -x["days"])
    return {"total": total, "sample": cold[:top],
            "touched": len(seen), "items": len(rows)}

def shells(con, top: int = 5) -> dict:
    rows = con.execute(
        "SELECT i.uid, i.title, i.duration_sec FROM item i "
        "WHERE NOT EXISTS (SELECT 1 FROM content_block b WHERE b.item_uid = i.uid) "
        "ORDER BY i.duration_sec DESC LIMIT ?", (top,)).fetchall()
    total = con.execute(
        "SELECT COUNT(*) FROM item i WHERE NOT EXISTS "
        "(SELECT 1 FROM content_block b WHERE b.item_uid = i.uid)").fetchone()[0]
    return {"total": total, "sample": [
        {"uid": r[0], "title": str(r[1] or ""), "duration_sec": r[2]} for r in rows]}

def build(con) -> dict:
    return {"cold": never_touched(con), "shells": shells(con)}

def proposals(data: dict) -> list:
    out = []
    c = data["cold"]
    if c["sample"]:
        b = c["sample"][0]
        out.append("回看：%s（%s，%d 天前收的，从没被查过）" % (
            b["title"][:28], b["uid"].split(":")[-1], b["days"]))
    s = data["shells"]
    if s["sample"]:
        b = s["sample"][0]
        out.append("补抓：%s（%s，%d 分钟）—— 收藏了但没内容" % (
            b["title"][:28], b["uid"].split(":")[-1], (b["duration_sec"] or 0) // 60))
    if len(c["sample"]) > 3:
        b = c["sample"][3]
        out.append("回看：%s（%s，%d 天前）" % (
            b["title"][:28], b["uid"].split(":")[-1], b["days"]))
    return out[:3]

def main() -> int:
    import argparse
    from bili_digest import paths as _p
    _p.enable_utf8_stdout()
    from bili_digest.schema.core_schema import connect
    from bili_digest.paths import DB
    ap = argparse.ArgumentParser(description="每周简报：把「该看什么」主动送到眼前")
    ap.add_argument("--top", type=int, default=8, help="每节列几条（默认 8）")
    a = ap.parse_args()
    con = connect(DB)
    con.row_factory = __import__("sqlite3").Row
    try:
        d = build(con)
        c, s = d["cold"], d["shells"]
        print("  ── 本周简报 ─────────────────────────────")
        print()
        print("  从没被查过   %d / %d 条" % (c["total"], c["items"]))
        print("  （有检索记录的只有 %d 条，其余从没在任何结果里出现过）" % c["touched"])
        for b in c["sample"][:a.top]:
            print("    %4d 天  %-14s %s" % (b["days"], b["uid"].split(":")[-1], b["title"][:44]))
        print()
        print("  空  壳       %d 条（收藏了但没内容）" % s["total"])
        for b in s["sample"][:3]:
            print("    %4d 分  %-14s %s" % ((b["duration_sec"] or 0) // 60,
                                           b["uid"].split(":")[-1], b["title"][:44]))
        print()
        ps = proposals(d)
        if ps:
            print("  这周可以做")
            for i, p in enumerate(ps, 1):
                print("    %d. %s" % (i, p))
    finally:
        con.close()

if __name__ == "__main__":
    main()
