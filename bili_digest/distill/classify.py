#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os as _os
import sys as _sys

from bili_digest import paths

import json
import re
import sys
from dataclasses import dataclass, field

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

TAGS = {
    "knowledge": [

        "教程", "学习", "入门", "讲解", "科普", "教学", "干货", "知识",
        "分析", "课程", "公开课", "doc", "guide", "tutorial", "lecture",

        "科技", "编程", "开发", "逆向", "安全", "算法", "架构", "源码",
        "数学", "物理", "化学", "生物", "医学", "法律", "历史", "哲学",
        "经济", "金融", "财经", "考研", "考公", "心理", "统计", "工程",
        "航空", "航天", "电子", "电路", "机械", "材料", "光学", "语言",
        "英语", "写作", "论文", "研究", "实验", "理论", "原理", "机制",
        "设计", "建模", "渲染", "剪辑技巧", "摄影技巧", "音乐理论",
    ],
    "tool": [
        "软件", "工具", "资源", "插件", "开源", "免费", "下载", "神器",
        "app", "tool", "software", "utility", "share", "替代",
        "跨平台", "自部署", "脚本", "自动化", "插件", "拓展",
    ],
    "music": [
        "音乐", "歌曲", "翻唱", "原创音乐", "mv", "bgm", "纯音乐",
        "演奏", "钢琴", "吉他", "乐队", "说唱", "rap", "翻调", "编曲",
        "作曲", "唱见", "vocaloid", "初音", "音mad", "电音", "伴奏",
        "歌回", "填词", "同人曲",
    ],
    "entertainment": [

        "搞笑", "沙雕", "玩梗", "鬼畜", "剧情", "短剧", "整活",
        "娱乐", "趣事", "段子", "综艺", "魔性", "迷惑行为", "名场面",
        "切片", "reaction", "吐槽",
    ],
    "vlog": [
        "日常", "vlog", "生活", "旅行", "美食", "探店", "开箱", "记录",
    ],
}

PLATFORM_ONLY_TAGS = [
    "单机游戏", "沙盒游戏", "网络游戏", "手机游戏", "电子竞技",
    "我的世界", "minecraft", "游戏实况", "游戏解说",
    "动画", "番剧", "影视", "纪录片", "娱乐区", "生活区", "科技区",
]

TITLE_PATTERNS = {
    "music": [

        r"(纯音乐|伴奏|翻唱|弹唱|演奏版|歌回|填词翻唱)",
        r"\b(cover|remix|official audio|lyric video|music video)\b",

        r"[《【\[](.*?)[》】\]]\s*(歌曲|翻唱|演奏|cover|mv)",
        r"(歌名|歌词|歌曲)\s*[:：]",

    ],
    "entertainment": [
        r"(笑死|沙雕|离谱|整活|炸房|鬼畜)",
        r"(第\d+集|完结|番剧)",
    ],
}

@dataclass
class Verdict:
    content_kinds: list
    extractable: bool | None
    retain: bool
    reason: str
    evidence_used: list = field(default_factory=list)

    def __str__(self):
        k = "+".join(self.content_kinds) or "?"
        if self.extractable is None:
            e = "待判"
        else:
            e = "可提炼" if self.extractable else "无可提炼素材"
        r = "保留" if self.retain else "不保留"
        return f"{k} → {e}（{r}；{self.reason}）"

    @property
    def worth_digesting(self):
        return self.extractable is not False

RETAIN_ALWAYS = True

SIGNALS = {
    "knowledge": [
        "学到了", "讲得好", "讲的真好", "讲得真好", "老师", "笔记", "懂了",
        "复习", "干货", "讲解", "细", "详细", "怎么", "如何", "为什么",
        "原理", "教程", "下一课", "进阶", "求教程", "up主能不能讲",
    ],
    "tool": [
        "好用", "下载", "装好", "配置", "安装", "在哪下", "求地址",
        "有链接吗", "白嫖", "已装", "安卓能装吗", "win能用吗",
    ],
    "music": [
        "求bgm", "什么歌", "歌名", "好听", "循环", "歌手", "翻唱",
        "单曲", "原曲", "什么曲子", "音源", "谁的歌",
    ],
    "entertainment": [
        "哈哈", "笑死", "笑", "草", "绝了", "鬼畜", "前方高能",
        "名场面", "太魔性", "乐死", "绷不住",
    ],
}

MIN_EVIDENCE_CHARS = 60
MIN_CHARS_FOR_DIGEST = 200

def norm_tags(tags) -> list:
    if not tags:
        return []
    if isinstance(tags, str):
        s = tags.strip()
        if not s:
            return []
        if s[:1] in "[{":
            try:
                v = json.loads(s)
            except (ValueError, TypeError):
                v = None
            if isinstance(v, list):
                return norm_tags(v)
            if isinstance(v, str):
                return norm_tags(v)

        parts = re.split(r"[,，、|]", s)
        out = []
        for p in parts:
            p = p.strip()
            if p and p not in out:
                out.append(p)
        return out
    if isinstance(tags, (list, tuple, set)):
        out = []
        for t in tags:
            if isinstance(t, dict):

                t = t.get("tag_name") or t.get("name") or t.get("tag") or ""
            t = str(t).strip()
            if t and t not in out:
                out.append(t)
        return out
    return [str(tags).strip()]

