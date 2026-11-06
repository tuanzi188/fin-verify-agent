---
status: active
scope: backend/evaluation
authoritative_for: 评测目录里每个脚本的职责
read_when: 新增或修改评测脚本、找入口前
---

# evaluation/ 目录索引

> 本目录保持**扁平**：模块内部全部使用绝对导入 `evaluation.<module>`，
> 外部有测试、CI、README、前端命令文本依赖这些路径。不要按目录重排，
> 新增脚本时更新本表即可。

## 运行入口（可 `python -m` 调用）

| 文件 | 用途 |
|---|---|
| `run_finance_eval.py` | 金融答案层跑批（真实模型），产出 runs |
| `score_agent_runs.py` | 对 runs 判分，产出 report |
| `release_gate.py` | 发布硬门禁判定 |
| `eval_report.py` | 三层报告汇总 |
| `run_rag_eval.py` | 固定窗 / 结构化检索对照（需本地语料） |

> 结构检索的 CI 回归门禁在 `tests/test_block_retrieval_regression.py`：
> 合成语料 + hash embedding，不依赖模型下载或本地语料。
> 通用 RAG 的 `run_eval.py` / `check_regression.py` / `run_agent_eval.py`
> 与制度库语料链已于 2026-11-06 下线。

## 指标与判定

`retrieval_metrics.py`、`routing_metrics.py`、`agent_metrics.py`、
`agent_checks.py`、`agent_judge.py`、`equivalence.py`、`numeric_audit.py`、
`faithfulness.py`、`failure_taxonomy.py`、`trajectory_eval.py`、
`evidence_flow.py`（定位→读取→引用的证据流转化，零 token，不需要任务卡）、
`citation_anchor.py`、`calc_schema.py`、`agent_report.py`。

## 任务卡与维护

`finance_tasks.jsonl`（主集）、`finance_tasks_complex.jsonl`（复杂）、
`finance_tasks_stress.jsonl`（压力）；
`lint_finance_tasks.py`（金融专项）、`lint_common.py`（与语料无关的公共规则）、
`render_tasks.py`、`rebind_coords.py`、`rebuild_ms11_tasks.py`、`holdout_registry.py`。

## 语料与基线

`finance_corpus.py`（chunk_id → 原文）、`rag_corpus.py`（固定窗，故意丢结构）、
`structured_corpus.py`（结构块，直接复用 `BlockIndex`）、`rag_baseline.py`。

## 基础设施

`_runner_common.py`（跑批公共层，只抽不改）、`provenance.py`（产物版本溯源）。

## legacy/

历史脚本，不进 CI、不被主线 import：`compare_arms.py`（两臂配对比较）、
`check_block_retrieval.py`（Agent 与结构 RAG 的检索一致性核对）、
`experiments/` 下的矩阵实验。用法见 `legacy/README.md`。
