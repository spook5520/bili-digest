# -*- coding: utf-8 -*-

import os as _os
import sys as _sys

from bili_digest import paths

NS = {
    "一次向量点积(512维)": 200,
    "一次余弦排序(1万候选)": 2000000,

    "矩阵乘矩阵(512x512)x(512x512)": 60000000,
    "向量乘矩阵(1x512)x(512x512)": 120000,
    "SQLite 单行查询": 10000,
    "SQLite 全表扫描(13万行)": 3600000,
    "读一个 512 维向量": 300,

    "写回索引一条": 274000,
    "一次嵌入编码": 5000000,
    "一次 B站 API": 200000000,
    "一次 LLM 调用": 20000000000,
}

def estimate(plan):
    total = 0.0
    rows = []
    for item in plan:
        if len(item) == 2:
            name, count = item
            unit = NS.get(name)
            if unit is None:
                rows.append((name, count, None, None))
                continue
        else:
            name, unit, count = item
        ms = unit * count / 1e6
        total += ms
        rows.append((name, count, unit, ms))
    return {"total_ms": round(total, 1), "breakdown": rows}

def render(r):
    out = ["%-28s %12s %14s %12s" % ("操作", "次数", "单次(ns)", "小计(ms)")]
    out.append("-" * 70)
    for name, count, unit, ms in r["breakdown"]:
        if unit is None:
            out.append("%-28s %12s %14s %12s" % (name, count, "(未知)", "-"))
        else:
            out.append("%-28s %12d %14d %12.1f" % (name, count, unit, ms))
    out.append("-" * 70)
    out.append("%-28s %12s %14s %12.1f" % ("合计", "", "", r["total_ms"]))
    return chr(10).join(out)

def main() -> int:
    import argparse
    from bili_digest import paths as _p
    _p.enable_utf8_stdout()
    ap = argparse.ArgumentParser(description="信封背面计算")
    ap.add_argument("--numbers", action="store_true", help="列出操作常数")
    ap.add_argument("--whitening", action="store_true", help="估算白化方案")
    ap.add_argument("--multirecall", action="store_true", help="估算多路召回方案")
    a = ap.parse_args()

    if not (a.whitening or a.multirecall):
        print("  本项目的操作常数（单位纳秒）")
        print("  " + "-" * 58)
        for k, v in NS.items():
            print("  %-30s %14d  ns  (~%.1f ms)" % (k, v, v / 1e6))
        print()
        print("  用法：--whitening / --multirecall 看具体方案的估算")
        return

    if a.whitening:
        N = 134476
        print("  方案：全部向量做白化（x 减均值后乘 Σ^(-1/2)），查询时同变换")
        print()
        print("  【离线：算白化矩阵 + 变换全部向量 + 写回索引】")
        print(render(estimate([
            ("矩阵乘矩阵(512x512)x(512x512)", 1),
            ("读一个 512 维向量", N),
            ("一次向量点积(512维)", N * 2),
            ("写回索引一条", N),
        ])))
        print()
        print("  【在线：每次查询】")
        print(render(estimate([("向量乘矩阵(1x512)x(512x512)", 1)])))
        print()
        print("  判断：离线一次性约 37 秒（写回索引是大头）· 在线 0.1ms。")
        print("        → 代价上完全可做。剩下的是【有没有用】，那只能实测。")

    if a.multirecall:
        print("  方案：低命中时多路召回（换参数再取一次，求并集）")
        print()
        print(render(estimate([
            ("一次向量点积(512维)", 3 * 128),
            ("SQLite 单行查询", 3 * 128),
        ])))
        print()
        print("  判断：时间代价毫秒级，但【实测 R@10 提升为 0】——")
        print("        并集扩的是候选池不是排序，新结果进不了 top-10。")
        print("        → 代价不是问题，是【方法本身不对】。")

if __name__ == "__main__":
    main()
