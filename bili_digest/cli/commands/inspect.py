#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import json
import sqlite3
import sys
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from bili_digest import paths

from bili_digest.schema import core_schema as cs
from bili_digest.quality import lifecycle as lc
from bili_digest.retrieve import rag_core as rc
from bili_digest.paths import source_key, bili_uid
from bili_digest import paths as cs_paths
from bili_digest.schema.core_schema import (BLOCK_KINDS, TIER_CACHE, TIER_KNOWLEDGE,
                         TIER_RAW, connect)

DEFAULT_DB = str(cs_paths.DB)
DEFAULT_VEC = str(cs_paths.VEC)

from bili_digest.cli.helpers import db_of

def cmd_graph(a) -> None:
    from bili_digest.graph import graph as gp
    con = db_of(a)
    con.row_factory = sqlite3.Row
    try:
        if a.build:
            vault = a.vault
            if vault is None:

                from bili_digest.paths import find_vault
                vault = find_vault()
                if vault is None:
                    print("  找不到 Obsidian vault（用 KB_VAULT 或 --vault 指定）——"
                          "只建「同UP/共享主题」两类边")
            print("  建边中…")
            print("  " + json.dumps(gp.build_all(vault), ensure_ascii=False))
            print("  " + json.dumps(gp.stats(con), ensure_ascii=False))
        elif a.expand:
            r = gp.expand(con, a.expand, hops=a.hops)
            print(f"  从 {a.expand} 扩展 {a.hops} 跳，发现 {r['discovered']} 个条目")
            for lv in r["levels"]:
                print(f"\n  ── 第 {lv['hop']} 跳 ──")
                for it in lv["items"][:8]:
                    print(f"    {it['score']:.3f}  [{it['tier'] or '?'}] "
                          f"{str(it['title'])[:46]}")
        else:
            print(json.dumps(gp.stats(con), ensure_ascii=False, indent=1))
    finally:
        con.close()

def cmd_health(a) -> None:
    from bili_digest.quality import health as _h
    rows = _h.snapshot(full=getattr(a, "full", False))
    for ln in _h.render(rows):
        print(ln)

def cmd_stats(a) -> None:
    con = db_of(a)
    s = cs.stats(con)
    for k in ("items", "blocks", "collections", "members", "tags"):
        print(f"  {k:<12} {s[k]}")
    for group in ("by_platform", "by_kind", "by_tier", "blocklist"):
        if s.get(group):
            print(f"  {group}:")
            for k, v in s[group].items():
                print(f"      {k:<14} {v}")

    from pathlib import Path as _Path
    _vp = _Path(a.vec)
    try:
        _n_vec = con.execute("SELECT COUNT(*) FROM vec_state").fetchone()[0]
    except Exception:
        _n_vec = 0
    _mb = _vp.stat().st_size / 1e6 if _vp.exists() else 0.0
    print(f"  向量索引     {_n_vec} 条  {_mb:.1f}MB（文件）")

    if getattr(a, "check", False):
        try:
            from bili_digest.quality import check_index as ci
            r = ci.check(verbose=False)
            if r["consistent"]:
                print(f"  索引一致性   ✓")
            else:
                kinds = ", ".join(f"{k}缺{v}" for k, v in r["missing_by_kind"].items())
                print(f"  索引一致性   ✗ 缺 {r['missing']} 条（{kinds}）"
                      f"  孤立 {r['orphan']}  过期 {r['stale']}")
                print(f"               跑 `python build_kb_index.py` 重建"
                      f"（或 `python check_index.py --fix`）")
        except Exception as e:
            print(f"  索引一致性   检查失败: {type(e).__name__}: {str(e)[:50]}")
    con.close()

def cmd_expand(a) -> None:
    from bili_digest.retrieve import expand as _expand
    con = db_of(a)
    try:
        for line in _expand.render(_expand.around(con, a.block_id, a.n)):
            print(line)
    finally:
        con.close()

