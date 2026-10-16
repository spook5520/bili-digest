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

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).parent
from classify import norm_tags
from paths import DIGEST_DIR

WANT = {
    "tags": "tags",
    "tname": "tname",
    "desc": "desc",
    "view": "view",
    "danmaku": "danmaku_count",
    "reply": "reply_count",
    "like": "like",
}

def _read_raw(bvid: str) -> dict:
    p = DIGEST_DIR / bvid / "raw.sqlite"
    if not p.exists():
        return {}
    con = sqlite3.connect(str(p))
    try:
        kv = {r[0]: r[1] for r in con.execute("SELECT k, v FROM meta")}
    except sqlite3.OperationalError:
        kv = {}
    finally:
        con.close()
    return kv

def collect(db: str, verbose: bool = True) -> dict:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row

    rows = con.execute(
        "SELECT uid, item_id, extras FROM item WHERE platform='bilibili'"
    ).fetchall()

    plan = []
    no_raw = 0
    for r in rows:
        try:
            cur = json.loads(r["extras"] or "{}") or {}
        except (ValueError, TypeError):
            cur = {}
        if cur.get("tags"):
            continue
        kv = _read_raw(r["item_id"])
        if not kv:
            no_raw += 1
            continue
        add = {}
        for raw_k, ex_k in WANT.items():
            v = kv.get(raw_k)
            if v in (None, "", []):
                continue
            if isinstance(v, str) and not v.strip():
                continue
            if ex_k in cur and cur[ex_k] not in (None, ""):
                continue
            add[ex_k] = norm_tags(v) if ex_k == "tags" else v
        if add:
            plan.append((r["uid"], r["item_id"], cur, add))

    if verbose:
        print(f"  扫描 {len(rows)} 条（B站）")
        print(f"  有 raw.sqlite 且可补: {len(plan)} 条")
        print(f"  无 raw.sqlite（尚未抓取）: {no_raw} 条")
        for uid, bv, cur, add in plan[:8]:
            keys = ", ".join(add)
            tg = add.get("tags")
            extra = f"  tags={tg[:3]}" if tg else ""
            print(f"    {bv:<14} +{keys}{extra}")
        if len(plan) > 8:
            print(f"    … 另 {len(plan)-8} 条")
    con.close()
    return {"plan": plan, "n_scan": len(rows), "no_raw": no_raw}

def apply(db: str, verbose: bool = True) -> dict:
    c = collect(db, verbose=verbose)
    plan = c["plan"]
    if not plan:
        if verbose:
            print("  无需补写")
        return {"updated": 0}

    con = sqlite3.connect(db)
    n = 0
    for uid, bv, cur, add in plan:
        cur.update(add)
        con.execute("UPDATE item SET extras=?, updated_at=strftime('%s','now') "
                    "WHERE uid=?", (json.dumps(cur, ensure_ascii=False), uid))
        n += 1
    con.commit()

    tagged = 0
    for uid, bv, cur, add in plan:
        for t in (add.get("tags") or []):
            try:
                con.execute(
                    "INSERT OR IGNORE INTO tag(item_uid,name,kind,source) "
                    "VALUES(?,?,?,?)", (uid, t, "system", "bilibili"))
                tagged += 1
            except sqlite3.OperationalError:
                pass
    con.commit()
    con.close()
    if verbose:
        print(f"\n  ✓ 补写 {n} 条 item.extras，新增 {tagged} 条 tag 记录")
        print("  [!] 标签变了 → classify 判定会变，建议重跑："
              "python kb.py retier")
    return {"updated": n, "tags_added": tagged}

def main():
    import argparse
    ap = argparse.ArgumentParser(description="从 raw.sqlite 补回 item.extras 字段")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--db", default=None)
    a = ap.parse_args()

    from paths import DB
    db = a.db or str(DB)
    if a.apply:
        apply(db)
    else:
        collect(db)

if __name__ == "__main__":
    main()
