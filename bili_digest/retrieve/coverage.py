#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import re

CONTENT_KINDS = ("subtitle_seg", "subtitle", "summary",
                 "key_points", "insights", "seg_summary")

_STOP = set("的 了 吗 呢 吧 啊 是 我 你 他 她 它 们 这 那 哪 个 和 与 在 有 不 也 就 都 很 还 "
            "怎么 怎样 如何 什么 为什 应该 一个 一些 可以 需要 哪些 这个 那个 就是 然后 所以 "
            "的话 一下 我想 帮我 告诉 知道 觉得 是否 有没有".split())

def query_terms(q: str) -> list[str]:
    from bili_digest.retrieve.query_terms import tokenize as _tok
    return [w for w in _tok(q or "")
            if w not in _STOP and len(w) >= 2]

def coverage(con, question: str, hits: list) -> float | None:
    ws = query_terms(question)
    if len(ws) < 2:
        return None

    top = None
    for h in (hits or []):
        if h.get("kind") in CONTENT_KINDS:
            top = h
            break
    if top is None:
        return 0.0

    hay = ((top.get("text") or "") + " " + (top.get("title") or "")).lower()
    hit = sum(1 for w in ws if w.lower() in hay)
    return hit / len(ws)

def has_content(con, question: str, hits: list) -> dict:
    ws = query_terms(question)
    base = {"terms": ws, "cover": None, "hit": 0}

    if not ws:
        return {**base, "verdict": "no", "why": "查询里没有实义词"}

    top = None
    for h in (hits or []):
        if h.get("kind") in CONTENT_KINDS:
            top = h
            break
    if top is None:
        return {**base, "verdict": "no", "why": "没有内容块命中（只有评论/弹幕）"}

    hay = ((top.get("text") or "") + " " + (top.get("title") or "")).lower()
    hit = sum(1 for w in ws if w.lower() in hay)
    cover = hit / len(ws)
    ok = (hit >= 1) if len(ws) == 1 else (hit >= 2 and cover >= COVER_MIN)

    if ok:
        why = "top1 里命中 %d/%d 个实义词" % (hit, len(ws))
    elif hit:
        why = "只命中 %d/%d（%s），不承载主题" % (hit, len(ws), "、".join(ws))
    else:
        why = "top1 里一个实义词都没有"
    return {"terms": ws, "cover": cover, "hit": hit,
            "verdict": "yes" if ok else "no", "why": why}

COVER_MIN = 0.5

def verdict(cover: float | None) -> str:
    if cover is None:
        return "词太少，判不了（直接看结果）"
    if cover >= COVER_MIN:
        return "库里有相关内容"
    if cover > 0:
        return "库里只有零散提及，没有系统讲过的"
    return "库里没有"

_ANALYTICAL = (
    "观众怎么看", "观众怎么评价", "大家怎么看", "大家觉得", "怎么评价",
    "口碑", "争议", "赞成", "反对", "支持的人", "反对的人",
    "评论区", "弹幕怎么说", "统计口径", "总体看法", "大部分人",
)
_HOW = ("怎么", "如何", "怎样", "步骤", "教程", "入门", "上手", "配置", "安装", "搭建", "实现", "部署")
_WHAT = ("是什么", "什么是", "介绍", "概念", "原理", "为什么")
_WHICH = ("哪个好", "对比", "区别", "取舍", "选型", "还是")

HOW_KINDS = ("seg_summary", "subtitle_seg", "subtitle")

def intent(q: str) -> str:
    s = q or ""

    if any(w in s for w in _ANALYTICAL):
        return "analytical"
    if any(w in s for w in _HOW):
        return "how"
    if any(w in s for w in _WHICH):
        return "which"
    if any(w in s for w in _WHAT):
        return "what"
    return "other"

_DEICTIC = ("这个", "那个", "它", "该", "此", "上述", "刚才", "前面", "上面",
            "这期", "那期", "这条", "那条")

def is_deictic(q: str, has_context: bool = False) -> bool:
    if has_context:
        return False
    s = (q or "").strip()
    if len(s) > 14:
        return False
    return any(w in s for w in _DEICTIC)

_AGGREGATE_Q = ("都讲了什么", "都说了什么", "有没有矛盾", "有什么不同", "有什么差异",
                "整体如何", "整体怎么样", "综述", "总结一下", "汇总",
                "哪几个讲了", "哪些讲了", "都提到")

_BOILER = ("我收藏里", "我收藏的", "我的收藏", "收藏里", "关于",
           "这几期", "那几期", "这几个", "这几个视频", "这几条",
           "都讲了什么", "都说了什么",
           "有没有矛盾", "有什么不同", "有什么差异", "整体如何", "整体怎么样",
           "总结一下", "汇总", "哪几个讲了", "哪些讲了", "都提到", "讲了些", "什么",
           "吗", "？", "?", "的", "了", "和", "与")

def extract_topic(q: str) -> str:
    s = (q or "").strip()
    for w in sorted(_BOILER, key=len, reverse=True):
        s = s.replace(w, " ")
    s = " ".join(s.split())
    return s

def kinds_for(q: str) -> tuple | None:
    return HOW_KINDS if intent(q) == "how" else None
