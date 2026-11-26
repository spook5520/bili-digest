# -*- coding: utf-8 -*-
import argparse
import hashlib
import json
import random
import statistics
from pathlib import Path

from bili_digest import paths

CACHE = Path(__file__).parent / ".cache" / "seat_cache.json"

PROMPT_EVIDENCE = """你是检索结果的证据评审。给定一条查询和一些候选片段，\
只按【该片段自身的内容】判断它作为回答依据有多强：

0 = 与查询无关，或者只是碰巧含相同的词
1 = 与查询有关，能提供背景、侧证或部分线索，但单独不足以回答
2 = 直接包含回答查询所需的关键信息

查询：{q}

候选：
{cands}

只输出 JSON，格式：{{"labels": [分数1, 分数2, ...]}}
labels 长度必须等于候选数量（{n}）。严格区分 1 与 2：2 要求仅凭该片段就能\
回答查询，而不是「读起来相关」。"""

PROMPT_SEAT = """你是检索结果的席位评审。给定一条查询和一批按当前排名排列的候选，\
为每一段判两件事：

A. evidence：只看这一段时，它作为回答依据有多强
   0 = 与查询无关，或只是碰巧含相同的词
   1 = 有关，能给背景/侧证/部分线索，单独不足以回答
   2 = 直接包含回答所需的关键信息

B. redundant：这一段的信息是否已被【排位比它更靠前的候选】完整覆盖
   0 = 没有（含「与前面某段部分重叠但仍含独有信息」）
   1 = 是（完整覆盖，再占一个席位只是重复）
   第 1 位永远记 0。

查询：{q}

候选（按当前排名）：
{cands}

只输出 JSON，格式：{{"evidence": [...], "redundant": [...]}}
两个数组长度都必须等于候选数量（{n}）。严格区分 evidence 1 与 2：\
2 要求仅凭该片段就能回答，而不是「读起来相关」。"""

def _norm(text, n=300):
    return " ".join((text or "").split())[:n]

def _key(tag, q, text):
    return hashlib.md5(f"{tag}|{q}|{text}".encode()).hexdigest()

def _load():
    try:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    except Exception:
        return {}

def _save(d):
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")

def _ask(prompt, retry=2):
    from bili_digest.distill.extract import call_llm
    for _ in range(retry + 1):
        try:
            return json.loads(call_llm(prompt, timeout=180))
        except Exception:
            continue
    return {}

def _ints(vals, n):
    out = [int(x) if str(x).isdigit() and int(x) in (0, 1, 2) else None
           for x in list(vals)[:n]]
    return out + [None] * (n - len(out))

def _batch(tag, q, texts, ask_batch, cache):
    keys = [_key(tag, q, t) for t in texts]
    miss = [i for i, k in enumerate(keys) if k not in cache]
    if miss:
        got = ask_batch(q, [texts[i] for i in miss])
        for i, v in zip(miss, got):
            if isinstance(v, tuple):
                if v[0] is not None:
                    cache[keys[i]] = list(v)
            elif v is not None:
                cache[keys[i]] = v
    return [cache.get(k) for k in keys]

def judge_evidence(q, texts, cache=None):
    cache = _load() if cache is None else cache

    def ask(qq, ts):
        lines = [f"{i}. {_norm(t)}" for i, t in enumerate(ts, 1)]
        v = _ask(PROMPT_EVIDENCE.format(q=qq, cands="\n".join(lines), n=len(ts)))
        return _ints(v.get("labels", []), len(ts))

    out = _batch("ev", q, texts, ask, cache)
    return [o if isinstance(o, int) else None for o in out]

def judge_seat(q, texts, cache=None):
    cache = _load() if cache is None else cache
    key = _key("seat-batch", q, "\u0001".join(texts))
    if key not in cache:
        lines = [f"{i}. {_norm(t)}" for i, t in enumerate(texts, 1)]
        v = _ask(PROMPT_SEAT.format(q=q, cands="\n".join(lines), n=len(texts)))
        ev = _ints(v.get("evidence", []), len(texts))
        rd = _ints(v.get("redundant", []), len(texts))
        if any(x is None for x in ev):
            return [(None, None)] * len(texts)
        cache[key] = [[a, b] for a, b in zip(ev, rd)]
    return [tuple(x) for x in cache[key]]

