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

## 📖 目录

- [每条摘要都标着它建立在什么之上](#basis)
- [它解决什么问题](#problem)
- [安装](#install)
- [快速开始](#quickstart)
- [配置](#config)
- [用法](#usage)
- [它是怎么工作的](#how)
- [闭环：产出反过来决定抓什么](#loop)
- [作为 MCP 服务](#mcp)
- [数据来源](#sources)
- [许可与隐私](#license)

<p align="right">(<a href="#readme-top">回到顶部</a>)</p>

<a id="basis"></a>
## 🔍 每条摘要都标着它建立在什么之上

这是这个库最该先看的一个字段。同样的 200 字摘要，从**视频原话**提炼的和从**观众评论**
推的，可靠性差一个量级 —— 而它们读起来一模一样，光看摘要分不出来。

所以每个条目都有一个 `evidence_basis`：

| 值 | 含义 |
|:---|:---|
| `mixed` | 有正文也有评论 |
| `comment-only` | 只有评论 —— 摘要从观众评论推的 |
| `none` | 什么素材都没有（空壳） |

`comment-only` 的比例取决于你怎么抓：只抓评论的话它能占到多数，
补过字幕之后会明显下降。所以这里不给数字 —— **查你自己库里的分布**：

```bash
python -m bili_digest stats --evidence
```

**为什么要把这件事显式标出来**：`comment-only` 的链条是
「视频原话 → 观众用 AI 总结了一版 → 我们读评论提炼 → 你读到」。
三层传递，每一层都忠实地转述了它读到的东西，**所以没有任何一层会报错**。
不标出来，你会把「观众的理解」当成「视频的观点」。

补正文的办法见 [补字幕](#补字幕按需-asr)：B站的 AI 字幕是**视频发布一段时间后
才生成**的，所以入库太早会记成「无字幕」——重扫一遍接口就能补回相当一部分。

<details>
<summary><b>目录</b></summary>

- [每条摘要都标着它建立在什么之上](#basis)
- [它解决什么问题](#problem)
- [安装](#install)
- [快速开始](#quickstart)
- [配置](#config)
- [用法](#-用法) — [检索](#检索) · [抓取](#抓取) · [先重扫 B站 AI 字幕](#先重扫-b站-ai-字幕再考虑-asr) · [提炼](#提炼) · [维护](#维护) · [图谱与浏览](#图谱与浏览) · [导出](#导出)
- [它是怎么工作的](#how)
- [闭环](#-闭环产出反过来决定抓什么)
- [作为 MCP 服务](#mcp)
- [数据来源](#sources)
- [许可与隐私](#license)

</details>

<p align="right">(<a href="#readme-top">回到顶部</a>)</p>

<a id="problem"></a>
## 🎯 它解决什么问题

| | 原生收藏夹 | 本工具 |
|:---|:---|:---|
| 找东西 | 只认标题 | **按内容**：说了什么、哪一句、谁的观点 |
| 跨条目 | 无 | 「哪些视频讲过同一个话题」· 矛盾点 · 综述 |
| 时间维度 | 按收藏时间排 | 热点 / UP 时间线 / 沉寂话题 / 谁最早讲 |
| 观众反应 | 看不到 | 弹幕密度（哪段值得看）· 高赞评论 |
| 数据归属 | 平台 | 本地 SQLite + 本地向量索引，可备份可迁移 |
| 给 AI 用 | 不支持 | MCP server，AI 助手直接查 |

<p align="right">(<a href="#readme-top">回到顶部</a>)</p>

<a id="install"></a>
## 📦 安装

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
| `.[tokenizer]` | 换切词引擎（cppjieba）：0.28 ms/条 vs jieba 4.6 ms。**不装会退到字符级** —— jieba 0.42.1 在 Python 3.12 上部分崩（`xrange`） |
| `.[mcp]` | MCP server（官方 SDK） |
| `.[asr]` | 本地转写（faster-whisper + yt-dlp）—— 没有字幕的视频靠它 |

<p align="right">(<a href="#readme-top">回到顶部</a>)</p>

<a id="quickstart"></a>
## 🚀 快速开始

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

### 搜出来是什么样

```
$ kb search "<你的查询词>"

  混合检索 命中 <N> 条（top-k=<K>）
  ↓ 第 <K> 条之后有断层（比值 0.35）        ← 或「· 结果分数齐平（最小相邻比值 0.93）」

  [1] [视频原话 12:34] <命中片段的首句>
      <下一句，用来判断上下文够不够>
  [2] [观众评论 👍128 某用户] <评论原文>
      …
  [3] [AI 摘要] …
      …
```

**时间戳是视频内的真实位置**（`190:18` 这种表示第 190 分 18 秒，属于该分P 内的连续时间轴），
不是收藏时间，也不是估算。点进去能定位到**那一句话**，而不是「某条视频讲过这个」。

多分P 的视频按分P 独立抓取、切段、索引 —— 所以后面某个分P 里讲的内容，
不会因为前面几个分P 而丢失。

### 能问什么

不知道能问什么，就不会问。这类库能命中的问法有个共同点：
**问题里带具体的产品名、专有名词，或明确的对比项。**

| 问法的形状 | 例子 |
|:---|:---|
| 两个具体工具的取舍 | 「A 和 B 在某个功能上有什么差别」 |
| 具体操作怎么做 | 「某个协议 / 工具怎么从零实现」 |
| 两套材料的对比 | 「X 的速成版和完整版差在哪」 |
| 具体原理为什么 | 「某技术为什么比另一个快」 |
| 具体故障怎么修 | 「某环境编译报错怎么解决」 |

> **不给真实例子** —— 那会泄露这个库里有什么。形状对得上就够了。

反过来，纯抽象问法（「什么是方法论」这种）、人物机构名通常搜不到。**而「原型链」不算**——实测它是八条查询里最相关的一个（top1/top2 都是 JS 原型链的真人讲解）。
**那不是检索坏了，是收藏里没有系统讲过那个** ——
这类库的素材是「别人讲过什么」，不是「知识本身」。

想要跨条目问题（哪些视频讲过同一个话题、哪个 UP 讲得最全），
用 `-g item` 或 `trends` / `browse`，不要用单条搜索硬找。

<p align="right">(<a href="#readme-top">回到顶部</a>)</p>

<a id="config"></a>
## ⚙️ 配置

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

<p align="right">(<a href="#readme-top">回到顶部</a>)</p>

<a id="usage"></a>
## 🔧 用法

### 命令总览（43 个，按用途分组；下表按用途列 35 条，其余见各节）

> 一张表回答「这个项目能做什么」。每个命令只写它**解决什么问题** —— 参数看 `kb <命令> --help`。

**入库**

| 命令 | 解决什么 |
|:--|:--|
| `fetch` | 抓单条/小批（字幕、弹幕、评论）；没有退避，别拿它批量 |
| `import` | 导入已抓好的 `bili-digest-out/` 目录 |
| `refine` | 重抓并按新提示词重提炼一条 |
| `discover` | 按关键词搜索并采纳进库（会先过积压护栏） |
| `adapter` | 看有哪些平台适配器（bilibili / YouTube / 知乎） |

**检索与阅读**

| 命令 | 解决什么 |
|:--|:--|
| `search` | 混合检索（向量 + 全文 + RRF），支持粒度/时间/观众过滤 |
| `expand` | 从命中块展开上下文（粗索引 → 细证据） |
| `asr` | 给没字幕的条目转写（按需、默认限 30 分钟） |

**提炼与产出**

| 命令 | 解决什么 |
|:--|:--|
| `derive` | LLM 提炼：摘要 / 要点 / 观众视角 / 主题 / 洞察 / 五维打分 |
| `synth` | 反刍合成：综述、矛盾点、待验证问题、行动项 |
| `note` | 导出 Obsidian 笔记 |
| `moc` | 为主题生成 MOC 枢纽笔记（Graph View 的星形中心） |
| `bases` | 生成 Obsidian Bases 视图（表格） |

**质量与维护**

| 命令 | 解决什么 |
|:--|:--|
| `health` | 库健康总览（17 项一致性，秒级，可当 CI 门禁） |
| `gaps` | 检索失败信号：零结果 / 重复查询 / 低分（只读 query_log） |
| `why` | 某条为什么是这个分级（因子分解） |
| `stats` | 计数统计（条目/块/索引规模） |
| `harvest` | 产出侧缺口：积压、薄主题、孤点 |
| `index` | 建/更新向量索引（`--rebuild` 回收孤儿） |
| `lateon` | 晚交互重排缓存（可选功能，默认不启用） |
| `weights` | 源权重：按反馈淘汰低质来源 |
| `retier` | 重新分级（`--apply` 才写库） |
| `selfheal` | 自愈：发现 → 计划 → 执行 → **复测**（默认只跑可逆动作） |

**系统与自主性**

| 命令 | 解决什么 |
|:--|:--|
| `status` | **统一状态视图**：整体怎么样、下一步该做哪一件（复用上述四个入口） |
| `budget` | 自主动作的预算与账本：今天它自己做了什么、超没超额度 |
| `gate` | 认识论分层：validated / provisional / reject（默认只读，写库要 `--apply --yes`） |

**个人使用（人的动作，不是系统的）**

| 命令 | 解决什么 |
|:--|:--|
| `question` | 问题池：长期想回答的问题（抓来的内容会自动对照） |
| `pick` | 记一次「采纳」—— 检索后哪条真的有用（唯一的真实反馈） |
| `mark` | 给条目打「重要」标记（影响评分的一维） |
| `annotate` | 「我的思考」批注双向同步 |
| `block` | 屏蔽库（UP / 关键词 / 分区） |
| `blocksuggest` | 屏蔽增强：从行为推断该屏蔽谁 |
| `quiz` | 从库里出题（主动回忆）；`--rate` 评分建立复习记录，`--due` 只出到期的 |
| `rot` | 知识腐烂检查：哪些结论该用新资料复核 |
| `weekly` | 每周简报：没被查过的、空壳、这周做什么 |

### 检索

```bash
python -m bili_digest search "问题"                     # 块粒度，定位到具体某一句
python -m bili_digest search "问题" -g item              # 条目粒度（哪个视频），自动聚合 + 附摘要
python -m bili_digest search "问题" -g item --rerank     # 加晚交互重排（top-1 更高，慢约 5 秒）
python -m bili_digest search "问题" --fast               # 纯全文，不加载模型（1.7s → 0.45s，R@10 略降）
python -m bili_digest search "问题" --since 30d          # 时间过滤
python -m bili_digest search "问题" --deep               # 口语查询改写后再搜（多一次 LLM 调用）
python -m bili_digest search "问题" --audience exclude   # 只要内容本身，不混评论与弹幕
```

`--audience` 是观众反应（评论/弹幕）的总开关：默认 block 粒度含、item 粒度排除，
显式给了就覆盖两边。

**评论不是噪音**（观众常在评论里补充、纠错、给命令），但它的条数比字幕段多一个量级，
会霸占 top-10 过半席位。`exclude` 换来的是更高的块级精度，代价是结果多样性略降。

#### 哪些块进检索池

字幕有两种粒度，**都不进池会互相挤**：

先分清三张表，否则「检索池有多大」会得出三个不同的答案：

| 表 | 是什么 |
|:---|:---|
| `content_block` | **全部块**（含不进池的 subtitle）。回答「库里有什么」 |
| `block_fts` | **FTS5 表**。回答「全文索引里有什么」—— 它可能含出池规则生效前的历史残留 |
| `vec_state` | **向量索引的记账表**。回答「出池后的真实检索池有多大」 |

**「检索池有多大」只认 `vec_state`。** 具体行数用 `kb stats` 现查 —— 不写死在文档里，
因为写死的数字必然过期，而读者分不清「文档旧了」和「库真的变小了」。

各块类型与是否进池：

| 块类型 | 进向量索引 | 理由 |
|:---|:---:|:---|
| `subtitle` | ✗ | **出池**。和 `subtitle_seg` 高度重叠 —— 同一句话的两种粒度，进池会把 seg 挤掉（实测句级占满前排、含它的段几乎不进）。仍留在库里做**时间定位**（`kb expand` 展开、时间戳跳转），只是不参与打分 |
| `subtitle_seg` | ✓ | **正文主力**，按语义边界切段 |
| `comment` | ✓ | 观众补充、纠错、给命令。默认入池，`--audience exclude` 可去掉 |
| `danmaku` | ✓ | 弹幕。同上受 `--audience` 控制 |
| `audience` | ✓ | 观众反应的聚合块 |
| `meta` | ✓ | 标题 + UP + 时长，1 条目 1 块 |
| `summary` / `key_points` / `insights` / `seg_summary` | ✓ | 提炼产出 |
| `annotation` | ✓ | 人工批注 |

排除清单其实有**两处独立定义**，各管一个索引：

```
schema/types.py:210      NO_FTS_KINDS = frozenset({"subtitle"})   → 管【进不进 FTS】
retrieve/rag_core.py:323 _DEFAULT_EXCLUDED = "subtitle"            → 管【进不进向量索引】
```

**两处值恰好相同，但语义不同** —— 前者是 FTS 的黑名单，后者是向量池的环境变量默认值
（可用 `KB_EXCLUDE_KINDS` 覆盖）。所以改一处不会同步另一处：
把 `danmaku` 加进环境变量只影响向量，FTS 照收（`""` 回到全都进池）。

> **⚠ 已知未闭环**：`block_fts` 可能仍留着 `subtitle` 行的历史残留 ——
> 出池规则改了，而 FTS 表没跟着重建。它不影响向量检索，
> 但如果有人直接查 FTS，会拿到已经出池的句子。重建命令是 `kb index --rebuild`。

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

### 先重扫 B站 AI 字幕，再考虑 ASR

库里记着「无字幕」的条目，**过几周可能就有了**。

B站官方公告的原话是「**在稿件对观众发布一段时间后，算法若判定其无字幕，会自动为其添加字幕**」。
所以当天入库记成「无字幕」是正常的 —— 那不是抓取失败，是**抓得太早**。

判定一个条目现在有没有字幕，一个请求就够：

```
GET https://api.bilibili.com/x/player/wbi/v2?bvid=<BV>&cid=<cid>
→ data.subtitle.subtitles[]        每项含 lan / lan_doc / subtitle_url
  lan = "ai-zh"   B站自己的 ASR（中文）
  lan = "zh-CN"   UP 上传的字幕
  空数组          真没有
```

实测（把所有「无字幕」条目逐条重扫一遍）：**约三分之一其实有字幕**，且绝大多数是 `ai-zh`（机器翻译轨）—— 也就是说「没字幕」这个判断本身会假阴性。
`subtitle_url` 返回的 `body[]` 每项有 `from` / `to` / `content`，
正好是 `subtitle_seg` 切段需要的输入 —— 补回来能直接走现有链路。

补完之后重建段落（`--incremental` 只处理「有字幕、无段落」的条目）：

```bash
python -m bili_digest.distill.seg_boundary --build --incremental
python -m bili_digest index            # 段落变了必须重建索引
```

> **默认就保留 `subtitle` 原始行**（2026-11-04 起 `drop_lines` 默认 `False`，此前相反）。
> 而下次全量重建要从那些行里收集字幕 —— 删了就重建不出段落。
>
> 也别用 `--uid` 列一长串条目：Windows 命令行上限 8191 字符，
> 超了会被静默截断成空列表，于是走进全量分支。条目多就用 `--incremental`。

### 补字幕（按需 ASR）

```bash
python -m bili_digest asr --list                 # 有内容但没字幕的条目（按时长升序）
python -m bili_digest asr BVxxxxxxxxxx           # 转一条（默认只转前 30 分钟）
python -m bili_digest asr BVxxxxxxxxxx --dry     # 只看时长/估算/依赖，不下载不转写
python -m bili_digest asr BVxxxxxxxxxx --all     # 整条转（2 小时视频约 20~35 分钟）
```

默认引擎是 **SenseVoice**（`pip install "bili-digest[asr]"`，另需 `yt-dlp` + `ffmpeg`）。
CPU 上约 **3~6 倍实时**，模型 228 MB。相比 whisper small：更快、更小、专有名词识别更准。

想换回 whisper 有两种显式办法（都不是默认）：`engine="whisper"` 或 `KB_ASR_ENGINE=whisper`。

**默认限 30 分钟**，不做批量 —— 一条 2 小时的视频整条转写约 20~35 分钟机时。

转写产出写进 `subtitle_seg`，与实际字幕切出的段落块同一口径，
覆盖等级随之从「无字幕」变成「有字幕」，检索立刻能命中。
注意：转写只是补素材，之前基于「无字幕」提炼出的摘要已经过时，
需要的话再跑一次 `derive --uid ... --force`。

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
提炼覆盖、规模与可读性、抓取失败记录、重排缓存、表示一致性。退出码 0=全绿 / 2=FAIL / 3=WARN（1 是 USAGE，表示命令本身没跑成），
有 FAIL 是 2，可直接当 CI 门禁。

其中「表示一致性」查的是**派生的展示字段有没有真的写出来**：

```
播放量     extras 里 play / view 两个键名各自的覆盖率
标签       tag 表覆盖多少个条目
提示词片段 产出文件里的「条目 N」与库的实际值差多少
```

判据是**比例**而不是布尔：任一派生字段的「写出篇数 / 该有篇数」低于 90% 就 WARN。
这类缺失不会让任何命令报错，只有比例能发现。

### 图谱怎么用

> **它是候选图，不是知识图谱。** 边由 `suggest_related` 按语义相似度生成 ——
> 只代表「这两条的文字够像」，不代表「它们有关系」这个判断。

| 想看什么 | Graph 的 Search 框 | 代价 |
|:---|:---|:---|
| **笔记之间真实的相似关系** | `-path:"_主题" -path:_全部标签` | 只含笔记-笔记边，2 跳很小（少了后一项会把那个超级节点放进来） |
| **主题骨架** | `path:_主题` | 只含 MOC，边数 = 相关主题条数 |
| 全局 | 空 | **只当健康检查，不要拿它读结构** |

> **`graph_config.py` 写的配置不一定是 vault 里正在用的。** 实测当前 `_vault/.obsidian/graph.json`
> 的 `search` 是 `path:Bվ`（**「站」被写成了乱码**），`repelStrength=20`/`centerStrength=0`/`scale=0.020`，
> 而 `graph_config.py` 会写三档筛选式。**所以上面那三个式子在这个 vault 里一个都没在用** ——
> 要用就在 Graph 的 Search 框里手敲，或者重跑 `graph_config`。

**导航用局部图谱**（命令面板 → `Open local graph`），不是全局图。
全局图里有几个巨型 MOC 会把不相干的笔记连成一片 —— 2 跳指数扩张，
而屏幕上能画下的点数有物理上限，**那不是「调参数」能解决的**。

全局图该看的是结构性异常，不是结构本身：孤立点、超大 MOC（那些其实是兜底桶）、
以及连通分量（只有一个主分量 = 任何一篇都能走到任何一篇）。

### 图谱与浏览

```bash
python -m bili_digest graph --build                    # 建边（同 UP / 共享主题 / 双链）
python -m bili_digest browse --topic 检索增强生成       # 按主题浏览
python -m bili_digest trends --hot 30                  # 最近哪些话题被多个 UP 在讲
python -m bili_digest highlights <uid>                 # 哪段值得看（弹幕密度）
python -m bili_digest popularity --by play --top 10    # 哪个更值得看
```

`highlights` 用弹幕密度当「值不值得看」的信号 —— 观众在哪一刻发弹幕，就是在标注那一刻。

### 导出到 Obsidian

```bash
python -m bili_digest note --vault D:\\Obsidian --uid bilibili:BVxxxxxxxxxx
```

一篇笔记的段落顺序是**结论先行**（按「读到它的人需要什么」排，不按生成顺序）：

```
frontmatter      uid · title · author · duration · views · gen_* · evidence_basis

## 摘要          一句话结论

## 知识要点      实测 12 条，每条带引文（无来源块时标 〔推〕）

## 章节          5~9 个时间戳 + 一句代表话（有正文的条目才有）

## 洞察

## 证据          （高赞评论是没有证据时的 fallback，段名不是「高赞评论」）

## 相关笔记      2 条 wikilink

## 复习          自测题（默认折叠）—— 放在最后，读过的人再测

> [!note]- 我的思考   留白，给人写（是折叠 callout，不是二级标题）
```

`## 章节` 按相邻字幕段的**语义相似度骤降**切分（不是硬切 5 分钟），每组取最长的一段当代表句。
实测《一小时速通计算机网络》切成 8 章、间隔 9 分钟：分层模型 / 协议栈 / 滑动窗口 / CIDR / UDP / TCP 挥手 ——
顺序和一门课几乎一致。纯计算，零 LLM 调用。

三处顺序是按「读到它的人需要什么」定的，不是按生成顺序：

- **摘要在最前** —— 结论先行。
- **复习在最后** —— 「复习」按字面发生在读过之后，放前面等于让第一次打开的人先被考。
- **不写生成时间和页脚预算** —— 判据是：把这个字段的值抄到另一台机器上，它还有意义吗？
  「提炼于 2026-10-31」换台机器就是废信息。

vault 里还有一个 `_全部标签.md`：MOC 只收「成员 >= 3」的主题，其余按首字分组列出全部主题名。

**而它没有把主题变成可达的。** 实测：1,701 条链接里 **1,598 条是红链**（93.9%），
真能点开 103 条 —— 相对「只有 MOC 收的才可达」那一档（101 条）多 **2 个**。
原因是结构性的：它列的是库里的全部主题词（1,789 个），而 `_主题/` 只放成员 >= 3 的那 104 个文件，
剩下 **1,598 个主题天然没有落点**。

**给了名字，没给节点。** 要真解决得给那一千多个单/双成员主题各建一个文件，
而那会产出一堆只连一条线的孤儿节点 —— 那两条路互斥，当前选了「不建文件、只列名字」。

`_全部标签.md` **也没有生成器**（全项目没有任何代码写它），所以它不可能和库同步：
实测 9 处漂移（3 个 ★ 指向被 `_EXCLUDE` 排除的主题、6 个主题漂移或缺失）。

<p align="right">(<a href="#readme-top">回到顶部</a>)</p>

<a id="how"></a>
## 🔬 它是怎么工作的

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

<p align="right">(<a href="#readme-top">回到顶部</a>)</p>

<a id="loop"></a>
## 🔄 闭环：产出反过来决定抓什么

大部分工具是单向的：抓 → 提炼 → 存，存下来的不会再说话。这一层是反过来的那半。

```bash
kb question --add "我长期想搞清的一个问题"   # 登记长期问题
kb question --from-log                     # 从查询记录里发现「我反复在问什么」
kb harvest                                 # 从已有产出的结构反推：还缺什么
kb clues                                   # 从内容里提到的 BV 反推：你从没收藏但反复被指向的
kb selfheal                                # 体检发现的问题 → 生成动作 → 执行 → 复验
kb purge --up "某个UP名"                    # 按 UP 彻底清除：本地 6 张表 + 笔记 + vec_state 记账
kb purge --up "某个UP名" --apply            # 真删（先备份 DB）
kb purge --bv BV1xx --apply --remote --media-id 3177807922   # 连 B站收藏一起取消
```

`kb purge` 默认 dry，只报数：会用**「就删这些行」**的形式列出 item / content_block /
block_fts / vec_state / collection_member / lifecycle / community 各多少行，以及按 BV 号
匹配到的笔记文件数。**按 BV 匹配文件名而不是按标题**——文件名格式是「标题 (BV号)」，
拿标题去比永远匹配不到（这个坑踩过一次）。

`--remote` 取消 B站收藏要三段：`bvid` → `x/web-interface/view` 拿 `aid` →
`x/v3/fav/resource/deal` 传 `rid=aid` + `del_media_ids=<收藏夹 id>`。
**`rid` 必须是数字 aid，传 BV 返回 `-400 请求错误`**（看起来像「B站不让操作」，
其实是参数类型错）。

登记的问题会影响抓取顺序（`batch_refine --order value` 优先抓与问题相关的那批），
抓完再自动对照一遍，告诉你这批里有没有在回答你的问题。

`kb clues` 与 `kb harvest` 的区别值得说清：harvest 从**产出**反推（哪些主题块太薄），
clues 从**内容里提到的 BV 号**反推（评论、简介、字幕里出现的、但你从没收藏的）。
后者按来源分层——**第 1 层是 UP 自己在简介里引用的**（一部翻唱引的原曲、一期教程引的配套课），
这类引用天然是单次的，所以判据不能按「被提到几次」算。实测那些「不在库的 BV」里，
UP 主动引用的、和观众评论里顺口一提的混在一起 —— 混在一起等于没排。

**判断标准是「值得不值得」，不是「完不完整」**：`harvest` 会明确说出哪个缺口不该补 ——
补齐一个你不关注的薄主题，只是让图谱好看。

<p align="right">(<a href="#readme-top">回到顶部</a>)</p>

<a id="mcp"></a>
## 🔌 作为 MCP 服务

`python -m bili_digest.mcp.server` 让 AI 助手直接查这个库，不用你自己复制粘贴。

它给的不是「搜索框」，是**可引用的检索**：命中结果带 block id 与时间戳，
agent 可以先 `kb_search` 找、`kb_expand` 展开前后文、`kb_item` 取整条提炼、
`kb_related` 顺着图走，最后 `kb_annotate` 把结论写回笔记的「我的思考」。
一句话：让 agent 回答时能指认「这句话出自哪条视频的第几分钟」，而不是给一段无法核对的转述。

配置：

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
| `kb_popularity` | 按播放/点赞/弹幕排序 |
| `kb_stats` | 规模与覆盖统计 |
| `kb_annotate` | 读写「我的思考」批注（省略 text 为只读） |

> **弹幕密度（判「在叫好还是争论」）不在这个表里** —— 它只有 CLI 入口
> （`kb highlights <uid>`），`mcp/server.py` 里那个同名函数没有注册成 MCP 工具。

<p align="right">(<a href="#readme-top">回到顶部</a>)</p>

<a id="sources"></a>
## 📊 数据来源

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

<p align="right">(<a href="#readme-top">回到顶部</a>)</p>

<a id="license"></a>
## ⚖️ 许可与隐私

MIT，见 [LICENSE](./LICENSE)。

**凭据**由使用者显式提供，程序不扫描磁盘、不落盘、不进版本库。

**数据文件**（`knowledge.db` · `vectors.usearch` · `bili-digest-out/` · 笔记目录）
均不入版本库。
