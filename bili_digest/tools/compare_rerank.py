# -*- coding: utf-8 -*-
import argparse
import hashlib
import json
import math
import os
import random
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from bili_digest import paths

ROOT = Path(__file__).resolve().parents[2]
CACHE = Path(".cache")
LLM_CACHE = CACHE / "compare_rerank_llm.json"
JUDGE_CACHE = Path("bili_digest/tools/.cache/judge_cache.json")
FLYWHEEL = CACHE / "flywheel.jsonl"

METHODS = ("original", "heuristic", "single", "permsc", "setwise", "oracle")
LLM_METHODS = ("single", "permsc", "setwise")

def pool_path(src: str) -> Path:
    return CACHE / ("compare_rerank_pool_%s.json" % src)

def result_path(src: str) -> Path:
    return CACHE / ("compare_rerank_result_%s.json" % src)

def _md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()

def _valid_response(raw: str, prompt: str) -> bool:
    try:
        d = json.loads(raw or "")
    except Exception:
        return False
    if not isinstance(d, dict):
        return False
    if '{"best"' in prompt:
        v = d.get("best")
        return isinstance(v, int) and not isinstance(v, bool) and v >= 1
    o = d.get("order")
    return isinstance(o, list) and len(o) >= 2

def _read_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default

def _write_json(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)

def _build_hybrid(pool_size: int = 10) -> dict:
    import sqlite3
    from bili_digest.schema.core_schema import connect
    from bili_digest.paths import DB, VEC
    from bili_digest.retrieve import rag_core as rc
    from bili_digest.tools import eval_retrieval as ev

    judge = _read_json(JUDGE_CACHE, {})
    if not judge:
        raise RuntimeError("judge_cache 为空或不存在：%s" % JUDGE_CACHE)
    queries = []
    for line in FLYWHEEL.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        q = json.loads(line).get("q")
        if q and q not in queries:
            queries.append(q)

    con = connect(DB)
    con.row_factory = sqlite3.Row
    vi = rc.VectorIndex(str(VEC))
    out, stats = [], {"queries": len(queries), "kept": 0, "no_mix": 0,
                      "too_small": 0, "gold_missing": 0, "blocks": 0}
    t0 = time.time()
    try:
        for n, q in enumerate(queries, 1):

            pool = ev.collect_pool(con, vi, q, [{}, {"recency": 0.3}], pool_size)
            items, miss = [], 0
            for bid, text, uid in pool[:pool_size]:
                g = judge.get(_md5("%s|%s" % (q, text)))
                if g is None:
                    miss += 1
                    continue
                row = con.execute(
                    "SELECT kind, weight FROM content_block WHERE id=?",
                    (bid,)).fetchone()
                it = con.execute(
                    "SELECT title FROM item WHERE uid=?", (uid,)).fetchone()
                items.append({
                    "block_id": bid,
                    "item_uid": uid,
                    "title": (it["title"] if it else "") or "",
                    "kind": (row["kind"] if row else "") or "",
                    "weight": (row["weight"] if row else None),
                    "text": text or "",
                    "g": int(g),
                })
            stats["gold_missing"] += miss
            if len(items) < 3:
                stats["too_small"] += 1
                continue
            gains = [x["g"] for x in items]
            if not (any(g >= 2 for g in gains) and any(g < 2 for g in gains)):
                stats["no_mix"] += 1
                continue
            out.append({"q": q, "items": items, "gold_missing": miss})
            stats["kept"] += 1
            stats["blocks"] += len(items)
            if n % 100 == 0:
                print("    ...%d/%d  可用 %d  %.0fs" % (
                    n, len(queries), stats["kept"], time.time() - t0),
                    flush=True)
    finally:
        con.close()

    doc = {"meta": {"source": "hybrid", "pool_size": pool_size,
                    "judge_cache_entries": len(judge),
                    "built_sec": round(time.time() - t0, 1),
                    "baseline": "original = 真实检索排名（第一变体）",
                    "order": "collect_pool 第一变体检索排名（rag_core.hybrid, block 粒度, 无 mLateOn）",
                    "gold": "judge_cache md5(q|全文)，同一 LLM 判定",
                    "power": "n=55 时 detectable_delta≈0.034，不足以判 1.1pt"},
           "stats": stats, "queries": out}
    _write_json(pool_path("hybrid"), doc)
    print("\n  [hybrid] 建池完成：%d/%d 可用 · %d 块 · 无 gold 丢弃 %d · 无混合 %d · 过小 %d · %.0fs"
          % (stats["kept"], stats["queries"], stats["blocks"],
             stats["gold_missing"], stats["no_mix"], stats["too_small"],
             time.time() - t0))
    return doc

