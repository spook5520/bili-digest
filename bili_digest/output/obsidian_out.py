#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os as _os
import re
import sys as _sys

from bili_digest import paths

import json
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

from bili_digest.schema.core_schema import (KIND_COMMENT, KIND_DANMAKU, KIND_SUBTITLE,
                         KIND_SUBTITLE_SEG, connect)

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

_SHARED_VI = {}

def shared_vi():
    if "vi" not in _SHARED_VI:
        try:
            from bili_digest.retrieve import rag_core as _rc
            from bili_digest.paths import VEC as _VEC
            _SHARED_VI["vi"] = _rc.VectorIndex(str(_VEC), readonly=True)
        except Exception:
            _SHARED_VI["vi"] = None
    return _SHARED_VI["vi"]

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

    title = title.replace("#", "＃")

    title = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", title)
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

_TECH_TAG = re.compile(r"^(any_|kb_)|已清理|已修复|串轨|重建")

def build_frontmatter(item: dict, budget: dict, tags: list[str],
                      collections: list[str], topics: list = None,
                      prov: dict = None) -> str:

    lines = ["---"]
    lines.append(f"uid: {yaml_escape(item['uid'])}")
    lines.append(f"title: {yaml_escape(item.get('title') or '')}")
    if item.get("author_name"):
        lines.append(f"author: {yaml_escape(item['author_name'])}")
    if item.get("published_at"):
        lines.append(f"published: {_iso(item['published_at'])}")
    if item.get("saved_at"):
        lines.append(f"saved: {_iso(item['saved_at'])}")
    if item.get("duration_sec"):
        lines.append(f"duration: {item['duration_sec']}")

    try:
        _ex = item.get("extras")
        _d = json.loads(_ex) if isinstance(_ex, str) else (_ex or {})

        _v = _d.get("view") if _d.get("view") is not None else _d.get("play")
        if _v is not None:
            lines.append(f"views: {int(_v)}")
    except Exception:
        pass
    if collections:
        lines.append("collections:")
        for c in collections:
            lines.append(f"  - {yaml_escape(c)}")

    if tags:
        lines.append("tags: " + yaml_escape(" ".join(tags)))
    if topics:
        lines.append("topics:" + "".join(
            "\n  - " + yaml_escape(t) for t in topics))

    if prov:
        if prov.get("who"):
            lines.append(f"gen_model: {yaml_escape(prov['who'])}")

        if prov.get("hash"):
            lines.append(f"gen_src_hash: {yaml_escape(prov['hash'])}")
        if prov.get("what"):
            lines.append(f"gen_transcript: {yaml_escape(prov['what'])}")
        if prov.get("outcome") != "":
            lines.append(f"gen_tx_ratio: {prov['outcome']}")
        if prov.get("where"):
            lines.append(f"gen_refs: {yaml_escape(prov['where'])}")
        if prov.get("evidence_basis"):
            lines.append(f"evidence_basis: {yaml_escape(prov['evidence_basis'])}")
        if prov.get("status"):
            lines.append(f"status: {prov['status']}")

        if prov.get("community") is not None:
            lines.append(f"community: {prov['community']}")
        if prov.get("comm_label"):
            lines.append(f"comm_label: {yaml_escape(prov['comm_label'])}")
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
    if kind in (KIND_SUBTITLE, KIND_SUBTITLE_SEG, KIND_DANMAKU) \
            and block.get("offset_sec") is not None:
        m, s = divmod(int(block["offset_sec"]), 60)
        tag = {KIND_SUBTITLE: "字幕", KIND_SUBTITLE_SEG: "段落",
               KIND_DANMAKU: "弹幕"}.get(kind, "段落")
        return f"[{tag} {m:02d}:{s:02d}]"
    if kind == KIND_COMMENT:
        w = int(block.get("weight") or 0)
        who = block.get("author") or "匿名"
        return f"[评论 👍{w} {who}]"
    return ""

