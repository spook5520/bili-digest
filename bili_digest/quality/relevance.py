# -*- coding: utf-8 -*-

import os as _os
import sys as _sys

ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
from bili_digest import paths

import json as _json
import jieba as _jieba

def _terms(s):
    from bili_digest.retrieve import rag_core as rc
    return set(t for t in _jieba.lcut(str(s or ""))
               if t.strip() and t not in rc.STOPWORDS and len(t) > 1)

def build(con, with_vec: bool = True):
    from collections import Counter
    topics, df, title, dur, author, summ = {}, Counter(), {}, {}, {}, {}
    for r in con.execute("SELECT item_uid, content FROM derived WHERE kind = ?", ("topics",)):
        try:
            ts = set(str(x).strip() for x in _json.loads(str(r[1])) if str(x).strip())
        except Exception:
            continue
        topics[r[0]] = ts
        for t in ts:
            df[t] += 1
    for r in con.execute("SELECT uid, title, duration_sec, author_name FROM item"):
        title[r[0]] = str(r[1] or "")
        dur[r[0]] = r[2] or 0
        author[r[0]] = str(r[3] or "")
    vec = {}
    if with_vec:
        try:
            import numpy as _np
            from bili_digest.retrieve import rag_core as rc
            d = rc.deps()
            model = d["model"]
            for r in con.execute(
                    "SELECT item_uid, text FROM content_block WHERE kind = ?",
                    ("summary",)):
                summ.setdefault(r[0], str(r[1] or ""))
            for u, s in summ.items():
                if not s:
                    continue
                v = _np.array(list(model.query_embed([s[:400]]))[0], dtype=_np.float32)
                vec[u] = v / (_np.linalg.norm(v) + 1e-9)
        except Exception:
            vec = {}
    return {"topics": topics, "df": df, "title": title, "dur": dur,
            "author": author, "vec": vec}

def score(ctx, a, b):
    if not a or not b or a == b:
        return 0.0, {}
    hits = {}
    ta = set(t for t in ctx["topics"].get(a, set()) if ctx["df"][t] >= 2)
    tb = set(t for t in ctx["topics"].get(b, set()) if ctx["df"][t] >= 2)
    if ta & tb:
        hits["topic_shared"] = 1.0
    if len(_terms(ctx["title"].get(a)) & _terms(ctx["title"].get(b))) >= 2:
        hits["title_overlap"] = 1.0
    da, db = ctx["dur"].get(a) or 0, ctx["dur"].get(b) or 0
    if da > 0 and db > 0:
        r = float(da) / float(db)
        if 0.5 <= r <= 2.0:
            hits["duration_ratio"] = 1.0
    if ctx["author"].get(a) and ctx["author"].get(a) == ctx["author"].get(b):
        hits["same_author"] = 1.0
    va, vb = ctx.get("vec", {}).get(a), ctx.get("vec", {}).get(b)
    if va is not None and vb is not None:
        try:
            if float(va @ vb) > 0.6:
                hits["vec_sim"] = 1.0
        except Exception:
            pass
    return sum(hits.values()), hits

def is_relevant(ctx, a, b, thresh: float = 2.0) -> bool:
    return score(ctx, a, b)[0] >= thresh
