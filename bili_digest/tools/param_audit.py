# -*- coding: utf-8 -*-
import argparse
import statistics

from bili_digest import paths

CFGS = (
    [("comment=%.2f" % v, {}, {"kind": ("comment", v)})
     for v in (0.50, 0.60, 0.70, 0.80, 0.90, 0.95, 1.00)]
    + [("danmaku=%.2f" % v, {}, {"kind": ("danmaku", v)})
       for v in (0.20, 0.30, 0.55, 0.65, 0.75, 0.85, 1.00)]
    + [("合并=%.2f" % v, {}, {"merged": v}) for v in (0.45, 0.55, 0.65, 0.75, 0.85)]
    + [("short=%.2f" % v, {}, {"penalty": v}) for v in (0.30, 0.80, 1.00)]
    + [("literal=%.1f" % v, {}, {"literal": v}) for v in (1.0, 1.5, 3.0)]
)

TOP1_CFGS = [
    ("default 0.85/0.45", None, None),
    ("short=1.00 关掉", None, {"penalty": 1.00}),
    ("short=0.60", None, {"penalty": 0.60}),
    ("short=0.30 加强", None, {"penalty": 0.30}),
    ("danmaku=1.00 平权", None, {"kind": ("danmaku", 1.00)}),
    ("danmaku=0.70", None, {"kind": ("danmaku", 0.70)}),
    ("合并=0.45", None, {"merged": 0.45}),
    ("合并=0.65", None, {"merged": 0.65}),
    ("合并=0.85", None, {"merged": 0.85}),
]

def _apply(patch):
    from bili_digest.retrieve import rag_core as rc
    saved = {}
    if patch:
        if "kind" in patch:
            field, val = patch["kind"]
            saved["KIND_WEIGHT"] = dict(rc.KIND_WEIGHT)
            kw = dict(rc.KIND_WEIGHT)
            kw[field] = val
            rc.KIND_WEIGHT = kw
        if "penalty" in patch:
            saved["SHORT_PENALTY"] = rc.SHORT_PENALTY
            rc.SHORT_PENALTY = patch["penalty"]
        if "literal" in patch:
            saved["LITERAL_BOOST"] = rc.LITERAL_BOOST
            rc.LITERAL_BOOST = patch["literal"]
        if "merged" in patch:
            saved["KIND_WEIGHT"] = dict(rc.KIND_WEIGHT)
            kw = dict(rc.KIND_WEIGHT)
            kw["comment"] = kw["danmaku"] = patch["merged"]
            rc.KIND_WEIGHT = kw

    def restore():
        for k, v in saved.items():
            setattr(rc, k, v)
    return restore

def hits_of(con, vindex, query, kw=None, patch=None, top_k=10):
    from bili_digest.retrieve import rag_core as rc
    restore = _apply(patch)
    try:
        return rc.hybrid(con, vindex, query, top_k=top_k, **(kw or {}))
    finally:
        restore()

def labeled_hits(con, vindex, queries, kw=None, patch=None, top_k=10, cache=None):
    from bili_digest.tools import eval_retrieval as ev
    cache = ev._cache_load() if cache is None else cache
    out = []
    for q in queries:
        hits = hits_of(con, vindex, q, kw, patch, top_k)
        gains = [cache.get(ev._ckey(q, h.get("text") or "")) for h in hits]
        if hits and all(g is not None for g in gains):
            out.append((hits, gains))
    return out

def verdict_of(delta, floor, reject):
    if abs(delta) <= floor:
        return "测不动"
    return "显著" if reject else "超下限未校正"

def sweep(con, vindex, queries, cfgs=None, top_k=10, rel=2):
    from bili_digest.tools import block_criterion as bc
    from bili_digest.tools import stats_test as st

    base = [bc.context_precision(g, top_k, rel)
            for _h, g in labeled_hits(con, vindex, queries, top_k=top_k)]
    rows = []
    for label, kw, patch in (cfgs or CFGS):
        cps = [bc.context_precision(g, top_k, rel)
               for _h, g in labeled_hits(con, vindex, queries, kw, patch, top_k)]
        n = min(len(cps), len(base))
        d = [cps[i] - base[i] for i in range(n)]
        rows.append({"label": label, "n": n, "cp": statistics.mean(cps),
                     "delta": statistics.mean(d), "sigma": statistics.pstdev(d),
                     "floor": bc.detectable_delta(n, statistics.pstdev(d)),
                     "p": st.wilcoxon_signed_rank(d)["p"]})
    bh = st.benjamini_hochberg([r["p"] for r in rows])
    for r, pa, rej in zip(rows, bh["p_adj"], bh["reject"]):
        r["p_adj"] = pa
        r["reject"] = rej
        r["verdict"] = verdict_of(r["delta"], r["floor"], rej)
    return {"base": statistics.mean(base), "n_base": len(base), "rows": rows,
            "sig_raw": sum(1 for r in rows if r["p"] <= 0.05),
            "sig_adj": sum(1 for r in rows if r["reject"])}

