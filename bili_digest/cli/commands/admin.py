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

from bili_digest.cli.helpers import db_of, _as_uid

def cmd_why(a) -> None:
    from bili_digest.quality import lifecycle as lc
    con = db_of(a)
    uid = _as_uid(a.uid)

    item = con.execute("SELECT * FROM item WHERE uid=?", (uid,)).fetchone()
    if not item:
        print("  没有这条：%s" % uid)
        con.close()
        return
    _d = {k: item[k] for k in item.keys()}
    try:
        s = lc.score_from_prefetch(_d, lc._prefetch(con), lc.load_blocklist(con))
    except Exception:
        s = lc.score_item(con, uid)
    if not s:
        print("  没有这条：%s" % uid)
        con.close()
        return

    row = con.execute(
        "SELECT l.tier, i.title FROM item i "
        "LEFT JOIN lifecycle l ON l.item_uid=i.uid WHERE i.uid=?", (uid,)).fetchone()
    print("  %s" % ((row["title"] or "")[:60] if row else ""))
    print("  分级 %-10s 得分 %.4f" % (row["tier"] if row else "?", s["score"]))
    print()
    print("  %-14s %8s %8s %8s" % ("因子", "权重", "取值", "贡献"))
    for name, w, v, c in sorted(s.get("factors") or [], key=lambda x: -abs(x[3])):
        print("  %-14s %8.4f %8.4f %+8.4f" % (name, w, v, c))
    print()
    for k in ("density", "quality", "freshness", "source", "cps", "ttl"):
        if k in s:
            print("  %-12s %s" % (k, s[k]))
    if s.get("blocked"):
        print("  屏蔽：%s" % s.get("block_reason"))
    con.close()

def cmd_harvest(a) -> None:
    from bili_digest.quality import harvest as _hv
    con = db_of(a)
    for ln in _hv.render(_hv.gaps(con, thin_topic=a.thin)):
        print(ln)

def cmd_bases(a) -> None:
    from bili_digest.output import bases as _b
    p = _b.write(a.vault, a.name)
    print("  已写入 %s" % p)
    print("  表格视图 Obsidian 内置即可用；图谱视图需社区插件 Base Graph")

def cmd_selfheal(a) -> None:
    from bili_digest.quality import selfheal as _sh
    from bili_digest.quality import health as _h
    before = _h.snapshot(full=False)
    acts = _sh.plan(allow_llm=getattr(a, "allow_llm", False))
    if getattr(a, "dry", False) or not acts:
        for ln in _sh.render(acts):
            print(ln)
        return
    results = _sh.execute(acts)
    ver = _sh.verify(before)
    for ln in _sh.render(acts, results, ver):
        print(ln)

def cmd_moc(a) -> None:
    from bili_digest.output import obsidian_out as oo
    con = db_of(a)
    r = oo.write_moc(con, a.vault, min_members=a.min_members)
    print(f"  生成 {r['written']} 个主题笔记（成员 >= {a.min_members}）→ {a.vault}/_主题/")
    for t, c in list(r["topics"].items())[:12]:
        print(f"    {t:<20} {c} 条")
    if r["written"]:
        print()
        print("视频笔记的「主题」段会链到这些节点 —— 用 kb note 重新导出即可生效")

def cmd_init(a) -> None:
    con = db_of(a)
    mode = cs.ensure_fts(con)
    print(f"  已建库 {a.db}")
    print(f"  schema v{cs.SCHEMA_VERSION}  FTS 方案 {mode}"
          f"（jieba {'可用' if cs.has_jieba() else '缺失，已退到 trigram'}）")
    con.close()

def cmd_block(a) -> None:
    con = db_of(a)
    if a.action == "add":
        cs.add_block_rule(con, a.kind, a.value, a.label, a.reason)
        con.commit()
        print(f"  已加屏蔽 {a.kind}:{a.value}"
              f"{' (' + a.label + ')' if a.label else ''}")
        print("  提示：跑 `kb.py retier --apply` 让已入库内容也生效")
    elif a.action == "rm":
        cur = con.execute("DELETE FROM blocklist WHERE kind=? AND value=?",
                          (a.kind, a.value))
        con.commit()
        print(f"  已删 {cur.rowcount} 条规则")
    elif a.action == "list":
        rules = cs.load_blocklist(con)
        for kind in BLOCK_KINDS:
            items = rules[kind]
            if items:
                print(f"  [{kind}] {len(items)} 条")
                for e in items[:20]:
                    print(f"      {e['value']}"
                          f"{'  ' + e['label'] if e['label'] else ''}")
    con.close()

