# -*- coding: utf-8 -*-

import os as _os
import sys as _sys
import time
from pathlib import Path

from bili_digest import paths

BUDGETS = [
    ("建索引（增量，无新块）", 3000, "只做新鲜度检查与跳过"),
    ("建索引（增量，有少量新块）", 20000, "几百块的编码"),
    ("建索引（全量重建）", 1500000, "7~13 万块，实测 1190~1500 秒"),
    ("检索一次（CLI，含冷启动）", 4000, "进程启动 + 模型加载占大头"),
    ("检索一次（MCP 常驻）", 100, "模型已在内存"),
    ("模型加载（embedding）", 2000, "首次 import + 权重载入，实测 1252ms"),
    ("jieba 分词器加载", 1500, "首次加载词典"),
    ("单条提炼（LLM）", 40000, "取决于素材长度"),
    ("单条抓取（含节流）", 20000, "风控退避是主要成本"),
    ("结构重建（边 + 社区）", 5000, "实测 1.6~2.1 秒"),
    ("一致性检查 check_index", 30000, "全表比对"),
]

def measure(label: str, fn, repeat: int = 1) -> dict:
    t0 = time.perf_counter()
    for _ in range(max(1, repeat)):
        fn()
    dt_ms = (time.perf_counter() - t0) * 1000 / max(1, repeat)
    budget = next((b for n, b, _ in BUDGETS if n == label), None)
    ratio = (dt_ms / budget) if budget else None
    return {'label': label, 'ms': round(dt_ms, 1), 'budget': budget,
            'ratio': round(ratio, 2) if ratio else None}

def report(rows: list = None) -> str:
    if not rows:
        rows = []
    out = ["%-30s %12s %12s %8s" % ("操作", "实测(ms)", "预算(ms)", "倍率")]
    out.append("-" * 66)
    for r in rows:
        flag = ""
        if r.get("ratio"):
            flag = "  ← 超出" if r["ratio"] > 2.0 else ""
        out.append("%-30s %12s %12s %8s%s" % (
            r["label"], r["ms"], r.get("budget") or "-",
            r.get("ratio") or "-", flag))
    return chr(10).join(out)

def main() -> int:
    import argparse
    from bili_digest import paths as _p
    _p.enable_utf8_stdout()
    ap = argparse.ArgumentParser(description="延迟预算表")
    ap.add_argument("--list", action="store_true", help="只列预算，不测")
    ap.add_argument("--run", action="store_true", help="实测几项关键路径")
    a = ap.parse_args()

    if a.list or not a.run:
        print("  %-32s %12s  %s" % ("操作", "预算(ms)", "说明"))
        print("  " + "-" * 74)
        for name, b, note in BUDGETS:
            print("  %-32s %12d  %s" % (name, b, note))
        print()
        print("  用法：--run 实测几项（会加载模型，约 10 秒）")
        return

    rows = []
    from bili_digest.retrieve import rag_core as rc
    from bili_digest.schema.core_schema import connect
    from bili_digest.paths import DB, VEC

    def _load_model():
        rc.deps()
    rows.append(measure("模型加载（embedding）", _load_model))

    con = connect(DB)
    def _health():
        con.execute("SELECT COUNT(*) FROM content_block").fetchone()
    rows.append(measure("一致性检查 check_index", _health))
    con.close()

    from bili_digest.graph import refresh
    def _fresh():
        con2 = connect(DB)
        refresh.staleness(con2)
        con2.close()
    rows.append(measure("结构重建（边 + 社区）", _fresh))

    print()
    print(report(rows))

if __name__ == "__main__":
    main()
