# -*- coding: utf-8 -*-
import statistics
import sys
from pathlib import Path

from bili_digest import paths

CH2TOK = 0.7
TOPK = (4, 8, 16, 32)
RECALL_K = 10

def _run(con, vi, qs, top_k):
    from bili_digest.retrieve import retrieve as rv
    hit, ranks, chars = 0, [], 0
    for uid, q, _s in qs:
        hits = rv.retrieve(con, vi, q, granularity="item", top_k=top_k)
        for h in hits:
            ev = h.get("evidence") or {}
            chars += len(str(ev.get("text") or "")) + len(str(h.get("summary") or ""))
        uids = [h.get("item_uid") for h in hits][:RECALL_K]
        if uid in uids:
            hit += 1
            ranks.append(1.0 / (uids.index(uid) + 1))
        else:
            ranks.append(0.0)
    n = max(len(qs), 1)
    return {"r10": hit * 100.0 / n, "mrr": statistics.mean(ranks),
            "tokens": int(chars / n * CH2TOK)}

def main() -> int:
    paths.enable_utf8_stdout()
    from bili_digest.tools import ablate
    from bili_digest.retrieve import rag_core as rc
    from bili_digest.tools import rewrite_eval as rwe
    from bili_digest.schema.core_schema import connect
    from bili_digest.paths import DB, VEC

    con = connect(DB)
    con.row_factory = __import__("sqlite3").Row
    vi = rc.VectorIndex(str(VEC))
    try:
        for gname, qs in (("口语", rwe.make(40)),
                          ("术语", ablate.literal_queries(con, 40))):
            if len(qs) < 5:
                continue
            print("== %s（n=%d）==" % (gname, len(qs)))
            print("  %-6s %8s %8s %10s %8s" % ("top_k", "R@10", "MRR", "token/次", "ΔR@10"))
            prev = None
            for k in TOPK:
                r = _run(con, vi, qs, k)
                d = "" if prev is None else "%+.1fpt" % (r["r10"] - prev)
                print("  %-6d %7.1f%% %8.4f %10d %8s" % (
                    k, r["r10"], r["mrr"], r["tokens"], d))
                prev = r["r10"]
            print()
    finally:
        con.close()

if __name__ == "__main__":
    main()