def cohen_kappa(a, b):
    pairs = [(x, y) for x, y in zip(a, b) if x is not None and y is not None]
    if not pairs:
        return float("nan")
    n = len(pairs)
    po = sum(1 for x, y in pairs if x == y) / n
    cats = {x for x, _ in pairs} | {y for _, y in pairs}
    pe = sum((sum(1 for x, _ in pairs if x == c) / n) *
             (sum(1 for _, y in pairs if y == c) / n) for c in cats)
    return (po - pe) / (1 - pe) if pe < 1 else float("nan")

def weighted_kappa(a, b, levels=3):
    pairs = [(x, y) for x, y in zip(a, b) if x is not None and y is not None]
    if not pairs:
        return float("nan")
    n = len(pairs)
    denom = (levels - 1) ** 2 or 1
    obs = exp = 0.0
    for i in range(levels):
        for j in range(levels):
            w = (i - j) ** 2 / denom
            obs += w * sum(1 for x, y in pairs if x == i and y == j) / n
            r = sum(1 for x, _ in pairs if x == i)
            c = sum(1 for _, y in pairs if y == j)
            exp += w * r * c / (n * n)
    return 1 - obs / exp if exp else float("nan")

def agreement(a, b, levels=3, rel=2):
    pairs = [(x, y) for x, y in zip(a, b) if x is not None and y is not None]
    if not pairs:
        return {"n": 0}
    return {
        "n": len(pairs),
        "exact": sum(1 for x, y in pairs if x == y) / len(pairs),
        "qwk": weighted_kappa([x for x, _ in pairs], [y for _, y in pairs], levels),
        "kappa_rel": cohen_kappa([1 if x >= rel else 0 for x, _ in pairs],
                                 [1 if y >= rel else 0 for _, y in pairs]),
        "mean_a": statistics.mean(x for x, _ in pairs),
        "mean_b": statistics.mean(y for _, y in pairs),
    }

def context_precision(gains, k=10, rel=1):
    g = gains[:k]
    total_rel = sum(1 for x in g if x is not None and x >= rel)
    if total_rel == 0:
        return 0.0
    acc, tp = 0.0, 0
    for i, x in enumerate(g):
        if x is not None and x >= rel:
            tp += 1
            acc += tp / (i + 1)
    return acc / total_rel

def detectable_delta(n, sigma, power_factor=2.8):
    return power_factor * sigma / (n ** 0.5) if n > 0 else float("inf")

def n_needed(sigma, delta, power_factor=2.8):
    return int(round((power_factor * sigma / delta) ** 2))

def pick_samples(con, vindex, n_queries=30, k=6, seed=0, qkind="fragment"):
    from bili_digest.tools import eval_retrieval as ev
    qs = ev.build_queries(con, qkind, 100000)
    rnd = random.Random(seed)
    out = []
    for uid, q in rnd.sample(qs, min(n_queries, len(qs))):
        pool = ev.collect_pool(con, vindex, q, [{}], k=k)
        if len(pool) < 4:
            continue
        out.append((uid, q, [(b, t) for b, t, _u in pool]))
    return out

def calibrate(con, vindex, samples):
    from bili_digest.tools import eval_retrieval as ev
    cache = _load()
    pos, neg, miss = [], [], 0
    for i, (uid, q, _blocks) in enumerate(samples):
        row = con.execute(
            "SELECT text FROM content_block WHERE kind='subtitle_seg' AND item_uid=? "
            "AND text LIKE ? LIMIT 1", (uid, "%" + q[:20].strip() + "%")).fetchone()
        if not row:
            miss += 1
            continue
        pos.append(judge_evidence(q, [row[0]], cache)[0])
        far = con.execute(
            "SELECT text FROM content_block WHERE kind='subtitle_seg' AND item_uid<>? "
            "ORDER BY (id*7919) % 10007 LIMIT 1", (uid,)).fetchone()[0]
        neg.append(judge_evidence(q, [far], cache)[0])
    _save(cache)
    return {"pos": pos, "neg": neg, "miss": miss,
            "pos_hit2": sum(1 for x in pos if x == 2) / max(len(pos), 1),
            "neg_hit0": sum(1 for x in neg if x == 0) / max(len(neg), 1)}

