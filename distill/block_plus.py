#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
import paths

import json
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from paths import DB

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).parent

from core_schema import (BLOCK_KINDS, add_block_rule, connect, is_blocked,
                         load_blocklist, now_sec)

def block_up_of_video(con, bvid: str, reason: str = None, dry=False) -> dict:
    r = con.execute(
        "SELECT uid,item_id,author_uid,author_name,extras FROM item "
        "WHERE item_id=?", (bvid,)).fetchone()
    if not r:
        return {"error": f"库里没有 {bvid}"}
    if not r["author_uid"]:
        return {"error": f"{bvid} 没有 author_uid"}
    if dry:
        return {"kind": "up", "value": r["author_uid"], "label": r["author_name"]}
    add_block_rule(con, "up", r["author_uid"],
                   label=r["author_name"],
                   reason=reason or f"从 {bvid} 拉黑")

    n = con.execute("SELECT COUNT(*) FROM item WHERE author_uid=?",
                    (r["author_uid"],)).fetchone()[0]
    return {"kind": "up", "value": r["author_uid"],
            "label": r["author_name"], "affects": n}

def block_partition_of_video(con, bvid: str, dry=False) -> dict:
    r = con.execute("SELECT extras FROM item WHERE item_id=?", (bvid,)).fetchone()
    if not r:
        return {"error": f"库里没有 {bvid}"}
    try:
        ex = json.loads(r["extras"] or "{}")
    except Exception:
        ex = {}
    tname = ex.get("tname")
    if not tname:
        return {"error": f"{bvid} 没有分区信息（extras.tname 为空）"}
    if dry:
        return {"kind": "partition", "value": tname}
    add_block_rule(con, "partition", tname, label=tname,
                   reason=f"从 {bvid} 拉黑")
    n = con.execute("SELECT COUNT(*) FROM item WHERE extras LIKE ?",
                    (f'%"{tname}"%',)).fetchone()[0]
    return {"kind": "partition", "value": tname, "affects": n}

STOPWORDS = set("的了是吗呢啊哦嗯是在和与或有不这个我你他她它什么怎么如何"
                "就都也很还只把被让给对从到于并且但因为所以如果虽然"
                "一集全集完整版视频教程分享自制能可以怎样")

_QUESTION = re.compile(r"(吗|呢|吧|啊|么|怎样|怎么|如何|为什么)$")

def _title_terms(title: str) -> list[str]:
    try:
        import jieba
        jieba.setLogLevel(60)
        segs = jieba.lcut(str(title))
    except Exception:
        segs = re.split(r"[^\u4e00-\u9fffA-Za-z0-9]+", str(title))

    out = []
    for s in segs:
        s = s.strip()
        if len(s) < 2 or len(s) > 5:
            continue
        if s in STOPWORDS or _QUESTION.search(s):
            continue
        if s.isdigit():
            continue
        if re.fullmatch(r"[A-Za-z]+", s) and len(s) < 3:
            continue
        out.append(s)
    return out

def suggest_rules(con, min_items: int = 3, min_discard_ratio: float = 0.8,
                  topn: int = 10) -> dict:
    sug = {"up": [], "keyword": [], "partition": []}

    rows = con.execute("""
        SELECT i.author_uid, i.author_name,
               COUNT(*) n,
               SUM(CASE WHEN l.tier='discarded' THEN 1 ELSE 0 END) n_disc,
               SUM(CASE WHEN l.tier='knowledge' THEN 1 ELSE 0 END) n_keep
        FROM item i LEFT JOIN lifecycle l ON l.item_uid=i.uid
        WHERE i.author_uid IS NOT NULL
        GROUP BY i.author_uid HAVING n >= ?
    """, (min_items,)).fetchall()
    for r in rows:
        if r["n_keep"] > 0:
            continue
        ratio = r["n_disc"] / r["n"]
        if ratio >= min_discard_ratio:
            sug["up"].append({"value": r["author_uid"], "label": r["author_name"],
                              "count": r["n"], "discard": r["n_disc"],
                              "ratio": round(ratio, 2)})

    terms = Counter()
    for r in con.execute("""
        SELECT i.title FROM item i JOIN lifecycle l ON l.item_uid=i.uid
        WHERE l.tier='discarded'
    """):
        for t in _title_terms(r["title"]):
            terms[t] += 1
    for term, n in terms.most_common(topn):
        if n >= min_items:
            sug["keyword"].append({"value": term, "count": n})

    for r in con.execute("""
        SELECT json_extract(i.extras,'$.tname') tn,
               COUNT(*) n,
               SUM(CASE WHEN l.tier='discarded' THEN 1 ELSE 0 END) n_disc,
               SUM(CASE WHEN l.tier='knowledge' THEN 1 ELSE 0 END) n_keep
        FROM item i LEFT JOIN lifecycle l ON l.item_uid=i.uid
        WHERE tn IS NOT NULL
        GROUP BY tn HAVING n >= ?
    """, (min_items,)):
        if r["n_keep"] == 0 and r["n_disc"] / r["n"] >= min_discard_ratio:
            sug["partition"].append({"value": r["tn"], "count": r["n"],
                                     "discard": r["n_disc"]})

    sug["up"].sort(key=lambda x: -x["count"])
    sug["partition"].sort(key=lambda x: -x["count"])
    return sug

