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

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import paths

import core_schema as cs
import lifecycle as lc
import rag_core as rc
from paths import source_key, bili_uid
import paths as cs_paths
from core_schema import (BLOCK_KINDS, TIER_CACHE, TIER_KNOWLEDGE,
                         TIER_RAW, connect)

DEFAULT_DB = str(cs_paths.DB)
DEFAULT_VEC = str(cs_paths.VEC)

def db_of(a) -> sqlite3.Connection:
    return connect(a.db)

def cmd_init(a):
    con = db_of(a)
    mode = cs.ensure_fts(con)
    print(f"  已建库 {a.db}")
    print(f"  schema v{cs.SCHEMA_VERSION}  FTS 方案 {mode}"
          f"（jieba {'可用' if cs.has_jieba() else '缺失，已退到 trigram'}）")
    con.close()

def cmd_fetch(a):
    import adapters
    con = db_of(a)
    ad = adapters.get_adapter(a.platform)
    for iid in a.ids:
        try:
            r = adapters.import_item(con, ad, iid, fetch_content=not a.meta_only)
            print(f"  {iid}: {r}")
        except Exception as e:
            print(f"  {iid}: 失败 {type(e).__name__} {str(e)[:60]}")
    con.close()

def cmd_import(a):
    import adapters
    con = db_of(a)
    base = Path(a.digest)
    raws = []
    if (base / "raw.sqlite").exists():

        raws = [(base.name, base / "raw.sqlite")]
    else:
        raws = [(d.name, d / "raw.sqlite") for d in sorted(base.iterdir())
                if d.is_dir() and (d / "raw.sqlite").exists()]
    if not raws:
        print(f"  在 {base} 下没找到 raw.sqlite")
        return
    agg = {"ok": 0, "skipped": 0, "blocks": 0}
    for name, raw in raws:
        r = adapters.import_digest(con, raw)
        if r.get("skipped"):
            agg["skipped"] += 1
            print(f"  {name}: 屏蔽跳过（{r['reason']}）")
        elif r.get("error"):
            print(f"  {name}: {r['error']}")
        else:
            blk = r.get("blocks") or {}
            n = sum(blk.values()) if isinstance(blk, dict) else blk
            agg["ok"] += 1
            agg["blocks"] += n
            print(f"  {name}: {n} 块  tier={r['tier']}  score={r['score']}")
    print(f"\n  导入 {agg['ok']} 条，跳过 {agg['skipped']} 条，共 {agg['blocks']} 块")
    print(f"  库内现有 {cs.stats(con)['items']} 条目")
    con.close()

def cmd_block(a):
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

def cmd_retier(a):
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

def _as_uid(raw: str) -> str:

    return raw if ":" in (raw or "") else bili_uid(raw)

def cmd_question(a):
    con = db_of(a)
    import annotate
    annotate.ensure_table(con)

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

def cmd_mark(a):
    import lifecycle as lc
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

def cmd_pick(a):
    import obs
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

def cmd_note(a):
    import obsidian_out as oo
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

def cmd_index(a):
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

def _log_search(con, a, hits, mode, gran, t0, top_uid=None):
    try:
        import obs
        import time as _t
        obs.log_query(
            con, getattr(a, "query", "") or "",
            mode=mode, granularity=gran, n_hits=len(hits),
            top_uid=top_uid, ms=(_t.time() - t0) * 1000.0,
            hits=hits,
            filt={"since": getattr(a, "since", None),
                  "until": getattr(a, "until", None),
                  "uid": getattr(a, "uid", None),
                  "partitioned": getattr(a, "partitioned", False)})
    except Exception:
        pass

