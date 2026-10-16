#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
import paths

import json
import sqlite3
import sys
from pathlib import Path
from paths import DB, source_key, PLATFORM
from sources import extract_sources, kinds_for, label_of, role_of

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).parent

from core_schema import connect, now_sec

DDL = """
CREATE TABLE IF NOT EXISTS source_weight (
    source_key  TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,      -- up / partition / platform / tag
    label       TEXT,
    n_items     INTEGER DEFAULT 0,
    avg_score   REAL,               -- LLM 细滤均分（1~5）
    keep_ratio  REAL,               -- (knowledge+raw)/n
    usage_count INTEGER DEFAULT 0,  -- 检索命中次数
    manual      REAL,               -- 人工评分（0~1），给了则主导
    weight      REAL,               -- 最终权重 0~1
    updated_at  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_sw_kind ON source_weight(kind);
"""

W_BASE = 0.40
W_SCORE = 0.35
W_KEEP = 0.25

from score import Factor, Scorer as _Scorer

_SOURCE_SCORER = _Scorer("source", [
    Factor("base", W_BASE, lambda c: 1.0),
    Factor("score", W_SCORE, lambda c: c.get("ns") or 0.0),
    Factor("keep", W_KEEP, lambda c: c.get("keep_ratio") or 0.0),
])
_NO_SCORE_DISCOUNT = 0.85

def ensure_table(con):
    con.executescript(DDL)
    con.commit()

def _norm_score(s) -> float | None:
    if s is None:
        return None
    return max(0.0, min(1.0, (float(s) - 1.0) / 4.0))

def rebuild(con, verbose=True) -> dict:
    ensure_table(con)

    rows = con.execute("""
        SELECT i.uid, i.platform, i.item_id, i.author_uid, i.author_name,
               i.extras, COALESCE(l.tier,'unknown') tier
        FROM item i
        LEFT JOIN lifecycle l ON l.item_uid = i.uid
    """).fetchall()

    score_map = {}
    for r in con.execute("SELECT item_uid, content FROM derived WHERE kind='scores'"):
        try:
            score_map[r["item_uid"]] = json.loads(r["content"]).get("mean")
        except Exception:
            pass

    groups = {}
    for r in rows:
        tier = r["tier"]
        keep = 1 if tier in ("knowledge", "raw") else 0
        sc = score_map.get(r["uid"])

        item = {k: r[k] for k in r.keys()}
        entries = [(source_key(kind, value, item.get("platform") or PLATFORM),
                    kind, label)
                   for kind, value, label in extract_sources(item)]

        for key, kind, label in entries:
            g = groups.setdefault(key, {"kind": kind, "label": label,
                                        "n": 0, "keep": 0, "scores": []})
            g["n"] += 1
            g["keep"] += keep
            if sc is not None:
                g["scores"].append(float(sc))
            if not g["label"] and label:
                g["label"] = label

    ts = now_sec()
    n_written = 0
    for key, g in groups.items():
        avg = round(sum(g["scores"]) / len(g["scores"]), 3) if g["scores"] else None
        keep_ratio = round(g["keep"] / g["n"], 3) if g["n"] else 0.0
        ns = _norm_score(avg)

        _r = _SOURCE_SCORER.evaluate({"ns": ns, "keep_ratio": keep_ratio})
        weight = _r.score * (1.0 if ns is not None else _NO_SCORE_DISCOUNT)
        weight = max(0.0, min(1.0, weight))

        old = con.execute("SELECT manual FROM source_weight WHERE source_key=?",
                          (key,)).fetchone()
        manual = old["manual"] if old else None
        if manual is not None:
            weight = float(manual)

        con.execute("""
            INSERT INTO source_weight(source_key,kind,label,n_items,avg_score,
                                      keep_ratio,usage_count,manual,weight,updated_at)
            VALUES(?,?,?,?,?,?,0,?,?,?)
            ON CONFLICT(source_key) DO UPDATE SET
                kind=excluded.kind, label=excluded.label,
                n_items=excluded.n_items, avg_score=excluded.avg_score,
                keep_ratio=excluded.keep_ratio,
                weight=excluded.weight, updated_at=excluded.updated_at
        """, (key, g["kind"], g["label"], g["n"], avg, keep_ratio,
              manual, round(weight, 4), ts))
        n_written += 1

    con.commit()
    if verbose:
        print(f"  重算 {n_written} 个源的权重")
    return {"sources": n_written}

