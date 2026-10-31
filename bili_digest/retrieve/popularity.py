# -*- coding: utf-8 -*-

import os as _os
import sys as _sys

from bili_digest import paths

def _parse_extras(raw) -> dict:
    import json as _json
    try:
        ex = _json.loads(raw or "{}")
    except Exception:
        return {}

    def _int(*names):
        for n in names:
            v = ex.get(n)
            if v is None:
                continue
            if isinstance(v, str) and v.strip().isdigit():
                return int(v)
            if isinstance(v, (int, float)):
                return int(v)
        return None

    return {"play": _int("play", "view"),
            "like": _int("like"),
            "reply": _int("reply_count", "reply"),
            "danmaku": _int("danmaku_count", "danmaku"),
            "tags": ex.get("tags") or None}

def stats_of(con, uid: str) -> dict:
    r = con.execute("SELECT extras FROM item WHERE uid = ?", (uid,)).fetchone()
    if not r:
        return {}
    return _parse_extras(r[0] if not hasattr(r, "keys") else r["extras"])

def rank(con, uids: list, by: str = "play") -> list:
    rows = []
    for u in uids:
        s = stats_of(con, u)
        rows.append((u, s.get(by)))
    rows.sort(key=lambda x: (x[1] is None, -(x[1] or 0)))
    return rows

def top(con, by: str = "play", n: int = 20) -> list:
    rows = con.execute("SELECT uid, title, extras FROM item").fetchall()
    out = []
    for r in rows:
        out.append({"uid": r[0], "title": r[1] or "", **_parse_extras(r[2])})
    out.sort(key=lambda x: (x.get(by) is None, -(x.get(by) or 0)))
    return out[:n] if n and n > 0 else out

def boosted_score(base: float, stats: dict, weight: float = 0.3) -> float:
    import math
    p = stats.get("play")
    if not p or p <= 0:
        return base
    return base * (1.0 + weight * math.log10(1 + p) / 6.0)

def summary(con, uids: list) -> dict:
    rows = [dict(uid=u, **stats_of(con, u)) for u in uids]
    have = [r for r in rows if r.get("play") is not None]
    out = {"n": len(rows), "n_with_play": len(have)}
    if have:
        have.sort(key=lambda r: -(r["play"] or 0))
        out["top"] = have[:5]
        out["median_play"] = sorted(r["play"] for r in have)[len(have) // 2]
    if len(have) < len(rows):
        out["note"] = ("%d 条没有播放量数据 —— 早先抓的条目没记这些字段，"
                       "缺失不等于播放量为 0")
    return out

def main() -> int:
    import argparse
    from bili_digest import paths as _p
    _p.enable_utf8_stdout()
    from bili_digest.schema.core_schema import connect
    from bili_digest.paths import DB
    ap = argparse.ArgumentParser(description="热度：用库里的播放/点赞/弹幕数排序")
    ap.add_argument("uids", nargs="+", help="条目 uid 或 BV 号")
    ap.add_argument("--by", default="play", choices=["play", "like", "reply", "danmaku"])
    a = ap.parse_args()

    con = connect(DB)
    try:
        from bili_digest.paths import bili_uid
        uids = [u if ":" in u else bili_uid(u) for u in a.uids]
        for u, v in rank(con, uids, by=a.by):
            s = stats_of(con, u)
            print("  %-22s %s=%s  播放=%s 点赞=%s 弹幕=%s" % (
                u.split(":")[-1], a.by, v, s.get("play"), s.get("like"),
                s.get("danmaku")))
        print()
        s = summary(con, uids)
        print("  概览:", {k: v for k, v in s.items() if k != "top"})
        if s.get("top"):
            for t in s["top"]:
                print("    最热: %s  播放 %s" % (t["uid"].split(":")[-1], t["play"]))
    finally:
        con.close()

if __name__ == "__main__":
    main()
