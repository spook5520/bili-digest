<a id="readme-top"></a>

<div align="center">

# bili-digest

**B站收藏 → 本地可检索的知识库**

字幕 · 弹幕 · 评论 → LLM 提炼 → 按内容搜索 · 跨条目综合 · 时间维度查询

<img src="https://img.shields.io/badge/Python-3.10%2B-3776ab?style=flat-square&logo=python&logoColor=white" alt="Python" />
<img src="https://img.shields.io/badge/Deps-4-2ea043?style=flat-square" alt="Deps" />
<img src="https://img.shields.io/badge/Offline-检索全程本地-6f42c1?style=flat-square" alt="Offline" />
<img src="https://img.shields.io/badge/License-MIT-blue?style=flat-square" alt="License" />

</div>

---

B站收藏夹只能按标题找。这个工具把每个视频的**字幕、弹幕、评论**抓下来，交给 LLM
提炼成结构化条目（摘要 · 要点 · 观众视角 · 主题词 · 洞察 · 评分），存进一个本地
SQLite —— 之后就能**按内容搜**。

```bash
pip install -e .
python -m bili_digest search "我收藏里有哪些视频讲过某个主题"
```

检索全程在本地跑（本地 ONNX 嵌入模型 + 全文索引，无网络调用）。只有抓取（B站 API）
和提炼（LLM 端点）会出网。**不需要 GPU，也不需要外部向量数据库。**

<details>
<summary><b>目录</b></summary>