def up_weight(con, author_uid: str) -> float | None:
    if not author_uid:
        return None
    r = con.execute("SELECT weight FROM source_weight WHERE source_key=?",
                    (source_key("up", author_uid),)).fetchone()
    return float(r["weight"]) if r else None

def median_weight(con) -> float:
    vs = sorted((r[0] or 0.0) for r in
                con.execute("SELECT weight FROM source_weight WHERE kind='up'"))
    return float(vs[len(vs) // 2]) if vs else 0.0

def up_factor(con, author_uid: str) -> float:
    v = up_weight(con, author_uid)
    if v is None:
        return median_weight(con)
    return max(0.0, min(1.0, float(v)))

def bump_usage(con, item_uids: list[str]) -> int:
    ensure_table(con)
    n = 0
    for uid in item_uids:
        r = con.execute("SELECT author_uid FROM item WHERE uid=?", (uid,)).fetchone()
        if not r or not r["author_uid"]:
            continue
        con.execute("UPDATE source_weight SET usage_count=usage_count+1, updated_at=? "
                    "WHERE source_key=?", (now_sec(), source_key("up", r["author_uid"])))
        n += 1
    con.commit()
    return n

def show(con, order: str = "top", limit: int = 12, kind: str = None):
    ensure_table(con)
    sql = "SELECT * FROM source_weight"
    args = []
    if kind:
        sql += " WHERE kind=?"
        args.append(kind)
    sql += f" ORDER BY weight {'DESC' if order == 'top' else 'ASC'} LIMIT ?"
    args.append(limit)

    print(f"  {'权重':>6}  {'条目':>5}  {'细滤':>5}  {'保留率':>7}  {'用量':>5}  源")
    print("  " + "-" * 74)
    for r in con.execute(sql, args):
        avg = f"{r['avg_score']:.2f}" if r["avg_score"] is not None else "  - "
        manual = " [人工]" if r["manual"] is not None else ""
        print(f"  {r['weight']:>6.3f}  {r['n_items']:>5}  {avg:>5}  "
              f"{r['keep_ratio']:>7.2f}  {r['usage_count']:>5}  "
              f"{r['kind']}:{str(r['label'] or r['source_key'])[:26]}{manual}")

def add_cli_args(p, *, with_db: bool = True):
    if with_db:
        p.add_argument("--db", default=str(DB))
    p.add_argument("--rebuild", action="store_true", help="重算全部源权重")
    p.add_argument("--top", type=int, nargs="?", const=12)
    p.add_argument("--bottom", type=int, nargs="?", const=12, help="最该淘汰的源")
    p.add_argument("--kind", choices=["up", "partition", "platform"])
    p.add_argument("--manual", help="人工评分，格式 kind:value:0~1，如 up:123456:0.9")
    return p

def run(a, con) -> dict:
    if a.manual:
        try:
            kind, value, score = a.manual.rsplit(":", 2)
            score = float(score)
        except ValueError:
            raise SystemExit("格式应为 kind:value:0~1")
        key = source_key(kind, value)
        ensure_table(con)
        n = con.execute("UPDATE source_weight SET manual=?, weight=?, updated_at=? "
                        "WHERE source_key=?",
                        (score, max(0.0, min(1.0, score)), now_sec(), key)).rowcount
        con.commit()
        if n:
            print(f"  已设人工评分 {key} = {score}")
        else:
            print(f"  没找到 {key}（先跑 --rebuild 生成）")
        return {"manual": key, "applied": bool(n)}
    if a.rebuild:
        r = rebuild(con)
        print("  " + json.dumps(r, ensure_ascii=False))
        show(con, "top", 10, a.kind)
        return r
    if a.top is not None:
        show(con, "top", a.top, a.kind)
        return {"shown": "top"}
    if a.bottom is not None:
        print("  最该淘汰的源：")
        show(con, "bottom", a.bottom, a.kind)
        return {"shown": "bottom"}
    print("  用法：--rebuild | --top [N] | --bottom [N] | --manual kind:value:0~1")
    return {}

def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="源权重（反馈淘汰）")
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
