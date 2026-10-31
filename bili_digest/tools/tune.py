# -*- coding: utf-8 -*-

import argparse
import itertools
import math
import sys

from bili_digest import paths

def _folds(items, k):
    return [items[i::k] for i in range(k)]

def _evaluate(con, vi, rv, qs, metric):
    ranks = []
    for uid, q, _s in qs:
        hits = rv.retrieve(con, vi, q, granularity="item", top_k=10)
        got = [h.get("item_uid") for h in hits]
        ranks.append(got.index(uid) + 1 if uid in got else 0)
    n = len(ranks) or 1
    if metric == "r10":
        return sum(1 for r in ranks if r) / n
    if metric == "top1":
        return sum(1 for r in ranks if r == 1) / n
    return sum(1.0 / r for r in ranks if r) / n

def main() -> int:
    ap = argparse.ArgumentParser(description="交叉验证搜查询期参数")
    ap.add_argument("--folds", type=int, default=3)
    ap.add_argument("--n", type=int, default=240, help="最多用多少条查询")
    ap.add_argument("--metric", default="mrr", choices=("mrr", "r10", "top1"))
    ap.add_argument("--full", action="store_true",
                   help="用完整网格（27 组合，三折约 8 分钟）")
    a = ap.parse_args()

    from bili_digest.retrieve import rag_core as rc
    from bili_digest.retrieve import retrieve as rv
    from bili_digest.tools import rewrite_eval as rwe
    from bili_digest.schema.core_schema import connect
    from bili_digest.paths import DB, VEC

    con = connect(DB)
    con.row_factory = __import__("sqlite3").Row

    import random as _random
    _pool = [q for q in rwe.make(limit=min(600, max(a.n * 4, 400)), src_kind="summary")]
    _random.seed(42)
    _random.shuffle(_pool)
    qs = _pool[:a.n]
    vi = rc.VectorIndex(str(VEC), readonly=True)
    rc.deps()

    if a.full:
        GRID = {
            "RRF_K": [20, 60, 100],
            "SHORT_PENALTY": [0.3, 0.55, 1.0],
            "CANDIDATE_K": [64, 128, 256],
        }
    else:
        GRID = {
            "RRF_K": [20, 60],
            "SHORT_PENALTY": [0.3, 0.55],
            "CANDIDATE_K": [64, 128],
        }
    keys = list(GRID)
    combos = list(itertools.product(*(GRID[k] for k in keys)))
    print("  查询 %d 条 · %d 折 · 网格 %d 组合 · 指标 %s" % (
        len(qs), a.folds, len(combos), a.metric.upper()))
    print("  注：LITERAL_BOOST 不在网格里 —— 实测它只有开/关两态，扫数值无意义。")
    print()

    saved = {k: getattr(rc, k) for k in keys}
    folds = _folds(qs, a.folds)
    picks = []
    held_scores = []
    try:
        for fi in range(a.folds):
            train = [q for j, f in enumerate(folds) if j != fi for q in f]
            test = folds[fi]
            best, best_v = None, -1.0
            for combo in combos:
                for k, v in zip(keys, combo):
                    setattr(rc, k, v)
                try:
                    sc = _evaluate(con, vi, rv, train, a.metric)
                except Exception:
                    sc = -1.0
                if sc > best_v:
                    best_v, best = sc, combo
            for k, v in zip(keys, best):
                setattr(rc, k, v)
            hs = _evaluate(con, vi, rv, test, a.metric)
            picks.append(best)
            held_scores.append(hs)
            print("    折 %d/%d  训练选出 %s（训练 %.4f）-> 留出 %.4f" % (
                fi + 1, a.folds,
                " ".join("%s=%s" % (k, v) for k, v in zip(keys, best)),
                best_v, hs))
    finally:
        for k, v in saved.items():
            setattr(rc, k, v)

    print()
    uniq = len(set(picks))
    print("  留出均值 %.4f ± %.4f" % (
        sum(held_scores) / len(held_scores),
        (sum((x - sum(held_scores) / len(held_scores)) ** 2 for x in held_scores)
         / len(held_scores)) ** 0.5))
    if uniq == 1:
        print("  ★ %d 折选出【同一组】参数：%s" % (len(picks), picks[0]))
        print("    这组参数可以采信（跨查询子集稳定）。")
    else:
        print("  ✗ %d 折选出了 %d 组【不同】参数：" % (len(picks), uniq))
        for i, p in enumerate(picks, 1):
            print("      折 %d: %s" % (i, p))
        print("    -> 说明当前查询量不足以定这些参数，应当【保留默认值】。")
        print("       取众数只是取「更常见的噪声」。")
    con.close()

if __name__ == "__main__":
    main()
