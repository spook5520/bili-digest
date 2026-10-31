#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os as _os
import sys as _sys

from bili_digest import paths

import json
import re
import sqlite3
import sys
from pathlib import Path
from bili_digest.paths import DB

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).parent

from bili_digest.schema.core_schema import connect, now_sec

SECTION = "## 我的思考"

DDL = """
CREATE TABLE IF NOT EXISTS annotation (
    id         INTEGER PRIMARY KEY,
    item_uid   TEXT NOT NULL,
    kind       TEXT DEFAULT 'note',   -- note / question / todo
    text       TEXT NOT NULL,
    created_at INTEGER,
    updated_at INTEGER,
    UNIQUE(item_uid, kind, text)
);
CREATE INDEX IF NOT EXISTS idx_anno_item ON annotation(item_uid);

-- 待验证问题（由 synthesize --questions 产出，人可标记已解决）
CREATE TABLE IF NOT EXISTS open_question (
    id         INTEGER PRIMARY KEY,
    item_uid   TEXT,
    question   TEXT NOT NULL,
    how        TEXT,
    status     TEXT DEFAULT 'open',   -- open / answered / dropped
    created_at INTEGER,
    UNIQUE(question)
);
"""

def ensure_table(con) -> None:
    con.executescript(DDL)
    con.commit()

def render_section(con, item_uid: str) -> str:
    ensure_table(con)
    rows = con.execute(
        "SELECT kind,text FROM annotation WHERE item_uid=? ORDER BY created_at",
        (item_uid,)).fetchall()
    qs = con.execute(
        "SELECT question,how,status FROM open_question WHERE item_uid=? AND status='open'",
        (item_uid,)).fetchall()

    L = ["", SECTION, ""]
    if rows:
        for r in rows:
            mark = {"todo": "- [ ] ", "question": "- ? ", "note": "- "}.get(r["kind"], "- ")
            L.append(f"{mark}{r['text']}")
    else:
        L.append("<!-- 在这里写你的判断、疑问、待办；")
        L.append("     跑 `annotate --sync --vault <vault>` 会回写到库，")
        L.append("     之后重新生成笔记也不会丢 -->")
    if qs:
        L += ["", "### 待验证", ""]
        for q in qs:
            L.append(f"- [ ] {q['question']}"
                     + (f"（{q['how']}）" if q["how"] else ""))
    return "\n".join(L)

def parse_section(text: str) -> str:
    i = text.find(SECTION)
    if i < 0:
        return ""
    rest = text[i + len(SECTION):]
    lines = []
    for line in rest.splitlines():
        s = line.strip()
        if s.startswith("## "):
            break
        if s == "---" or (s.startswith("*") and s.endswith("*")):
            break
        lines.append(line)
    return "\n".join(lines).strip()

def _clean_lines(body: str) -> list[tuple[str, str]]:
    out = []
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("<!--") or line.startswith("###"):
            continue

        if line in ("-->", "---", "--") or line.startswith("-->"):
            continue
        if line.startswith("- [ ]"):
            out.append(("todo", line[5:].strip()))
            continue
        if line.startswith("- [x]") or line.startswith("- [X]"):
            out.append(("todo_done", line[5:].strip()))
            continue
        if line.startswith("- ?"):

            out.append(("question", line[2:].strip().lstrip("? ").strip()))
            continue
        if line.startswith("-"):
            txt = line[1:].strip()
            if txt and txt not in ("-", "--"):
                out.append(("note", txt))
    return [(k, t) for k, t in out if t]

def sync_from_vault(con, vault: str, subdir: str = "B站",
                    verbose: bool = True) -> dict:
    ensure_table(con)
    root = Path(vault) / subdir
    if not root.exists():
        return {"error": f"{root} 不存在"}

    added, updated, skipped = 0, 0, 0
    for md in sorted(root.rglob("*.md")):
        text = md.read_text(encoding="utf-8", errors="replace")
        uid = None
        m = re.search(r'^uid:\s*"?([^"\n]+)"?', text, re.M)
        if m:
            uid = m.group(1).strip()
        if not uid:
            continue
        body = parse_section(text)
        if not body:
            skipped += 1
            continue

        for kind, txt in _clean_lines(body):
            if kind == "todo_done":

                n = con.execute("UPDATE annotation SET kind='done', updated_at=? "
                                "WHERE item_uid=? AND text=?",
                                (now_sec(), uid, txt)).rowcount
                if n:
                    updated += n
                continue
            try:
                r = con.execute(
                    "INSERT INTO annotation(item_uid,kind,text,created_at,updated_at) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(item_uid,kind,text) DO NOTHING",
                    (uid, kind, txt, now_sec(), now_sec()))
                if r.rowcount:
                    added += 1
                else:
                    skipped += 1
            except sqlite3.OperationalError as e:
                if verbose:
                    print(f"    [!] {md.name}: {e}")
    con.commit()
    return {"added": added, "updated": updated, "skipped": skipped}

