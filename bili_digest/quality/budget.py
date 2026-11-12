#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys
import time

from bili_digest import paths
from bili_digest.paths import DB
from bili_digest.schema.core_schema import now_sec

PREFIX = "budget:"

DIMENSIONS = [
    ("fetch_items", "自主抓取条数", 40,
     "job_log 里 step 含 fetch 的行数"),
    ("write_rows", "自主写入行数", 20000,
     "job_log.rows 求和（块 / 条目 / 边）"),
    ("llm_calls", "自主 LLM 调用", 60,
     "job_log 里 step 以 refine / derive 开头的行数"),
    ("rebuilds", "全库重建次数", 4,
     "step ∈ index / seg_boundary / lateon / keywords"),
    ("concurrent", "并发自主进程", 2,
     "status='running' 且未过期的租约数"),

    ("inbox_replies", "对外回复次数", 20,
     "step='inbox_reply' 且 status='ok' 的行数"),
]

REBUILD_STEPS = ("index", "seg_boundary", "lateon", "keywords",
                 "reindex", "reindex-stale")
LEASE_TTL = 1800

class BudgetStop(RuntimeError):

    def __init__(self, dim: str, used: float, limit: float, need: float,
                 hint: str = ""):
        self.dim, self.used, self.limit, self.need = dim, used, limit, need
        name = dict((k, n) for k, n, _d, _m in DIMENSIONS)[dim]
        super().__init__(
            f"{name} 已用 {used:g} / 上限 {limit:g}，本次还需要 {need:g}" +
            (f"\n      {hint}" if hint else ""))

def limits(con) -> dict:
    out = {k: d for k, _n, d, _m in DIMENSIONS}
    try:
        for k, v in con.execute(
                "SELECT k, v FROM meta_kv WHERE k LIKE ?", (PREFIX + "%",)):
            key = k[len(PREFIX):]
            if key in out:
                try:
                    out[key] = float(v)
                except (TypeError, ValueError):
                    pass
    except Exception:
        pass
    return out

def set_limit(con, key: str, value: float) -> None:
    if key not in dict((k, k) for k, _n, _d, _m in DIMENSIONS):
        raise KeyError(f"未知预算维度 {key}；可选 "
                       f"{[k for k, _n, _d, _m in DIMENSIONS]}")
    con.execute("INSERT OR REPLACE INTO meta_kv(k,v) VALUES(?,?)",
                (PREFIX + key, str(value)))
    con.commit()

def reset(con) -> int:
    cur = con.execute("DELETE FROM meta_kv WHERE k LIKE ?", (PREFIX + "%",))
    con.commit()
    return cur.rowcount

def day_start(ts: float = None) -> int:
    t = time.localtime(ts or time.time())
    return int(time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1)))

def usage(con, since: int = None) -> dict:
    since = since or day_start()
    u = dict((k, 0.0) for k, _n, _d, _m in DIMENSIONS)
    try:
        row = con.execute("""
            SELECT
              -- 「抓取」按动作算：refine 是「抓取+提炼」一体，也计入抓取条数；
              -- 只认 '%fetch%' 会漏掉 42 条成功的 refine（step 名不统一造成的口径洞）
              SUM(CASE WHEN (step LIKE '%fetch%' OR step IN ('refine','import'))
                        AND status IN ('ok','fail','skip')
                       THEN 1 ELSE 0 END),
              SUM(COALESCE(rows, 0)),
              SUM(CASE WHEN step LIKE 'refine%' OR step LIKE 'derive%'
                       THEN 1 ELSE 0 END),
              SUM(CASE WHEN step IN ('index','seg_boundary','lateon','keywords')
                       THEN 1 ELSE 0 END),
              -- 对外回复只数【真的发出去了】的：dry 与 failed 都不占额，
              -- 否则「先演练看一眼」会吃掉额度，人就不敢先看
              SUM(CASE WHEN step='inbox_reply' AND status='ok'
                       THEN 1 ELSE 0 END)
            FROM job_log WHERE ts >= ?""", (since,)).fetchone()
        for i, k in enumerate(("fetch_items", "write_rows", "llm_calls",
                               "rebuilds", "inbox_replies")):
            u[k] = float((row[i] if row else 0) or 0)

        import re
        for (detail,) in con.execute(
                "SELECT detail FROM job_log WHERE ts >= ? AND rows IS NULL "
                "AND detail LIKE '%blocks=%'", (since,)):
            m = re.search(r"blocks=(\d+)", detail or "")
            if m:
                u["write_rows"] += float(m.group(1))
        u["concurrent"] = float(active_leases(con))
    except Exception:
        pass
    return u

def active_leases(con) -> int:
    try:
        return con.execute(
            "SELECT COUNT(*) FROM job_log WHERE status='running' AND ts >= ?",
            (now_sec() - LEASE_TTL,)).fetchone()[0]
    except Exception:
        return 0

