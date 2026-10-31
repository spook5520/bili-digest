import os as _os
import sys as _sys

from bili_digest import paths

import json
import os
import sys
import hashlib
import time
from pathlib import Path

CACHE = Path(__file__).parent / ".cache" / "judge_cache.json"

def _cache_load() -> dict:
    try:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    except Exception:
        return {}

def _cache_save(d: dict) -> None:
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")

PROMPT = """你是相关性评判员。判断每个候选片段与查询的相关程度，用 0-3 打分：

0 = 与查询无关
1 = 与查询相关，但没有回答查询
2 = 对查询有一些答案，但不够清楚或被无关信息掩盖
3 = 专门针对查询，包含确切答案

查询：{q}

候选：
{cands}

只输出 JSON，格式：{{"labels": [分数1, 分数2, ...]}}
labels 的长度必须等于候选数量（{n}）。评分要严格区分：不要因为「提到了同一个词」
就给 3 —— 3 要求该片段本身【回答了】查询。"""

def _ckey(q: str, text: str) -> str:
    return hashlib.md5(f"{q}|{text}".encode()).hexdigest()

def judge_batch(q: str, texts: list) -> list:
    from bili_digest.distill.extract import call_llm
    lines = []
    for i, t in enumerate(texts, 1):
        t2 = " ".join((t or "").split())[:300]
        lines.append(f"{i}. {t2}")
    p = PROMPT.format(q=q, cands="\n".join(lines), n=len(texts))
    try:
        raw = call_llm(p, timeout=180)
        v = json.loads(raw)
        labels = v.get("labels") or []
        labels = [int(x) if str(x).lstrip("-").isdigit() else 0 for x in labels]
        if len(labels) != len(texts):
            labels = (labels + [0] * len(texts))[:len(texts)]
        return labels
    except Exception as e:
        print(f"    [!] 判定失败：{type(e).__name__}: {str(e)[:50]}")
        return [None] * len(texts)

def ndcg(gains: list, k: int = 10) -> float:
    import math
    g = gains[:k]
    dcg = sum((2 ** x - 1) / math.log2(i + 2) for i, x in enumerate(g) if x)
    ideal = sorted((x for x in gains if x), reverse=True)[:k]
    idcg = sum((2 ** x - 1) / math.log2(i + 2) for i, x in enumerate(ideal))
    return dcg / idcg if idcg else 0.0

FLYWHEEL = Path(".cache/flywheel.jsonl")

def _now_sec() -> int:
    from bili_digest.schema.core_schema import now_sec
    return now_sec()

