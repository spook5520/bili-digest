# -*- coding: utf-8 -*-

from typing import Any, TypedDict

class ItemExtras(TypedDict, total=False):

    aid: int
    cid: int
    legacy_id: str

    intro: str

    play: int
    danmaku_count: int
    reply_count: int
    like: int
    favorite: int
    share: int

    tname: str
    tags: list

    pages: list

class BlockExtras(TypedDict, total=False):

    page: int
    page_duration: int
    sources: dict

_ITEM_ALIASES = (
    ("intro", ("intro", "desc", "description")),
    ("play", ("view", "play")),
    ("danmaku_count", ("danmaku_count", "danmaku")),
    ("reply_count", ("reply_count", "reply")),
    ("like", ("like",)),
    ("favorite", ("favorite",)),
    ("share", ("share",)),
    ("aid", ("aid",)),
    ("cid", ("cid",)),
    ("legacy_id", ("legacy_id",)),
    ("tname", ("tname",)),
    ("tags", ("tags",)),
    ("pages", ("pages",)),
)

def _load(raw: Any) -> dict:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        import json
        d = json.loads(raw)
        return d if isinstance(d, dict) else {}
    except (ValueError, TypeError):
        return {}

def item_extras(raw: Any) -> dict:
    d = _load(raw)
    out = dict(d)
    for canon, aliases in _ITEM_ALIASES:
        if out.get(canon) is not None:
            continue
        for a in aliases:
            v = d.get(a)
            if v is not None:
                out[canon] = v
                break
    return out

def block_extras(raw: Any) -> dict:
    return _load(raw)

PROV_PRIMARY = "primary"
PROV_CONTEXT = "context"
PROV_REACTION = "reaction"
PROV_DERIVED = "derived"

PROVENANCE = (PROV_PRIMARY, PROV_CONTEXT, PROV_REACTION, PROV_DERIVED)

_PROV_BY_KIND = {

    "subtitle": PROV_PRIMARY,
    "subtitle_seg": PROV_PRIMARY,
    "body": PROV_PRIMARY,

    "meta": PROV_CONTEXT,

    "comment": PROV_REACTION,
    "danmaku": PROV_REACTION,

    "summary": PROV_DERIVED,
    "key_points": PROV_DERIVED,
    "insights": PROV_DERIVED,
    "audience": PROV_DERIVED,
    "seg_summary": PROV_DERIVED,
    "annotation": PROV_DERIVED,
    "growth": PROV_DERIVED,
}

def provenance_of(kind: str) -> str:
    return _PROV_BY_KIND.get(kind or "", PROV_DERIVED)

def provenance_counts(con) -> dict:
    out = {}
    for k, n, m in con.execute(
            "SELECT kind, COUNT(*), COUNT(DISTINCT item_uid) FROM content_block "
            "GROUP BY kind"):
        p = provenance_of(k)
        cur = out.setdefault(p, [0, 0])
        cur[0] += n
        cur[1] += m
    return {k: {"blocks": v[0], "items": v[1]} for k, v in out.items()}

NO_FTS_KINDS = frozenset({"subtitle"})

def in_fts(kind: str) -> bool:
    return kind not in NO_FTS_KINDS
