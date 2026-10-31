
import os as _os
import sys as _sys

from bili_digest import paths

import argparse
import json
import random
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

from bili_digest.paths import DB, enable_utf8_stdout

enable_utf8_stdout()
from bili_digest.schema.core_schema import MIN_INFO_CHARS, info_len
from bili_digest.retrieve import retrieve as rv
from bili_digest.retrieve import partitioned_search as ps

def build_cases(con, n_items: int, seed: int = 42):
    random.seed(seed)
    cases = []
    rows = con.execute("""
        SELECT i.uid, i.item_id, i.title,
          (SELECT COUNT(*) FROM content_block cb WHERE cb.item_uid=i.uid
             AND cb.kind IN ('subtitle','subtitle_seg','danmaku','comment')) n
        FROM item i WHERE n >= 30 ORDER BY RANDOM() LIMIT ?""",
        (n_items,)).fetchall()
    for r in rows:
        uid, item_id = r["uid"], r["item_id"]
        qs = {}

        for kind in ("subtitle", "danmaku", "comment"):
            cands = con.execute(
                "SELECT text FROM content_block WHERE item_uid=? AND kind=? "
                "AND LENGTH(text) BETWEEN 20 AND 60 ORDER BY RANDOM() LIMIT 20",
                (uid, kind)).fetchall()
            for c in cands:
                if info_len(c["text"]) >= MIN_INFO_CHARS:
                    qs["原文"] = c["text"]
                    break
            if "原文" in qs:
                break

        d = {}
        for x in con.execute("SELECT kind,content FROM derived WHERE item_uid=?",
                             (uid,)):
            try:
                d[x["kind"]] = json.loads(x["content"])
            except (ValueError, TypeError):
                pass
        if isinstance(d.get("summary"), str) and len(d["summary"]) > 20:
            qs["摘要"] = d["summary"][:150]
        tp = d.get("topics")
        if isinstance(tp, list) and tp:
            qs["主题词"] = " ".join(str(x) for x in tp[:5])
        ins = d.get("insights")
        if isinstance(ins, list) and ins:
            c = ins[0].get("claim") if isinstance(ins[0], dict) else None
            if c:
                qs["洞察"] = str(c)[:120]
        if qs:
            cases.append((item_id, r["title"], qs))
    return cases

def run(n_items=40, top_k=5, mode="hybrid", seed=42):
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row

    if mode == "partitioned":
        from fastembed import TextEmbedding
        vi = ps._load_index()
        model = TextEmbedding(model_name=ps.EMBED_MODEL,
                              cache_dir=str(Path(__file__).parent / "models"))
    else:
        from bili_digest.retrieve import rag_core as rc
        vi = rc.VectorIndex(str(ps.VEC))
        model = None

    cases = build_cases(con, n_items, seed)
    stat = {"原文": Counter(), "摘要": Counter(), "主题词": Counter(),
            "洞察": Counter()}
    err_msgs = Counter()
    t0 = time.time()
    misses = []
    for item_id, title, qs in cases:
        for label, q in qs.items():
            try:
                hits = rv.retrieve(con, vi, q, granularity="item",
                                   top_k=top_k, mode=mode, model=model)
            except Exception as e:
                stat[label]["err"] += 1
                err_msgs[f"{type(e).__name__}: {str(e)[:70]}"] += 1
                continue
            ids = [str(h.get("item_id") or "") for h in hits]
            rank = next((i + 1 for i, s in enumerate(ids) if s == item_id), None)
            if rank == 1:
                stat[label]["r1"] += 1
            elif rank:
                stat[label]["r5"] += 1
            else:
                stat[label]["miss"] += 1
                misses.append((label, item_id, q))

    el = time.time() - t0
    total = sum(sum(c.values()) for c in stat.values())
    n_err = sum(c["err"] for c in stat.values())
    print(f"  ── 检索鲁棒性（{mode} 链路 · granularity=item · top_k={top_k}）──")
    print(f"  条目 {len(cases)} 个，共 {total} 次查询，用时 {el:.1f}s")
    if n_err:
        print(f"  [!] 其中 {n_err} 次调用抛异常（{n_err/total*100:.0f}%）—— "
              f"比例高时下面的命中率不具意义，先看异常原因")
        print("      异常分布:")
        for msg, cnt in err_msgs.most_common(4):
            print(f"        {cnt:>4} 次  {msg}")
    print()
    print(f"  {'查询来源':<9}{'次数':>6}{'异常':>6}{'R@1':>8}{'前5':>8}{'未命中':>8}   说明")
    note = {"原文": "字面相同（基线：检索有没有坏）",
            "摘要": "意思相同字面不同（语义检索能力）",
            "主题词": "极短意图（模糊查询）",
            "洞察": "判断句反查内容"}
    for label in ("原文", "摘要", "主题词", "洞察"):
        c = stat[label]
        n_ok = c["r1"] + c["r5"] + c["miss"]
        n_all = n_ok + c["err"]
        if not n_all:
            continue
        if not n_ok:
            print(f"  {label:<9}{n_all:>6}{c['err']:>6}{'—':>8}{'—':>8}"
                  f"{'—':>8}   {note[label]}（全异常，无有效样本）")
            continue
        print(f"  {label:<9}{n_all:>6}{c['err']:>6}"
              f"{c['r1']/n_ok*100:>7.0f}%"
              f"{(c['r1']+c['r5'])/n_ok*100:>7.0f}%{c['miss']:>8}   {note[label]}"
              + ("  ← 分母只含成功调用" if c["err"] else ""))
    if misses:
        print(f"\n  未命中样本（前 8）:")
        for label, item_id, q in misses[:8]:
            print(f"    [{label}] {item_id:<14} {str(q)[:44]!r}")
    con.close()
    return stat

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="检索鲁棒性观测")
    ap.add_argument("--n", type=int, default=40, help="样本条目数")
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--mode", default="hybrid", choices=("hybrid", "partitioned"))
    ap.add_argument("--seed", type=int, default=42)
    a = ap.parse_args()
    run(a.n, a.top_k, a.mode, a.seed)
