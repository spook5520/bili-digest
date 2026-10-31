# -*- coding: utf-8 -*-

import os as _os
import statistics
import sys as _sys
from pathlib import Path

from bili_digest import paths

def _run_once(con, vi, qs, params: dict, top_k: int = 10,
              granularity: str = "item") -> dict:
    from bili_digest.retrieve import rag_core as rc
    from bili_digest.retrieve import retrieve as rv

    saved = {}
    for k, v in (params or {}).items():
        if hasattr(rc, k):
            saved[k] = getattr(rc, k)
            setattr(rc, k, v)
    try:
        rr = []
        hit = 0
        for uid, q, _s in qs:
            hits = rv.retrieve(con, vi, q, granularity=granularity,
                               top_k=top_k, mode="hybrid")

            seen = []
            for h in hits:
                u = h.get("item_uid")
                if u and u not in seen:
                    seen.append(u)
            uids = seen
            if uid in uids:
                hit += 1
                rr.append(1.0 / (uids.index(uid) + 1))
            else:
                rr.append(0.0)
        return {"per_query_rr": rr, "r10": hit / len(qs) * 100,
                "mrr": statistics.mean(rr)}
    finally:
        for k, v in saved.items():
            setattr(rc, k, v)

def compare(name: str, baseline: dict, variants: list,
            n_queries: int = 40, top_k: int = 10, granularity: str = "item",
            write_ledger: bool = True, verbose: bool = True) -> dict:
    from bili_digest.retrieve import rag_core as rc
    from bili_digest.tools import rewrite_eval as rwe
    from bili_digest.schema.core_schema import connect
    from bili_digest.paths import DB, VEC

    con = connect(DB)
    con.row_factory = __import__("sqlite3").Row
    vi = rc.VectorIndex(str(VEC))
    qs = rwe.make(n_queries)
    if verbose:
        print("  实验 %s · 改写档 %d 条查询" % (name, len(qs)))

    base = _run_once(con, vi, qs, baseline, top_k, granularity)
    if verbose:
        print("  基线 R@%d %.1f%% · MRR %.4f" % (top_k, base["r10"], base["mrr"]))

    rows = []
    per_variant = []
    for i, v in enumerate(variants):
        r = _run_once(con, vi, qs, v, top_k, granularity)
        per_variant.append(r)
        rows.append((str(v), r["r10"], r["mrr"]))
        if verbose:
            print("  变体%d %-40s R@%d %5.1f%% · MRR %.4f"
                  % (i + 1, str(v)[:40], top_k, r["r10"], r["mrr"]))

    sig_rows = []
    try:
        from bili_digest.tools import stats_test as st
        import numpy as _np
        cases = []
        base_rr = _np.asarray(base["per_query_rr"], dtype=float)
        for i, r in enumerate(per_variant):
            d = _np.asarray(r["per_query_rr"], dtype=float) - base_rr

            sigma_q = float(_np.std(d, ddof=1)) if len(d) > 1 else 0.0
            cases.append((str(variants[i])[:40], d.tolist(), sigma_q))
        rep = st.report_many(cases) if hasattr(st, "report_many") else None
        if rep and isinstance(rep, dict):
            sig_rows = rep.get("rows") or []
    except Exception as e:
        if verbose:
            print("  （显著性检验不可用：%s: %s）"
                  % (type(e).__name__, str(e)[:50]))

    significant = [s for s in sig_rows if s.get("reject")]

    table = ["%-42s %10s %10s" % ("变体", "R@10", "MRR"), "-" * 64]
    table.append("%-42s %9.1f%% %10.4f" % ("基线 " + str(baseline)[:32],
                                           base["r10"], base["mrr"]))
    for i, (lab, r10, mrr) in enumerate(rows):
        star = ""
        if i < len(sig_rows) and sig_rows[i].get("reject"):
            star = "  *显著"
        table.append("%-42s %9.1f%% %10.4f%s" % (lab[:42], r10, mrr, star))

    written = []
    if write_ledger:
        try:
            from bili_digest.tools import ledger
            for i, (lab, r10, mrr) in enumerate(rows):
                is_sig = i < len(sig_rows) and sig_rows[i].get("reject")
                if is_sig:
                    continue
                ledger.record(
                    "%s: %s" % (name, lab),
                    verdict="未显著",
                    metric="R@10 %.1f%% vs 基线 %.1f%%" % (r10, base["r10"]),
                    why="BH 校正后不显著，未能证明优于基线",
                    evidence="tools/experiment.py")
                written.append(lab)
        except Exception:
            pass

    con.close()
    return {"table": chr(10).join(table), "results": rows,
            "baseline": (base["r10"], base["mrr"]),
            "significant": significant, "ledger_written": written}

def main() -> int:
    import argparse
    from bili_digest import paths as _p
    _p.enable_utf8_stdout()
    ap = argparse.ArgumentParser(description="实验闭环：跑对照 + 判显著 + 记账")
    ap.add_argument("--name", default="参数对照")
    ap.add_argument("--param", required=True, help="参数名（rag_core 里的常量）")
    ap.add_argument("--baseline", type=float, required=True)
    ap.add_argument("--variants", required=True,
                    help="逗号分隔的变体值，如 0.3,0.7")
    ap.add_argument("--queries", type=int, default=40)
    ap.add_argument("--granularity", default="item",
                    choices=["item", "block", "seg", "topic"])
    a = ap.parse_args()

    base = {a.param: a.baseline}
    vs = [{a.param: float(x)} for x in a.variants.split(",") if x.strip()]
    r = compare(a.name, base, vs, n_queries=a.queries,
                granularity=a.granularity)
    print()
    print(r["table"])
    print()
    if r["significant"]:
        print("  有显著变体 —— 可以改配置，但请人工确认后再改。")
    else:
        print("  没有显著变体 —— 已写入失败账本。")

if __name__ == "__main__":
    main()
