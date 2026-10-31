#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys
from collections import defaultdict

from bili_digest import paths
from bili_digest.paths import DB

def _tok_any(text: str) -> set:
    try:
        from bili_digest.quality.agenda import _tokens
        return _tokens(text or "")
    except Exception:
        return set(text or "")

def gaps(con, thin_topic: int = 5, min_empty_ratio: float = 0.5) -> list:
    out = []

    try:
        total = con.execute("SELECT COUNT(*) FROM item").fetchone()[0]
        empty = con.execute(
            "SELECT COUNT(*) FROM item i WHERE NOT EXISTS("
            "  SELECT 1 FROM content_block cb WHERE cb.item_uid=i.uid)").fetchone()[0]
        if total and empty / total > 0.25:
            out.append({
                "kind": "先清积压",
                "what": "%d/%d 条还没有任何内容（%.0f%%）"
                        % (empty, total, 100.0 * empty / total),
                "why": "下面所有「该补哪个主题」的建议都建立在「已有内容处理完」之上。"
                       "积压过半时再去找新主题，就是收集不处理 —— 项目自己的 "
                       "discover 护栏也会拦（它的原话：PKM 的典型反模式）",
                "hint": "下面三条是结构观察，不是马上要做的动作",
                "action": "python -m bili_digest.ingest.batch_refine --order empty",
            })
    except Exception:
        pass

    try:

        qwords = set()
        try:
            from bili_digest.quality.agenda import _tokens
            for (_qid, q) in con.execute(
                    "SELECT id, question FROM open_question WHERE status='open'"):
                qwords |= _tokens(q)
        except Exception:
            pass

        rows = con.execute(
            "SELECT m.topic, COUNT(*) c, "
            "  (SELECT AVG(n) FROM (SELECT COUNT(*) n FROM content_block cb "
            "     JOIN topic_member m2 ON m2.item_uid=cb.item_uid "
            "     WHERE m2.topic=m.topic GROUP BY cb.item_uid)) avg_blocks "
            "FROM topic_member m GROUP BY m.topic HAVING c BETWEEN ? AND ? "
            "ORDER BY c DESC, m.topic",
            (max(2, thin_topic - 2), thin_topic - 1)).fetchall()

        keep = []
        for t, c, _avg_b in rows:
            if qwords & _tok_any(t):
                keep.append((t, c, "与问题相关"))
        if keep:
            out.append({
                "kind": "值得补的主题",
                "what": "%d 个主题差 1~2 条，且【有理由】补" % len(keep),
                "why": "光「差几条」不算理由 —— 补齐只是让 MOC 好看。"
                       "这里只留两种：与开放问题沾边的，或该主题下内容本身很厚的",
                "hint": "、".join("%s(%d,%s)" % x for x in keep[:6]),
                "action": "这些主题的关键词值得去 B站搜；其余「差一条」的主题忽略即可",
            })
        elif rows:
            out.append({
                "kind": "薄主题（不建议补）",
                "what": "%d 个主题差 1~2 条，但没有一个值得补" % len(rows),
                "why": "它们与开放问题都没有交集。补到 5 条的唯一后果是"
                       "MOC 里多一个看起来立住的节点 —— 为了图谱好看而囤积内容，"
                       "正是这个项目一直在反对的事",
                "action": "忽略。想要这些方向的内容，先在 kb question --add 里登记问题",
            })
    except Exception:
        pass

    try:
        def _n(cond) -> int:
            return con.execute(
                "SELECT COUNT(*) FROM item i WHERE " + cond).fetchone()[0]
        no_edge = ("NOT EXISTS(SELECT 1 FROM edge e WHERE e.src=i.uid OR e.dst=i.uid)")
        has_cb = ("EXISTS(SELECT 1 FROM content_block cb WHERE cb.item_uid=i.uid)")
        has_d = ("EXISTS(SELECT 1 FROM derived d WHERE d.item_uid=i.uid)")

        fixable = _n(no_edge + " AND " + has_cb + " AND NOT " + has_d)
        unique_ok = _n(no_edge + " AND " + has_cb + " AND " + has_d)
        empty_noe = _n(no_edge + " AND NOT " + has_cb)

        if fixable:
            out.append({
                "kind": "可修的孤点",
                "what": "%d 条有内容但没提炼，因此没有主题、进不了图谱" % fixable,
                "why": "这是唯一「derive 一下就能接上边」的一类 —— 有素材、只是没提炼",
                "action": "kb derive --limit %d" % fixable,
            })
        if empty_noe:
            out.append({
                "kind": "孤点中的空壳（不在这里处理）",
                "what": "%d 条既没内容也没边" % empty_noe,
                "why": "它们属于积压（见第一条），不是图谱问题。内容抓到之后自然会连上",
                "action": "看第一条的 batch_refine",
            })
        if unique_ok:
            out.append({
                "kind": "独特内容（不该修）",
                "what": "%d 条有内容、已提炼，但仍无边" % unique_ok,
                "why": "抽样看是「如何合法造枪」「原神过场动画」「行为经济学」这类 —— "
                       "主题太独特，没有同类可共享。**独特内容本来就该是孤点**，"
                       "为了连上而给它造边才是噪音",
                "action": "忽略。它们照样能被检索到 —— 检索不依赖图谱",
            })
    except Exception:
        pass

    try:
        rows = con.execute("""
            SELECT m.topic, COUNT(*) n,
                   SUM(CASE WHEN NOT EXISTS(SELECT 1 FROM content_block cb
                        WHERE cb.item_uid=m.item_uid) THEN 1 ELSE 0 END) e
            FROM topic_member m GROUP BY m.topic HAVING n >= 4""").fetchall()
        bad = [(t, n, e) for t, n, e in rows if n and e / n >= min_empty_ratio]
        if bad:
            bad.sort(key=lambda x: -x[2] / x[1])
            out.append({
                "kind": "主题空壳",
                "what": "%d 个主题里过半成员没有内容" % len(bad),
                "why": "标题收了、视频没抓，MOC 列出来点开是空的 —— 比没收录更差",
                "hint": "最重的：" + "、".join("%s(%d/%d)" % (t, e, n) for t, n, e in bad[:4]),
            })
    except Exception:
        pass

    try:
        n = con.execute("SELECT COUNT(DISTINCT item_uid) FROM annotation").fetchone()[0]
        tot = con.execute(
            "SELECT COUNT(*) FROM item WHERE EXISTS("
            "  SELECT 1 FROM content_block cb WHERE cb.item_uid=item.uid)").fetchone()[0]
        if tot and n / tot < 0.05:

            out.append({
                "kind": "（参考）批注使用情况",
                "what": "%d/%d 条有手写批注（%.1f%%）" % (n, tot, 100.0 * n / tot),
                "why": "这条不是缺口，是状态：系统补不了它。列出来只为说明"
                       "「输出决定输入」那条闭环目前只有系统侧在转 —— "
                       "系统侧的输出（笔记/图谱/Bases）已经齐了，输入（人的判断）还是空的",
                "action": "不需要现在做什么。真要用的时候：kb annotate --add <uid> \"…\"",
            })
    except Exception:
        pass

    return out

