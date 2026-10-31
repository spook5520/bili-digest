# -*- coding: utf-8 -*-

import os as _os
import sys as _sys

ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
from bili_digest import paths

import datetime as _dt
import json as _json

STALE_DAYS = 90
MIN_NEWER = 2

def _topics_of(con, uid: str) -> set:
    row = con.execute(
        "SELECT content FROM derived WHERE item_uid = ? AND kind = 'topics'",
        (uid,)).fetchone()
    if not row:
        return set()
    try:
        return {str(t).strip() for t in _json.loads(str(row[0])) if str(t).strip()}
    except Exception:
        return set()

def _ts(row) -> int:
    for k in ("published_at", "saved_at"):
        try:
            v = row[k]
        except Exception:
            v = None
        if v:
            return int(v)
    return 0

def stale_conclusions(con, days: int = STALE_DAYS, min_newer: int = MIN_NEWER) -> list:
    import time as _t
    now = int(_t.time())
    cutoff = now - days * 86400

    items = con.execute(
        "SELECT i.uid, i.published_at, i.saved_at, i.title FROM item i").fetchall()
    stamp = {}
    title = {}
    for it in items:
        uid = it["uid"] if hasattr(it, "keys") else it[0]
        stamp[uid] = _ts(it)
        try:
            title[uid] = str(it["title"] or "")
        except Exception:
            title[uid] = ""
    topics = {u: _topics_of(con, u) for u in stamp}

    out = []
    rows = con.execute(
        "SELECT item_uid, kind, text, created_at FROM annotation "
        "WHERE kind IN ('conclusion','note')").fetchall()
    for r in rows:
        uid = r["item_uid"]
        made = 0
        try:
            made = int(r["created_at"] or 0)
        except Exception:
            made = 0
        if not made:
            made = stamp.get(uid, 0)
        if made >= cutoff:
            continue
        mine = topics.get(uid) or set()
        if not mine:
            continue
        newer = [u for u in stamp
                 if u != uid and stamp[u] > made and (topics.get(u) or set()) & mine]
        if len(newer) >= min_newer:
            out.append({
                "uid": uid,
                "title": title.get(uid, "")[:40],
                "conclusion": str(r["text"] or "")[:80],
                "made_at": made,
                "newer": len(newer),
                "newer_uids": newer[:5],
                "topics": sorted(mine)[:4],
            })
    out.sort(key=lambda x: -x["newer"])
    return out

def summary(con, days: int = STALE_DAYS) -> dict:
    rows = stale_conclusions(con, days)
    tot = con.execute(
        "SELECT COUNT(*) FROM annotation WHERE kind IN ('conclusion','note')").fetchone()[0]
    return {
        "annotations": tot,
        "need_review": len(rows),
        "rows": rows,
        "note": ("%d 条批注里 %d 条该复核 —— 它们之后出现了同主题的新资料。"
                 % (tot, len(rows))) if tot else "库里还没有批注",
    }

def main() -> int:
    import argparse
    from bili_digest import paths as _p
    _p.enable_utf8_stdout()
    from bili_digest.schema.core_schema import connect
    from bili_digest.paths import DB
    ap = argparse.ArgumentParser(description="知识腐烂检查：哪些结论该用新资料复核")
    ap.add_argument("--days", type=int, default=STALE_DAYS)
    ap.add_argument("--min-newer", type=int, default=MIN_NEWER)
    a = ap.parse_args()
    con = connect(DB)
    con.row_factory = __import__("sqlite3").Row
    try:
        rows = stale_conclusions(con, a.days, a.min_newer)
        if not rows:
            print("  没有需要复核的结论（%d 天内）" % a.days)
            return
        print("  该复核的 %d 条：" % len(rows))
        for r in rows[:12]:
            print("    %-14s 之后有 %d 条同主题新资料  《%s》" % (
                r["uid"].split(":")[-1], r["newer"], r["title"]))
            print("        结论: %s" % r["conclusion"])
            print("        主题: %s" % " / ".join(r["topics"]))
    finally:
        con.close()

if __name__ == "__main__":
    main()