def _proc_record(con, item_uid: str, blocks: list) -> list:
    L = ["> [!note]- 处理记录（点开看：这条是怎么来的）"]
    try:
        rows = con.execute(
            "SELECT kind, model, created_at FROM derived WHERE item_uid=? "
            "ORDER BY created_at DESC", (item_uid,)).fetchall()
    except Exception:
        rows = []
    models = sorted({r[1] for r in rows if r[1]})
    last = max([r[2] or 0 for r in rows], default=0)
    L.append("> 模型：%s" % ("、".join(models) if models else "无（该条没有 LLM 提炼记录）"))
    if last:
        L.append("> 提炼于：%s" % _iso(last))

    n_by = {}
    for b in blocks:
        n_by[b["kind"]] = n_by.get(b["kind"], 0) + 1
    name = {"subtitle_seg": "段落", "comment": "评论", "danmaku": "弹幕",
            "summary": "摘要", "meta": "元信息", "annotation": "批注"}
    parts = ["%s %d" % (name.get(k, k), v) for k, v in
             sorted(n_by.items(), key=lambda x: -x[1])[:6]]
    L.append("> 规模：%s" % (" · ".join(parts) if parts else "无内容块"))

    by_id = {b["id"]: b for b in blocks}

    cites = []
    try:
        from bili_digest.distill import extract as _ex
        for ins in (_ex.get_derived(con, item_uid).get("insights") or []):
            if isinstance(ins, dict):
                cites += ins.get("cites") or []
    except Exception:
        pass
    if cites:
        st = _cite_state(cites, by_id, con=con, item_uid=item_uid)
        L.append("> 引用：%d 条 · 可回查 %d · 重新对齐 %d · 仅编号 %d · 失效 %d · 错位 %d"
                 % (st["total"], len(st["ok"]), len(st["real"]), len(st["unk"]),
                    st["missing"], st["mismatch"]))
    else:
        L.append("> 引用：0 条（该条提炼没有给引用）")

    todo = []
    if cites and (st["missing"] or st["mismatch"]):
        if st["missing"]:
            todo.append("%d 条引用在原库中已找不到（块被重建过，引用未同步）" % st["missing"])
        if st["mismatch"]:
            todo.append("%d 条引用的引文与当前块对不上（可能指错了地方）" % st["mismatch"])
    if not models:
        todo.append("没有 LLM 提炼 —— 笔记只有原文与预算声明")
    L.append("> 未解决：%s" % ("；".join(todo) if todo else "无"))
    return L

def _provenance(con: sqlite3.Connection, item_uid: str) -> dict:
    out = {"who": "", "when": "", "what": "", "outcome": "", "where": ""}
    try:
        r = con.execute(
            "SELECT model, MAX(created_at) ts FROM derived WHERE item_uid=? "
            "AND model IS NOT NULL AND model != ''", (item_uid,)).fetchone()
        if r:
            out["who"] = (r[0] or "").strip()
            out["when"] = int(r[1] or 0)
        kinds = {}
        for k, n in con.execute(
                "SELECT kind, COUNT(*) FROM content_block WHERE item_uid=? GROUP BY kind",
                (item_uid,)):
            kinds[k] = n

        _tx = kinds.get("subtitle", 0) + kinds.get("subtitle_seg", 0)
        _cm = kinds.get("comment", 0) + kinds.get("danmaku", 0)
        out["what"] = "has-transcript" if _tx else "no-transcript"

        if _tx and _cm:
            out["evidence_basis"] = "mixed"
        elif _tx:
            out["evidence_basis"] = "transcript"
        elif _cm:
            out["evidence_basis"] = "comment-only"
        else:
            out["evidence_basis"] = "none"
        out["n_tx"] = _tx
        out["n_cm"] = _cm

        try:
            _has_sum = con.execute(
                "SELECT 1 FROM derived WHERE item_uid=? AND kind IN ('summary','topics') LIMIT 1",
                (item_uid,)).fetchone() is not None
        except Exception:
            _has_sum = False
        _sup = ""
        try:
            _r2 = con.execute(
                "SELECT 1 FROM supersede WHERE item_uid=? LIMIT 1",
                (item_uid,)).fetchone()
            if _r2:
                _sup = "archived"
        except Exception:
            pass
        if _sup:
            out["status"] = _sup
        elif _has_sum:
            out["status"] = "derived"
        elif _tx or _cm:
            out["status"] = "raw"
        else:
            out["status"] = "raw"

        try:
            _cr = con.execute(
                "SELECT comm_id, size, rep_terms FROM community WHERE item_uid=?",
                (item_uid,)).fetchone()
            if _cr:
                out["community"] = _cr[0]
                out["comm_size"] = _cr[1]
                try:
                    _terms = json.loads(_cr[2] or "[]")
                except Exception:
                    _terms = []
                out["comm_label"] = (_terms[0] if _terms else "")
        except Exception:
            pass

        tot = sum(kinds.values()) or 1
        out["outcome"] = round(100.0 * (kinds.get("subtitle_seg", 0)
                                        + kinds.get("subtitle", 0)) / tot, 1)

        n_ref = con.execute(
            "SELECT COUNT(*) FROM derived WHERE item_uid=? AND kind='insights'",
            (item_uid,)).fetchone()[0]
        out["where"] = ("refs:%d" % n_ref) if n_ref else "no-refs"
    except Exception:
        pass
    return out

