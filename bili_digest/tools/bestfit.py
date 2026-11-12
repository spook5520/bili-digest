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

SETWISE_TAIL = """

只输出 json：{{"best": 编号}}
编号从 1 开始，只能选一个：最能【直接】回答上面问题的那条证据。
"""

def _cand_lines(hits: list, order: list, chars: int) -> list:
    lines = []
    for pos, idx in enumerate(order, 1):
        h = hits[idx]
        ev = h.get("evidence") or {}
        text = str(ev.get("text") or h.get("text") or "")[:chars]
        title = str(h.get("title") or "")[:60]
        lines.append("%d. [%s] %s" % (pos, title, text))
    return lines

def _prompt(query: str, lines: list, tail: str = None) -> str:
    if tail is None:
        tail = PROMPT_TAIL.format(n=len(lines))
    return PROMPT_HEAD.format(q=query) + "\n".join(lines) + tail

def _call(prompt: str, llm=None) -> str:
    if llm is not None:
        return llm(prompt)
    from bili_digest.distill import extract
    return extract.call_llm(prompt)

def _parse_order(raw: str, n: int) -> list:
    import json as _json
    try:
        order = (_json.loads(raw or "{}") or {}).get("order") or []
    except Exception:
        order = []
    out, seen = [], set()
    for x in order:
        try:
            i = int(x) - 1
        except (TypeError, ValueError):
            continue
        if 0 <= i < n and i not in seen:
            seen.add(i)
            out.append(i)
    for i in range(n):
        if i not in seen:
            out.append(i)
    return out

def _parse_best(raw: str, n: int) -> int:
    try:
        import json as _json
        i = int(((_json.loads(raw or "{}") or {}).get("best"))) - 1
        if 0 <= i < n:
            return i
    except Exception:
        pass
    return 0

def _tau(a: list, b: list) -> float:
    pos_b = {x: i for i, x in enumerate(b)}
    sb = [pos_b[x] for x in a if x in pos_b]
    n, conc, disc = len(sb), 0, 0
    for i in range(n):
        for j in range(i + 1, n):
            if sb[i] < sb[j]:
                conc += 1
            elif sb[i] > sb[j]:
                disc += 1
    return (conc - disc) / (conc + disc) if (conc + disc) else 0.0

def rank_single(query: str, hits: list, llm=None, chars: int = 700) -> list:
    n = len(hits)
    if n < 2:
        return list(range(n))
    p = _prompt(query, _cand_lines(hits, list(range(n)), chars))
    return _parse_order(_call(p, llm), n)

def rank_permsc(query: str, hits: list, k: int = 3, seed: int = 20261112,
                llm=None, chars: int = 700) -> tuple:
    import random as _random
    n = len(hits)
    if n < 2:
        return list(range(n)), 0.0, [], []
    rng = _random.Random(seed)
    perms = [list(range(n))]
    for _ in range(k - 1):
        p = list(range(n))
        rng.shuffle(p)
        perms.append(p)

    rankings = []
    for perm in perms:
        p = _prompt(query, _cand_lines(hits, perm, chars))
        disp = _parse_order(_call(p, llm), n)
        rankings.append([perm[i] for i in disp])

    score = [0.0] * n
    for r in rankings:
        for pos, idx in enumerate(r):
            score[idx] += pos
    order = sorted(range(n), key=lambda i: (score[i], i))
    taus = [_tau(rankings[a], rankings[b])
            for a in range(len(rankings)) for b in range(a + 1, len(rankings))]
    tau_mean = sum(taus) / len(taus) if taus else 0.0
    return order, tau_mean, taus, rankings

def rank_setwise(query: str, hits: list, group: int = 3,
                 llm=None, chars: int = 700) -> list:
    n = len(hits)
    remaining, out = list(range(n)), []
    while remaining:
        if len(remaining) == 1:
            out.append(remaining[0])
            break
        win = remaining[:group]
        p = _prompt(query, _cand_lines(hits, win, chars), SETWISE_TAIL)
        pick = _parse_best(_call(p, llm), len(win))
        idx = win[pick]
        out.append(idx)
        remaining.remove(idx)
    return out

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
