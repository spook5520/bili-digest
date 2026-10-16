#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
import paths

import re
import sqlite3
import sys
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from core_schema import (KIND_COMMENT, KIND_DANMAKU, KIND_SUBTITLE,
                         connect)

BUDGET_TIERS = [
    (300,     200,   500, 12),
    (900,     400,   900, 15),
    (1800,    700,  1500, 18),
    (3600,   1200,  2500, 22),
    (10**9,  1800,  4000, 28),
]

DENSITY_COMPLETE = 2.0
DENSITY_PARTIAL = 1.0

CALLOUT_SPARSE = (
    "> [!warning] 覆盖度声明\n"
    "> 本视频字幕密度偏低（{cps:.2f} 字/秒，正常解说约 2.5），"
    "核心信息可能位于画面、PPT、板书或代码演示中。\n"
    "> 本笔记仅覆盖字幕与互动内容，未包含视觉信息，"
    "如需完整理解请参照原视频。\n"
)

def yaml_escape(v) -> str:
    s = str(v)
    if not s:
        return '""'
    if re.search(r"[:#\[\]{}&*!|>'\"%@`]|^\s|\s$", s):
        return '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"'
    return s

def note_filename(item: dict) -> str:
    title = (item.get("title") or "未命名").strip()
    title = re.sub(r'[\\/:*?"<>|]', "_", title)
    title = title.replace("[", "【").replace("]", "】")
    title = re.sub(r"\s+", " ", title).strip()
    if len(title) > 80:
        title = title[:80].rstrip()
    pid = item.get("item_id") or ""
    return f"{title} ({pid}).md" if pid else f"{title}.md"

def compute_budget(item: dict, blocks: list[dict]) -> dict:
    dur = max(item.get("duration_sec") or 0, 1)

    sub = [b for b in blocks if b["kind"] == "subtitle_seg"]
    if not sub:
        sub = [b for b in blocks if b["kind"] == KIND_SUBTITLE]
    dms = [b for b in blocks if b["kind"] == KIND_DANMAKU]
    cms = [b for b in blocks if b["kind"] == KIND_COMMENT]

    sub_chars = sum(len(b["text"]) for b in sub)
    cm_chars = sum(len(b["text"]) for b in cms)
    dm_chars = sum(len(b["text"]) for b in dms)
    total_chars = sub_chars + cm_chars + dm_chars

    lo, hi, ratio = BUDGET_TIERS[-1][1:]
    for limit, l, h, r in BUDGET_TIERS:
        if dur <= limit:
            lo, hi, ratio = l, h, r
            break

    if total_chars < 500:
        lo, hi = int(lo * 0.4), int(hi * 0.5)
    elif total_chars < 2000:
        lo, hi = int(lo * 0.7), int(hi * 0.8)

    hi_cm = len([b for b in cms if (b.get("weight") or 0) >= 20])
    quality_mult = min(1.0 + hi_cm / 20.0, 1.5)
    hi = int(hi * quality_mult)

    cps = sub_chars / dur
    if cps >= DENSITY_COMPLETE:
        coverage = "complete"
    elif cps >= DENSITY_PARTIAL:
        coverage = "partial"
    else:
        coverage = "sparse"

    return {
        "duration_sec": dur,
        "subtitle_chars": sub_chars,
        "subtitle_count": len(sub),
        "danmaku_count": len(dms),
        "comment_count": len(cms),
        "high_like_comments": hi_cm,
        "total_chars": total_chars,
        "word_budget": [lo, hi],
        "compression_target": ratio,
        "actual_compression": round(total_chars / max(hi, 1), 1),
        "visual_dependency": {
            "chars_per_sec": round(cps, 2),
            "coverage": coverage,
            "needs_visual_review": coverage == "sparse",
            "warnings": (
                ["字幕密度低，核心信息可能在画面"]
                if coverage == "sparse" else
                (["字幕密度偏低，建议核对画面"] if coverage == "partial" else [])
            ),
        },
    }