def filter_queue(con, rows: list) -> tuple[list, list]:
    rules = load_blocklist(con)
    if not any(rules.get(k) for k in BLOCK_KINDS):
        return rows, []

    keep, blocked = [], []
    for r in rows:
        uid = r["uid"]
        item = con.execute(
            "SELECT item_id,author_uid,title,extras FROM item WHERE uid=?",
            (uid,)).fetchone()
        if not item:
            keep.append(r)
            continue
        try:
            ex = json.loads(item["extras"] or "{}")
        except Exception:
            ex = {}
        tags = ex.get("tags")
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        hit, why = is_blocked(
            rules,
            item_id=str(item["item_id"] or ""),
            author_uid=str(item["author_uid"] or ""),
            partition=ex.get("tname"),
            tags=tags,
            text=str(item["title"] or ""))
        if hit:
            blocked.append({"uid": uid, "reason": why})
        else:
            keep.append(r)
    return keep, blocked

def add_cli_args(p, *, with_db: bool = True):
    if with_db:
        p.add_argument("--db", default=str(DB))
    p.add_argument("--from-video", metavar="BVID", help="拉黑该视频的 UP")
    p.add_argument("--from-partition", metavar="BVID", help="拉黑该视频的分区")
    p.add_argument("--suggest", action="store_true", help="从数据推断可屏蔽项")
    p.add_argument("--min-items", type=int, default=3)
    p.add_argument("--apply", metavar="KIND:VALUE", action="append",
                   help="采纳建议，如 --apply up:123456")
    p.add_argument("--list", action="store_true")
    return p

def run(a, con) -> dict:
    if a.from_video:
        r = block_up_of_video(con, a.from_video)
        con.commit()
        print(f"  {r}")
        return r
    if a.from_partition:
        r = block_partition_of_video(con, a.from_partition)
        con.commit()
        print(f"  {r}")
        return r
    if a.apply:
        for spec in a.apply:
            kind, _, value = spec.partition(":")
            add_block_rule(con, kind, value, reason="从建议采纳")
            print(f"  已添加 {kind}={value}")
        con.commit()
        return {"applied": len(a.apply)}
    if a.suggest:
        return _print_suggestions(con, a.min_items)
    if a.list:
        rules = load_blocklist(con)
        rules = {k: v for k, v in rules.items() if v}
        if not rules:
            print("  屏蔽库为空")
        for k, v in rules.items():
            print(f"  {k}: {len(v)} 条  {v[:6]}")
        return rules
    print("  用法：--suggest | --from-video BVID | --from-partition BVID "
          "| --apply KIND:VALUE | --list")
    return {}

def _print_suggestions(con, min_items: int) -> dict:
    sug = suggest_rules(con, min_items=min_items)
    print("  候选屏蔽规则（基于已被判 discard 的数据，不含主观猜测）\n")
    if sug["up"]:
        print("  【UP 主】该 UP 的视频全部被丢弃")
        for x in sug["up"][:8]:
            print(f"    up:{x['value']:<14} {str(x['label'])[:20]:<22}"
                  f" {x['discard']}/{x['count']} 丢弃")
    if sug["partition"]:
        print("\n  【分区】")
        for x in sug["partition"][:8]:
            print(f"    partition:{x['value']:<14} {x['discard']}/{x['count']} 丢弃")
    if sug["keyword"]:
        print("\n  【标题关键词】反复出现在被丢弃的视频里")
        for x in sug["keyword"][:10]:
            print(f"    keyword:{x['value']:<14} 出现 {x['count']} 次")
    if not any(sug.values()):
        print("  （暂无足够数据推断。需要先跑 retier --apply 产出分级结果）")
    return sug

def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="屏蔽库增强")
    add_cli_args(ap)
    a = ap.parse_args(argv)
    con = connect(a.db)
    con.row_factory = sqlite3.Row
    try:
        run(a, con)
    finally:
        con.close()

if __name__ == "__main__":
    main()
