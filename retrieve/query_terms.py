#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
import paths

import math
import re
import sys
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).parent

QUERY_STOP = set("""
收藏 视频 观众 问题 内容 评论 弹幕 字幕 这个 那个 这些 那些 我的 我 你
我们 你们 他们 她们 咱们 大家 自己 别人 的话 来说 之类 等等 一些 一点
关于 哪些 什么 哪个 怎么 为什么 讲了 说到 提到 提了 说了 有过 都有 在讲
里 中 的 了 是 在 和 与 或 都 有 说 看 找 想 要 能 会 可以 应该
帮我 给我 告诉 一下 一些 一点 大概 主要 整体 情况 方面 部分
东西 事儿 事情 玩意 那个 这个 吧 啊 呀 呢 嘛 哈 哦
话说 其实 就是 然后 所以 但是 因为 不过 反正 好像 可能
""".split())

def tokenize(query: str) -> list:
    import jieba
    out = []
    for w in jieba.cut(query or ""):
        w = w.strip().lower()
        if len(w) < 2:
            continue
        if re.fullmatch(r"[\d\W_]+", w):
            continue
        out.append(w)
    return out

def query_content_terms(con, query: str) -> dict:
    terms = [t for t in tokenize(query) if t not in QUERY_STOP]
    if not terms:
        return {}
    n = con.execute("SELECT COUNT(*) n FROM content_block").fetchone()["n"] or 1
    out = {}
    for t in terms:
        r = con.execute("SELECT df FROM keyword WHERE term=?", (t,)).fetchone()
        df = (r["df"] if r else 1) or 1
        out[t] = math.log((n + 1) / (df + 1))
    return out

def strip_generic(con, query: str, min_idf_ratio: float = 0.5) -> str:
    terms = tokenize(query)
    if not terms:
        return query
    weights = query_content_terms(con, query)
    content = [t for t in terms if t in weights]
    if not content:
        return query
    top = max(weights[t] for t in content)
    kept = [t for t in content if weights[t] >= top * min_idf_ratio]
    if not kept:
        return query
    return " ".join(kept)

_strip_generic = strip_generic