def build_frontmatter(item: dict, budget: dict, tags: list[str],
                      collections: list[str]) -> str:
    lines = ["---"]
    lines.append(f"uid: {yaml_escape(item['uid'])}")
    lines.append(f"platform: {yaml_escape(item.get('platform') or '')}")
    lines.append(f"item_id: {yaml_escape(item.get('item_id') or '')}")
    lines.append(f"title: {yaml_escape(item.get('title') or '')}")
    if item.get("author_name"):
        lines.append(f"author: {yaml_escape(item['author_name'])}")
    if item.get("author_uid"):
        lines.append(f"author_uid: {yaml_escape(item['author_uid'])}")
    if item.get("url"):
        lines.append(f"source: {yaml_escape(item['url'])}")
    if item.get("published_at"):
        lines.append(f"published: {_iso(item['published_at'])}")
    if item.get("saved_at"):
        lines.append(f"saved: {_iso(item['saved_at'])}")
    if item.get("duration_sec"):
        lines.append(f"duration: {item['duration_sec']}")
    lines.append(f"coverage: {budget['visual_dependency']['coverage']}")
    lines.append(f"content_chars: {budget['total_chars']}")
    lines.append(f"word_budget: [{budget['word_budget'][0]}, {budget['word_budget'][1]}]")
    if collections:
        lines.append("collections:")
        for c in collections:
            lines.append(f"  - {yaml_escape(c)}")
    if tags:
        lines.append("tags:")
        for t in tags:
            lines.append(f"  - {yaml_escape(t)}")
    lines.append("---")
    return "\n".join(lines)

def _iso(ts) -> str:
    import datetime
    try:
        return datetime.datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d")
    except Exception:
        return ""

def fmt_loc(block: dict) -> str:
    kind = block["kind"]
    if kind in (KIND_SUBTITLE, KIND_DANMAKU) and block.get("offset_sec") is not None:
        m, s = divmod(int(block["offset_sec"]), 60)
        tag = "字幕" if kind == KIND_SUBTITLE else "弹幕"
        return f"[{tag} {m:02d}:{s:02d}]"
    if kind == KIND_COMMENT:
        w = int(block.get("weight") or 0)
        who = block.get("author") or "匿名"
        return f"[评论 👍{w} {who}]"
    return ""

def _collect_note_context(con: sqlite3.Connection, item_uid: str) -> dict:
    row = con.execute("SELECT * FROM item WHERE uid=?", (item_uid,)).fetchone()
    if not row:
        raise SystemExit(f"知识库中没有 {item_uid}")
    ctx = {"item": {k: row[k] for k in row.keys()}}
    ctx["blocks"] = [{k: r[k] for k in r.keys()} for r in con.execute(
        "SELECT id,kind,offset_sec,seq,text,author,weight FROM content_block "
        "WHERE item_uid=?", (item_uid,))]
    ctx["tags"] = [r[0] for r in con.execute(
        "SELECT name FROM tag WHERE item_uid=?", (item_uid,))]
    ctx["colls"] = [r[0] for r in con.execute(
        "SELECT c.name FROM collection c JOIN collection_member m "
        "ON m.collection_id=c.id WHERE m.item_uid=?", (item_uid,))]
    return ctx

