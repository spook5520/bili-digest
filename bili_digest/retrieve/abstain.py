# -*- coding: utf-8 -*-

import os as _os
import sys as _sys

ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
from bili_digest import paths

TOP1_HIGH = 0.55
TOP1_MID = 0.50
SEP_MID = 0.25

def confidence(con, query: str, k: int = 20) -> dict:
    import numpy as np
    from bili_digest.retrieve import rag_core as rc
    from bili_digest.retrieve import retrieve as rv

    try:
        model = rc.deps()["model"]
        vindex = rc.VectorIndex(str(paths.VEC), readonly=True)

        hits = rv.retrieve(con, vindex, query, granularity="block", top_k=k)
        texts = [str(h.get("text") or "")[:300] for h in hits if h.get("text")]
        if not texts:
            return {"top1": None, "separation": None, "note": "无干净块可算"}
        qv = np.array(list(model.query_embed([query]))[0], dtype=np.float32)
        qv /= (np.linalg.norm(qv) + 1e-9)
        D = np.array(list(model.embed(texts)), dtype=np.float32)
        D /= (np.linalg.norm(D, axis=1, keepdims=True) + 1e-9)
        sims = np.sort(D @ qv)[::-1]
        top1 = float(sims[0])
        sep = top1 - float(sims.mean())
        return {
            "top1": round(top1, 4),
            "separation": round(sep, 4),
            "n_blocks": len(texts),

            "note": "在干净块集上计算（已排除 comment/danmaku）",
        }
    except Exception as e:
        return {"top1": None, "separation": None, "error": type(e).__name__}

def main() -> int:
    import argparse
    from bili_digest import paths as _p
    _p.enable_utf8_stdout()
    from bili_digest.schema.core_schema import connect
    from bili_digest.paths import DB
    ap = argparse.ArgumentParser(description="观测一次查询的相似度分布（不用来判断有没有）")
    ap.add_argument("query")
    a = ap.parse_args()
    con = connect(DB)
    try:
        r = confidence(con, a.query)
        print("  查询: %s" % a.query)
        print("  top1=%s  分离度=%s" % (r.get("top1"), r.get("separation")))
        print("  %s" % r.get("note", r.get("error", "")))
    finally:
        con.close()

if __name__ == "__main__":
    main()
