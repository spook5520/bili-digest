# -*- coding: utf-8 -*-

import os as _os
import sys as _sys

ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
from bili_digest import paths

import random as _random

MIN_LEN = 12
MAX_LEN = 80
MIN_CORE = 6

def _core_len(t: str) -> int:
    out = []
    depth = 0
    for ch in t:
        if ch == "[":
            depth += 1
            continue
        if ch == "]" and depth:
            depth -= 1
            continue
        if depth:
            continue
        if ch.isalnum() or ch >= "一":
            out.append(ch)
    return len(out)

def usable(text: str) -> bool:
    t = " ".join((text or "").split())
    if not (MIN_LEN <= len(t) <= MAX_LEN):
        return False
    if "回复 @" in t or "回复@" in t:
        return False
    return _core_len(t) >= MIN_CORE

def from_comments(con, n: int = 1000, seed: int | None = None) -> list:
    rows = con.execute(
        "SELECT item_uid, text FROM content_block WHERE kind = ?",
        ("comment",)).fetchall()
    kept = []
    for r in rows:
        uid, text = r[0], r[1]
        if usable(text):
            kept.append((uid, " ".join(str(text).split()), "comment"))
    if not kept:
        return []
    _random.Random(seed).shuffle(kept) if seed is not None else _random.shuffle(kept)
    seen, out = set(), []
    for uid, q, src in kept:
        if q in seen:
            continue
        seen.add(q)
        out.append((uid, q, src))
        if len(out) >= n:
            break
    return out

def stats(con) -> dict:
    total = con.execute(
        "SELECT COUNT(*) FROM content_block WHERE kind = ?", ("comment",)).fetchone()[0]
    qs = from_comments(con, n=10 ** 9)
    uids = {u for u, _q, _s in qs}
    return {"comments": total, "usable": len(qs), "items": len(uids),
            "rate": round(len(qs) / max(total, 1), 3)}

def main() -> int:
    import argparse
    from bili_digest import paths as _p
    _p.enable_utf8_stdout()
    from bili_digest.schema.core_schema import connect
    from bili_digest.paths import DB
    ap = argparse.ArgumentParser(description="从评论造查询语料（零 LLM 成本）")
    ap.add_argument("--n", type=int, default=20, help="展示几条")
    ap.add_argument("--stats", action="store_true", help="只报统计")
    ap.add_argument("--seed", type=int, default=None)
    a = ap.parse_args()
    con = connect(DB)
    try:
        s = stats(con)
        print("  评论 %d 条 · 可用 %d 条（%.1f%%）· 覆盖 %d 个条目" % (
            s["comments"], s["usable"], s["rate"] * 100, s["items"]))
        if a.stats:
            return
        print()
        print("  样例（查询 → 目标条目）:")
        for uid, q, _src in from_comments(con, a.n, a.seed):
            print("    %-15s %s" % (uid.split(":")[-1], q[:70]))
    finally:
        con.close()

if __name__ == "__main__":
    main()
