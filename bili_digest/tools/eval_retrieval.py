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

def cp_by_item(pool: list, gains: list, k: int = 10, rel: int = 2) -> tuple:
    seen, g_item = set(), []
    for (row, g) in zip(pool[:k], gains[:k]):
        uid = row[2] if isinstance(row, (tuple, list)) and len(row) > 2 else row
        if uid in seen:
            continue
        seen.add(uid)
        g_item.append(g)
    n = len(g_item)
    if n == 0:
        return 0.0, 0
    total_rel = sum(1 for x in g_item if x is not None and x >= rel)
    if total_rel == 0:
        return 0.0, n
    acc, tp = 0.0, 0
    for i, x in enumerate(g_item):
        if x is not None and x >= rel:
            tp += 1
            acc += tp / (i + 1)
    return acc / total_rel, n

_NATURAL_QUERIES = (
    "Frida 怎么安装",
    "CIDR 是什么",
    "WASM 为什么比 JS 快",
    "光刻机光源原理",
    "安卓逆向 hook 怎么实现",
    "卡塔兰数",
    "哪个视频讲了光刻机",
    "怎么学逆向",
    "Frida 和 Xposed 差别",
    "RAG 增量更新怎么做",
    "系统学 Java 该看哪套课",
    "计算机网络 考研速通",
    "USB HID 协议怎么从零实现",
    "BIOS 怎么改默认设置",
    "群晖 内网穿透",
    "显卡花屏怎么修",
    "弹幕文化 起源",
    "开源许可证 怎么选",
    "Docker 和虚拟机 区别",
    "音游 判定延迟 怎么调",
)

def build_queries(con, kind: str = "title", limit: int = 40,
                  per_item: int = 1) -> list:
    out = []
    if kind == "natural":

        for q in _NATURAL_QUERIES[:limit]:
            out.append(("natural", q))
    elif kind == "title":

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

            picks = [len(seg) // 2] if per_item <= 1 else [
                min(len(seg) - 1, int((i + 1) * len(seg) / (per_item + 1)))
                for i in range(per_item)]
            for si in picks:
                mid_seg = " ".join((seg[si]["text"] or "").split())
                toks = mid_seg.split(" ")
                piece = ""
                for t in toks:
                    if len(piece) + len(t) + 1 > 80:
                        break
                    piece = (piece + " " + t).strip()
                if 18 <= len(piece) <= 80:
                    out.append((r["uid"], piece))
    elif kind == "fragment_derived":

        import re as _re
        for r in con.execute("""SELECT item_uid, text FROM content_block
                                WHERE kind='seg_summary'"""):
            parts = [p.strip() for p in _re.split(r"第\d+段：", r["text"] or "")
                     if p.strip()]
            if not parts:
                continue
            src = parts[1] if len(parts) > 1 else parts[0]
            acc = ""
            for t in " ".join(src.split()).split(" "):
                if len(acc) + len(t) + 1 > 78:
                    break
                acc = (acc + " " + t).strip()
            if 18 <= len(acc) <= 80:
                out.append((r["item_uid"], acc))
    return out[:limit]

_POOL_CACHE: dict = {}

def _retrieve(con, vi, q: str, k: int, **kw):
    import os as _os
    if not _os.environ.get("KB_USE_POOLS"):
        from bili_digest.retrieve import rag_core as _rc
        return _rc.hybrid(con, vi, q, top_k=k, **kw)
    import numpy as _np
    from bili_digest.retrieve import rag_core as _rc
    from bili_digest.retrieve.pools import PoolIndex
    key = id(vi)
    pi = _POOL_CACHE.get(key)
    if pi is None:
        pi = PoolIndex(con, vi)
        _POOL_CACHE[key] = pi
    model = _rc.deps()["model"]
    qv = _np.array(list(model.query_embed([q]))[0], dtype=_np.float32)

    hits = pi.search(q, qv, per_pool=max(10, k * 3))
    out = []
    for h in hits[:k]:
        row = con.execute(
            "SELECT id, kind, text, item_uid FROM content_block WHERE id=?",
            (h["block_id"],)).fetchone()
        if row:
            out.append({"id": row["id"], "block_id": row["id"], "kind": row["kind"],
                        "text": row["text"], "item_uid": row["item_uid"],
                        "score": h["score"]})
    return out

