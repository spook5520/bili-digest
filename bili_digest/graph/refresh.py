# -*- coding: utf-8 -*-

import os as _os
import subprocess
import sys
import time
from pathlib import Path

from bili_digest import paths

_ROOT = str(paths.HERE)

from bili_digest.schema.core_schema import connect
from bili_digest.paths import DB

MIN_NEW = 20
DERIVED_KINDS = ("summary", "key_points", "insights", "topics")

def staleness(con) -> dict:
    row = con.execute("SELECT COALESCE(MAX(updated_at),0) FROM edge").fetchone()
    edge_t = row[0] if row else 0
    ph = ",".join("?" * len(DERIVED_KINDS))
    row = con.execute(
        "SELECT COALESCE(MAX(created_at),0), COUNT(*) FROM content_block "
        "WHERE kind IN (%s)" % ph, DERIVED_KINDS).fetchone()
    content_t, n_derived = (row[0], row[1]) if row else (0, 0)
    return {
        "edge_t": edge_t,
        "edge_age_h": round((time.time() - edge_t) / 3600, 1) if edge_t else 0.0,
        "n_derived": n_derived,
        "stale": bool(content_t > edge_t and n_derived >= MIN_NEW),
    }

KW_KINDS = ("subtitle", "subtitle_seg", "danmaku", "comment", "meta")
KW_MIN_NEW = 50

def keyword_staleness(con) -> dict:

    try:
        stored = con.execute(
            "SELECT COUNT(DISTINCT block_id) FROM keyword_block").fetchone()[0]
    except Exception:
        stored = None
    ph = ",".join("?" * len(KW_KINDS))
    expect = con.execute(
        "SELECT COUNT(*) FROM content_block WHERE kind IN (%s)" % ph,
        KW_KINDS).fetchone()[0]

    try:
        _prev = con.execute(
            "SELECT v FROM meta_kv WHERE k='kw_block_total'").fetchone()
        prev = int(_prev[0]) if _prev and _prev[0] else None
    except Exception:
        prev = None

    missing = (expect - prev) if prev is not None else 0

    return {
        "kw_stored": prev if prev is not None else stored,
        "kw_expect": expect,
        "kw_missing": missing,
        "kw_stale": bool(prev is None and stored is None) or missing >= KW_MIN_NEW,
    }

def _run(cmd: list, timeout: int = 600) -> int:
    kw = {}
    if sys.platform == "win32":
        kw["encoding"] = "utf-8"
        kw["errors"] = "replace"
    r = subprocess.run(cmd, cwd=str(_ROOT), capture_output=True,
                       text=True, timeout=timeout, **kw)
    return r.returncode

def refresh(con=None, verbose: bool = True, force: bool = False) -> dict:
    own = con is None
    if own:
        con = connect(DB)
    try:
        st = staleness(con)
        kst = keyword_staleness(con)

        kw_rebuilt = False
        if force or kst["kw_stale"]:
            missing_before = kst["kw_missing"]
            root0 = Path(_ROOT)
            rc_kw = _run([sys.executable, "-m", "bili_digest.retrieve.keywords",
                          "--build"], timeout=900)
            kw_rebuilt = rc_kw == 0
            kst = keyword_staleness(con)
            if verbose:
                print("  keyword 表重建：%s（差 %d 块 → 现差 %d，rc=%d）"
                      % ("成功" if kw_rebuilt else "失败",
                         missing_before, kst["kw_missing"], rc_kw))

        if not (force or st["stale"]):
            if verbose:
                print("  结构新鲜，跳过（边 %.1f 小时前 · 派生块 %d）"
                      % (st["edge_age_h"], st["n_derived"]))
            return {"rebuilt": False, "reason": "fresh", "keyword_rebuilt": kw_rebuilt,
                    **st, **kst}
        t0 = time.time()

        root = Path(_ROOT)
        rc1 = _run([sys.executable, "-m", "bili_digest.graph.graph", "--build"])
        rc2 = _run([sys.executable, "-m", "bili_digest.retrieve.community", "--run"])
        n_edge = con.execute("SELECT COUNT(*) FROM edge").fetchone()[0]
        n_comm = con.execute("SELECT COUNT(DISTINCT comm_id) FROM community").fetchone()[0]
        out = {
            "rebuilt": True,
            "reason": "force" if force else "stale",
            "edges": n_edge,
            "communities": n_comm,
            "rc": [rc1, rc2],
            "keyword_rebuilt": kw_rebuilt,
            "seconds": round(time.time() - t0, 1),
            **kst,
        }
        if verbose:
            print("  重建结构：边 %d 条 · 社区 %d 个 · %.1fs（rc=%s）"
                  % (n_edge, n_comm, out["seconds"], out["rc"]))
        return out
    finally:
        if own:
            con.close()

def main() -> int:
    import argparse
    from bili_digest import paths as _p
    _p.enable_utf8_stdout()
    ap = argparse.ArgumentParser(description="结构新鲜度：需要时重建边与社区")
    ap.add_argument("--status", action="store_true", help="只看现状")
    ap.add_argument("--run", action="store_true", help="需要就重建")
    ap.add_argument("--force", action="store_true", help="无条件重建")
    a = ap.parse_args()
    con = connect(DB)
    try:
        if a.force:
            refresh(con, verbose=True, force=True)
            return
        st = staleness(con)
        if a.run:
            refresh(con, verbose=True)
            return
        kst = keyword_staleness(con)
        print("  边最后重建:  %.1f 小时前" % st["edge_age_h"])
        print("  派生内容块:  %d（阈值 %d）" % (st["n_derived"], MIN_NEW))
        print("  结构需重建:  %s" % ("是" if st["stale"] else "否"))
        print("  keyword 覆盖: 记录 %s / 当前 %d（差 %d，阈值 %d）"
              % (kst["kw_stored"], kst["kw_expect"], kst["kw_missing"], KW_MIN_NEW))
        print("  keyword 需重建: %s" % ("是" if kst["kw_stale"] else "否"))
    finally:
        con.close()

if __name__ == "__main__":
    main()
