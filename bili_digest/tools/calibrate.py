#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os as _os
import sys as _sys

from bili_digest import paths

import argparse
import json
import math
import sys
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).parent

from bili_digest.schema.core_schema import connect
from bili_digest.paths import DB

NEGATIVE_QUERIES = [
    "量子纠缠退相干时间的测量方法", "塞尔维亚语名词变格表", "火山岩薄片偏光鉴定",
    "宋朝盐铁专营制度的演变", "北极燕鸥的迁徙路线", "罗马混凝土的火山灰配方",
    "鸟类羽毛的微观结构", "敦煌壁画颜料成分分析", "亚马逊雨林冠层物种调查",
    "古埃及象形文字破译史", "深海热液喷口的化能合成", "日语敬语的二重敬语规范",
]

POSITIVE_QUERIES_MIN = 12

def score_query(con, vi, model, q: str) -> float:
    import numpy as np
    from bili_digest.retrieve import rag_core as rc
    v = np.array(list(model.query_embed([q]))[0], dtype=np.float32)
    v = v / (np.linalg.norm(v) + 1e-9)
    r = vi.search(v, 1, with_dist=True)
    return 1.0 - float(r[0][1]) if r else 0.0

def collect_samples(con, vi, model):
    import random
    q = con.execute
    rnd = random.Random(11)

    items = q("""SELECT i.uid, i.title FROM item i
        WHERE EXISTS(SELECT 1 FROM content_block cb WHERE cb.item_uid=i.uid
            AND cb.kind IN ('subtitle','subtitle_seg','danmaku','comment'))""").fetchall()
    rnd.shuffle(items)

    pos, used = [], 0
    for r in items:
        if used >= 40:
            break

        tp = q("""SELECT content FROM derived WHERE item_uid=? AND kind='topics'
            LIMIT 1""", (r["uid"],)).fetchone()

        sm = q("""SELECT content FROM derived WHERE item_uid=? AND kind='summary'
            LIMIT 1""", (r["uid"],)).fetchone()
        cands = []
        if tp and tp["content"]:
            try:
                t = json.loads(tp["content"])
                if isinstance(t, list) and len(t) >= 2:
                    cands.append(" ".join(str(x) for x in t[:3]))
            except Exception:
                pass
        if sm and sm["content"]:
            txt = str(sm["content"])
            try:
                txt = json.loads(txt)
            except Exception:
                pass
            txt = str(txt)
            if len(txt) >= 24:
                cands.append(txt[8:48])
        if not cands:
            continue
        qy = rnd.choice(cands)
        if len(qy.strip()) < 4:
            continue
        pos.append((score_query(con, vi, model, qy), 1))
        used += 1
    neg = [(score_query(con, vi, model, t), 0) for t in NEGATIVE_QUERIES]
    return pos, neg

def fit_logistic(samples, iters: int = 400, lr: float = 0.5):
    a, b = 0.0, 0.0
    n = len(samples)
    for _ in range(iters):
        ga = gb = 0.0
        for s, y in samples:
            p = 1.0 / (1.0 + math.exp(-(a * s + b)))
            ga += (p - y) * s
            gb += (p - y)
        a -= lr * ga / n
        b -= lr * gb / n
    return a, b

def predict(a, b, s: float) -> float:
    return 1.0 / (1.0 + math.exp(-(a * s + b)))

def ece(pairs, bins: int = 8) -> float:
    buckets = [[] for _ in range(bins)]
    for p, y in pairs:
        buckets[min(int(p * bins), bins - 1)].append((p, y))
    e = 0.0
    n = len(pairs) or 1
    for bk in buckets:
        if not bk:
            continue
        avg_p = sum(x[0] for x in bk) / len(bk)
        avg_y = sum(x[1] for x in bk) / len(bk)
        e += len(bk) / n * abs(avg_p - avg_y)
    return e

def auroc(pairs) -> float:
    pos = [p for p, y in pairs if y == 1]
    neg = [p for p, y in pairs if y == 0]
    if not pos or not neg:
        return float("nan")
    win = sum(1 for a in pos for b in neg if a > b) + \
        0.5 * sum(1 for a in pos for b in neg if a == b)
    return win / (len(pos) * len(neg))

def main() -> int:
    ap = argparse.ArgumentParser(description="标定「库里有没有」的概率")
    ap.add_argument("--apply", action="store_true", help="把参数写进 partitions.json")
    a = ap.parse_args()

    from bili_digest.retrieve import rag_core as rc
    from bili_digest.paths import VEC
    con = connect(DB)
    con.row_factory = __import__("sqlite3").Row
    vi = rc.VectorIndex(str(VEC))
    model = rc.deps()["model"]

    pos, neg = collect_samples(con, vi, model)
    samples = pos + neg
    print(f"  构造样本：正 {len(pos)} · 负 {len(neg)}")
    ps = [s for s, _ in pos]
    ns = [s for s, _ in neg]
    print(f"    正样本贴近度 {min(ps):.3f}~{max(ps):.3f}")
    print(f"    负样本贴近度 {min(ns):.3f}~{max(ns):.3f}")

    alpha, beta = fit_logistic(samples)
    print(f"\n  拟合 p = σ({alpha:.3f}·s + {beta:.3f})")

    raw_pairs = [(s, y) for s, y in samples]
    cal_pairs = [(predict(alpha, beta, s), y) for s, y in samples]
    print(f"\n  概率质量：")
    print(f"    原始相似度 ECE {ece(raw_pairs):.3f} · AUROC {auroc(raw_pairs):.3f}")
    print(f"    标定后     ECE {ece(cal_pairs):.3f} · AUROC {auroc(cal_pairs):.3f}")

    print(f"\n  标定后的解释（p = 库里确有相关的概率）：")
    for s in (0.55, 0.65, 0.72, 0.80, 0.85):
        print(f"    贴近度 {s:.2f} → p = {predict(alpha, beta, s):.2f}")

    if a.apply:
        pp = HERE / "partitions.json"
        cfg = json.loads(pp.read_text(encoding="utf-8")) if pp.exists() else {}
        cfg["calibration"] = {"alpha": round(alpha, 6), "beta": round(beta, 6),
                             "note": "由 calibrate.py 拟合（构造样本，非人工标注）"}
        pp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n  ✓ 已写入 {pp.name} 的 calibration 段")
    else:
        print("\n  （加 --apply 才会写入配置）")
    con.close()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
