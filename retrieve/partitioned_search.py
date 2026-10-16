#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
import paths

import json
import math
import sqlite3
import sys
import time
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).parent

from paths import DB, VEC, EMBED_MODEL

DEFAULT_CONFIG = {
    "partitions": {
        "seg":   {"kinds": ["subtitle_seg", "meta"], "weight": 1.0},
        "short": {"kinds": ["danmaku", "comment"], "weight": 0.9},
        "line":  {"kinds": ["subtitle"], "weight": 0.65},
    },

    "sigmoid": {"center": 0.80, "slope": 12.0},

    "min_sim": 0.72,
}

CONFIG_PATH = HERE / "partitions.json"

def _load_config(path: Path = None) -> dict:
    p = Path(path) if path else CONFIG_PATH
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if p.exists():
        try:
            user = json.loads(p.read_text(encoding="utf-8"))
            for k, v in user.items():
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    for kk, vv in v.items():
                        if isinstance(vv, dict) and isinstance(cfg[k].get(kk), dict):
                            cfg[k][kk].update(vv)
                        else:
                            cfg[k][kk] = vv
                else:
                    cfg[k] = v
        except Exception as e:
            import sys as _s
            print(f"  [!] partitions.json 解析失败，用默认值：{e}", file=_s.stderr)
    return cfg

_CFG = _load_config()

def _partitions() -> dict:
    return {k: v["kinds"] for k, v in _CFG["partitions"].items()}

def _weight(name: str) -> float:
    return float(_CFG["partitions"].get(name, {}).get("weight", 1.0))

def _norm_scores(scores: list[float]) -> list[float]:
    center = float(_CFG["sigmoid"]["center"])
    slope = float(_CFG["sigmoid"]["slope"])
    out = []
    for s in scores:
        try:
            out.append(1.0 / (1.0 + math.exp(-(float(s) - center) * slope)))
        except OverflowError:
            out.append(0.0 if s < center else 1.0)
    return out

def _load_index():
    from usearch.index import Index, MetricKind, ScalarKind
    from paths import EMBED_DIM

    idx = Index(ndim=EMBED_DIM, metric=MetricKind.Cos, dtype=ScalarKind.F32)
    idx.load(str(VEC))
    return idx

def search(con, vi, model, query: str, top_k: int = 8,
           partitions: dict = None, per_partition: int = 20,
           item_filter: str = None, min_sim: float = None) -> list[dict]:
    import numpy as np
    parts = partitions or _partitions()
    if min_sim is None:
        min_sim = float(_CFG.get("min_sim", 0.72))

    from rag_core import normalize_query
    query = normalize_query(query)

    qv = np.array(list(model.query_embed([query]))[0], dtype=np.float32)
    qv = qv / (np.linalg.norm(qv) + 1e-9)

    total = sum(per_partition for _ in parts)
    r = vi.search(qv, min(total * 3, 400))
    keys = r.keys.tolist()
    dists = r.distances.tolist()

    rows = {}
    for k, d in zip(keys, dists):
        row = con.execute(
            "SELECT cb.id,cb.item_uid,cb.kind,cb.offset_sec,cb.text,"
            "cb.author,cb.weight,i.title,i.url,i.item_id "
            "FROM content_block cb JOIN item i ON i.uid=cb.item_uid "
            "WHERE cb.id=?", (int(k),)).fetchone()
        if not row:
            continue
        if item_filter and row["item_uid"] != item_filter:
            continue
        rows[int(k)] = (dict(row), 1.0 - float(d))

    out = []
    dropped = []
    for pname, kinds in parts.items():
        cand = [(k, v[0], v[1]) for k, v in rows.items() if v[0]["kind"] in kinds]
        if not cand:
            continue
        cand.sort(key=lambda x: -x[2])

        if cand[0][2] < min_sim:
            dropped.append((pname, round(cand[0][2], 3)))
            continue
        cand = cand[:per_partition]
        normed = _norm_scores([c[2] for c in cand])
        w = _weight(pname)
        for (k, row, raw), s in zip(cand, normed):
            out.append({
                "block_id": k, "item_uid": row["item_uid"],
                "kind": row["kind"], "partition": pname,
                "text": row["text"], "title": row["title"],
                "url": row["url"], "item_id": row["item_id"],
                "offset_sec": row["offset_sec"], "author": row["author"],
                "raw_sim": round(raw, 4),
                "part_score": round(s * w, 4),
            })

    out.sort(key=lambda x: -x["part_score"])
    if dropped:
        out.append({"_dropped_partitions": dropped})
    return out[:top_k]

def main():
    import argparse
    import os
    os.environ.setdefault("HF_ENDPOINT", __import__("paths").HF_ENDPOINT)
    ap = argparse.ArgumentParser(description="分区检索")
    ap.add_argument("query")
    ap.add_argument("--top-k", type=int, default=8)
    ap.add_argument("--item", help="限定条目")
    a = ap.parse_args()

    import rag_core as rc
    from fastembed import TextEmbedding
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    vi = _load_index()
    model = TextEmbedding(model_name=EMBED_MODEL, cache_dir=str(HERE / "models"))

    t0 = time.time()
    hits = search(con, vi, model, a.query, a.top_k, item_filter=a.item)
    dt = time.time() - t0

    print(f"  查询「{a.query}」  {dt*1000:.0f}ms\n")
    for i, h in enumerate(hits, 1):
        loc = {"subtitle_seg": "段落", "subtitle": "字幕行", "danmaku": "弹幕",
               "comment": "评论", "meta": "标题简介"}.get(h["kind"], h["kind"])
        tm = ""
        if h["offset_sec"] is not None:
            m, s = divmod(int(h["offset_sec"]), 60)
            tm = f" {m:02d}:{s:02d}"
        print(f"  [{i}] {h['part_score']:.3f} [{h['partition']}/{loc}]{tm} "
              f"{h['text'][:56]}")
        print(f"        ← 《{str(h['title'])[:36]}》")
    con.close()

if __name__ == "__main__":
    main()
