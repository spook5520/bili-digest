from __future__ import annotations

import argparse
import json
import sqlite3
import sys

from bili_digest.paths import DB

WINDOW_SEC = 600

def link(con, window: int = WINDOW_SEC, limit: int = 40) -> list[dict]:
    exists = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='usage_log'").fetchone()
    if not exists:
        return [{"error": "usage_log 表不存在 —— 还没跑过任何 expand"}]
    qs = con.execute(
        "SELECT id, ts, question, hits_json FROM query_log "
        "WHERE mode = 'ask' ORDER BY ts").fetchall()
    if not qs:
        return []
    ids_cache: dict[int, list] = {}

    def _ids(qid: int, raw) -> list:
        if qid not in ids_cache:
            try:
                hits = json.loads(raw) if raw else []
            except Exception:
                hits = []
            ids_cache[qid] = [h.get("block_id") or h.get("id")
                              for h in hits if isinstance(h, dict)]
        return ids_cache[qid]

    seen: dict[tuple, dict] = {}
    for bid, at in con.execute(
            "SELECT block_id, at FROM usage_log WHERE kind = 'expand' ORDER BY at"):
        owner = None
        for qid, ts, question, raw in qs:
            if ts <= at:
                owner = (qid, ts, question, raw)
            else:
                break
        if owner is None:
            continue
        qid, ts, question, raw = owner
        if at - ts > window:
            continue
        key = (qid, bid)
        if key in seen:
            seen[key]["repeat"] += 1
            continue
        ids = _ids(qid, raw)
        rank = (ids.index(bid) + 1) if bid in ids else None
        seen[key] = {"question_id": qid, "question": question, "block_id": bid,
                     "rank_in_answer": rank, "seconds_after": at - ts,
                     "repeat": 1, "was_returned": rank is not None}
    rows = sorted(seen.values(), key=lambda r: (r["question_id"], r["block_id"]))
    return rows[-limit:]

def cmd_link(args) -> int:
    con = sqlite3.connect(str(DB))
    try:
        rows = link(con, args.window, args.n)
    finally:
        con.close()
    if rows and "error" in rows[0]:
        print(f"  [!] {rows[0]['error']}")
        return 1
    if args.json:
        sys.stdout.buffer.write(json.dumps(rows, ensure_ascii=False).encode("utf-8"))
        sys.stdout.buffer.write(b"\n")
        return 0
    adopted = [r for r in rows if r["was_returned"]]
    outside = [r for r in rows if not r["was_returned"]]
    calls = sum(r["repeat"] for r in rows)
    print()
    print(f"  窗口 {args.window}s 内：「走过」{len(rows)} 次（共 {calls} 次 expand 调用）")
    print(f"    其中「这次答案里返回过的」  {len(adopted)}   <- 采纳信号")
    print(f"    其中「没在这次答案里」      {len(outside)}   <- 用户自己找的，另一回事")
    if adopted:
        delays = sorted(r["seconds_after"] for r in adopted)
        print(f"    采纳的延迟：最快 {delays[0]}s · 中位 {delays[len(delays)//2]}s · 最慢 {delays[-1]}s")
        print("       （延迟分布要几百条才有意义 —— 现在只记着，不用它做权重）")
    print()
    for r in rows[:20]:
        mark = f"第 {r['rank_in_answer']} 条" if r["rank_in_answer"] else "不在答案里"
        rep = f" ×{r['repeat']}" if r["repeat"] > 1 else ""
        print(f"  q{r['question_id']}  {r['seconds_after']:>4}s  #{r['block_id']}{rep}  {mark}   {(r['question'] or '')[:24]}")
    return 0

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="kb-usage")
    p.add_argument("--window", type=int, default=WINDOW_SEC)
    p.add_argument("--n", type=int, default=40)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_link)
    a = p.parse_args(argv)
    return a.fn(a)

if __name__ == "__main__":
    raise SystemExit(main())