def _build_flywheel(pool_size: int = 10, seed: int = 20261112) -> dict:
    import sqlite3
    from bili_digest.schema.core_schema import connect
    from bili_digest.paths import DB

    con = connect(DB)
    con.row_factory = sqlite3.Row
    groups, conflicts, dup = {}, 0, 0
    for line in FLYWHEEL.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        q, bid = r.get("q"), r.get("block_id")
        if not q or bid is None:
            continue
        g = groups.setdefault(q, {})
        if bid in g:
            dup += 1
            if g[bid]["g"] != r["score"]:
                conflicts += 1
            if r.get("ts", 0) >= g[bid]["ts"]:
                g[bid] = {"g": int(r["score"]), "ts": r.get("ts", 0),
                          "uid": r.get("item_uid")}
        else:
            g[bid] = {"g": int(r["score"]), "ts": r.get("ts", 0),
                      "uid": r.get("item_uid")}

    stats = {"queries": len(groups), "kept": 0, "no_mix": 0, "too_small": 0,
             "blocks": 0, "duplicates": dup, "score_conflicts": conflicts,
             "no_fulltext": 0}
    out, t0 = [], time.time()
    try:
        for q, blocks in groups.items():
            rng = random.Random(_md5(q) + ":%d" % seed)
            bids = list(blocks)
            rng.shuffle(bids)
            items = []
            for bid in bids[:pool_size]:
                row = con.execute(
                    "SELECT item_uid, kind, weight, text FROM content_block WHERE id=?",
                    (bid,)).fetchone()
                if row is None:
                    stats["no_fulltext"] += 1
                    continue
                uid = row["item_uid"] or blocks[bid]["uid"]
                it = con.execute("SELECT title FROM item WHERE uid=?",
                                 (uid,)).fetchone()
                items.append({
                    "block_id": bid, "item_uid": uid,
                    "title": (it["title"] if it else "") or "",
                    "kind": row["kind"] or "", "weight": row["weight"],
                    "text": row["text"] or "", "g": blocks[bid]["g"],
                })
            if len(items) < 3:
                stats["too_small"] += 1
                continue
            gains = [x["g"] for x in items]
            if not (any(g >= 2 for g in gains) and any(g < 2 for g in gains)):
                stats["no_mix"] += 1
                continue
            out.append({"q": q, "items": items, "gold_missing": 0})
            stats["kept"] += 1
            stats["blocks"] += len(items)
    finally:
        con.close()

    doc = {"meta": {"source": "flywheel", "pool_size": pool_size,
                    "seed": seed, "built_sec": round(time.time() - t0, 1),
                    "baseline": "original = 确定性乱序（无信息输入）",
                    "order": "flywheel 判定日志按 (q,block_id) 去重后确定性打乱",
                    "gold": "flywheel.score（同一 LLM 判定，冲突取 ts 最大）",
                    "power": "n≈%d，detectable_delta≈%.4f（σ=0.088 估计）"
                             % (stats["kept"],
                                0.088 * math.sqrt(8.0 / max(stats["kept"], 1)))},
           "stats": stats, "queries": out}
    _write_json(pool_path("flywheel"), doc)
    print("\n  [flywheel] 建池完成：%d/%d 可用 · %d 块 · 重复 %d · 分数冲突 %d"
          " · 全文缺失 %d · 抽样后无混合 %d · 过小 %d · %.0fs"
          % (stats["kept"], stats["queries"], stats["blocks"], dup, conflicts,
             stats["no_fulltext"], stats["no_mix"], stats["too_small"],
             time.time() - t0))
    return doc

