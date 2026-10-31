# -*- coding: utf-8 -*-

import os as _os
import sys as _sys
from pathlib import Path

from bili_digest import paths

_ROOT = str(paths.HERE)

class Whitening:

    def __init__(self, mean=None, matrix=None):
        self.mean = mean
        self.matrix = matrix

    @property
    def ready(self):
        return self.mean is not None and self.matrix is not None

    def fit(self, vectors, verbose=False):
        import numpy as np
        X = np.asarray(vectors, dtype=np.float64)
        if X.ndim != 2 or len(X) < 50:
            if verbose:
                print("  样本太少（%d），不估计" % (0 if X.ndim != 2 else len(X)))
            return self
        mu = X.mean(axis=0, keepdims=True)
        Xc = X - mu
        cov = (Xc.T @ Xc) / max(len(Xc) - 1, 1)
        w, V = np.linalg.eigh(cov)
        w = np.maximum(w, 1e-12)
        self.mean = mu
        self.matrix = V @ np.diag(1.0 / np.sqrt(w)) @ V.T
        if verbose:
            anis = _anisotropy(X)
            print("  白化矩阵已估（样本 %d）· 原始各向异性 %.3f" % (len(X), anis))
        return self

    def apply(self, vec):
        import numpy as np
        if not self.ready:
            return vec
        v = np.asarray(vec, dtype=np.float64).reshape(1, -1)
        y = (v - self.mean) @ self.matrix
        n = np.linalg.norm(y)
        return (y / n if n > 0 else y).reshape(-1).astype(np.float32)

    def save(self, path):
        import json as _json
        import numpy as np
        if not self.ready:
            return False
        Path(path).write_text(_json.dumps({
            "mean": np.asarray(self.mean).tolist(),
            "matrix": np.asarray(self.matrix).tolist(),
        }), encoding="utf-8")
        return True

    @classmethod
    def load(cls, path):
        import json as _json
        import numpy as np
        p = Path(path)
        if not p.exists():
            return cls()
        try:
            d = _json.loads(p.read_text(encoding="utf-8"))
            return cls(mean=np.asarray(d["mean"], dtype=np.float64),
                       matrix=np.asarray(d["matrix"], dtype=np.float64))
        except Exception:
            return cls()

def _anisotropy(M):
    import numpy as np
    mu = M.mean(axis=0)
    mu = mu / (np.linalg.norm(mu) + 1e-9)
    Mn = M / (np.linalg.norm(M, axis=1, keepdims=True) + 1e-9)
    return float((Mn @ mu).mean())

def fit_from_index(vector_index, sample: int = 8000, verbose: bool = False,
                   con=None):
    import numpy as np
    idx = vector_index.idx
    if con is None:
        from bili_digest.schema.core_schema import connect
        from bili_digest.paths import DB
        con = connect(DB)
        close = True
    else:
        close = False
    try:
        keys = [r[0] for r in con.execute(
            "SELECT id FROM content_block ORDER BY id LIMIT 300000")]
    finally:
        if close:
            con.close()
    if len(keys) < 50:
        return Whitening()
    rng = np.random.default_rng(0)
    sel = rng.choice(np.array(keys), size=min(sample, len(keys)), replace=False)
    vecs = []
    for k in sel:
        try:
            v = np.asarray(idx.get(int(k)), dtype=np.float64).reshape(-1)
            if v.size and np.isfinite(v).all():
                vecs.append(v / max(np.linalg.norm(v), 1e-9))
        except Exception:
            pass
    if len(vecs) < 50:
        if verbose:
            print("  只取到 %d 条向量" % len(vecs))
        return Whitening()
    return Whitening().fit(np.vstack(vecs), verbose=verbose)

def main() -> int:
    import argparse
    from bili_digest import paths as _p
    _p.enable_utf8_stdout()
    ap = argparse.ArgumentParser(description="向量白化：估计与应用")
    ap.add_argument("--fit", action="store_true", help="估计并保存变换")
    ap.add_argument("--sample", type=int, default=8000)

    ap.add_argument("--out", default=str(Path(_ROOT) / "whitening.json"))
    a = ap.parse_args()

    from bili_digest.retrieve import rag_core as rc
    from bili_digest.paths import VEC
    vi = rc.VectorIndex(str(VEC))
    if a.fit:
        w = fit_from_index(vi, sample=a.sample, verbose=True)
        if w.save(a.out):
            print("  已保存 →", a.out)
        else:
            print("  估计失败（样本不足）")
        return
    print("  用法：--fit 估计并保存白化变换")

if __name__ == "__main__":
    main()
