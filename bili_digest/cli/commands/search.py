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

from bili_digest.cli.helpers import db_of, _log_search

def cmd_search(a) -> None:
    con = db_of(a)
    from bili_digest.retrieve import temporal as tp
    import time as _t
    _t0 = _t.time()

    if not getattr(a, "rerank", False):
        from bili_digest.retrieve import retrieve as _rv
        _rv.LATEON_DEPTH = 0

    gran = getattr(a, "granularity", "block")

    _aud = getattr(a, "audience", None)
    if _aud is None and getattr(a, "content_only", False):
        _aud = "exclude"

    for _lo, _hi, _name in (
            (getattr(a, "min_duration", None), getattr(a, "max_duration", None), "时长"),
            (getattr(a, "min_blocks", None), getattr(a, "max_blocks", None), "块数")):
        if _lo is not None and _hi is not None and _lo > _hi:
            raise SystemExit(
                f"{_name}范围反了：min={_lo} > max={_hi}，不可能有结果"
                f"（若确实想按上限筛，去掉 min）")

    seg_mode = (gran == "seg")
    if seg_mode:
        gran = "block"

    if gran in ("item", "topic"):
        from bili_digest.retrieve import retrieve as rv
        if getattr(a, "partitioned", False):
            from bili_digest.retrieve import partitioned_search as ps
            from fastembed import TextEmbedding
            vi = ps._load_index()
            model = TextEmbedding(model_name=ps.EMBED_MODEL,
                                  cache_dir=str(Path(__file__).parent / "models"))
            mode = "partitioned"
        else:

            vi = rc.VectorIndex(a.vec, readonly=True)
            model = None
            mode = "hybrid"
        kw = {}
        if a.uid:
            kw["item_filter"] = a.uid
        if a.since or a.until:
            kw["since"] = a.since
            kw["until"] = a.until
            kw["time_field"] = a.time_field
        if getattr(a, "deep", False):
            print("  深查：改写查询中（每查询一次 LLM 调用）…")
        if _aud == "include":
            kw["include_audience_blocks"] = True
        hits = rv.retrieve(con, vi, a.query, granularity=gran, top_k=a.top_k,
                           mode=mode, model=model,
                           termify=getattr(a, "deep", None), **kw)
        print(f"  {gran} 粒度 命中 {len(hits)} 条（链路 {mode}）")
        print()
        _log_search(con, a, hits, mode, gran, _t0,
                    top_uid=(hits[0].get("item_uid") if hits else None))
        for i, h in enumerate(hits, 1):
            if gran == "item":
                print(f"  [{i}] {h['score']:.3f} 《{str(h['title'])[:40]}》"
                      f"  命中 {h['n_hits']} 块 {h['matched_kinds']}")
                ev = h["evidence"]
                print(f"        证据[{ev['kind']}/{ev['origin_hint']}] "
                      f"{ev['text'][:60]}")
                if h["summary"]:
                    print(f"        摘要: {h['summary'][:64]}")
                if a.show_source and h.get("url"):
                    print(f"        {h['url']}")
            else:
                print(f"  [{i}] {h['score']:.3f} 「{h['topic']}」"
                      f"  {h['n_items']} 个条目")
                for it in h["items"][:4]:
                    print(f"        {it['score']:.3f} {it['evidence'][:58]}")
        con.close()
        return

    if getattr(a, "partitioned", False):

        from bili_digest.retrieve import partitioned_search as ps
        from fastembed import TextEmbedding
        vi = ps._load_index()
        model = TextEmbedding(model_name=ps.EMBED_MODEL,
                              cache_dir=str(Path(__file__).parent / "models"))
        hits = ps.search(con, vi, model, a.query, a.top_k, item_filter=a.uid)
        dropped = None
        if hits and "_dropped_partitions" in hits[-1]:
            dropped = hits.pop()["_dropped_partitions"]
        print(f"  分区检索 命中 {len(hits)} 条\n")
        _log_search(con, a, hits, "partitioned", "block", _t0)
        for i, h in enumerate(hits, 1):
            loc = {"subtitle_seg": "段落", "subtitle": "字幕行", "danmaku": "弹幕",
                   "comment": "评论", "meta": "标题简介"}.get(h["kind"], h["kind"])
            tm = ""
            if h.get("offset_sec") is not None:
                m, s = divmod(int(h["offset_sec"]), 60)
                tm = f" {m:02d}:{s:02d}"
            print(f"  [{i}] {h['part_score']:.3f} [{h['partition']}/{loc}]{tm} "
                  f"{h['text'][:60]}")
            if a.show_source:
                print(f"        ← 《{str(h['title'])[:40]}》 {h.get('url') or ''}")
        if dropped:
            print(f"\n  （分区被门控丢弃：{dropped} —— 最高相似度低于阈值，"
                  f"视为该分区无相关内容）")
        con.close()
        return

    _fast = getattr(a, "fast", False)
    vi = None if _fast else rc.VectorIndex(a.vec)
    if vi is not None and not vi.size:
        raise SystemExit("向量索引为空，先跑 `kb.py index`")
    k = rc.auto_top_k(a.query, a.top_k) if a.auto_k else a.top_k

    _CONTENT = ("subtitle", "subtitle_seg", "summary", "key_points",
                "insights", "audience", "seg_summary", "meta")
    if seg_mode:
        _kinds = ["subtitle_seg"]
    elif _aud == "exclude":
        _kinds = list(_CONTENT)
    else:
        _kinds = a.kinds.split(",") if a.kinds else None
    hits = rc.hybrid(con, vi, a.query, top_k=k,
                     no_vector=_fast,
                     item_filter=a.uid, kinds=_kinds,
                     since=a.since, until=a.until,
                     time_field=a.time_field, recency=a.recency,
                     min_duration=a.min_duration, max_duration=a.max_duration,
                     min_blocks=a.min_blocks, max_blocks=a.max_blocks)
    if not a.no_rerank:
        hits = rc.heuristic_rerank(hits, a.query)
    tinfo = ""
    if a.since or a.until:
        tinfo = f"  时间 {tp.describe(a.since, a.until)}（按 {a.time_field}）"
    if a.recency:
        tinfo += f"  近期加权 {a.recency}"

    finfo = []
    if a.min_duration is not None or a.max_duration is not None:
        lo, hi = a.min_duration, a.max_duration
        if lo and hi:
            finfo.append(f"时长 {lo // 60}~{hi // 60}分")
        elif lo:
            finfo.append(f"时长 ≥{lo // 60}分")
        else:
            finfo.append(f"时长 ≤{hi // 60}分")
    if a.min_blocks is not None or a.max_blocks is not None:
        lo, hi = a.min_blocks, a.max_blocks
        if lo is not None and hi is not None:
            finfo.append(f"块数 {lo}~{hi}")
        elif lo is not None:
            finfo.append(f"块数 ≥{lo}")
        else:
            finfo.append(f"块数 ≤{hi}")
    if finfo:
        tinfo += "  形式 " + "·".join(finfo)

    _mode_label = "全文检索（--fast）" if _fast else "混合检索"
    print(f"  {_mode_label} 命中 {len(hits)} 条（top-k={k}"
          f"{'，自适应' if a.auto_k else ''}）{tinfo}")

    _vs = None if _fast else (hits[0].get("query_vec_sim") if hits else None)
    if _vs is not None:
        print(f"  语义贴近度 {_vs:.2f}（0~1，越高说明库里有越贴题的内容）")

    if getattr(a, "verbose", False):
        try:
            from bili_digest.quality import kb_cov as _cov
            _cov.annotate_hits(con, hits)
        except Exception as _e:
            print(f"  （覆盖信息不可用：{type(_e).__name__}）")

    _cl = rc.cliff(hits)
    if _cl["flat"]:
        print(f"  · 结果分数齐平（最小相邻比值 {_cl['ratio']}）——"
              f"没有哪条明显突出，请自己看内容判断")
    elif _cl["cut"] < len(hits):
        print(f"  ↓ 第 {_cl['cut']} 条之后有断层（比值 {_cl['ratio']}）——"
              f"再往后的 {len(hits) - _cl['cut']} 条多半是填充")

    try:
        from bili_digest.quality import obs as _obs
        rows_n = con.execute("SELECT COUNT(*) FROM pick_log").fetchone()[0]
        same = con.execute("SELECT COUNT(*) FROM query_log WHERE question=?",
                           (a.query,)).fetchone()[0]
        tip = [f"有用就回一句：kb.py pick <编号>"]
        if same > 1:
            tip.append(f"这个问题你问过 {same} 次")

        if 0 < rows_n < 5:
            tip.append(f"已攒 {rows_n} 条采纳（满 5 条才够用，见下方注释）")
        print(f"  · {' · '.join(tip)}")

        if len(hits) < 3 or _cl["flat"]:
            print("  · 想留一句自己的判断：kb.py annotate --add <条目> \"...\""
                  "（会进检索，以后能搜到）")
    except Exception:
        pass

    if gran == "block" and len(hits) >= 4:
        from collections import Counter as _C
        _c = _C(h.get("item_uid") for h in hits)
        _top_uid, _top_n = _c.most_common(1)[0]
        if _top_n >= max(4, len(hits) // 2):
            _t = next((h.get("title") for h in hits
                       if h.get("item_uid") == _top_uid), "")
            print(f"  · 其中 {_top_n}/{len(hits)} 条来自《{str(_t)[:28]}》"
                  f"—— 想只看「是哪一期」用 -g item；看整段用 -g seg")

    print()
    _log_search(con, a, hits, "hybrid", "block", _t0,
                top_uid=(hits[0].get("item_uid") if hits else None))
    for i, h in enumerate(hits, 1):
        print(f"  [{i}] {rc.fmt_hit(h)}")
        if getattr(a, "verbose", False) and h.get("coverage"):
            _note = h.get("coverage_note") or ""
            print(f"      覆盖 {h['coverage']}" + (f" · {_note}" if _note else ""))
        if a.show_source and h.get("title"):
            print(f"        ← 《{h['title'][:40]}》 {h.get('url') or ''}")
    con.close()
