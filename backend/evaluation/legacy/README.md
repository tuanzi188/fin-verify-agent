# legacy：历史评测脚本（只读参考，不再维护）

这里放的是**早期阶段跑出过数字、但已被主线替代**的脚本。归档而不删除的理由很简单：
它们产出的结论还写在 `docs/archive/20261027_agent_eval_project_spec.md` 与 `experiments/RESULTS.md` 里，
删掉脚本，那些数字就只剩一句"当时跑过"。

**规矩**：这个目录不进 CI、不被任何主线模块 import、不随新需求改动。
要用里面的数，先确认它还跑得起来；改它之前先想清楚"是不是该在主线里重做"。

## 清单

| 文件 | 当初做什么 | 被什么替代 |
|---|---|---|
| `run_abstention_eval.py` | 制度库 20 负 + 20 正样本的拒答评测：干净拒答率 / 编造率 / 误拒率 | Agent 评测的「拒答与边界」维度（`agent_tasks.jsonl` + `score_agent_runs`），拒答变成带证据与状态的维度分，而不是独立脚本 |
| `negative_samples.py` | 上面那套的负样本清单（20 条），配套 `NEGATIVE_QUERIES` | 同上；金融侧对应任务是 FIN-13/14/15/16 |
| `experiments/run_retrieval_matrix.py` | 向量 / 关键词 / 混合三路 × top_k 的召回矩阵 | 检索回归夹具的固定基线 `baseline_retrieval.json`（由 `check_regression.py` 守） |
| `experiments/run_rerank_matrix.py` | 加不加 L2 精排的对照矩阵 | 精排已在 `app/services/rerank.py` 定版 |
| `experiments/run_coverage_check.py` | 语料对问题集的覆盖率体检 | 已被能力评测集取代 |
| `experiments/run_failure_analysis.py` | 失败案例自动归类，产出 `FAILURE_CASES.md` | 根因分类搬进了 `evaluation/agent_report.classify_causes` |

`experiments/RESULTS.md` 与 `FAILURE_CASES.md` 是那批实验的结论存档，一并留在这里。

## 怎么跑（需要时）

在 `backend/` 目录下：

```bash
python -m evaluation.legacy.run_abstention_eval --limit 5
python -m evaluation.legacy.experiments.run_retrieval_matrix
```

这些脚本仍然会建 Chroma 索引、调模型，属于**联网 + 烧 token**的操作；
日常回归不要跑它们。
