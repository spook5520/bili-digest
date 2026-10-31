#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os as _os
import sys as _sys

from bili_digest import paths

import json
import re
import sqlite3
import sys
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).parent

from bili_digest.schema.core_schema import sample_blocks

DANMAKU_SAMPLE = 300
COMMENT_SAMPLE = 60

def gather(con: sqlite3.Connection, uid: str) -> dict:
    ev = {
        "uid": uid,
        "title": "", "tags": [], "desc": "",
        "duration": 0, "sub_chars": 0, "n_blocks": 0,
        "n_subtitle": 0, "n_seg": 0, "seg_chars": 0,
        "n_danmaku": 0, "n_comment": 0,
        "danmaku_chars": 0, "comment_chars": 0,
        "danmaku_text": "", "comment_text": "",
        "top_comment_likes": 0,
    }

    row = con.execute(
        "SELECT title, duration_sec, extras FROM item WHERE uid=?", (uid,)).fetchone()
    if row:
        ev["title"] = row["title"] or ""
        ev["duration"] = row["duration_sec"] or 0
        try:
            ex = json.loads(row["extras"] or "{}") or {}
        except (ValueError, TypeError):
            ex = {}
        ev["tags"] = ex.get("tags") or []
        ev["desc"] = ex.get("desc") or ""

    for r in con.execute(
            "SELECT kind, COUNT(*) n, COALESCE(SUM(LENGTH(text)),0) c "
            "FROM content_block WHERE item_uid=? GROUP BY kind", (uid,)):
        k, n, c = r["kind"], r["n"], r["c"]
        ev["n_blocks"] += n
        if k == "subtitle":
            ev["n_subtitle"] = n
            ev["sub_chars"] = c
        elif k == "subtitle_seg":
            ev["n_seg"] = n
            ev["seg_chars"] = c

            if ev["n_subtitle"] == 0:
                ev["n_subtitle"] = n
                ev["sub_chars"] = c
            else:

                ev["sub_chars"] = max(ev["sub_chars"], c)
        elif k == "danmaku":
            ev["n_danmaku"] = n
            ev["danmaku_chars"] = c
        elif k == "comment":
            ev["n_comment"] = n
            ev["comment_chars"] = c

    if ev["n_danmaku"]:
        rows = sample_blocks(con, uid, "danmaku", DANMAKU_SAMPLE,
                             columns="text")
        ev["danmaku_text"] = " ".join((r["text"] or "") for r in rows)

    if ev["n_comment"]:
        rows = con.execute(
            "SELECT text, weight FROM content_block "
            "WHERE item_uid=? AND kind='comment' "
            "ORDER BY COALESCE(weight,0) DESC LIMIT ?",
            (uid, COMMENT_SAMPLE)).fetchall()
        ev["comment_text"] = " ".join((r["text"] or "") for r in rows)
        ev["top_comment_likes"] = int(rows[0]["weight"] or 0) if rows else 0

    return ev

def merge_hint(ev: dict, kind_hint: str = None) -> dict:
    if kind_hint:
        ev = dict(ev)
        ev["kind_hint"] = kind_hint
    return ev

def signal_hits(text: str, words: list) -> dict:
    if not text:
        return {}
    out = {}
    for w in words:
        n = text.count(w)
        if n:
            out[w] = n
    return out

if __name__ == "__main__":
    import sqlite3 as _s
    from bili_digest.paths import DB
    con = _s.connect(str(DB))
    con.row_factory = _s.Row
    for bv in ("BVxxxxxxxxxx", "BVxxxxxxxxxx", "BVxxxxxxxxxx"):
        r = con.execute("SELECT uid FROM item WHERE item_id=?", (bv,)).fetchone()
        if not r:
            continue
        ev = gather(con, r["uid"])
        print(f"  {bv}  {ev['title'][:34]}")
        print(f"    tags={ev['tags']}")
        print(f"    字幕 {ev['n_subtitle']} 条/{ev['sub_chars']} 字 · "
              f"弹幕 {ev['n_danmaku']} · 评论 {ev['n_comment']} · "
              f"时长 {ev['duration']}s")
        print(f"    弹幕样本: {ev['danmaku_text'][:80]}")
        print(f"    评论样本: {ev['comment_text'][:80]}")
        print()
    con.close()