def build_pool(pool_size: int = 10, source: str = "both",
               seed: int = 20261112) -> dict:
    out = {}
    if source in ("hybrid", "both"):
        out["hybrid"] = _build_hybrid(pool_size)
    if source in ("flywheel", "both"):
        out["flywheel"] = _build_flywheel(pool_size, seed)
    return out

class CachedLLM:

    def __init__(self, dry: bool = False, retries: int = 3):
        self.dry = dry
        self.retries = retries
        self.d = {} if dry else _read_json(LLM_CACHE, {})
        self.calls = 0
        self.hits = 0
        self.fails = 0
        self._dirty = 0

        self._lock = threading.Lock()

    def __call__(self, prompt: str) -> str:
        key = _md5(prompt)
        with self._lock:
            if key in self.d:
                self.hits += 1
                return self.d[key]
        if self.dry:

            rng = random.Random(int(key[:8], 16))
            n = prompt.count("\n") - 8
            n = max(2, min(n, 20))
            if '{"best"' in prompt:
                return json.dumps({"best": rng.randint(1, n)})
            order = list(range(1, n + 1))
            rng.shuffle(order)
            return json.dumps({"order": order})

        raw = None
        for attempt in range(self.retries):
            try:
                from bili_digest.distill.extract import call_llm
                cand = call_llm(prompt, timeout=180)
                if not _valid_response(cand, prompt):
                    raise ValueError("响应不可用：%s" % str(cand)[:60])
                raw = cand
                break
            except Exception as e:
                self.fails += 1
                print("    [!] LLM %s（第 %d 次）：%s"
                      % (type(e).__name__, attempt + 1, str(e)[:70]), flush=True)
                if attempt + 1 < self.retries:
                    time.sleep(min(2 ** attempt * 3, 20))
        if raw is None:

            raise RuntimeError("LLM 重试 %d 次仍无有效响应" % self.retries)
        with self._lock:
            self.calls += 1
            self.d[key] = raw
            self._dirty += 1
            need = self._dirty >= 25
        if need:
            self.flush()
        return raw

    def flush(self) -> None:
        with self._lock:
            if not self.dry and self._dirty:
                _write_json(LLM_CACHE, dict(self.d))
                self._dirty = 0

def _hits(items: list) -> list:
    return [{"title": x["title"][:60], "text": x["text"],
             "kind": x["kind"], "weight": x["weight"],
             "evidence": {"text": x["text"]}, "_idx": i}
            for i, x in enumerate(items)]

def _metrics(order: list, items: list, k: int) -> dict:
    from bili_digest.tools import eval_retrieval as ev
    gains = [items[i]["g"] for i in order]
    return {"ndcg": ev.ndcg(gains, k), "cp": ev.context_precision(gains, k),
            "order": order}

def rank_orders(q: str, items: list, llm, chars: int,
                k_perm: int, group: int, use: tuple) -> tuple:
    from bili_digest.retrieve import rag_core as rc
    from bili_digest.tools import bestfit as bf
    hits, n = _hits(items), len(items)
    orders, aux = {}, {}
    if "original" in use:
        orders["original"] = list(range(n))
    if "oracle" in use:
        orders["oracle"] = sorted(range(n), key=lambda i: (-items[i]["g"], i))
    if "heuristic" in use:
        hs = [dict(h) for h in hits]
        rc.heuristic_rerank(hs, q)
        orders["heuristic"] = [h["_idx"] for h in hs]
    if "single" in use:
        orders["single"] = bf.rank_single(q, hits, llm=llm, chars=chars)
    if "permsc" in use:
        o, tau, taus, _rs = bf.rank_permsc(q, hits, k=k_perm, llm=llm, chars=chars)
        orders["permsc"] = o
        aux["tau_mean"] = tau
        aux["taus"] = taus
    if "setwise" in use:
        orders["setwise"] = bf.rank_setwise(q, hits, group=group, llm=llm, chars=chars)
    return orders, aux

