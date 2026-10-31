#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os as _os
import sys as _sys

from bili_digest import paths

import hashlib
import json
import sqlite3
import sys
import time
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).parent
from bili_digest.schema.core_schema import block_hash, connect, now_sec
from bili_digest.paths import DB

BACKFILL = {
    "danmaku": ("captured", "bili_digest"),
    "subtitle": ("captured", "bili_digest"),
    "comment": ("captured", "bili_digest"),
    "meta": ("captured", "bili_digest"),
    "subtitle_seg": ("derived", "seg_boundary"),
}

IMPORT_DERIVED = {
    "summary": "summary",
    "key_points": "key_points",
    "insights": "insights",

    "seg_summary": "seg_summary",
    "growth": "growth",

    "audience": "audience",
}

def _hash(kind: str, text: str) -> str:
    return block_hash(kind, text)

def _render(value) -> str:
    from bili_digest.distill import extract as _ex
    if value is None:
        return ""
    try:
        v = json.loads(value)
    except (ValueError, TypeError):
        return str(value)
    if isinstance(v, str):
        return v

    if isinstance(v, dict) and "segments" in v:
        parts = []
        for ln in (v.get("segments") or []):
            ln = str(ln).strip()
            if not ln:
                continue
            if "|" in ln:
                idx, desc = ln.split("|", 1)
                parts.append(f"第{idx.strip()}段：{desc.strip()}")
            else:
                parts.append(ln)
        return "  ".join(parts)
    if isinstance(v, dict) and ("new_points" in v or "growth_note" in v
                                or "new_insights" in v):
        parts = []
        note = (v.get("growth_note") or "").strip()
        if note:
            parts.append(f"（增量更新）{note}")
        elif any(v.get(k) for k in ("new_points", "new_insights", "new_audience")):
            parts.append("（增量更新）该条目的素材后来增长了，以下是新增内容")
        for key, label in (("new_insights", "新增结论"),
                           ("new_points", "新增要点"),
                           ("new_audience", "新增观众关注"),
                           ("revisions", "对既有结论的修正")):
            for x in (v.get(key) or []):
                if isinstance(x, dict):
                    body = x.get("claim") or x.get("text") or ""

                    cites = _ex.cite_ids(x.get("block_ids") or x.get("cites"))
                    if body and cites:
                        parts.append(f"[{label}] {body}（证据 {', '.join(map(str, cites))}）")
                    elif body:
                        parts.append(f"[{label}] {body}")
                    else:
                        parts.append(f"[{label}] {json.dumps(x, ensure_ascii=False)}")
                else:
                    parts.append(f"[{label}] {x}")
        return "\n".join(p for p in parts if p)
    if isinstance(v, list):
        parts = []
        for x in v:
            if isinstance(x, dict):

                claim = x.get("claim") or x.get("point") or x.get("text") or ""

                cites = _ex.cite_ids(x.get("cites"))
                if claim and cites:
                    parts.append(f"{claim}（证据 {', '.join(map(str, cites))}）")
                elif claim:
                    parts.append(claim)
                else:
                    parts.append(json.dumps(x, ensure_ascii=False))
            else:
                parts.append(str(x))
        return "\n".join(p for p in parts if p)
    return json.dumps(v, ensure_ascii=False)

