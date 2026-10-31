# -*- coding: utf-8 -*-

import os as _os
import sys as _sys

ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
from bili_digest import paths

CH2TOK = 0.7
IMG_TOK = 1024

def _chars_of_blocks(con, where: str, args=()) -> tuple:
    row = con.execute(
        "SELECT COALESCE(SUM(LENGTH(text)),0), COUNT(*), "
        "COUNT(DISTINCT item_uid) FROM content_block WHERE " + where,
        args).fetchone()
    return (row[0], row[1], row[2])

def library_scale(con) -> dict:
    total, blocks, items = _chars_of_blocks(con, "1=1")
    sub, sblk, sitems = _chars_of_blocks(
        con, "kind IN (?,?)", ("subtitle", "subtitle_seg"))
    return {
        "blocks": blocks,
        "items": items,
        "chars": total,
        "tokens_if_all": int(total * CH2TOK),
        "subtitle_items": sitems,
        "subtitle_chars": sub,
        "median_subtitle_chars_per_item": (sub // sitems) if sitems else 0,
    }

def search_budget(hits: list) -> dict:
    chars = 0
    for h in hits:
        ev = h.get("evidence") or {}
        chars += len(str(ev.get("text") or ""))
        chars += len(str(h.get("summary") or ""))
    return {"n_hits": len(hits), "chars": chars, "tokens": int(chars * CH2TOK)}

def compare(con, vec_index=None, questions=("Frida hook", "缓存淘汰")) -> dict:
    from bili_digest.retrieve import rag_core as rc
    from bili_digest.retrieve import retrieve as rv

    scale = library_scale(con)
    own = []
    if vec_index is not None:
        for q in questions:
            hits = rv.retrieve(con, vec_index, q, granularity="item", top_k=8)
            b = search_budget(hits)
            b["question"] = q
            own.append(b)
    own_avg = (sum(x["tokens"] for x in own) // len(own)) if own else 0

    per_video = scale["median_subtitle_chars_per_item"] * CH2TOK
    return {
        "library": scale,
        "own_per_task_tokens": own_avg,
        "own_samples": own,
        "competitor_per_video_tokens": int(per_video),
        "competitor_5_videos_tokens": int(per_video * 5),
        "competitor_100_videos_tokens": int(per_video * 100),
        "note": ("本项目的单位任务成本与库规模无关；",
                 "竞品随「要看几条」线性增长 —— 这才是本质差别"),
    }

def main() -> int:
    import argparse
    from bili_digest import paths as _p
    _p.enable_utf8_stdout()
    from bili_digest.schema.core_schema import connect
    from bili_digest.paths import DB, VEC
    ap = argparse.ArgumentParser(description="token 成本模型（可复算）")
    ap.add_argument("--compare", action="store_true", help="跑真实对照（要加载模型）")
    ap.add_argument("--scale", action="store_true", help="只报库规模")
    a = ap.parse_args()

    con = connect(DB)
    try:
        s = library_scale(con)
        print("  库规模：%d 块 · %d 条目 · %.1f 万字符" % (
            s["blocks"], s["items"], s["chars"] / 10000))
        print("  全塞进 prompt ≈ %d 万 token（这不是本项目的做法，是分母）" % (
            s["tokens_if_all"] // 10000))
        print("  单条视频字幕中位：%d 字 ≈ %d token" % (
            s["median_subtitle_chars_per_item"],
            int(s["median_subtitle_chars_per_item"] * CH2TOK)))
        if a.scale or not a.compare:
            print()
            print("  加 --compare 跑真实检索对照（约 30 秒，要加载模型）")
            return
        from bili_digest.retrieve import rag_core as rc
        vi = rc.VectorIndex(str(VEC))
        r = compare(con, vi)
        print()
        print("  本项目一次问答：%d token" % r["own_per_task_tokens"])
        for x in r["own_samples"]:
            print("    %-28s %5d 字 ≈ %4d token" % (
                x["question"][:28], x["chars"], x["tokens"]))
        print()
        print("  对照（整篇送料）：")
        print("    看 1 条视频    %6d token" % r["competitor_per_video_tokens"])
        print("    看 5 条综合    %6d token" % r["competitor_5_videos_tokens"])
        print("    看 100 条综合  %6d token   ← 已超多数模型上下文窗口" % (
            r["competitor_100_videos_tokens"]))
        print("    本项目          %6d token   ← 不变" % r["own_per_task_tokens"])
    finally:
        con.close()

if __name__ == "__main__":
    main()
