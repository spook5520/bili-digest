#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sqlite3
import sys

from bili_digest import paths
from bili_digest.paths import DB, VEC

def _q(con, sql, args=()) -> object:
    try:
        return con.execute(sql, args).fetchone()[0]
    except sqlite3.OperationalError:
        return None

def snapshot(full: bool = False) -> dict:
    from bili_digest.quality import check_index as ci

    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    rows = {}

    try:
        r = ci.check(verbose=False)
        rows["索引一致性"] = (
            "OK" if r["consistent"] else "FAIL",
            "库 %d / 向量 %d / FTS %s" % (r["blocks"], r["vec"], r["fts"]),
            "" if r["consistent"] else
            "缺 %d · 孤儿 %d · 陈旧 %d" % (r["missing"], r["orphan"], r["stale"]),
        )
    except Exception as e:
        rows["索引一致性"] = ("FAIL", "-", "%s: %s" % (type(e).__name__, str(e)[:50]))

    try:
        n_stale = len(ci.check(verbose=False).get("stale") or [])
        rows["陈旧向量"] = ("OK" if not n_stale else "FAIL",
                        "%d 条文本已变未重编" % n_stale, "")
    except Exception:
        rows["陈旧向量"] = ("WARN", "未测", "check_index 不可用")

    try:
        base = _q(con, "SELECT v FROM meta_kv WHERE k='kw_block_total'")
        real = _q(con, "SELECT COUNT(*) FROM content_block WHERE kind IN "
                       "('subtitle','subtitle_seg','danmaku','comment','meta')")
        if base is None:
            rows["关键词基线"] = ("WARN", "未记录", "跑 python -m bili_digest.retrieve.keywords")
        else:
            gap = int(real) - int(base)
            rows["关键词基线"] = ("OK" if gap == 0 else "WARN",
                              "基线 %s / 实际 %d" % (base, real),
                              "" if gap == 0 else "差 %d 块，图已过期" % gap)
    except Exception as e:
        rows["关键词基线"] = ("WARN", "-", str(e)[:50])

    try:
        n = _q(con, """SELECT COUNT(*) FROM item i WHERE
                       EXISTS(SELECT 1 FROM content_block cb
                              WHERE cb.item_uid=i.uid AND cb.kind='subtitle')
                       AND NOT EXISTS(SELECT 1 FROM content_block cb
                              WHERE cb.item_uid=i.uid AND cb.kind='subtitle_seg')""")
        rows["切段完整性"] = ("OK" if not n else "WARN", "%d 条缺段落" % (n or 0), "")
    except Exception as e:
        rows["切段完整性"] = ("WARN", "-", str(e)[:50])

    try:
        n = _q(con, """SELECT COUNT(*) FROM item i JOIN lifecycle l ON l.item_uid=i.uid
                       WHERE l.tier='knowledge' AND NOT EXISTS(
                         SELECT 1 FROM content_block cb
                         WHERE cb.item_uid=i.uid AND cb.kind='summary')""")
        rows["提炼覆盖"] = ("OK" if not n else "WARN", "%d 条 knowledge 档缺提炼" % (n or 0), "")
    except Exception as e:
        rows["提炼覆盖"] = ("WARN", "-", str(e)[:50])

    try:
        items = _q(con, "SELECT COUNT(*) FROM item")
        blocks = _q(con, "SELECT COUNT(*) FROM content_block")
        with_sub = _q(con, """SELECT COUNT(DISTINCT item_uid) FROM content_block
                              WHERE kind IN ('subtitle','subtitle_seg')""")
        pct = 100.0 * (with_sub or 0) / max(items or 1, 1)
        rows["规模"] = ("OK", "%d 条目 / %d 块" % (items, blocks),
                     "有字幕 %.1f%%（%d 条）" % (pct, with_sub or 0))
    except Exception as e:
        rows["规模"] = ("WARN", "-", str(e)[:50])

    try:
        n = _q(con, "SELECT COUNT(*) FROM job_log WHERE status='fail'")
        rows["抓取失败"] = ("OK" if not n else "WARN", "%d 条 fail 记录" % (n or 0), "")
    except Exception as e:
        rows["抓取失败"] = ("WARN", "-", str(e)[:50])

    try:
        import os
        exists = os.path.exists(str(paths.LATEON_CACHE))
        rows["重排缓存"] = ("OK", "已建" if exists else "未建（可选）", "")
    except Exception:
        rows["重排缓存"] = ("WARN", "-", "")

    con.close()

    if full:
        rows.update(_full_checks())
    return rows

def _full_checks() -> dict:
    out = {}
    try:
        import importlib
        miss = [m for m in ("fastembed", "usearch", "jieba", "numpy")
                if importlib.util.find_spec(m) is None]
        out["核心依赖"] = ("OK" if not miss else "FAIL",
                        "齐全" if not miss else "缺 " + ", ".join(miss), "")
    except Exception as e:
        out["核心依赖"] = ("WARN", "-", str(e)[:50])
    try:
        from bili_digest.ingest import fetcher
        cred = fetcher.load_credential()
        out["B站登录态"] = ("OK" if cred.get("sessdata") else "WARN",
                        "有 SESSDATA" if cred.get("sessdata") else "缺",
                        "只要网络请求能过就够，不一定需要登录")
    except Exception as e:
        out["B站登录态"] = ("WARN", "未配置", str(e)[:40])
    return out

def render(rows: dict) -> list:
    lines = ["", "  ── 库健康 ─────────────────────────────", ""]
    order = ["索引一致性", "陈旧向量", "关键词基线", "切段完整性", "提炼覆盖",
             "规模", "抓取失败", "重排缓存", "核心依赖", "B站登录态"]
    for k in order:
        if k not in rows:
            continue
        st, val, note = rows[k]
        mark = {"OK": "✓", "WARN": "!", "FAIL": "✗"}.get(st, "?")
        lines.append("  [%s] %-10s %-28s %s" % (mark, k, val, note))
    bad = [k for k, (st, _, _) in rows.items() if st == "FAIL"]
    warn = [k for k, (st, _, _) in rows.items() if st == "WARN"]
    lines.append("")
    if bad:
        lines.append("  ✗ %d 项失败：%s" % (len(bad), "、".join(bad)))
    elif warn:
        lines.append("  ✓ 无失败 · %d 项需留意：%s" % (len(warn), "、".join(warn)))
    else:
        lines.append("  ✓ 全部正常")
    return lines

def main() -> int:
    import argparse
    paths.enable_utf8_stdout()
    ap = argparse.ArgumentParser(description="库健康总览：一致性 / 新鲜度 / 覆盖 / 失败记录")
    ap.add_argument("--full", action="store_true", help="加依赖自检与登录态（会出网）")
    a = ap.parse_args()
    rows = snapshot(full=a.full)
    for ln in render(rows):
        print(ln)
    bad = [k for k, (st, _, _) in rows.items() if st == "FAIL"]
    warn = [k for k, (st, _, _) in rows.items() if st == "WARN"]
    return 2 if bad else (1 if warn else 0)

if __name__ == "__main__":
    sys.exit(main())
