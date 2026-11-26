import sys

def _cos(a, b):
    import numpy as np
    na = float(np.linalg.norm(a)); nb = float(np.linalg.norm(b))
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))

def chapters_semantic(con, item_uid, min_sec: int = 180, max_sec: int = 480,
                      drop: float = 0.30, max_n: int = 16):
    rows = list(con.execute(
        "SELECT offset_sec, text FROM content_block "
        "WHERE item_uid=? AND kind='subtitle_seg' ORDER BY offset_sec",
        (item_uid,)))
    if len(rows) < 6:
        return []
    texts = [" ".join((r["text"] or "").split())[:200] for r in rows]

    try:
        import numpy as _np
        from bili_digest.retrieve import rag_core as _rc
        _model = _rc.deps()["model"]
        vecs = _np.array([list(v) for v in _model.passage_embed(texts)],
                         dtype=_np.float32)
    except Exception:
        return []
    sims = [_cos(vecs[i], vecs[i + 1]) for i in range(len(vecs) - 1)]
    if not sims:
        return []
    med = sorted(sims)[len(sims) // 2]
    thr = med * (1 - drop)
    groups, cur = [], [0]
    for i in range(1, len(rows)):
        t0 = rows[cur[0]]["offset_sec"] or 0
        t1 = rows[i]["offset_sec"] or 0
        span = t1 - t0
        if span >= max_sec or (sims[i - 1] < thr and span >= min_sec):
            groups.append(cur)
            cur = [i]
        else:
            cur.append(i)
    if cur:
        groups.append(cur)

    while len(groups) > max_n:
        k = min(range(len(groups) - 1),
                key=lambda j: (rows[groups[j + 1][0]]["offset_sec"] or 0)
                - (rows[groups[j][0]]["offset_sec"] or 0))
        groups[k:k + 2] = [groups[k] + groups[k + 1]]
    out = []
    for g in groups:
        if len(g) < 2:
            continue
        best = max((rows[i] for i in g),
                   key=lambda x: len((x["text"] or "").strip()))
        txt = " ".join((best["text"] or "").split())[:96]
        if txt:
            out.append({"t": int(rows[g[0]]["offset_sec"] or 0),
                        "text": txt, "n": len(g)})
    return out
