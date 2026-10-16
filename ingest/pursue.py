#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
import paths

import argparse
import json
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).parent

STATE = HERE / "_pursue_state.json"

AUTHOR_HINT = re.compile(r"austin", re.I)

TOPIC = re.compile(
    r"普查|水文|有多少|多少人|多少发|到底(?:在|是)?哪|去哪|来自哪|往哪|"
    r"合规|违法|犯罪|算犯罪|符合.{0,4}吗|真实吗|几分真|够真实|"
    r"边界|河流|水系|电网|供水|供电|机场|考据|考究|失业|经济",
    re.I)
GAME = re.compile(
    r"GTA|自由城|洛圣都|荒野大镖客|镖客|redemption|辐射|fallout|"
    r"赛博朋克|2077|夜之城|塞尔达|天际|上古卷轴|最后生还者|巫师|"
    r"塔科夫|燕云|传送门|portal|我的世界|minecraft", re.I)

FAKE = re.compile(
    r"平台|项目|框架|开源|面试|Java|企业级|消息推送|组件|"
    r"UT\s*Austin|教授|大学|校园|芯片|学位|实验室|论文",
    re.I)

def grade(cand: dict, known_ups: set) -> str:
    t = cand.get("title") or ""
    author = cand.get("author") or ""
    up = cand.get("up") or ""
    srcs = cand.get("sources") or []

    if AUTHOR_HINT.search(author) or (AUTHOR_HINT.search(t) and not FAKE.search(t)):
        return "A"
    if up in known_ups:
        return "B"
    if any("相关推荐" in s for s in srcs) and TOPIC.search(t) and GAME.search(t):
        return "B"
    if TOPIC.search(t) and GAME.search(t):
        return "C"
    return "D"

def known_carriers(cand: dict) -> set:
    out = set()
    for c in cand.values():
        t = c.get("title") or ""
        a = c.get("author") or ""
        if (AUTHOR_HINT.search(a) or AUTHOR_HINT.search(t)) and c.get("up"):
            out.add(c["up"])
    return out