def _collect_note_context(con: sqlite3.Connection, item_uid: str) -> dict:
    row = con.execute("SELECT * FROM item WHERE uid=?", (item_uid,)).fetchone()
    if not row:
        raise SystemExit(f"知识库中没有 {item_uid}")
    ctx = {"item": {k: row[k] for k in row.keys()}}

    ctx["prov"] = _provenance(con, item_uid)
    ctx["blocks"] = [{k: r[k] for k in r.keys()} for r in con.execute(
        "SELECT id,kind,offset_sec,seq,text,author,weight FROM content_block "
        "WHERE item_uid=?", (item_uid,))]

    try:
        ctx["proc_lines"] = _proc_record(con, item_uid, ctx["blocks"])
    except Exception:
        ctx["proc_lines"] = []
    ctx["tags"] = [r[0] for r in con.execute(
        "SELECT name FROM tag WHERE item_uid=?", (item_uid,))
        if not _TECH_TAG.search(r[0])]
    ctx["colls"] = [r[0] for r in con.execute(
        "SELECT c.name FROM collection c JOIN collection_member m "
        "ON m.collection_id=c.id WHERE m.item_uid=?", (item_uid,))]

    try:
        ctx["moc_topics"] = {r[0] for r in con.execute(
            "SELECT topic FROM topic_member GROUP BY topic HAVING COUNT(*) >= 3")}
    except sqlite3.OperationalError:
        ctx["moc_topics"] = set()

    try:
        from bili_digest.output import supersede as _sp
        _hit = _sp.find(con, item_uid)
        ctx["supersede_lines"] = _sp.render_lines(con, _hit) if _hit else []
    except Exception:
        ctx["supersede_lines"] = []
    return ctx

def _cite_state(cites, by_id, con=None, item_uid=None) -> dict:
    from bili_digest.quality import fix_cites as _fc
    from bili_digest.distill import extract as _ex
    out = {"ok": [], "real": [], "unk": [], "healed": [],
           "missing": 0, "mismatch": 0, "total": 0}
    for c in (cites if isinstance(cites, list) else []):
        out["total"] += 1
        cid, q = _fc._id_of(c), _fc._quote_of(c)
        b = by_id.get(cid)
        healed = False
        if b is None and con is not None:
            try:
                row = con.execute("SELECT hash FROM cite_hash WHERE block_id=?",
                                  (cid,)).fetchone()
                if row and row[0]:
                    nid = con.execute(
                        "SELECT id FROM content_block WHERE item_uid=? AND hash=? LIMIT 1",
                        (item_uid, row[0])).fetchone()
                    if nid and nid[0] in by_id:
                        b, healed = by_id[nid[0]], True
            except Exception:
                pass
        if b is None:
            out["missing"] += 1
            continue

        _off = b.get("offset_sec")
        ts = ("%02d:%02d" % divmod(int(_off), 60)) if _off is not None else ""

        if healed:
            out["healed"].append(ts)
        if not q:
            out["unk"].append(ts)
        elif q in (b["text"] or ""):
            if isinstance(c, dict) and c.get("realign"):
                out["real"].append(ts)
            else:
                out["ok"].append(ts)
        else:
            out["mismatch"] += 1
    return out

def _cite_note(st: dict) -> str:
    if not (st["real"] or st["unk"] or st["missing"] or st["mismatch"]):
        return ""
    bits = []
    if st["mismatch"]:
        bits.append("%d 条错位（id 在、但引文不是该块的原文）" % st["mismatch"])
    if st["missing"]:
        bits.append("%d 条失效（该 id 在库里已无对应块）" % st["missing"])
    if st["real"]:
        bits.append("%d 条由系统重新对齐（标 ↻，引文取自该块原文，但不是当初写的那句）"
                     % len(st["real"]))
    if st["unk"]:
        bits.append("%d 条只有 id 没有引文，无法自证（标 ?）" % len(st["unk"]))
    if st["healed"]:
        bits.append("%d 条按内容哈希找回（块被重建、id 变了）" % len(st["healed"]))
    return ("*引用核对：共 %d 条，%d 条原始可回查；%s。*"
            % (st["total"], len(st["ok"]), "；".join(bits)))

