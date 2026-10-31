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

def db_of(a) -> sqlite3.Connection:
    return connect(a.db)

def _as_uid(raw: str) -> str:

    return raw if ":" in (raw or "") else bili_uid(raw)

def _log_search(con, a, hits, mode, gran, t0, top_uid=None):
    try:
        from bili_digest.quality import obs
        import time as _t
        obs.log_query(
            con, getattr(a, "query", "") or "",
            mode=mode, granularity=gran, n_hits=len(hits),
            top_uid=top_uid, ms=(_t.time() - t0) * 1000.0,
            hits=hits,
            filt={"since": getattr(a, "since", None),
                  "until": getattr(a, "until", None),
                  "uid": getattr(a, "uid", None),
                  "partitioned": getattr(a, "partitioned", False)})
    except Exception:
        pass

    try:
        from bili_digest.quality import source_weight as _sw
        _uids = {h.get("item_uid") for h in hits if h.get("item_uid")}
        if _uids:
            _sw.bump_usage(con, list(_uids))
    except Exception:
        pass

def _auto_index(con, verbose: bool = True) -> bool:
    try:
        from bili_digest.retrieve import rag_core as rc
        from bili_digest.paths import VEC
        n_blocks = con.execute("SELECT COUNT(*) FROM content_block").fetchone()[0]
        keys = rc.load_index_keys(VEC)
        if keys is not None and len(keys) >= n_blocks:
            return False
    except Exception:
        pass
    try:
        from bili_digest.tools import build_kb_index as bi
        r = bi.build_vec(con, verbose=False, rebuild=False)
        if verbose:
            print(f"  ✓ 已增量索引 {r.get('indexed', 0)} 条 —— 新提炼的内容现在可检索")
        return True
    except Exception as e:
        if verbose:
            print(f"  [!] 自动索引失败，请手动跑 build_kb_index.py"
                  f"（{type(e).__name__}: {str(e)[:50]}）")
        return False
