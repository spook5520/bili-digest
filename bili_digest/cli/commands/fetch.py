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
