#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os as _os
import sys as _sys

from bili_digest import paths

import argparse
import json
import sys
import time
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).parent

from bili_digest.schema.core_schema import block_hash, connect, now_sec
from bili_digest.paths import DB

PROMPT = """下面是一个视频按内容分段后的文本，每段是连续的字幕。

给【每一段】写一句不超过 28 字的摘要，说明这一段实际在讲什么。

要求：
- 保留该段的具体信息（人名、术语、数字、结论），不要泛化成「介绍了某话题」
- 陈述句，不要「本段讲述」「作者认为」这类前缀
- 段号一一对应，不要合并或跳过

只输出 JSON：{"segments": ["1|摘要", "2|摘要", ...]}

分段内容：
{body}"""

SEG_CHARS = 220

MIN_RESULT = 40

def segments_of(con, uid) -> list:
    rows = con.execute("""
        SELECT seq, text FROM content_block
        WHERE item_uid=? AND kind='subtitle_seg'
        ORDER BY COALESCE(seq, 0), id
    """, (uid,)).fetchall()
    return [(r["seq"], (r["text"] or "").strip())
            for r in rows if (r["text"] or "").strip()]

def render(items) -> str:
    parts = []
    for ln in items:
        ln = str(ln).strip()
        if not ln:
            continue
        if "|" in ln:
            idx, desc = ln.split("|", 1)
            parts.append("第%s段：%s" % (idx.strip(), desc.strip()))
        else:
            parts.append(ln)
    return "  ".join(parts)

def generate_one(con, uid: str, timeout: int = 180) -> tuple:
    from bili_digest.distill.extract import call_llm
    segs = segments_of(con, uid)
    if not segs:
        return None, "无 subtitle_seg"
    body = "\n".join("第%d段：%s" % (i, t[:SEG_CHARS])
                     for i, (_s, t) in enumerate(segs, 1))

    try:
        raw = call_llm(PROMPT.replace("{body}", body), timeout=timeout)
        items = (json.loads(raw) or {}).get("segments") or []
    except Exception as e:
        return None, "%s: %s" % (type(e).__name__, str(e)[:60])
    if not items:
        return None, "模型没返回 segments"
    text = render(items)
    if len(text) < MIN_RESULT:
        return None, "结果过短（该条目分段太少，配不出导航价值）"
    return text, ""

def pending(con, limit: int) -> list:
    return con.execute("""
        SELECT DISTINCT cb.item_uid AS uid, i.title AS title,
               (SELECT COUNT(*) FROM content_block s
                 WHERE s.item_uid = cb.item_uid AND s.kind='subtitle_seg') AS n_seg
        FROM content_block cb JOIN item i ON i.uid = cb.item_uid
        WHERE cb.kind='subtitle_seg'
          AND cb.item_uid NOT IN (SELECT item_uid FROM content_block
                                   WHERE kind='seg_summary')
        ORDER BY n_seg DESC
        LIMIT ?
    """, (limit,)).fetchall()

def run(limit: int = 20, write: bool = False) -> dict:
    con = connect(DB)
    con.row_factory = __import__("sqlite3").Row
    todo = pending(con, limit)
    total = con.execute("SELECT COUNT(DISTINCT item_uid) FROM content_block "
                        "WHERE kind='subtitle_seg'").fetchone()[0]
    done = con.execute("SELECT COUNT(DISTINCT item_uid) FROM content_block "
                       "WHERE kind='seg_summary'").fetchone()[0]
    print("  已有 %d / %d 条（有字幕段的条目）· 本次处理 %d 条\n"
          % (done, total, len(todo)))

    ok = fail = 0
    for r in todo:
        uid, title, n = r["uid"], r["title"], r["n_seg"]
        print("  %-14s %s  (%d 段)" % (uid.split(":")[-1], (title or "")[:36], n))
        t0 = time.time()
        text, err = generate_one(con, uid)
        if not text:
            print("      ✗ %s" % err)
            fail += 1
            continue
        print("      %.0fs · %d 字" % (time.time() - t0, len(text)))
        if write:
            con.execute(
                "INSERT INTO content_block(item_uid,kind,text,hash,origin,"
                "producer,created_at) VALUES(?,?,?,?,?,?,?)",
                (uid, "seg_summary", text, block_hash("seg_summary", text),
                 "derived", "seg_summary", now_sec()))
            con.commit()
        ok += 1

    print("\n  完成 %d · 跳过 %d%s"
          % (ok, fail, "" if write else "（演练：未写库，加 --write 落库）"))
    if write and ok:
        print("  接着跑：python build_kb_index.py && python keywords.py --build")
    con.close()
    return {"ok": ok, "skip": fail}

def main() -> int:
    ap = argparse.ArgumentParser(description="为长视频生成段落导航摘要")
    ap.add_argument("--limit", type=int, default=20, help="本批处理多少条（默认 20）")
    ap.add_argument("--write", action="store_true", help="写库（默认只演练）")
    a = ap.parse_args()
    r = run(a.limit, a.write)
    return 0 if r["ok"] or not a.write else 1

if __name__ == "__main__":
    raise SystemExit(main())
