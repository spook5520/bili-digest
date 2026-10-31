# -*- coding: utf-8 -*-

import os as _os
import sys as _sys

ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
from bili_digest import paths

def _index(con):
    import json as _json
    idx = {}
    rows = con.execute(
        "SELECT d.item_uid, d.content, i.title, i.saved_at FROM derived d "
        "JOIN item i ON i.uid = d.item_uid WHERE d.kind = ?", ("topics",)).fetchall()
    for r in rows:
        try:
            ts = _json.loads(str(r[1]))
        except Exception:
            continue
        for t in ts:
            name = str(t).strip()
            if not name:
                continue
            idx.setdefault(name, []).append((r[0], str(r[2] or ""), r[3]))
    return idx

def classify(con):
    idx = _index(con)
    real, weak, single = [], [], []
    for k, v in idx.items():
        n = len(v)
        if n >= 4:
            real.append({"topic": k, "n": n})
        elif n >= 2:
            weak.append({"topic": k, "n": n})
        else:
            single.append({"topic": k, "n": n})
    for lst in (real, weak, single):
        lst.sort(key=lambda x: (-x["n"], x["topic"]))
    return {"real": real, "weak": weak, "single": single}

def topics(con, limit=40, include_keywords=False):
    c = classify(con)
    out = c["real"] + (c["weak"] if include_keywords else [])
    return out[:limit]

def items_of(con, topic, limit=30):
    idx = _index(con)
    rows = idx.get(topic, [])
    rows = sorted(rows, key=lambda x: -(x[2] or 0))[:limit]
    return [{"uid": u, "title": t, "saved_at": sa} for u, t, sa in rows]

def find(con, keyword):
    idx = _index(con)
    hits = [{"topic": k, "n": len(v)} for k, v in idx.items() if keyword in k]
    hits.sort(key=lambda x: -x["n"])
    return hits[:10]

def main() -> int:
    import argparse
    from bili_digest import paths as _p
    _p.enable_utf8_stdout()
    from bili_digest.schema.core_schema import connect
    from bili_digest.paths import DB
    ap = argparse.ArgumentParser(description="按主题浏览知识库")
    ap.add_argument("--topic", default=None, help="主题名（可模糊）")
    ap.add_argument("--limit", type=int, default=40)
    a = ap.parse_args()
    con = connect(DB)
    con.row_factory = __import__("sqlite3").Row
    try:
        if not a.topic:
            ts = topics(con, a.limit)
            print("  共 %d 个主题（按条目数降序）" % len(ts))
            print()
            for t in ts:
                print("    %-24s %3d 条" % (str(t["topic"])[:24], t["n"]))
            print()
            print("  看某个主题：kb.py browse --topic <名字>")
            return

        its = items_of(con, a.topic, a.limit)
        if not its:
            cand = find(con, a.topic)
            if not cand:
                print("  没有匹配的主题：%s" % a.topic)
                return
            print("  「%s」没有精确匹配，相近的主题：" % a.topic)
            for c in cand:
                print("    %-24s %3d 条" % (str(c["topic"])[:24], c["n"] or 0))
            return
        print("  主题「%s」下 %d 条" % (a.topic, len(its)))
        print()
        import time as _t
        for it in its:
            d = _t.strftime("%Y-%m", _t.localtime(it["saved_at"])) if it["saved_at"] else "?"
            print("    %s  %-14s %s" % (d, it["uid"].split(":")[-1], it["title"][:48]))
    finally:
        con.close()

if __name__ == "__main__":
    main()