def check(verbose: bool = True) -> dict:
    con = connect(DB)
    con.row_factory = sqlite3.Row
    cols = [r[1] for r in con.execute("PRAGMA table_info(content_block)")]
    has_cols = "origin" in cols and "producer" in cols

    out = {"has_columns": has_cols, "existing": {}, "to_import": {}}
    for r in con.execute("SELECT kind, COUNT(*) n FROM content_block "
                         "GROUP BY kind ORDER BY -n"):
        out["existing"][r["kind"]] = r["n"]

    for dk, ck in IMPORT_DERIVED.items():
        n = 0
        for r in con.execute("SELECT content FROM derived WHERE kind=?", (dk,)):
            t = _render(r["content"]).strip()
            if not t:
                continue
            h = _hash(ck, t)
            dup = con.execute(
                "SELECT 1 FROM content_block cb WHERE cb.kind=? AND cb.hash=?",
                (ck, h)).fetchone()
            if not dup:
                n += 1
        out["to_import"][f"derived.{dk}→{ck}"] = n

    ann = con.execute("SELECT COUNT(*) FROM annotation").fetchone()[0]
    already = 0
    for r in con.execute("SELECT item_uid, text FROM annotation"):
        t = (r["text"] or "").strip()
        if not t:
            continue
        h = _hash("annotation", t)
        if con.execute("SELECT 1 FROM content_block cb WHERE cb.kind=? AND cb.hash=?",
                       ("annotation", h)).fetchone():
            already += 1
    out["to_import"]["annotation→annotation"] = ann - already
    out["annotation_total"] = ann

    if verbose:
        print(f"  content_block 是否已有 origin/producer 列: "
              f"{'✓' if has_cols else '✗ 需添加'}")
        print(f"\n  现有内容块:")
        for k, n in out["existing"].items():
            print(f"    {k:<16} {n:>6}")
        print(f"\n  待纳入检索的派生物:")
        for k, n in out["to_import"].items():
            print(f"    {k:<28} {n:>5}")
        tot = sum(out["to_import"].values())
        print(f"    {'合计':<28} {tot:>5}")
    con.close()
    return out

def ensure_origin_cols(con) -> list:
    cols = [r[1] for r in con.execute("PRAGMA table_info(content_block)")]
    added = []
    if "origin" not in cols:
        con.execute("ALTER TABLE content_block ADD COLUMN origin TEXT")
        added.append("origin")
    if "producer" not in cols:
        con.execute("ALTER TABLE content_block ADD COLUMN producer TEXT")
        added.append("producer")
    return added

def sync_derived(con, uid: str = None, verbose: bool = False) -> dict:
    ensure_origin_cols(con)
    ts = now_sec()
    out = {}

    where = " WHERE item_uid=?" if uid else ""
    for dk, ck in IMPORT_DERIVED.items():
        n = 0
        for r in con.execute(
                f"SELECT item_uid, content, created_at FROM derived "
                f"WHERE kind=?{where.replace(' WHERE', ' AND') if where else ''}",
                ((dk, uid) if uid else (dk,))):
            t = _render(r["content"]).strip()
            if not t:
                continue
            h = _hash(ck, t)

            existing = con.execute(
                "SELECT id, hash FROM content_block WHERE item_uid=? AND kind=?",
                (r["item_uid"], ck)).fetchall()
            if any(e["hash"] == h for e in existing):

                n += 1
                continue

            old_ids = [e["id"] for e in existing]
            for bid in old_ids:
                con.execute("DELETE FROM content_block WHERE id=?", (bid,))

                try:
                    con.execute("DELETE FROM vec_state WHERE block_id=?", (bid,))
                except sqlite3.OperationalError:
                    pass
                try:
                    con.execute("DELETE FROM block_fts WHERE rowid=?", (bid,))
                except sqlite3.OperationalError:
                    pass

            cur = con.execute(
                "INSERT INTO content_block(item_uid,kind,offset_sec,seq,text,"
                "author,weight,extras,hash,created_at,origin,producer) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (r["item_uid"], ck, None, None, t, None, None, None,
                 h, r["created_at"] or ts, "derived", "extract"))
            n += 1

            try:
                from bili_digest.schema.core_schema import index_block as _ib
                _ib(con, cur.lastrowid, t, r["item_uid"], ck)
            except Exception:
                pass

    n_ann = 0
    sql = "SELECT item_uid, text, created_at FROM annotation"
    args = ()
    if uid:
        sql += " WHERE item_uid=?"
        args = (uid,)
    for r in con.execute(sql, args):
        t = (r["text"] or "").strip()
        if not t:
            continue
        h = _hash("annotation", t)
        if con.execute("SELECT 1 FROM content_block WHERE kind=? AND hash=?",
                       ("annotation", h)).fetchone():
            continue
        con.execute(
            "INSERT INTO content_block(item_uid,kind,offset_sec,seq,text,"
            "author,weight,extras,hash,created_at,origin,producer) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (r["item_uid"], "annotation", None, None, t, None, None, None,
             h, r["created_at"] or ts, "derived", "human"))
        n_ann += 1
    if n_ann:
        out["annotation"] = n_ann
    con.commit()
    if verbose:
        print(f"    派生物入检索: {out or '无新增'}")
    return out