def _norm_uid(item_uid: str) -> str:
    s = str(item_uid or "").strip()
    if not s:
        return s

    if ":" in s:
        return s
    try:
        from bili_digest.paths import bili_uid
        return bili_uid(s)
    except Exception:
        return s

def add_note(con, item_uid: str, text: str, kind: str = "note") -> bool:
    ensure_table(con)
    item_uid = _norm_uid(item_uid)
    try:
        con.execute("INSERT INTO annotation(item_uid,kind,text,created_at,updated_at) "
                    "VALUES(?,?,?,?,?)", (item_uid, kind, text, now_sec(), now_sec()))
        con.commit()
        return True
    except sqlite3.IntegrityError:
        return False

def add_conclusion(con, text: str, sources: list[str]) -> tuple[bool, str]:
    if not text.strip():
        return False, "结论为空"
    src = [s for s in (sources or []) if s]
    if not src:
        return False, "需要至少一个来源条目"
    body = text.strip()
    if src:
        body = f"{body}（来源 {'、'.join(src[:6])}）"
    ok = add_note(con, src[0], body, kind="conclusion")
    return ok, ("已记下" if ok else "该结论已存在（同条目+同文本）")

def list_notes(con, limit: int = 20):
    ensure_table(con)
    rows = con.execute("""
        SELECT a.item_uid,a.kind,a.text,i.title FROM annotation a
        LEFT JOIN item i ON i.uid=a.item_uid
        ORDER BY a.updated_at DESC LIMIT ?
    """, (limit,)).fetchall()
    if not rows:
        print("  还没有批注")
        return
    for r in rows:
        print(f"  [{r['kind']:<8}] {str(r['title'] or r['item_uid'])[:40]}")
        print(f"             {r['text'][:100]}")

def add_cli_args(p, *, with_db: bool = True) -> None:
    if with_db:
        p.add_argument("--db", default=str(DB))
    p.add_argument("--add", nargs=2, metavar=("UID", "TEXT"), help="加一条批注")
    p.add_argument("--kind", default="note",
                   choices=["note", "question", "todo", "conclusion"])
    p.add_argument("--conclusion", metavar="TEXT",
                   help="记一条跨条目的结论（把检索得到的答案写回库）")
    p.add_argument("--sources", metavar="UIDS",
                   help="结论的来源条目，逗号分隔（与 --conclusion 同用）")
    p.add_argument("--sync", action="store_true", help="从 vault 回读批注")
    p.add_argument("--vault")
    p.add_argument("--subdir", default="B站")
    p.add_argument("--list", action="store_true", help="列出全部批注")
    p.add_argument("--show", metavar="UID", help="预览某条目的「我的思考」段")
    return p

def run(a, con) -> dict:
    if getattr(a, "conclusion", None):
        srcs = [x.strip() for x in (a.sources or "").split(",") if x.strip()]
        ok, msg = add_conclusion(con, a.conclusion, srcs)
        print(f"  {msg}")
        return {"conclusion": ok}
    if a.add:
        uid, text = a.add
        ok = add_note(con, uid, text, a.kind)
        print(f"  {'已添加' if ok else '已存在（重复）'}")
        return {"added": ok}
    if a.sync:
        if not a.vault:
            raise SystemExit("--sync 需要 --vault")
        r = sync_from_vault(con, a.vault, a.subdir)
        print(f"  {json.dumps(r, ensure_ascii=False)}")
        return r
    if a.list:
        list_notes(con)
        return {"listed": True}
    if a.show:
        print(render_section(con, a.show))
        return {"shown": a.show}
    print("  用法：--add UID TEXT | --conclusion TEXT --sources UIDS |"
          " --sync --vault PATH | --list | --show UID")
    return {}

def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="我的思考（批注双向同步）")
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