def _plan_sections(ctx: dict, provided: dict, con=None) -> list[tuple]:
    from bili_digest.distill import extract as _ex
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

    if ctx.get("supersede_lines"):
        plan.append((None, ctx["supersede_lines"], "raw"))

    if provided.get("summary"):
        plan.append(("摘要", [provided["summary"]], "h2"))

    if provided.get("topics"):

        _moc = ctx.get("moc_topics") or set()
        parts = [(f"[[{t}]]" if t in _moc else f"#{t}") for t in provided["topics"]]
        plan.append(("主题", [" · ".join(parts)], "h2"))

    if provided.get("key_points"):

        lines = []
        for p in provided["key_points"]:
            if isinstance(p, dict):
                txt = p.get("point") or ""

                _qs = [str(c.get("quote") or "").strip() for c in (p.get("cites") or [])
                       if isinstance(c, dict) and c.get("quote")]
                _q0 = next((q for q in _qs if q), "")
                if _q0:
                    _q0 = " ".join(_q0.split())[:26]
                    lines.append(f"- {txt}　<small>〔{_q0}…〕</small>")
                else:

                    lines.append(f"- 〔推〕{txt}")
            else:

                lines.append(f"- 〔推〕{p}")

        plan.append(("知识要点", lines, "h2"))

    if con is not None and blocks:
        try:

            from bili_digest.output.chapter import chapters_semantic as _ch
            _ch_list = _ch(con, item.get("uid") or "")
            if _ch_list:
                lines = []
                for c in _ch_list:
                    _m, _s = divmod(c["t"], 60)
                    lines.append("- `%02d:%02d` %s" % (_m, _s, c["text"]))
                plan.append(("章节", lines, "h2"))
        except Exception:
            pass

    if provided.get("insights"):
        by_id = {b["id"]: b for b in blocks}
        lines = []
        n_bad = n_all = n_ok = n_unk = n_real = 0
        agg = {"total": 0, "ok": [], "real": [], "unk": [], "healed": [],
               "missing": 0, "mismatch": 0}
        for it in provided["insights"]:
            claim = it.get("claim") if isinstance(it, dict) else str(it)
            if not claim:
                continue

            _cs = (it.get("cites") or []) if isinstance(it, dict) else []
            _q = chr(96)
            st = _cite_state(_cs, by_id, con=con, item_uid=ctx["item"].get("uid"))
            agg["total"] += st["total"]
            agg["ok"] += st["ok"]
            agg["real"] += st["real"]
            agg["unk"] += st["unk"]
            agg["healed"] += st["healed"]
            agg["missing"] += st["missing"]
            agg["mismatch"] += st["mismatch"]
            tags = list(st["ok"]) + ["↻" + t for t in st["real"]] + [t + "?" for t in st["unk"]]
            line = "- " + claim

            _ts_keep = list(st.get("ok") or []) + ["↻" + t for t in (st.get("real") or [])]
            if _ts_keep:
                line += "  " + _q + " ".join(_ts_keep) + _q
            if st["mismatch"]:
                line += "  " + _q + "⚠ %d 条错位" % st["mismatch"] + _q
            if st["missing"]:
                line += "  " + _q + "✗ %d 条失效" % st["missing"] + _q
            lines.append(line)
        if lines:
            note = _cite_note(agg)
            if note:
                lines = [note, ""] + lines
            plan.append(("洞察", lines, "h2"))

    ev_ids = provided.get("evidence_ids") or []
    if ev_ids:
        by_id = {b["id"]: b for b in blocks}
        lines = []
        missing = 0
        for i, bid in enumerate(ev_ids, 1):
            b = by_id.get(bid)
            if b:
                lines.append(f"[{i}] {fmt_loc(b)} {_clip(b['text'])}{_anchor(b['id'])}")
            else:

                missing += 1
        alive = len(lines)
        if missing:
            lines.append(f"*（另有 {missing} 条引用在原库中已找不到 —— "
                         f"块被重建过，引用未同步）*")
        if alive:
            _q = chr(96)
            lines = ["*行尾的 %s^b<id>%s 是块锚点：%s[[本笔记#^b<id>]]%s 与检索返回的 block 是同一个地址，可从笔记跳回被命中的那一段。*" % (_q, _q, _q, _q), ""] + lines
            plan.append(("证据", lines, "h2"))
        else:

            if ev_ids:
                plan.append((None, ["*本条的 %d 条引用指向的块已被重建删除，无法回查 —— "
                                    "重新提炼可恢复，否则忽略本行。*" % missing], "raw"))
            _append_top_comments(plan, blocks)
    else:
        _append_top_comments(plan, blocks)

    if provided.get("related"):
        lines = []
        for group, names in provided["related"].items():
            if names:

                lines.append(f"**{group}**：" + " ".join(f"[[{n}]]" for n in names[:8]))
                lines.append("")
        if lines:
            plan.append(("相关笔记", lines, "h2"))

    if provided.get("review", True):
        from bili_digest.output import review_sheet as _rv
        rl = _rv.build_review_section(ctx, provided)
        if rl:
            plan.append(("复习", rl, "h2"))

    return plan

