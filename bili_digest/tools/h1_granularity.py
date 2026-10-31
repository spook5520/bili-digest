# -*- coding: utf-8 -*-
import statistics
import sys
from pathlib import Path

from bili_digest import paths

KINDS = [("subtitle_seg", "语义分段"), ("seg_summary", "段摘要")]

def _run(con, vi, qs, kinds, top_k=8):
    from bili_digest.retrieve import retrieve as rv
    hit, ranks, chars = 0, [], 0
    for uid, q, _s in qs:
        hits = rv.retrieve(con, vi, q, granularity="block", top_k=top_k,
                           kinds=[kinds])
        for h in hits:
            chars += len(str(h.get("text") or ""))
        seen = []
        for h in hits:
            u = h.get("item_uid")
            if u and u not in seen:
                seen.append(u)
        seen = seen[:10]
        if uid in seen:
            hit += 1
            ranks.append(1.0 / (seen.index(uid) + 1))
        else:
            ranks.append(0.0)
    n = max(len(qs), 1)
    return {"r10": hit * 100.0 / n, "mrr": statistics.mean(ranks),
            "tokens": int(chars / n * 0.7)}

def main() -> int:
    paths.enable_utf8_stdout()
    from bili_digest.retrieve import rag_core as rc
    from bili_digest.tools import rewrite_eval as rwe
    from bili_digest.schema.core_schema import connect
    from bili_digest.paths import DB, VEC

    con = connect(DB)
    con.row_factory = __import__("sqlite3").Row
    vi = rc.VectorIndex(str(VEC))
    try:
        all_qs = rwe.make(40)
        fair = []
        for uid, q, s in all_qs:
            a = con.execute("SELECT COUNT(*) FROM content_block WHERE item_uid=? AND kind=?",
                            (uid, "subtitle_seg")).fetchone()[0]
            b = con.execute("SELECT COUNT(*) FROM content_block WHERE item_uid=? AND kind=?",
                            (uid, "seg_summary")).fetchone()[0]
            if a and b:
                fair.append((uid, q, s))
        print("  公平集: %d / %d 条（两种块都存在）" % (len(fair), len(all_qs)))
        print()
        print("  %-14s %8s %8s %10s" % ("块类型", "R@10", "MRR", "token/次"))
        for kind, note in KINDS:
            r = _run(con, vi, fair, kind)
            print("  %-14s %7.1f%% %8.4f %10d   %s" % (
                kind, r["r10"], r["mrr"], r["tokens"], note))
        print()
        print("  （对照）全 40 条上 —— 这个口径是错的，见模块 docstring:")
        for kind, _note in KINDS:
            r = _run(con, vi, all_qs, kind)
            print("  %-14s %7.1f%% %8.4f %10d" % (kind, r["r10"], r["mrr"], r["tokens"]))
    finally:
        con.close()

if __name__ == "__main__":
    main()