def collect_pool(con, vi, q: str, variants: list, k: int = 10) -> list:
    from bili_digest.retrieve import rag_core as rc
    seen, pool = set(), []
    for kw in variants:
        hits = _retrieve(con, vi, q, k, **kw)
        for h in hits:
            bid = h.get("id") or h.get("block_id")
            if bid is None or bid in seen:
                continue
            seen.add(bid)
            pool.append((bid, h.get("text") or "", h.get("item_uid")))
    return pool

def run(limit: int = 30, k: int = 10, qkind: str = "title",
        compare: bool = False, per_item: int = 1, dump: str = "") -> dict:
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
    for n, (uid, q) in enumerate(build_queries(con, qkind, limit, per_item), 1):
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
            hits = _retrieve(con, vi, q, k, **kw)
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

    _cp_item, _n_items = [], []
    for _u, _q, _p, _g in rows:
        _c, _n = cp_by_item(_p, _g, k)
        _cp_item.append(_c)
        _n_items.append(_n)

    _dump = dump
    if _dump:
        import json as _j
        per_q = [{"q": _q, "ndcg": ndcg(g, k), "cp": context_precision(g, k)}
                 for _u, _q, _p, g in rows]
        with open(_dump, "w", encoding="utf-8", newline="\n") as _f:
            _j.dump(per_q, _f, ensure_ascii=False, indent=1)
        print(f"  per-query 得分已写入 {_dump}（{len(per_q)} 条）")

    _nd_sd = statistics.stdev(nd_all) if len(nd_all) > 1 else 0.0
    _mde_nd = 2.8016 * _nd_sd / (len(nd_all) ** 0.5) if _nd_sd else 0.0
    _mde_cp = (2.8016 * statistics.stdev(cp_all) / (len(cp_all) ** 0.5)
               if len(cp_all) > 1 else 0.0)
    print(f"  池内 nDCG@{k} 均值 {statistics.mean(nd_all):.4f}"
          f"  ·  CP@{k} 均值 {statistics.mean(cp_all):.4f}")
    print(f"  波动 nDCG σ={_nd_sd:.4f} MDE={_mde_nd:.4f}"
          f"  ·  CP σ={statistics.stdev(cp_all) if len(cp_all) > 1 else 0:.4f}"
          f" MDE={_mde_cp:.4f}   <- 差值小于 MDE 就说「测不出」")
    print(f"  按条目 CP@{k} 均值 {statistics.mean(_cp_item):.4f}"
          f"  ·  平均覆盖 {statistics.mean(_n_items):.1f} 个条目（top-{k}）")
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
                hits = _retrieve(con, vi, q, k, **kw)
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
                    choices=["title", "topic", "fragment", "natural"])
    ap.add_argument("--per-item", type=int, default=1,
                    help=("每条目取几段当查询（同档扩充，不换难度）。"
                          "实测 ICC=0.004，同条目查询的 Δ 几乎不相关，"
                          "名义 n 可当有效 n 用。默认 1。"
                          "★ 为什么要它：n=80 时 MDE=0.046（σ=0.147），"
                          "而这一夜多数改动的 Δ 在 0.003~0.03 —— 全部在窗口外。"
                          "per-item=4 可把 n 提到 300+，MDE 降到 0.023。"))
    ap.add_argument("--dump", default="",
                    help=("把 per-query 得分写成 JSON（供配对检验算 rho）。"
                          "MDE 的公式要分清独立两组 vs 配对："
                          "配对是 2.8016*sd*sqrt(1-rho)/sqrt(n)，"
                          "而 rho 只能从 per-query 得分算，不能猜。"))
    ap.add_argument("--compare", action="store_true",
                    help="额外跑一组参数对比（检验评估的分辨力）")
    a = ap.parse_args()
    run(limit=a.limit, qkind=a.qkind, compare=a.compare, per_item=a.per_item,
        dump=getattr(a, "dump", ""))
