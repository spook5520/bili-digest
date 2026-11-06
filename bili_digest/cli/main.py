#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import json
import sqlite3
import sys
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

HERE = Path(__file__).parent

from bili_digest import paths

from bili_digest.schema import core_schema as cs
from bili_digest.quality import lifecycle as lc
from bili_digest.retrieve import rag_core as rc
from bili_digest.paths import source_key, bili_uid
from bili_digest import paths as cs_paths
from bili_digest.schema.core_schema import (BLOCK_KINDS, TIER_CACHE, TIER_KNOWLEDGE,
                         TIER_RAW, connect)

DEFAULT_DB = str(cs_paths.DB)
DEFAULT_VEC = str(cs_paths.VEC)

from bili_digest.cli.helpers import db_of, _as_uid, _log_search, _auto_index
from bili_digest.cli.commands.search import cmd_search
from bili_digest.cli.commands.fetch import cmd_fetch, cmd_import, cmd_discover, cmd_refine
from bili_digest.cli.commands.distill import cmd_derive, cmd_synth
from bili_digest.cli.commands.inspect import cmd_graph, cmd_health, cmd_stats, cmd_expand, cmd_browse, cmd_weekly, cmd_quiz, cmd_rot, cmd_highlights, cmd_popularity, cmd_trends
from bili_digest.cli.commands.admin import cmd_why, cmd_harvest, cmd_bases, cmd_selfheal, cmd_moc, cmd_init, cmd_block, cmd_retier, cmd_question, cmd_mark, cmd_pick, cmd_note, cmd_index, cmd_adapter, cmd_blocksuggest, cmd_lateon, cmd_weights, cmd_annotate
from bili_digest.cli.commands.asr import cmd_asr
from bili_digest.cli.commands.budget import cmd_budget
from bili_digest.cli.commands.status import cmd_status
from bili_digest.cli.commands.gate import cmd_gate
from bili_digest.cli.commands.gaps import cmd_gaps
from bili_digest.cli.commands.clues import cmd_clues

class _Parser(argparse.ArgumentParser):

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(1, "%s: error: %s\n" % (self.prog, message))