def _anchor(block_id) -> str:

    if block_id in (None, ""):
        return ""
    return " ^b%s" % block_id

def _clip(text, limit: int = 240) -> str:

    t = " ".join(str(text or "").split())
    return t if len(t) <= limit else t[:limit].rstrip() + "……"

def _append_top_comments(plan: list, blocks: list, limit: int = 5) -> None:
    top_cm = sorted([b for b in blocks if b["kind"] == KIND_COMMENT],
                    key=lambda x: -(x.get("weight") or 0))[:limit]
    if top_cm:
        plan.append(("高赞评论",
                     [f"- {fmt_loc(b)} {_clip(b['text'], 200)}{_anchor(b['id'])}"
                      for b in top_cm], "h2"))

def _render_note(ctx: dict, budget: dict, provided: dict,
                 plan: list, archive_dir: str = None,
                 con: sqlite3.Connection = None) -> str:
    fm = build_frontmatter(ctx["item"], budget, ctx["tags"], ctx["colls"],
                          provided.get("topics"), ctx.get("prov"))
    L = [fm, "", f"# {ctx['item'].get('title') or '未命名'}"]

    vd = budget["visual_dependency"]

    for title, lines, level in plan:
        if level == "h2":
            L += ["", f"## {title}", ""]
        L += lines

    _sec = ""
    if con is not None:
        from bili_digest.distill import annotate as _an
        _sec = (_an.render_section(con, ctx["item"]["uid"]) or "").strip()

    if _sec and not any(ln.lstrip().startswith("- ")
                        for ln in _sec.splitlines()):
        _sec = ""
    if _sec:
        L.append(_sec)
    else:

        L += ["", "> [!note]- 我的思考", "> 写你的判断、疑问、待办"]

    if any(" ^b" in x for x in L):
        _q2 = chr(96)
        L.append("*行尾的 %s^b<id>%s 是块锚点：用 %s[[本笔记名#^b<id>]]%s 可从别处跳回这一行；"
                 "它与检索返回的 block id 是同一个地址。*" % (_q2, _q2, _q2, _q2))
        L.append("")

    L += ["", "---", ""]
    if vd["coverage"] == "sparse":
        L += [CALLOUT_SPARSE.format(cps=vd["chars_per_sec"]).rstrip(), ""]

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
               archive_dir: str | None = None,
               review: bool = True) -> tuple[str, dict]:
    ctx = _collect_note_context(con, item_uid)
    budget = compute_budget(ctx["item"], ctx["blocks"])
    provided = {"summary": summary, "key_points": key_points,
                "insights": insights, "topics": topics,
                "evidence_ids": evidence_ids, "related": related,
                "review": review}
    plan = _plan_sections(ctx, provided, con=con)
    text = _render_note(ctx, budget, provided, plan, archive_dir,
                          con=con)
    return text, budget

def collect_derived(con: sqlite3.Connection, item_uid: str) -> dict:
    try:
        from bili_digest.distill import extract as _ex
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

