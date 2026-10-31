# tools/ —— 标定、评估与实验

这里的脚本**定位不同，不要当成同一种东西**。判据是「有没有调用点」：
`grep -rn "import <名字>" --include=*.py .`

## 一、被模块 import（运行时依赖）

| 文件 | 调用点数 | 作用 |
|:---|:---|:---|
| `build_kb_index.py` | 12 | 建/更新向量索引与 FTS —— 检索链的末端 |
| `rewrite_eval.py` | 7 | 查询改写评估 |
| `stats_test.py` | 6 | 显著性检验：Wilcoxon / 配对 t / 效应量 / 可检测下限 |
| `ablate.py` | 4 | 消融实验 |
| `assess.py` | 4 | 素材充分性评估 |
| `ledger.py` | 3 | 失败账本：试过、有据、被否掉的方向 |
| `calibrate.py` | 2 | 标定「库里有没有」的概率阈值 |
| `envelope.py` | 2 | 信封背面计算（用估算代替实测） |
| `eval_retrieval.py` | 2 | 检索质量评估（R@k / MRR / nDCG） |
| `experiment.py` | 2 | 实验编排 |
| `bench3.py` | 1 | 基准测试 |
| `embed_onnx.py` | 1 | ONNX 嵌入 |

## 二、独立工具（直接跑，不被 import）

| 文件 | 作用 |
|:---|:---|
| `smoke_cli.py` | 子命令冒烟。判据是**输出特征**而不是退出码 —— 只看退出码会漏掉「exit 0 但正文为空」那类静默失效 |
| `tune.py` | 用交叉验证搜查询期参数（`RRF_K` / `SHORT_PENALTY` / `CANDIDATE_K` / `LATEON_DEPTH`），替代手工拍数。`python tools/tune.py --folds 5 --metric mrr` |

## 三、一次性实验（跑过一次，结论写在文件头）

这些**没有调用点，也不该接调用点** —— 它们的产出是文件头那段结论，
不是可复用的函数。留着是为了「下次想重做同一个实验时不用从头想」。

| 文件 | 结论 |
|:---|:---|
| `h1_granularity.py` | 语义分段 vs 段摘要：方向一致但不显著。fair set 32 条，seg 档 R@10 62.5% / MRR 0.5349；seg_summary 档 46.9% / 0.4479，token 差 5.7 倍 |
| `h2_topk.py` | `top_k=8` 是性价比拐点。口语 4→8 +12.5pt、8→16 +2.5pt（token 翻倍）、16→32 +0.0pt；术语 8→16 +0.0pt |
| `bestfit.py` | LLM 偏好重排（BestFit，arXiv 2509.16112），方法已落地 |
| `bor.py` | Bits-over-Random：给 R@k 做机会校正（arXiv 2605.24660） |
| `flips.py` | 升级后哪些原本对的查询变错了（negative flips，arXiv 2610.07266） |
| `invariants.py` | 检索的不变量测试 / 变形关系（arXiv 2402.14480） |
| `query_corpus.py` | 从库内真人评论造查询，零 LLM 成本（要压到 ±2pt 需约 2000 条） |

## 为什么留着零引用的文件

项目撞过这个坑：`quality/relevance.py` 三个函数从未执行过；`bump_usage` /
`annotate_hits` / `build_same_batch` 写了但零调用点。所以「零引用」要**逐个判断性质**：

- 该接没接（bug）→ 接上
- 设计上可选（如 `boosted_score` 默认不启用）→ 标注一句
- 一次性实验的落点 → 留在本目录第三节，结论写进文件头

一律当 bug 删掉会丢结论，一律当正常会漏 bug。判据是**文件头有没有结论**。
