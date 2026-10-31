# -*- coding: utf-8 -*-

import os as _os
import sys as _sys

ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
from bili_digest import paths

PROMPT_HEAD = """下面是一个问题，以及若干条候选证据。请按「哪条最能回答这个问题」排序。

判断依据：读完这条证据之后，能不能【直接】回答问题。不是「相关」，是「够用」。

问题：{q}

候选：
"""

PROMPT_TAIL = """

只输出 json：{{"order": [最能回答的候选编号, 第二, ...]}}
编号从 1 开始，列出全部 {n} 条。
"""

def rerank(con, query: str, hits: list, verbose: bool = False) -> list:
    if len(hits) < 2:
        return hits
    import json as _json

    cands = []
    for i, h in enumerate(hits, 1):
        ev = h.get("evidence") or {}
        text = str(ev.get("text") or "")[:220]
        title = str(h.get("title") or "")[:60]
        cands.append("%d. [%s] %s" % (i, title, text))
    prompt = (PROMPT_HEAD.format(q=query)
              + chr(10).join(cands)
              + PROMPT_TAIL.format(n=len(hits)))

    try:
        from bili_digest.distill import extract
        raw = extract.call_llm(prompt)
        order = _json.loads(raw or "{}").get("order") or []
    except Exception as e:
        if verbose:
            print("    （BestFit 不可用：%s）" % type(e).__name__)
        return hits

    seen, out = set(), []
    for idx in order:
        try:
            i = int(idx) - 1
        except Exception:
            continue
        if 0 <= i < len(hits) and i not in seen:
            seen.add(i)
            out.append(hits[i])

    for i, h in enumerate(hits):
        if i not in seen:
            out.append(h)
    if verbose:
        print("    BestFit: 前三条 %s → %s" % (
            [str(h.get("item_uid", ""))[-6:] for h in hits[:3]],
            [str(h.get("item_uid", ""))[-6:] for h in out[:3]]))
    return out

def main() -> int:
    import argparse
    from bili_digest import paths as _p
    _p.enable_utf8_stdout()
    from bili_digest.schema.core_schema import connect
    from bili_digest.paths import DB, VEC
    ap = argparse.ArgumentParser(description="BestFit：用 LLM 偏好重排（CodeRAG）")
    ap.add_argument("query")
    ap.add_argument("--top", type=int, default=8)
    a = ap.parse_args()

    from bili_digest.retrieve import rag_core as rc
    from bili_digest.retrieve import retrieve as rv
    con = connect(DB)
    con.row_factory = __import__("sqlite3").Row
    try:
        vi = rc.VectorIndex(str(VEC))
        hits = rv.retrieve(con, vi, a.query, granularity="item", top_k=a.top)
        print("  重排前:")
        for i, h in enumerate(hits[:5], 1):
            print("    %d. %s" % (i, str(h.get("title"))[:56]))
        out = rerank(con, a.query, hits, verbose=True)
        print()
        print("  重排后:")
        for i, h in enumerate(out[:5], 1):
            print("    %d. %s" % (i, str(h.get("title"))[:56]))
    finally:
        con.close()

if __name__ == "__main__":
    main()
