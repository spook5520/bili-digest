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

def _ev_mod():
    from bili_digest.quality import task_event as _te
    return _te

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

        _had = con.execute("SELECT 1 FROM derived WHERE item_uid=? LIMIT 1",
                           (a.uid,)).fetchone() is not None
        r = ex.extract_item(con, a.uid, force=a.force)

        _te = _ev_mod()
        if r:
            _kp_n, _in_n = len(r.get("key_points") or []), len(r.get("insights") or [])
            print(f"    摘要: {str(r.get('summary'))[:90]}")
            print(f"    要点 {_kp_n} 条 / "
                  f"观众视角 {len(r.get('audience') or [])} 条 / "
                  f"洞察 {_in_n} 条")
            _te.record(con, a.uid, "distill", "completed",
                       f"key_points={_kp_n} insights={_in_n}", commit=True)
            _auto_index(con)
        elif _had:
            print("    已提炼过，未重跑（--force 可覆盖）")
            _te.record(con, a.uid, "distill", "aborted", "already distilled",
                       commit=True)
        else:

            _te.record(con, a.uid, "distill", "rejected", "insufficient material",
                       commit=True)
    else:
        r = ex.extract_many(con, limit=a.limit, tier=a.tier, force=a.force)
        print(f"\n  完成 {r['ok']}/{r['total']}，失败 {r['fail']}")
        _te = _ev_mod()
        _te.record(con, "-", "distill", "completed",
                   f"ok={r['ok']} total={r['total']} fail={r['fail']}", commit=True)
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