def _plan_sections(ctx: dict, provided: dict) -> list[tuple]:
    import extract as _ex
    item, blocks = ctx["item"], ctx["blocks"]
    plan = []

    bits = []
    if item.get("author_name"):
        bits.append(f"UP：{item['author_name']}")
    if item.get("duration_sec"):
        m, s = divmod(item["duration_sec"], 60)
        bits.append(f"时长：{m}:{s:02d}")
    if item.get("published_at"):
        bits.append(f"发布：{_iso(item['published_at'])}")
    if item.get("saved_at"):
        bits.append(f"收藏：{_iso(item['saved_at'])}")
    if item.get("url"):
        bits.append(f"[原视频]({item['url']})")
    if bits:
        plan.append((None, [" · ".join(bits)], "raw"))

    if provided.get("summary"):
        plan.append(("摘要", [provided["summary"]], "h2"))

    if provided.get("topics"):
        plan.append(("主题",
                     [" · ".join(f"#{t}" for t in provided["topics"])], "h2"))

    if provided.get("key_points"):

        lines = []
        for p in provided["key_points"]:
            if isinstance(p, dict):
                txt = p.get("point") or ""

                cs = _ex.cite_ids(p.get("cites"))
                lines.append(f"- {txt}" + (f"　`证据 {', '.join(map(str, cs))}`" if cs else ""))
            else:
                lines.append(f"- {p}")
        plan.append(("知识要点", lines, "h2"))

    if provided.get("insights"):
        by_id = {b["id"]: b for b in blocks}
        lines = []
        for it in provided["insights"]:
            claim = it.get("claim") if isinstance(it, dict) else str(it)
            if not claim:
                continue
            refs = []
            for cid in (it.get("cites") or []) if isinstance(it, dict) else []:
                b = by_id.get(cid)
                if b:
                    m, s = divmod(int(b["offset_sec"] or 0), 60)
                    refs.append(f"{m:02d}:{s:02d}")
            lines.append(f"- {claim}" + (f"  `{' '.join(refs)}`" if refs else ""))
        if lines:
            plan.append(("洞察", lines, "h2"))

    ev_ids = provided.get("evidence_ids") or []
    if ev_ids:
        by_id = {b["id"]: b for b in blocks}
        lines = []
        missing = 0
        for i, bid in enumerate(ev_ids, 1):
            b = by_id.get(bid)
            if b:
                lines.append(f"[{i}] {fmt_loc(b)} {b['text']}")
            else:

                missing += 1
        if missing:
            lines.append(f"*（另有 {missing} 条引用在原库中已找不到 —— "
                         f"块被重建过，引用未同步）*")
        if lines:
            plan.append(("证据", lines, "h2"))
    else:

        top_cm = sorted([b for b in blocks if b["kind"] == KIND_COMMENT],
                        key=lambda x: -(x.get("weight") or 0))[:5]
        if top_cm:
            plan.append(("高赞评论",
                         [f"- {fmt_loc(b)} {b['text'][:200]}" for b in top_cm], "h2"))

    if provided.get("related"):
        lines = []
        for group, names in provided["related"].items():
            if names:
                lines.append(f"**{group}**：" + " ".join(f"[[{n}]]" for n in names[:8]))
                lines.append("")
        if lines:
            plan.append(("相关笔记", lines, "h2"))

    return plan

def _render_note(ctx: dict, budget: dict, provided: dict,
                 plan: list, archive_dir: str = None) -> str:
    fm = build_frontmatter(ctx["item"], budget, ctx["tags"], ctx["colls"])
    L = [fm, "", f"# {ctx['item'].get('title') or '未命名'}"]

    vd = budget["visual_dependency"]
    if vd["coverage"] == "sparse":
        L += ["", CALLOUT_SPARSE.format(
            cps=vd["chars_per_sec"]).rstrip()]

    for title, lines, level in plan:
        if level == "h2":
            L += ["", f"## {title}", ""]
        L += lines

    try:
        import annotate as _an
        L.append(_an.render_section(ctx["item"]["uid"]))
    except Exception:
        L += ["", "## 我的思考", "", "<!-- 写你的判断、疑问、待办 -->"]

    L += ["", "---", ""]
    L.append(f"*笔记预算 {budget['word_budget'][0]}~{budget['word_budget'][1]} 字"
             f"（时长 {budget['duration_sec']}s，原文 {budget['total_chars']} 字，"
             f"目标压缩比 {budget['compression_target']}:1）*")
    if vd["warnings"]:
        L.append(f"*覆盖度 {vd['coverage']}：{'；'.join(vd['warnings'])}*")
    if archive_dir:
        L += ["", f"原始材料：`{archive_dir}`"]
    return "\n".join(L) + "\n"

def build_note(con: sqlite3.Connection, item_uid: str,
               related: dict[str, list[str]] | None = None,
               summary: str | None = None,
               key_points: list[str] | None = None,
               insights: list[dict] | None = None,
               topics: list[str] | None = None,
               evidence_ids: list[int] | None = None,
               archive_dir: str | None = None) -> tuple[str, dict]:
    ctx = _collect_note_context(con, item_uid)
    budget = compute_budget(ctx["item"], ctx["blocks"])
    provided = {"summary": summary, "key_points": key_points,
                "insights": insights, "topics": topics,
                "evidence_ids": evidence_ids, "related": related}
    plan = _plan_sections(ctx, provided)
    text = _render_note(ctx, budget, provided, plan, archive_dir)
    return text, budget

