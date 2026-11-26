# -*- coding: utf-8 -*-

import os as _os
import sqlite3

import numpy as np

POOLS = (
    ("primary", ("subtitle_seg", "body")),
    ("derived", ("summary", "key_points", "insights", "seg_summary",
                 "annotation", "growth")),
    ("context", ("meta",)),
    ("reaction", ("comment", "danmaku")),
)

POOL_WEIGHT = {"fts": 0.9, "primary": 1.0, "derived": 0.7,
               "context": 0.45, "reaction": 0.3}

if (_os.environ.get("KB_POOL_EQUAL") or "") == "1":
    POOL_WEIGHT = {k: 1.0 for k in POOL_WEIGHT}

BRUTE_MAX = 20000

class PoolIndex:

    def __init__(self, con: sqlite3.Connection, vindex):
        self.con = con
        self.vi = vindex
        self.mats = {}
        self.big = {}
        for name, kinds in POOLS:
            ph = ",".join("?" * len(kinds))
            ids = [r[0] for r in con.execute(
                "SELECT id FROM content_block WHERE kind IN (%s)" % ph, kinds)]
            if not ids:
                continue
            if len(ids) > BRUTE_MAX:
                self.big[name] = list(kinds)
                continue
            vecs = []
            keep = []
            for bid in ids:
                try:
                    v = np.asarray(vindex.idx.get(int(bid)), dtype=np.float32).ravel()
                except Exception:
                    continue
                n = float(np.linalg.norm(v))
                if n <= 0:
                    continue
                vecs.append(v / n)
                keep.append(bid)
            if vecs:
                self.mats[name] = (np.asarray(keep, dtype=np.int64),
                                   np.vstack(vecs))

    def stats(self) -> dict:
        return {name: (len(v[0]) if name in self.mats else -1)
                for name, _ in POOLS for v in [self.mats.get(name, self.big.get(name))]}

    def search(self, query: str, qv, per_pool: int = 8, rrf_k: int = 60) -> list:
        qv = np.asarray(qv, dtype=np.float32).ravel()
        n = float(np.linalg.norm(qv))
        if n <= 0:
            return []
        qv = qv / n
        fused = {}
        for name, _ in POOLS:
            if name in self.mats:
                ids, mat = self.mats[name]
                sims = mat @ qv
                k = min(per_pool, len(ids))
                top = np.argpartition(-sims, k - 1)[:k] if k < len(ids) else np.arange(len(ids))
                top = top[np.argsort(-sims[top])]
                ranked = [(int(ids[i]), float(sims[i])) for i in top]
            elif name in self.big:

                raw = self.vi.search(qv, k=max(per_pool * 20, 200), with_dist=True)
                kinds = set(self.big[name])
                cand = []
                for bid, dist in raw:
                    row = self.con.execute(
                        "SELECT kind FROM content_block WHERE id=?", (int(bid),)).fetchone()
                    if row and row[0] in kinds:
                        cand.append((int(bid), 1.0 - float(dist)))
                    if len(cand) >= per_pool:
                        break
                ranked = cand
            else:
                continue
            w = POOL_WEIGHT.get(name, 0.5)
            for rank, (bid, sim) in enumerate(ranked, 1):
                _fuse(fused, bid, name, rank, sim, w, rrf_k)

        try:
            from bili_digest.retrieve import retrieve as _rv
            for rank, h in enumerate(
                    _rv.retrieve(self.con, self.vi, query, granularity="block",
                                 top_k=max(per_pool * 3, 24)), 1):
                bid = h.get("id") or h.get("block_id")
                if bid:
                    _fuse(fused, int(bid), "fts", rank,
                          float(h.get("score") or 0),
                          POOL_WEIGHT["fts"], rrf_k)
        except Exception:
            pass
        return sorted(fused.values(), key=lambda x: -x["score"])

def _fuse(fused: dict, bid: int, pool: str, rank: int, sim: float,
          weight: float, rrf_k: int) -> None:
    add = weight / (rrf_k + rank)
    cur = fused.get(bid)
    if cur is None:
        fused[bid] = {"block_id": bid, "pool": pool, "rank": rank,
                      "sim": round(float(sim), 4), "score": add}
    else:
        cur["score"] += add
        cur.setdefault("also", []).append(pool)