def check(con, *, actor: str, step: str, rows: int = 0,
          llm: int = 0, rebuild: int = 0, concurrent: int = 1,
          inbox: int = 0, allow_over: bool = False) -> dict:
    lim = limits(con)
    u = usage(con)
    need = {"fetch_items": 0, "write_rows": rows, "llm_calls": llm,
            "rebuilds": rebuild, "concurrent": concurrent,
            "inbox_replies": inbox}
    if step and "fetch" in step:
        need["fetch_items"] = 1
    for k, n in need.items():
        if n and u.get(k, 0) + n > lim.get(k, 0):

            if allow_over:

                record(con, actor=actor, step=step, status="overridden",
                       detail=f"{k} {u[k]:g}+{n:g} > {lim[k]:g}（人已放行）",
                       rows=0, reversible=True)
                continue
            record(con, actor=actor, step=step, status="blocked",
                   detail=f"{k} {u[k]:g}+{n:g} > {lim[k]:g}", rows=0,
                   reversible=True)
            raise BudgetStop(
                k, u[k], lim[k], n,
                hint=f"放行本次：--yes；提高上限：kb budget --set {k}=<新值>；"
                     f"看账本：kb budget")
    return {k: (u.get(k, 0), lim.get(k, 0)) for k in need}

def record(con, *, actor: str, step: str, status: str = "ok",
           rows: int = None, seconds: float = None,
           reversible: bool = True, detail: str = None,
           scope: str = "*") -> None:
    from bili_digest.schema.core_schema import log_job
    try:
        log_job(con, scope, step, status, detail, actor=actor,
                rows=rows, seconds=seconds, reversible=reversible)
        con.commit()
    except Exception:
        pass

def acquire(con, actor: str, step: str, scope: str = "*") -> int:
    check(con, actor=actor, step=step, concurrent=1)
    from bili_digest.schema.core_schema import log_job
    log_job(con, scope, step, "running", "lease", actor=actor)
    con.commit()
    return con.execute("SELECT MAX(id) FROM job_log").fetchone()[0]

def release(con, lease_id: int, status: str = "ok", detail: str = None,
            rows: int = None, seconds: float = None) -> None:
    try:
        con.execute("UPDATE job_log SET status=?, detail=COALESCE(?,detail), "
                    "rows=COALESCE(?,rows), seconds=COALESCE(?,seconds) WHERE id=?",
                    (status, detail, rows, seconds, lease_id))
        con.commit()
    except Exception:
        pass

def render(con, days: int = 1, limit: int = 12) -> list:
    since = day_start() - (days - 1) * 86400
    lim = limits(con)
    u = usage(con, since)
    label = "今天" if days == 1 else f"近 {days} 天"
    L = ["", f"  ── {label}系统自主做了什么（账本：job_log）──", ""]
    L.append("    %-14s %10s %10s   %s" % ("维度", "已用", "上限", "计量"))
    for k, name, _d, how in DIMENSIONS:
        used = u.get(k, 0)
        cap = lim.get(k, 0)
        flag = "  ← 超" if cap and used > cap else ""
        L.append("    %-14s %10g %10g   %s%s" % (name, used, cap, how, flag))

    rows = con.execute("""
        SELECT COALESCE(actor,'(未标注)') a, step, status, COUNT(*) n,
               SUM(COALESCE(rows,0)) r, SUM(COALESCE(seconds,0)) s
        FROM job_log WHERE ts >= ? GROUP BY a, step, status
        ORDER BY n DESC LIMIT ?""", (since, limit)).fetchall()
    if rows:
        L.append("")
        L.append("    %-12s %-18s %-8s %6s %9s %8s" % (
            "谁", "动作", "状态", "次数", "改行数", "耗时s"))
        for a, step, st, n, r, s in rows:

            L.append("    %-12s %-18s %-8s %6d %9s %8s" % (
                a, step, st, n, ("%d" % r) if r else "-",
                ("%.1f" % s) if s else "-"))
    else:
        L.append("")
        L.append("    （这段时间没有自主行动记录）")

    n_null = con.execute(
        "SELECT COUNT(*) FROM job_log WHERE ts >= ? AND actor IS NULL",
        (since,)).fetchone()[0]
    if n_null:
        L.append("")
        L.append("    [!] %d 行的 actor 为空：这些是接线（2026-11-01）之前记的，" % n_null)
        L.append("        分不清是人让它干的还是它自己干的。改行数已从 detail 的")
        L.append("        blocks=N 回填，actor 维度从接线之后才开始准。")
    L.append("")
    return L

def main() -> int:
    import argparse
    paths.enable_utf8_stdout()
    ap = argparse.ArgumentParser(description="自主动作的预算与账本")
    ap.add_argument("--days", type=int, default=1, help="看近几天（默认今天）")
    ap.add_argument("--set", metavar="KEY=VAL", help="改上限（写入 meta_kv，可 --reset 改回）")
    ap.add_argument("--reset", action="store_true", help="预算恢复默认（账本不动）")
    ap.add_argument("--limit", type=int, default=12, help="明细列几行")
    a = ap.parse_args()

    from bili_digest.schema.core_schema import connect
    con = connect(DB)
    try:
        if a.reset:
            print("  已删除 %d 条预算覆盖，恢复默认" % reset(con))
        if a.set:
            k, _, v = a.set.partition("=")
            set_limit(con, k.strip(), float(v))
            print("  %s = %g（存 meta_kv；--reset 可改回）" % (k.strip(), float(v)))
        for ln in render(con, days=a.days, limit=a.limit):
            print(ln)
    finally:
        con.close()
    return 0

if __name__ == "__main__":
    sys.exit(main())
