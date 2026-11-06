# legacy：历史脚本（不进 CI，不被主线 import）

## 已删除（2026-11-06）

通用知识库 RAG 下线时，依赖它的脚本一并移除：
`run_abstention_eval.py`、`experiments/run_retrieval_matrix.py`、
`run_rerank_matrix.py`、`run_coverage_check.py`、`run_failure_analysis.py`、
`negative_samples.py`。

删除理由不是"跑不通"，而是**底层模块已经不存在**：`vector_store` / `keyword_search` /
`hybrid_search` / `graph_search` / `reranker` / `rag_engine` 全部移除，产品只剩
`/finance/*` 一条路径，证据单元是 `app/finance/block_index.py`。

## 仍可手工运行

| 文件 | 用途 | 命令（在 backend 下） |
|---|---|---|
| `compare_arms.py` | Agent 与 RAG 两臂逐题配对，看多花的 token 买到多少质量 | `python -m evaluation.legacy.compare_arms ...` |
| `check_block_retrieval.py` | Agent `search_blocks` 与结构 RAG `search` 是否同序同内容 | `python -m evaluation.legacy.check_block_retrieval --tasks ...` |

两者都需要本地 `data/finance` 语料，不参与 CI。历史结论仍在
`docs/archive/20261027_agent_eval_project_spec.md` 与 `docs/evidence/`，
引用时按 `AGENTS.md` §6 的口径要求标明是历史读数、不可与当前代码相减。

检索层现在的回归门禁是 `tests/test_block_retrieval_regression.py`
（合成语料 + hash embedding，零模型、零下载、进 CI）。
