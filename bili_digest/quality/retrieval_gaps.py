#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import sys

from bili_digest import paths

def _tok(text: str) -> set:
    from bili_digest.quality import agenda as ag
    try:
        return {w for w in ag._tokens(text or "") if w not in ag._STOP}
    except Exception:
        return set(w for w in (text or "") if len(w) >= 2)

def zero_results(con, days: int = 30) -> dict:
    since = _since(days)
    rows = con.execute(
        "SELECT id, ts, question, mode, granularity, ms FROM query_log "
        "WHERE n_hits = 0 AND ts >= ? ORDER BY ts DESC", (since,)).fetchall()
    total = con.execute("SELECT COUNT(*) FROM query_log WHERE ts >= ?",
                        (since,)).fetchone()[0]
    real, noise = [], []
    for r in rows:
        q = (r[2] or "").strip()
        toks = _tok(q)

        if len(q) < 2 or len(toks) < 2:
            noise.append({"id": r[0], "question": q,
                          "why": "太短、全是泛词，或只有一个实词"})
        else:
            real.append({"id": r[0], "ts": r[1], "question": q, "mode": r[3],
                         "granularity": r[4]})
    return {"n_queries": total, "n_zero": len(rows), "real": real,
            "noise": noise,
            "rate": round(len(rows) / total, 3) if total else 0.0}

def repeated_queries(con, days: int = 30, window_min: int = 60,
                     min_jaccard: float = 0.6) -> dict:
    since = _since(days)
    rows = con.execute(
        "SELECT id, ts, question, n_hits FROM query_log WHERE ts >= ? "
        "ORDER BY ts", (since,)).fetchall()

    groups = []
    for r in rows:
        toks = _tok(r[2])
        if not toks:
            continue
        best, best_j = None, 0.0
        for g in groups:

            if r[1] - g["members"][-1]["ts"] > window_min * 60:
                continue
            inter = len(toks & g["tokens"])
            union = len(toks | g["tokens"])
            jac = inter / union if union else 0.0
            if jac > best_j:
                best, best_j = g, jac
        if best is not None and best_j >= min_jaccard:
            best["members"].append({"id": r[0], "ts": r[1], "q": r[2],
                                    "n_hits": r[3]})
        else:
            groups.append({"tokens": toks,
                           "members": [{"id": r[0], "ts": r[1], "q": r[2],
                                        "n_hits": r[3]}]})

    repeated = [g for g in groups if len(g["members"]) >= 2]
    items = []
    for g in repeated:
        ms = g["members"]
        items.append({
            "q": ms[0]["q"],
            "n": len(ms),
            "span_min": round((ms[-1]["ts"] - ms[0]["ts"]) / 60.0, 1),
            "any_zero": any(m["n_hits"] == 0 for m in ms),
            "n_hits": [m["n_hits"] for m in ms],
        })
    items.sort(key=lambda x: -x["n"])
    return {"n": len(items), "n_events": sum(i["n"] for i in items),
            "n_zero_groups": sum(1 for i in items if i["any_zero"]),
            "items": items[:12]}

def low_score(con, days: int = 30, pct: float = 0.1) -> dict:
    since = _since(days)
    tops = []
    for (hj,) in con.execute(
            "SELECT hits_json FROM query_log WHERE ts >= ? AND hits_json IS NOT NULL",
            (since,)):
        try:
            d = json.loads(hj)
        except ValueError:
            continue
        if not d:
            continue
        s = d[0].get("score")
        if isinstance(s, (int, float)):
            tops.append(float(s))
    if len(tops) < 5:
        return {"n": 0, "note": "样本不足 5 条，分位数不可用"}
    tops.sort()
    cut = tops[max(0, int(len(tops) * pct) - 1)]
    low = [t for t in tops if t <= cut]
    return {"n": len(low), "n_total": len(tops), "cut": round(cut, 4),
            "median": round(tops[len(tops) // 2], 4)}

def _since(days: int) -> int:

    from bili_digest.schema.core_schema import now_sec
    return now_sec() - days * 86400

def report(con, days: int = 30) -> dict:
    return {"days": days,
            "zero": zero_results(con, days),
            "repeat": repeated_queries(con, days),
            "low": low_score(con, days)}

def render(con, days: int = 30) -> list:
    r = report(con, days)
    z, p, l = r["zero"], r["repeat"], r["low"]
    L = ["", "  ── 检索失败信号（query_log · 近 %d 天）──" % days, ""]
    L.append("  查询 %d 条 · 零结果 %d 条（%.1f%%）· 其中真缺口 %d、噪音 %d"
             % (z["n_queries"], z["n_zero"], 100 * z["rate"],
                len(z["real"]), len(z["noise"])))
    for x in z["real"][:6]:
        L.append("      · 「%s」(%s/%s)" % (x["question"][:34], x["mode"], x["granularity"]))
    if z["noise"]:
        L.append("      （已滤掉的噪音：%s）"
                 % "、".join("「%s」" % n["question"][:10] for n in z["noise"][:5]))
    L.append("")
    L.append("  重复查询 %d 组（共 %d 次，其中 %d 组里有一次是零结果）"
             " —— 行为即证据，无需标注" % (p["n"], p["n_events"], p["n_zero_groups"]))
    for x in p["items"][:5]:
        L.append("      · 「%s」搜了 %d 次（跨度 %.0f 分钟，各次结果数 %s）"
                 % (x["q"][:26], x["n"], x["span_min"], x["n_hits"]))
    L.append("")
    if l.get("n"):
        L.append("  低分结果 %d/%d 条（top-1 <= %.4f，取自分布底 %d%%；中位 %.4f）"
                 % (l["n"], l["n_total"], l["cut"], 10, l["median"]))
    else:
        L.append("  低分结果：%s" % l.get("note", "无数据"))
    L.append("")
    L.append("  注：本轮只观测，不改检索算法。修的方向要等这些数字攒够才谈。")
    L.append("")
    return L

def main() -> int:
    import argparse
    paths.enable_utf8_stdout()
    ap = argparse.ArgumentParser(description="检索失败信号（零结果/重复查询/低分）")
    ap.add_argument("--days", type=int, default=30)
    a = ap.parse_args()
    from bili_digest.schema.core_schema import connect
    from bili_digest.paths import DB
    con = connect(DB)
    try:
        for ln in render(con, a.days):
            print(ln)
    finally:
        con.close()
    return 0

if __name__ == "__main__":
    sys.exit(main())
