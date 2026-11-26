from __future__ import annotations

import sqlite3
import sys

from bili_digest.paths import DB
from bili_digest.retrieve.temporal import now_sec
from bili_digest.schema.core_schema import (
    STATUS_REPAIRED, log_job, status_in,
)

_SA = status_in(STATUS_REPAIRED)

GONE_MARKS = ("62012", "稿件不可见", "已删除", "未能从参数", "识别 bv")

WINDOW_DAYS = 7

def find_gone(con: sqlite3.Connection, window_days: int = WINDOW_DAYS) -> list:
    rows = con.execute(f"""
        SELECT j.scope, j.detail, j.ts FROM job_log j
        WHERE j.status='fail' AND j.ts > ?
          AND NOT EXISTS(SELECT 1 FROM job_log k
                         WHERE k.scope=j.scope
                           AND k.status IN {_SA} AND k.ts > j.ts)
        ORDER BY j.ts DESC
        """, (now_sec() - window_days * 86400,)).fetchall()
    out = []
    for scope, detail, ts in rows:
        low = (detail or "").lower()
        if any(w in low for w in GONE_MARKS):
            out.append((scope, detail or "", ts))
    return out

def mark(con: sqlite3.Connection, apply: bool = False) -> dict:
    targets = find_gone(con)
    if apply:
        for scope, detail, _ts in targets:

            log_job(con, scope, "mark_gone", "skip",
                    "条目已不存在，不再重试（原失败：%s）" % detail[:80],
                    actor="selfheal", reversible=True)
        con.commit()
    return {"gone": len(targets), "applied": bool(apply),
            "scopes": [s for s, _d, _t in targets]}

def main() -> int:

    import argparse
    ap = argparse.ArgumentParser(
        description="把「确实没了」的抓取失败标成 skip，让它可以退出 WARN")
    ap.add_argument("--apply", action="store_true",
                    help="真的写入 skip（默认干跑，只报告）")
    a = ap.parse_args()
    apply = a.apply
    con = sqlite3.connect(str(DB), timeout=30)
    try:
        res = mark(con, apply=apply)
    finally:
        con.close()
    verb = "已标记" if res["applied"] else "待标记（干跑）"
    print("%s %d 条 gone 类失败" % (verb, res["gone"]))
    for s in res["scopes"]:
        print("  ·", s)
    if res["gone"] and not res["applied"]:
        print("\n加 --apply 才会真的写入 skip")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
