#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
import paths

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent

from paths import DB, VEC

def main():
    R = {}
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row

    print("=" * 70)
    print("  1. 数据层")
    print("=" * 70)
    tabs = {}
    for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' "
                         "AND name NOT LIKE 'sqlite_%' ORDER BY name"):
        n = con.execute(f"SELECT COUNT(*) FROM {r[0]}").fetchone()[0]
        tabs[r[0]] = n
    R["tables"] = tabs
    for k, v in tabs.items():
        print(f"  {k:<22} {v:>7}")

    print()
    by_kind = {r[0]: r[1] for r in con.execute(
        "SELECT kind, COUNT(*) FROM content_block GROUP BY kind ORDER BY -COUNT(*)")}
    R["by_kind"] = by_kind
    print("  content_block 明细:")
    for k, v in by_kind.items():
        print(f"    {k:<18} {v:>6}")

    by_tier = {r[0]: r[1] for r in con.execute(
        "SELECT COALESCE(tier,'none'), COUNT(*) FROM lifecycle GROUP BY tier")}
    R["by_tier"] = by_tier
    print(f"\n  分级: {by_tier}")

    tot = tabs.get("item", 0)
    with_content = con.execute(
        "SELECT COUNT(DISTINCT item_uid) FROM content_block").fetchone()[0]
    with_derived = con.execute(
        "SELECT COUNT(DISTINCT item_uid) FROM derived WHERE kind='summary'").fetchone()[0]
    in_graph = con.execute(
        "SELECT COUNT(DISTINCT uid) FROM (SELECT src AS uid FROM edge "
        "UNION SELECT dst FROM edge)").fetchone()[0]
    R["coverage"] = {"items": tot, "with_content": with_content,
                     "with_derived": with_derived, "in_graph": in_graph}
    print(f"\n  覆盖率（共 {tot} 条）:")
    print(f"    有内容   {with_content:>4}  {with_content/tot*100:>5.1f}%")
    print(f"    有提炼   {with_derived:>4}  {with_derived/tot*100:>5.1f}%")
    print(f"    在图里   {in_graph:>4}  {in_graph/tot*100:>5.1f}%")

    print()
    print("=" * 70)
    print("  2. 检索索引")
    print("=" * 70)
    n_cb = tabs.get("content_block", 0)
    n_vec = tabs.get("vec_state", 0)
    try:
        n_fts = con.execute("SELECT COUNT(*) FROM block_fts").fetchone()[0]
    except Exception:
        n_fts = None
    R["index"] = {"content_block": n_cb, "vec_state": n_vec, "fts": n_fts}

    try:
        import rag_core as _rc
        _rows = con.execute("SELECT text FROM content_block").fetchall()
        n_indexable = sum(1 for r in _rows
                          if _rc.info_len(r["text"]) >= _rc.MIN_INFO_CHARS)
    except Exception:
        n_indexable = n_cb
    print(f"  content_block {n_cb}   其中可索引 {n_indexable}   向量 {n_vec}   FTS {n_fts}")
    ok = (n_indexable == n_vec == n_fts)
    print(f"  {'✓ 三者一致' if ok else '✗ 不一致'}")
    if not ok and n_indexable != n_cb:
        print(f"  （差额 {n_cb - n_indexable} 是无信息块，按设计不入索引）")
    if VEC.exists():
        print(f"  索引文件 {VEC.stat().st_size/1024/1024:.1f}MB")

    print()
    print("=" * 70)
    print("  3. 代码层")
    print("=" * 70)
    mods = sorted(p.name[:-3] for p in HERE.glob("*.py") if not p.name.startswith("_"))
    specials = {"bench2", "bench3", "calibrate", "calibrate_rerank", "test_core",
                "partitioned_search", "check_index", "seg_boundary",
                "batch_refine", "audit"}
    R["modules"] = mods
    lines = sum(len(p.read_text(encoding="utf-8", errors="replace").splitlines())
                for p in HERE.glob("*.py") if not p.name.startswith("_"))
    print(f"  模块 {len(mods)} 个，共 {lines} 行")
    print(f"  其中工具类: {sorted(m for m in mods if m in specials)}")

    print()
    print("=" * 70)
    print("  4. 功能可用性（端到端）")
    print("=" * 70)
    probes = [
        ("混合检索", ["python", "kb.py", "search", "Frida", "--top-k", "2"]),
        ("分区检索", ["python", "kb.py", "search", "Frida", "--partitioned", "--top-k", "2"]),
        ("统计", ["python", "kb.py", "stats"]),
        ("图谱", ["python", "kb.py", "graph", "--stats"]),
        ("一致性", ["python", "check_index.py"]),
    ]
    R["probes"] = {}
    for name, cmd in probes:
        try:
            p = subprocess.run(cmd, cwd=str(HERE), capture_output=True, timeout=120)
            out = (p.stdout or b"").decode("utf-8", "replace")
            good = p.returncode == 0 and "Traceback" not in out
            R["probes"][name] = "ok" if good else "fail"
            print(f"  [{'✓' if good else '✗'}] {name}")
            if not good:
                print(f"      {(p.stderr or b'').decode('utf-8','replace')[:120]}")
        except subprocess.TimeoutExpired:
            R["probes"][name] = "timeout"
            print(f"  [T] {name} 超时")

    con.close()
    print()
    print("=" * 70)
    print("  5. 依赖与配置")
    print("=" * 70)
    for d in ("usearch", "fastembed", "jieba", "numpy", "yaml"):
        try:
            m = __import__(d)
            print(f"  ✓ {d:<12} {getattr(m, '__version__', '?')}")
        except ImportError:
            print(f"  ✗ {d:<12} 缺失")
    try:
        import mcp
        print(f"  ✓ mcp          {getattr(mcp, '__version__', '?')}")
    except ImportError:
        print(f"  ✗ mcp          缺失（MCP server 不可用）")

    import os
    target = Path(os.environ.get("KB_CONTEXT_TARGET")
                  or (Path(__file__).parent / "kb-context.md")).expanduser()
    if target.exists():
        t = target.read_text(encoding="utf-8", errors="replace")
        has = "bili-kb-index" in t
        print(f"  {'✓' if has else '✗'} 提示词片段 {target.name}"
              f"（{len(t.encode('utf-8'))} 字节，索引段{'在' if has else '不在'}）")
    else:
        print(f"  - 提示词片段 未生成（{target.name}）")

    (HERE / "audit_result.json").write_text(
        json.dumps(R, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  结果存 audit_result.json")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