def _merge_user_notes(old: str, new: str) -> str:
    import re as _re
    if not old:
        return new
    pat = ("^" + _re.escape("> [!note]- 我的思考") + _re.escape(chr(10))
           + "((?:> .*" + _re.escape(chr(10)) + ")*)")
    m = _re.search(pat, old, _re.M)

    if m:
        body = m.group(1).rstrip(chr(10))
        if body.strip() and body.strip() != "> 写你的判断、疑问、待办":
            old_head = "> [!note]- 我的思考" + chr(10) + "> 写你的判断、疑问、待办"
            new_head = "> [!note]- 我的思考" + chr(10) + body
            new = new.replace(old_head, new_head, 1)

    def _sectionOf(text):
        m = _re.search(r"^## 我的思考[ \t]*$\n(.*?)(?=^## |^---$|\Z)",
                       text, _re.M | _re.S)
        return m

    om = _sectionOf(old)
    nm = _sectionOf(new)
    if om and nm:
        keep = om.group(1).strip(chr(10))
        if not keep.strip():
            return new
        return new[:nm.start(1)] + keep + chr(10) + new[nm.end(1):]
    if om and not nm:
        keep = om.group(1).strip(chr(10))
        if keep.strip():
            return (new.rstrip(chr(10)) + chr(10) + chr(10)
                    + "## 我的思考" + chr(10) + keep + chr(10))
    return new

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
    pid = row["item_id"] or ""
    if pid and not path.exists():
        suffix = " (%s).md" % pid
        try:
            for old in outdir.glob("*.md"):
                if old.name.endswith(suffix) and old.name != path.name:
                    path = old
                    break
        except OSError:
            pass

    if path.exists():
        try:
            text = _merge_user_notes(path.read_text(encoding="utf-8"), text)
        except OSError:
            pass
    path.write_text(text, encoding="utf-8")

    try:
        from bili_digest.quality import budget as _bg
        _bg.record(con, actor="publish", step="publish", rows=1,
                   scope=item_uid, detail=str(path.name)[:80])
    except Exception:
        pass
    return path

_BIDIR_CACHE = {}

