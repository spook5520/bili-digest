#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os as _os
import sys as _sys

from bili_digest import paths

import json
import sqlite3
import sys
from pathlib import Path
from bili_digest.paths import DB

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).parent

from bili_digest.schema.core_schema import render_budgeted, connect, now_sec

DDL = """
CREATE TABLE IF NOT EXISTS synthesis (
    id         INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL,     -- report / contradictions / questions / actions
    scope      TEXT,              -- 作用域描述（如 tier=knowledge 或 topic=Frida）
    content    TEXT NOT NULL,     -- JSON
    sources    TEXT,              -- JSON: [item_uid, ...] 证据链
    model      TEXT,
    created_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_syn_kind ON synthesis(kind, created_at);
"""

def ensure_table(con) -> None:
    con.executescript(DDL)
    con.commit()

def gather(con, tier: str = "knowledge", topic: str = None,
           limit: int = 40, uids: list = None) -> list[dict]:
    sql = """
        SELECT i.uid, i.title, i.author_name, i.duration_sec, l.tier,
               d.kind dk, d.content dc
        FROM item i
        JOIN derived d ON d.item_uid = i.uid
        LEFT JOIN lifecycle l ON l.item_uid = i.uid
        WHERE d.kind IN ('summary','key_points','topics','insights','scores')
    """
    args = []
    if uids:
        ph = ",".join("?" * len(uids))
        sql += f" AND i.uid IN ({ph})"
        args.extend(uids)
    if tier:
        sql += " AND l.tier = ?"
        args.append(tier)

    sql += " ORDER BY COALESCE(i.saved_at,0) DESC"

    by_uid = {}
    for r in con.execute(sql, args):
        e = by_uid.setdefault(r["uid"], {
            "uid": r["uid"], "title": r["title"], "author": r["author_name"],
            "tier": r["tier"], "summary": None, "key_points": [],
            "topics": [], "insights": [], "scores": None})
        try:
            v = json.loads(r["dc"])
        except Exception:
            continue
        k = r["dk"]
        if k == "summary":
            e["summary"] = v
        elif k in ("key_points", "topics", "insights"):
            e[k] = v or []
        elif k == "scores":
            e["scores"] = v

    out = list(by_uid.values())
    if topic:

        try:
            import jieba
            toks = [w.strip() for w in jieba.cut(topic) if len(w.strip()) >= 2]
        except Exception:
            toks = [w for w in topic.split() if len(w) >= 2]
        low_topic = topic.lower()
        if toks:
            def _hit(e, _toks=toks, _low=low_topic):
                blob = " ".join([
                    str(e["title"] or ""),
                    " ".join(str(x) for x in (e["topics"] or [])),
                    str(e["summary"] or ""),
                ]).lower()
                return (_low in blob
                        or any(w.lower() in blob for w in _toks))
            out = [e for e in out if _hit(e)]
        else:
            t = low_topic
            out = [e for e in out
                   if t in str(e["title"]).lower()
                   or any(t in str(x).lower() for x in e["topics"])
                   or t in str(e["summary"]).lower()]

    out.sort(key=lambda e: (-(e["scores"] or {}).get("mean", 0), ))
    return out[:limit]

def render_materials(items: list[dict], max_chars: int = 3000) -> str:
    lines = []
    for i, e in enumerate(items, 1):
        sc = e["scores"] or {}
        head = f"[{i}] {e['title']}（{e['author'] or '?'}"
        if sc.get("mean") is not None:
            head += f" · 细滤{sc['mean']}"
        head += "）"
        block = [head]
        if e["summary"]:
            block.append(f"    摘要：{str(e['summary'])[:150]}")
        if e["topics"]:
            block.append(f"    主题：{'/'.join(str(x) for x in e['topics'][:6])}")
        for ins in (e["insights"] or [])[:3]:
            claim = ins.get("claim") if isinstance(ins, dict) else str(ins)
            if claim:
                block.append(f"    洞察：{str(claim)[:120]}")
        lines.append("\n".join(block))
    rendered, _used, _dropped = render_budgeted(
        lines, max_chars,
        tail_note="  …（另有 {n} 条未列入）")
    return rendered

PROMPTS = {
    "report": """下面是知识库里的一批条目素材（摘要/主题/洞察/细滤评分）。

{materials}

请只输出 JSON：
{{
  "overview": "3~5 句话概括这批内容整体在讲什么、有什么脉络",
  "themes": [{{"name": "主题名", "items": [编号], "note": "该主题下的共同点或进展"}}],
  "gaps": ["这批内容明显没覆盖到的方向"]
}}
要求：只依据素材；themes 的 items 用条目编号；中文。""",

    "contradictions": """下面是知识库里的条目素材。

{materials}

请找出其中互相矛盾或结论不一致的地方。只输出 JSON：
{{
  "contradictions": [
    {{"topic": "分歧点", "side_a": {{"claim": "一方观点", "items": [编号]}},
      "side_b": {{"claim": "另一方观点", "items": [编号]}},
      "note": "可能的原因（如适用场景不同、时间不同、前提不同）"}}
  ]
}}
要求：
1. 只在素材确有不一致时输出；没有就给空数组，不要硬凑
2. 区分「真矛盾」与「不同场景下的不同结论」，后者要在 note 里说明
3. 中文。""",

    "questions": """下面是知识库里的条目素材。

{materials}

基于这些材料，指出哪些问题目前还回答不了。只输出 JSON：
{{
  "questions": [
    {{"question": "待验证的问题", "why": "为什么现有材料答不了",
      "related": [编号], "how_to_verify": "可以通过什么方式验证"}}
  ]
}}
要求：问题要具体、可验证，不要泛泛而问；中文。""",

    "actions": """下面是知识库里的条目素材。

{materials}

提炼可以直接动手做的行动项。只输出 JSON：
{{
  "actions": [
    {{"action": "具体可执行的动作", "basis": [编号], "effort": "low/medium/high"}}
  ]
}}
要求：
1. 只列出素材明确支持的动作，不要凭空建议
2. 动作要具体（"用 Frida hook 某个函数" 而不是 "学习逆向"）
3. 中文。""",
}