def cmd_retier(a) -> None:
    con = db_of(a)
    r = lc.sweep(con, apply=a.apply)
    print(f"  扫描 {r['total']} 条")
    for t, n in sorted(r["by_tier"].items(), key=lambda x: -x[1]):
        print(f"    {t:<12} {n}")
    if a.expire:
        e = lc.expire(con, apply=a.apply)
        print(f"  TTL 到期 {e['expired']}  容量淘汰 {e['evicted']}  "
              f"事后屏蔽 {e['blocked_knowledge']}")
    print("  演练模式；加 --apply 生效" if not a.apply else "  已生效")
    con.close()

def cmd_question(a) -> None:
    con = db_of(a)
    from bili_digest.distill import annotate
    annotate.ensure_table(con)

    if getattr(a, "from_log", False):

        from bili_digest.quality import agenda as _ag
        items = _ag.suggest_from_log(con, min_times=getattr(a, "min_times", 3))
        for ln in _ag.render_suggest(items):
            print(ln)
        con.close()
        return

    if getattr(a, "add", None):

        cur = con.execute(
            "INSERT INTO open_question(item_uid, question, how, status, created_at) "
            "VALUES(NULL, ?, '用户登记（长期问题）', 'open', ?)",
            (a.add.strip(), int(__import__("time").time())))
        con.commit()
        print(f"  ✓ 已登记问题 #{cur.lastrowid}：{a.add.strip()[:60]}")
        print("    之后 kb ingest 收尾会拿新抓的内容对照它")
        con.close()
        return

    if a.resolve or a.drop:
        qid = a.resolve or a.drop
        st = "answered" if a.resolve else "dropped"
        cur = con.execute("UPDATE open_question SET status=? WHERE id=?", (st, qid))
        con.commit()
        if cur.rowcount:
            print(f"  ✓ 问题 #{qid} → {st}")
        else:
            print(f"  没有 #{qid}")
        con.close()
        return

    rows = con.execute("""SELECT id,question,how,status,created_at FROM open_question
                          ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END, id""").fetchall()
    if not rows:
        print("  问题池是空的。先跑 kb.py synth --questions 产出一批待验证问题。")
        con.close()
        return
    open_n = sum(1 for r in rows if r["status"] == "open")
    print(f"  待验证问题 {len(rows)} 个（未解决 {open_n}）\n")
    for r in rows:
        mark = {"open": "·", "answered": "✓", "dropped": "×"}.get(r["status"], "?")
        print(f"  {mark} #{r['id']:<4} {r['question'][:76]}")
        if a.verbose and r["how"]:
            print(f"        为什么现有材料答不了: {r['how'][:70]}")
    print(f"\n  标记已解决: kb.py question --resolve <id>")
    print(f"  放弃:       kb.py question --drop <id>")
    con.close()

def cmd_mark(a) -> None:
    from bili_digest.quality import lifecycle as lc
    con = db_of(a)
    uid = _as_uid(a.uid)
    row = con.execute("SELECT item_id,title FROM item WHERE uid=?", (uid,)).fetchone()
    if not row:
        print(f"  库里没有 {uid}")
        con.close()
        return
    before = lc.score_item(con, uid)["score"]
    lc.set_mark(con, uid, a.name, not a.off)
    after = lc.score_item(con, uid)["score"]
    act = "取消" if a.off else "打上"
    print(f"  {act}「{a.name}」标记: {row['item_id']}  {str(row['title'])[:40]}")
    print(f"  分数 {before:.3f} → {after:.3f}（{after - before:+.3f}）")
    print(f"  提示：跑 kb.py retier --apply 才会写进 lifecycle 表")
    con.close()