def collect_derived(con: sqlite3.Connection, item_uid: str) -> dict:
    try:
        import extract as _ex
        _ex.ensure_table(con)
        d = _ex.get_derived(con, item_uid) or {}
    except Exception:
        return {}

    ids = []
    for ins in (d.get("insights") or []):
        for c in _ex.cite_ids(ins.get("cites")):
            if c not in ids:
                ids.append(c)
    d["evidence_ids"] = ids[:8]
    return d

def write_note(con: sqlite3.Connection, item_uid: str, vault: str | Path,
               subdir: str = "B站", **kw) -> Path:
    vault = Path(vault)
    outdir = vault / subdir
    outdir.mkdir(parents=True, exist_ok=True)

    row = con.execute("SELECT title,item_id FROM item WHERE uid=?",
                      (item_uid,)).fetchone()
    if not row:
        raise SystemExit(f"知识库中没有 {item_uid}")

    text, budget = build_note(con, item_uid, **kw)
    path = outdir / note_filename({"title": row["title"], "item_id": row["item_id"]})
    path.write_text(text, encoding="utf-8")
    return path

def suggest_related(con: sqlite3.Connection, item_uid: str,
                    limit: int = 8) -> dict:
    row = con.execute("SELECT author_uid,author_name FROM item WHERE uid=?",
                      (item_uid,)).fetchone()
    out = {"同UP": [], "同标签": []}
    if not row:
        return out

    def link(title, pid):
        return note_filename({"title": title, "item_id": pid})[:-3]

    if row["author_uid"]:
        for r in con.execute(
                "SELECT i.title,i.item_id FROM item i "
                "WHERE i.author_uid=? AND i.uid!=? LIMIT ?",
                (row["author_uid"], item_uid, limit)):
            out["同UP"].append(link(r[0], r[1]))

    for r in con.execute(
            "SELECT DISTINCT i.title,i.item_id FROM tag t "
            "JOIN tag t2 ON t2.name=t.name AND t2.item_uid!=t.item_uid "
            "JOIN item i ON i.uid=t2.item_uid "
            "WHERE t.item_uid=? LIMIT ?", (item_uid, limit)):
        out["同标签"].append(link(r[0], r[1]))

    return out

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="输出 Obsidian 笔记")
    ap.add_argument("db", nargs="?", default="knowledge.db")
    ap.add_argument("--vault", required=False, help="Obsidian vault 路径")
    ap.add_argument("--subdir", default="B站")
    ap.add_argument("--uid", help="只输出某条")
    ap.add_argument("--budget", metavar="UID", help="只看预算，不写文件")
    a = ap.parse_args()

    c = connect(a.db)
    if a.budget:
        row = c.execute("SELECT * FROM item WHERE uid=?", (a.budget,)).fetchone()
        if not row:
            raise SystemExit("没有该条目")
        blocks = [{k: r[k] for k in r.keys()} for r in c.execute(
            "SELECT id,kind,offset_sec,seq,text,author,weight FROM content_block "
            "WHERE item_uid=?", (a.budget,))]
        b = compute_budget({k: row[k] for k in row.keys()}, blocks)
        for k, v in b.items():
            print(f"  {k:<20} {v}")
    elif a.uid and a.vault:
        d = collect_derived(c, a.uid)
        p = write_note(c, a.uid, a.vault, a.subdir,
                       related=suggest_related(c, a.uid),
                       summary=d.get("summary"),
                       key_points=d.get("key_points"),
                       insights=d.get("insights"),
                       topics=d.get("topics"),
                       evidence_ids=d.get("evidence_ids"))
        print(f"  已写入 {p}")
    else:
        print("用法：--budget UID 查预算；--uid UID --vault PATH 输出笔记")
    c.close()
