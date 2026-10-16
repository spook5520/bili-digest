#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
import paths

import sqlite3
import sys
from pathlib import Path
from paths import DB as KB, SHELF_DB as SHELF, PLATFORM

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).parent

from core_schema import connect, make_uid, now_sec, upsert_item

def import_collections(verbose=True) -> dict:
    if not SHELF.exists():
        raise SystemExit(f"找不到 {SHELF}，先跑 bili_shelf.py sync")
    sh = sqlite3.connect(str(SHELF))
    sh.row_factory = sqlite3.Row
    kb = connect(KB)

    stat = {"folders": 0, "videos": 0, "members": 0}

    for f in sh.execute("SELECT id,title,media_count,attr,fav_time,synced_at FROM folder"):
        cid = make_uid(PLATFORM, str(f["id"]))
        kb.execute("""
            INSERT INTO collection(id,platform,name,total,extras,synced_at)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name, total=excluded.total,
                extras=excluded.extras, synced_at=excluded.synced_at
        """, (cid, PLATFORM, f["title"], f["media_count"],
              f'{{"attr":{f["attr"] or 0},"fav_time":{f["fav_time"] or 0}}}',
              f["synced_at"]))
        stat["folders"] += 1

    vids = {r["bvid"]: r for r in sh.execute(
        "SELECT bvid,title,intro,upper_mid,upper,fav_time,pubtime,duration,"
        "play,danmaku,cover,attr,type FROM video")}

    for m in sh.execute("SELECT folder_id,bvid,fav_time FROM membership"):
        v = vids.get(m["bvid"])
        if not v:
            continue
        uid = upsert_item(
            kb, platform=PLATFORM, item_id=m["bvid"],
            title=v["title"],
            url=f"https://www.bilibili.com/video/{m['bvid']}",
            author_uid=str(v["upper_mid"]) if v["upper_mid"] else None,
            author_name=v["upper"],
            published_at=v["pubtime"],
            saved_at=v["fav_time"],
            duration_sec=v["duration"],
            cover=v["cover"],
            extras={"intro": v["intro"], "play": v["play"],
                    "danmaku_count": v["danmaku"], "legacy_id": True})
        stat["videos"] += 1

        cid = make_uid(PLATFORM, str(m["folder_id"]))
        kb.execute("""
            INSERT INTO collection_member(collection_id,item_uid,added_at)
            VALUES(?,?,?)
            ON CONFLICT(collection_id,item_uid) DO UPDATE SET
                added_at=excluded.added_at
        """, (cid, uid, m["fav_time"]))
        stat["members"] += 1

    kb.commit()
    if verbose:
        print(f"  导入收藏夹 {stat['folders']} 个，视频 {stat['videos']} 个，"
              f"收藏关系 {stat['members']} 条")
        n_mem = kb.execute("SELECT COUNT(*) FROM collection_member").fetchone()[0]
        n_item = kb.execute("SELECT COUNT(*) FROM item").fetchone()[0]
        print(f"  knowledge.db 现状：item {n_item}，collection_member {n_mem}")

    sh.close()
    kb.close()
    return stat

if __name__ == "__main__":
    print("=== shelf.db → knowledge.db ===")
    import_collections()
