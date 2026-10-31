#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys

from bili_digest import paths
from bili_digest.paths import DB

_STOP = {
    "问题", "回答", "答案", "视频", "教程", "内容", "方法", "介绍", "分享",
    "什么", "怎么", "如何", "可以", "需要", "一个", "这个", "那个", "自己",
    "我们", "他们", "大家", "东西", "情况", "时候", "地方", "部分", "方面",
    "使用", "进行", "通过", "以及", "而且", "但是", "因为", "所以", "如果",
    "检索", "搜索", "查询",
}

def _tokens(text: str) -> set:
    try:
        from bili_digest.schema.core_schema import has_jieba, _tokenizer
        if has_jieba():
            ws = _tokenizer().lcut(text or "")
        else:
            ws = list(text or "")
    except Exception:
        ws = list(text or "")

    return {w for w in ws if len(w) >= 2 and w.strip()}

def open_questions(con) -> list:
    try:
        return list(con.execute(
            "SELECT id, question FROM open_question WHERE status='open' ORDER BY id"))
    except Exception:
        return []

def match_content(con, item_uids: list, min_overlap: int = 2) -> list:
    qs = open_questions(con)
    if not qs or not item_uids:
        return []
    qtok = [(r[0], r[1], _tokens(r[1])) for r in qs]

    generic = set(_STOP)
    try:
        generic |= {r[0] for r in con.execute(
            "SELECT term FROM keyword WHERE df > 100")}
    except Exception:
        pass

    def _content_tokens(text: str) -> set:
        return {w for w in _tokens(text) if w not in generic}

    out = []
    ph = ",".join("?" * len(item_uids))

    rows = con.execute(
        "SELECT i.uid, i.title, "
        "  (SELECT content FROM derived d WHERE d.item_uid=i.uid AND d.kind='summary') s "
        "FROM item i WHERE i.uid IN (%s)" % ph, item_uids).fetchall()
    for uid, title, summary in rows:
        toks = _content_tokens(title or "")
        toks |= _content_tokens(summary or "")
        if not toks:
            continue
        for qid, q, qs_tok in qtok:

            hit = (_content_tokens(q) if qs_tok else set()) & toks
            if len(hit) >= min_overlap:
                out.append({"qid": qid, "question": q, "item_uid": uid,
                            "title": title or "", "overlap": sorted(hit)[:6]})
    out.sort(key=lambda x: -len(x["overlap"]))
    return out

def render(matches: list, limit: int = 5) -> list:
    if not matches:
        return []
    L = ["", "  ── 与你登记的问题相关（%d 条）──" % len(matches)]
    seen = set()
    for m in matches:
        if m["qid"] in seen:
            continue
        seen.add(m["qid"])
        L.append("    #%d %s" % (m["qid"], m["question"][:52]))
        same = [x for x in matches if x["qid"] == m["qid"]][:3]
        for x in same:
            L.append("       · 《%s》  命中：%s"
                     % (x["title"][:36], "、".join(x["overlap"][:4])))
        if len(seen) >= limit:
            break
    return L

def _content_tokens(con, text: str) -> set:
    generic = set(_STOP)
    try:
        generic |= {r[0] for r in con.execute(
            "SELECT term FROM keyword WHERE df > 100")}
    except Exception:
        pass
    return {w for w in _tokens(text) if w not in generic}

def suggest_from_log(con, min_times: int = 3, limit: int = 10) -> list:
    try:
        rows = con.execute(
            "SELECT question, COUNT(*) c FROM query_log "
            "WHERE question IS NOT NULL AND LENGTH(TRIM(question)) >= 2 "
            "GROUP BY question HAVING c >= ? ORDER BY c DESC LIMIT ?",
            (min_times, limit)).fetchall()
    except Exception:
        return []
    existing = set()
    try:
        existing = {r[0] for r in con.execute(
            "SELECT question FROM open_question")}
    except Exception:
        pass

    out = []
    for q, c in rows:
        core = _content_tokens(con, q)
        if not core:
            continue
        out.append({"q": q, "n": c, "in_pool": q in existing})
    return out

def render_suggest(items: list) -> list:
    if not items:
        return ["  没有反复出现的问题（阈值 3 次）"]
    L = ["", "  ── 你反复在问的（来自 query_log）─────────", ""]
    for x in items:
        mark = "（已在问题池）" if x["in_pool"] else ""
        L.append("  %2d 次  %s%s" % (x["n"], x["q"][:50], mark))
    L.append("")
    L.append("  把这些登记成长期问题，抓取就会按它们排优先级：")
    L.append("    kb question --add \"…\"")
    return L

def main() -> int:
    import argparse
    import sqlite3
    paths.enable_utf8_stdout()
    ap = argparse.ArgumentParser(description="新内容 × 长期问题")
    ap.add_argument("--uids", nargs="*", help="条目 uid；不给则扫最近 50 条")
    a = ap.parse_args()
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    uids = a.uids or [r[0] for r in con.execute(
        "SELECT uid FROM item ORDER BY saved_at DESC LIMIT 50")]
    m = match_content(con, uids)
    for ln in render(m):
        print(ln)
    if not m:
        print("  （没有与问题池相关的新内容；先 kb question --add 登记几个长期问题）")
    return 0

if __name__ == "__main__":
    sys.exit(main())
