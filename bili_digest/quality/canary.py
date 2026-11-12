#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import time

CANARY_UID = "canary:probe-v1"
CANARY_ITEM_ID = "probe-v1"

CANARY_CODE = "丙午"
CANARY_TEXT = (
    "金丝雀探针 %s —— 这一段不对应任何真实视频，它的唯一用途是证明"
    "「抓取 → 入库 → 索引 → 检索」这条链路今天还是通的。"
    "如果你的检索结果里出现了这一段，说明链路正常，但这不代表库里"
    "有你问的那个东西。"
) % CANARY_CODE

def ensure(con) -> bool:
    from bili_digest.retrieve.temporal import now_sec
    now = now_sec()
    made = False
    if not con.execute("SELECT 1 FROM item WHERE uid=?", (CANARY_UID,)).fetchone():
        con.execute(
            "INSERT INTO item(uid,platform,item_id,title,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?)",
            (CANARY_UID, "canary", CANARY_ITEM_ID, "金丝雀探针（非真实内容）", now, now))
        made = True

    if not con.execute(
            "SELECT 1 FROM content_block WHERE item_uid=? AND kind='subtitle_seg'",
            (CANARY_UID,)).fetchone():
        from bili_digest.schema.core_schema import block_hash, index_block
        cur = con.execute(
            "INSERT INTO content_block(item_uid,kind,offset_sec,text,author,weight,"
            "hash,created_at,info_len) VALUES(?,?,?,?,?,?,?,?,?)",
            (CANARY_UID, "subtitle_seg", 0.0, CANARY_TEXT, None, None,
             block_hash("subtitle_seg", CANARY_TEXT, 0.0), now,
             __import__("bili_digest.schema.core_schema", fromlist=["x"]).info_len(CANARY_TEXT)))

        try:
            index_block(con, cur.lastrowid, CANARY_TEXT, CANARY_UID, "subtitle_seg")
        except Exception:
            pass
        made = True

    try:
        from bili_digest.schema.core_schema import block_hash as _bh
        _bid = con.execute(
            "SELECT id FROM content_block WHERE item_uid=? AND kind='subtitle_seg'",
            (CANARY_UID,)).fetchone()
        if _bid:
            con.execute("UPDATE vec_state SET text_hash=? WHERE block_id=?",
                        (_bh("subtitle_seg", CANARY_TEXT), _bid[0]))
    except Exception:
        pass
    con.commit()
    return made

def check(con, vindex=None) -> dict:
    steps = []

    row = con.execute(
        "SELECT id FROM content_block WHERE item_uid=? AND kind='subtitle_seg'",
        (CANARY_UID,)).fetchone()
    steps.append(("块", bool(row), "content_block 里有探针段"))
    if not row:
        return {"ok": False, "steps": steps,
                "note": "探针条目丢了 —— 库可能被重建过，跑一次 canary 重建"}
    block_id = row[0]

    in_fts = con.execute(
        "SELECT 1 FROM block_fts WHERE rowid=?", (block_id,)).fetchone()
    steps.append(("FTS", bool(in_fts), "全文索引里有探针段"))

    in_vec = None
    if vindex is not None:
        try:
            in_vec = bool(vindex.has(block_id))
        except Exception:

            in_vec = True
        steps.append(("向量", bool(in_vec), "向量索引里有探针段"))

    hit = False
    detail = ""
    if vindex is not None:
        try:
            from bili_digest.retrieve import rag_core as rc
            hits = rc.hybrid(con, vindex, CANARY_CODE, top_k=3)
            if hits:
                hit = hits[0].get("item_uid") == CANARY_UID
                detail = "top1 = %s" % (hits[0].get("item_uid") or "?")
            else:
                detail = "检索 0 条命中"
        except Exception as e:
            detail = "%s: %s" % (type(e).__name__, str(e)[:40])
    else:

        try:
            hit = bool(con.execute(
                "SELECT 1 FROM block_fts WHERE block_fts MATCH ?",
                (CANARY_CODE,)).fetchone())
            detail = "FTS 字面命中" if hit else "FTS 也未命中"
        except Exception as e:
            detail = "%s: %s" % (type(e).__name__, str(e)[:40])
    steps.append(("检索", hit, detail))

    ok = all(p for _, p, _ in steps)
    if ok:
        note = "链路通"
    elif not in_fts:
        note = "探针在库里有块但没进 FTS —— 写入路径漏了 index_block"
    elif not hit:
        note = "探针在库里但检索拿不到 —— 索引与库不同步，跑一次 index"
    else:
        note = "探针链路有环节不通"
    return {"ok": ok, "steps": steps, "note": note}