- [它解决什么问题](#它解决什么问题)
- [安装](#安装)
- [快速开始](#快速开始)
- [配置](#配置)
- [用法](#用法) — [检索](#检索) · [抓取](#抓取) · [提炼](#提炼) · [维护](#维护) · [图谱与浏览](#图谱与浏览) · [导出](#导出)
- [它是怎么工作的](#它是怎么工作的)
- [闭环](#闭环产出反过来决定抓什么)
- [作为 MCP 服务](#作为-mcp-服务)
- [数据来源](#数据来源)
- [许可与隐私](#许可与隐私)

</details>

## 它解决什么问题

| | 原生收藏夹 | 本工具 |
|:---|:---|:---|
| 找东西 | 只认标题 | **按内容**：说了什么、哪一句、谁的观点 |
| 跨条目 | 无 | 「哪些视频讲过同一个话题」· 矛盾点 · 综述 |
| 时间维度 | 按收藏时间排 | 热点 / UP 时间线 / 沉寂话题 / 谁最早讲 |
| 观众反应 | 看不到 | 弹幕密度（哪段值得看）· 高赞评论 |
| 数据归属 | 平台 | 本地 SQLite + 本地向量索引，可备份可迁移 |
| 给 AI 用 | 不支持 | MCP server，AI 助手直接查 |

## 安装

```bash
pip install -e .                        # 注册 kb / kb-mcp 两个命令
python -m bili_digest.quality.doctor    # 自检，会告诉你还缺什么
```

不想装也行，`python -m bili_digest <子命令>` 与 `kb <子命令>` 等价。

可选依赖按需装：

| 组 | 用途 |
|:---|:---|
| `.[login]` | 扫码登录（qrcode） |
| `.[review]` | 间隔重复排期（fsrs） |
| `.[tokenizer]` | 换切词引擎，省约 1.1 秒启动（cppjieba） |
| `.[mcp]` | MCP server（官方 SDK） |

## 快速开始

```bash
# 1. 建库并导入收藏夹（此时只有标题，还不能按内容搜）
python -m bili_digest init
python -m bili_digest.ingest.bili_shelf sync        # 收藏夹 → shelf.db
python -m bili_digest.ingest.import_collections     # shelf.db → knowledge.db

# 2. 抓内容 + 提炼（断点续跑；收尾自动切段并建索引）
python -m bili_digest.ingest.batch_refine --batch 20 --max 60

# 3. 按内容搜
python -m bili_digest search "你的问题"
```

首次搜索会下载嵌入模型（约 90MB），之后**离线可用**。

> **`--batch` 是每批条数，`--max` 才是本次总数** —— 不配 `--max` 会一直跑到队列空。

## 配置

两项凭据，写进 `.env` 或设为环境变量。

```bash
# B站登录态（抓内容需要）—— 推荐扫码，凭证自动写 .env
python -m bili_digest.ingest.qrlogin

# LLM 端点（提炼需要）—— 任何 OpenAI 兼容端点都行
KB_LLM_BASE=https://api.deepseek.com/v1
KB_LLM_MODEL=deepseek-chat
KB_LLM_KEY=<key>
```

指向本地推理服务即可让内容完全不出网（vLLM `127.0.0.1:8000/v1`、Ollama
`127.0.0.1:11434/v1`、LM Studio `127.0.0.1:1234/v1`，此时 key 随便填）。

<details>
<summary><b>全部可配置项</b></summary>

| 变量 | 默认 | 说明 |
| :--- | :--- | :--- |
| `KB_DATA_DIR` | 项目根 | 库 / 索引 / 模型 / 抓取产物 |
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
| `KB_CONTEXT_TARGET` | 项目根 `kb-context.md` | 提示词片段写入目标 |

</details>

## 用法

### 检索

```bash
python -m bili_digest search "问题"                     # 块粒度，定位到具体某一句
python -m bili_digest search "问题" -g item              # 条目粒度（哪个视频），自动聚合 + 附摘要
python -m bili_digest search "问题" -g item --rerank     # 加晚交互重排（top-1 更高，慢约 5 秒）
python -m bili_digest search "问题" --fast               # 纯全文，不加载模型（2.2s → 1.8s，R@10 略降）
python -m bili_digest search "问题" --since 30d          # 时间过滤
python -m bili_digest search "问题" --deep               # 口语查询改写后再搜（多一次 LLM 调用）
python -m bili_digest search "问题" --audience exclude   # 只要内容本身，不混评论与弹幕
```

`--audience` 是观众反应（评论/弹幕）的总开关：默认 block 粒度含、item 粒度排除，
显式给了就覆盖两边。

**评论不是噪音**（实测 350 条里 68% 有实质内容），但它的条数比字幕段高两个数量级，
会霸占 top-10 过半席位。`exclude` 换来的是更高的块级精度，代价是结果多样性略降。

### 抓取

```bash
python -m bili_digest fetch BVxxxxxxxxxx            # 单条（默认抓字幕/弹幕/评论）
python -m bili_digest fetch bilibili:BVxxxxxxxxxx   # 平台前缀会被剥掉
python -m bili_digest fetch BVTestExample BVxxxxxxxxxx   # 一次多条
python -m bili_digest fetch BVTestExample --meta-only    # 只取元数据，不抓内容
```

> `--meta-only` 只写 `item` 表（标题 / 时长 / 封面），**不产生任何内容块** ——
> 这样建出来的条目检索不到，要等之后正常抓一次才有内容。

`fetch` 是**单条 / 小批**工具：没有条数上限参数，也不带风控退避。要批量就交给
`python -m bili_digest.ingest.batch_refine` —— 它才是有退避、断点续跑、边抓边提炼的入口。

`batch_refine` 固定抓元数据 + 字幕 + 评论（不含弹幕：短视频常无字幕，而评论的信息
密度比弹幕稳），遇风控自适应冷却。

### 提炼

```bash
python -m bili_digest derive --limit 10    # 提炼 10 条（自动建索引）
python -m bili_digest refine BVxxxxxxxxxx  # 重抓并按新提示词重提炼
```

### 维护

```bash
python -m bili_digest health                      # 库健康总览（一个入口看全局，秒级）
python -m bili_digest health --full               # 加依赖自检与登录态（会出网）
python -m bili_digest.quality.check_index         # 深检：索引一致性 / 孤儿块 / 陈旧向量
python -m bili_digest.quality.kb_cov --library    # 覆盖率分级
```

`health` 把分散在各处的检查合成一张表：索引一致性、陈旧向量、关键词基线、切段完整性、
提炼覆盖、规模与可读性、抓取失败记录、重排缓存。全绿退出码 0，有 WARN 是 1，
有 FAIL 是 2，可直接当 CI 门禁。

### 图谱与浏览

```bash
python -m bili_digest graph --build                    # 建边（同 UP / 共享主题 / 双链）
python -m bili_digest browse --topic 检索增强生成       # 按主题浏览
python -m bili_digest trends --hot 30                  # 最近哪些话题被多个 UP 在讲
python -m bili_digest highlights <uid>                 # 哪段值得看（弹幕密度）
python -m bili_digest popularity --by play --top 10    # 哪个更值得看
```

`highlights` 用弹幕密度当「值不值得看」的信号 —— 观众在哪一刻发弹幕，就是在标注那一刻。

### 导出

```bash
python -m bili_digest note --vault D:\\Obsidian --uid bilibili:BVxxxxxxxxxx
```

<p align="right">(<a href="#readme-top">back to top</a>)</p>

## 它是怎么工作的

```
收藏夹 / 稍后再看 / 合集 / 专栏
        │  bili_shelf sync          （只拿标题与元数据）
        ▼
    shelf.db ──import_collections──▶ knowledge.db
                                       ▲
B站 API ──fetcher──▶ raw.sqlite ──refine┘   抓取 + 入库 + 打分分档
                    │
                    └─ LLM 提炼 ─▶ 摘要/要点/主题/洞察/评分 ─▶ 切段 ─▶ 索引
                                                              │
                                          search / MCP ◀───────┘
```

两个库分工：`shelf.db` 存「我收藏了什么」（来自 B站，可重新同步），
`knowledge.db` 存「内容讲了什么」（抓取 + 提炼的产物，不可重建）。

代码收在单一包 `bili_digest/` 下，按职责分层：

| 目录 | 职责 |
| :--- | :--- |
| `cli/` `mcp/` | 两个入口：命令行（`cli/commands/` 按职责分组）与 MCP server |
| `schema/` | 通用 schema（平台无关）与 uid 约定 |
| `adapters/` | 平台适配：B站 / YouTube / 知乎 |
| `ingest/` | 抓取、入库、批量编排、迁移 |
| `distill/` | LLM 提炼：摘要 / 要点 / 主题 / 洞察 / 评分 |
| `retrieve/` | 检索内核：向量 + 全文 + RRF 融合 + 重排 |
| `graph/` | 知识图谱边（同 UP / 共享主题 / 双链） |
| `output/` | Obsidian 导出、提示词索引 |
| `quality/` | 体检与诊断 |
| `tools/` | 标定、评估、实验 |
| `paths.py` | 路径与配置的唯一来源 |

<p align="right">(<a href="#readme-top">back to top</a>)</p>

## 闭环：产出反过来决定抓什么

大部分工具是单向的：抓 → 提炼 → 存，存下来的不会再说话。这一层是反过来的那半。

```bash
kb question --add "我长期想搞清的一个问题"   # 登记长期问题
kb question --from-log                     # 从查询记录里发现「我反复在问什么」
kb harvest                                 # 从已有产出的结构反推：还缺什么
kb selfheal                                # 体检发现的问题 → 生成动作 → 执行 → 复验
```

登记的问题会影响抓取顺序（`batch_refine --order value` 优先抓与问题相关的那批），
抓完再自动对照一遍，告诉你这批里有没有在回答你的问题。

**判断标准是「值得不值得」，不是「完不完整」**：`harvest` 会明确说出哪个缺口不该补 ——
补齐一个你不关注的薄主题，只是让图谱好看。

## 作为 MCP 服务

`python -m bili_digest.mcp.server` 让 AI 助手直接查这个库，不用你自己复制粘贴：

```json
{
  "mcpServers": {
    "bili_kb": {
      "command": "python",
      "args": ["-m", "bili_digest.mcp.server"],
      "cwd": "D:/path/to/bili-digest"
    }
  }
}
```

| 工具 | 用途 |
| :--- | :--- |
| `kb_search` | 混合检索（向量 + 中文全文 + RRF），支持时间过滤 |
| `kb_item` | 条目详情：元数据 + 摘要/要点/主题/洞察/评分 + 批注 |
| `kb_related` | 图谱多跳扩展：同 UP / 共享主题 / 双链 |
| `kb_expand` | 按时间轴展开上下文（命中段前后各 2 块） |
| `kb_synthesize` | 横向合成：综述 / 矛盾点 / 待验证问题 / 行动项 |
| `kb_trends` | 时间维度：热点 / UP 时间线 / 沉寂 / 首发 |
| `kb_highlights` | 弹幕密度曲线，判「在叫好还是争论」 |
| `kb_popularity` | 按播放/点赞/弹幕排序 |
| `kb_stats` | 规模与覆盖统计 |

## 数据来源

四种 B站来源，统一写进 `shelf.db`，再由 `import_collections` 带进知识库：

```bash
python -m bili_digest.ingest.bili_shelf sync                  # 收藏夹
python -m bili_digest.ingest.bili_shelf toview                # 稍后再看
python -m bili_digest.ingest.bili_shelf seasons --up <MID>    # 某 UP 的视频合集
python -m bili_digest.ingest.bili_shelf articles --up <MID>   # 某 UP 的专栏
python -m bili_digest.ingest.import_collections               # 以上四种 → knowledge.db（幂等）
```

`python -m bili_digest import --digest <目录>` 是另一回事：它导入旧的
`bili-digest-out/` 单视频产物，不读 `shelf.db`。

## 许可与隐私

MIT，见 [LICENSE](./LICENSE)。

**凭据**由使用者显式提供，程序不扫描磁盘、不落盘、不进版本库。

**数据文件**（`knowledge.db` · `vectors.usearch` · `bili-digest-out/` · 笔记目录）
均不入版本库。
