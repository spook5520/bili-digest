# -*- coding: utf-8 -*-

import json as _json
import os as _os
import sys as _sys
from collections import Counter, defaultdict

from bili_digest import paths

def _cutoff(days: int) -> int:
    import datetime as dt
    return int((dt.datetime.now() - dt.timedelta(days=days)).timestamp())

def _fmt_date(ts) -> str:
    import datetime as dt
    try:
        v = int(ts or 0)
        return dt.datetime.fromtimestamp(v).strftime("%Y-%m-%d") if v else "?"
    except Exception:
        return "?"

def _topics_of(r) -> list:
    try:
        ts = _json.loads(str(r[4] or "[]"))
    except Exception:
        return []
    return [t.strip() for t in ts if isinstance(t, str) and t.strip()]

def trends(con, days: int = 7, min_items: int = 2, limit: int = 15,
           verbose: bool = False) -> list:
    since = _cutoff(days)
    rows = con.execute(
        "SELECT i.uid, i.title, i.author_name, i.published_at, d.content "
        "FROM item i JOIN derived d ON d.item_uid = i.uid "
        "WHERE d.kind = 'topics' AND i.published_at >= ?",
        (since,)).fetchall()
    by_topic = defaultdict(list)
    for r in rows:
        for t in _topics_of(r):
            by_topic[t].append(r)
    out = []
    for topic, items in by_topic.items():
        if len(items) < min_items:
            continue
        authors = {str(i[2] or "") for i in items}
        out.append({
            "topic": topic,
            "n_items": len(items),
            "n_authors": len(authors),
            "latest": _fmt_date(max(int(i[3] or 0) for i in items)),
            "earliest": _fmt_date(min(int(i[3] or 0) for i in items)),
            "sample": [(i[1], i[2], i[3]) for i in items[:5]],
        })
    out.sort(key=lambda x: (-x["n_items"], -x["n_authors"]))
    if verbose:
        print("  近 %d 天（>= %s）· %d 个话题达到 %d 条以上" % (
            days, since, len(out), min_items))
    return out[:limit]

def author_timeline(con, author: str, since: str = None, until: str = None,
                    verbose: bool = False) -> dict:
    sql = ("SELECT uid, title, published_at, duration_sec FROM item "
           "WHERE author_name LIKE ?")
    args = ["%" + author + "%"]

    def _ts(day: str, end: bool = False) -> int:
        import datetime as dt
        d = dt.date.fromisoformat(str(day)[:10])
        if end:
            d = d + dt.timedelta(days=1)
        return int(dt.datetime(d.year, d.month, d.day).timestamp()) - (1 if end else 0)
    if since:
        sql += " AND published_at >= ?"
        args.append(_ts(since))
    if until:
        sql += " AND published_at <= ?"
        args.append(_ts(until, end=True))
    sql += " ORDER BY published_at"
    rows = con.execute(sql, args).fetchall()
    if not rows:
        return {"author": author, "n": 0, "items": [],
                "note": "该 UP 在这个时间范围内没有条目（名字要匹配得上）"}

    import datetime as _dt
    dates = [_dt.datetime.fromtimestamp(int(r[2])).strftime("%Y-%m-%d")
             for r in rows if r[2]]
    span = cadence = None
    if len(dates) >= 2:
        import datetime as dt
        try:
            d0 = dt.date.fromisoformat(min(dates))
            d1 = dt.date.fromisoformat(max(dates))
            span = (d1 - d0).days
            cadence = round(span / (len(dates) - 1), 1)
        except Exception:
            pass
    if verbose:
        print("  %s · %d 条 · %s ~ %s%s" % (
            author, len(rows), min(dates) if dates else "?",
            max(dates) if dates else "?",
            ("  · 平均 %.0f 天更一次" % cadence) if cadence else ""))
    return {"author": author, "n": len(rows),
            "first": min(dates) if dates else None,
            "last": max(dates) if dates else None,
            "span_days": span, "cadence_days": cadence,
            "items": [{"uid": r[0], "title": r[1],
                       "date": _dt.datetime.fromtimestamp(int(r[2])).strftime("%Y-%m-%d")
                       if r[2] else None,
                       "duration_sec": r[3]} for r in rows]}