def marginal(con, vindex, queries, k=24, band=4, rel=2):
    from bili_digest.tools import eval_retrieval as ev
    jc = ev._cache_load()
    rows = []
    for q in queries:
        hits = hits_of(con, vindex, q, top_k=k)
        gains = [jc.get(ev._ckey(q, h.get("text") or "")) for h in hits]
        if hits and all(g is not None for g in gains):
            rows.append((hits, gains))
    out = []
    for lo in range(0, k, band):
        win = [(h, g) for hits, gs in rows
               for h, g in list(zip(hits, gs))[lo:lo + band]]
        if not win:
            continue
        n = len(win)
        out.append({
            "band": (lo + 1, lo + band),
            "strong": sum(1 for _h, g in win if g >= 2) / n,
            "weak": sum(1 for _h, g in win if g >= 1) / n,
            "zero": sum(1 for _h, g in win if g == 0) / n,
            "n": n,
        })
    return out

def top1_quality(con, vindex, queries, cfgs=None):
    from bili_digest.tools import eval_retrieval as ev
    jc = ev._cache_load()
    out = []
    for label, kw, patch in (cfgs or TOP1_CFGS):
        n = short = dan = zero = 0
        for q in queries:
            hits = hits_of(con, vindex, q, kw, patch, 10)
            if not hits:
                continue
            n += 1
            h0 = hits[0]
            if len((h0.get("text") or "").strip()) < 12:
                short += 1
            if h0["kind"] == "danmaku":
                dan += 1
            if jc.get(ev._ckey(q, h0.get("text") or "")) == 0:
                zero += 1
        out.append({"label": label, "n": n, "short": short / max(n, 1),
                    "danmaku": dan / max(n, 1), "zero": zero / max(n, 1)})
    return out

def main():
    from bili_digest.schema.core_schema import connect
    from bili_digest.paths import DB, VEC
    from bili_digest.retrieve import rag_core as rc
    from bili_digest.tools import eval_retrieval as ev

    ap = argparse.ArgumentParser()
    ap.add_argument("--marginal", action="store_true", help="只看边际曲线")
    a = ap.parse_args()

    con = connect(DB)
    con.row_factory = __import__("sqlite3").Row
    vi = rc.VectorIndex(str(VEC))
    frag = [q for _u, q in ev.build_queries(con, "fragment", 100000)]
    short = [r[0] for r in con.execute(
        "SELECT term FROM keyword WHERE length(term) BETWEEN 2 AND 5 "
        "AND n_blocks BETWEEN 8 AND 400 ORDER BY n_blocks DESC LIMIT 100")]

    if not a.marginal:
        print("═══ ① 块级扫描（fragment 档 %d 条）═══" % len(frag))
        r = sweep(con, vi, frag)
        print("  基线 CP@10 %.4f · n=%d · 未校正 p<0.05 %d 个 · BH 后仍显著 %d 个"
              % (r["base"], r["n_base"], r["sig_raw"], r["sig_adj"]))
        print("  %-13s %8s %8s %8s %8s %7s %7s" %
              ("配置", "ΔCP", "σ(D)", "下限", "p", "BH后", "判定"))
        for x in r["rows"]:
            print("  %-13s %+8.4f %8.4f %8.4f %8.4f %7.4f   %s"
                  % (x["label"], x["delta"], x["sigma"], x["floor"],
                     x["p"], x["p_adj"], x["verdict"]))

        print("\n═══ ② 短查询档的 top-1 质量（n=%d）═══" % len(short))
        for x in top1_quality(con, vi, short):
            print("  %-18s n=%d  top1 短块 %.3f · top1 弹幕 %.3f · top1 无关 %.3f"
                  % (x["label"], x["n"], x["short"], x["danmaku"], x["zero"]))

    print("\n═══ ③ 多返回一条值不值（fragment 档，top-24）═══")
    for b in marginal(con, vi, frag):
        lo, hi = b["band"]
        print("  第 %2d~%2d 位  强相关 %.3f  弱相关 %.3f  无关 %.3f  n=%d"
              % (lo, hi, b["strong"], b["weak"], b["zero"], b["n"]))
    con.close()

if __name__ == "__main__":
    main()
