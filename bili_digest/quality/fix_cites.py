#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json

SRC_KINDS = ("subtitle", "subtitle_seg", "danmaku", "comment", "meta")
MIN_QUOTE = 6

def _blocks(con, uid: str) -> list:
    return [dict(r) for r in con.execute(
        "SELECT id,kind,offset_sec,text FROM content_block WHERE item_uid=?", (uid,))]

def _match(blocks: list, quote: str):
    q = (quote or "").strip()
    if len(q) < MIN_QUOTE:
        return None, 0
    hits = []
    for b in blocks:
        if b.get("kind") not in SRC_KINDS:
            continue
        t = (b.get("text") or "").strip()
        if len(t) < MIN_QUOTE:
            continue
        if q in t:

            hits.append((len(q), len(t), b))
        elif t in q:

            hits.append((len(t), -len(t), b))
    if not hits:
        return None, 0
    hits.sort(key=lambda x: (-x[0], x[1]))
    return hits[0][2], len(hits)

def _quote_of(c) -> str:
    if isinstance(c, dict):
        return str(c.get("q") or c.get("quote") or "").strip()
    return ""

def _id_of(c):
    if isinstance(c, dict):
        return c.get("id", c.get("i"))
    return c

def audit(con) -> dict:
    st = {"total": 0, "ok": 0, "wrong": 0, "dangling": 0, "unverified": 0,
          "fixable": 0, "fixable_unique": 0, "ambiguous": 0, "unrecoverable": 0}
    per_item = {"items": 0, "all_ok": 0, "part_bad": 0, "all_bad": 0}
    detail = []
    rows = con.execute("SELECT item_uid,kind,content FROM derived "
                       "WHERE kind IN ('key_points','insights')").fetchall()
    for r in rows:
        try:
            arr = json.loads(r["content"])
        except Exception:
            continue
        if not isinstance(arr, list):
            continue
        uid = r["item_uid"]
        blocks = _blocks(con, uid)
        by_id = {b["id"]: b for b in blocks}
        bad = tot = 0
        for idx, it in enumerate(arr):
            if not isinstance(it, dict):
                continue
            cites = it.get("cites")
            if not isinstance(cites, list):
                continue
            for c in cites:
                st["total"] += 1
                tot += 1
                cid, q = _id_of(c), _quote_of(c)
                b = by_id.get(cid)
                if not q:
                    st["unverified"] += 1
                    st["unrecoverable"] += 1
                    detail.append((uid, r["kind"], idx, "unverified"))
                    continue
                if b is not None and q in (b["text"] or ""):
                    st["ok"] += 1
                    continue
                bad += 1
                if b is None:
                    st["dangling"] += 1
                    verdict = "dangling"
                else:
                    st["wrong"] += 1
                    verdict = "wrong"
                nb, n = _match(blocks, q)
                if nb is not None:
                    st["fixable"] += 1
                    if n == 1:
                        st["fixable_unique"] += 1
                    else:
                        st["ambiguous"] += 1
                else:
                    st["unrecoverable"] += 1
                detail.append((uid, r["kind"], idx, verdict))
        if tot:
            per_item["items"] += 1
            if bad == 0:
                per_item["all_ok"] += 1
            elif bad == tot:
                per_item["all_bad"] += 1
            else:
                per_item["part_bad"] += 1
    return {"cites": st, "items": per_item, "detail": detail}

def repair(con, dry_run: bool = False, verbose: bool = False) -> dict:
    st = {"items_scanned": 0, "items_changed": 0, "cites_fixed": 0,
          "ambiguous": 0, "unrecoverable": 0, "dry_run": dry_run}
    rows = con.execute("SELECT rowid AS rid,item_uid,kind,content FROM derived "
                       "WHERE kind IN ('key_points','insights')").fetchall()
    for r in rows:
        try:
            arr = json.loads(r["content"])
        except Exception:
            continue
        if not isinstance(arr, list):
            continue
        st["items_scanned"] += 1
        blocks = _blocks(con, r["item_uid"])
        by_id = {b["id"]: b for b in blocks}
        changed = 0
        for it in arr:
            if not isinstance(it, dict):
                continue
            cites = it.get("cites")
            if not isinstance(cites, list):
                continue
            for c in cites:
                cid, q = _id_of(c), _quote_of(c)
                if not q:
                    st["unrecoverable"] += 1
                    continue
                b = by_id.get(cid)
                if b is not None and q in (b["text"] or ""):
                    continue
                nb, n = _match(blocks, q)
                if nb is None:
                    st["unrecoverable"] += 1
                    continue
                if n != 1:
                    st["ambiguous"] += 1
                    continue
                if nb["id"] != cid:
                    if isinstance(c, dict):
                        c["id"] = nb["id"]
                    changed += 1
        if changed:
            st["items_changed"] += 1
            st["cites_fixed"] += changed
            if verbose:
                print("  %s 修复 %d 条" % (r["item_uid"], changed))
            if not dry_run:
                con.execute("UPDATE derived SET content=? WHERE rowid=?",
                            (json.dumps(arr, ensure_ascii=False), r["rid"]))
    if not dry_run:
        con.commit()
    return st

def main() -> int:
    import argparse
    from bili_digest import paths as _p
    _p.enable_utf8_stdout()
    from bili_digest.schema.core_schema import connect
    from bili_digest.paths import DB

    ap = argparse.ArgumentParser(description="修复 cites 的块指针（按引文回查）")
    ap.add_argument("--db", default=str(DB))
    ap.add_argument("--apply", action="store_true", help="真的写回（默认只对账）")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()

    con = connect(a.db)
    con.row_factory = __import__("sqlite3").Row
    if not a.apply:
        r = audit(con)
        s = r["cites"]
        print("  引用总数 %d" % s["total"])
        print("    指向正确   %d" % s["ok"])
        print("    错指       %d  （id 存在但引文不在该块）" % s["wrong"])
        print("    悬空       %d  （id 已不存在）" % s["dangling"])
        print("    旧格式     %d  （无引文，无从验证/修复）" % s["unverified"])
        print("    可回查修复 %d  （唯一命中 %d / 多命中 %d）" % (
            s["fixable"], s["fixable_unique"], s["ambiguous"]))
        print("    无法恢复   %d" % s["unrecoverable"])
        print("  按条目 %s" % r["items"])
    else:
        r = repair(con, dry_run=False, verbose=a.verbose)
        print("  扫描 %d 条 derived，改动 %d 条，修复 %d 处引用" % (
            r["items_scanned"], r["items_changed"], r["cites_fixed"]))
        print("  多命中跳过 %d，无法回查 %d" % (r["ambiguous"], r["unrecoverable"]))
    con.close()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
