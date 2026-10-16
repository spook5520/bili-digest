<div align="center">

# bili-digest

**把 B站收藏变成本地可检索的知识库**

字幕 / 弹幕 / 评论 → LLM 提炼 → 按内容搜索

<img src="https://img.shields.io/badge/Python-3.10%2B-3776ab?style=for-the-badge&logo=python&logoColor=white" alt="Python" />
<img src="https://img.shields.io/badge/Deps-4-2ea043?style=for-the-badge" alt="Deps" />
<img src="https://img.shields.io/badge/Tests-276%20passing-success?style=for-the-badge" alt="Tests" />
<img src="https://img.shields.io/badge/License-MIT-blue?style=for-the-badge" alt="License" />

</div>

---

## 它做什么

B站收藏夹只能按标题找。这个工具把每个视频的**字幕、弹幕、评论**抓下来，交给
LLM 提炼成结构化条目（摘要 · 要点 · 观众视角 · 主题词 · 洞察 · 评分），统一存进
一个本地 SQLite —— 之后就能**按内容搜**，而不只是按标题：

> 「我收藏里有哪些视频讲过某个主题」
> 「关于某件事，我存过的视频都怎么说的」
> 「这句话是哪个视频里讲的」

分别对应三种粒度：**按内容找视频** · **跨视频聚合** · **定位到具体某一句**。

