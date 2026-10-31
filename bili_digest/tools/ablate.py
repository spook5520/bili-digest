# -*- coding: utf-8 -*-

import os as _os
import statistics
import sys as _sys

ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
from bili_digest import paths

VARIANTS = [
    ("full",       {},                        "当前配置"),
    ("no_rrf",     {"no_rrf": True},           "两路都取但不融合"),
    ("no_vector",  {"no_vector": True},        "只用全文（BM25）"),
    ("no_fts",     {"no_fts": True},           "只用向量"),
    ("no_literal", {"_patch": {"LITERAL_BOOST": 1.0}}, "关掉字面加权"),
    ("no_short",   {"_patch": {"SHORT_PENALTY": 1.0}}, "关掉短块降权"),

    ("adaptive",   {"adaptive": True},        "按字面支持度调权（候选改进）"),
]

def literal_queries(con, n: int = 40) -> list:
    import json as _json
    rows = con.execute(
        "SELECT i.uid, d.content FROM item i JOIN derived d ON d.item_uid = i.uid "
        "WHERE d.kind = ? ORDER BY i.published_at DESC LIMIT 800",
        ("topics",)).fetchall()
    out = []
    seen = set()
    for uid, content in rows:
        if uid in seen:
            continue
        try:
            ts = _json.loads(str(content or "[]"))
        except Exception:
            continue
        for t in ts:

            s = str(t).strip()
            if len(s) >= 4 and (any(c.isascii() and c.isalpha() for c in s) or len(s) >= 5):
                out.append((uid, s, s))
                seen.add(uid)
                break
        if len(out) >= n:
            break
    return out

def _run(con, vi, qs, kwargs, patch=None, top_k=10):
    from bili_digest.retrieve import rag_core as rc
    from bili_digest.retrieve import retrieve as rv
    saved = {}
    if patch:
        for k, v in patch.items():
            if hasattr(rc, k):
                saved[k] = getattr(rc, k)
                setattr(rc, k, v)
    try:
        rr = []
        for uid, q, _src in qs:
            hits = rv.retrieve(con, vi, q, granularity="item", top_k=top_k,
                               mode="hybrid", **kwargs)
            uids = [h.get("item_uid") for h in hits]
            rr.append(1.0 / (uids.index(uid) + 1) if uid in uids else 0.0)
        return rr
    finally:
        for k, v in saved.items():
            setattr(rc, k, v)

def run(qs, label: str, top_k: int = 10, verbose: bool = True) -> dict:
    from bili_digest.retrieve import rag_core as rc
    from bili_digest.schema.core_schema import connect
    from bili_digest.paths import DB, VEC

    con = connect(DB)
    con.row_factory = __import__("sqlite3").Row
    vi = rc.VectorIndex(str(VEC))
    if verbose:
        print("── %s：%d 条查询 · top_k=%d" % (label, len(qs), top_k))

    rows = []
    base = None
    for name, kw0, note in VARIANTS:

        kw = dict(kw0)
        patch = kw.pop("_patch", None)
        rr = _run(con, vi, qs, kw, patch, top_k)
        r10 = sum(1 for x in rr if x > 0) * 100.0 / max(len(rr), 1)
        mrr = statistics.mean(rr) if rr else 0.0
        rows.append({"name": name, "note": note, "r10": r10, "mrr": mrr, "rr": rr})
        if name == "full":
            base = rr
        if verbose:
            print("  %-11s R@%d %5.1f%%   MRR %.4f   %s" % (
                name, top_k, r10, mrr, note))
    con.close()

    import numpy as np
    try:
        from bili_digest.tools import stats_test as st
    except Exception:
        st = None
    for r in rows[1:]:
        d = np.asarray(r["rr"]) - np.asarray(base)
        r["delta_mrr"] = float(d.mean())
        if st and base:
            try:
                rep = st.report_many([(r["name"], d.tolist(), float(np.std(d, ddof=1)))])
                row = (rep.get("rows") or [{}])[0]
                r["p"] = row.get("wilcoxon", {}).get("p")
                r["reject"] = row.get("reject")
            except Exception:
                pass
    if verbose:
        print()
        print("  与 full 配对比较：")
        for r in rows[1:]:
            print("    %-11s ΔMRR %+.4f  p=%.4f  显著=%s" % (
                r["name"], r["delta_mrr"], r.get("p") or -1, r.get("reject")))
        print()
    return {"rows": rows, "n": len(qs), "label": label}

def main() -> int:
    import argparse
    from bili_digest import paths as _p
    _p.enable_utf8_stdout()
    from bili_digest.schema.core_schema import connect
    from bili_digest.paths import DB
    ap = argparse.ArgumentParser(description="消融实验：逐个摘掉检索组件")
    ap.add_argument("--queries", type=int, default=40)
    ap.add_argument("--top-k", type=int, default=10)
    ap.add_argument("--both", action="store_true",
                    help="两组都跑：口语改写档 + 术语查询（推荐）")
    a = ap.parse_args()

    from bili_digest.tools import rewrite_eval as rwe
    con = connect(DB)
    con.row_factory = __import__("sqlite3").Row
    try:
        if a.both:
            r1 = run(rwe.make(a.queries), "colloquial（口语改写档）", a.top_k)
            print()
            r2 = run(literal_queries(con, a.queries), "literal（库内术语）", a.top_k)
            print()
            print("  ★ 两组结论若不同，说明能力【依赖查询类型】—— 这才是要写进文档的事实")
        else:
            run(rwe.make(a.queries), "colloquial（口语改写档）", a.top_k)
    finally:
        con.close()

if __name__ == "__main__":
    main()
