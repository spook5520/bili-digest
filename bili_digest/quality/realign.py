#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import difflib
import json

MIN_OVERLAP = 0.6
MIN_QUOTE = 6

def _overlap(a, b):
    a = (a or "").strip()
    b = (b or "").strip()
    if not a or not b:
        return 0.0
    m = difflib.SequenceMatcher(None, a, b).find_longest_match(0, len(a), 0, len(b))
    return m.size / max(len(a), 1)

def _slice_from_block(block_text, quote):
    t = (block_text or "").strip()
    q = (quote or "").strip()
    if not t or not q:
        return ""
    m = difflib.SequenceMatcher(None, q, t).find_longest_match(0, len(q), 0, len(t))
    if m.size < MIN_QUOTE:
        return ""
    start = max(0, m.b - m.a)
    end = min(len(t), start + len(q))
    out = t[start:end].strip()
    return out if len(out) >= MIN_QUOTE else ""

def _blocks(con, uid):
    return [dict(r) for r in con.execute(
        "SELECT id,kind,offset_sec,text FROM content_block WHERE item_uid=?", (uid,))]

def classify(con) -> dict:
    from bili_digest.quality import fix_cites as fc
    counts = {"ok": 0, "A": 0, "B": 0, "B_ambiguous": 0, "C": 0, "no_quote": 0}
    items = []
    rows = con.execute("SELECT rowid AS rid,item_uid,content FROM derived "
                       "WHERE kind IN ('key_points','insights')").fetchall()
    for r in rows:
        try:
            arr = json.loads(r["content"])
        except Exception:
            continue
        bl = _blocks(con, r["item_uid"])
        by_id = {b["id"]: b for b in bl}
        for idx, it in enumerate(arr):
            if not isinstance(it, dict):
                continue
            for j, c in enumerate(it.get("cites") or []):
                q, cid = fc._quote_of(c), fc._id_of(c)
                if not q:
                    counts["no_quote"] += 1
                    continue
                b = by_id.get(cid)
                if b is not None and q in (b["text"] or ""):
                    counts["ok"] += 1
                    continue

                if b is not None and _overlap(q, b["text"]) >= MIN_OVERLAP:
                    fix = _slice_from_block(b["text"], q)
                    if fix:
                        counts["A"] += 1
                        items.append({"rid": r["rid"], "uid": r["item_uid"],
                                      "idx": idx, "j": j, "kind": "A",
                                      "block": b["id"], "old": q, "new": fix})
                        continue

                hits = []
                for cand in bl:
                    t = cand["text"] or ""
                    if q in t or _overlap(q, t) >= MIN_OVERLAP:
                        hits.append(cand)
                if len(hits) == 1:
                    nb = hits[0]
                    fix = q if q in (nb["text"] or "") else _slice_from_block(nb["text"], q)
                    if fix:
                        counts["B"] += 1
                        items.append({"rid": r["rid"], "uid": r["item_uid"],
                                      "idx": idx, "j": j, "kind": "B",
                                      "block": nb["id"], "old": q, "new": fix})
                        continue
                if len(hits) > 1:
                    counts["B_ambiguous"] += 1
                    continue
                counts["C"] += 1
    return {"counts": counts, "items": items}

def plan(con) -> dict:
    r = classify(con)
    c = r["counts"]
    bad = c["A"] + c["B"] + c["B_ambiguous"] + c["C"]
    return {"counts": c, "fixable": len(r["items"]), "broken": bad,
            "fixable_share": (len(r["items"]) / bad) if bad else 0.0,
            "items": r["items"]}

def apply(con, dry_run: bool = False) -> dict:
    r = classify(con)
    if dry_run:
        return {"dry_run": True, "fixable": len(r["items"]), "applied": 0,
                "counts": r["counts"]}
    by_rid = {}
    for it in r["items"]:
        by_rid.setdefault(it["rid"], []).append(it)
    applied = 0
    for rid, group in by_rid.items():
        row = con.execute("SELECT content FROM derived WHERE rowid=?", (rid,)).fetchone()
        try:
            arr = json.loads(row[0])
        except Exception:
            continue
        changed = False
        for it in group:
            try:
                c = arr[it["idx"]]["cites"][it["j"]]
            except Exception:
                continue
            if not isinstance(c, dict):
                continue
            c["id"] = it["block"]
            c["quote"] = it["new"]
            c["realign"] = it["kind"]
            changed = True
            applied += 1
        if changed:
            con.execute("UPDATE derived SET content=? WHERE rowid=?",
                        (json.dumps(arr, ensure_ascii=False), rid))
    con.commit()
    return {"dry_run": False, "fixable": len(r["items"]), "applied": applied,
            "counts": r["counts"]}

def main() -> int:
    import argparse
    from bili_digest import paths as _p
    _p.enable_utf8_stdout()
    from bili_digest.schema.core_schema import connect
    from bili_digest.paths import DB
    ap = argparse.ArgumentParser(description="引用重新对齐（默认只读）")
    ap.add_argument("--db", default=str(DB))
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    con = connect(a.db)
    con.row_factory = __import__("sqlite3").Row
    if a.apply:
        res = apply(con, dry_run=False)
        print("  已写回 %d 处（可修 %d）" % (res["applied"], res["fixable"]))
    else:
        res = plan(con)
        c = res["counts"]
        print("  ok %d · A %d · B %d · B多命中 %d · C %d · 无引文 %d" % (
            c["ok"], c["A"], c["B"], c["B_ambiguous"], c["C"], c["no_quote"]))
        print("  对不上 %d 处，可修 %d（%.1f%%）" % (
            res["broken"], res["fixable"], 100.0 * res["fixable_share"]))
    con.close()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
