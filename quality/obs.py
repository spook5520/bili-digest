#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
import paths

import argparse
import json

from core_schema import now_sec
import sys
import time
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).parent

def ensure_table(con):
    con.execute("""
    CREATE TABLE IF NOT EXISTS query_log (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        ts         INTEGER NOT NULL,
        question   TEXT NOT NULL,
        mode       TEXT,
        granularity TEXT,
        n_hits     INTEGER,
        top_uid    TEXT,
        ms         REAL,
        filt       TEXT
    )""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_ql_ts ON query_log(ts)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_ql_hits ON query_log(n_hits)")

def log_query(con, question: str, *, mode: str = None, granularity: str = None,
              n_hits: int = 0, top_uid: str = None, ms: float = 0.0,
              filt: dict = None, hits: list = None, commit: bool = True):
    try:
        ensure_table(con)
        cols = [r[1] for r in con.execute("PRAGMA table_info(query_log)")]
        if "hits_json" not in cols:
            con.execute("ALTER TABLE query_log ADD COLUMN hits_json TEXT")
        compact = None
        if hits:
            compact = json.dumps([
                {"rank": i + 1, "uid": h.get("item_uid") or h.get("uid"),
                 "block_id": h.get("block_id"), "kind": h.get("kind"),

                 "score": round(float(h.get("score") or 0.0), 4),
                 "title": (h.get("title") or "")[:80],
                 "text": (h.get("text") or "")[:160]}
                for i, h in enumerate(hits)
            ], ensure_ascii=False)
        con.execute(
            "INSERT INTO query_log(ts,question,mode,granularity,n_hits,top_uid,ms,filt,"
            "hits_json) VALUES(?,?,?,?,?,?,?,?,?)",
            (now_sec(), question, mode, granularity, n_hits, top_uid,
             round(ms, 1), json.dumps(filt, ensure_ascii=False) if filt else None,
             compact))
        if commit:
            con.commit()
    except Exception:
        pass

def ensure_pick_table(con):
    con.execute("""CREATE TABLE IF NOT EXISTS pick_log (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        ts        INTEGER NOT NULL,
        query_id  INTEGER,
        question  TEXT,
        rank      INTEGER,
        item_uid  TEXT,
        block_id  INTEGER,
        note      TEXT
    )""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_pick_uid ON pick_log(item_uid)")
    con.commit()

def log_pick(con, rank: int, item_uid: str, *, query_id: int = None,
             question: str = None, block_id: int = None, note: str = None) -> bool:
    try:
        ensure_pick_table(con)
        con.execute(
            "INSERT INTO pick_log(ts,query_id,question,rank,item_uid,block_id,note) "
            "VALUES(?,?,?,?,?,?,?)",
            (now_sec(), query_id, question, rank, item_uid, block_id, note))
        con.commit()
        return True
    except Exception:
        return False

def last_query(con) -> dict | None:
    ensure_table(con)
    r = con.execute("SELECT * FROM query_log ORDER BY id DESC LIMIT 1").fetchone()
    return dict(r) if r else None

def pick_report(con, verbose: bool = True) -> dict:
    ensure_pick_table(con)
    picks = con.execute("SELECT * FROM pick_log").fetchall()
    out = {"n": len(picks), "rank_dist": {}, "top1_ratio": None,
           "score_of_picked": [], "top_picked": []}
    if not picks:
        if verbose:
            print("  还没有采纳记录 —— 先 kb.py search 再看结果，然后 kb.py pick <第几条>")
        return out

    for r in picks:
        if r["rank"] is not None:
            out["rank_dist"][r["rank"]] = out["rank_dist"].get(r["rank"], 0) + 1
    tot_ranked = sum(out["rank_dist"].values())
    if tot_ranked:
        out["top1_ratio"] = out["rank_dist"].get(1, 0) / tot_ranked

    for r in picks:
        row = con.execute("SELECT score FROM lifecycle WHERE item_uid=?",
                          (r["item_uid"],)).fetchone()
        if row and row["score"] is not None:
            out["score_of_picked"].append(row["score"])
    all_scores = [x[0] for x in con.execute(
        "SELECT score FROM lifecycle WHERE score IS NOT NULL")]
    out["score_all_mean"] = (sum(all_scores) / len(all_scores)) if all_scores else None
    out["score_picked_mean"] = (sum(out["score_of_picked"]) / len(out["score_of_picked"])
                                if out["score_of_picked"] else None)

    for r in con.execute("""SELECT item_uid, COUNT(*) n FROM pick_log
                            GROUP BY item_uid ORDER BY -n LIMIT 10"""):
        it = con.execute("SELECT item_id,title FROM item WHERE uid=?",
                         (r["item_uid"],)).fetchone()
        out["top_picked"].append((it["item_id"] if it else r["item_uid"], r["n"],
                                  (it["title"] if it else "")))

    if verbose:
        print(f"  采纳记录 {out['n']} 条")
        if out["rank_dist"]:
            print(f"    排名分布: " +
                  " · ".join(f"{k}位×{v}" for k, v in sorted(out["rank_dist"].items())))
            print(f"    top1 占比 {out['top1_ratio']*100:.0f}%  "
                  f"→ {'排序可信' if out['top1_ratio'] >= 0.5 else '排序有改进空间（召回够、排序差）'}")
        if out["score_picked_mean"] is not None and out["score_all_mean"] is not None:
            d = out["score_picked_mean"] - out["score_all_mean"]
            print(f"    评分对照: 被采纳均值 {out['score_picked_mean']:.3f} · "
                  f"全库均值 {out['score_all_mean']:.3f} （{d:+.3f}）")
            if d > 0.05:
                print(f"      → 正相关：评分与「实际有用」方向一致")
            elif d < -0.05:
                print(f"      → 负相关：评分高的反而不常被用，该复核维度权重")
            else:
                print(f"      → 无差别：评分与「有没有用」基本无关 —— "
                      f"这正是「评分只该做提示、不该做门槛」的实证")
        if out["top_picked"]:
            print(f"    被反复采纳的:")
            for bv, n, t in out["top_picked"][:5]:
                print(f"      {n} 次  {bv:<14} {str(t)[:40]}")
    return out

MIN_REPEAT = 5

def report(con, only_empty: bool = False, limit: int = 25, picks: bool = True):
    ensure_table(con)
    tot = con.execute("SELECT COUNT(*) n FROM query_log").fetchone()[0]
    if not tot:
        print("  还没有查询记录（跑几次 kb.py search 就有了）")
        return
    empty = con.execute("SELECT COUNT(*) n FROM query_log WHERE n_hits=0").fetchone()[0]
    avg_ms = con.execute("SELECT AVG(ms) a FROM query_log").fetchone()[0] or 0
    slow = con.execute("""SELECT question, ms FROM query_log
                          ORDER BY ms DESC LIMIT 5""").fetchall()

    print(f"═══ 检索观测 ═══")
    print(f"  总查询 {tot} · 空结果 {empty} ({empty/tot*100:.1f}%) · 平均 {avg_ms:.0f}ms")
    print(f"\n  空结果率是这里最有价值的信号 —— 它同时指向两种问题：")
    print(f"    ① 内容缺口（库里确实没有）")
    print(f"    ② 查询改写缺失（有内容但说法不匹配）")
    print(f"  区分两者需要人工看几条，然后决定加内容还是加改写。")

    print()
    print("  ── 分数最低的命中（有结果 ≠ 有答案，仅供人工判断）──")
    _rows = con.execute(
        "SELECT question, hits_json, ts FROM query_log "
        "WHERE hits_json IS NOT NULL AND hits_json != '[]' "
        "ORDER BY ts DESC LIMIT 300").fetchall()
    _flat = []
    for _q, _hj, _ts in _rows:
        try:
            for _h in json.loads(_hj):
                if "score" in _h and _h.get("title"):
                    _flat.append((float(_h["score"]), _q, _h))
        except Exception:
            continue
    _flat.sort(key=lambda x: x[0])
    if not _flat:
        print("    （无带分数的记录 —— 旧日志没有 score 字段，跑几次检索即可）")
    for _s, _q, _h in _flat[:6]:
        print("    %.4f  「%s」" % (_s, str(_q)[:26]))
        print("            → 《%s》 %s" % (str(_h.get("title"))[:34],
                                       str(_h.get("kind") or "")))
    if _flat:
        print("    读法：分数越低越可疑，但不能按阈值一刀切 ——"
              " 相关与无关查询的分数是重叠的。")
    if only_empty or empty:
        print(f"\n  ── 返回空的查询（{'全部' if not only_empty else '只列这些'}）──")
        for r in con.execute("""SELECT question, granularity, ts FROM query_log
                                WHERE n_hits=0 ORDER BY ts DESC LIMIT ?""", (limit,)):
            t = time.strftime("%m-%d %H:%M", time.localtime(r["ts"]))
            print(f"    [{t}] ({r['granularity'] or '-'}) {r['question'][:50]}")

    if not only_empty:
        print(f"\n  ── 最慢的查询 ──")
        for r in slow:
            print(f"    {r['ms']:>7.0f}ms  {r['question'][:48]}")

        print(f"\n  ── 重复查询（问过多次 → 该考虑做成主题页）──")
        _rep = con.execute(
            "SELECT question, COUNT(*) n, MIN(ts) t0, MAX(ts) t1 "
            "FROM query_log GROUP BY question HAVING n >= ? "
            "ORDER BY n DESC LIMIT 10", (MIN_REPEAT,)).fetchall()
        if not _rep:
            print(f"    （没有达到门槛的重复 —— 门槛 {MIN_REPEAT} 次）")
        for r in _rep:
            _span_h = (r["t1"] - r["t0"]) / 3600.0
            _tag = ""
            if _span_h < 2:
                _tag = "  ← 集中在 %.0f 分钟里，多半是测试/调试" % (_span_h * 60)
            print(f"    {r['n']:>3}×  {r['question'][:44]}{_tag}")
        if _rep:
            print("    读法：重复多【且分散在多天】才说明该做成主题页；"
                  "集中在一小段时间的多半是测试。")

def main():
    ap = argparse.ArgumentParser(description="检索的可观测性")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--empty", action="store_true", help="只看返回空的查询")
    ap.add_argument("--clear", action="store_true", help="清空日志")
    a = ap.parse_args()

    from core_schema import connect
    from paths import DB
    con = connect(DB)
    con.row_factory = __import__("sqlite3").Row

    if a.clear:
        ensure_table(con)
        con.execute("DELETE FROM query_log")
        con.commit()
        print("  已清空查询日志")
    else:
        report(con, only_empty=a.empty)
        print()
        pick_report(con)
    con.close()

if __name__ == "__main__":
    main()
