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
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).parent

LIFT_GOOD = 0.06
LIFT_POOR = 0.015
COVER_GOOD = 0.6
COVER_POOR = 0.25
FOCUS_GOOD = 0.5
FOCUS_POOR = 0.2

def _baseline_sim(con, model_embed, query: str, n: int = 8) -> float:
    import numpy as np
    rows = con.execute(
        "SELECT text FROM content_block WHERE LENGTH(text) > 30 "
        "ORDER BY RANDOM() LIMIT ?", (n,)).fetchall()
    if not rows:
        return 0.0
    qv = np.array(list(model_embed.query_embed([query]))[0], dtype=np.float32)
    sims = []
    for r in rows:
        dv = np.array(list(model_embed.embed([r["text"][:300]]))[0], dtype=np.float32)
        d = (np.linalg.norm(qv) * np.linalg.norm(dv))
        sims.append(float(qv @ dv / d) if d else 0.0)
    return sum(sims) / len(sims)

def assess(con, query: str, hits: list, top_n: int = 8,
           model_embed=None) -> dict:
    if not hits:
        return {"verdict": "poor", "signals": {}, "reason": "无结果"}

    hs = hits[:top_n]

    lift = None
    if model_embed is not None:
        try:
            import numpy as np
            base = _baseline_sim(con, model_embed, query)
            top_text = str(hs[0].get("text") or "")[:300]
            qv = np.array(list(model_embed.query_embed([query]))[0], dtype=np.float32)
            dv = np.array(list(model_embed.embed([top_text]))[0], dtype=np.float32)
            d = (np.linalg.norm(qv) * np.linalg.norm(dv))
            top_sim = float(qv @ dv / d) if d else 0.0
            lift = top_sim - base
        except Exception:
            lift = None

    cover = 1.0
    try:
        import aggregate as ag
        qw = ag.query_content_terms(con, query)
        if qw:

            blob = " ".join(str(h.get("text") or "") for h in hs).lower()
            cover = sum(1 for w in qw if w.lower() in blob) / len(qw)
    except Exception:
        pass

    from collections import Counter
    src = Counter(h.get("item_uid") for h in hs if h.get("item_uid"))
    focus = (max(src.values()) / len(hs)) if src and len(hs) else 0.0

    votes_good = votes_poor = 0
    if lift is not None:
        votes_good += lift >= LIFT_GOOD
        votes_poor += lift < LIFT_POOR
    votes_good += cover >= COVER_GOOD
    votes_poor += cover < COVER_POOR
    votes_good += focus >= FOCUS_GOOD
    votes_poor += focus < FOCUS_POOR

    if votes_poor >= 2:
        verdict = "poor"
    elif votes_good >= 2:
        verdict = "good"
    else:
        verdict = "ambiguous"

    why = []
    if lift is not None:
        why.append(f"相对随机基线提升 {lift:+.3f}")
    why.append(f"内容词覆盖 {cover:.0%}")
    why.append(f"来源集中度 {focus:.0%}（{len(src)} 来源）")

    return {"verdict": verdict,
            "signals": {"lift": (round(lift, 4) if lift is not None else None),
                        "cover": round(cover, 3), "focus": round(focus, 3),
                        "n_sources": len(src)},
            "reason": "；".join(why)}

def annotate(res: dict) -> str:
    v = res["verdict"]
    if v == "good":
        return "✓ 结果可信"
    if v == "poor":
        return "✗ 大概率没找到（库里可能没有，或换个说法）"
    return "… 不太确定，仅供参考"

def main():
    ap = argparse.ArgumentParser(description="检索质量自评估")
    ap.add_argument("query")
    ap.add_argument("--top-k", type=int, default=8)
    a = ap.parse_args()

    from core_schema import connect
    from paths import DB, VEC
    import rag_core as rc
    con = connect(DB)
    con.row_factory = __import__("sqlite3").Row
    vi = rc.VectorIndex(str(VEC))
    hits = rc.hybrid(con, vi, a.query, top_k=a.top_k)
    try:
        from fastembed import TextEmbedding
        import paths as _p
        model = TextEmbedding(model_name=_p.EMBED_MODEL,
                              cache_dir=str(_p.MODELS_DIR))
    except Exception:
        model = None
    res = assess(con, a.query, hits, model_embed=model)
    print(f"  「{a.query}」")
    print(f"    判定: {res['verdict']}  {annotate(res)}")
    print(f"    信号: {res['signals']}")
    print(f"    依据: {res['reason']}")
    con.close()

if __name__ == "__main__":
    main()