def cmd_pick(a) -> None:
    from bili_digest.quality import obs
    con = db_of(a)
    obs.ensure_pick_table(con)

    if a.list:
        rows = con.execute("SELECT * FROM pick_log ORDER BY id DESC LIMIT 30").fetchall()
        if not rows:
            print("  还没有采纳记录。用法：先 kb.py search \"...\"，再看结果，"
                  "然后 kb.py pick <第几条>")
            con.close()
            return
        print(f"  最近 {len(rows)} 条采纳:")
        for r in rows:
            it = con.execute("SELECT item_id, title FROM item WHERE uid=?",
                             (r["item_uid"],)).fetchone()
            print(f"    #{r['rank']:<2} {r['item_uid'].split(':')[-1]:<14} "
                  f"{str(it['title'])[:36] if it else '(条目已不在)'}"
                  f"{'  · ' + r['note'] if r['note'] else ''}")

        dist = {}
        for r in con.execute("SELECT rank, COUNT(*) n FROM pick_log "
                             "WHERE rank IS NOT NULL GROUP BY rank ORDER BY rank"):
            dist[r["rank"]] = r["n"]
        if dist:
            tot = sum(dist.values())
            top1 = dist.get(1, 0)
            print("  排名分布: " + " · ".join(f"{k}位×{v}" for k, v in dist.items()))
            print(f"  top1 占比 {top1/tot*100:.0f}%（越高说明排序越准）")
        con.close()
        return

    if a.uid:

        uid = _as_uid(a.uid)
        it = con.execute("SELECT item_id,title FROM item WHERE uid=?", (uid,)).fetchone()
        if not it:
            print(f"  库里没有 {uid}")
            con.close()
            return
        lq = obs.last_query(con)
        ok = obs.log_pick(con, 0, uid, query_id=lq["id"] if lq else None,
                          question=lq["question"] if lq else None, note=a.note)
        print(f"  {'✓' if ok else '✗'} 采纳 {it['item_id']}  {str(it['title'])[:44]}")
        con.close()
        return

    if not a.rank:
        print("  用法: kb.py pick <第几条>   或   kb.py pick --uid BVxxxx  或  kb.py pick --list")
        con.close()
        return

    lq = obs.last_query(con)
    if not lq:
        print("  还没有检索记录 —— 先跑一次 kb.py search")
        con.close()
        return

    stored = None
    try:
        if lq.get("hits_json"):
            stored = json.loads(lq["hits_json"])
    except Exception:
        stored = None

    if not stored:
        print("  最近那次查询没有存下命中（可能是旧版本记的）—— 重新 search 一次再来")
        con.close()
        return
    if len(stored) < a.rank:
        print(f"  最近查询「{lq['question']}」当时只列出 {len(stored)} 条，"
              f"没有第 {a.rank} 条")
        con.close()
        return
    h = stored[a.rank - 1]
    uid = h.get("uid")
    ok = obs.log_pick(con, a.rank, uid, query_id=lq["id"], question=lq["question"],
                      block_id=h.get("block_id"), note=a.note)
    it = con.execute("SELECT item_id,title FROM item WHERE uid=?", (uid,)).fetchone()
    print(f"  {'✓' if ok else '✗'} 采纳最近查询「{lq['question']}」的第 {a.rank} 条")
    print(f"     {str(it['title'])[:52] if it else uid}")
    print(f"     {str(h.get('text'))[:70]}")
    con.close()

def cmd_note(a) -> None:
    from bili_digest.output import obsidian_out as oo
    con = db_of(a)
    if a.list:

        for r in con.execute(
                "SELECT i.uid,i.title,l.tier FROM item i "
                "LEFT JOIN lifecycle l ON l.item_uid=i.uid "
                "WHERE COALESCE(l.tier,?) IN (?,?) ORDER BY i.saved_at DESC LIMIT ?",
                (TIER_CACHE, TIER_KNOWLEDGE, TIER_RAW, a.limit)):
            print(f"  {r['tier'] or '?':<10} {r['uid']:<24} {(r['title'] or '')[:44]}")
        con.close()
        return
    if not a.vault:
        raise SystemExit("需要 --vault 指定 Obsidian vault 路径")
    uids = a.uid or []
    if a.all:

        if getattr(a, "all_tiers", False):
            tiers = (TIER_CACHE, TIER_KNOWLEDGE, TIER_RAW, "discarded")
        elif getattr(a, "tier", None):
            tiers = tuple(t.strip() for t in a.tier.split(",") if t.strip())
        else:
            tiers = (TIER_KNOWLEDGE, TIER_RAW)
        ph = ",".join("?" * len(tiers))
        uids = [r[0] for r in con.execute(
            f"SELECT item_uid FROM lifecycle WHERE tier IN ({ph})", tiers)]
    if not uids:
        raise SystemExit("没指定 --uid 也没有可输出的条目")
    for uid in uids:
        try:
            rel = oo.suggest_related(con, uid)

            d = oo.collect_derived(con, uid)
            p = oo.write_note(con, uid, a.vault, a.subdir, related=rel,
                              summary=d.get("summary"),
                              key_points=d.get("key_points"),
                              insights=d.get("insights"),
                              topics=d.get("topics"),
                              evidence_ids=d.get("evidence_ids"))
            lc.touch(con, uid)
            print(f"  {p.name}")
        except SystemExit as e:
            print(f"  {uid}: {e}")
    con.commit()
    con.close()