def render(items: list) -> list:
    if not items:
        return ["", "  ✓ 结构上看不出明显缺口", ""]
    L = ["", "  ── 产出侧诊断 ─────────────────────────", "",
         "  先说清楚：下面每一条都标了它是「缺口」还是「状态」。",
         "  缺口能派动作，状态只是事实 —— 混在一起会让人以为什么都得修。", ""]
    for i, x in enumerate(items, 1):
        L.append("  [%d] %s：%s" % (i, x["kind"], x["what"]))

        _lbl = "说明" if x["kind"].startswith("（参考）") else "为什么算缺口"
        L.append("      %s：%s" % (_lbl, x["why"]))
        if x.get("hint"):
            L.append("      → %s" % x["hint"])
        if x.get("action"):
            L.append("      ▶ 可做：%s" % x["action"])
        L.append("")
    return L

def main() -> int:
    import argparse
    import sqlite3
    paths.enable_utf8_stdout()
    ap = argparse.ArgumentParser(description="从产出结构反推缺口")
    ap.add_argument("--thin", type=int, default=5, help="主题少于几条算薄（默认 5）")
    a = ap.parse_args()
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    for ln in render(gaps(con, thin_topic=a.thin)):
        print(ln)
    return 0

if __name__ == "__main__":
    sys.exit(main())
