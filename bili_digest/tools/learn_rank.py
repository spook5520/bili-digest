# -*- coding: utf-8 -*-

from __future__ import annotations

import json
import math
import random
import sys
from collections import defaultdict
from pathlib import Path

FLYWHEEL = Path(".cache/flywheel.jsonl")

KINDS = ("subtitle", "subtitle_seg", "summary", "key_points", "insights",
         "audience", "growth", "meta", "annotation", "comment", "danmaku")

def load_rows(path: Path = FLYWHEEL) -> list[dict]:
    out = []
    with path.open(encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                d = json.loads(ln)
            except Exception:
                continue
            if "q" not in d or "score" not in d:
                continue
            out.append(d)
    return out

def group_by_query(rows: list[dict]) -> dict:
    g = defaultdict(list)
    for r in rows:
        g[r["q"]].append(r)

    return {q: v for q, v in g.items() if len(v) >= 2}

def _maxsim_features(sims: list[float]) -> tuple:
    n = len(sims)
    order = sorted(range(n), key=lambda i: -sims[i])
    rank_of = {i: r for r, i in enumerate(order)}
    top = max(sims) if sims else 0.0
    mean = sum(sims) / n if n else 0.0
    var = sum((s - mean) ** 2 for s in sims) / n if n else 0.0
    sd = math.sqrt(var) or 1e-9
    f_rank, f_rel, f_z = [], [], []
    for i, s in enumerate(sims):
        f_rank.append(1.0 / (1.0 + rank_of[i]))
        f_rel.append((s / top) if top else 0.0)
        f_z.append((s - mean) / sd)
    return f_rank, f_rel, f_z

def build_pairs(groups: dict, con=None) -> list[tuple]:
    from bili_digest.retrieve.lateon import maxsim, _model, _load
    import numpy as np

    kind_of, refuted = {}, set()
    if con is not None:
        try:
            for bid, k in con.execute("SELECT id, kind FROM content_block"):
                kind_of[bid] = k
        except Exception:
            pass
        try:
            from bili_digest.quality.refute import refuted_pairs
            refuted = refuted_pairs(con)
        except Exception:
            pass

    enc = _load(con) if con is not None else None
    model = None
    try:
        model = _model()
    except Exception:
        pass

    pairs = []
    for q, rows in groups.items():
        if model is None:
            break
        try:
            qv = np.asarray(list(model.query_embed([q]))[0], dtype="float32")
        except Exception:
            continue

        sims = []
        for r in rows:
            dv = enc.get(r.get("item_uid")) if enc is not None else None
            if dv is None and model is not None:
                try:
                    dv = np.asarray(list(model.embed(
                        [(r.get("text") or " ")[:400]]))[0], dtype="float32")
                except Exception:
                    dv = None
            sims.append(maxsim(qv, dv) if dv is not None else 0.0)
        f_rank, f_rel, f_z = _maxsim_features(sims)
        feats = []
        for i, r in enumerate(rows):
            k = kind_of.get(r.get("block_id"), "")
            ohot = [1.0 if k == kk else 0.0 for kk in KINDS]
            txt = r.get("text") or ""
            feats.append([
                f_rank[i], f_rel[i], f_z[i],
                *ohot,
                1.0 if len(txt) < 40 else 0.0,
                1.0 if len(q) > 12 and k in ("comment", "danmaku") else 0.0,
                1.0 if (r.get("item_uid"), k) in refuted else 0.0,
                float(r.get("ts") or 0) / 1e9,
            ])

        for a in range(len(rows)):
            for b in range(a + 1, len(rows)):
                sa, sb = rows[a].get("score", 0), rows[b].get("score", 0)
                if sa == sb:
                    continue
                dndcg = abs(2 ** sa - 2 ** sb) / 7.0
                if sa > sb:
                    pairs.append((feats[a], feats[b], dndcg, sims[a], sims[b]))
                else:
                    pairs.append((feats[b], feats[a], dndcg, sims[b], sims[a]))
    return pairs

def filter_summary(rows: list[dict], con=None) -> list[dict]:
    if con is None:
        return rows
    kind_of = {}
    try:
        for bid, k in con.execute("SELECT id, kind FROM content_block"):
            kind_of[bid] = k
    except Exception:
        return rows
    return [r for r in rows if kind_of.get(r.get("block_id")) == "summary"]

def kfold_both(pairs: list[tuple], k: int, dim: int) -> dict:
    if not pairs:
        return {"lr_folds": [], "lr_mean": 0.0, "bs_folds": [], "bs_mean": 0.0}
    idx = list(range(len(pairs)))
    random.Random(20261110).shuffle(idx)
    step = max(1, len(idx) // k)
    lr_accs, bs_accs = [], []
    for f in range(k):
        test = set(idx[f * step:(f + 1) * step])
        te = [pairs[i] for i in sorted(test)]

        ok = 0
        for _fi, _fj, _wt, si, sj in te:
            if si > sj:
                ok += 1
        bs_accs.append(ok / max(len(te), 1))

        tr = [pairs[i] for i in idx if i not in test]
        w = fit_logistic(tr, dim)
        ok = 0
        for fi, fj, _wt, si, sj in te:
            sii = sum(fi[m] * w[m] for m in range(dim))
            sjj = sum(fj[m] * w[m] for m in range(dim))
            if sii > sjj:
                ok += 1
        lr_accs.append(ok / max(len(te), 1))
    return {"lr_folds": lr_accs, "lr_mean": sum(lr_accs) / len(lr_accs),
            "bs_folds": bs_accs, "bs_mean": sum(bs_accs) / len(bs_accs)}

def fit_logistic(pairs: list[tuple], dim: int, l2: float = 1e-3,
                 epochs: int = 300, lr: float = 0.05) -> list[float]:
    w = [0.0] * dim
    if not pairs:
        return w
    for ep in range(epochs):
        grad = [l2 * x for x in w]
        for fi, fj, wt, *_ in pairs:
            d = sum((fi[k] - fj[k]) * w[k] for k in range(dim))
            p = 1.0 / (1.0 + math.exp(max(-30.0, min(30.0, -d))))
            for k in range(dim):
                grad[k] -= wt * (1.0 - p) * (fi[k] - fj[k])
        for k in range(dim):
            w[k] -= lr * grad[k]
    return w

def kfold(pairs: list[tuple], k: int, dim: int) -> dict:
    if not pairs:
        return {"folds": [], "mean": 0.0}
    idx = list(range(len(pairs)))
    random.Random(20261110).shuffle(idx)
    step = max(1, len(idx) // k)
    accs = []
    for f in range(k):
        test = set(idx[f * step:(f + 1) * step])
        tr = [pairs[i] for i in idx if i not in test]
        te = [pairs[i] for i in sorted(test)]
        w = fit_logistic(tr, dim)
        ok = 0
        for fi, fj, _wt in te:
            si = sum(fi[m] * w[m] for m in range(dim))
            sj = sum(fj[m] * w[m] for m in range(dim))
            if si > sj:
                ok += 1
        accs.append(ok / max(len(te), 1))
    return {"folds": accs, "mean": sum(accs) / len(accs)}

def _load_weights():
    try:
        from bili_digest.retrieve import weights as W
        cfg = W.load()
    except Exception:
        import tomllib
        p = Path(__file__).resolve().parent.parent / "retrieve" / "weights.toml"
        cfg = tomllib.loads(p.read_text(encoding="utf-8"))
    sc = cfg.get("scoring", cfg)
    return (
        sc.get("kind_weight", {}),
        float(sc.get("short_penalty", 0.55)),
        int(sc.get("short_chars", 12)),
        int(sc.get("long_query_chars", 15)),
        float(sc.get("long_chat_penalty", 0.70)),
        float(sc.get("refute_penalty", 0.30)),
    )

def current_score(text, kind, q, item_uid, refuted, st):
    kw_map, short_pen, short_chars, lq_chars, lc_pen, ref_pen = st
    kw = float(kw_map.get(kind, 1.0))
    if len(q) >= lq_chars and kind in ("comment", "danmaku", "annotation"):
        kw *= lc_pen
    sw = short_pen if len((text or "").strip()) < short_chars else 1.0
    rf = ref_pen if (item_uid, kind) in refuted else 1.0
    return kw * sw * rf

def baseline_winrate(rows, con):
    st = _load_weights()
    kind_of, refuted = {}, set()
    if con is not None:
        try:
            for bid, k in con.execute("SELECT id, kind FROM content_block"):
                kind_of[bid] = k
        except Exception:
            pass
        try:
            from bili_digest.quality.refute import refuted_pairs
            refuted = refuted_pairs(con)
        except Exception:
            pass
    groups = group_by_query(rows)
    if a.limit:
        _ks = list(groups)[:a.limit]
        groups = {k: groups[k] for k in _ks}
    wins = total = 0
    for q, grp in groups.items():
        sc_of = {}
        for r in grp:
            k = kind_of.get(r.get("block_id"), r.get("kind", ""))
            sc_of[id(r)] = current_score(r.get("text", ""), k, q,
                                        r.get("item_uid"), refuted, st)
        for a in range(len(grp)):
            for b in range(a + 1, len(grp)):
                sa, sb = grp[a].get("score", 0), grp[b].get("score", 0)
                if sa == sb:
                    continue
                total += 1
                hi, lo = (grp[a], grp[b]) if sa > sb else (grp[b], grp[a])
                if sc_of[id(hi)] > sc_of[id(lo)]:
                    wins += 1
    return wins, total

def main() -> int:
    import argparse
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="从 0-3 标签学排序权重")
    ap.add_argument("--dry", action="store_true", help="只报数，不写")
    ap.add_argument("--kfold", type=int, default=5)
    ap.add_argument("--limit", type=int, default=0,
                    help="只取前 N 个 query；0 表示全部")
    ap.add_argument("--all-kinds", action="store_true",
                    help="不过滤 summary（路径 B 用）；默认只比 summary 候选（路径 A）")
    ap.add_argument("--write", action="store_true", help="写回 weights.toml（尚未接线，需人点头）")
    a = ap.parse_args()

    rows = load_rows()
    if not rows:
        print("  flywheel.jsonl 为空或不存在：%s" % FLYWHEEL)
        return 1

    con = None
    try:
        from bili_digest.schema.core_schema import connect
        from bili_digest.paths import DB
        con = connect(DB)
    except Exception:
        pass

    if not a.all_kinds:
        before = len(rows)
        rows = filter_summary(rows, con)
        print("  路径 A（仅 summary）：标签 %d → %d 条" % (before, len(rows)))

    groups = group_by_query(rows)
    print("  可比较 query %d 个 · 标签 %d 条" % (len(groups), len(rows)))

    if a.limit:
        _ks = list(groups)[:a.limit]
        groups = {k: groups[k] for k in _ks}
        print("  --limit %d：取前 %d 个 query" % (a.limit, len(groups)))

    pairs = build_pairs(groups, con)
    print("  pairwise 样本 %d 对" % len(pairs))
    if not pairs:
        print("  没有可用的偏好对 —— 可能是 MaxSim 模型加载失败，或 summary 样本为空")
        return 1

    dim = 3 + len(KINDS) + 4
    r = kfold_both(pairs, a.kfold, dim)
    print("")
    print("  learn_rank   %d 折：%s" % (a.kfold, " ".join("%.3f" % x for x in r["lr_folds"])))
    print("  均值 %.4f" % r["lr_mean"])
    print("  基线(纯语义) %d 折：%s" % (a.kfold, " ".join("%.3f" % x for x in r["bs_folds"])))
    print("  均值 %.4f" % r["bs_mean"])
    diff = r["lr_mean"] - r["bs_mean"]
    print("  差值         %+.4f" % diff)
    print("  （随机 0.5；数值是「该排前的那条确实得分更高」的比例，同折切分保证两者同基准）")
    if diff >= 0.02:
        print("  → 学到的权重显著优于纯语义分，值得走 param_audit 真实检索对照")
    elif diff <= -0.02:
        print("  → 学到的权重不如纯语义分 —— 不要写回")
    else:
        print("  → 差值在 ±0.02 内：两者实质相当；写回不划算（权重一动历史可比性就断）")

    if a.write:
        print("\n  --write 尚未接线：写回要人点头，且要先跑 param_audit 的对照。")
        return 1
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
