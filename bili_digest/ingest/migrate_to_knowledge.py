#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os as _os
import sys as _sys

from bili_digest import paths

import hashlib
import sqlite3
import sys
from pathlib import Path
from bili_digest.paths import DB as NEW_DB, DIGEST_DIR as DIGEST

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).parent

from bili_digest.schema.core_schema import (KIND_COMMENT, KIND_DANMAKU, KIND_META, KIND_SUBTITLE,
                         add_block, add_tag, connect, make_uid, upsert_item)

OLD_DB = paths.HERE / "index.db"

SOURCE_MAP = {
    "danmaku": KIND_DANMAKU,
    "reply": KIND_COMMENT,
    "subtitle": KIND_SUBTITLE,
    "meta": KIND_META,
}

def _int(v, default=None):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default

def load_item_meta(bvid: str) -> dict:
    raw = DIGEST / bvid / "raw.sqlite"
    if not raw.exists():
        return {}
    c = sqlite3.connect(str(raw))
    try:
        md = {r[0]: r[1] for r in c.execute("SELECT k,v FROM meta")}
    except sqlite3.OperationalError:
        md = {}
    c.close()
    return md

def migrate(verbose=True) -> dict:
    if not OLD_DB.exists():
        raise SystemExit(f"找不到旧库 {OLD_DB}")

    old = sqlite3.connect(str(OLD_DB))
    old.row_factory = sqlite3.Row
    new = connect(NEW_DB)

    rows = old.execute("SELECT id,bvid,source,meta,extra,text,hash FROM chunks").fetchall()
    by_bvid = {}
    for r in rows:
        by_bvid.setdefault(r["bvid"], []).append(r)

    stat = {"items": 0, "blocks": 0, "skipped": 0, "by_kind": {}}

    for bvid, bl in sorted(by_bvid.items()):
        md = load_item_meta(bvid)
        uid = upsert_item(
            new,
            platform="bilibili",
            item_id=bvid,
            title=md.get("title"),
            url=f"https://www.bilibili.com/video/{bvid}",
            author_uid=md.get("owner_mid"),
            author_name=md.get("owner"),
            published_at=_int(md.get("pubdate")),
            duration_sec=_int(md.get("duration")),
            language="zh",
            extras={
                "aid": _int(md.get("aid")), "cid": _int(md.get("cid")),
                "tname": md.get("tname"), "desc": md.get("desc"),
                "view": _int(md.get("view")), "like": _int(md.get("like")),
                "reply_count": _int(md.get("reply")),
                "danmaku_count": _int(md.get("danmaku")),
                "legacy_id": True,
            })
        stat["items"] += 1

        if md.get("tags"):
            for t in str(md["tags"]).split(","):
                if t.strip():
                    add_tag(new, uid, t.strip(), kind="system", source="bilibili")

        for r in bl:
            kind = SOURCE_MAP.get(r["source"])
            if not kind:
                continue
            offset = None
            if kind in (KIND_DANMAKU, KIND_SUBTITLE):
                offset = r["meta"]
            bid = add_block(new, uid, kind, r["text"] or "",
                            offset_sec=offset,
                            author=r["extra"] if kind == KIND_COMMENT else None,
                            weight=r["meta"] if kind == KIND_COMMENT else None,
                            index_fts=False)
            if bid:
                stat["blocks"] += 1
                stat["by_kind"][kind] = stat["by_kind"].get(kind, 0) + 1
            else:
                stat["skipped"] += 1

    new.commit()
    if verbose:
        print(f"  迁移完成：{stat['items']} 条目，{stat['blocks']} 内容块"
              f"（跳过重复 {stat['skipped']}）")
        for k, v in sorted(stat["by_kind"].items(), key=lambda x: -x[1]):
            print(f"    {k:<10} {v}")

    old.close()
    new.close()
    return stat

if __name__ == "__main__":
    print("=== 迁移 index.db → knowledge.db ===")
    migrate()
