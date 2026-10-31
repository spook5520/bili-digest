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

from bili_digest.cli.helpers import db_of, _auto_index

def cmd_derive(a) -> None:
    from bili_digest.distill import extract as ex
    con = db_of(a)

    if getattr(a, "baseline", False):
        ex.set_baseline(con)
        con.close()
        return

    if getattr(a, "incremental", False):
        uids = ([a.uid] if a.uid else
                [r["item_uid"] for r in con.execute(
                    "SELECT item_uid FROM derived WHERE kind='summary' "
                    "ORDER BY created_at DESC LIMIT ?", (a.limit or 100,))])
        need, done = [], 0
        for uid in uids:
            chk = ex.needs_incremental(con, uid)
            if chk["needed"]:
                need.append((uid, chk))
        print(f"  检查 {len(uids)} 条，需要增量 {len(need)} 条")
        for uid, chk in need:
            print(f"\n  增量 {uid}\n    原因：{chk['reason']}")
            r = ex.extract_incremental(con, uid)
            if r:
                done += 1
        print(f"\n  完成 {done}/{len(need)}")
        if done:
            _auto_index(con)
        con.close()
        return

    if a.show:
        d = ex.get_derived(con, a.show)
        if not d:
            print("  无提炼结果")
        for k, v in d.items():
            print(f"\n  [{k}]\n    {json.dumps(v, ensure_ascii=False)[:400]}")
    elif a.uid:
        print(f"  提炼 {a.uid}")
        r = ex.extract_item(con, a.uid, force=a.force)
        if r:
            print(f"    摘要: {str(r.get('summary'))[:90]}")
            print(f"    要点 {len(r.get('key_points') or [])} 条 / "
                  f"观众视角 {len(r.get('audience') or [])} 条 / "
                  f"洞察 {len(r.get('insights') or [])} 条")
            _auto_index(con)
    else:
        r = ex.extract_many(con, limit=a.limit, tier=a.tier, force=a.force)
        print(f"\n  完成 {r['ok']}/{r['total']}，失败 {r['fail']}")
        if r.get("ok"):
            _auto_index(con)
    con.close()

def cmd_synth(a) -> None:
    from bili_digest.distill import synthesize as sy
    con = db_of(a)
    con.row_factory = sqlite3.Row
    try:
        sy.run(a, con)
    finally:
        con.close()