def consistency(con, vindex, samples):
    from bili_digest.tools import eval_retrieval as ev
    cache = _load()
    fwd, rev, seat, single, cp_f, cp_r = [], [], [], [], [], []
    for _uid, q, blocks in samples:
        texts = [t for _b, t in blocks]
        a = ev.judge_batch(q, texts)
        b = list(reversed(ev.judge_batch(q, list(reversed(texts)))))
        s = judge_seat(q, texts, cache)
        one = [judge_evidence(q, [t], cache)[0] for t in texts]
        fwd += a
        rev += b
        seat += [x[0] for x in s]
        single += one
        cp_f.append(context_precision(a, len(texts), 2))
        cp_r.append(context_precision(b, len(texts), 2))
    _save(cache)
    d = [y - x for x, y in zip(cp_f, cp_r)]
    return {
        "order": agreement(fwd, rev, levels=4, rel=2),
        "batch": agreement(seat, single, levels=3, rel=2),
        "cross": agreement(fwd, seat, levels=4, rel=2),
        "cp_fwd": statistics.mean(cp_f), "cp_rev": statistics.mean(cp_r),
        "sigma_cp": statistics.pstdev(d), "max_cp": max(abs(x) for x in d),
    }

def seat_stats(con, vindex, limit=None, qkind="fragment"):
    from bili_digest.tools import eval_retrieval as ev
    cache = _load()
    qs = ev.build_queries(con, qkind, limit or 100000)
    top, pool = [], []
    for _uid, q in qs:
        p = ev.collect_pool(con, vindex, q, [{}, {"recency": 0.3}], 10)
        if not p:
            continue
        s = judge_seat(q, [t for _b, t, _u in p], cache)
        top += s[:10]
        pool += s
    _save(cache)
    def share(xs, pred):
        xs2 = [x for x in xs if x[0] is not None and x[1] is not None]
        return sum(1 for x in xs2 if pred(x)) / max(len(xs2), 1), len(xs2)
    return {
        "top": {k: share(top, f) for k, f in (
            ("强席位", lambda x: x[0] == 2 and x[1] == 0),
            ("弱席位", lambda x: x[0] == 1 and x[1] == 0),
            ("重复", lambda x: x[1] == 1),
            ("无关", lambda x: x[0] == 0),
        )},
        "pool": {k: share(pool, f) for k, f in (
            ("强席位", lambda x: x[0] == 2 and x[1] == 0),
            ("弱席位", lambda x: x[0] == 1 and x[1] == 0),
            ("重复", lambda x: x[1] == 1),
            ("无关", lambda x: x[0] == 0),
        )},
    }

def floor_table(con, vindex, labels, cfgs, k=10, rel=2):
    from bili_digest.retrieve import rag_core as rc
    out = []
    for name, cfg in cfgs:
        a, b = [], []
        for q, gmap in labels.items():
            ga = [_g(h, gmap) for h in rc.hybrid(con, vindex, q, top_k=k)]
            gb = [_g(h, gmap) for h in rc.hybrid(con, vindex, q, top_k=k, **cfg)]
            if None in ga or None in gb or not ga or not gb:
                continue
            a.append(context_precision(ga, k, rel))
            b.append(context_precision(gb, k, rel))
        if len(a) < 2:
            out.append({"cfg": name, "n": len(a)})
            continue
        d = [y - x for x, y in zip(a, b)]
        sd = statistics.pstdev(d)
        out.append({"cfg": name, "n": len(a), "delta": statistics.mean(d),
                    "sigma_base": statistics.pstdev(a), "sigma_d": sd,
                    "floor": detectable_delta(len(d), sd),
                    "n_for_1pt": n_needed(sd, 0.01)})
    return out

def _g(hit, gmap):
    bid = hit.get("id") or hit.get("block_id")
    return gmap.get(bid)