def apply(verbose: bool = True) -> dict:
    con = connect(DB)
    con.row_factory = sqlite3.Row
    con.execute("BEGIN")

    cols = [r[1] for r in con.execute("PRAGMA table_info(content_block)")]
    added = []
    if "origin" not in cols:
        con.execute("ALTER TABLE content_block ADD COLUMN origin TEXT")
        added.append("origin")
    if "producer" not in cols:
        con.execute("ALTER TABLE content_block ADD COLUMN producer TEXT")
        added.append("producer")

    filled = 0
    for kind, (origin, producer) in BACKFILL.items():
        cur = con.execute(
            "UPDATE content_block SET origin=?, producer=? "
            "WHERE kind=? AND (origin IS NULL OR producer IS NULL)",
            (origin, producer, kind))
        filled += cur.rowcount

    cur = con.execute("UPDATE content_block SET origin='captured', "
                      "producer='unknown' WHERE origin IS NULL")
    filled += cur.rowcount

    ts = now_sec()
    imported = {}
    for dk, ck in IMPORT_DERIVED.items():
        n = 0
        for r in con.execute(
                "SELECT item_uid, content, created_at FROM derived WHERE kind=?",
                (dk,)):
            t = _render(r["content"]).strip()
            if not t:
                continue
            h = _hash(ck, t)
            if con.execute(
                    "SELECT 1 FROM content_block WHERE kind=? AND hash=?",
                    (ck, h)).fetchone():
                continue
            con.execute(
                "INSERT INTO content_block(item_uid,kind,offset_sec,seq,text,"
                "author,weight,extras,hash,created_at,origin,producer) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (r["item_uid"], ck, None, None, t, None, None, None,
                 h, r["created_at"] or ts, "derived", "extract"))
            n += 1
        imported[f"derived.{dk}"] = n

    n_ann = 0
    for r in con.execute("SELECT item_uid, text, created_at FROM annotation"):
        t = (r["text"] or "").strip()
        if not t:
            continue
        h = _hash("annotation", t)
        if con.execute("SELECT 1 FROM content_block WHERE kind=? AND hash=?",
                       ("annotation", h)).fetchone():
            continue
        con.execute(
            "INSERT INTO content_block(item_uid,kind,offset_sec,seq,text,"
            "author,weight,extras,hash,created_at,origin,producer) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (r["item_uid"], "annotation", None, None, t, None, None, None,
             h, r["created_at"] or ts, "derived", "human"))
        n_ann += 1
    imported["annotation"] = n_ann

    con.execute("CREATE INDEX IF NOT EXISTS idx_cb_origin "
                "ON content_block(origin, kind)")
    con.commit()

    total = con.execute("SELECT COUNT(*) FROM content_block").fetchone()[0]
    by_origin = {r[0]: r[1] for r in con.execute(
        "SELECT COALESCE(origin,'?'), COUNT(*) FROM content_block GROUP BY origin")}
    con.close()

    r = {"added_columns": added, "backfilled": filled,
         "imported": imported, "total_blocks": total,
         "by_origin": by_origin}
    if verbose:
        print(f"  新增列: {added or '（已存在）'}")
        print(f"  回填现有块: {filled} 条")
        print(f"  导入派生物:")
        for k, v in imported.items():
            print(f"    {k:<22} {v:>5}")
        print(f"\n  内容块总数: {total}")
        print(f"  按来源分布: {by_origin}")
        print(f"\n  [!] 索引已失效 —— 必须重建：python build_kb_index.py")
    return r

def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="统一内容概念：把派生物纳入内容块")
    ap.add_argument("--check", action="store_true", help="只检查（不改库）")
    ap.add_argument("--apply", action="store_true", help="执行迁移")
    a = ap.parse_args()

    if a.apply:
        t0 = time.time()
        apply()
        print(f"  耗时 {time.time()-t0:.1f}s")
    else:
        check()

if __name__ == "__main__":
    main()
