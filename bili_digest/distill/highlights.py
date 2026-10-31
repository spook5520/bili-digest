# -*- coding: utf-8 -*-

import os as _os
import sys as _sys

from bili_digest import paths

def density(con, uid: str, bucket_sec: int = 60) -> dict:
    rows = con.execute(
        "SELECT offset_sec FROM content_block "
        "WHERE item_uid = ? AND kind = ? AND offset_sec IS NOT NULL",
        (uid, "danmaku")).fetchall()
    if not rows:
        return {"uid": uid, "n": 0, "buckets": [],
                "note": "该条目没有带时间戳的弹幕（未抓弹幕，或抓时没记时间）"}
    counts = {}
    for r in rows:
        sec = int(r[0] if not hasattr(r, "keys") else r["offset_sec"])
        b = sec // bucket_sec
        counts[b] = counts.get(b, 0) + 1
    n = len(rows)
    ordered = sorted(counts.items())
    peak = max(ordered, key=lambda x: x[1]) if ordered else (0, 0)

    vals = sorted(counts.values())
    med = vals[len(vals) // 2] if vals else 0
    return {
        "uid": uid, "n": n, "bucket_sec": bucket_sec,
        "buckets": [{"start_sec": b * bucket_sec, "n": c} for b, c in ordered],
        "peak": {"start_sec": peak[0] * bucket_sec, "n": peak[1]},
        "median": med,
        "duration_sec": max(int(r[0] if not hasattr(r, "keys") else r["offset_sec"])
                            for r in rows),
    }

def moments(con, uid: str, top: int = 5, bucket_sec: int = 60,
            with_text: bool = True, text_per: int = 6) -> list:
    d = density(con, uid, bucket_sec)
    if not d["buckets"]:
        return []
    out = []
    for b in sorted(d["buckets"], key=lambda x: -x["n"])[:top]:
        item = {"start_sec": b["start_sec"], "n": b["n"],
                "min": b["start_sec"] // 60, "sec": b["start_sec"] % 60,
                "end_sec": b["start_sec"] + bucket_sec}

        item.update(classify_bucket(con, uid, b["start_sec"],
                                    b["start_sec"] + bucket_sec))
        if with_text:
            rows = con.execute(
                "SELECT text FROM content_block WHERE item_uid = ? AND kind = ? "
                "AND offset_sec >= ? AND offset_sec < ? ORDER BY offset_sec LIMIT ?",
                (uid, "danmaku", b["start_sec"], b["start_sec"] + bucket_sec,
                 text_per)).fetchall()
            item["samples"] = [str(r[0] if not hasattr(r, "keys") else r["text"])
                               for r in rows]
        out.append(item)
    return out

DEBATE_WORDS = (
    "错", "不对", "不懂", "乱说", "其实", "纠正", "笑死", "打脸", "杠",
    "谬", "扯", "误区", "存疑", "质疑", "反驳", "不同意", "有问题",
)
HYPE_WORDS = (
    "哈哈", "笑死", "牛逼", "牛", "厉害", "泪目", "太强", "绝了", "神",
    "感动", "震撼", "舒服", "妙", "精彩", "强", "赞",
)

def classify_bucket(con, uid: str, start_sec: int, end_sec: int) -> dict:
    rows = con.execute(
        "SELECT text FROM content_block WHERE item_uid = ? AND kind = ? "
        "AND offset_sec >= ? AND offset_sec < ?",
        (uid, "danmaku", start_sec, end_sec)).fetchall()
    texts = [str(r[0] if not hasattr(r, "keys") else r["text"]) for r in rows]
    d = sum(1 for t in texts if any(w in t for w in DEBATE_WORDS))
    h = sum(1 for t in texts if any(w in t for w in HYPE_WORDS))
    n = len(texts) or 1
    if d >= 3 and d >= h:
        kind = "debate"
    elif h >= 3 and h > d:
        kind = "hype"
    elif d >= 1 and h >= 1:
        kind = "mixed"
    else:
        kind = "neutral"
    return {"kind": kind, "debate": d, "hype": h, "n": len(texts),
            "share": round(max(d, h) * 1.0 / n, 2)}

def worthwhile(con, uid: str, bucket_sec: int = 60) -> dict:
    d = density(con, uid, bucket_sec)
    if not d["buckets"]:
        return {"uid": uid, "verdict": "unknown", "note": d.get("note")}
    pk, med = d["peak"]["n"], d["median"] or 1
    ratio = pk / med
    if ratio >= 3:
        shape = "有明确高能点"
    elif ratio >= 1.6:
        shape = "有起伏"
    else:
        shape = "全程平"
    return {"uid": uid, "shape": shape, "peak_at_min": d["peak"]["start_sec"] // 60,
            "peak_n": pk, "median_n": med, "ratio": round(ratio, 2),
            "total": d["n"], "duration_sec": d["duration_sec"],
            "note": "弹幕密度说明「热闹」，不说明「好」—— 争议和翻车也会出峰"}

def main() -> int:
    import argparse
    from bili_digest import paths as _p
    _p.enable_utf8_stdout()
    from bili_digest.schema.core_schema import connect
    from bili_digest.paths import DB, bili_uid
    ap = argparse.ArgumentParser(description="高能时刻：弹幕密度找值得看的段落")
    ap.add_argument("uid", help="条目 uid 或 BV 号")
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--bucket", type=int, default=60, help="分桶秒数（短视频用 10）")
    a = ap.parse_args()

    uid = a.uid if ":" in a.uid else bili_uid(a.uid)
    con = connect(DB)
    try:
        w = worthwhile(con, uid, a.bucket)
        if w.get("verdict") == "unknown" or "shape" not in w:
            print("  %s" % w.get("note", "没有弹幕数据"))
            return
        print("  %s · %d 条弹幕 · 时长 %ds" % (uid.split(":")[-1], w["total"],
                                              w["duration_sec"]))
        print("  形状: %s · 峰值在第 %d 分钟（%d 条 vs 中位 %d，比值 %.1f）" % (
            w["shape"], w["peak_at_min"], w["peak_n"], w["median_n"], w["ratio"]))
        print("  注: %s" % w["note"])
        print()
        KIND_CN = {"debate": "在争论", "hype": "在叫好",
                   "mixed": "有争论也叫好", "neutral": "讨论"}
        for m in moments(con, uid, top=a.top, bucket_sec=a.bucket):
            print("  [%02d:%02d] %d 条 · %s（争论词 %d · 叫好词 %d）" % (
                m["min"], m["sec"], m["n"],
                KIND_CN.get(m.get("kind"), m.get("kind", "?")),
                m.get("debate", 0), m.get("hype", 0)))
            for s in m.get("samples", [])[:4]:
                print("      %s" % str(s)[:56])
    finally:
        con.close()

if __name__ == "__main__":
    main()