def full_labels(con, vindex, limit=None, k=10, qkind="fragment"):
    from bili_digest.tools import eval_retrieval as ev
    qs = ev.build_queries(con, qkind, limit or 100000)
    jc = ev._cache_load()
    out, added = {}, 0
    for _uid, q in qs:
        p = ev.collect_pool(con, vindex, q, [{}, {"recency": 0.3}], k)
        if not p:
            continue
        keys = [ev._ckey(q, t) for _b, t, _u in p]
        miss = [i for i, kk in enumerate(keys) if kk not in jc]
        if miss:
            got = ev.judge_batch(q, [p[i][1] for i in miss])
            for i, g in zip(miss, got):
                if g is not None:
                    jc[keys[i]] = g
                    added += 1
        gains = [jc.get(kk) for kk in keys]
        if any(g is None for g in gains):
            continue
        out[q] = {b: g for (b, _t, _u), g in zip(p, gains)}
    if added:
        ev._cache_save(jc)
        print("   判定缓存新增 %d 条（总量 %d）" % (added, len(jc)))
    return out

CFGS = [
    ("recency=0.3", {"recency": 0.3}),
    ("去观众反应", {"kinds": ["subtitle_seg", "summary", "comment"]}),
]

def run(n_consistent=30, k=6, seed=0, do_full=False, limit=None):
    from bili_digest.schema.core_schema import connect
    from bili_digest.paths import DB, VEC
    from bili_digest.retrieve import rag_core as rc

    con = connect(DB)
    con.row_factory = __import__("sqlite3").Row
    vi = rc.VectorIndex(str(VEC))
    samples = pick_samples(con, vi, n_consistent, k, seed)
    print("样本：%d 条查询 × 最多 %d 块" % (len(samples), k))

    cal = calibrate(con, vi, samples)
    print("\n① 已知答案对照（单块口径）")
    print("   正控（查询所出自的块）判 2 的比例 %.3f  n=%d  分布 %s"
          % (cal["pos_hit2"], len(cal["pos"]),
             {s: cal["pos"].count(s) for s in sorted(set(cal["pos"]))}))
    print("   负控（无关条目的一块）判 0 的比例 %.3f  n=%d"
          % (cal["neg_hit0"], len(cal["neg"])))
    if cal["miss"]:
        print("   有 %d 条查询没定位到来源块（不计入正控）" % cal["miss"])

    c = consistency(con, vi, samples)
    print("\n② 同一判据在两种扰动下的一致性")
    for label, r in (("0-3 原序 vs 反序", c["order"]),
                     ("证据 整批 vs 单块", c["batch"])):
        print("   %-20s n=%3d 精确 %.3f 加权κ %.3f 二值κ %.3f 均值 %.2f/%.2f"
              % (label, r["n"], r["exact"], r["qwk"], r["kappa_rel"],
                 r["mean_a"], r["mean_b"]))
    print("   跨判据（0-3>=2 与 证据>=2 是否同口径）二值κ %.3f"
          % c["cross"]["kappa_rel"])
    print("\n③ 判定对指标的传导（同一排序，只换判定）")
    print("   CP@k %.4f → %.4f  σ(ΔCP) %.4f  最大|Δ| %.4f"
          % (c["cp_fwd"], c["cp_rev"], c["sigma_cp"], c["max_cp"]))

    if not do_full:
        con.close()
        return {"calibrate": cal, "consistency": c}

    labels = full_labels(con, vi, limit)
    rows = floor_table(con, vi, labels, CFGS)
    print("\n④ 固定判定下的配对差与可检测下限（n=%d 条查询）" % len(labels))
    for r in rows:
        if "sigma_d" not in r:
            print("   %-14s 可用查询不足（%d）" % (r["cfg"], r["n"]))
            continue
        print("   %-14s n=%3d Δ%+.4f σ(D) %.4f 下限 %.4f 测 1pt 需 n≈%d"
              % (r["cfg"], r["n"], r["delta"], r["sigma_d"],
                 r["floor"], r["n_for_1pt"]))
    ss = seat_stats(con, vi, limit)
    print("\n⑤ 席位构成（top-10 与整池）")
    for scope in ("top", "pool"):
        print("   %-5s " % scope + "  ".join(
            "%s %.3f (n=%d)" % (k2, v[0], v[1]) for k2, v in ss[scope].items()))
    con.close()
    return {"calibrate": cal, "consistency": c, "floor": rows, "seat": ss}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=30, help="一致性样本的查询数")
    ap.add_argument("--k", type=int, default=6, help="每条查询的候选块数")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--full", action="store_true", help="加上全量配对与席位统计")
    ap.add_argument("--limit", type=int, default=None)
    a = ap.parse_args()
    run(a.n, a.k, a.seed, a.full, a.limit)

if __name__ == "__main__":
    main()