def cmd_browse(a) -> None:
    from bili_digest.retrieve import browse as _b
    con = db_of(a)
    try:
        if not a.topic:
            ts = _b.topics(con, a.limit)
            print("  共 %d 个主题（按条目数降序）" % len(ts))
            print()
            for t in ts:
                print("    %-24s %3d 条" % (str(t["topic"])[:24], t["n"]))
            print()
            print("  看某个主题：kb.py browse --topic <名字>")
            return
        its = _b.items_of(con, a.topic, a.limit)
        if not its:
            cand = _b.find(con, a.topic)
            if not cand:
                print("  没有匹配的主题：%s" % a.topic)
                return
            print("  「%s」没有精确匹配，相近的主题：" % a.topic)
            for c in cand:
                print("    %-24s %3d 条" % (str(c["topic"])[:24], c["n"] or 0))
            return
        print("  主题「%s」下 %d 条" % (a.topic, len(its)))
        print()
        import time as _t
        for it in its:
            d = _t.strftime("%Y-%m", _t.localtime(it["saved_at"])) if it["saved_at"] else "?"
            print("    %s  %-14s %s" % (d, it["uid"].split(":")[-1], it["title"][:48]))
    finally:
        con.close()

def cmd_weekly(a) -> None:

    from bili_digest.distill import weekly as _w
    con = db_of(a)
    try:
        d = _w.build(con)
        c, s = d[chr(34)+chr(34)+chr(34) if False else 'cold'], d['shells']
        print('  ── 每周简报 ─────────────────────────────')
        print()
        print('  从没被查过   %d / %d 条' % (c['total'], c['items']))
        print('  （有检索记录的只有 %d 条）' % c['touched'])
        for b in c['sample'][:a.top]:
            print('    %4d 天  %-14s %s' % (b['days'], b['uid'].split(':')[-1], b['title'][:44]))
        print()
        print('  空  壳       %d 条（收藏了但没内容）' % s['total'])
        for b in s['sample'][:3]:
            print('    %4d 分  %-14s %s' % ((b['duration_sec'] or 0) // 60, b['uid'].split(':')[-1], b['title'][:44]))
        print()
        ps = _w.proposals(d)
        if ps:
            print('  这周可以做')
            for i, x in enumerate(ps, 1):
                print('    %d. %s' % (i, x))
    finally:
        con.close()

def cmd_quiz(a) -> None:
    from bili_digest.distill import quiz as _quiz
    con = db_of(a)
    try:
        qs = _quiz.make_quiz(con, a.n, a.seed, a.recent)
        if not qs:
            print("  没有可出题的条目（需要 derived.topics）")
            return
        print("  共 %d 题。先自己想，再揭晓。" % len(qs))
        print()
        for i, q in enumerate(qs, 1):
            print("  ── 第 %d 题 ─────────────────────────────" % i)
            print("  主题词: %s" % " / ".join(q["topics"]))
            if not a.no_wait:
                try:
                    input("  想好了按回车揭晓…")
                except EOFError:
                    pass
            print()
            print("  摘要: %s" % q["answer_summary"][:300])
            print("  出处: %s《%s》" % (q["uid"].split(":")[-1], q["title"][:40]))
            print()
    finally:
        con.close()

def cmd_rot(a) -> None:
    from bili_digest.distill import rot as _rot
    con = db_of(a)
    try:
        rows = _rot.stale_conclusions(con, a.days, a.min_newer)
        if not rows:
            print("  没有需要复核的结论（%d 天内、同主题新资料 >= %d 条）"
                  % (a.days, a.min_newer))
            return
        print("  该复核的 %d 条：" % len(rows))
        for r in rows[:20]:
            print()
            print("  %s  《%s》" % (r["uid"].split(":")[-1], r["title"]))
            print("    之后有 %d 条同主题新资料 · 主题 %s"
                  % (r["newer"], " / ".join(r["topics"])))
            print("    结论: %s" % r["conclusion"])
    finally:
        con.close()

def cmd_highlights(a) -> None:
    from bili_digest.distill import highlights as hl
    from bili_digest.paths import bili_uid
    con = db_of(a)
    try:
        uid = a.uid if ":" in a.uid else bili_uid(a.uid)
        w = hl.worthwhile(con, uid, a.bucket)
        if "shape" not in w:
            print("  %s" % w.get("note", "没有弹幕数据"))
            return
        print("  %s · %d 条弹幕 · 时长 %ds" % (
            uid.split(":")[-1], w["total"], w["duration_sec"]))
        print("  形状: %s · 峰值在第 %d 分钟（%d 条 vs 中位 %d，比值 %.1f）" % (
            w["shape"], w["peak_at_min"], w["peak_n"], w["median_n"], w["ratio"]))
        print("  注: %s" % w["note"])
        print()
        KIND_CN = {"debate": "在争论", "hype": "在叫好",
                   "mixed": "有争论也叫好", "neutral": "讨论"}
        for m in hl.moments(con, uid, top=a.top, bucket_sec=a.bucket):
            print("  [%02d:%02d] %d 条 · %s（争论词 %d · 叫好词 %d）" % (
                m["min"], m["sec"], m["n"],
                KIND_CN.get(m.get("kind"), "?"), m.get("debate", 0), m.get("hype", 0)))
            for s in m.get("samples", [])[:4]:
                print("      %s" % str(s)[:56])
    finally:
        con.close()

def cmd_popularity(a) -> None:
    from bili_digest.retrieve import popularity as pop
    from bili_digest.paths import bili_uid
    con = db_of(a)
    try:
        if not a.uids:
            rows = pop.top(con, by=a.by, n=a.top)
            total = con.execute("SELECT COUNT(*) FROM item").fetchone()[0]
            print("  全库按 %s 排前 %d 条（库内共 %d 条）" % (a.by, len(rows), total))
            if a.by in ("like", "reply"):
                n_have = sum(1 for r in con.execute("SELECT extras FROM item")
                             if pop._parse_extras(r[0]).get(a.by) is not None)
                print("  注：%s 只有 %d 条有数据，其余缺值排在最后（缺值不等于 0）"
                      % (a.by, n_have))
            print()
            for r in rows:
                print("  %-14s 播放=%-10s 点赞=%-8s 弹幕=%-8s  %s" % (
                    r["uid"].split(":")[-1], r.get("play"), r.get("like"),
                    r.get("danmaku"), (r.get("title") or "")[:38]))
            return
        uids = [u if ":" in u else bili_uid(u) for u in a.uids]
        for u, v in pop.rank(con, uids, by=a.by):
            s = pop.stats_of(con, u)
            print("  %-22s %s=%-10s 播放=%-9s 点赞=%-7s 弹幕=%-7s" % (
                u.split(":")[-1], a.by, v, s.get("play"), s.get("like"),
                s.get("danmaku")))
    finally:
        con.close()

def cmd_trends(a) -> None:
    from bili_digest.distill import trends as tr
    con = db_of(a)
    con.row_factory = sqlite3.Row
    try:
        if getattr(a, "hot", None):
            rows = tr.trends(con, days=a.hot, min_items=a.min_items, verbose=True)
            for t in rows:
                print("    %-24s %2d 条 · %d 个 UP · %s ~ %s" % (
                    t["topic"][:24], t["n_items"], t["n_authors"],
                    t["earliest"], t["latest"]))
            if not rows:
                print("    （这个时间窗内没有达到 %d 条的话题）" % a.min_items)
        elif getattr(a, "author", None):
            r = tr.author_timeline(con, a.author, a.since, a.until, verbose=True)
            if not r["n"]:
                print("    %s" % r.get("note"))
            for it in r["items"][:a.limit if hasattr(a, "limit") else 30]:
                print("    %s  %s" % (it["date"], str(it["title"])[:56]))
            if r["n"] > 30:
                print("    …还有 %d 条（用 --limit 调整）" % (r["n"] - 30))
        elif getattr(a, "dormant", None):
            rows = tr.dormant_topics(con, quiet_days=a.dormant)
            print("  近 %d 天没再被提到的主题：" % a.dormant)
            for t in rows:
                print("    %-26s 共 %2d 条 · 近 %d 天 0 条" % (
                    t["topic"][:26], t["total"], a.dormant))
            if not rows:
                print("    （没有沉寂的话题）")
        elif getattr(a, "first", None):
            rows = tr.first_mention(con, a.first)
            print("  最早提到「%s」的条目：" % a.first)
            for it in rows:
                print("    %s  %-42s UP:%s" % (
                    it["date"], str(it["title"])[:42], str(it["author"])[:14]))
        else:
            print("  用法：--hot N / --author 名字 / --dormant N / --first 关键词")
    finally:
        con.close()