def dormant_topics(con, quiet_days: int = 90, min_items: int = 3,
                   limit: int = 15) -> list:
    cut = _cutoff(quiet_days)
    rows = con.execute(
        "SELECT i.uid, i.published_at, d.content "
        "FROM item i JOIN derived d ON d.item_uid = i.uid "
        "WHERE d.kind = 'topics'").fetchall()
    total, recent = Counter(), Counter()
    for uid, pub, content in rows:
        try:
            ts = _json.loads(str(content or "[]"))
        except Exception:
            continue
        for t in ts:
            if not (isinstance(t, str) and t.strip()):
                continue
            t = t.strip()
            total[t] += 1
            if int(pub or 0) >= cut:
                recent[t] += 1
    out = [{"topic": t, "total": n, "recent": 0}
           for t, n in total.items()
           if n >= min_items and recent.get(t, 0) == 0]
    out.sort(key=lambda x: -x["total"])
    return out[:limit]

def first_mention(con, keyword: str, limit: int = 5) -> list:
    rows = con.execute(
        "SELECT uid, title, author_name, published_at FROM item "
        "WHERE title LIKE ? OR uid IN (SELECT item_uid FROM content_block "
        "WHERE text LIKE ?) ORDER BY published_at LIMIT ?",
        ("%" + keyword + "%", "%" + keyword + "%", limit)).fetchall()
    return [{"uid": r[0], "title": r[1], "author": r[2],
             "date": _fmt_date(r[3])} for r in rows]

def main() -> int:
    import argparse
    from bili_digest import paths as _p
    _p.enable_utf8_stdout()
    from bili_digest.schema.core_schema import connect
    from bili_digest.paths import DB
    ap = argparse.ArgumentParser(description="时间维度：热点 / UP 主 / 沉寂 / 首发")
    ap.add_argument("--hot", type=int, metavar="DAYS", help="近 N 天的热点")
    ap.add_argument("--author", help="某 UP 的视频时间线")
    ap.add_argument("--since", help="起始日期 YYYY-MM-DD")
    ap.add_argument("--until", help="截止日期 YYYY-MM-DD")
    ap.add_argument("--dormant", type=int, metavar="DAYS", help="沉寂话题")
    ap.add_argument("--first", help="谁最早讲了某关键词")
    ap.add_argument("--min-items", type=int, default=2)
    a = ap.parse_args()

    con = connect(DB)
    con.row_factory = __import__("sqlite3").Row
    try:
        if a.hot:
            for t in trends(con, days=a.hot, min_items=a.min_items, verbose=True):
                print("    %-22s %2d 条 · %d 个 UP · %s ~ %s" % (
                    t["topic"][:22], t["n_items"], t["n_authors"],
                    t["earliest"][:10], t["latest"][:10]))
        elif a.author:
            r = author_timeline(con, a.author, a.since, a.until, verbose=True)
            for it in r["items"][:20]:
                print("    %s  %s" % (it["date"], str(it["title"])[:56]))
            if r["n"] > 20:
                print("    …还有 %d 条" % (r["n"] - 20))
        elif a.dormant:
            for t in dormant_topics(con, quiet_days=a.dormant):
                print("    %-24s 共 %2d 条 · 近 %d 天 0 条" % (
                    t["topic"][:24], t["total"], a.dormant))
        elif a.first:
            for it in first_mention(con, a.first):
                print("    %s  %-40s UP:%s" % (
                    it["date"], str(it["title"])[:40], str(it["author"])[:12]))
        else:
            ap.print_help()
    finally:
        con.close()

if __name__ == "__main__":
    main()
