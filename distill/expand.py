#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
import paths

import argparse
import sys
from collections import defaultdict
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).parent

MAX_EXPAND = 6

EXPAND_WEIGHT = 2

def related_terms(con, term: str, top_n: int = MAX_EXPAND,
                  min_co: int = 3) -> list:
    import math
    row = con.execute("SELECT df FROM keyword WHERE term=?", (term,)).fetchone()
    if not row:
        return []
    df_a = row["df"] or 1
    out = []
    for r in con.execute("""
        SELECT b.term AS t, COUNT(*) AS co
        FROM keyword_block a JOIN keyword_block b ON b.block_id = a.block_id
        WHERE a.term = ? AND b.term <> ?
        GROUP BY b.term ORDER BY co DESC LIMIT ?""", (term, term, top_n * 4)):
        co = r["co"]
        if co < min_co:
            break
        d = con.execute("SELECT df FROM keyword WHERE term=?", (r["t"],)).fetchone()
        df_b = (d["df"] if d else 1) or 1
        strength = co / math.sqrt(df_a * df_b)
        out.append((r["t"], co, round(strength, 3)))
    out.sort(key=lambda x: -x[2])
    return out[:top_n]

def expand_query(con, query: str, max_expand: int = MAX_EXPAND,
                 verbose: bool = False) -> dict:
    try:

        from query_terms import QUERY_STOP, tokenize
        terms = [t for t in tokenize(query) if t not in QUERY_STOP]
    except Exception:
        return {"query": query, "added": [], "base": []}

    if not terms:
        return {"query": query, "added": [], "base": []}

    added = []
    for w in terms:

        for t, co, st in related_terms(con, w, top_n=max_expand):
            if t not in terms and t not in added:
                added.append(t)

        if len(added) < 2 and len(w) >= 3:
            for r in con.execute(
                    "SELECT term FROM keyword WHERE (term LIKE ? OR term LIKE ?) "
                    "AND term <> ? AND df >= 2 ORDER BY df DESC LIMIT 6",
                    (f"{w}%", f"%{w}", w)):
                t = r["term"]
                if t not in terms and t not in added:
                    added.append(t)

    try:
        from query_terms import QUERY_STOP
        added = [t for t in added if t not in QUERY_STOP]
    except Exception:
        pass
    added = added[:max_expand]

    if not added:
        return {"query": query, "added": [], "base": terms}

    base_part = " ".join(list(terms) * EXPAND_WEIGHT)
    q2 = f"{base_part} {' '.join(added)}"
    if verbose:
        print(f"    查询扩展: {terms} → +{added}")
    return {"query": q2, "added": added, "base": terms}

def main():
    ap = argparse.ArgumentParser(description="查询扩展（共现扩展）")
    ap.add_argument("query")
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--no-expand", action="store_true", help="对照：不扩展")
    a = ap.parse_args()

    from core_schema import connect
    from paths import DB, VEC
    import rag_core as rc
    con = connect(DB)
    con.row_factory = __import__("sqlite3").Row
    vi = rc.VectorIndex(str(VEC))

    if a.no_expand:
        q2 = a.query
        print(f"  ── 对照（不扩展）──")
    else:
        r = expand_query(con, a.query, verbose=True)
        q2 = r["query"]
        print(f"  ── 扩展后 ──")

    hits = rc.hybrid(con, vi, q2, top_k=a.top_k)
    print(f"  查询「{a.query}」")
    if not a.no_expand:
        print(f"  实际送入: {q2[:100]}")
    for h in hits:
        print(f"    {h['score']:.3f} [{h['kind']}] {str(h['text'])[:58]}")
    con.close()

if __name__ == "__main__":
    main()
