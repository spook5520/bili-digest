# -*- coding: utf-8 -*-

import os as _os
import sys as _sys

ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
from bili_digest import paths

import json as _json
import random as _random

def pool(con, min_topics: int = 2) -> list:
    rows = con.execute(
        "SELECT d.item_uid, d.content, i.title FROM derived d "
        "JOIN item i ON i.uid = d.item_uid WHERE d.kind = 'topics'").fetchall()
    out = []
    for r in rows:
        try:
            ts = [str(t).strip() for t in _json.loads(str(r["content"])) if str(t).strip()]
        except Exception:
            continue
        if len(ts) >= min_topics:
            out.append({"uid": r["item_uid"], "topics": ts,
                        "title": str(r["title"] or "")})
    return out

def make_quiz(con, n: int = 5, seed: int | None = None,
              only_recent_days: int | None = None) -> list:
    import time as _t
    items = pool(con)
    if only_recent_days:
        cutoff = int(_t.time()) - only_recent_days * 86400
        kept = []
        for it in items:
            row = con.execute("SELECT published_at, saved_at FROM item WHERE uid = ?",
                              (it["uid"],)).fetchone()
            ts = 0
            if row:
                ts = int(row[0] or 0) or int(row[1] or 0)
            if ts >= cutoff:
                kept.append(it)
        items = kept
    if not items:
        return []
    rng = _random.Random(seed)
    rng.shuffle(items)

    out = []
    for it in items[:n]:
        d = con.execute(
            "SELECT content FROM derived WHERE item_uid = ? AND kind = 'summary'",
            (it["uid"],)).fetchone()
        summary = str(d["content"]) if d else ""
        if not summary:
            continue

        give = it["topics"][:3]
        out.append({
            "uid": it["uid"],
            "question": "看到这些主题词，你能想起这条讲了什么吗？",
            "topics": give,
            "answer_summary": summary,
            "title": it["title"],
        })
    return out

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS quiz_card (",
    "  item_uid TEXT PRIMARY KEY,",
    "  state_json TEXT NOT NULL,",
    "  due TEXT NOT NULL,",
    "  updated_at TEXT NOT NULL",
    ")",
)
_LOG = (
    "CREATE TABLE IF NOT EXISTS quiz_log (",
    "  item_uid TEXT NOT NULL,",
    "  rating INTEGER NOT NULL,",
    "  reviewed_at TEXT NOT NULL,",
    "  due_after TEXT NOT NULL",
    ")",
)

def _ensure_tables(con):

    for stmt in (_SCHEMA, _LOG):
        con.execute("".join(stmt) if isinstance(stmt, tuple) else stmt)
    con.commit()

def rate(con, item_uid: str, stars: int, now=None):
    try:
        from fsrs import Scheduler, Card, Rating
    except ImportError:
        return None
    import json as _j
    from datetime import datetime, timezone
    _ensure_tables(con)
    now = now or datetime.now(timezone.utc)
    rating = {1: Rating.Again, 2: Rating.Hard, 3: Rating.Good, 4: Rating.Easy}[max(1, min(4, stars))]
    row = con.execute("SELECT state_json FROM quiz_card WHERE item_uid = ?",
                      (item_uid,)).fetchone()
    if row:
        c = Card.from_dict(_j.loads(row[0]))
    else:
        c = Card()

    from datetime import timedelta as _td
    _sched = Scheduler(desired_retention=0.90,
                       learning_steps=(_td(days=1),))
    c2, _log = _sched.review_card(c, rating, review_datetime=now)
    due = c2.due.isoformat()
    con.execute("INSERT OR REPLACE INTO quiz_card VALUES (?,?,?,?)",
                (item_uid, _j.dumps(c2.to_dict()), due, now.isoformat()))
    con.execute("INSERT INTO quiz_log VALUES (?,?,?,?)",
                (item_uid, stars, now.isoformat(), due))
    con.commit()
    return due

def due_now(con, limit: int = 20):
    try:
        _ensure_tables(con)
        from datetime import datetime, timezone
        return [r[0] for r in con.execute(
            "SELECT item_uid FROM quiz_card WHERE due <= ? ORDER BY due LIMIT ?",
            (datetime.now(timezone.utc).isoformat(), limit))]
    except Exception:
        return []

def main() -> int:
    import argparse
    from bili_digest import paths as _p
    _p.enable_utf8_stdout()
    from bili_digest.schema.core_schema import connect
    from bili_digest.paths import DB
    ap = argparse.ArgumentParser(description="从库里出题（主动回忆）")
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--recent", type=int, default=None,
                    help="只从近 N 天的条目里出题")
    ap.add_argument("--rate", action="store_true",
                    help="揭晓后给四档评分（1忘记/2困难/3记得/4轻松），"
                         "写入 quiz_card/quiz_log 并算下次到期（需 pip install fsrs）")
    ap.add_argument("--due", action="store_true",
                    help="只出到期的题（按 FSRS due 升序；没有记录的条目不会出现）")
    ap.add_argument("--no-wait", action="store_true",
                    help="不逐题停顿，一次全打出来")
    a = ap.parse_args()

    con = connect(DB)
    con.row_factory = __import__("sqlite3").Row
    try:
        if getattr(a, "due", False):
            du = due_now(con, a.n)
            if not du:
                print("  没有到期的条目。先跑一次 --rate 建立复习记录，"
                      "或直接出题（会从全部 597 条里抽）")
                return
            qs = [q for q in make_quiz(con, 10 ** 6, a.seed, a.recent)
                  if q["uid"] in set(du)][:a.n]
            if not qs:
                print("  到期的条目里没有可出题的（需要 derived.topics）")
                return
        else:
            qs = make_quiz(con, a.n, a.seed, a.recent)
        if not qs:
            print("  没有可出题的条目（需要 derived.topics）")
            return
        print("  共 %d 题。先自己想，再揭晓。" % len(qs))
        print()
        for i, q in enumerate(qs, 1):
            print("  ── 第 %d 题 ─────────────────────────────" % i)
            print("  主题词: %s" % " / ".join(q["topics"]))
            print("  想好了吗？")
            if not a.no_wait:
                try:
                    input("  按回车揭晓…")
                except EOFError:
                    pass
            print()
            print("  摘要: %s" % q["answer_summary"][:300])
            print("  出处: %s《%s》" % (q["uid"].split(":")[-1], q["title"][:40]))
            if getattr(a, "rate", False):
                try:
                    s = input("  评分 1忘记 2困难 3记得 4轻松（回车跳过）: ").strip()
                except EOFError:
                    s = ""
                if s in ("1", "2", "3", "4"):
                    nd = rate(con, q["uid"], int(s))
                    if nd:
                        print("  下次到期: %s" % nd[:19])
                    else:
                        print("  （未装 fsrs，跳过记录：pip install fsrs）")
            print()
    finally:
        con.close()

if __name__ == "__main__":
    main()
