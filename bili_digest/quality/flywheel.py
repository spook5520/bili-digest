#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

from bili_digest import paths
from bili_digest.paths import DB

FLY = Path(".cache/flywheel.jsonl")

def _load() -> list:
    if not FLY.exists():
        return []
    out = []
    for ln in FLY.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return out

def _bucket(rows: list, key_fn, label: str, min_n: int) -> list:
    g = defaultdict(lambda: [0, 0])
    for r in rows:
        k = key_fn(r)
        if k is None:
            continue
        g[k][0] += 1
        if r.get("score", 0) == 0:
            g[k][1] += 1
    out = []
    for k, (n, neg) in g.items():
        if n >= min_n:
            out.append({"维度": label, "取值": str(k), "n": n,
                        "负样本率": neg / n})
    return sorted(out, key=lambda x: -x["负样本率"])

def report(min_n: int = 15) -> dict:
    rows = _load()
    if not rows:
        return {"n": 0, "rows": [], "note": "还没有 flywheel 记录"}

    con = sqlite3.connect(str(DB))
    ids = list({r["block_id"] for r in rows if r.get("block_id")})
    kind_of, len_of = {}, {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        ph = ",".join("?" * len(chunk))
        for bid, kind, text in con.execute(
                "SELECT id, kind, LENGTH(text) FROM content_block "
                "WHERE id IN (%s)" % ph, chunk):
            kind_of[bid] = kind
            len_of[bid] = text or 0
    con.close()

    def _len_bucket(r) -> "str | None":
        n = len_of.get(r.get("block_id"))
        if n is None:
            return None
        return "<20" if n < 20 else ("20-60" if n < 60 else ("60-200" if n < 200 else ">=200"))

    out = []
    out += _bucket(rows, lambda r: kind_of.get(r.get("block_id")), "块类型", min_n)
    out += _bucket(rows, _len_bucket, "块长度", min_n)
    out += _bucket(rows, lambda r: (r.get("q") or "")[:2] or None, "查询开头两字", min_n)
    out += _bucket(rows, lambda r: len(r.get("q") or "") // 10 * 10 or None, "查询长度段", min_n)

    dist = defaultdict(int)
    for r in rows:
        dist[r.get("score", 0)] += 1
    return {"n": len(rows), "rows": out, "dist": dict(dist)}

def render(res: dict, top: int = 6) -> list:
    if not res.get("n"):
        return ["", "  （还没有 flywheel 记录：跑一次 eval_retrieval 就会开始积累）"]
    L = ["", "  ── 负样本诊断 ─────────────────────────", ""]
    tot = res["n"]
    L.append("  样本 %d 条 · 分数分布 %s" % (tot, res.get("dist")))
    L.append("")
    by_dim = defaultdict(list)
    for r in res["rows"]:
        by_dim[r["维度"]].append(r)
    for dim, items in by_dim.items():
        L.append("  【%s】0 分率最高的几档" % dim)
        for r in items[:top]:
            L.append("    %-10s n=%-4d 0分 %5.1f%%"
                     % (r["取值"][:10], r["n"], 100 * r["负样本率"]))
        L.append("")
    return L

def main() -> int:
    import argparse
    paths.enable_utf8_stdout()
    ap = argparse.ArgumentParser(description="从负样本里挖失败模式")
    ap.add_argument("--min", type=int, default=15, help="样本数下限（默认 15）")
    a = ap.parse_args()
    res = report(min_n=a.min)
    for ln in render(res):
        print(ln)
    return 0

if __name__ == "__main__":
    sys.exit(main())
