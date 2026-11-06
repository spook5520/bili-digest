#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

STEP_KINDS = ("subtitle_seg", "seg_summary")

META_KINDS = ("meta",)

CHAT_KINDS = ("comment", "danmaku", "audience", "annotation")

STEP_MIN_ITEMS = 2

THIN_BLOCKS = 30

SUGGEST = {
    "缺步骤": "{topic} 教程 实战 完整",
    "缺内容": "{topic} 原理 讲解",
    "缺对照": "{topic} 对比 区别 选型",
    "太薄": "{topic} 系统 课程 全集",
}

def analyse(con, question: str, hits: list, intent: str,
            hc_no: bool = False, hc_why: str = "") -> dict:
    by_uid = {}
    for h in hits or []:
        u = h.get("item_uid")
        if u:
            by_uid.setdefault(u, []).append(h)

    step_uids = {u for u, hs in by_uid.items()
                 if any(h.get("kind") in STEP_KINDS for h in hs)}
    chat_only = [u for u, hs in by_uid.items()
                 if all(h.get("kind") in CHAT_KINDS + META_KINDS for h in hs)]

    blocks_of = {}
    for u in by_uid:
        try:
            blocks_of[u] = con.execute(
                "SELECT COUNT(*) FROM content_block WHERE item_uid=?", (u,)).fetchone()[0]
        except Exception:
            blocks_of[u] = 0

    gaps = []

    if intent == "how" and len(step_uids) < STEP_MIN_ITEMS:
        gaps.append({
            "type": "缺步骤",
            "why": "问的是「怎么做」，但只有 %d 个条目有步骤类内容（少于 %d 个）"
                   % (len(step_uids), STEP_MIN_ITEMS),
            "suggest": SUGGEST["缺步骤"],
        })

    if chat_only and len(chat_only) == len(by_uid) and by_uid:
        gaps.append({
            "type": "缺内容",
            "why": "命中的 %d 个条目都只有评论/弹幕/标题，没有正文 —— 那是「别人提到过」，不是「有人讲过」"
                   % len(chat_only),
            "suggest": SUGGEST["缺内容"],
        })

    if intent == "which" and len(by_uid) <= 1:
        gaps.append({
            "type": "缺对照",
            "why": "问的是「哪个好 / 有什么区别」，但库里只命中 %d 个条目，没有可对照的两方"
                   % len(by_uid),
            "suggest": SUGGEST["缺对照"],
        })

    if hc_no and not gaps:
        gaps.append({
            "type": "缺内容",
            "why": "词面覆盖率判定不承载主题（%s），而命中的内容块也以评论/标题为主"
                   % (hc_why or "实义词命中不足"),
            "suggest": SUGGEST["缺内容"],
        })

    thin = [u for u, n in blocks_of.items() if n < THIN_BLOCKS]
    if thin and len(thin) == len(by_uid) and by_uid:
        gaps.append({
            "type": "太薄",
            "why": "命中的 %d 个条目块数都低于 %d（实测正常提炼后是 100~1000 块，薄的只有 9~16 块）"
                   % (len(thin), THIN_BLOCKS),
            "suggest": SUGGEST["太薄"],
        })

    return {
        "gaps": gaps,
        "stats": {
            "items": len(by_uid),
            "with_steps": len(step_uids),
            "chat_only": len(chat_only),
            "blocks": blocks_of,
        },
    }
