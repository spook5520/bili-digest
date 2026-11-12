# -*- coding: utf-8 -*-

import os as _os
import sys as _sys

ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
from bili_digest import paths

import json as _json
import time as _time

def touched_uids(con) -> set:
    out = set()
    for r in con.execute("SELECT hits_json FROM query_log WHERE hits_json IS NOT NULL"):
        try:
            for h in _json.loads(str(r[0])):
                u = h.get("uid")
                if u:
                    out.add(u)
        except Exception:
            continue
    return out

def never_touched(con, top: int = 8) -> dict:
    seen = touched_uids(con)
    rows = con.execute(
        "SELECT uid, title, saved_at, duration_sec FROM item "
        "WHERE saved_at > 0 ORDER BY saved_at ASC").fetchall()
    now = int(_time.time())
    cold = []
    for r in rows:
        if r[0] in seen:
            continue
        age = (now - int(r[2])) // 86400
        cold.append({"uid": r[0], "title": str(r[1] or ""),
                     "days": age, "duration_sec": r[3]})
    total = len(cold)
    cold.sort(key=lambda x: -x["days"])
    return {"total": total, "sample": cold[:top],
            "touched": len(seen), "items": len(rows)}

def shells(con, top: int = 5) -> dict:
    rows = con.execute(
        "SELECT i.uid, i.title, i.duration_sec FROM item i "
        "WHERE NOT EXISTS (SELECT 1 FROM content_block b WHERE b.item_uid = i.uid) "
        "ORDER BY i.duration_sec DESC LIMIT ?", (top,)).fetchall()
    total = con.execute(
        "SELECT COUNT(*) FROM item i WHERE NOT EXISTS "
        "(SELECT 1 FROM content_block b WHERE b.item_uid = i.uid)").fetchone()[0]
    return {"total": total, "sample": [
        {"uid": r[0], "title": str(r[1] or ""), "duration_sec": r[2]} for r in rows]}

def _picked_uids(con) -> set:
    try:
        return {r[0] for r in con.execute("SELECT item_uid FROM pick_log")}
    except Exception:
        return set()