def run(limit: int, chars: int, k: int, k_perm: int, group: int,
        dry: bool, use: tuple, seed: int, source: str = "flywheel",
        workers: int = 4) -> dict:
    doc = _read_json(pool_path(source), None)
    if not doc:
        raise RuntimeError("没有池文件，先跑 --build")
    queries = doc["queries"]
    rng = random.Random(seed)

    idxs = list(range(len(queries)))
    rng.shuffle(idxs)
    if limit > 0:
        idxs = idxs[:limit]

    results = {} if dry else _read_json(result_path(source), {})
    if not dry:

        before = len(idxs)
        idxs = [qi for qi in idxs if queries[qi]["q"] not in results]
        if before != len(idxs):
            print("  跳过已完成 %d 条" % (before - len(idxs)), flush=True)
    print("  池源 %s · %d query · 基线：%s · 本次 %d · 并发 %d · 方法 %s%s"
          % (source, len(queries), doc["meta"]["baseline"], len(idxs),
             workers, ",".join(use),
             "  [DRY RUN 结果不可用于结论]" if dry else ""), flush=True)

    llm = CachedLLM(dry=dry)
    t0 = time.time()
    done = 0

    def one(qi: int):
        row = queries[qi]
        q = row["q"]
        items = row["items"]
        orders, aux = rank_orders(q, items, llm, chars, k_perm, group, use)
        rec = {"n": len(items), "gold_missing": row["gold_missing"],
               "m": {m: _metrics(o, items, k) for m, o in orders.items()},
               "aux": aux}
        return q, rec

    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(one, qi) for qi in idxs]
            for pos, fut in enumerate(as_completed(futs), 1):
                try:
                    q, rec = fut.result()
                except Exception as e:
                    print("    [!] 某条 query 失败（不中断）：%s %s"
                          % (type(e).__name__, str(e)[:70]), flush=True)
                    continue
                if not dry:
                    results[q] = rec
                    _write_json(result_path(source), results)
                done += 1
                if pos % 10 == 0 or pos == 1:
                    print("    ...%d/%d  LLM 新调用 %d 命中缓存 %d 失败 %d  %.0fs"
                          % (pos, len(idxs), llm.calls, llm.hits, llm.fails,
                             time.time() - t0), flush=True)
    finally:
        llm.flush()
    print("\n  跑完 %d 条 · 新调用 %d · 缓存命中 %d · 失败 %d · %.0fs"
          % (done, llm.calls, llm.hits, llm.fails, time.time() - t0))
    return results