def cmd_search(a):
    con = db_of(a)
    import temporal as tp
    import time as _t
    _t0 = _t.time()

    gran = getattr(a, "granularity", "block")

    for _lo, _hi, _name in (
            (getattr(a, "min_duration", None), getattr(a, "max_duration", None), "时长"),
            (getattr(a, "min_blocks", None), getattr(a, "max_blocks", None), "块数")):
        if _lo is not None and _hi is not None and _lo > _hi:
            raise SystemExit(
                f"{_name}范围反了：min={_lo} > max={_hi}，不可能有结果"
                f"（若确实想按上限筛，去掉 min）")

    seg_mode = (gran == "seg")
    if seg_mode:
        gran = "block"

    if gran in ("item", "topic"):
        import retrieve as rv
        if getattr(a, "partitioned", False):
            import partitioned_search as ps
            from fastembed import TextEmbedding
            vi = ps._load_index()
            model = TextEmbedding(model_name=ps.EMBED_MODEL,
                                  cache_dir=str(Path(__file__).parent / "models"))
            mode = "partitioned"
        else:
            vi = rc.VectorIndex(a.vec)
            model = None
            mode = "hybrid"
        kw = {}
        if a.uid:
            kw["item_filter"] = a.uid
        if a.since or a.until:
            kw["since"] = a.since
            kw["until"] = a.until
            kw["time_field"] = a.time_field
        if getattr(a, "deep", False):
            print("  深查：改写查询中（每查询一次 LLM 调用）…")
        hits = rv.retrieve(con, vi, a.query, granularity=gran, top_k=a.top_k,
                           mode=mode, model=model,
                           termify=getattr(a, "deep", None), **kw)
        print(f"  {gran} 粒度 命中 {len(hits)} 条（链路 {mode}）")
        print()
        _log_search(con, a, hits, mode, gran, _t0,
                    top_uid=(hits[0].get("item_uid") if hits else None))
        for i, h in enumerate(hits, 1):
            if gran == "item":
                print(f"  [{i}] {h['score']:.3f} 《{str(h['title'])[:40]}》"
                      f"  命中 {h['n_hits']} 块 {h['matched_kinds']}")
                ev = h["evidence"]
                print(f"        证据[{ev['kind']}/{ev['origin_hint']}] "
                      f"{ev['text'][:60]}")
                if h["summary"]:
                    print(f"        摘要: {h['summary'][:64]}")
                if a.show_source and h.get("url"):
                    print(f"        {h['url']}")
            else:
                print(f"  [{i}] {h['score']:.3f} 「{h['topic']}」"
                      f"  {h['n_items']} 个条目")
                for it in h["items"][:4]:
                    print(f"        {it['score']:.3f} {it['evidence'][:58]}")
        con.close()
        return

    if getattr(a, "partitioned", False):

        import partitioned_search as ps
        from fastembed import TextEmbedding
        vi = ps._load_index()
        model = TextEmbedding(model_name=ps.EMBED_MODEL,
                              cache_dir=str(Path(__file__).parent / "models"))
        hits = ps.search(con, vi, model, a.query, a.top_k, item_filter=a.uid)
        dropped = None
        if hits and "_dropped_partitions" in hits[-1]:
            dropped = hits.pop()["_dropped_partitions"]
        print(f"  分区检索 命中 {len(hits)} 条\n")
        _log_search(con, a, hits, "partitioned", "block", _t0)
        for i, h in enumerate(hits, 1):
            loc = {"subtitle_seg": "段落", "subtitle": "字幕行", "danmaku": "弹幕",
                   "comment": "评论", "meta": "标题简介"}.get(h["kind"], h["kind"])
            tm = ""
            if h.get("offset_sec") is not None:
                m, s = divmod(int(h["offset_sec"]), 60)
                tm = f" {m:02d}:{s:02d}"
            print(f"  [{i}] {h['part_score']:.3f} [{h['partition']}/{loc}]{tm} "
                  f"{h['text'][:60]}")
            if a.show_source:
                print(f"        ← 《{str(h['title'])[:40]}》 {h.get('url') or ''}")
        if dropped:
            print(f"\n  （分区被门控丢弃：{dropped} —— 最高相似度低于阈值，"
                  f"视为该分区无相关内容）")
        con.close()
        return

    vi = rc.VectorIndex(a.vec)
    if not vi.size:
        raise SystemExit("向量索引为空，先跑 `kb.py index`")
    k = rc.auto_top_k(a.query, a.top_k) if a.auto_k else a.top_k

    _kinds = ["subtitle_seg"] if seg_mode else (
        a.kinds.split(",") if a.kinds else None)
    hits = rc.hybrid(con, vi, a.query, top_k=k,
                     item_filter=a.uid, kinds=_kinds,
                     since=a.since, until=a.until,
                     time_field=a.time_field, recency=a.recency,
                     min_duration=a.min_duration, max_duration=a.max_duration,
                     min_blocks=a.min_blocks, max_blocks=a.max_blocks)
    if not a.no_rerank:
        hits = rc.heuristic_rerank(hits, a.query)
    tinfo = ""
    if a.since or a.until:
        tinfo = f"  时间 {tp.describe(a.since, a.until)}（按 {a.time_field}）"
    if a.recency:
        tinfo += f"  近期加权 {a.recency}"

    finfo = []
    if a.min_duration is not None or a.max_duration is not None:
        lo, hi = a.min_duration, a.max_duration
        if lo and hi:
            finfo.append(f"时长 {lo // 60}~{hi // 60}分")
        elif lo:
            finfo.append(f"时长 ≥{lo // 60}分")
        else:
            finfo.append(f"时长 ≤{hi // 60}分")
    if a.min_blocks is not None or a.max_blocks is not None:
        lo, hi = a.min_blocks, a.max_blocks
        if lo is not None and hi is not None:
            finfo.append(f"块数 {lo}~{hi}")
        elif lo is not None:
            finfo.append(f"块数 ≥{lo}")
        else:
            finfo.append(f"块数 ≤{hi}")
    if finfo:
        tinfo += "  形式 " + "·".join(finfo)
    print(f"  混合检索 命中 {len(hits)} 条（top-k={k}"
          f"{'，自适应' if a.auto_k else ''}）{tinfo}")

    _vs = hits[0].get("query_vec_sim") if hits else None
    if _vs is not None:
        print(f"  语义贴近度 {_vs:.2f}（0~1，越高说明库里有越贴题的内容）")

    _cl = rc.cliff(hits)
    if _cl["flat"]:
        print(f"  · 结果分数齐平（最小相邻比值 {_cl['ratio']}）——"
              f"没有哪条明显突出，请自己看内容判断")
    elif _cl["cut"] < len(hits):
        print(f"  ↓ 第 {_cl['cut']} 条之后有断层（比值 {_cl['ratio']}）——"
              f"再往后的 {len(hits) - _cl['cut']} 条多半是填充")

    try:
        import obs as _obs
        rows_n = con.execute("SELECT COUNT(*) FROM pick_log").fetchone()[0]
        same = con.execute("SELECT COUNT(*) FROM query_log WHERE question=?",
                           (a.query,)).fetchone()[0]
        tip = [f"有用就回一句：kb.py pick <编号>"]
        if same > 1:
            tip.append(f"这个问题你问过 {same} 次")

        if 0 < rows_n < 5:
            tip.append(f"已攒 {rows_n} 条采纳（满 5 条才够用，见下方注释）")
        print(f"  · {' · '.join(tip)}")

        if len(hits) < 3 or _cl["flat"]:
            print("  · 想留一句自己的判断：kb.py annotate --add <条目> \"...\""
                  "（会进检索，以后能搜到）")
    except Exception:
        pass

    if gran == "block" and len(hits) >= 4:
        from collections import Counter as _C
        _c = _C(h.get("item_uid") for h in hits)
        _top_uid, _top_n = _c.most_common(1)[0]
        if _top_n >= max(4, len(hits) // 2):
            _t = next((h.get("title") for h in hits
                       if h.get("item_uid") == _top_uid), "")
            print(f"  · 其中 {_top_n}/{len(hits)} 条来自《{str(_t)[:28]}》"
                  f"—— 想只看「是哪一期」用 -g item；看整段用 -g seg")

    print()
    _log_search(con, a, hits, "hybrid", "block", _t0,
                top_uid=(hits[0].get("item_uid") if hits else None))
    for i, h in enumerate(hits, 1):
        print(f"  [{i}] {rc.fmt_hit(h)}")
        if a.show_source and h.get("title"):
            print(f"        ← 《{h['title'][:40]}》 {h.get('url') or ''}")
    con.close()

def cmd_graph(a):
    import graph as gp
    con = db_of(a)
    con.row_factory = sqlite3.Row
    try:
        if a.build:
            vault = a.vault
            if vault is None:

                from paths import find_vault
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

def cmd_stats(a):
    con = db_of(a)
    s = cs.stats(con)
    for k in ("items", "blocks", "collections", "members", "tags"):
        print(f"  {k:<12} {s[k]}")
    for group in ("by_platform", "by_kind", "by_tier", "blocklist"):
        if s.get(group):
            print(f"  {group}:")
            for k, v in s[group].items():
                print(f"      {k:<14} {v}")
    vi = rc.VectorIndex(a.vec)
    print(f"  向量索引     {vi.size} 条  {vi.memory_mb():.1f}MB  ({vi.mode})")

    try:
        import check_index as ci
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

def cmd_adapter(a):
    import adapters
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

def cmd_discover(a):
    import discover as dp
    con = db_of(a)
    con.row_factory = sqlite3.Row
    try:
        dp.run(a, con)
    finally:
        con.close()

def cmd_refine(a):
    import refine as rf
    st = rf.run(a)
    print(f"\n  {json.dumps(st, ensure_ascii=False)}")

def cmd_derive(a):
    import extract as ex
    con = db_of(a)

    if getattr(a, "baseline", False):
        ex.set_baseline(con)
        con.close()
        return

    if getattr(a, "incremental", False):
        uids = ([a.uid] if a.uid else
                [r["item_uid"] for r in con.execute(
                    "SELECT item_uid FROM derived WHERE kind='summary' "
                    "ORDER BY created_at DESC LIMIT ?", (a.limit or 100,))])
        need, done = [], 0
        for uid in uids:
            chk = ex.needs_incremental(con, uid)
            if chk["needed"]:
                need.append((uid, chk))
        print(f"  检查 {len(uids)} 条，需要增量 {len(need)} 条")
        for uid, chk in need:
            print(f"\n  增量 {uid}\n    原因：{chk['reason']}")
            r = ex.extract_incremental(con, uid)
            if r:
                done += 1
        print(f"\n  完成 {done}/{len(need)}")
        if done:
            _auto_index(con)
        con.close()
        return

    if a.show:
        d = ex.get_derived(con, a.show)
        if not d:
            print("  无提炼结果")
        for k, v in d.items():
            print(f"\n  [{k}]\n    {json.dumps(v, ensure_ascii=False)[:400]}")
    elif a.uid:
        print(f"  提炼 {a.uid}")
        r = ex.extract_item(con, a.uid, force=a.force)
        if r:
            print(f"    摘要: {str(r.get('summary'))[:90]}")
            print(f"    要点 {len(r.get('key_points') or [])} 条 / "
                  f"观众视角 {len(r.get('audience') or [])} 条 / "
                  f"洞察 {len(r.get('insights') or [])} 条")
            _auto_index(con)
    else:
        r = ex.extract_many(con, limit=a.limit, tier=a.tier, force=a.force)
        print(f"\n  完成 {r['ok']}/{r['total']}，失败 {r['fail']}")
        if r.get("ok"):
            _auto_index(con)
    con.close()

def _auto_index(con, verbose: bool = True) -> bool:
    try:
        import rag_core as rc
        from paths import VEC
        n_blocks = con.execute("SELECT COUNT(*) FROM content_block").fetchone()[0]
        keys = rc.load_index_keys(VEC)
        if keys is not None and len(keys) >= n_blocks:
            return False
    except Exception:
        pass
    try:
        import build_kb_index as bi
        r = bi.build_vec(con, verbose=False, rebuild=False)
        if verbose:
            print(f"  ✓ 已增量索引 {r.get('indexed', 0)} 条 —— 新提炼的内容现在可检索")
        return True
    except Exception as e:
        if verbose:
            print(f"  [!] 自动索引失败，请手动跑 build_kb_index.py"
                  f"（{type(e).__name__}: {str(e)[:50]}）")
        return False

def cmd_blocksuggest(a):
    import block_plus as bp
    con = db_of(a)
    con.row_factory = sqlite3.Row
    try:
        bp.run(a, con)
    finally:
        con.close()

def cmd_synth(a):
    import synthesize as sy
    con = db_of(a)
    con.row_factory = sqlite3.Row
    try:
        sy.run(a, con)
    finally:
        con.close()

def cmd_trends(a):
    import trends as tr
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

def cmd_weights(a):
    import source_weight as sw
    con = db_of(a)
    con.row_factory = sqlite3.Row
    try:
        sw.run(a, con)
    finally:
        con.close()

def cmd_annotate(a):
    import annotate as an
    con = db_of(a)
    con.row_factory = sqlite3.Row
    try:
        an.run(a, con)
    finally:
        con.close()

def main():
    ap = argparse.ArgumentParser(
        prog="kb", description="B站知识库（通用 schema，可扩展多平台）",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--vec", default=DEFAULT_VEC)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="建库").set_defaults(fn=cmd_init)

    p = sub.add_parser("question", help="待验证问题的池子（由 synth --questions 产出）")
    p.add_argument("--resolve", type=int, help="标记某问题已解决")
    p.add_argument("--drop", type=int, help="放弃某问题")
    p.add_argument("-v", "--verbose", action="store_true", help="显示「为什么答不了」")
    p.set_defaults(fn=cmd_question)

    p = sub.add_parser("pick", help="记一次「采纳」—— 检索后哪条真的有用（唯一的真实反馈）")
    p.add_argument("rank", nargs="?", type=int, help="第几条（对最近一次 search）")
    p.add_argument("--uid", help="直接指定条目，跳过排名")
    p.add_argument("--note", help="为什么有用（可选）")
    p.add_argument("--list", action="store_true", help="看已采纳记录与排名分布")
    p.set_defaults(fn=cmd_pick)

    p = sub.add_parser("mark", help="给条目打/去「重要」标记（影响评分 0.20 的维度）")
    p.add_argument("uid", help="条目 id，如 BVxxxxxxxxxx 或 bilibili:BVxxxxxxxxxx")
    p.add_argument("--name", default="important", help="标记名（important/manual/star）")
    p.add_argument("--off", action="store_true", help="取消标记")
    p.set_defaults(fn=cmd_mark)

    p = sub.add_parser("fetch", help="采集条目（联网）")
    p.add_argument("ids", nargs="+")
    p.add_argument("--platform", default="bilibili")
    p.add_argument("--meta-only", action="store_true", help="只取元数据不抓内容")
    p.set_defaults(fn=cmd_fetch)

    p = sub.add_parser("import", help="导入已抓取的 raw.sqlite")
    p.add_argument("--digest", required=True, help="bili-digest-out 目录或某个视频目录")
    p.set_defaults(fn=cmd_import)

    p = sub.add_parser("block", help="屏蔽库")
    p.add_argument("action", choices=["add", "rm", "list"])
    p.add_argument("--kind", choices=BLOCK_KINDS)
    p.add_argument("--value")
    p.add_argument("--label", help="人类可读名称，如 UP 名")
    p.add_argument("--reason")
    p.set_defaults(fn=cmd_block)

    import block_plus as _bp
    p = sub.add_parser("blocksuggest", help="屏蔽增强：推断/拉黑/采纳")
    _bp.add_cli_args(p, with_db=False)
    p.set_defaults(fn=cmd_blocksuggest)

    import discover as _disc
    p = sub.add_parser("discover", help="搜索视频并采纳进知识库")
    _disc.add_cli_args(p, with_db=False)
    p.set_defaults(fn=cmd_discover)

    p = sub.add_parser("refine", help="提炼流水线：cache → knowledge")
    import refine as _rf
    _rf.add_cli_args(p)
    p.set_defaults(fn=cmd_refine)

    p = sub.add_parser("derive", help="LLM 提炼：摘要/要点/观众视角/主题/洞察/五维打分")
    p.add_argument("--uid", help="单个条目")
    p.add_argument("--tier", help="按分级批量，如 knowledge / cache")
    p.add_argument("--limit", type=int, default=5)
    p.add_argument("--force", action="store_true", help="已存在也重跑")
    p.add_argument("--show", metavar="UID", help="查看提炼结果")
    p.add_argument("--incremental", action="store_true",
                   help="增量提炼：素材增长后（弹幕评论变多）只提新增部分，"
                        "不覆盖首次成果")
    p.add_argument("--baseline", action="store_true",
                   help="给已有提炼的条目建素材指纹基线（不调 LLM）")
    p.set_defaults(fn=cmd_derive)

    import synthesize as _syn
    p = sub.add_parser("synth", help="反刍合成：综述/矛盾点/待验证问题/行动项")
    _syn.add_cli_args(p, with_db=False)
    p.set_defaults(fn=cmd_synth)

    import source_weight as _sw
    p = sub.add_parser("trends", help="时间维度：热点 / UP 主时间线 / 沉寂 / 首发")
    p.add_argument("--hot", type=int, metavar="DAYS",
                   help="近 N 天的热点（被多个条目、多个 UP 讲的主题）")
    p.add_argument("--author", help="某 UP 的视频时间线")
    p.add_argument("--since", help="起始日期 YYYY-MM-DD（配合 --author）")
    p.add_argument("--until", help="截止日期 YYYY-MM-DD（配合 --author）")
    p.add_argument("--dormant", type=int, metavar="DAYS",
                   help="沉寂话题：以前讲得多、近 N 天没人提")
    p.add_argument("--first", help="谁最早讲了某关键词")
    p.add_argument("--min-items", type=int, default=2, help="热点最少被几条讲过")
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(fn=cmd_trends)

    p = sub.add_parser("weights", help="源权重：反馈淘汰")
    _sw.add_cli_args(p, with_db=False)
    p.set_defaults(fn=cmd_weights)

    import annotate as _an
    p = sub.add_parser("annotate", help="我的思考：批注双向同步")
    _an.add_cli_args(p, with_db=False)
    p.set_defaults(fn=cmd_annotate)

    p = sub.add_parser("retier", help="重新分级")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--expire", action="store_true", help="同时处理 TTL 与容量")
    p.set_defaults(fn=cmd_retier)

    p = sub.add_parser("note", help="输出 Obsidian 笔记")
    p.add_argument("--vault")
    p.add_argument("--subdir", default="B站")
    p.add_argument("--uid", action="append")
    p.add_argument("--all", action="store_true", help="输出所有 knowledge/raw 条目")
    p.add_argument("--tier", default=None,
                   help="指定档位（逗号分隔），如 knowledge,raw,cache,discarded")
    p.add_argument("--all-tiers", action="store_true",
                   help="导出所有档位（含 cache 与 discarded）")
    p.add_argument("--list", action="store_true", help="只列出待输出条目")
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(fn=cmd_note)

    p = sub.add_parser("index", help="建/更新向量索引")
    p.add_argument("--dtype", default="f32", choices=["f32", "f16", "i8"])
    p.add_argument("--rebuild", action="store_true",
                   help="清空重建（回收孤儿向量；全量编码，6 万块约 5~7 分钟）")
    p.set_defaults(fn=cmd_index)

    p = sub.add_parser("search", help="混合检索")
    p.add_argument("query")
    p.add_argument("--top-k", type=int, default=rc.DEFAULT_TOP_K)
    p.add_argument("--auto-k", action="store_true", help="按问题复杂度自适应 top-k")
    p.add_argument("--uid", help="限定单个条目")
    p.add_argument("--kinds", help="限定内容块类型，逗号分隔")
    p.add_argument("--since", help="起始时间：7d / 30d / last_week / 2026-01-01")
    p.add_argument("--until", help="结束时间，省略表示到现在")
    p.add_argument("--time-field", default="saved_at",
                   choices=["saved_at", "published_at"])

    p.add_argument("--min-duration", type=int, metavar="秒", help="最短时长")
    p.add_argument("--max-duration", type=int, metavar="秒", help="最长时长")
    p.add_argument("--min-blocks", type=int, help="内容块数下限（弹幕/评论多 = 块多）")
    p.add_argument("--max-blocks", type=int, help="内容块数上限")
    p.add_argument("--recency", type=float, default=0.0,
                   help="0~1 的近期加权强度，0 不启用")
    p.add_argument("--no-rerank", action="store_true")
    p.add_argument("--deep", action="store_true",
                   help="深查：把口语查询译成术语化表达再审一次（每查询多一次 LLM 调用，"
                        "实测 R@10 +23pt；默认关）")
    p.add_argument("--partitioned", action="store_true",
                   help="分区检索：按块间独立度分组（段落/短句/字幕行），"
                        "各自归一化后融合。推荐用于话题型查询")
    p.add_argument("--granularity", "-g", default="block",
                   choices=["block", "seg", "item", "topic"],
                   help="结果粒度：block=哪句话（默认）/ item=哪个视频（自动聚合+附摘要）"
                        "/ topic=按主题归组")
    p.add_argument("--show-source", action="store_true", help="显示来源视频")
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser("graph", help="知识图谱边（图扩展）")
    p.add_argument("--build", action="store_true", help="建边")
    p.add_argument("--vault", help="Obsidian vault 路径（读双链）")
    p.add_argument("--subdir", default="B站")
    p.add_argument("--expand", metavar="UID", help="从条目做多跳扩展")
    p.add_argument("--hops", type=int, default=2)
    p.add_argument("--stats", action="store_true")
    p.set_defaults(fn=cmd_graph)

    sub.add_parser("stats", help="统计").set_defaults(fn=cmd_stats)
    sub.add_parser("adapter", help="列出平台适配器").set_defaults(fn=cmd_adapter)

    a = ap.parse_args()
    a.fn(a)

if __name__ == "__main__":
    main()
