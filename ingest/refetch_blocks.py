#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
import paths

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).parent
from paths import DB

KIND_KEY = {"dm": "danmaku", "reply": "comment", "sub": "subtitle"}
DECLARED_KEY = {"dm": "danmaku_count", "reply": "reply_count"}

MIN_DECLARED = {"dm": 10, "reply": 5, "sub": 20}

def find_gaps(con, only=("dm", "reply"), min_declared=None) -> list:
    min_declared = min_declared or MIN_DECLARED
    rows = con.execute(
        "SELECT uid, item_id, title, duration_sec, extras FROM item").fetchall()

    have = {}
    for r in con.execute("SELECT item_uid, kind, COUNT(*) n FROM content_block "
                         "GROUP BY item_uid, kind"):
        have.setdefault(r["item_uid"], {})[r["kind"]] = r["n"]

    gaps = []
    for r in rows:
        try:
            ex = json.loads(r["extras"] or "{}") or {}
        except (ValueError, TypeError):
            ex = {}
        h = have.get(r["uid"], {})
        missing = []
        weight = 0
        for src in only:
            kind = KIND_KEY.get(src)
            dk = DECLARED_KEY.get(src)
            if not kind or not dk:
                continue
            declared = ex.get(dk) or 0
            if declared > min_declared.get(src, 0) and h.get(kind, 0) == 0:
                missing.append(src)
                weight += declared
        if missing:
            gaps.append({
                "uid": r["uid"], "item_id": r["item_id"], "title": r["title"],
                "duration": r["duration_sec"] or 0,
                "missing": missing, "declared_weight": weight,
            })
    gaps.sort(key=lambda x: -x["declared_weight"])
    return gaps

def check(con, only, verbose=True) -> dict:
    gaps = find_gaps(con, only)
    if verbose:
        from collections import Counter
        c = Counter("+".join(g["missing"]) for g in gaps)
        print(f"  声明有内容但实抓为 0 的条目: {len(gaps)} 条")
        for k, n in c.most_common():
            print(f"    缺 {k:<12} {n:>4}")
        print(f"\n  按声明量排序（最该补的前 10）:")
        for g in gaps[:10]:
            print(f"    {g['item_id']:<14} {str(g['title'])[:30]:<32} "
                  f"缺 {','.join(g['missing']):<8} 声明量 {g['declared_weight']}")
    return {"count": len(gaps), "gaps": gaps}

def apply(con, only, limit: int, cooldown: int, verbose=True) -> dict:
    import refine

    gaps = find_gaps(con, only)
    if limit:
        gaps = gaps[:limit]
    if not gaps:
        if verbose:
            print("  ✓ 没有需要补抓的条目")
        return {"fetched": 0}

    print(f"  待补抓 {len(gaps)} 条，只抓 {','.join(only)}，"
          f"条目间冷却 {cooldown}s")
    t0 = time.time()
    ok = 0
    fail = 0
    for i, g in enumerate(gaps, 1):
        bvid = g["item_id"]
        print(f"\n  [{i}/{len(gaps)}] {bvid}  {str(g['title'])[:36]}")
        print(f"       缺 {','.join(g['missing'])}（声明量 {g['declared_weight']}）")

        reused = False
        try:
            r2 = refine.step_import(con, bvid)
            if not r2.get("error") and not r2.get("skipped"):
                blocks = r2.get("blocks") or {}

                want_kinds = {KIND_KEY[s] for s in g["missing"] if s in KIND_KEY}
                gained = sum(v for k, v in blocks.items() if k in want_kinds)
                if gained:
                    print(f"       从已有 raw.sqlite 入库 {blocks}"
                          f"（补上 {','.join(sorted(want_kinds))}）")
                    ok += 1
                    fail = 0
                    reused = True
                else:
                    have = {k: v for k, v in blocks.items() if v}
                    print(f"       已有 raw.sqlite 未含所需内容 {want_kinds}"
                          f"（仅 {have or '空'}）→ 需重抓")
        except Exception:
            pass
        if reused:
            if i < len(gaps):
                time.sleep(cooldown)
            continue

        try:
            ok_flag, msg = refine.step_fetch(
                bvid, sources="meta," + ",".join(g["missing"]), force=True)
        except Exception as e:
            print(f"       采集失败: {type(e).__name__}: {str(e)[:60]}")
            fail += 1
            if fail >= 3:
                print(f"\n  [!] 连续 {fail} 次失败，停止（风控）")
                break
            time.sleep(min(cooldown, 30))
            continue

        if not ok_flag:
            print(f"       采集失败: {str(msg)[:70]}")
            fail += 1
            if fail >= 3:
                print(f"\n  [!] 连续 {fail} 次失败，停止（风控）")
                break
            time.sleep(min(cooldown, 30))
            continue
        print(f"       采集 {msg}")

        try:
            r2 = refine.step_import(con, bvid)
            if r2.get("error") or r2.get("skipped"):
                print(f"       入库跳过: {r2}")
                fail += 1
            else:
                blocks = r2.get("blocks") or {}
                got = {k: v for k, v in blocks.items() if v}
                print(f"       入库 {got}")
                ok += 1
                fail = 0
        except Exception as e:
            print(f"       入库失败: {type(e).__name__}: {str(e)[:60]}")
            fail += 1

        if i < len(gaps):
            time.sleep(cooldown)

    el = time.time() - t0
    print(f"\n  ✓ 补抓 {ok} 条成功，{fail} 条失败，用时 {el/60:.1f} 分钟")
    if ok:
        print("  [!] 新增了内容块 → 建议重建索引：python build_kb_index.py")
    return {"fetched": ok, "failed": fail}

def main():
    ap = argparse.ArgumentParser(description="补抓缺失的弹幕/评论/字幕")
    ap.add_argument("--check", action="store_true", help="只看缺多少")
    ap.add_argument("--apply", action="store_true", help="执行补抓")
    ap.add_argument("--only", default="dm,reply",
                    help="只补哪些源：dm / reply / sub（逗号分隔）")
    ap.add_argument("--limit", type=int, default=0, help="最多补几条（0=不限）")
    ap.add_argument("--cooldown", type=int, default=60,
                    help="条目间冷却秒数（风控限制）")
    a = ap.parse_args()

    only = tuple(x.strip() for x in a.only.split(",") if x.strip())
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    try:
        if a.apply:
            apply(con, only, a.limit, a.cooldown)
        else:
            check(con, only)
    finally:
        con.close()

if __name__ == "__main__":
    main()
