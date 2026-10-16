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
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).parent

STOP = set("""
的 了 是 在 我 有 和 就 不 人 都 一 一个 上 也 很 到 说 要 去 你 会 着 没有 看 好
自己 这 那 它 他 她 们 这个 那个 什么 怎么 为什么 因为 所以 但是 而且 然后
可以 这样 那样 时候 现在 还是 已经 就是 如果 只是 这样 那样 这些 那些
吧 呢 啊 哦 嗯 呀 吗 嘛 哈 诶 唉 喂 嘿 呜 嗷 哇 哎
一个 一些 一样 一直 完全 不存在 可能 应该 或者 以及 同时 其实 真的 确实
非常 特别 比较 更加 最好 第一 第二 最后 开始 结束 部分 方面 情况 问题
内容 视频 事情 东西 地方 时间 方式 方法 结果 原因 意思 感觉 觉得 知道
the a an and or but if of to in on at for with is are was were be been
this that these those it its as by from not no yes you your we our they
""".split())

def _tok(text: str) -> list:
    import jieba
    out = []
    for w in jieba.cut(text or ""):
        w = w.strip().lower()
        if len(w) < 2:
            continue
        if w in STOP:
            continue
        if re.fullmatch(r"[\d\W_]+", w):
            continue
        out.append(w)
    return out

def ensure_table(con):
    con.executescript("""
    CREATE TABLE IF NOT EXISTS keyword (
        term     TEXT PRIMARY KEY,
        df       INTEGER DEFAULT 0,     -- 文档频率（出现在多少个块里）
        n_blocks INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS keyword_block (
        term     TEXT NOT NULL,
        block_id INTEGER NOT NULL,
        tf       INTEGER DEFAULT 1,     -- 该块内出现次数
        weight   REAL DEFAULT 1.0,      -- tf-idf
        PRIMARY KEY (term, block_id)
    );
    CREATE INDEX IF NOT EXISTS idx_kb_term ON keyword_block(term);
    CREATE INDEX IF NOT EXISTS idx_kb_block ON keyword_block(block_id);
    """)

def build(con, kinds=("subtitle", "subtitle_seg", "danmaku", "comment", "meta"),
          min_df: int = 2, max_df_ratio: float = 0.5, verbose: bool = True) -> dict:
    ensure_table(con)
    con.execute("DELETE FROM keyword")
    con.execute("DELETE FROM keyword_block")

    df = Counter()
    per_block = {}
    qmarks = ",".join("?" * len(kinds))
    rows = con.execute(
        f"SELECT id, text FROM content_block WHERE kind IN ({qmarks})", kinds).fetchall()
    n_blocks = len(rows)
    if verbose:
        print(f"  扫描 {n_blocks} 个块…")
    for r in rows:
        toks = _tok(r["text"])
        if not toks:
            continue
        c = Counter(toks)
        per_block[r["id"]] = c
        for t in c:
            df[t] += 1

    max_df = int(n_blocks * max_df_ratio)
    kept = {t: d for t, d in df.items() if d >= min_df and d <= max_df}
    if verbose:
        print(f"  词表：{len(df)} → 保留 {len(kept)}"
              f"（丢弃 df<{min_df} 的 {sum(1 for d in df.values() if d < min_df)}，"
              f"df>{max_df} 的 {sum(1 for d in df.values() if d > max_df)}）")

    con.executemany("INSERT OR REPLACE INTO keyword(term,df,n_blocks) VALUES(?,?,?)",
                    [(t, d, n_blocks) for t, d in kept.items()])

    batch = []
    for bid, c in per_block.items():
        for t, tf in c.items():
            if t not in kept:
                continue

            w = (1 + math.log(tf)) * math.log((n_blocks + 1) / (df[t] + 1))
            batch.append((t, bid, tf, round(w, 4)))
    con.executemany(
        "INSERT OR REPLACE INTO keyword_block(term,block_id,tf,weight) VALUES(?,?,?,?)",
        batch)
    con.commit()
    if verbose:
        print(f"  二部图：{len(kept)} 词 × {len(batch)} 条边")
    return {"terms": len(kept), "edges": len(batch), "blocks": n_blocks}

def search(con, query: str, k: int = 128, expand: bool = True,
           min_ratio: float = 0.45, min_term_hit: float = 0.34) -> list:
    terms = _tok(query)
    if not terms:
        return []
    qmarks = ",".join("?" * len(terms))

    scores = defaultdict(float)
    hit_terms = set()
    for r in con.execute(
            f"SELECT term, block_id, weight FROM keyword_block "
            f"WHERE term IN ({qmarks})", terms):
        scores[r["block_id"]] += r["weight"]
        hit_terms.add(r["term"])

    if len(hit_terms) / len(terms) < min_term_hit:
        return []

    if expand and hit_terms:
        co = defaultdict(int)
        hm = ",".join("?" * len(hit_terms))
        for r in con.execute(
                f"SELECT a.block_id FROM keyword_block a "
                f"JOIN keyword_block b ON b.block_id=a.block_id "
                f"WHERE a.term IN ({hm}) AND b.term NOT IN ({hm})",
                list(hit_terms) + list(hit_terms)):
            co[r["block_id"]] += 1

        if co:
            mx = max(co.values())
            for bid, n in co.items():
                if n >= max(2, mx * 0.3):
                    scores[bid] += 0.3 * n

    if not scores:
        return []
    mx = max(scores.values())

    out = [(bid, s) for bid, s in sorted(scores.items(), key=lambda x: -x[1])
           if s >= mx * min_ratio][:k]
    return [(bid, s / mx) for bid, s in out]

def stats(con, top: int = 20) -> dict:
    ensure_table(con)
    n_term = con.execute("SELECT COUNT(*) n FROM keyword").fetchone()[0]
    n_edge = con.execute("SELECT COUNT(*) n FROM keyword_block").fetchone()[0]
    print(f"  关键词 {n_term} · 边 {n_edge}")
    print(f"\n  文档频率最高的词（判别力低，已被 max_df 限制）:")
    for r in con.execute("SELECT term, df FROM keyword ORDER BY df DESC LIMIT ?", (top,)):
        print(f"    {r['df']:>6}  {r['term']}")
    print(f"\n  连接块最多的词（结构中心）:")
    for r in con.execute("""SELECT term, COUNT(*) n FROM keyword_block
                            GROUP BY term ORDER BY n DESC LIMIT ?""", (top,)):
        print(f"    {r['n']:>6}  {r['term']}")
    return {"terms": n_term, "edges": n_edge}

def main():
    import argparse
    ap = argparse.ArgumentParser(description="关键词二部图")
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--search", help="试搜一个查询")
    ap.add_argument("--min-df", type=int, default=2)
    a = ap.parse_args()

    from core_schema import connect
    from paths import DB
    con = connect(DB)
    con.row_factory = __import__("sqlite3").Row

    if a.build:
        r = build(con, min_df=a.min_df)
        print(f"  {r}")
    if a.stats:
        stats(con)
    if a.search:
        hits = search(con, a.search, k=10)
        print(f"\n  搜「{a.search}」→ {len(hits)} 块")
        for bid, s in hits:
            row = con.execute("SELECT kind, text FROM content_block WHERE id=?",
                              (bid,)).fetchone()
            if row:
                print(f"    {s:.3f} [{row['kind']}] {str(row['text'])[:60]}")
    con.close()

if __name__ == "__main__":
    main()
