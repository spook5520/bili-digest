#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os as _os
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
import paths

import sys
from pathlib import Path

from core_schema import connect
from paths import DB

def readings(con) -> dict:
    q = con.execute
    n = q("SELECT COUNT(*) FROM item").fetchone()[0]
    mat = q("""SELECT COUNT(*) FROM item i WHERE EXISTS(SELECT 1 FROM content_block cb
        WHERE cb.item_uid=i.uid AND cb.kind IN
        ('subtitle','subtitle_seg','danmaku','comment'))""").fetchone()[0]
    dis = q("SELECT COUNT(DISTINCT item_uid) FROM derived "
            "WHERE kind='summary'").fetchone()[0]

    def cnt(tbl, where="1=1"):
        try:
            return q(f"SELECT COUNT(*) FROM {tbl} WHERE {where}").fetchone()[0]
        except Exception:
            return None

    return {
        "条目": n,
        "有素材": mat,
        "已提炼": dis,
        "无任何内容": n - mat,
        "ann+": cnt("content_block", "kind='annotation'"),
        "pick": cnt("pick_log"),
        "查询日志": cnt("query_log"),
        "抓取失败": cnt("job_log", "status='fail'"),
    }

MATRIX = [
    ("capture", "信息", "内容块为 0 的条目占多数 —— 采集未覆盖，而那是知识库的前提。"
                        "job_log 里的 fail 全是 HTTP 412（风控）"),
    ("capture", "行为", "收藏（用户的动作）与抓取（需另行触发）是两条线，中间没有自动衔接"),
    ("capture", "技术", "串轨有跨度校验；抓取上限以声明值兜底"),
    ("organize", "技术", "四粒度（block/seg/item/topic）；派生物与原文统一进 content_block；"
                         "索引与检查同一口径"),
    ("retrieve", "信息", "语义贴近度可报，但「有内容」与「无内容」两组【重叠】—— "
                         "无法可靠判断库里有没有"),
    ("retrieve", "认知", "pick 未达 5 条门槛，排序无法据真实意图调（门槛值有出处：Stanford IR 教材）"),
    ("apply", "行为", "annotate 能存、进 FTS、检索时提示（机制完整）；使用量低是用户侧的事，"
                      "不是缺陷"),
    ("apply", "认知", "库只被「读」用于回答 —— 没有把新结论写回并再提炼的闭环"),
]

def main() -> int:
    con = connect(DB)
    con.row_factory = __import__("sqlite3").Row
    r = readings(con)
    print("  读数：" + " · ".join(f"{k} {v}" for k, v in r.items()))
    print()
    print("  过程        维度      状况")
    for proc, dim, text in MATRIX:
        print(f"  {proc:<10} {dim:<8} {text}")
    print()
    print("  缺口：capture × 信息（多数条目无内容，卡在风控节奏）")
    print("        apply × 认知（没有「回答之后写回再提炼」的闭环）")
    con.close()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