def _flywheel_log(rows: list) -> None:
    if not rows:
        return
    try:
        FLYWHEEL.parent.mkdir(parents=True, exist_ok=True)
        with FLYWHEEL.open("a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + chr(10))
    except OSError:
        pass

def context_precision(gains: list, k: int = 10, rel: int = 2) -> float:
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

def build_queries(con, kind: str = "title", limit: int = 40) -> list:
    out = []
    if kind == "title":

        for r in con.execute("""SELECT i.uid, i.title FROM item i
                                WHERE EXISTS(SELECT 1 FROM content_block cb
                                             WHERE cb.item_uid=i.uid
                                             AND cb.kind IN
                                                 ('subtitle','subtitle_seg'))"""):
            t = (r["title"] or "").strip()
            if len(t) > 10:
                out.append((r["uid"], t[:36]))
    elif kind == "topic":
        for r in con.execute("""SELECT d.item_uid uid, d.content FROM derived d
                                WHERE d.kind='topics'"""):
            try:
                v = json.loads(r["content"])
            except Exception:
                continue
            if isinstance(v, list):
                ws = [x if isinstance(x, str) else (x.get("term") or "") for x in v]
                ws = [w for w in ws if w and len(w) >= 2]
                if len(ws) >= 3:
                    out.append((r["uid"], " ".join(ws[:3])))
    elif kind == "fragment":

        for r in con.execute("""SELECT i.uid, i.item_id FROM item i
                                WHERE EXISTS(SELECT 1 FROM content_block cb
                                             WHERE cb.item_uid=i.uid
                                             AND cb.kind='subtitle_seg')"""):
            seg = con.execute("""SELECT text FROM content_block
                                 WHERE item_uid=? AND kind='subtitle_seg'
                                 ORDER BY offset_sec LIMIT 40""", (r["uid"],)).fetchall()
            if len(seg) < 4:
                continue

            mid_seg = " ".join((seg[len(seg) // 2]["text"] or "").split())
            toks = mid_seg.split(" ")
            piece = ""
            for t in toks:
                if len(piece) + len(t) + 1 > 80:
                    break
                piece = (piece + " " + t).strip()
            if 18 <= len(piece) <= 80:
                out.append((r["uid"], piece))
    return out[:limit]

def collect_pool(con, vi, q: str, variants: list, k: int = 10) -> list:
    from bili_digest.retrieve import rag_core as rc
    seen, pool = set(), []
    for kw in variants:
        hits = rc.hybrid(con, vi, q, top_k=k, **kw)
        for h in hits:
            bid = h.get("id") or h.get("block_id")
            if bid is None or bid in seen:
                continue
            seen.add(bid)
            pool.append((bid, h.get("text") or "", h.get("item_uid")))
    return pool

def run(limit: int = 30, k: int = 10, qkind: str = "title",
        compare: bool = False) -> dict:
    from bili_digest.schema.core_schema import connect
    from bili_digest.paths import DB, VEC
    from bili_digest.retrieve import rag_core as rc

    con = connect(DB)
    con.row_factory = __import__("sqlite3").Row
    vi = rc.VectorIndex(str(VEC))
    cache = _cache_load()

    variants = [{}, {"recency": 0.3}]
    if compare:
        variants += [{"kw_weight": 0.5}, {"kw_weight": 0.0}]

    print(f"  查询档 {qkind} · {limit} 条 · 池深 {k} · 入池路数 {len(variants)}\n")
    t0 = time.time()
    rows = []
    judged = 0
    for n, (uid, q) in enumerate(build_queries(con, qkind, limit), 1):
        pool = collect_pool(con, vi, q, variants, k)
        if not pool:
            continue
        need = []
        for bid, text, _u in pool:
            hkey = _ckey(q, text)
            if hkey not in cache:

                need.append((hkey, text, bid, _u))
        if need:
            labels = judge_batch(q, [t for _k, t, _b, _u in need])
            _fly = []
            for (hkey, _t, _b, _u), lab in zip(need, labels):
                if lab is not None:
                    cache[hkey] = lab
                    _fly.append({"q": q, "text": _t[:400], "score": lab,
                                 "block_id": _b, "item_uid": _u,
                                 "ts": _now_sec()})
            _flywheel_log(_fly)
            judged += len(need)
        gains = []
        for bid, text, _u in pool:
            hkey = _ckey(q, text)
            gains.append(cache.get(hkey))
        if any(g is None for g in gains):
            continue
        rows.append((uid, q, pool, gains))
        if n % 10 == 0:
            print(f"    ...{n}/{limit}  已判定 {judged} 对  缓存 {len(cache)}")

    _cache_save(cache)
    el = time.time() - t0
    print(f"\n  完成：{len(rows)} 条查询可用 · 本次新判定 {judged} 对 · {el:.0f}s\n")

    if not rows:
        print("  没有可用结果（检查 LLM key）")
        con.close()
        return {}

    import statistics

    def score_with(kw: dict, label: str):
        nd, cps = [], []
        for uid, q, pool, gains in rows:
            gmap = {}
            for (bid, _t, _u), g in zip(pool, gains):
                gmap[bid] = g
            hits = rc.hybrid(con, vi, q, top_k=k, **kw)
            g = []
            for h in hits:
                bid = h.get("id") or h.get("block_id")
                if bid in gmap:
                    g.append(gmap[bid])
            if g:
                nd.append(ndcg(g, k))
                cps.append(context_precision(g, k))
        m = statistics.mean(nd) if nd else 0.0
        cp = statistics.mean(cps) if cps else 0.0
        print(f"    {label:<32} nDCG@{k} {m:.4f}  CP@{k} {cp:.4f}  (n={len(nd)})")
        return m

    print("═══ 结果 ═══\n")
    from collections import Counter
    dist = Counter(g for _u, _q, _p, gs in rows for g in gs)
    tot = sum(dist.values())
    nd_all = [ndcg(g, k) for _u, _q, _p, g in rows]
    cp_all = [context_precision(g, k) for _u, _q, _p, g in rows]
    print(f"  池内 nDCG@{k} 均值 {statistics.mean(nd_all):.4f}"
          f"  ·  CP@{k} 均值 {statistics.mean(cp_all):.4f}"
          f"  （CP 标准差 {statistics.stdev(cp_all):.4f}）")
    print(f"  含「3 分（专门回答）」的查询: "
          f"{sum(1 for _u,_q,_p,g in rows if 3 in g)}/{len(rows)}")
    print(f"\n  分数分布（共 {tot} 对）:", end=" ")
    print(" · ".join(f"{s}分 {dist.get(s,0)}({dist.get(s,0)/tot*100:.0f}%)"
                     for s in (3, 2, 1, 0)))
    print()

    if compare:
        print("═══ 参数对比（同一查询集、同一套判定）═══\n")
        from bili_digest.tools import stats_test as st
        per_q = {}

        def score_per_query(kw: dict, label: str):
            out = []
            for uid, q, pool, gains in rows:
                gmap = {bid: g for (bid, _t, _u), g in zip(pool, gains)}
                hits = rc.hybrid(con, vi, q, top_k=k, **kw)
                g = []
                for h in hits:
                    bid = h.get("id") or h.get("block_id")
                    if bid in gmap:
                        g.append(gmap[bid])
                out.append(ndcg(g, k) if g else 0.0)
            per_q[label] = out
            m = statistics.mean(out) if out else 0.0
            print(f"    {label:<34} nDCG@{k} {m:.4f}  (n={len(out)})")
            return out

        a = score_per_query({}, "现状（kw_weight=0.0）")
        print()
        b = score_per_query({"kw_weight": 0.5}, "kw_weight=0.5")
        print()

        deltas = [y - x for x, y in zip(a, b)]

        sd = statistics.pstdev(a) if len(a) > 1 else 0.0
        print("═══ 配对显著性检验（Sakai 的三件套）═══\n")
        st.report(deltas, sigma_q=sd, label_a="现状", label_b="kw=0.5")
        print()
        print("═══ 这份评估能测出多大的差异 ═══\n")
        print(f"  每查询 nDCG 标准差 σ_q = {sd:.4f} · 查询数 n = {len(a)}")
        print(f"  → 本 n 下可检测的最小 delta ≈ {st.detectable_delta(len(a), sd):.4f}")
        for target in (0.02, 0.05, 0.10):
            need = st.min_queries_needed(sd, target)
            print(f"     要测出 delta={target:.2f} 需要 {need:>5} 条查询")
        print()
        print("  ★ 这是评估的【分辨率下限】，也是「该不该继续调参」的判据：")
        print("    若想验证的改动预期小于上面的可检测下限，那这个评估集")
        print("    根本回答不了 —— 要么扩查询集，要么承认测不出。")
    print()
    print("  缓存：.cache/judge_cache.json（换参数重跑只判新增的池成员）")
    con.close()
    return {"rows": len(rows), "ndcg": statistics.mean(nd_all), "judged": judged}

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("limit", nargs="?", type=int, default=24)
    ap.add_argument("--qkind", default="title",
                    choices=["title", "topic", "fragment"])
    ap.add_argument("--compare", action="store_true",
                    help="额外跑一组参数对比（检验评估的分辨力）")
    a = ap.parse_args()
    run(limit=a.limit, qkind=a.qkind, compare=a.compare)
