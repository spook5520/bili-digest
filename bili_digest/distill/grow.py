#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os as _os
import sys as _sys

from bili_digest import paths

import argparse
import json
import sys
import time
from pathlib import Path

from bili_digest.paths import enable_utf8_stdout

enable_utf8_stdout()

HERE = Path(__file__).parent

def probe(con, uid: str, meta_now: dict) -> dict:
    row = con.execute("SELECT extras FROM item WHERE uid=?", (uid,)).fetchone()
    try:
        ex = json.loads(row["extras"] or "{}") if row else {}
    except (ValueError, TypeError):
        ex = {}

    before = {"danmaku": ex.get("danmaku_count"),
              "reply": ex.get("reply_count")}
    after = {"danmaku": (meta_now.get("danmaku_count") or meta_now.get("danmaku")),
             "reply": (meta_now.get("reply_count") or meta_now.get("reply"))}

    delta = {}
    for k in ("danmaku", "reply"):
        b, a = before.get(k), after.get(k)
        if isinstance(b, int) and isinstance(a, int):
            delta[k] = a - b
    grew = any(v >= 0 for v in delta.values()) and any(
        v > 0 for v in delta.values())
    return {"grew": grew, "delta": delta, "before": before, "after": after}

def main() -> int:
    ap = argparse.ArgumentParser(description="检查素材增长并跑增量提炼")
    ap.add_argument("--apply", action="store_true", help="真的重抓与提炼")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--min-growth", type=int, default=30,
                    help="计数增长多少算「值得重抓」（默认 30 条）")
    ap.add_argument("--cooldown", type=int, default=8,
                    help="每条抓取之间的间隔秒数（风控考虑，默认 8）")
    ap.add_argument("--uid", default=None, help="只查某一条")
    a = ap.parse_args()

    from bili_digest.ingest import fetcher as bd
    from bili_digest.schema.core_schema import connect
    from bili_digest.paths import DB

    con = connect(DB)
    con.row_factory = __import__("sqlite3").Row

    sql = """SELECT i.uid, i.item_id, i.title FROM item i
             WHERE EXISTS(SELECT 1 FROM derived d
                          WHERE d.item_uid=i.uid AND d.kind='summary')"""
    params = ()
    if a.uid:
        sql += " AND i.uid=?"
        params = (a.uid,)
    sql += " ORDER BY i.saved_at DESC"
    rows = con.execute(sql, params).fetchall()
    if a.limit:
        rows = rows[:a.limit]
    print(f"  检查 {len(rows)} 条已提炼条目（只调 view 接口探测计数）\n")

    ck = bd.cookie_header(bd.load_credential())
    to_grow = []
    for n, r in enumerate(rows, 1):
        try:
            m = bd.fetch_meta(r["item_id"], ck)
        except Exception as e:
            print(f"  {r['item_id']}  探测失败 {type(e).__name__}")
            time.sleep(2)
            continue
        p = probe(con, r["uid"], m)
        d = p["delta"]
        grew = sum(v for v in d.values() if v > 0)
        if grew >= a.min_growth:
            to_grow.append((r, d, p))
            print(f"  ★ {r['item_id']:<14} 弹幕 +{d.get('danmaku', 0):<5} "
                  f"评论 +{d.get('reply', 0):<5}  {str(r['title'])[:30]}")
        else:
            print(f"    {r['item_id']:<14} 增长 {grew}（未达门槛 {a.min_growth}）")
        time.sleep(1.5)

    print(f"\n  值得增量的: {len(to_grow)} 条")
    if not to_grow:
        con.close()
        return
    if not a.apply:
        print("  （加 --apply 才会真的重抓与提炼）")
        con.close()
        return

    from bili_digest.ingest import refine
    from bili_digest.distill import extract
    ok = fail = 0
    for n, (r, d, _p) in enumerate(to_grow, 1):
        print(f"\n  [{n}/{len(to_grow)}] {r['item_id']}  {str(r['title'])[:36]}")
        try:
            fok, msg = refine.step_fetch(r["item_id"], sources="meta,sub,dm,reply",
                                         force=True)
            print(f"    抓取: {fok} ({msg})")
            imp = refine.step_import(con, r["item_id"])
            print(f"    导入: {imp.get('blocks')}")
            res = extract.extract_incremental(con, r["uid"], verbose=True)
            if res:
                ok += 1
            else:
                print("    增量: 无产出（看上面的原因）")
        except Exception as e:
            print(f"    ✗ {type(e).__name__}: {str(e)[:60]}")
            fail += 1
        if n < len(to_grow):
            time.sleep(a.cooldown)

    print(f"\n  增量提炼成功 {ok} · 失败 {fail}")
    print(f"  同步索引: {refine.step_index()}")
    con.close()

if __name__ == "__main__":
    main()
