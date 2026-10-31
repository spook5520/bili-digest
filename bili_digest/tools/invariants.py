# -*- coding: utf-8 -*-

import os as _os
import sys as _sys

ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
from bili_digest import paths

import random as _random

def _sample(con, n, seed=0):
    rows = con.execute(
        "SELECT b.item_uid, b.id, b.text FROM content_block b "
        "WHERE b.kind = ? AND LENGTH(b.text) BETWEEN 40 AND 120 "
        "AND b.item_uid IN (SELECT item_uid FROM content_block "
        "WHERE kind = ? GROUP BY item_uid HAVING COUNT(*) >= 4)",
        ("subtitle_seg", "subtitle_seg")).fetchall()
    if not rows:
        return []
    lst = list(rows)
    _random.Random(seed).shuffle(lst)
    return lst[:n]

def i1_self_identity(con, vi, n=20, seed=0):
    from bili_digest.retrieve import retrieve as _rv
    rows = _sample(con, n, seed)
    ok = fail = 0
    fails = []
    for r in rows:
        uid, text = r[0], str(r[2] or "")[:120]
        try:
            hits = _rv.retrieve(con, vi, text, granularity="item", top_k=1)
        except Exception:
            continue
        if hits and hits[0].get("item_uid") == uid:
            ok += 1
        else:
            fail += 1
            if len(fails) < 3:
                got = hits[0].get("item_uid") if hits else None
                fails.append((uid.split(":")[-1], text[:30], got))
    return {"name": "I1 自同一性", "ok": ok, "fail": fail,
            "rate": ok / max(ok + fail, 1), "fails": fails}

def i2_stopword_stable(con, vi, n=20, seed=0):
    from bili_digest.retrieve import retrieve as _rv
    rows = _sample(con, n, seed)
    ok = fail = 0
    fails = []
    for r in rows:
        uid, text = r[0], str(r[2] or "")[:80]
        try:
            a = _rv.retrieve(con, vi, text, granularity="item", top_k=1)
            b = _rv.retrieve(con, vi, text + "怎么", granularity="item", top_k=1)
        except Exception:
            continue
        if not a or not b:
            continue
        if a[0].get("item_uid") == b[0].get("item_uid"):
            ok += 1
        else:
            fail += 1
            if len(fails) < 3:
                fails.append((uid.split(":")[-1], text[:26],
                              b[0].get("item_uid").split(":")[-1]))
    return {"name": "I2 停用词不变", "ok": ok, "fail": fail,
            "rate": ok / max(ok + fail, 1), "fails": fails}

def i4_granularity(con, vi, n=15, seed=0):
    from bili_digest.retrieve import retrieve as _rv
    rows = _sample(con, n, seed)
    ok = fail = 0
    fails = []
    for r in rows:
        text = str(r[2] or "")[:80]
        try:
            it = _rv.retrieve(con, vi, text, granularity="item", top_k=3)
            bl = _rv.retrieve(con, vi, text, granularity="block", top_k=60)
        except Exception:
            continue
        if not it:
            continue
        bl_uids = set(h.get("item_uid") for h in bl)
        for h in it:
            u = h.get("item_uid")
            if u in bl_uids:
                ok += 1
            else:
                fail += 1
                if len(fails) < 3:
                    fails.append((str(u).split(":")[-1], text[:28]))
    return {"name": "I4 粒度一致", "ok": ok, "fail": fail,
            "rate": ok / max(ok + fail, 1), "fails": fails}

def i5_robustness(con, vi, n=0, seed=0):
    from bili_digest.retrieve import retrieve as _rv
    cases = [
        ("", "空串"),
        ("a", "单字符"),
        ("   ", "纯空白"),
        ("？？？？", "纯符号"),
        ("1111111111", "重复数字"),
        ("x" * 500, "超长 500 字符"),
        ("RAG " * 100, "重复词 100 次"),
    ]
    ok = fail = 0
    fails = []
    for q, label in cases:
        try:
            _rv.retrieve(con, vi, q, granularity="item", top_k=3)
            ok += 1
        except Exception as e:
            fail += 1
            fails.append((label, type(e).__name__ + ": " + str(e)[:60]))
    return {"name": "I5 健壮性", "ok": ok, "fail": fail,
            "rate": ok / max(ok + fail, 1), "fails": fails}

def i6_idempotent(con, vi, n=10, seed=0):
    from bili_digest.retrieve import retrieve as _rv
    rows = _sample(con, n, seed)
    ok = fail = 0
    fails = []
    for r in rows:
        q = str(r[2] or "")[:60]
        try:
            a = _rv.retrieve(con, vi, q, granularity="item", top_k=10)
            b = _rv.retrieve(con, vi, q, granularity="item", top_k=10)
        except Exception:
            continue
        ua = [h.get("item_uid") for h in a]
        ub = [h.get("item_uid") for h in b]
        if ua == ub:
            ok += 1
        else:
            fail += 1
            if len(fails) < 3:
                diff = [i for i in range(min(len(ua), len(ub))) if ua[i] != ub[i]]
                fails.append((q[:24], "首个差异位 " + str(diff[:1] or "长度不同")))
    return {"name": "I6 幂等性", "ok": ok, "fail": fail,
            "rate": ok / max(ok + fail, 1), "fails": fails}

def main() -> int:
    import argparse
    from bili_digest import paths as _p
    _p.enable_utf8_stdout()
    from bili_digest.schema.core_schema import connect
    from bili_digest.paths import DB, VEC
    from bili_digest.retrieve import rag_core as _rc
    ap = argparse.ArgumentParser(description="检索不变量测试（metamorphic）")
    ap.add_argument("--n", type=int, default=15, help="每条抽多少样本")
    ap.add_argument("--only", default=None, help="只跑某条（i1/i2/i4）")
    a = ap.parse_args()
    con = connect(DB)
    con.row_factory = __import__("sqlite3").Row
    vi = _rc.VectorIndex(str(VEC))
    try:
        tests = [("i1", i1_self_identity), ("i2", i2_stopword_stable),
                 ("i4", i4_granularity), ("i5", i5_robustness),
                 ("i6", i6_idempotent)]
        for key, fn in tests:
            if a.only and a.only != key:
                continue
            r = fn(con, vi, a.n)
            tot = r["ok"] + r["fail"]
            print("  %-14s %5.1f%%  (%d/%d)" % (
                r["name"], r["rate"] * 100, r["ok"], tot))
            for f in r["fails"]:
                print("      违反: %s" % str(f)[:96])
            print()
    finally:
        con.close()

if __name__ == "__main__":
    main()
