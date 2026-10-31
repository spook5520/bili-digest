#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os as _os
import sys as _sys

from bili_digest import paths

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).parent
from bili_digest.paths import DB

def classify_items(con) -> dict:
    from bili_digest.distill import classify
    from bili_digest.distill import extract
    out = {"A": [], "B": [], "skip": []}
    rows = con.execute("""
        SELECT d.item_uid, i.item_id, i.title, i.duration_sec, i.extras,
               (SELECT content FROM derived d2 WHERE d2.item_uid=d.item_uid
                AND d2.kind='scores') sc,
               (SELECT content FROM derived d2 WHERE d2.item_uid=d.item_uid
                AND d2.kind='summary') sm,
               (SELECT COUNT(*) FROM content_block cb
                WHERE cb.item_uid=d.item_uid AND cb.kind='subtitle') nsub
        FROM derived d JOIN item i ON i.uid=d.item_uid WHERE d.kind='summary'
    """).fetchall()
    for r in rows:
        try:
            sc = json.loads(r["sc"]) if r["sc"] else {}
        except (ValueError, TypeError):
            sc = {}
        depth = sc.get("depth")
        mean = sc.get("mean")
        nsub = r["nsub"] or 0
        entry = {"uid": r["item_uid"], "item_id": r["item_id"],
                 "title": r["title"], "depth": depth, "mean": mean, "nsub": nsub}

        if nsub < 10:
            entry["why"] = f"无字幕（{nsub} 条）—— 抽样优化无效"
            out["skip"].append(entry)
            continue

        try:
            v = classify.classify_item(con, r["item_uid"])
            if v.extractable is False:
                entry["why"] = f"classify 判素材不足（{v.reason[:28]}）→ 重跑必早退"
                out["skip"].append(entry)
                continue
        except Exception as e:
            entry["why"] = f"classify 失败：{type(e).__name__}"
            out["skip"].append(entry)
            continue

        sus = extract.subtitle_suspect(con, r["item_uid"], r)
        if sus["suspect"]:
            entry["why"] = f"字幕可疑：{sus['reason']}"
            out["B"].append(entry)
            continue

        if (depth is not None and depth >= 3) or (mean is not None and mean >= 3.5):
            entry["why"] = f"已达标（depth {depth} / mean {mean}）"
            out["skip"].append(entry)
            continue

        entry["why"] = f"depth {depth} / mean {mean}、字幕 {nsub} 条 → 抽样优化受益"
        out["A"].append(entry)
    return out

def main() -> int:
    ap = argparse.ArgumentParser(description="重提炼字幕可信的条目")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    c = classify_items(con)

    if a.check or not a.apply:
        print(f"  A 类「抽样受益」{len(c['A'])} 条（建议重跑）:")
        for x in sorted(c["A"], key=lambda y: (y["depth"] or 9)):
            print(f"    depth {x['depth']}  字幕 {x['nsub']:>4} 条  "
                  f"{x['item_id']:<14} {str(x['title'])[:30]}")
        print(f"\n  B 类「字幕串轨」{len(c['B'])} 条（重跑无用，应等字幕修复）:")
        for x in c["B"]:
            print(f"    {x['item_id']:<14} {str(x['title'])[:30]:<32} {x['why'][:36]}")
        print(f"\n  跳过 {len(c['skip'])} 条:")
        for x in c["skip"][:6]:
            print(f"    {x['item_id']:<14} {str(x['title'])[:28]:<30} {x['why'][:34]}")
        con.close()
        return

    from bili_digest.distill import extract
    print(f"  重跑 {len(c['A'])} 条…")
    t0 = time.time()
    ok = fail = 0
    for i, x in enumerate(sorted(c["A"], key=lambda y: (y["depth"] or 9)), 1):
        it = con.execute("SELECT title,duration_sec,extras FROM item WHERE uid=?",
                         (x["uid"],)).fetchone()
        print(f"\n  [{i}/{len(c['A'])}] {x['item_id']}  {str(x['title'])[:34]}")
        before = x["depth"]
        try:
            r = extract.extract_item(con, x["uid"], force=True, verbose=False)
            if not r:
                print("       提炼未产出（素材不足？）")
                fail += 1
                continue
            sc = con.execute("SELECT content FROM derived WHERE item_uid=? "
                             "AND kind='scores'", (x["uid"],)).fetchone()
            after = (json.loads(sc["content"]).get("depth") if sc else None)
            arrow = "↑" if (after or 0) > (before or 0) else \
                    ("=" if after == before else "↓")
            print(f"       depth {before} → {after} {arrow}")
            ok += 1
        except Exception as e:
            print(f"       失败 {type(e).__name__}: {str(e)[:56]}")
            fail += 1
        time.sleep(1)

    print(f"\n  ✓ 重跑 {ok} 条成功，{fail} 条失败，用时 {(time.time()-t0)/60:.1f} 分钟")
    if ok:
        print("  [!] 提炼结果变了 → 建议重建索引（partialed_search 用 subtitle_seg，"
              "提炼不影响索引；但笔记/合成分可能引用旧摘要）")
    con.close()

if __name__ == "__main__":
    main()
