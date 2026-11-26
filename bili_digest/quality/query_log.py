# -*- coding: utf-8 -*-

import sys
from collections import Counter

def load(con, since: int = 0) -> list:
    try:
        rows = list(con.execute(
            "SELECT question, mode, granularity, n_hits, ms, ts "
            "FROM query_log WHERE ts >= ? ORDER BY ts", (since,)))
    except Exception:
        return []
    out = []
    for r in rows:
        q = (r[0] or "").strip()
        if not q:
            continue
        out.append({"q": q, "mode": r[1], "granularity": r[2],
                    "n_hits": r[3], "ms": r[4], "ts": r[5]})
    return out

def machine_filter(rows: list) -> tuple:
    import time as _t
    human, machine = [], []
    prev_ts = None
    day_cnt = Counter()
    try:
        from bili_digest.tools.eval_retrieval import _NATURAL_QUERIES as _NQ
        probe = set(_NQ)
    except Exception:
        probe = set()
    for r in rows:
        ts = r.get("ts") or 0
        day = _t.strftime("%Y-%m-%d", _t.localtime(ts)) if ts else "?"
        day_cnt[day] += 1
        why = None
        if r["q"] in probe:
            why = "自我测试"
        elif prev_ts is not None and 0 <= ts - prev_ts < 120:
            why = "连发(%.0fs)" % (ts - prev_ts)
        elif day_cnt[day] > 10:
            why = "当天第 %d 次" % day_cnt[day]
        (machine if why else human).append((r, why) if why else r)
        prev_ts = ts
    return human, machine

def main() -> int:
    from bili_digest.schema.core_schema import connect
    from bili_digest.paths import DB
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    argv = sys.argv[1:]
    con = connect(DB)
    rows = load(con)
    cnt = Counter(r["q"] for r in rows)

    def _opt(name, default=None):
        if name not in argv:
            return default
        i = argv.index(name)
        return argv[i + 1] if i + 1 < len(argv) and not argv[i + 1].startswith("-") else True

    export = _opt("--export")
    minrep = int(_opt("--min-repeat", 1) or 1)
    show_filter = "--filter" in argv

    picked = [r for r in rows if cnt[r["q"]] >= minrep]
    seen, uniq = set(), []
    for r in picked:
        if r["q"] not in seen:
            seen.add(r["q"])
            uniq.append(r["q"])

    if export and export is not True:
        with open(export, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(uniq) + "\n")
        print("导出 %d 条去重查询 -> %s" % (len(uniq), export))
        return 0

    print("真实查询记录: %d 次调用 · 去重 %d 条" % (len(rows), len(uniq)))
    by_mode = Counter(r["mode"] for r in rows)
    for k, v in by_mode.most_common():
        print("  模式 %-10s %4d" % (k, v))
    rep = [(q, n) for q, n in cnt.most_common(12) if n > 1]
    if rep:
        print("重复问过（>1 次）—— 这些是「重要但上次没答好」的候选:")
        for q, n in rep:
            print("  %2d 次  %s" % (n, q[:56]))
    else:
        print("还没有重复的查询（新记录从 2026-11-04 起）")

    if show_filter:
        human, machine = machine_filter(rows)
        hq = len({r["q"] for r in human})
        print()
        print("机器流量筛：人类 %d 次（去重 %d）· 疑似机器 %d 次" %
              (len(human), hq, len(machine)))
        print("  典型机器记录:")
        for r, why in machine[:5]:
            print("    [%s] %s" % (why, r["q"][:44]))
    con.close()
    return 0

if __name__ == "__main__":
    sys.exit(main())
