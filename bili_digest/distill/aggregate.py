#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os as _os
import sys as _sys

from bili_digest import paths

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).parent

INTENT_PATTERNS = [
    ("compare", re.compile(
        r"区别|差别|差异|对比|相比|优劣|好处|坏处|代价|优缺点|两者|哪个更")),
    ("summarize", re.compile(
        r"整体|总体上|都说了|讲了什么|概括|综述|总结|主要观点|大概")),
    ("enumerate", re.compile(
        r"哪些|有什么|分别|一共|几次|多少(个|次|种)|列举|都有|各处|逐一")),
]

from bili_digest.retrieve.query_terms import QUERY_STOP, query_content_terms
from bili_digest.retrieve.query_terms import strip_generic as _strip_generic_impl

def _strip_generic(con, query: str, min_idf_ratio: float = 0.5) -> str:
    return _strip_generic_impl(con, query, min_idf_ratio)

def detect_intent(query: str) -> str:
    for name, pat in INTENT_PATTERNS:
        if pat.search(query):
            return name
    return "block"

REL_FLOOR = 0.55

MIN_CHARS = 15

def aggregate(con, vindex, query: str, top_k: int = 8,
              per_item: int = 3, mode: str = None,
              candidate_k: int = 128, item_filter: str = None) -> dict:
    from bili_digest.retrieve import rag_core as rc

    mode = mode or detect_intent(query)
    if mode == "block":
        hits = rc.hybrid(con, vindex, query, top_k=top_k,
                         item_filter=item_filter)
        return {"mode": "block", "items": [], "blocks": hits}

    q2 = query if item_filter else _strip_generic(con, query)

    kw = {"item_filter": item_filter} if item_filter else {}
    raw = rc.hybrid(con, vindex, q2, top_k=candidate_k,
                    candidate_k=max(candidate_k, rc.CANDIDATE_K), **kw)

    if raw:
        top = max((h.get("score", 0) for h in raw), default=0)
        raw = [h for h in raw if h.get("score", 0) >= top * REL_FLOOR]

    raw = [h for h in raw if len(str(h.get("text") or "").strip()) >= MIN_CHARS]

    by_item = defaultdict(list)
    for h in raw:
        uid = h.get("item_uid")
        if uid:
            by_item[uid].append(h)

    meta_of = {}
    for uid in by_item:
        r = con.execute("SELECT title, url FROM item WHERE uid=?", (uid,)).fetchone()
        if r:
            meta_of[uid] = {"title": r["title"], "url": r["url"]}

    groups = []
    if mode == "enumerate":

        import math as _m
        for uid, hs in by_item.items():
            hs.sort(key=lambda x: -x.get("score", 0))
            top = hs[0].get("score", 0) if hs else 0
            groups.append({"uid": uid, **meta_of.get(uid, {}),
                           "n_blocks": len(hs),
                           "evidence": hs[:per_item],
                           "rank": top * _m.log1p(len(hs))})
        groups.sort(key=lambda g: -g["rank"])
    elif mode == "compare":

        for uid, hs in by_item.items():
            hs.sort(key=lambda x: -x.get("score", 0))
            groups.append({"uid": uid, **meta_of.get(uid, {}),
                           "n_blocks": len(hs),
                           "evidence": hs[:per_item]})
        groups.sort(key=lambda g: -(g["evidence"][0].get("score", 0)
                                     if g["evidence"] else 0))
    else:

        for uid, hs in by_item.items():

            from bili_digest.schema.core_schema import sample_blocks
            by_id = {h.get("id"): h for h in hs}
            picked = [by_id[r["id"]] for r in sample_blocks(con, uid, None, per_item)
                      if r["id"] in by_id]
            if not picked:
                picked = hs[:per_item]
            groups.append({"uid": uid, **meta_of.get(uid, {}),
                           "n_blocks": len(hs), "evidence": picked})
        groups.sort(key=lambda g: -g["n_blocks"])

    return {"mode": mode, "n_items": len(groups),
            "n_blocks_scanned": len(raw), "items": groups[:top_k],
            "blocks": []}

def render(res: dict, max_chars: int = 2600) -> str:
    if res["mode"] == "block":
        lines = []
        for i, h in enumerate(res["blocks"], 1):
            lines.append(f"[{i}] ({h.get('kind')}) {str(h.get('text'))[:300]}")
        return "\n".join(lines)[:max_chars]

    lines = [f"【聚合模式】{res['mode']} · 覆盖 {res['n_items']} 个来源"
             f"（扫描 {res['n_blocks_scanned']} 块）", ""]
    n = 0
    for g in res["items"]:
        lines.append(f"── 来源：{str(g.get('title'))[:50]} ──")
        for h in g["evidence"]:
            n += 1
            lines.append(f"[{n}] ({h.get('kind')}) {str(h.get('text'))[:240]}")
        lines.append("")
        if sum(len(x) for x in lines) > max_chars:
            lines.append("…（已截断，优先保留了靠前的来源）")
            break
    return "\n".join(lines)[:max_chars]

def main() -> int:
    ap = argparse.ArgumentParser(description="跨块/跨条目的证据聚合")
    ap.add_argument("query")
    ap.add_argument("--mode", choices=("auto", "enumerate", "compare", "summarize"),
                    default="auto")
    ap.add_argument("--top-k", type=int, default=8)
    ap.add_argument("--per-item", type=int, default=3)
    ap.add_argument("--render", action="store_true", help="输出渲染后的上下文")
    ap.add_argument("--scope", help="限定在某个条目内聚合（uid）")
    a = ap.parse_args()

    from bili_digest.schema.core_schema import connect
    from bili_digest.paths import DB, VEC
    from bili_digest.retrieve import rag_core as rc
    con = connect(DB)
    con.row_factory = __import__("sqlite3").Row
    vi = rc.VectorIndex(str(VEC))

    mode = None if a.mode == "auto" else a.mode
    print(f"  意图识别: {detect_intent(a.query)}")
    res = aggregate(con, vi, a.query, top_k=a.top_k, per_item=a.per_item, mode=mode,
                    item_filter=a.scope)
    print(f"  模式 {res['mode']} · 覆盖 {res.get('n_items', 0)} 个来源 · "
          f"扫描 {res.get('n_blocks_scanned', len(res.get('blocks', [])))} 块\n")
    if res["mode"] == "block":
        for i, h in enumerate(res["blocks"][:6], 1):
            print(f"  [{i}] {h.get('score', 0):.3f} {str(h.get('text'))[:70]}")
    else:
        for g in res["items"][:6]:
            print(f"  ▸ {str(g.get('title'))[:52]}（{g['n_blocks']} 块）")
            for h in g["evidence"][:2]:
                print(f"      {str(h.get('text'))[:66]}")
    if a.render:
        print("\n── 渲染后的上下文（前 600 字）──")
        print(render(res)[:600])
    con.close()

if __name__ == "__main__":
    main()