def main() -> int:
    ap = _Parser(
        prog="kb", description="B站知识库（通用 schema，可扩展多平台）",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--vec", default=DEFAULT_VEC)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="建库").set_defaults(fn=cmd_init)

    p = sub.add_parser("question", help="问题池：我长期想回答的问题（费曼的「12 个最爱问题」）")
    p.add_argument("--add", metavar="问题", help="登记一个长期问题 —— 之后抓来的内容会自动对照它")
    p.add_argument("--from-log", action="store_true",
                   help="从查询日志里找「反复在问的问题」，建议登记进问题池")
    p.add_argument("--min-times", type=int, default=3,
                   help="--from-log 的出现次数阈值（默认 3）")
    p.add_argument("--resolve", type=int, help="标记某问题已解决")
    p.add_argument("--drop", type=int, help="放弃某问题")
    p.add_argument("-v", "--verbose", action="store_true", help="显示「为什么答不了」")
    p.set_defaults(fn=cmd_question)

    p = sub.add_parser("pick", help="记一次「采纳」—— 检索后哪条真的有用（唯一的真实反馈）")
    p.add_argument("rank", nargs="?", type=int, help="第几条（对最近一次 search）")
    p.add_argument("--uid", help="直接指定条目，跳过排名")
    p.add_argument("--note", help="为什么有用（可选）")
    p.add_argument("--list", action="store_true", help="看已采纳记录与排名分布")
    p.set_defaults(fn=cmd_pick)

    p = sub.add_parser("mark", help="给条目打/去「重要」标记（影响评分 0.20 的维度）")
    p.add_argument("uid", help="条目 id，如 BVxxxxxxxxxx 或 bilibili:BVxxxxxxxxxx")
    p.add_argument("--name", default="important", help="标记名（important/manual/star）")
    p.add_argument("--off", action="store_true", help="取消标记")
    p.set_defaults(fn=cmd_mark)

    p = sub.add_parser("fetch", help="采集条目（联网）")

    p.add_argument("ids", nargs="*",
                   help="BV 号或 bilibili:BVxxx 两种写法都行")
    p.add_argument("--platform", default="bilibili")
    p.add_argument("--meta-only", action="store_true", help="只取元数据不抓内容")
    p.add_argument("--queue", action="store_true",
                   help="只看抓取队列（不抓）：前 N 条 + 为什么是它们 + 预估耗时")
    p.add_argument("--order", default="empty",
                   choices=["saved", "hot", "short", "empty", "value"],
                   help="队列顺序（默认 empty=补空壳）")
    p.add_argument("--limit", type=int, default=10, help="--queue 列几条（默认 10）")
    p.set_defaults(fn=cmd_fetch)

    p = sub.add_parser("import", help="导入已抓取的 raw.sqlite")
    p.add_argument("--digest", required=True, help="bili-digest-out 目录或某个视频目录")
    p.set_defaults(fn=cmd_import)

    p = sub.add_parser("block", help="屏蔽库")
    p.add_argument("action", choices=["add", "rm", "list"])
    p.add_argument("--kind", choices=BLOCK_KINDS)
    p.add_argument("--value")
    p.add_argument("--label", help="人类可读名称，如 UP 名")
    p.add_argument("--reason")
    p.set_defaults(fn=cmd_block)

    from bili_digest.distill import block_plus as _bp
    p = sub.add_parser("blocksuggest", help="屏蔽增强：推断/拉黑/采纳")
    _bp.add_cli_args(p, with_db=False)
    p.set_defaults(fn=cmd_blocksuggest)

    from bili_digest.ingest import discover as _disc
    p = sub.add_parser("discover", help="搜索视频并采纳进知识库")
    _disc.add_cli_args(p, with_db=False)
    p.set_defaults(fn=cmd_discover)

    p = sub.add_parser("refine", help="提炼流水线：cache → knowledge")
    from bili_digest.ingest import refine as _rf
    _rf.add_cli_args(p)
    p.set_defaults(fn=cmd_refine)

    p = sub.add_parser("derive", help="LLM 提炼：摘要/要点/观众视角/主题/洞察/五维打分")
    p.add_argument("--uid", help="单个条目")
    p.add_argument("--tier", help="按分级批量，如 knowledge / cache")
    p.add_argument("--limit", type=int, default=5)
    p.add_argument("--force", action="store_true", help="已存在也重跑")
    p.add_argument("--show", metavar="UID", help="查看提炼结果")
    p.add_argument("--incremental", action="store_true",
                   help="增量提炼：素材增长后（弹幕评论变多）只提新增部分，"
                        "不覆盖首次成果")
    p.add_argument("--baseline", action="store_true",
                   help="给已有提炼的条目建素材指纹基线（不调 LLM）")
    p.set_defaults(fn=cmd_derive)

    from bili_digest.distill import synthesize as _syn
    p = sub.add_parser("synth", help="反刍合成：综述/矛盾点/待验证问题/行动项")
    _syn.add_cli_args(p, with_db=False)
    p.set_defaults(fn=cmd_synth)

    from bili_digest.quality import source_weight as _sw
    p = sub.add_parser("highlights",
                       help="高能时刻：用弹幕密度找「哪段值得看」，并判别是叫好还是争论")
    p.add_argument("uid", help="条目 uid 或 BV 号")
    p.add_argument("--top", type=int, default=5)
    p.add_argument("--bucket", type=int, default=60,
                   help="分桶秒数（长视频 60、短视频 10）")
    p.set_defaults(fn=cmd_highlights)

    p = sub.add_parser("popularity",
                       help="热度：用库里的播放/点赞/弹幕数排序（这些字段一直在，只是没过）")
    p.add_argument("uids", nargs="*", help="条目 uid 或 BV 号；不给则排全库")
    p.add_argument("--by", default="play", choices=["play", "like", "reply", "danmaku"])
    p.add_argument("--top", type=int, default=20, help="不给 uid 时取前多少条")
    p.set_defaults(fn=cmd_popularity)

    p = sub.add_parser("expand", help="展开一个块的上下文（粗索引→细证据）")
    p.add_argument("block_id", type=int, help="content_block.id（见 search --show-id）")
    p.add_argument("--n", type=int, default=2, help="前后各取几块（默认 2）")
    p.add_argument("--claim", action="store_true", help="不展开上下文，改为列出这条论断引用了哪些证据块（走 cite_edge，不走正文里的时间戳）")
    p.set_defaults(fn=cmd_expand)
    p = sub.add_parser("browse", help="按主题浏览：匹配「收藏」的组织轴，而非提问")
    p.add_argument("--topic", default=None, help="主题名（可模糊）")
    p.add_argument("--limit", type=int, default=40, help="列几条（默认 40）")
    p.set_defaults(fn=cmd_browse)
    p = sub.add_parser("weekly", help="每周简报：从没被查过的 / 空壳 / 这周做什么")
    p.add_argument("--top", type=int, default=8, help="每节列几条（默认 8）")
    p.set_defaults(fn=cmd_weekly)
    p = sub.add_parser("quiz", help="从库里出题：主动回忆，治「看了就忘」")
    p.add_argument("--n", type=int, default=5, help="出几题（默认 5）")
    p.add_argument("--seed", type=int, default=None, help="随机种子（复现同一批题）")
    p.add_argument("--recent", type=int, default=None, help="只从近 N 天的条目出题")

    p.add_argument("--due", action="store_true", help="只出到期的题（按 FSRS due 升序）")
    p.add_argument("--rate", action="store_true", help="揭晓后四档评分并写入下次到期")
    p.add_argument("--no-wait", action="store_true", help="不逐题停顿，一次全打出来")
    p.set_defaults(fn=cmd_quiz)
    p = sub.add_parser("rot", help="知识腐烂检查：哪些结论该用新资料复核")
    p.add_argument("--days", type=int, default=90,
                   help="结论多久没复核算该看了（默认 90 天）")
    p.add_argument("--min-newer", type=int, default=2,
                   help="之后至少有几条同主题新资料才提示（默认 2）")
    p.set_defaults(fn=cmd_rot)
    p = sub.add_parser("trends", help="时间维度：热点 / UP 主时间线 / 沉寂 / 首发")
    p.add_argument("--hot", type=int, metavar="DAYS",
                   help="近 N 天的热点（被多个条目、多个 UP 讲的主题）")
    p.add_argument("--author", help="某 UP 的视频时间线")
    p.add_argument("--since", help="起始日期 YYYY-MM-DD（配合 --author）")
    p.add_argument("--until", help="截止日期 YYYY-MM-DD（配合 --author）")
    p.add_argument("--dormant", type=int, metavar="DAYS",
                   help="沉寂话题：以前讲得多、近 N 天没人提")
    p.add_argument("--first", help="谁最早讲了某关键词")
    p.add_argument("--min-items", type=int, default=2, help="热点最少被几条讲过")
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(fn=cmd_trends)

    p = sub.add_parser("weights", help="源权重：反馈淘汰")
    _sw.add_cli_args(p, with_db=False)
    p.set_defaults(fn=cmd_weights)

    from bili_digest.distill import annotate as _an
    p = sub.add_parser("annotate", help="我的思考：批注双向同步")
    _an.add_cli_args(p, with_db=False)
    p.set_defaults(fn=cmd_annotate)

    p = sub.add_parser("retier", help="重新分级")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--expire", action="store_true", help="同时处理 TTL 与容量")
    p.set_defaults(fn=cmd_retier)

    p = sub.add_parser("note", help="输出 Obsidian 笔记")
    p.add_argument("--vault")
    p.add_argument("--subdir", default="B站")
    p.add_argument("--uid", action="append")
    p.add_argument("--all", action="store_true", help="输出所有 knowledge/raw 条目")
    p.add_argument("--tier", default=None,
                   help="指定档位（逗号分隔），如 knowledge,raw,cache,discarded")
    p.add_argument("--all-tiers", action="store_true",
                   help="导出所有档位（含 cache 与 discarded）")
    p.add_argument("--list", action="store_true", help="只列出待输出条目")
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(fn=cmd_note)

    p = sub.add_parser("index", help="建/更新向量索引")
    p.add_argument("--dtype", default="f16", choices=["f32", "f16", "i8"])
    p.add_argument("--rebuild", action="store_true",
                   help="清空重建（回收孤儿向量；全量编码，6 万块约 5~7 分钟）")
    p.set_defaults(fn=cmd_index)

    p = sub.add_parser("search", help="混合检索")
    p.add_argument("query")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="显示每条结果的覆盖等级（这个条目读到了多少）")
    p.add_argument("--top-k", type=int, default=rc.DEFAULT_TOP_K)
    p.add_argument("--auto-k", action="store_true", help="按问题复杂度自适应 top-k")
    p.add_argument("--uid", help="限定单个条目")
    p.add_argument("--kinds", help="限定内容块类型，逗号分隔")
    p.add_argument("--content-only", action="store_true",
                   help="同 --audience exclude（只返回内容本身，不混评论与弹幕）")

    p.add_argument("--show-id", action="store_true",
                   help="每条结果后附 block id（短 uid 本来就显示）")
    p.add_argument("--pick", metavar="N[,M]",
                   help="就地采纳第 N 条；可 --pick 1,4,6 一次采纳多条")
    p.add_argument("--note", nargs=2, metavar=("N", "TEXT"),
                   help="就地批注第 N 条所属的条目")
    p.add_argument("--pin", action="store_true",
                   help="把这次查询登记进问题池")
    p.add_argument("--sources", choices=["primary", "derived", "all"],
                   help=("按证据角色选来源：primary=只用正文 / "
                         "derived=再加摘要要点与标题 / all=全部（含评论弹幕，默认）。"
                         "比 --kinds 好记：按 provenance 分档，不按 12 个 kind 名"))
    p.add_argument("--audience", choices=["include", "exclude"],
                   help="观众反应（评论/弹幕）：include 纳入、exclude 排除。"
                        "不给则各粒度按原默认（block 含、item 排除）")
    p.add_argument("--since", help="起始时间：7d / 30d / last_week / 2026-01-01")
    p.add_argument("--until", help="结束时间，省略表示到现在")
    p.add_argument("--time-field", default="saved_at",
                   choices=["saved_at", "published_at"])

    p.add_argument("--min-duration", type=int, metavar="秒", help="最短时长")
    p.add_argument("--max-duration", type=int, metavar="秒", help="最长时长")
    p.add_argument("--min-blocks", type=int, help="内容块数下限（弹幕/评论多 = 块多）")
    p.add_argument("--max-blocks", type=int, help="内容块数上限")
    p.add_argument("--recency", type=float, default=0.0,
                   help="0~1 的近期加权强度，0 不启用")
    p.add_argument("--no-rerank", action="store_true")
    p.add_argument("--deep", action="store_true",
                   help="深查：把口语查询译成术语化表达再审一次（每查询多一次 LLM 调用，"
                        "实测 R@10 +23pt；默认关）")
    p.add_argument("--partitioned", action="store_true",
                   help="分区检索：按块间独立度分组（段落/短句/字幕行），"
                        "各自归一化后融合。推荐用于话题型查询")
    p.add_argument("--granularity", "-g", default="block",
                   choices=["block", "seg", "item", "topic"],
                   help="结果粒度：block=哪句话（默认）/ item=哪个视频（自动聚合+附摘要）"
                        "/ topic=按主题归组")
    p.add_argument("--fast", action="store_true",
                   help="纯全文检索：不加载嵌入模型（约 2.2 秒 → 0.2 秒），"
                        "代价是单路检索（R@10 98.7%% → 95.3%%）。适合只想快速定位的场景")
    p.add_argument("--rerank", action="store_true",
                   help="启用晚交互重排：top-1 81.5%%->97%%，但冷启动多约 5 秒"
                        "（要加载 1.25GB 模型；MCP 服务端默认开启，CLI 默认关闭）")
    p.add_argument("--show-source", action="store_true", help="显示来源视频")
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser("lateon", help="晚交互重排缓存（构建/状态）")
    p.add_argument("--build", action="store_true", help="构建缓存（需下载 1.25GB 模型）")
    p.add_argument("--force", action="store_true", help="强制重建")
    p.set_defaults(fn=cmd_lateon)

    p = sub.add_parser("graph", help="知识图谱边（图扩展）")
    p.add_argument("--build", action="store_true", help="建边")
    p.add_argument("--vault", help="Obsidian vault 路径（读双链）")
    p.add_argument("--subdir", default="B站")
    p.add_argument("--expand", metavar="UID", help="从条目做多跳扩展")
    p.add_argument("--hops", type=int, default=2)
    p.add_argument("--stats", action="store_true")
    p.set_defaults(fn=cmd_graph)

    p = sub.add_parser("asr", help="按需转写：给无字幕条目补字幕（默认只转前 30 分钟）")
    p.add_argument("uid", nargs="?", help="条目 uid 或 BV 号")
    p.add_argument("--queue", action="store_true",
                   help="优先级队列：该先转哪几条（命中问题 > 有观众反应 > 只有标题）"
                        "，附每条与合计的预估机时")
    p.add_argument("--list", action="store_true",
                   help="列出「有内容但无字幕」的条目（纯按时长升序，不做相关性匹配）")
    p.add_argument("--limit", type=int, default=20, help="--list 列几条（默认 20）")
    p.add_argument("--model", default="small",
                   help="whisper 模型（tiny/base/small/medium；默认 small）")
    p.add_argument("--max-minutes", type=float, default=30.0,
                   help="限时长：只转前 N 分钟（默认 30）")
    p.add_argument("--all", action="store_true", help="不裁剪，整条转")
    p.add_argument("--language", default="zh", help="语言（zh/en/auto…）")
    p.add_argument("--threads", type=int, default=8, help="CPU 线程数（默认 8）")
    p.add_argument("--force", action="store_true", help="已有字幕/已转过也重跑")
    p.add_argument("--keep-audio", action="store_true", help="保留转写用的 wav")
    p.add_argument("--audio", help="用本地音频文件，跳过下载")
    p.add_argument("--hotwords", action="store_true",
                   help="用评论/弹幕里的高频专有名词当前置热词（实测召回 +1~6 个，"
                        "转写可能慢 20~80%%）")
    p.add_argument("--no-index", action="store_true", help="只写块，不建向量索引")
    p.add_argument("--dry", action="store_true", help="只看时长/估算/依赖，不下载不转写")
    p.set_defaults(fn=cmd_asr)

    p = sub.add_parser("gaps",
                       help="检索失败信号：零结果 / 重复查询 / 低分（只读 query_log）")
    p.add_argument("--days", type=int, default=30)
    p.set_defaults(fn=cmd_gaps)

    p = sub.add_parser("clues",
                       help="「我缺什么」：内容里提到但库里没有的 BV（只读，零请求）")
    p.add_argument("--tier", type=int, default=0, choices=[0, 1, 2, 3],
                   help="只显示某一层：1=UP主动引用 · 2=提炼时提到 · 3=观众/其它 · 0=全部")
    p.add_argument("--limit", type=int, default=40)
    p.add_argument("--export", metavar="FILE",
                   help=("把候选 BV 导出成一行一个，可直接喂给 fetch（分批 30~40 个）。"
                         "为什么需要：第 1 层有 134 个候选，靠手工复制「要看它是什么」"
                         "不现实 —— 那正是「预测主动」没闭环的地方：能给建议，不能给队列"))
    p.set_defaults(fn=cmd_clues)

    p = sub.add_parser("purge",
                       help="按 UP 或 BV 彻底清除条目（本地 + 可选 B站取消收藏，默认 dry）")
    p.add_argument("--up", default="", help="按 UP 名精确匹配")
    p.add_argument("--bv", default="", help="按单个 BV 号")
    p.add_argument("--apply", action="store_true",
                   help="真删本地（DB 6 张表 + 笔记文件）；不加只报数")
    p.add_argument("--remote", action="store_true",
                   help="同时取消 B站收藏（需 --apply 与 --media-id）")
    p.add_argument("--media-id", default="",
                   help="收藏夹 id；默认收藏夹通常是 3177807922")
    from bili_digest.quality.purge import run as _purge_run
    p.add_argument("--db", default="knowledge.db")
    p.add_argument("--vault", default="_vault")
    p.set_defaults(fn=_purge_run)

    p = sub.add_parser("gate",
                       help="认识论晋升门：validated / provisional / reject（默认只读）")
    p.add_argument("--tier", default="knowledge", help="看哪个档（默认 knowledge）")
    p.add_argument("--apply", action="store_true",
                   help="写库（还要 --yes）")
    p.add_argument("--revert", metavar="BATCH", help="撤销某个批次（还要 --yes）")
    p.add_argument("--yes", action="store_true", help="确认写库")
    p.set_defaults(fn=cmd_gate)

    p = sub.add_parser("status",
                       help="统一状态视图：整体怎么样、下一步该做哪一件（复用 health/budget/harvest/gate）")
    p.add_argument("--json", action="store_true", help="机器可读")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("budget",
                       help="自主动作的预算与账本：今天系统自己做了什么、超没超额度")
    p.add_argument("--days", type=int, default=1, help="看近几天（默认今天）")
    p.add_argument("--set", metavar="KEY=VAL",
                   help="改某维度的日上限（写 meta_kv，--reset 可改回）")
    p.add_argument("--reset", action="store_true", help="预算恢复默认（账本不动）")
    p.add_argument("--limit", type=int, default=12, help="明细列几行")
    p.set_defaults(fn=cmd_budget)

    p = sub.add_parser("stats", help="统计")
    p.add_argument("--check", action="store_true",
                   help="跑完整的索引一致性检查（多 4 秒；深度检查用 python -m bili_digest.quality.check_index）")
    p.add_argument("--evidence", action="store_true",
                   help="看 evidence_basis 分布（每条摘要建立在什么之上：正文 / 只有评论 / 空壳）")
    p.set_defaults(fn=cmd_stats)

    p = sub.add_parser("why", help="解释某条为什么是这个分级（因子分解）")
    p.add_argument("uid", help="条目 uid 或 BV 号")
    p.set_defaults(fn=cmd_why)

    p = sub.add_parser("harvest", help="从产出结构反推缺口（该补哪些主题）")
    p.add_argument("--thin", type=int, default=5, help="主题少于几条算「差一点成型」（默认 5）")
    p.set_defaults(fn=cmd_harvest)

    p = sub.add_parser("bases", help="生成 Obsidian Bases 视图（表格 + 图谱）")
    p.add_argument("--vault", required=True, help="Obsidian vault 路径")
    p.add_argument("--name", default="B站视频.base")
    p.set_defaults(fn=cmd_bases)

    p = sub.add_parser("selfheal", help="自愈：发现 → 动作 → 执行 → 验证")
    p.add_argument("--dry", action="store_true", help="只出计划，不执行")
    p.add_argument("--allow-llm", action="store_true",
                   help="额外允许需要 LLM 的动作（会覆盖既有提炼）")
    p.set_defaults(fn=cmd_selfheal)

    p = sub.add_parser("moc", help="为主题生成 MOC 枢纽笔记（Obsidian Graph View 的星形中心）")
    p.add_argument("--vault", required=True, help="Obsidian vault 路径")
    p.add_argument("--min-members", type=int, default=3,
                   help="成员数达到该值的主题才建 MOC（默认 3）")
    p.set_defaults(fn=cmd_moc)

    p = sub.add_parser("health", help="库健康总览：一致性 / 新鲜度 / 覆盖 / 失败记录")
    p.add_argument("--full", action="store_true", help="加依赖自检与登录态（会出网）")
    p.set_defaults(fn=cmd_health)
    sub.add_parser("adapter", help="列出平台适配器").set_defaults(fn=cmd_adapter)

    a = ap.parse_args()

    try:
        _cmd = sys.argv[1] if len(sys.argv) > 1 else "?"
        from bili_digest.schema.core_schema import connect as _conn
        from bili_digest.quality import budget as _budget
        from bili_digest.paths import DB as _DB
        _c = _conn(_DB)
        _budget.record(_c, actor="cli", step="view:" + _cmd, rows=0, reversible=False)
        _c.close()
    except Exception:
        pass

    return a.fn(a) or 0