def related_bidirectional(con, limit: int = 8, force: bool = False) -> dict:

    import json as _json
    from bili_digest.paths import DATA_DIR as _DD
    _cf = _DD / "related_bidir.json"
    try:
        _dbm = (Path(__file__).parent.parent.parent / "knowledge.db")
        _dbm = _dbm.stat().st_mtime if _dbm.exists() else 0
    except OSError:
        _dbm = 0
    if not force and _cf.exists():
        try:
            _raw = _json.loads(_cf.read_text(encoding="utf-8"))
            if _raw.get("db_mtime") == _dbm and _raw.get("limit") == limit:
                _BIDIR_CACHE.clear()
                _BIDIR_CACHE.update(_raw.get("map") or {})
                return _BIDIR_CACHE
        except (ValueError, OSError):
            pass
    if _BIDIR_CACHE and not force:
        return _BIDIR_CACHE

    uids = [r[0] for r in con.execute(
        "SELECT uid FROM item WHERE platform='bilibili'")]
    own = {}
    picks = {}
    for u in uids:
        row = con.execute("SELECT title,item_id FROM item WHERE uid=?", (u,)).fetchone()
        me = note_filename({"title": row["title"] if row else "",
                            "item_id": row["item_id"] if row else ""})[:-3]
        own[u] = me
        try:
            r = suggest_related(con, u, limit=limit)
        except Exception:
            picks[u] = set()
            continue
        s = set()
        for grp in ("语义相关", "同一 UP", "共享主题"):
            for it in (r.get(grp) or []):
                if isinstance(it, str) and it:
                    s.add(it)
        picks[u] = s
    out = {}
    for u, s in picks.items():
        me = own.get(u)
        keep = [v for v in s if me and me in picks.get(
            next((x for x in uids if own.get(x) == v), None), ())]
        if keep:
            out[u] = keep
    _BIDIR_CACHE.clear()
    _BIDIR_CACHE.update(out)
    try:
        _cf.write_text(_json.dumps({"db_mtime": _dbm, "limit": limit, "map": out},
                                   ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return out

def suggest_related(con: sqlite3.Connection, item_uid: str,
                    limit: int = 8) -> dict:
    row = con.execute("SELECT author_uid,author_name FROM item WHERE uid=?",
                      (item_uid,)).fetchone()

    out = {}
    if not row:
        return out

    def link(title, pid):
        return note_filename({"title": title, "item_id": pid})[:-3]

    from bili_digest.graph import graph as _gp

    _LABEL = (("same_author", "同一 UP"), ("shared_topic", "共享主题"))

    _seen = set()

    try:
        from bili_digest.retrieve import rag_core as _rc
        from bili_digest.paths import VEC as _VEC
        _q = (row["title"] if "title" in row.keys() else "") or ""
        if not _q:
            _r = con.execute("SELECT title FROM item WHERE uid=?", (item_uid,)).fetchone()
            _q = (_r[0] if _r else "") or ""
        if _q:

            _vi = shared_vi()
            if _vi is None:
                raise RuntimeError("索引不可用")

            _hits = _rc.hybrid(con, _vi, _q, top_k=24)
            _picked = []
            for _h in _hits:
                _u = _h.get("item_uid") or ""
                if not _u or _u == item_uid:
                    continue
                _pid = _u.split(":")[-1]
                if _pid in _seen:
                    continue
                _seen.add(_pid)
                _picked.append(link(_h.get("title"), _pid))

                if len(_picked) >= 8:
                    break
            if _picked:
                out["语义相关"] = _picked
    except Exception:
        pass

    for kind, label in _LABEL:
        try:
            nbs = _gp.neighbors(con, item_uid, kinds=[kind], limit=limit)
        except Exception:
            nbs = []
        picked = []
        for n in nbs:
            pid = n["dst"].split(":")[-1]
            if pid in _seen:
                continue
            _seen.add(pid)
            picked.append(link(n["title"], pid))
        if picked:
            out[label] = picked

    if not out:
        if row["author_uid"]:
            for r in con.execute(
                    "SELECT i.title,i.item_id FROM item i "
                    "WHERE i.author_uid=? AND i.uid!=? LIMIT ?",
                    (row["author_uid"], item_uid, limit)):
                out.setdefault("同一 UP", []).append(link(r[0], r[1]))
        for r in con.execute(
                "SELECT DISTINCT i.title,i.item_id FROM tag t "
                "JOIN tag t2 ON t2.name=t.name AND t2.item_uid!=t.item_uid "
                "JOIN item i ON i.uid=t2.item_uid "
                "WHERE t.item_uid=? LIMIT ?", (item_uid, limit)):
            out.setdefault("同标签", []).append(link(r[0], r[1]))

    return out

def write_moc(con: sqlite3.Connection, vault: str | Path, min_members: int = 3,
              outdir: str = "_主题") -> dict:
    vault = Path(vault)
    out = vault / outdir
    out.mkdir(parents=True, exist_ok=True)
    try:
        rows = con.execute(
            "SELECT topic, COUNT(*) c FROM topic_member GROUP BY topic "
            "HAVING c >= ? ORDER BY c DESC", (min_members,)).fetchall()
    except sqlite3.OperationalError:
        return {"written": 0, "topics": {}}

    _EXCLUDE = {"知识科普", "科普", "教程"}
    _before = len(rows)
    rows = [r for r in rows if r[0] not in _EXCLUDE]
    if _before != len(rows):
        print("  跳过 %d 个兜底/形式型 topic" % (_before - len(rows)))

    def safe(name: str) -> str:

        return re.sub(r'[\\/:*?"<>|]', "_", name).strip() or "未命名主题"

    written = 0
    topics = {}
    for topic, cnt in rows:
        topics[topic] = cnt
        members = con.execute(
            "SELECT i.title, i.item_id FROM topic_member m "
            "JOIN item i ON i.uid=m.item_uid WHERE m.topic=? "
            "ORDER BY i.saved_at DESC", (topic,)).fetchall()
        if not members:
            continue

        stat = con.execute(
            "SELECT date(MIN(i.saved_at),'unixepoch','localtime') a, "
            "date(MAX(i.saved_at),'unixepoch','localtime') b FROM topic_member m "
            "JOIN item i ON i.uid=m.item_uid WHERE m.topic=?", (topic,)).fetchone()
        tags = con.execute(
            "SELECT t.name, COUNT(*) c FROM topic_member m "
            "JOIN tag t ON t.item_uid=m.item_uid WHERE m.topic=? "
            "GROUP BY t.name HAVING c >= 2 ORDER BY c DESC LIMIT 4", (topic,)).fetchall()
        L = ["---", "type: moc", "topic: %s" % yaml_escape(topic),
             "members: %d" % cnt, "---", "", "# %s" % topic, "", "## 速览", ""]
        span = ""
        if stat and stat[0] and stat[1]:
            span = " · %s ~ %s" % (stat[0], stat[1])
        L.append("**%d 条视频**%s" % (cnt, span))
        if tags:
            L.append("**高频标签**：" + " · ".join("%s(%d)" % (n, c) for n, c in tags))
        L += ["", "## 相关视频（%d）" % cnt, ""]

        _sum = {}
        try:
            _ids = ["bilibili:" + pid for _, pid in members if pid]
            for i in range(0, len(_ids), 400):
                _chunk = _ids[i:i + 400]
                _ph = ",".join("?" * len(_chunk))
                for r in con.execute(
                        "SELECT item_uid, content FROM derived WHERE kind='summary' "
                        "AND item_uid IN (%s)" % _ph, _chunk):
                    _c = r["content"]

                    try:
                        _c = json.loads(_c) if isinstance(_c, str) else _c
                    except (ValueError, TypeError):
                        pass
                    _sum[r["item_uid"]] = _c if isinstance(_c, str) else str(_c or "")
        except Exception:
            pass

        _comm = {}
        if len(members) > 40:
            try:
                _cids = ["bilibili:" + pid for _, pid in members if pid]
                for _i in range(0, len(_cids), 400):
                    _chunk = _cids[_i:_i + 400]
                    _ph = ",".join("?" * len(_chunk))
                    for r in con.execute(
                            "SELECT item_uid, size, rep_terms FROM community "
                            "WHERE item_uid IN (%s)" % _ph, _chunk):
                        try:
                            _t = json.loads(r["rep_terms"] or "[]")
                        except Exception:
                            _t = []
                        _comm[r["item_uid"]] = (_t[0] if _t else "") or "未分组"
            except Exception:
                _comm = {}

        if _comm:
            _grp = {}
            for _it in members:
                _lab = _comm.get("bilibili:" + (_it[1] or ""), "未分组")
                _grp.setdefault(_lab, []).append(_it)

            _none_items = _grp.pop("未分组", [])
            _order = sorted(_grp.items(), key=lambda kv: -len(kv[1]))
            _head, _tail = _order[:6], _order[6:]
            if _none_items:
                _tail = list(_tail) + [("未分组", _none_items)]
            if _tail:
                _head.append(("其他（%d 组共 %d 条）" % (len(_tail), sum(len(x[1]) for x in _tail)),
                              [it for _l, its in _tail for it in its]))
            L += ["", "*按社区簇分成 %d 组 —— 每组是「一起出现的那些视频」*" % len(_head)]
            _walk = [(lab, its) for lab, its in _head]
        else:
            _walk = [(None, members)]

        for _lab, _items in _walk:
            if _lab is not None:
                L += ["", "### %s（%d 条）" % (_lab, len(_items)), ""]
            for t, pid in _items:
                L.append("- [[%s]]" % note_filename({"title": t, "item_id": pid})[:-3])
            _s = _sum.get("bilibili:" + (pid or "")) or ""

            import re as _re
            _m = _re.search(r"(围绕|主打|主要|讲解|介绍|演示|盘点|对比|分析|拆解|展示|"
                            r"讨论|梳理|记录|分享|论证|复盘|测试|测评)", _s)
            if _m and _m.start() > 6:
                _s = _s[_m.start():]
            if _s:

                _s = " ".join(str(_s).split())
                for _p in ("。", "？", "！", ". "):
                    _i = _s.find(_p)
                    if 0 < _i < 60:
                        _s = _s[:_i + 1]
                        break
                else:
                    _s = _s[:60]
                L.append("  " + _s)

        rel = con.execute(
            "SELECT m2.topic, COUNT(*) c FROM topic_member m1 "
            "JOIN topic_member m2 ON m2.item_uid = m1.item_uid "
            "AND m2.topic != m1.topic WHERE m1.topic = ? "
            "GROUP BY m2.topic HAVING c >= 3 ORDER BY c DESC LIMIT 6",
            (topic,)).fetchall()
        rel = [(t, c) for t, c in rel if t in topics]
        if rel:
            L += ["", "## 相关主题", ""]
            L.append(" · ".join(f"[[{safe(t)}]]（共 {c}）" for t, c in rel))

        p = out / (safe(topic) + ".md")
        p.write_text("\n".join(L) + "\n", encoding="utf-8")
        written += 1

    _alive = {safe(t) for t in topics}
    _removed = []
    for _f in out.glob("*.md"):
        if _f.stem.startswith("_") or _f.stem in _alive:
            continue
        try:
            _f.unlink()
            _removed.append(_f.stem)
        except OSError:
            pass
    if _removed:
        print("  清理 %d 个已无成员的主题文件：%s"
              % (len(_removed), " / ".join(_removed[:6])))

    return {"written": written, "topics": topics, "removed": _removed}

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
