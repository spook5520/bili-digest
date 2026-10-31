# -*- coding: utf-8 -*-

import os as _os
import sys as _sys

ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
from bili_digest import paths

DERIVED_KINDS = {"summary", "key_points", "insights", "topics", "audience", "scores"}

def around(con, block_id: int, n: int = 2) -> dict:
    row = con.execute(
        "SELECT id, item_uid, kind, offset_sec, text FROM content_block WHERE id = ?",
        (block_id,)).fetchone()
    if not row:
        return {"error": "块不存在", "block_id": block_id}
    uid = row[1]
    kind = row[2]
    off = row[3]
    if kind in DERIVED_KINDS:
        title = con.execute("SELECT title FROM item WHERE uid = ?", (uid,)).fetchone()
        return {"uid": uid, "title": (title[0] if title else ""),
                "anchor_id": block_id, "blocks": [],
                "note": ("这是【整条提炼】（kind=%s），不是时间轴上的一个点，"
                         "没有前后文可展开。要看内容本身请读这条派生块。" % kind)}

    if off is None:

        rows = con.execute(
            "SELECT id, item_uid, kind, offset_sec, text FROM content_block "
            "WHERE item_uid = ? AND kind = ? ORDER BY id LIMIT ? OFFSET ?",
            (uid, kind, n * 2 + 1, max(0, _pos(con, uid, kind, block_id) - n))).fetchall()
        if not rows:

            rows = con.execute(
                "SELECT id, item_uid, kind, offset_sec, text FROM content_block "
                "WHERE item_uid = ? ORDER BY id LIMIT ? OFFSET ?",
                (uid, n * 2 + 1, max(0, _rank(con, uid, block_id) - n))).fetchall()
    else:

        before = con.execute(
            "SELECT id, item_uid, kind, offset_sec, text FROM content_block "
            "WHERE item_uid = ? AND kind = ? AND offset_sec IS NOT NULL "
            "AND offset_sec <= ? ORDER BY offset_sec DESC LIMIT ?",
            (uid, kind, off, n + 1)).fetchall()
        after = con.execute(
            "SELECT id, item_uid, kind, offset_sec, text FROM content_block "
            "WHERE item_uid = ? AND kind = ? AND offset_sec IS NOT NULL "
            "AND offset_sec > ? ORDER BY offset_sec ASC LIMIT ?",
            (uid, kind, off, n)).fetchall()
        rows = list(reversed(before)) + list(after)

    out = []
    for r in rows:
        out.append({
            "id": r[0], "kind": r[2], "offset_sec": r[3],
            "text": str(r[4] or ""), "is_anchor": r[0] == block_id,
        })
    title = con.execute("SELECT title FROM item WHERE uid = ?", (uid,)).fetchone()
    return {"uid": uid, "title": (title[0] if title else ""),
            "anchor_id": block_id, "blocks": out,
            "note": "按 offset_sec 取同条目上下文，不跨条目"}

def _pos(con, uid: str, kind: str, block_id: int) -> int:
    r = con.execute(
        "SELECT COUNT(*) FROM content_block WHERE item_uid = ? AND kind = ? AND id <= ?",
        (uid, kind, block_id)).fetchone()
    return int(r[0] or 1) - 1

def _rank(con, uid: str, block_id: int) -> int:
    r = con.execute(
        "SELECT COUNT(*) FROM content_block WHERE item_uid = ? AND id <= ?",
        (uid, block_id)).fetchone()
    return int(r[0] or 1) - 1

def context_of_hit(con, hit: dict, n: int = 2) -> dict:
    ev = hit.get("evidence") or {}
    bid = ev.get("id")
    if bid is None:

        uid = hit.get("item_uid")
        txt = str(ev.get("text") or "")[:80]
        row = con.execute(
            "SELECT id FROM content_block WHERE item_uid = ? AND substr(text,1,80) = ? "
            "LIMIT 1", (uid, txt)).fetchone()
        bid = row[0] if row else None
    if bid is None:
        return {"error": "这条结果没带可展开的块"}
    return around(con, int(bid), n)

def render(result: dict) -> list:
    if result.get("error"):
        return ["  %s" % result["error"]]
    lines = ["  《%s》  %s" % ((result.get("title") or "")[:44],
                             (result.get("uid") or "").split(":")[-1])]
    if result.get("note"):
        lines.append("  %s" % result["note"])
    blocks = result.get("blocks") or []
    if blocks:
        lines.append("")
    for b in blocks:
        t = b.get("offset_sec")
        stamp = ("%02d:%02d" % (int(t) // 60, int(t) % 60)
                 if t is not None else "--:--")
        lines.append("  %s [%s] %s" % ("▶" if b.get("is_anchor") else " ",
                                       stamp, str(b.get("text") or "")[:110]))
    return lines

def main() -> int:
    import argparse
    from bili_digest import paths as _p
    _p.enable_utf8_stdout()
    from bili_digest.schema.core_schema import connect
    from bili_digest.paths import DB
    ap = argparse.ArgumentParser(description="展开一个块的上下文（粗索引→细证据）")
    ap.add_argument("block_id", type=int, help="content_block.id")
    ap.add_argument("--n", type=int, default=2, help="前后各取几块（默认 2）")
    a = ap.parse_args()
    con = connect(DB)
    try:
        for line in render(around(con, a.block_id, a.n)):
            print(line)
    finally:
        con.close()

if __name__ == "__main__":
    main()
