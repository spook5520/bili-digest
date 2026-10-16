#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
import paths

import json
import random
import re
import sys
import time
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).parent

from paths import DB, VEC, EMBED_MODEL, EMBED_DIM

def lcs_len(a: str, b: str) -> int:
    if not a or not b:
        return 0
    if len(a) > len(b):
        a, b = b, a
    best = 0
    for i in range(len(a)):
        for j in range(i + best + 1, len(a) + 1):
            if a[i:j] in b:
                best = j - i
            else:
                break
    return best

def literal_ratio(query: str, source: str) -> float:
    return lcs_len(query, source) / max(len(query), 1)

def is_hit(returned_text: str, gold_text: str, gold_id: int,
           returned_id: int) -> bool:
    if returned_id == gold_id:
        return True
    r, g = (returned_text or "").strip(), (gold_text or "").strip()
    if not r or not g:
        return False
    if len(g) >= 10 and g in r:
        return True
    if len(r) >= 10 and r in g:
        return True
    return False

def build_cases(con, per_block: int = 1, seed: int = 23):
    rng = random.Random(seed)
    rows = con.execute(
        "SELECT id, text, kind FROM content_block "
        "WHERE LENGTH(text) BETWEEN 25 AND 400"
    ).fetchall()

    cases = []
    for r in rows:
        t = r["text"]

        variants = []

        if len(t) >= 20:
            s = rng.randint(0, len(t) - 18)
            variants.append(("literal", t[s:s + rng.randint(12, 18)]))

        words = [w for w in re.split(r"[，。！？；：、,.!?;:\s（）()\[\]]+", t)
                 if 2 <= len(w) <= 8]
        if len(words) >= 2:
            keep = words[:max(2, len(words) // 2)]
            variants.append(("partial", "".join(keep)))

        if len(t) >= 40:
            h = len(t) // 3
            variants.append(("semantic_like", (t[:h] + t[-h:])[:24]))

        for label, q in variants:
            cases.append({"q": q, "gold": r["id"], "kind": r["kind"],
                          "src": t, "label": label})

    for c in cases:
        c["ratio"] = literal_ratio(c["q"], c["src"])
        if c["ratio"] >= 0.6:
            c["band"] = "literal"
        elif c["ratio"] >= 0.3:
            c["band"] = "partial"
        else:
            c["band"] = "semantic"

    interfere = []
    for c in cases[:200]:
        src = c["src"]

        m = re.search(r"[\u4e00-\u9fff]{3,6}", src)
        if not m:
            continue
        fake = "".join(chr(0x4e00 + (ord(ch) % 0x1000) + 7) for ch in m.group(0))
        q = src.replace(m.group(0), fake, 1)[:40]
        interfere.append({"q": q, "gold": None, "kind": "interference",
                          "src": src, "band": "no_answer"})

    return cases, interfere

def evaluate(name, encode_docs, encode_query, blocks, cases,
             interfere, topk=10):
    import numpy as np

    texts = [b["text"] for b in blocks]
    t0 = time.time()
    D = encode_docs(texts)
    enc_sec = time.time() - t0
    D = D / (np.linalg.norm(D, axis=1, keepdims=True) + 1e-9)
    id2row = {b["id"]: i for i, b in enumerate(blocks)}

    def ranks_for(qs):
        Q = encode_query(qs)
        Q = Q / (np.linalg.norm(Q, axis=1, keepdims=True) + 1e-9)
        out = []
        for i in range(0, len(Q), 128):
            sims = Q[i:i + 128] @ D.T
            out.extend(np.argsort(-sims, axis=1)[:, :topk].tolist())
        return out

    ordered = ranks_for([c["q"] for c in cases])
    thru = len(texts) / max(enc_sec, 0.01)

    print(f"\n  【{name}】  编码 {thru:.0f} 条/秒")

    print(f"    {'字面档':<10}{'样本':>6}{'R@1':>9}{'R@5':>9}{'MRR':>9}")
    by_band = {}
    for c, order in zip(cases, ordered):
        rank = next((i + 1 for i, r in enumerate(order)
                     if is_hit(blocks[r]["text"], c["src"], c["gold"],
                               blocks[r]["id"])), None)
        d = by_band.setdefault(c["band"], {"n": 0, "r1": 0, "r5": 0, "mrr": 0.0})
        d["n"] += 1
        if rank:
            if rank <= 1: d["r1"] += 1
            if rank <= 5: d["r5"] += 1
            d["mrr"] += 1.0 / rank
    for b in ("literal", "partial", "semantic"):
        d = by_band.get(b)
        if d and d["n"]:
            print(f"    {b:<10}{d['n']:>6}{d['r1']/d['n']:>8.1%}"
                  f"{d['r5']/d['n']:>9.1%}{d['mrr']/d['n']:>9.3f}")

    print(f"    {'内容源':<10}{'样本':>6}{'R@1':>9}{'MRR':>9}")
    by_kind = {}
    for c, order in zip(cases, ordered):
        rank = next((i + 1 for i, r in enumerate(order)
                     if is_hit(blocks[r]["text"], c["src"], c["gold"],
                               blocks[r]["id"])), None)
        d = by_kind.setdefault(c["kind"], {"n": 0, "r1": 0, "mrr": 0.0})
        d["n"] += 1
        if rank:
            if rank <= 1: d["r1"] += 1
            d["mrr"] += 1.0 / rank
    for k, d in sorted(by_kind.items(), key=lambda x: -x[1]["n"]):
        if d["n"]:
            print(f"    {k:<10}{d['n']:>6}{d['r1']/d['n']:>8.1%}"
                  f"{d['mrr']/d['n']:>9.3f}")

    if interfere:
        iordered = ranks_for([c["q"] for c in interfere])
        top1_sims = []
        Q = encode_query([c["q"] for c in interfere])
        Q = Q / (np.linalg.norm(Q, axis=1, keepdims=True) + 1e-9)
        for i, order in enumerate(iordered):
            top1_sims.append(float(D[order[0]] @ Q[i]))
        import statistics
        answered = sum(1 for s in top1_sims if s > 0.6)
        print(f"    {'干扰查询':<10}{len(interfere):>6}  "
              f"top1 相似度 中位 {statistics.median(top1_sims):.3f}  "
              f"均值 {statistics.mean(top1_sims):.3f}  "
              f"高置信假阳性(>0.6) {answered}/{len(interfere)}")

    return {"name": name, "thru": thru,
            "bands": {k: {kk: (vv / d["n"] if kk != "n" else vv)
                          for kk, vv in d.items()} for k, d in by_band.items()},
            "kinds": {k: {kk: (vv / d["n"] if kk != "n" else vv)
                          for kk, vv in d.items()} for k, d in by_kind.items()}}

def main():
    import argparse
    import os
    os.environ.setdefault("HF_ENDPOINT", __import__("paths").HF_ENDPOINT)
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="small")
    ap.add_argument("--limit-blocks", type=int, default=2000)
    a = ap.parse_args()

    import sqlite3
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id, text, kind FROM content_block WHERE LENGTH(text) BETWEEN 25 AND 400 "
        "ORDER BY id LIMIT ?", (a.limit_blocks,)).fetchall()
    blocks = [{"id": r["id"], "text": r["text"], "kind": r["kind"]} for r in rows]
    cases, interfere = build_cases(con, seed=23)
    con.close()

    from collections import Counter
    print(f"  语料 {len(blocks)} 块   查询 {len(cases)} 条   干扰 {len(interfere)} 条")
    print(f"  字面度分布 {dict(Counter(c['band'] for c in cases))}")
    print(f"  内容源分布 {dict(Counter(c['kind'] for c in cases))}")

    results = []
    want = a.models.split(",")
    if "small" in want:
        from fastembed import TextEmbedding
        m = TextEmbedding(model_name=EMBED_MODEL, cache_dir=str(HERE / "models"))
        np_ = lambda x: __import__("numpy").array(x, dtype="float32")
        results.append(evaluate("bge-small", lambda t: np_(list(m.embed(t))),
                                lambda t: np_(list(m.embed(t))), blocks, cases, interfere))
    if "base" in want:
        from embed_onnx import OnnxEmbedder
        m = OnnxEmbedder(HERE / "models/bge-base-onnx", int8=True)
        results.append(evaluate("bge-base(int8)", m.embed_documents,
                                m.embed_queries, blocks, cases, interfere))

    out = HERE / "bench3_result.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  结果存 {out.name}")

if __name__ == "__main__":
    main()