def main():
    ap = argparse.ArgumentParser(description="一条龙：线索 → 发现 → 入库 → 提炼 → 建边")
    ap.add_argument("--seed", help="线索：BV 号")
    ap.add_argument("--keyword", help="线索：关键词")
    ap.add_argument("--tag", default=None, help="标签名（默认从线索推导）")
    ap.add_argument("--budget", type=int, default=60, help="发现阶段最多请求数")
    ap.add_argument("--rounds", type=int, default=3, help="发现阶段展开轮数")
    ap.add_argument("--cooldown", type=int, default=12, help="入库阶段每条间隔秒")
    ap.add_argument("--levels", default="AB", help="入库哪些级别（默认 AB）")
    ap.add_argument("--dry-run", action="store_true", help="只发现，不入库")
    ap.add_argument("--skip-distill", action="store_true")
    ap.add_argument("--quiet", action="store_true", help="只写日志")
    ap.add_argument("--report", default=None, help="报告写到文件")
    a = ap.parse_args()

    if not a.seed and not a.keyword:
        print("  需要 --seed 或 --keyword")
        return 1

    tag = a.tag or (a.seed or a.keyword).replace(" ", "_").lower()
    log_lines = []

    def say(msg):
        log_lines.append(f"{time.strftime('%H:%M:%S')}  {msg}")
        if not a.quiet:
            print(f"  {msg}")

    t0 = time.time()
    say(f"═══ pursue 开始 · 线索={a.seed or a.keyword} · tag={tag} ═══")

    say("① 发现（chain 展开）")
    import bili_digest as bd
    import chain as ch
    ck = bd.cookie_header(bd.load_credential())
    c = ch.Chain(ck, budget=a.budget, cooldown=3.0, verbose=not a.quiet)
    if a.seed:
        c.by_meta(a.seed)
    else:
        c.by_keyword(a.keyword)
    for rnd in range(1, a.rounds + 1):
        feats = c.features()
        if not feats["authors"] and not feats["patterns"]:
            break
        before = len(c.seen)
        c.expand_unexpanded(limit=6)
        feats = c.features()
        for _n, author in feats["authors"][:3]:
            c.by_keyword(author)
            for _m, pat in feats["patterns"][:4]:
                w = re.sub(r"[^\u4e00-\u9fffA-Za-z]", "", pat)
                if 2 <= len(w) <= 8:
                    c.by_keyword(f"{author} {w}")
        for _m, pat in feats["patterns"][:4]:
            w = re.sub(r"[^\u4e00-\u9fffA-Za-z]", "", pat)
            if 2 <= len(w) <= 6:
                c.by_keyword(w)
        if len(c.seen) == before:
            say(f"  轮 {rnd}：无新增，收敛")
            break
        say(f"  轮 {rnd}：+{len(c.seen)-before} 条（累计 {len(c.seen)}）")

    cand = {bv: {**r, "sources": sorted(r["sources"])} for bv, r in c.seen.items()}
    say(f"  候选 {len(cand)} 条 · {c.calls} 次请求")

    say("② 分级")
    up_set = known_carriers(cand)
    levels = defaultdict(list)
    for bv, r in cand.items():
        levels[grade(r, up_set)].append(bv)
    say(f"  A={len(levels['A'])} B={len(levels['B'])} "
        f"C={len(levels['C'])} D={len(levels['D'])}")

    pick_levels = set(a.levels.upper())
    picked = []
    for lv in ("A", "B", "C", "D"):
        if lv in pick_levels:
            picked += [(bv, lv) for bv in levels[lv]]
    say(f"  按 --levels={a.levels} 选中 {len(picked)} 条")

    from core_schema import connect
    from paths import DB
    con = connect(DB)
    con.row_factory = __import__("sqlite3").Row
    exist = {r["item_id"] for r in con.execute("SELECT item_id FROM item")}
    new = [(bv, lv) for bv, lv in picked if bv not in exist]

    if a.seed and a.seed not in exist:
        new.insert(0, (a.seed, "seed"))
        say(f"  seed {a.seed} 纳入入库（它是最相关的）")

    say(f"  差量：{len(picked)-len(new)} 条已在库 · {len(new)} 条待入库")

    Path(f"pursue_{tag}_candidates.json").write_text(
        json.dumps({"levels": {k: v for k, v in levels.items()},
                    "detail": cand}, ensure_ascii=False, indent=1),
        encoding="utf-8")

    if a.dry_run:
        say("--dry-run：到此为止")
        _finish(tag, log_lines, a, t0, levels, new)
        return 0

    if not new:
        say("没有新条目要入库")
        _finish(tag, log_lines, a, t0, levels, new)
        return 0

    say(f"③ 入库 {len(new)} 条")
    import core_schema as cs
    import refine
    from adapters import BilibiliAdapter
    adapter = BilibiliAdapter()
    ok = fail = 0
    for n, (bv, lv) in enumerate(new, 1):
        try:
            meta = adapter.fetch_item(bv)
            uid = cs.upsert_item(
                con, platform=meta["platform"], item_id=meta["item_id"],
                title=meta.get("title"), url=meta.get("url"),
                author_uid=meta.get("author_uid"),
                author_name=meta.get("author_name"),
                published_at=meta.get("published_at"),
                saved_at=meta.get("saved_at"),
                duration_sec=meta.get("duration_sec"),
                language=meta.get("language"), cover=meta.get("cover"),
                extras={**(meta.get("extras") or {}),
                        "source": "pursue", "grade": lv, "tag": tag})
            con.commit()
            refine.step_fetch(bv, sources="meta,sub,dm,reply")
            res = refine.step_import(con, bv)

            blk = res.get("blocks") if isinstance(res, dict) else None
            n_b = sum(v for v in blk.values() if isinstance(v, int)) if isinstance(blk, dict) else 0
            con.execute("INSERT OR REPLACE INTO tag(item_uid,name,kind,source) "
                        "VALUES(?,?,?,?)", (uid, tag, "system", "pursue"))
            con.commit()
            say(f"  [{n}/{len(new)}] {bv}[{lv}] {str(meta.get('title'))[:34]} 块={n_b}")
            ok += 1
        except Exception as e:
            say(f"  [{n}/{len(new)}] {bv} 失败 {type(e).__name__}: {str(e)[:50]}")
            fail += 1
        if n < len(new):
            time.sleep(a.cooldown)
    say(f"  入库: 成功 {ok} · 失败 {fail}")

    if not a.skip_distill:
        say("④ 提炼")
        rows = con.execute("""SELECT t.item_uid, i.item_id FROM tag t
                              LEFT JOIN item i ON i.uid=t.item_uid
                              WHERE t.name=? AND i.item_id IS NOT NULL""",
                           (tag,)).fetchall()
        d_ok = d_fail = 0
        for n, r in enumerate(rows, 1):
            n_derived = con.execute("SELECT COUNT(*) n FROM derived WHERE item_uid=?",
                                    (r["item_uid"],)).fetchone()["n"]
            if n_derived:
                continue
            try:
                if refine.step_extract(con, r["item_uid"]):
                    d_ok += 1
                else:
                    d_fail += 1
            except Exception as e:
                say(f"  {r['item_id']} 提炼异常 {type(e).__name__}")
                d_fail += 1
            time.sleep(2)
        say(f"  提炼: 成功 {d_ok} · 素材不足/失败 {d_fail}")

        say("  分段 + 建索引")
        say(f"  {refine.step_segment()}")
        say(f"  {refine.step_index()}")

    say("  建边")
    import graph as gp
    import ingest_chain as ic
    n_shared = ic.build_shared_tag(con)
    n_author = gp.build_same_author(con)
    say(f"  shared_tag {n_shared} · same_author {n_author}")

    con.close()
    _finish(tag, log_lines, a, t0, levels, new)
    return 0

def _finish(tag, log_lines, a, t0, levels, new):
    el = time.time() - t0
    log_lines.append(f"═══ 完成 · 耗时 {el/60:.1f} 分钟 ═══")
    Path(f"pursue_{tag}.log").write_text("\n".join(log_lines), encoding="utf-8")
    if a.report:
        rep = [
            f"# pursue 报告 · {tag}",
            "",
            f"- 线索: {a.seed or a.keyword}",
            f"- 候选分级: " + " · ".join(
                f"{k}={len(v)}" for k, v in sorted(levels.items())),
            f"- 入库: {len(new)} 条",
            f"- 耗时: {el/60:.1f} 分钟",
            "",
            "## 入库清单",
            "",
        ] + [f"- `{bv}` [{lv}]" for bv, lv in new]
        Path(a.report).write_text("\n".join(rep), encoding="utf-8")
        print(f"  报告: {a.report}")
    print(f"  日志: pursue_{tag}.log")

if __name__ == "__main__":
    sys.exit(main())