def _llm(prompt: str, timeout: int = 180) -> str:
    from bili_digest.distill import extract
    return extract.call_llm(prompt, timeout=timeout)

def synthesize(con, kind: str = "report", tier: str = "knowledge",
               topic: str = None, limit: int = 25, verbose: bool = True,
               uids: list = None) -> dict:
    if kind not in PROMPTS:
        return {"error": f"未知类型 {kind}，可选 {list(PROMPTS)}"}
    ensure_table(con)

    items = gather(con, tier=tier, topic=topic, limit=limit, uids=uids)
    if not items:
        return {"error": "没有可用素材（需先跑 derive 提炼）"}

    mats = render_materials(items)
    if verbose:
        print(f"  素材 {len(items)} 条，输入 {len(mats)} 字")

    raw = _llm(PROMPTS[kind].format(materials=mats))
    try:
        j = json.loads(raw)
    except Exception as e:
        return {"error": f"LLM 输出非 JSON: {str(e)[:60]}"}

    idx2uid = {i: e["uid"] for i, e in enumerate(items, 1)}
    used = set()

    def _collect_refs(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in ("items", "related", "basis") and isinstance(v, list):
                    for n in v:
                        if n in idx2uid:
                            used.add(idx2uid[n])
                else:
                    _collect_refs(v)
        elif isinstance(obj, list):
            for x in obj:
                _collect_refs(x)

    _collect_refs(j)

    from bili_digest.distill import extract
    _k, info = extract._load_api_key()
    scope = topic or f"tier={tier}"
    con.execute("""
        INSERT INTO synthesis(kind,scope,content,sources,model,created_at)
        VALUES(?,?,?,?,?,?)
    """, (kind, scope, json.dumps(j, ensure_ascii=False),
          json.dumps(sorted(used)), info.split("|", 1)[1], now_sec()))

    if kind == "questions":
        items = j if isinstance(j, list) else (j.get("questions") or [])
        try:
            from bili_digest.distill import annotate
            annotate.ensure_table(con)
        except Exception:
            pass
        n_new = 0
        for it in items:
            q = (it.get("question") if isinstance(it, dict) else str(it) or "").strip()
            if not q:
                continue
            how = it.get("why") if isinstance(it, dict) else None

            cur = con.execute(
                "INSERT OR IGNORE INTO open_question(item_uid,question,how,status,"
                "created_at) VALUES(?,?,?,?,?)", (None, q, how, "open", now_sec()))
            n_new += cur.rowcount
        if n_new:
            j["_new_questions"] = n_new
    con.commit()
    j["_sources"] = sorted(used)
    return j

def show(con, kind: str = None, limit: int = 3):
    ensure_table(con)
    sql = "SELECT * FROM synthesis"
    args = []
    if kind:
        sql += " WHERE kind=?"
        args.append(kind)
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit)
    rows = con.execute(sql, args).fetchall()
    if not rows:
        print("  还没有合成结果")
        return
    for r in rows:
        print(f"\n  【{r['kind']}】 {r['scope']}  "
              f"{__import__('datetime').datetime.fromtimestamp(r['created_at']):%Y-%m-%d %H:%M}")
        try:
            j = json.loads(r["content"])
        except Exception:
            print(f"    {r['content'][:200]}")
            continue
        print(f"    {json.dumps(j, ensure_ascii=False, indent=2)[:900]}")

def add_cli_args(p, *, with_db: bool = True) -> None:
    if with_db:
        p.add_argument("--db", default=str(DB))
    p.add_argument("--report", action="store_true", help="周期综述")
    p.add_argument("--contradictions", action="store_true", help="矛盾点")
    p.add_argument("--questions", action="store_true", help="待验证问题")
    p.add_argument("--actions", action="store_true", help="行动项")
    p.add_argument("--topic", help="限定主题（匹配标题/主题词/摘要）")
    p.add_argument("--tier", default="knowledge", help="作用分级；空字符串表示不限")
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--kind", help="看历史结果时限定类型（report/contradictions 等）")
    p.add_argument("--show", action="store_true", help="看历史合成结果")
    return p

def run(a, con) -> dict:
    kinds = [k for k, flag in (("report", a.report), ("contradictions", a.contradictions),
                               ("questions", a.questions), ("actions", a.actions)) if flag]
    if a.show:

        show(con, a.kind or (kinds[0] if kinds else None))
        return {"shown": True}
    if not kinds:
        print("  用法：--report | --contradictions | --questions | --actions "
              "[--topic X] [--tier knowledge] | --show")
        return {}
    out = {}
    for k in kinds:
        print(f"\n=== {k} ===")
        r = synthesize(con, k, tier=a.tier or None, topic=a.topic, limit=a.limit)
        if r.get("error"):
            print(f"  {r['error']}")
        else:
            print(f"  {json.dumps(r, ensure_ascii=False, indent=2)[:1400]}")
        out[k] = r
    return out

def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="反刍合成（综述/矛盾/问题/行动）")
    add_cli_args(ap)
    a = ap.parse_args(argv)
    con = connect(a.db)
    con.row_factory = sqlite3.Row
    try:
        run(a, con)
    finally:
        con.close()

if __name__ == "__main__":
    main()
