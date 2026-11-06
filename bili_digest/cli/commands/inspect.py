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
    from bili_digest.cli import exit_codes as EC
    rows = _h.snapshot(full=getattr(a, "full", False))

    try:
        import sqlite3 as _s
        from bili_digest.paths import DB as _DB
        _c = _s.connect(str(_DB))
        _h.record(_c, rows)
        _c.close()
    except Exception:
        pass

    _ages = {}
    try:
        _c2 = _s.connect(str(_DB))
        _ages = _h.ages(_c2)
        _c2.close()
    except Exception:
        pass
    for ln in _h.render(rows, _ages):
        print(ln)

    return EC.from_rows(rows)

def cmd_stats(a) -> None:
    con = db_of(a)

    if getattr(a, "evidence", False):
        from bili_digest.output.obsidian_out import _provenance as _prov
        cnt = {}
        for r in con.execute("SELECT uid FROM item WHERE platform='bilibili'"):
            try:
                b = _prov(con, r["uid"]).get("evidence_basis") or "none"
            except Exception:
                b = "?"
            cnt[b] = cnt.get(b, 0) + 1
        tot = sum(cnt.values()) or 1
        print("  evidence_basis（摘要建立在什么之上）：")
        for k in ("transcript", "mixed", "comment-only", "none", "?"):
            if cnt.get(k):
                print(f"      {k:<14} {cnt[k]:5d}  {100*cnt[k]/tot:5.1f}%")
        con.close()
        return
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

def _log_usage(con, block_id: int, kind: str) -> None:
    try:
        from bili_digest.schema.core_schema import now_sec
        con.execute("CREATE TABLE IF NOT EXISTS usage_log ("
                    "block_id INTEGER NOT NULL, kind TEXT NOT NULL, "
                    "at INTEGER NOT NULL)")
        con.execute("INSERT INTO usage_log VALUES(?,?,?)", (block_id, kind, now_sec()))
        con.commit()
    except Exception:
        pass

def cmd_expand(a) -> None:
    from bili_digest.retrieve import expand as _expand
    con = db_of(a)
    try:
        if getattr(a, "claim", False):
            _show_cite_edges(con, a.block_id)
            return

        _log_usage(con, a.block_id, "expand")
        for line in _expand.render(_expand.around(con, a.block_id, a.n)):
            print(line)
    finally:
        con.close()

def _show_cite_edges(con, claim_block_id: int) -> None:

    exists = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cite_edge'").fetchone()
    if not exists:
        print("  [!] cite_edge 表不存在 —— 迁移没跑过（不是「旧数据没有边」）。")
        print("      这张表由 derive 在写边时 CREATE；跑一次任意条目的 derive 就会建出来。")
        print(f"      在那之前，用块级入口：kb expand {claim_block_id}")
        return
    rows = con.execute(
        "SELECT e.claim_idx, e.evidence_block_id, b.kind, b.offset_sec, substr(b.text, 1, 110) "
        "FROM cite_edge e LEFT JOIN content_block b ON b.id = e.evidence_block_id "
        "WHERE e.claim_block_id = ? ORDER BY e.claim_idx IS NULL, e.claim_idx, e.evidence_block_id",
        (claim_block_id,)).fetchall()
    if not rows:
        print(f"  块 #{claim_block_id} 没有引用边 —— 这条提炼在 cite_edge 之前（2026-11-05）。")
        print("  那不是故障：旧条目没有边，而下次它被重新提炼时会自然长出来。")
        print(f"  在那之前，用块级入口：kb expand {claim_block_id}")
        return
    print(f"  块 #{claim_block_id} 引用了 {len(rows)} 个证据块：")
    last_idx = object()
    for cidx, bid, kind, off, text in rows:
        if cidx != last_idx:
            last_idx = cidx
            print(f"  ── 要点 #{cidx if cidx is not None else '?'} ──")
        where = ""
        if off is not None:
            where = "  %d:%02d" % (int(off) // 60, int(off) % 60)
        print(f"    #{bid}  {kind or '?'}{where}")
        if text:
            print(f"        {text}")

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
        for ln in _w.render(con, getattr(a, "top", 8)):
            print(ln)
    finally:
        con.close()

def cmd_quiz(a) -> None:
    from bili_digest.distill import quiz as _quiz
    argv = ["--db", str(a.db), "--n", str(a.n)]
    if a.seed is not None:
        argv += ["--seed", str(a.seed)]
    if a.recent is not None:
        argv += ["--recent", str(a.recent)]
    if getattr(a, "due", False):
        argv.append("--due")
    if getattr(a, "rate", False):
        argv.append("--rate")
    if a.no_wait:
        argv.append("--no-wait")
    _quiz.main(argv)

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