检索完全在本地跑（本地 ONNX 嵌入模型 + 全文索引，无网络调用）。
只有抓取（B站 API）与提炼（LLM 端点）会出网，详见[数据边界](#数据边界)。

## 仓库结构

代码按**职责**分目录。`import` 用短名（`import rag_core`），跨目录也照旧工作 ——
因为 `paths.py` 会把各子目录挂上 `sys.path`（它是所有模块都导入的那一个）。

```
bili-digest/
├── kb.py            命令行入口 ── 抓取 / 提炼 / 检索 / 图谱
├── kb_mcp.py        MCP 服务入口 ── 让 AI 助手直接查这个知识库
├── paths.py         路径与配置的唯一定义处（含子目录挂载）
│
├── core/            基础设施
│     数据模型与表结构 · 来源注册表 · 平台适配层
│
├── ingest/          把内容弄进来
│     收藏夹 / 稍后再看 / 合集 / 专栏的抓取 · 字幕弹幕评论 · 提炼编排
│
├── distill/         把原文变成可检索的形态
│     提炼 · 分类 · 主题 · 评分 · 分段 · 合成
│
├── retrieve/        把东西找回来
│     向量 + 全文 RRF 融合 · 分区检索 · 时间与形式过滤 · 关键词图
│
├── graph/           条目之间的关系
│     知识图谱 · 健康度 · 发现链
│
├── output/          往外送
│     Obsidian 导出 · 给 AI 助手用的索引生成
│
├── quality/         它到底好不好
│     生命周期分级 · 一致性检查 · 观测 · 自我回忆测试 · 体检
│
└── tools/           测量与标定
      检索评估 · 改写档评估 · 基准 · 参数标定 · 统计检验
```

**任何模块都能直接跑**（`python quality/check_index.py`）—— 项目大量模块是可独立执行的工具。
## 快速开始

```bash
pip install fastembed usearch jieba numpy     # 1. 装依赖
python quality/doctor.py                              # 2. 自检（会告诉你还缺什么）
python kb.py init && python kb.py import      # 3. 建库并导入收藏

# 可选：把另外三种来源也接进来（都写进 shelf.db，再由 import 带进知识库）
python ingest/bili_shelf.py toview                  # 「稍后再看」
python ingest/bili_shelf.py seasons --up <MID>       # 某个 UP 的视频合集
python ingest/bili_shelf.py articles --up <MID>      # 某个 UP 的专栏
python kb.py fetch --limit 20                 # 4. 抓内容（字幕/弹幕/评论）
python kb.py derive --limit 20                # 5. 提炼
python kb.py search "你的问题"                 # 6. 搜
```

第 6 步首次运行会下载嵌入模型（约 90MB），之后检索离线可用。

## 配置

两项凭据。也可以先跑 `python quality/doctor.py` 看还缺什么，或 `cp .env.example .env` 写文件。

**B站登录态**（抓收藏夹 / 弹幕 / 评论需要）— 推荐扫码，凭证自动写 `.env`：

```bash
python ingest/qrlogin.py                             # 手机 B站 App 扫
```

也可手动提供，按 `--sessdata` → `KB_BILI_SESSDATA` → `KB_BILI_HIVE`（客户端登录态
文件）→ `.env` 的顺序查找。SESSDATA 的手动取法：浏览器登录 B站 → `F12` →
`Application` → `Cookies` → `SESSDATA`。

**LLM**（提炼 / 合成需要）— 任何 OpenAI 兼容端点：

```bash
KB_LLM_BASE=https://api.deepseek.com/v1
KB_LLM_MODEL=deepseek-chat
KB_LLM_KEY=<key>
```

指向本地推理服务即可让内容不出网（vLLM `127.0.0.1:8000/v1` · Ollama
`127.0.0.1:11434/v1` · LM Studio `127.0.0.1:1234/v1`，此时 key 随便填）。

<details>
<summary><b>全部可配置项</b></summary>

| 变量 | 默认 | 说明 |
| :--- | :--- | :--- |
| `KB_DATA_DIR` | 脚本目录 | 库 / 索引 / 模型 / 抓取产物 |
| `KB_BILI_SESSDATA` | — | B站登录态 |
| `KB_BILI_HIVE` | — | 客户端登录态文件（能拿到 DedeUserID） |
| `KB_LLM_BASE` | `https://api.deepseek.com/v1` | LLM 端点 |
| `KB_LLM_MODEL` | `deepseek-chat` | 模型名 |
| `KB_LLM_KEY` | — | API key |
| `KB_EMBED_MODEL` | `BAAI/bge-small-zh-v1.5` | 换模型须同时改维度并 `--rebuild` |
| `KB_EMBED_DIM` | `512` | 嵌入维度 |
| `KB_HF_ENDPOINT` | `https://hf-mirror.com` | 模型下载镜像 |
| `KB_AD_SERVER` | `https://bsbsb.top` | 广告段数据源，留空则跳过 |
| `KB_VAULT` | 自动浅扫 | Obsidian vault 路径 |
| `KB_CONTEXT_TARGET` | 脚本目录 `kb-context.md` | 提示词片段写入目标 |

</details>

## 用法

**抓取** — 按需选择素材（`meta` 元数据 · `sub` 字幕 · `dm` 弹幕 · `reply` 评论）：

```bash
kb.py fetch --limit 20                 # 批量
kb.py fetch --uid bilibili:BVxxxx      # 单条
kb.py fetch --sources meta,sub         # 精简模式（省请求、降风控）
python ingest/batch_refine.py                 # 批量全流程：抓取 + 提炼，断点续跑
```

B站触发风控时返回 HTTP 412，`batch_refine.py` 会按失败率自适应冷却，可连抓数十条。

**提炼** — 产出摘要 / 要点 / 观众视角 / 主题词 / 洞察 / 五维评分：

```bash
kb.py derive --limit 20                       # 批量
kb.py derive --uid bilibili:BVxxxx --force    # 重提炼
kb.py derive --incremental                    # 只提炼素材有增长的条目
kb.py synth --report                          # 跨条目综述
```

**检索** — 四种粒度，决定返回「哪句话 / 哪个视频 / 哪类主题」：

```bash
kb.py search "..."                # 块级（默认）：定位到哪句话
kb.py search "..." -g seg         # 段级
kb.py search "..." -g item        # 条目级：哪个视频（聚合 + 附摘要）
kb.py search "..." -g topic       # 主题级：按主题归组
kb.py search "..." --since 7d     # 时间过滤
kb.py search "..." --partitioned  # 分区链路（按块间独立度分组）
```

**反馈** — `pick` 是项目唯一的真实反馈信号，同时记录「对」与「排第几名」：

```bash
kb.py pick 3                  # 采纳最近一次搜索的第 3 条
kb.py pick 3 --note "步骤清楚"  # 带理由
kb.py pick --list             # 采纳记录与排名分布
python quality/obs.py --report        # 采纳分析：排序准度、评分与实际有用的相关性
```

**私信 / @ 触发** — 在任意设备上用 B站账号跟这个号对话，它按**三层**回应，
每层用什么、花不花钱都不同：

```
发一句话（不是链接）
  ① 本地检索        库里的条目 → 直接给候选（零 LLM）
  ② 站内主动搜索    B站搜一遍 → 给出未收录的候选（零 LLM）
                    两路并列，因为它们回答的是不同问题：
                    库内是「最接近的是什么」，站内是「到底有没有」
  ③ 回一个序号      库里那条 → 直接给总结（零 LLM）
                    站内那条 → 抓取 + 提炼（这一步才用 LLM）
```

发视频链接（BV 或 av）则跳过前两层，直接进入第三层。

```bash
export KB_INBOX_ALLOW=<允许的uid>   # 白名单，逗号分隔；**不设 = 谁都不许**
python ingest/bili_inbox.py --check        # 凭据与接口是否通
python ingest/bili_inbox.py --once         # 处理一轮（演练）
python ingest/bili_inbox.py --once --send  # 真的回复
python ingest/bili_inbox.py --loop --send --interval 90
```

**检索不必花 token**：①② 两层都是纯本地的（站内搜索也只走 B站 API），
只有第 ③ 层选定之后才调 LLM。

**旧的链接触发方式仍然有效**：

```bash
python ingest/bili_inbox.py --check                       # 凭据与接口是否通
python ingest/bili_inbox.py --once                        # 处理一轮（演练：只显示会回什么）
python ingest/bili_inbox.py --once --send                 # 真的回复
python ingest/bili_inbox.py --loop --send --interval 90   # 常驻轮询
```

视频不在收藏夹里也能总结（抓取按 BV 号走，不依赖收藏关系）。回复是写操作，
**默认演练模式**，加 `--send` 才真发；处理过的消息落 `inbox_log`，
重复轮询不会重复总结、不会重复回复。

**导出** — 给人看的笔记、给宿主用的 MCP、给模型用的提示词索引：

```bash
python output/obsidian_out.py                 # Obsidian 笔记（双链 + 批注同步）
kb.py note --all --vault <路径>         # 批量导出（默认只导 knowledge + raw）
python kb_mcp.py                       # MCP server（stdio）
python output/build_context_index.py --apply  # 提示词片段（供宿主自动加载）
```

<details>
<summary><b>接入 MCP 宿主</b>（Claude Desktop / Cursor / 任何 stdio 宿主）</summary>

```json
{
  "mcpServers": {
    "bili_kb": {
      "command": "python",
      "args": ["/absolute/path/to/kb_mcp.py"]
    }
  }
}
```

可用工具：`kb_search` `kb_item` `kb_related` `kb_synthesize` `kb_stats` `kb_annotate`

</details>

<details>
<summary><b>进阶：素材增长 · 分级 · 体检 · 评测</b></summary>

**素材增长** — 已抓过的视频会有新弹幕/评论，`grow.py` 检测计数变化并按增量提炼：

```bash
python distill/grow.py                 # 只探测（一次 view 调用），列出有增长的条目
python distill/grow.py --apply         # 增长超过门槛才重抓 + 增量提炼
python distill/grow.py --min-growth 50 # 门槛（默认 30）
```

判据用声明值 `danmaku_count` / `reply_count`，不用「块数与计数的差距」——
后者会把抓取上限误判成漏抓。

**段落导航摘要** — 长视频被切成几十段后，每段是几百字连续字幕；给它配一句话，
「这视频分几块、每块讲什么」就一眼可见，也给"只记大意"的查询多开一个入口：

```bash
python distill/seg_summary.py --limit 20            # 看会生成什么（不写库）
python distill/seg_summary.py --limit 200 --write    # 实际生成
python tools/build_kb_index.py && python retrieve/keywords.py --build   # 之后重建索引
```

**分级与屏蔽** — 六维评分（收藏 0.28 · 标记 0.20 · 密度 0.18 · 来源 0.14 ·
质量 0.12 · 新鲜度 0.08）产出保留价值分，分 cache / knowledge / raw / discarded 四档：

```bash
kb.py retier         # 重算分级
kb.py mark BVxxxx    # 标记「这条重要」（计入 marked 维度）—— 需再跑 retier
kb.py blocksuggest   # 从已淘汰条目里建议屏蔽规则
```

档位**只影响 Obsidian 导出**（默认只导 knowledge + raw）—— 检索与提炼都看全库。
`discarded` 只写标记，内容与向量不删除。

**体检**：

```bash
python quality/doctor.py           # 配置自检
python quality/check_index.py      # 索引一致性 + 数据完整性抽查 + 判定一致性
python quality/obs_selfrecall.py   # 检索鲁棒性（四档查询）
python quality/faithfulness.py     # 提炼忠实性（实体级：编造 / 覆盖 / 内部矛盾）
python quality/audit.py            # 端到端审计
```

**评测**（离线，不进检索链路）— 用 LLM 打相关性标签，比较两组参数：

```bash
python tools/eval_retrieval.py 150 --qkind fragment --compare
python tools/rewrite_eval.py        # 改写查询档
python tools/bench3.py              # 合成查询档
```

</details>

## 数据来源

四种，都从 B站来，但**意图强度不同**——这决定了它们该被如何对待：

| 来源 | 含义 | 接入方式 | 标识 |
| :--- | :--- | :--- | :--- |
| **收藏夹** | 「我决定要留的」，已过一轮筛选 | `kb.py import` | `folder.id` 为正数 |
| **稍后再看** | 「我打算看的」，还没看 | `bili_shelf.py toview` | `folder.id = -1` |
| **视频合集** | 「这个 UP 的这一系列我都要」 | `bili_shelf.py seasons --up MID` | `folder.id = -(1000000+season_id)` |
| **专栏**（图文） | 「我读过的图文」 | `bili_shelf.py articles --up MID` | `type=21` · `vid='cv<id>'` |

这四者共用 `folder` / `video` / `membership` 三张表（在 `shelf.db`），
`import_collections` 遍历它们导入 `knowledge.db`——**新增来源不必改导入逻辑**。

`shelf.db.video.type` 记录内容形态：`2`=视频 · `21`=专栏 · `24`=影视。

## 数据边界

| 环节 | 在哪 | 说明 |
| :--- | :--- | :--- |
| 抓取 | 出网 | B站 API |
| 存储 | **本地** | 单文件 SQLite + usearch 索引 |
| 检索 | **本地** | 本地 ONNX 嵌入模型，无网络调用 |
| 提炼 | 出网 | 条目原文（约 2000 字）发给 `KB_LLM_BASE` |

想让提炼也不出网：把 `KB_LLM_BASE` 指向本地推理服务，或跳过提炼、只做抓取 + 检索。

## 实现

四个概念贯穿全部模块 —— 内容（原文与派生物统一进 `content_block`）· 加工（判定 /
提炼 / 增量 / 合成）· 评分（六维 + 生命周期分级）· 检索（向量 + 全文 RRF 融合，
四种粒度聚合）。配置集中在 `paths.py`，来源注册表在 `sources.py`，平台适配在
`adapters.py`。

<details>
<summary><b>模块职责表</b></summary>

| 概念 | 主要模块 |
| :--- | :--- |
| **内容** | `core_schema` `sources` `bili_digest` `seg_boundary` |
| **加工** | `classify` `evidence` `extract` `synthesize` |
| **评分** | `score` `lifecycle` `source_weight` |
| **检索** | `rag_core` `partitioned_search` `retrieve` `temporal` |

</details>

实测（7.4 万块 / 355 条目）：

| 场景 | 耗时 |
| :--- | :--- |
| **MCP server**（常驻，模型只加载一次） | 首次 1.1s · 之后约 11ms |
| **CLI**（`kb.py search`，每次都是新进程） | 每次 1.5~2.7s |

**这两个数差两个数量级，是场景不同**：嵌入模型（约 90MB）要在进程里加载一次，
CLI 每次调用都得重来，而 MCP server 启动时预热、之后一直驻留。

所以用 CLI 且在意延迟时，别拿 `kb.py search` 循环查 —— 起一次 `kb_mcp.py`
让宿主去问，或者走私信通道（那边也是常驻进程）。

（这个区别是查 `obs.py` 的慢查询报告时发现的：日志里最慢 2.7s，
而这里原先只写「稳态约 10ms」，没说那是 MCP 场景。）

## 依赖与借鉴

**依赖 4 个**：`fastembed` · `usearch` · `jieba` · `numpy`，外加可选的 `qrcode`（仅扫码登录用）。

**思路借鉴**（设计参考，代码自写）：[chonkie](https://github.com/chonkie-inc/chonkie) 的语义分段 ·
[LightRAG](https://github.com/HKUDS/LightRAG) 的图谱检索 ·
[bilibili-API-collect](https://github.com/SocialSisterYi/bilibili-API-collect) 的接口文档 ·
[TLRKFXE/BiliShelf](https://github.com/TLRKFXE/BiliShelf) 的节流策略 ·
[unified-agent-memory](https://github.com/Noelune/unified-agent-memory) 的「索引进提示词 + 工具查细节」。

## 许可与隐私

MIT，见 [LICENSE](./LICENSE)。

**凭据**由使用者显式提供（`--sessdata` / `KB_BILI_SESSDATA` / `KB_BILI_HIVE`）——
程序不扫描磁盘、不落盘、不进版本库。

**数据文件**（`knowledge.db` · `vectors.usearch` · `bili-digest-out/` · 笔记目录）均不入版本库。




