# -*- coding: utf-8 -*-

import os as _os
import sys as _sys

ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
from bili_digest import paths

LEVELS = ("full", "partial", "meta_only", "empty")

LEVEL_CN = {
    "full": "有字幕（说了什么我知道）",
    "partial": "无字幕，只有观众反应（弹幕/评论）",
    "meta_only": "只有标题和简介",
    "empty": "什么都没有，检索里不可见",
}

def asr_hint_from(dur, mx=None) -> str | None:
    d = float(dur or 0)
    try:
        if mx and float(mx) > d:
            d = float(mx)
    except (TypeError, ValueError):
        pass
    if not d:
        return None
    est_min = round(d / 3.0 / 60.0, 1)
    if d > 1800:
        return ("这条没字幕，整条转写约 %.0f 分钟（估）；"
                "超过 30 分钟建议只转关键段" % est_min)
    return "这条没字幕，转写约 %.1f 分钟（估，3 倍实时）" % est_min

def of_item(con, item_uid: str) -> dict:
    rows = con.execute(
        "SELECT kind, COUNT(*) FROM content_block WHERE item_uid = ? "
        "GROUP BY kind", (item_uid,)).fetchall()
    kinds = {r[0]: r[1] for r in rows}
    n = sum(kinds.values())
    has_sub = bool(kinds.get("subtitle") or kinds.get("subtitle_seg"))
    has_dm = bool(kinds.get("danmaku"))
    has_cm = bool(kinds.get("comment"))
    has_meta = bool(kinds.get("meta"))
    has_sum = bool(kinds.get("summary"))

    if n == 0:
        level = "empty"
    elif has_sub:
        level = "full"
    elif has_dm or has_cm:
        level = "partial"
    else:
        level = "meta_only"

    gaps = []
    if not has_sub:
        gaps.append("无字幕")
    if not has_sum:
        gaps.append("无提炼")
    if not has_dm and not has_cm:
        gaps.append("无弹幕评论")
    asr_hint = None
    if not has_sub and n:
        dur = con.execute(
            "SELECT duration_sec FROM item WHERE uid = ?", (item_uid,)).fetchone()
        dur = (dur[0] if dur else 0) or 0

        try:
            mx = con.execute(
                "SELECT MAX(offset_sec) FROM content_block WHERE item_uid = ?",
                (item_uid,)).fetchone()[0]
            if mx and mx > dur:
                dur = mx
        except Exception:
            pass
        asr_hint = asr_hint_from(dur, None)

    return {
        "level": level,
        "level_cn": LEVEL_CN[level],
        "has_subtitle": has_sub,
        "has_danmaku": has_dm,
        "has_comment": has_cm,
        "has_summary": has_sum,
        "n_blocks": n,
        "gap": "、".join(gaps) if gaps else "完整",
        "asr_hint": asr_hint,
    }

def library(con) -> dict:
    total = con.execute("SELECT COUNT(*) FROM item").fetchone()[0]
    ph = ",".join("?" * len(LEVELS))

    rows = con.execute(
        "SELECT i.uid, "
        "  MAX(CASE WHEN b.kind IN ('subtitle','subtitle_seg') THEN 1 ELSE 0 END) sub, "
        "  MAX(CASE WHEN b.kind = 'danmaku' THEN 1 ELSE 0 END) dm, "
        "  MAX(CASE WHEN b.kind = 'comment' THEN 1 ELSE 0 END) cm, "
        "  COUNT(b.id) n "
        "FROM item i LEFT JOIN content_block b ON b.item_uid = i.uid "
        "GROUP BY i.uid").fetchall()
    dist = {k: 0 for k in LEVELS}
    for _uid, sub, dm, cm, n in rows:
        if not n:
            dist["empty"] += 1
        elif sub:
            dist["full"] += 1
        elif dm or cm:
            dist["partial"] += 1
        else:
            dist["meta_only"] += 1
    return {
        "items": total,
        "distribution": dist,
        "readable_pct": round(dist["full"] * 100.0 / max(total, 1), 1),
        "note": ("readable_pct 是「有字幕的占比」—— 只有这部分，「它说了什么」才是我真知道的。",
                 "partial 只能从观众反应推测，meta_only/empty 基本等于没读到。"),
    }

def annotate_hits(con, hits: list) -> list:
    cache = {}
    for h in hits:
        uid = h.get("item_uid")
        if not uid:
            continue
        if uid not in cache:
            cache[uid] = of_item(con, uid)
        c = cache[uid]
        h["coverage"] = c["level"]
        h["coverage_note"] = c["gap"]
    return hits

def main() -> int:
    import argparse
    from bili_digest import paths as _p
    _p.enable_utf8_stdout()
    from bili_digest.schema.core_schema import connect
    from bili_digest.paths import DB, bili_uid
    ap = argparse.ArgumentParser(description="覆盖：每个条目被读到了多少")
    ap.add_argument("uid", nargs="?", help="条目 uid 或 BV 号")
    ap.add_argument("--library", action="store_true", help="报库整体分布")
    a = ap.parse_args()

    con = connect(DB)
    try:
        if a.library or not a.uid:
            r = library(con)
            print("  条目 %d · 有字幕 %s%%" % (r["items"], r["readable_pct"]))
            print()
            for k in LEVELS:
                n = r["distribution"][k]
                print("    %-10s %4d  %s" % (k, n, LEVEL_CN[k]))
            print()
            print("  %s" % r["note"][0])
            print("  %s" % r["note"][1])
            return
        uid = a.uid if ":" in a.uid else bili_uid(a.uid)
        c = of_item(con, uid)
        print("  %s" % uid)
        print("    等级 %s —— %s" % (c["level"], c["level_cn"]))
        print("    块数 %d · 缺口：%s" % (c["n_blocks"], c["gap"]))
    finally:
        con.close()

if __name__ == "__main__":
    main()