def cmd_index(a) -> None:
    con = db_of(a)

    if getattr(a, "rebuild", False):
        print("  ⚠ 清空重建：会全量重新编码（6 万块约 5~7 分钟）")
        n_cb = con.execute("SELECT COUNT(*) FROM content_block").fetchone()[0]
        vi = rc.VectorIndex(a.vec, dtype=a.dtype, rebuild=True)
        r = rc.index_chunks(con, vi, rebuild=True, verbose=True)
        print(f"  重建完成：库 {n_cb} 块 → 索引 {vi.size} 条"
              f"（回收孤儿 {max(0, n_cb - vi.size) if vi.size else '?'}）")
        con.close()
        return
    vi = rc.VectorIndex(a.vec, dtype=a.dtype)
    print(f"  向量后端 {vi.mode}  当前 {vi.size} 条  内存 {vi.memory_mb():.1f}MB")
    if vi.mode == "numpy":
        print("  [!] 未装 usearch，退到内存暴力扫描。")
        print("      小规模可用；到十万级请装：pip install usearch==2.9.2")
    r = rc.index_chunks(con, vi)
    print(f"  新增 {r['new']}，跳过 {r['skipped']}"
          f"{'，耗时 ' + str(r.get('elapsed')) + 's' if r.get('elapsed') else ''}")
    print(f"  索引总量 {vi.size}  内存 {vi.memory_mb():.1f}MB  → {a.vec}")
    con.close()

def cmd_adapter(a) -> None:
    from bili_digest.adapters import adapters
    print("  已注册平台：")
    for name in adapters.ADAPTERS:
        try:
            adapters.get_adapter(name)
            state = "可用"
        except NotImplementedError:
            state = "占位（接口已定）"
        except Exception:
            state = "需要凭证"
        print(f"    {name:<10} {state}")

def cmd_blocksuggest(a) -> None:
    from bili_digest.distill import block_plus as bp
    con = db_of(a)
    con.row_factory = sqlite3.Row
    try:
        bp.run(a, con)
    finally:
        con.close()

def cmd_lateon(a) -> None:
    from bili_digest.retrieve import lateon as lo
    con = db_of(a)
    try:
        if getattr(a, "build", False):
            r = lo.build(con, force=getattr(a, "force", False))
            if r.get("built"):
                print("  ✓ 已构建：%d 条 · %.1f 秒 · %.1f MB" % (
                    r["items"], r["seconds"], r["mb"]))
            else:
                print("  · 缓存已是最新（%s）" % r.get("reason", ""))
            return 0
        ok = lo.available(con)
        print("  缓存：%s" % (paths.LATEON_CACHE if paths.LATEON_CACHE.is_file() else "（无）"))
        print("  状态：%s" % ("可用（指纹匹配）" if ok else "不可用（未构建或已过期）"))
        if not ok:
            print("  构建：python kb.py lateon --build")
        return 0
    finally:
        con.close()

def cmd_weights(a) -> None:
    from bili_digest.quality import source_weight as sw
    con = db_of(a)
    con.row_factory = sqlite3.Row
    try:
        sw.run(a, con)
    finally:
        con.close()

def cmd_annotate(a) -> None:
    from bili_digest.distill import annotate as an
    con = db_of(a)
    con.row_factory = sqlite3.Row
    try:
        an.run(a, con)
    finally:
        con.close()