def themes(con, min_members: int = 3, top: int = 6) -> dict:
    A = touched_uids(con)
    B = _picked_uids(con)
    universe = {r[0] for r in con.execute(
        "SELECT DISTINCT item_uid FROM content_block")}
    topics = con.execute(
        "SELECT topic, COUNT(*) n FROM topic_member GROUP BY topic "
        "HAVING n >= ? ORDER BY n DESC", (min_members,)).fetchall()

    cold_a, cold_b = 0, 0
    rows = []
    for t in topics:
        members = [r[0] for r in con.execute(
            "SELECT item_uid FROM topic_member WHERE topic=?", (t[0],))]
        ha = sum(1 for m in members if m in A)
        hb = sum(1 for m in members if m in B)
        if ha == 0:
            cold_a += 1
        if hb == 0:
            cold_b += 1
        last = con.execute(
            "SELECT MAX(saved_at) FROM item WHERE uid IN (%s)"
            % ",".join("?" * len(members)), members).fetchone()[0] or 0
        rows.append({"topic": t[0], "n": t[1], "hit": ha, "pick": hb,
                     "age": int((_time.time() - last) // 86400) if last else -1})
    rows.sort(key=lambda x: -x["age"])
    total_a = len(topics)
    return {
        "n_topics": total_a,
        "cold_a": cold_a, "cold_b": cold_b,
        "cold_a_pct": 100.0 * cold_a / max(total_a, 1),
        "cold_b_pct": 100.0 * cold_b / max(total_a, 1),
        "sample": [r for r in rows if r["hit"] == 0][:top],
        "touched": len(A & universe), "universe": len(universe),
        "touched_pct": 100.0 * len(A & universe) / max(len(universe), 1),
        "history": _touched_history(con),
    }

def _touched_history(con, keep: int = 8) -> list:
    import json as _j
    try:
        row = con.execute("SELECT v FROM meta_kv WHERE k='weekly:touched'").fetchone()
        hist = _j.loads(row[0]) if row else []
    except Exception:
        hist = []
    return hist[-keep:]

def _record_touched(con, a: dict) -> None:
    import json as _j
    hist = _touched_history(con, keep=8)
    today = int(_time.time()) // 86400
    if hist and int(hist[-1].get("ts", 0)) // 86400 == today:
        hist[-1] = {"ts": int(_time.time()), "hit": a["touched"],
                    "all": a["universe"]}
    else:
        hist.append({"ts": int(_time.time()), "hit": a["touched"],
                     "all": a["universe"]})
    try:
        con.execute("INSERT INTO meta_kv(k,v) VALUES('weekly:touched',?) "
                    "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                    (_j.dumps(hist[-8:]),))
        con.commit()
    except Exception:
        pass

def build(con) -> dict:
    return {"cold": never_touched(con), "shells": shells(con),
            "themes": themes(con)}

def proposals(data: dict) -> list:
    out = []
    c = data["cold"]
    if c["sample"]:
        b = c["sample"][0]
        out.append("回看：%s（%s，%d 天前收的，从没被查过）" % (
            b["title"][:28], b["uid"].split(":")[-1], b["days"]))
    s = data["shells"]
    if s["sample"]:
        b = s["sample"][0]
        out.append("补抓：%s（%s，%d 分钟）—— 收藏了但没内容" % (
            b["title"][:28], b["uid"].split(":")[-1], (b["duration_sec"] or 0) // 60))
    if len(c["sample"]) > 3:
        b = c["sample"][3]
        out.append("回看：%s（%s，%d 天前）" % (
            b["title"][:28], b["uid"].split(":")[-1], b["days"]))
    return out[:3]

def render(con, top: int = 8) -> list:
    d = build(con)
    c, sh = d["cold"], d["shells"]
    L = ["  ── 本周简报 ─────────────────────────────", "",
         "  从没被查过   %d / %d 条" % (c["total"], c["items"]),
         "  （有检索记录的只有 %d 条）" % c["touched"]]
    for b in c["sample"][:top]:
        L.append("    %4d 天  %-14s %s" % (b["days"], b["uid"].split(":")[-1],
                                          b["title"][:44]))
    L += ["", "  空  壳       %d 条（收藏了但没内容）" % sh["total"]]
    for b in sh["sample"][:3]:
        L.append("    %4d 分  %-14s %s" % ((b["duration_sec"] or 0) // 60,
                                          b["uid"].split(":")[-1], b["title"][:44]))

    th = d["themes"]
    hist = th["history"]
    prev = hist[-1] if hist else None
    L += ["", "  没被翻过的主题（按最后一次收藏时间倒序）"]
    for x in th["sample"]:
        L.append("    · %-12s %2d 条 · 最近收藏 %3d 天前 · 检索 0 次"
                 % (x["topic"][:12], x["n"], x["age"]))
    L.append("      共 %d / %d 个主题（成员>=3）从未被检索过（%.0f%%）"
             % (th["cold_a"], th["n_topics"], th["cold_a_pct"]))
    L.append("      另：从未被【采纳】的是 %d / %d（%.0f%%）—— 不用它做判据"
             % (th["cold_b"], th["n_topics"], th["cold_b_pct"]))
    L.append("      （采纳率对行动没有指导性：做什么它都不会变；检索触达搜一次就动）")
    trend = ""
    if prev and prev.get("all"):
        d_pct = th["touched_pct"] - 100.0 * prev["hit"] / prev["all"]
        trend = "（上次 %.1f%% → 本次 %.1f%%，%s）" % (
            100.0 * prev["hit"] / prev["all"], th["touched_pct"],
            "涨了" if d_pct > 0.05 else ("没动" if abs(d_pct) <= 0.05 else "退了"))
    L.append("      触达率 %d/%d = %.1f%%%s"
             % (th["touched"], th["universe"], th["touched_pct"], trend))

    if len(hist) >= 4:
        last4 = hist[-4:]
        if len({(h["hit"], h["all"]) for h in last4}) == 1:
            L += ["", "      ⚠ 这一行连续 4 次触达率完全相同（%d/%d）——"
                  % (last4[-1]["hit"], last4[-1]["all"]),
                  "        它没有让任何人行动，按判据该删掉。"]
    _record_touched(con, th)

    ps = proposals(d)
    if ps:
        L += ["", "  这周可以做"]
        for i2, p in enumerate(ps, 1):
            L.append("    %d. %s" % (i2, p))
    return L

def main() -> int:
    import argparse
    from bili_digest import paths as _p
    _p.enable_utf8_stdout()
    from bili_digest.schema.core_schema import connect
    from bili_digest.paths import DB
    ap = argparse.ArgumentParser(description="每周简报：把「该看什么」主动送到眼前")
    ap.add_argument("--top", type=int, default=8, help="每节列几条（默认 8）")
    a = ap.parse_args()
    con = connect(DB)
    con.row_factory = __import__("sqlite3").Row
    try:
        for ln in render(con, getattr(a, 'top', 8)):
            print(ln)
    finally:
        con.close()

if __name__ == "__main__":
    main()
