#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
import paths

import re
import time
from datetime import datetime, timedelta, timezone

def now_sec() -> int:
    return int(time.time())

_REL = re.compile(r"^(\d+)\s*(m|min|h|d|w|mo|y)$", re.I)
_NAMED = {
    "today": 0, "yesterday": 1, "last_week": 7, "last_month": 30,
    "last_year": 365, "this_week": 7, "this_month": 30,
}
UNIT = {"m": 60, "min": 60, "h": 3600, "d": 86400,
        "w": 7 * 86400, "mo": 30 * 86400, "y": 365 * 86400}

def _named_start(name: str) -> int:
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    if name == "today":
        return int(today.timestamp())
    if name == "yesterday":
        return int((today - timedelta(days=1)).timestamp())
    if name == "last_week":
        monday = today - timedelta(days=today.weekday())
        return int((monday - timedelta(days=7)).timestamp())
    if name == "last_month":
        first = today.replace(day=1)
        prev_last = first - timedelta(days=1)
        return int(prev_last.replace(day=1).timestamp())
    if name == "last_year":
        return int(today.replace(month=1, day=1).replace(
            year=today.year - 1).timestamp())

    return now_sec() - _NAMED.get(name, 1) * 86400

def parse_time(s) -> int | None:
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return int(s)
    s = str(s).strip().lower()
    if not s:
        return None

    now = now_sec()
    if s in _NAMED:
        return _named_start(s)

    m = _REL.match(s)
    if m:
        n, u = int(m.group(1)), m.group(2).lower()
        return now - n * UNIT.get(u, 86400)

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                "%Y/%m/%d", "%Y%m%d"):
        try:

            dt = datetime.strptime(s, fmt)
            return int(dt.timestamp())
        except ValueError:
            continue
    return None

def time_range(since=None, until=None) -> tuple[int | None, int | None]:
    return parse_time(since), parse_time(until)

def recency_weight(ts: int | None, ref: int | None = None,
                   half_life_days: float = 180.0) -> float:
    if not ts:
        return 0.5
    ref = ref or now_sec()
    age_days = max(0.0, (ref - ts) / 86400.0)
    return float(0.5 ** (age_days / max(half_life_days, 1.0)))

def describe(since=None, until=None) -> str:
    s, u = time_range(since, until)
    if not s and not u:
        return "不限"
    def fmt(x):
        return datetime.fromtimestamp(x).strftime("%Y-%m-%d") if x else "…"
    return f"{fmt(s)} ~ {fmt(u) if u else '现在'}"

def filter_items(con, uids: list[str], since=None, until=None,
                 field: str = "saved_at") -> set[str]:
    s, u = time_range(since, until)
    if not s and not u:
        return set(uids)
    if not uids:
        return set()
    ph = ",".join("?" * len(uids))
    sql = f"SELECT uid FROM item WHERE uid IN ({ph})"
    args = list(uids)
    if s:
        sql += f" AND COALESCE({field},0) >= ?"
        args.append(s)
    if u:
        sql += f" AND COALESCE({field},0) <= ?"
        args.append(u)
    return {r[0] for r in con.execute(sql, args)}

if __name__ == "__main__":
    import sys
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    print("  解析自检：")
    for s in ("30d", "7d", "12h", "2w", "3mo", "1y", "last_week",
              "today", "2026-01-01", None, "abc"):
        t = parse_time(s)
        d = datetime.fromtimestamp(t).strftime("%Y-%m-%d") if t else "None"
        print(f"    {str(s):<14} → {d}")
    now = now_sec()
    print("\n  半衰期权重（180 天）：")
    for days in (0, 30, 90, 180, 365, 730):
        print(f"    {days:>4} 天前 → {recency_weight(now - days*86400):.3f}")
