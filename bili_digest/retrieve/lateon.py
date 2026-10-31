# -*- coding: utf-8 -*-

import hashlib
import os
import sys

from bili_digest import paths

import numpy as np

def _fingerprint(con) -> str:
    row = con.execute(
        "SELECT COUNT(*), COALESCE(MAX(id), 0), COALESCE(SUM(LENGTH(text)), 0) "
        "FROM content_block WHERE kind = ?", ("summary",)).fetchone()
    return "summary:%d:%d:%d" % (row[0], row[1], row[2])

def cache_meta_path():
    return paths.LATEON_CACHE.with_suffix(".meta")

def available(con=None) -> bool:
    try:
        if not paths.LATEON_CACHE.is_file():
            return False
        if con is None:
            return True
        meta = cache_meta_path()
        if not meta.is_file():
            return False
        return meta.read_text(encoding="utf-8").strip() == _fingerprint(con)
    except Exception:
        return False

def build(con, force: bool = False, batch: int = 32, verbose: bool = True) -> dict:
    import time
    fp = _fingerprint(con)
    if not force and available(con):
        return {"built": False, "items": 0, "seconds": 0.0, "mb": 0.0, "reason": "cache is current"}

    from fastembed import LateInteractionTextEmbedding
    os.environ.setdefault("HF_ENDPOINT", paths.HF_ENDPOINT)
    os.environ["HF_HUB_DISABLE_XET"] = "1"

    rows = con.execute(
        "SELECT item_uid, text FROM content_block WHERE kind = ? GROUP BY item_uid",
        ("summary",)).fetchall()
    uids = [r[0] for r in rows]
    texts = [str(r[1] or "")[:400] for r in rows]
    if verbose:
        print("  待编码 %d 条 summary" % len(uids), flush=True)

    model = LateInteractionTextEmbedding(model_name=paths.LATEON_MODEL,
                                        cache_dir=str(paths.MODELS_DIR))
    t0 = time.time()
    arrs = {}
    for i in range(0, len(uids), batch):
        chunk_u = uids[i:i + batch]
        chunk_t = texts[i:i + batch]
        for u, v in zip(chunk_u, model.embed(chunk_t)):
            arrs[u] = np.asarray(v, dtype="float32")
        if verbose and i % (batch * 5) == 0:
            print("    %d/%d  %.0f 秒" % (i, len(uids), time.time() - t0), flush=True)
    secs = time.time() - t0

    out = {u.replace(":", "_", 1): arrs[u] for u in uids}
    paths.LATEON_CACHE.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(str(paths.LATEON_CACHE), **out)
    cache_meta_path().write_text(fp, encoding="utf-8")
    mb = paths.LATEON_CACHE.stat().st_size / 1e6
    if verbose:
        print("  已写 %s（%.1f MB · %d 条 · %.1f 秒）" % (
            paths.LATEON_CACHE, mb, len(uids), secs))
    return {"built": True, "items": len(uids), "seconds": secs, "mb": mb}

_CACHE = {"fp": None, "enc": None, "model": None}

def _load(con):
    fp = _fingerprint(con)
    if _CACHE["fp"] == fp and _CACHE["enc"] is not None:
        return _CACHE["enc"]
    if not paths.LATEON_CACHE.is_file():
        return None
    try:
        z = np.load(str(paths.LATEON_CACHE))
        enc = {k.replace("_", ":", 1): z[k] for k in z.files}
    except Exception:
        return None
    _CACHE["fp"], _CACHE["enc"] = fp, enc
    return enc

def _model():
    if _CACHE["model"] is None:
        from fastembed import LateInteractionTextEmbedding
        os.environ.setdefault("HF_ENDPOINT", paths.HF_ENDPOINT)
        _CACHE["model"] = LateInteractionTextEmbedding(
            model_name=paths.LATEON_MODEL, cache_dir=str(paths.MODELS_DIR))
    return _CACHE["model"]

def maxsim(qv, dv) -> float:
    q = np.asarray(qv, dtype="float32")
    d = np.asarray(dv, dtype="float32")
    return float((q @ d.T).max(axis=1).sum())

def rerank(con, question: str, uids: list) -> list:
    if not uids:
        return uids
    enc = _load(con)
    if not enc:
        return uids
    cand = [u for u in uids if u in enc]
    if len(cand) < 2:
        return uids
    try:
        model = _model()
        qv = np.asarray(list(model.query_embed([question]))[0], dtype="float32")
    except Exception:
        return uids
    scored = sorted(((u, maxsim(qv, enc[u])) for u in cand), key=lambda kv: -kv[1])
    order = [u for u, _ in scored]

    rest = [u for u in uids if u not in enc]
    return order + rest
