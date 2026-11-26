#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import re

LEN_MIN = 12
LEN_PLAIN = 15
_Q = re.compile(r"[?？]")
_ASK = re.compile(r"怎么|为什么|为啥|如何|哪个|哪里|什么|多少|是不是|有没有|吗")
_PUNCT = re.compile(r"[，。；！,;!]")

_CACHE = {}

def is_quote_like(q: str) -> bool:
    s = (q or "").strip()
    if len(s) < LEN_MIN:
        return False
    if _Q.search(s) or _ASK.search(s):
        return False
    if _PUNCT.search(s):
        return True

    return len(s) >= LEN_PLAIN and s.count(" ") <= 1

def index(con) -> dict:
    try:
        st = con.execute("SELECT COUNT(*), COALESCE(MAX(updated_at),0) FROM item").fetchone()
        key = (id(con), int(st[0]), int(st[1] or 0))
    except Exception:
        key = None
    if key is not None and key in _CACHE:
        return _CACHE[key]
    from bili_digest.quality import fix_cites as fc
    out = {}
    for r in con.execute("SELECT item_uid,content FROM derived "
                         "WHERE kind IN ('key_points','insights')"):
        try:
            arr = json.loads(r[1])
        except Exception:
            continue
        blocks = {b["id"]: (b["text"] or "") for b in con.execute(
            "SELECT id,text FROM content_block WHERE item_uid=?", (r[0],))}
        for it in arr:
            if not isinstance(it, dict):
                continue
            for c in (it.get("cites") or []):
                u, cid = fc._quote_of(c), fc._id_of(c)
                t = blocks.get(cid)
                if u and len(u) >= LEN_MIN and t is not None and u in t:
                    out.setdefault(u.strip(), cid)
    if key is not None:
        _CACHE.clear()
        _CACHE[key] = out
    return out

def lookup(con, query: str):
    if not is_quote_like(query):
        return None
    q = query.strip()
    idx = index(con)
    for quote, bid in idx.items():
        if quote in q or (len(q) >= LEN_MIN and q in quote):
            return bid
    return None

def apply(con, hits: list, query: str, bonus: float) -> list:
    if not hits or not bonus:
        return hits
    tgt = lookup(con, query)
    if tgt is None:
        return hits
    out = []
    for h in hits:
        h2 = dict(h)
        h2["_score"] = (h.get("score") or 0) + (bonus if h.get("block_id") == tgt else 0.0)
        out.append(h2)
    out.sort(key=lambda x: -(x.get("_score") or 0))
    for h in out:
        h.pop("_score", None)
    return out
