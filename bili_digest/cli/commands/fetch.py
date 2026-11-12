#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import json
import sqlite3
import sys
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from bili_digest import paths

from bili_digest.schema import core_schema as cs
from bili_digest.quality import lifecycle as lc
from bili_digest.retrieve import rag_core as rc
from bili_digest.paths import source_key, bili_uid
from bili_digest import paths as cs_paths
from bili_digest.schema.core_schema import (BLOCK_KINDS, TIER_CACHE, TIER_KNOWLEDGE,
                         TIER_RAW, connect)

DEFAULT_DB = str(cs_paths.DB)
DEFAULT_VEC = str(cs_paths.VEC)

from bili_digest.cli.helpers import db_of

def cmd_fetch(a) -> None:
    if getattr(a, "queue", False):
        return _fetch_queue(a)
    if not a.ids:
        print("  给一个或多个 BV 号；只看队列用：kb fetch --queue")
        return
    from bili_digest.adapters import adapters
    con = db_of(a)
    ad = adapters.get_adapter(a.platform)
    for raw in a.ids:

        iid = cs.bare_id(raw)
        try:
            r = adapters.import_item(con, ad, iid, fetch_content=not a.meta_only)
            print(f"  {iid}: {r}")
        except Exception as e:
            print(f"  {iid}: 失败 {type(e).__name__} {str(e)[:60]}")
    con.close()

def cmd_import(a) -> None:
    from bili_digest.adapters import adapters
    con = db_of(a)
    base = Path(a.digest)
    raws = []
    if (base / "raw.sqlite").exists():

        raws = [(base.name, base / "raw.sqlite")]
    else:
        raws = [(d.name, d / "raw.sqlite") for d in sorted(base.iterdir())
                if d.is_dir() and (d / "raw.sqlite").exists()]
    if not raws:
        print(f"  在 {base} 下没找到 raw.sqlite")
        return
    agg = {"ok": 0, "skipped": 0, "blocks": 0}
    for name, raw in raws:
        r = adapters.import_digest(con, raw)
        if r.get("skipped"):
            agg["skipped"] += 1
            print(f"  {name}: 屏蔽跳过（{r['reason']}）")
        elif r.get("error"):
            print(f"  {name}: {r['error']}")
        else:
            blk = r.get("blocks") or {}
            n = sum(blk.values()) if isinstance(blk, dict) else blk
            agg["ok"] += 1
            agg["blocks"] += n
            print(f"  {name}: {n} 块  tier={r['tier']}  score={r['score']}")
    print(f"\n  导入 {agg['ok']} 条，跳过 {agg['skipped']} 条，共 {agg['blocks']} 块")
    print(f"  库内现有 {cs.stats(con)['items']} 条目")
    con.close()

def cmd_discover(a) -> None:
    from bili_digest.ingest import discover as dp
    con = db_of(a)
    con.row_factory = sqlite3.Row
    try:
        dp.run(a, con)
    finally:
        con.close()

def cmd_refine(a) -> None:
    from bili_digest.ingest import refine as rf
    st = rf.run(a)
    print(f"\n  {json.dumps(st, ensure_ascii=False)}")

def _fetch_queue(a) -> None:
    from bili_digest.ingest import refine as R
    con = db_of(a)
    try:
        q = R.queue_preview(con, limit=a.limit, order=a.order)
        if not q["items"]:
            print("  抓取队列为空（没有待抓条目，或都被屏蔽库过滤掉了）")
            return
        ORDER_CN = {
            "saved": "按收藏时间",
            "hot": "按播放量",
            "short": "按时长升序",
            "empty": "补空壳：块数升序 → 180~1800 秒优先 → 时长升序",
            "value": "按与开放问题的相关性（候选全空壳时会退化，会自己报）",
        }
        print("  抓取队列 · 顺序 %s（%s）" % (a.order, ORDER_CN.get(a.order, "?")))
        print()
        print("    #  条目                        时长    块数  同UP已抓  标题")
        for i, x in enumerate(q["items"], 1):
            print("   %2d  %-24s %4ds %5d %8s  %s" % (
                i, x["uid"].split(":")[-1], x["duration_sec"], x["blocks"],
                x["same_up"] or "-", (x["title"] or "")[:30]))
        print()
        print("  前 %d 条预估抓取耗时约 %.0f 秒（按 20 秒/条估，不含冷却）"
              % (q["n_queue"], q["est_sec"]))
        band = sum(1 for x in q["items"] if x["in_band"])
        print("  其中 %d/%d 条落在 180~1800 秒（empty 序认为性价比最高的一档）"
              % (band, q["n_queue"]))
        print()
        print("  依据：块数（0=空壳）、时长档、同 UP 已抓条数。")
        print("        相关性问题不在这个队列里 —— 实测 538 个空壳标题命中开放问题是 0 条，"
              "排序按它等于没排。")
        print("  真抓：python -m bili_digest.ingest.batch_refine --order %s" % a.order)
    finally:
        con.close()