def _norm_tags(tags) -> str:
    return " ".join(norm_tags(tags)).lower()

def classify(title: str = "", tags=None, sub_chars: int = 0,
             duration: int = 0, n_blocks: int = 0, kind_hint: str = None,
             danmaku_text: str = "", comment_text: str = "",
             n_danmaku: int = 0, n_comment: int = 0) -> Verdict:
    title = title or ""
    tags_list = norm_tags(tags)
    tg = " ".join(tags_list).lower()
    used = []

    tag_scores = {}
    for kind, words in TAGS.items():
        n = sum(1 for w in words if w in tg)
        if n:
            tag_scores[kind] = n
            used.append(f"tags:{kind}×{n}")

    title_kinds = set()
    for kind, pats in TITLE_PATTERNS.items():
        for p in pats:
            if re.search(p, title, re.I):
                title_kinds.add(kind)
                used.append(f"标题:{kind}")
                break

    sig_scores = {}
    for kind, words in SIGNALS.items():
        n = 0
        for w in words:
            n += danmaku_text.lower().count(w)
            n += comment_text.lower().count(w)
        if n:
            sig_scores[kind] = n
            used.append(f"弹幕/评论:{kind}×{n}")

    if danmaku_text and n_danmaku >= 20:
        hh = danmaku_text.count("哈")
        if hh >= 10:
            sig_scores["entertainment"] = sig_scores.get("entertainment", 0) + min(
                hh // 10, 5)
            used.append(f"弹幕:哈×{hh}")

    kinds = set(tag_scores) | title_kinds
    for kind, n in sig_scores.items():
        if n >= (3 if kind == "entertainment" else 2):
            kinds.add(kind)
    if kind_hint in TAGS:
        kinds.add(kind_hint)
        used.append(f"上游指定:{kind_hint}")
    if not kinds:
        kinds.add("unknown")

    dm_chars = len(danmaku_text or "")
    cm_chars = len(comment_text or "")
    has_material = (sub_chars >= MIN_CHARS_FOR_DIGEST) or \
                   (dm_chars + cm_chars >= MIN_EVIDENCE_CHARS) or \
                   (n_blocks >= 10 and (n_danmaku + n_comment) >= 5)
    fetched = (n_blocks > 0) or (sub_chars > 0)

    if not fetched:

        return Verdict(sorted(kinds), None, RETAIN_ALWAYS,
                       "尚未抓取，素材量未知", used)

    if not has_material:
        return Verdict(sorted(kinds), False, RETAIN_ALWAYS,
                       "已抓取但素材量不足（字幕少且无弹幕评论）—— 但保留", used)

    return Verdict(sorted(kinds), True, RETAIN_ALWAYS,
                   "素材充足", used)

def classify_item(con, uid: str) -> Verdict:
    from bili_digest.distill import evidence as ev_mod
    ev = ev_mod.gather(con, uid)
    return classify(
        title=ev["title"], tags=ev["tags"], sub_chars=ev["sub_chars"],
        duration=ev["duration"], n_blocks=ev["n_blocks"],
        danmaku_text=ev["danmaku_text"], comment_text=ev["comment_text"],
        n_danmaku=ev["n_danmaku"], n_comment=ev["n_comment"])

def extractable(con, uid: str) -> bool:
    v = classify_item(con, uid)
    return v.extractable is not False

def worth_digesting(title: str = "", tags=None, sub_chars: int = 0,
                    duration: int = 0, n_blocks: int = 0,
                    kind_hint: str = None, **kw) -> bool:
    return classify(title, tags, sub_chars, duration, n_blocks,
                    kind_hint, **kw).extractable is not False

def all_types() -> list:
    return sorted(TAGS)

if __name__ == "__main__":
    cases = [
        ("某 Android Frida Hook 入门教程", "科技猎手,学习,frida教程", 4000, 2668),
        ("《起风了》翻唱", "音乐,翻唱", 0, 240),
        ("某音MAD作品", "音乐,鬼畜", 40, 180),
        ("某个 30 秒的短视频", "", 0, 30),
        ("无名视频但有大量字幕", "", 3000, 900),
        ("某玩梗视频加强版", "搞笑,玩梗", 0, 120),
        ("一站式无线充电桌垫终极PLUS", "数码,分享", 400, 300),
    ]
    print(f"  {'标题':<34}{'类型':<16}{'分数':<8}理由")
    print("  " + "-" * 96)
    for title, tags, sc, dur in cases:
        v = classify(title, tags, sc, dur)
        print(f"  {title[:32]:<34}{v.content_kind:<16}"
              f"{'提炼' if v.worth_digesting else '跳过':<8}{v.reason[:44]}")
