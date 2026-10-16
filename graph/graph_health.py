#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
import paths

import argparse
import sys
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).parent

def check(con, min_blocks: int = 1, verbose: bool = True) -> dict:
    dangling = con.execute("""
        SELECT COUNT(*) n FROM edge
        WHERE src NOT IN (SELECT uid FROM item)
           OR dst NOT IN (SELECT uid FROM item)""").fetchone()["n"]

    hollow = con.execute("""
        SELECT COUNT(*) n FROM edge e
        WHERE (SELECT COUNT(*) FROM content_block cb WHERE cb.item_uid=e.src) < ?
           OR (SELECT COUNT(*) FROM content_block cb WHERE cb.item_uid=e.dst) < ?""",
        (min_blocks, min_blocks)).fetchone()["n"]

    total = con.execute("SELECT COUNT(*) n FROM edge").fetchone()["n"]

    if verbose:
        print(f"  边总数 {total}")
        print(f"  悬空边（src/dst 不在 item 表）  {dangling}"
              f"  {'✓' if dangling == 0 else '✗'}")
        print(f"  空壳边（一侧内容 < {min_blocks} 块）    {hollow}"
              f"  {'✓' if hollow == 0 else '✗'}")
        if hollow:
            rows = con.execute("""
                SELECT e.kind, e.src, e.dst, e.note,
                       (SELECT COUNT(*) FROM content_block cb WHERE cb.item_uid=e.src) a,
                       (SELECT COUNT(*) FROM content_block cb WHERE cb.item_uid=e.dst) b
                FROM edge e
                WHERE (SELECT COUNT(*) FROM content_block cb WHERE cb.item_uid=e.src) < ?
                   OR (SELECT COUNT(*) FROM content_block cb WHERE cb.item_uid=e.dst) < ?
                LIMIT 5""", (min_blocks, min_blocks)).fetchall()
            print(f"    样例:")
            for r in rows:
                print(f"      [{r['kind']}] {r['src'].split(':')[-1]}({r['a']}块) ↔ "
                      f"{r['dst'].split(':')[-1]}({r['b']}块)")
    return {"total": total, "dangling": dangling, "hollow": hollow,
            "min_blocks": min_blocks}

def purge(con, min_blocks: int = 1, verbose: bool = True) -> dict:
    n_d = con.execute("""
        DELETE FROM edge
        WHERE src NOT IN (SELECT uid FROM item)
           OR dst NOT IN (SELECT uid FROM item)""").rowcount
    n_h = con.execute("""
        DELETE FROM edge e
        WHERE (SELECT COUNT(*) FROM content_block cb WHERE cb.item_uid=e.src) < ?
           OR (SELECT COUNT(*) FROM content_block cb WHERE cb.item_uid=e.dst) < ?""",
        (min_blocks, min_blocks)).rowcount
    con.commit()
    if verbose:
        print(f"  删除悬空边 {n_d} · 空壳边 {n_h}")
    return {"purged_dangling": n_d, "purged_hollow": n_h}

def main():
    ap = argparse.ArgumentParser(description="图谱失效检查与清理")
    ap.add_argument("--purge", action="store_true")
    ap.add_argument("--strict", action="store_true",
                    help="一侧内容 <3 块也算无效")
    ap.add_argument("--min-blocks", type=int, default=None)
    a = ap.parse_args()

    from core_schema import connect
    from paths import DB
    con = connect(DB)
    con.row_factory = __import__("sqlite3").Row

    mb = a.min_blocks if a.min_blocks is not None else (3 if a.strict else 1)
    print(f"═══ 图谱失效检查（min_blocks={mb}）═══")
    check(con, mb)
    if a.purge:
        print(f"\n═══ 清理 ═══")
        purge(con, mb)
        print(f"\n═══ 清理后 ═══")
        check(con, mb)
    con.close()

if __name__ == "__main__":
    main()
