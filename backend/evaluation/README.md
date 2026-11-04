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
| `check_regression.py` | 检索回归门禁（零模型，CI 使用） |
| `check_block_retrieval.py` | blocks 检索层对照 |
| `release_gate.py` | 发布硬门禁判定 |
| `eval_report.py` | 三层报告汇总 |
| `run_agent_eval.py` / `run_rag_eval.py` / `run_eval.py` | 历史评测入口，保留用于复现 |

## 指标与判定

`metrics.py`、`retrieval_metrics.py`、`routing_metrics.py`、`agent_metrics.py`、
`agent_checks.py`、`agent_judge.py`、`equivalence.py`、`numeric_audit.py`、
`faithfulness.py`、`failure_taxonomy.py`、`trajectory_eval.py`、
`citation_anchor.py`、`calc_schema.py`、`agent_report.py`。

## 任务卡与维护

`finance_tasks.jsonl`（主集）、`finance_tasks_complex.jsonl`（复杂）、
`finance_tasks_stress.jsonl`（压力）、`agent_tasks*.jsonl`；
`lint_finance_tasks.py`、`lint_agent_tasks.py`、`render_tasks.py`、
`rebind_coords.py`、`rebuild_ms11_tasks.py`、`holdout_registry.py`。

## 语料与基线

`finance_corpus.py`、`agent_corpus.py`、`rag_corpus.py`、`structured_corpus.py`、
`sample_data.py`、`rag_baseline.py`、`baseline_retrieval.json`。

## 基础设施

`_runner_common.py`（跑批公共层，只抽不改）、`provenance.py`（产物版本溯源）。

## legacy/

早期跑出过数字、已被主线取代的脚本。**只读参考，不进 CI，不被主线 import，
不随新需求改动**；`legacy/experiments/out/` 是实验存档（已忽略）。