def report(k: int, source: str = "flywheel") -> None:
    from bili_digest.tools import stats_test as st
    results = _read_json(result_path(source), {})
    if not results:
        print("  没有结果，先跑 --run")
        return
    keys = sorted(results)
    print("\n  配对统计：n=%d query（同一批候选、同一份 gold）" % len(keys))
    pool = _read_json(pool_path(source), {})
    print("  池源 = %s · 基线 = %s"
          % (source, pool.get("meta", {}).get("baseline", "?")))

    fixed = st.detectable_delta(len(keys), 0.0878)
    print("  独立功效：σ=0.0878（AGENTS-PLAN 已测）→ 本 n 可检测下限 %.4f；"
          "1.1pt 需 n≥%d" % (fixed, st.min_queries_needed(0.0878, 0.011)))
    print("  ⚠ 若效应量级小于 %.4f，本样本【没有分辨力】，不得据此下结论。\n"
          % fixed)

    for metric in ("ndcg", "cp"):
        cases = []
        for m in ("heuristic", "single", "permsc", "setwise"):
            if m not in results[keys[0]]["m"]:
                continue
            deltas = [r["m"][m][metric] - r["m"]["original"][metric]
                      for r in results.values() if m in r["m"]]
            sigma = statistics.pstdev(deltas) if len(deltas) > 1 else 0.0
            cases.append(("%s Δ%s@%d" % (m, metric.upper(), k), deltas, sigma))
        print("  ── %s ──" % metric.upper())
        out = st.report_many(cases)
        for r in out["rows"]:
            ci, w = r["ci"], r["wilcoxon"]
            ci_ok = ci["lo"] > 0 or ci["hi"] < 0
            power_ok = abs(ci["mean"]) >= fixed
            if r["reject"] and ci_ok and power_ok:
                flag = "显著"
            elif r["reject"]:
                flag = "未证实（CI 跨 0 或低于分辨力）"
            else:
                flag = "未证实"
            print("    %-22s 均值 %+.4f  CI[%+.4f,%+.4f]  p=%.4f  p_adj=%.4f  %s"
                  % (r["label"], ci["mean"], ci["lo"], ci["hi"],
                     w["p"], r["p_adj"], flag))
        print("    BH: 未校正显著 %d / 校正后显著 %d\n"
              % (out["n_sig_raw"], out["n_sig_adj"]))

    pairs = (("single", "heuristic", "single vs heuristic（LLM vs 手写规则）"),
             ("permsc", "single", "permsc vs single（多排列聚合是否有增益）"),
             ("setwise", "single", "setwise vs single（局部选择是否有增益）"),
             ("setwise", "heuristic", "setwise vs heuristic"))
    cases = []
    have = results[keys[0]]["m"]
    for x, y, label in pairs:
        if x not in have or y not in have:
            continue
        deltas = [r["m"][x]["ndcg"] - r["m"][y]["ndcg"]
                  for r in results.values() if x in r["m"] and y in r["m"]]
        if len(deltas) < 2:
            continue
        cases.append((label, deltas, statistics.pstdev(deltas)))
    if cases:
        print("\n  ── 方法间配对（ΔnDCG@%d，正值 = 前者更好）──" % k)
        rows = []
        for label, deltas, _sigma in cases:
            w = st.wilcoxon_signed_rank(deltas)
            ci = st.bootstrap_ci(deltas)
            rows.append((label, w, ci))
        bh = st.benjamini_hochberg([r[1]["p"] for r in rows])
        for (label, w, ci), pa, rej in zip(rows, bh["p_adj"], bh["reject"]):
            ci_ok = ci["lo"] > 0 or ci["hi"] < 0
            power_ok = abs(ci["mean"]) >= fixed

            if rej and ci_ok and power_ok:
                verdict = "显著"
            elif rej and not (ci_ok and power_ok):
                verdict = "未证实（CI 跨 0 或低于分辨力）"
            else:
                verdict = "未证实"
            print("    %-44s 均值 %+.4f  CI[%+.4f,%+.4f]  p=%.4f  p_adj=%.4f  %s"
                  % (label, ci["mean"], ci["lo"], ci["hi"], w["p"], pa, verdict))
        print("    BH 原始显著 %d / 校正后 p_adj<0.05 %d；"
              "最终判读还要过「CI 不跨 0」+「|均值|≥%.4f」两道门"
              % (sum(1 for r in rows if r[1]["p"] <= 0.05), bh["k"], fixed))

    taus = [r["aux"]["tau_mean"] for r in results.values()
            if r.get("aux") and "tau_mean" in r["aux"]]
    if taus:
        print("  PermSC 置换一致性 Kendall τ：均值 %.4f  中位 %.4f（n=%d，无需 gold）"
              % (sum(taus) / len(taus), statistics.median(taus), len(taus)))
        print("  τ 低 = 单次 list-wise 受输入顺序影响大；这正是 PermSC 要修的东西。")

    base = statistics.mean(r["m"]["original"]["ndcg"] for r in results.values())
    orc = statistics.mean(r["m"]["oracle"]["ndcg"] for r in results.values()) \
        if "oracle" in results[keys[0]]["m"] else float("nan")
    print("\n  nDCG@%d  original %.4f · oracle %.4f（可提升空间 %.4f）"
          % (k, base, orc, orc - base))
    if orc - base > 1e-9:
        for m in ("heuristic", "single", "permsc", "setwise"):
            if m not in have:
                continue
            gain = statistics.mean(
                r["m"][m]["ndcg"] - r["m"]["original"]["ndcg"]
                for r in results.values() if m in r["m"])
            print("    天花板达成率 %-9s %5.1f%%  （Δ=%.4f / %.4f）"
                  % (m, 100 * gain / (orc - base), gain, orc - base))
    print("  ★ 判读纪律（AGENTS-PLAN.md）：小于 1.1pt 的改动一律按未证实处理；")
    print("    且 gold 与本重排同源 LLM，结论只可解读为「对齐下游 LLM 偏好」。")

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--build", action="store_true", help="重建检索池（不调 LLM）")
    ap.add_argument("--run", action="store_true", help="跑对照")
    ap.add_argument("--report", action="store_true", help="只读结果报统计")
    ap.add_argument("--dry-run", action="store_true", help="假 LLM 验管线")
    ap.add_argument("--source", default="flywheel",
                    choices=("flywheel", "hybrid", "both"),
                    help="池源：flywheel=无信息乱序（n 足）/ hybrid=真实检索排名（n 少）")
    ap.add_argument("--limit", type=int, default=0, help="本次最多跑几个 query（0=全部）")
    ap.add_argument("--pool", type=int, default=10, help="每池最多几条候选")
    ap.add_argument("--chars", type=int, default=700, help="每条证据给 LLM 的字数")
    ap.add_argument("--k", type=int, default=10, help="指标 @k")
    ap.add_argument("--perms", type=int, default=3, help="PermSC 排列数")
    ap.add_argument("--group", type=int, default=3, help="Setwise 窗口大小")
    ap.add_argument("--seed", type=int, default=20261112)
    ap.add_argument("--workers", type=int, default=4,
                    help="query 级并发数（LLM 服务限流时调小）")
    ap.add_argument("--methods", default=",".join(METHODS))
    a = ap.parse_args()
    if not (a.build or a.run or a.report or a.dry_run):
        ap.print_help()
        return 0
    if a.build:
        build_pool(a.pool, "both" if a.source == "both" else a.source, a.seed)
    use = tuple(x.strip() for x in a.methods.split(",") if x.strip())
    src = a.source if a.source != "both" else "flywheel"
    if a.run or a.dry_run:
        r = run(a.limit if not a.dry_run else min(a.limit or 5, 5),
                a.chars, a.k, a.perms, a.group, a.dry_run, use, a.seed, src,
                a.workers if not a.dry_run else 1)
        if a.dry_run:
            print("\n  [DRY RUN] 抽样一条结果形状（数字无意义）：")
            q = next(iter(r), None)
            if q is None:

                doc = _read_json(pool_path(src), {})
                row = doc["queries"][0]
                llm = CachedLLM(dry=True)
                orders, aux = rank_orders(row["q"], row["items"], llm,
                                          a.chars, a.perms, a.group, use)
                print("    q=%r  n=%d  methods=%s  tau=%s"
                      % (row["q"][:36], len(row["items"]), sorted(orders),
                         aux.get("tau_mean")))
    if a.report:
        for s in (("flywheel", "hybrid") if a.source == "both" else (src,)):
            print("\n  ===== 池源 %s =====" % s)
            report(a.k, s)
    return 0

if __name__ == "__main__":
    sys.exit(main())
