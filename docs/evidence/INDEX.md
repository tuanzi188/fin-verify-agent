---
status: active
scope: 评测产物
authoritative_for: 哪个读数有效、产物去哪找
read_when: 引用任何 pass@1 / token / 命中率数字前
---

# 评测产物索引与基线登记

> 规则：**只有指向本目录具体文件的数字才算数**。文档里的裸数、只有转述没有产物的
> 读数标 `unverified`。`status=current` 每种口径同时只能有一条。

## A. 当前有效基线（引用这些）

| 口径 | 产物 | 读数 | 绑定条件 |
|---|---|---|---|
| 答案层 pass@1（当前代码） | [finance_answer_report_20261103_rejudged.json](finance_answer_report_20261103_rejudged.json) | **27/42 = 64.3%** | 42 题 dev；`improved`×1；`--reuse-judgments` 零 token 重判；任务卡含 5 题等价表；`tasks_sha256=c376e1c9`；语料 `42597900ed95` |
| 缓存记账 / 成本口径 | [COST_OPTIMIZATION_PLAN_20261104.md](../COST_OPTIMIZATION_PLAN_20261104.md) §2 | 有效输入 = (tokens_in − hit) + 0.1×hit | `CACHE_HIT_PRICE_RATIO=0.1` 是**计价假设**，报告带 `effective_input_is_estimate: true` |
| 网关缓存行为判定 | [gateway_cache_probe_20261104.json](gateway_cache_probe_20261104.json) | H1/H2 均不成立 | 8 次短请求；带 tools 请求 hit 立即可见 |
| 检索层最新快照 | [eval_retrieval_20261103.md](eval_retrieval_20261103.md) | 主集 69.7% / 复杂 50.0% / 压力 33.3% | first_search@5，零模型；重解析后语料；掉点为稀释性挤出 |
| 路由判据 | [ROUTING_POLICY.md](../ROUTING_POLICY.md) | v2 | 无真实问题金标，未报告新准确率 |

**注意**：答案层与检索层命中率不是同一个口径——离线检索层用任务问题原文查询
（`first_search`），答案层用 Agent 改写后的 query，两个数字不可混。

## B. 三套判分读数的来源（最易踩坑）

同一批 42 题 runs（`finance_answer_runs_20261103.json`）有四个数字，全部保留、
各有绑定条件，**引用必须带产物名**：

| 读数 | 来源 | 口径 |
|---|---|---|
| 50%（21/42） | [finance_answer_report_20261103.json](finance_answer_report_20261103.json) | 修正前严格口径；`tasks_file` 指向 `%TEMP%\km_full_tasks.jsonl`（已失效） |
| 61.9%（26/42） | 仅文档转述（[20261103_finance_answer_eval.md](../archive/20261103_finance_answer_eval.md) §4） | `unverified`，**无独立落盘产物** |
| 64.3%（27/42） | [finance_answer_report_20261103_rejudged.json](finance_answer_report_20261103_rejudged.json) | **current**，当前代码重判；比 61.9% 多 FIN-02 一题（blocks 模式下工具路径约束判 pass） |
| 71.4%（30/42） | 文档 §1 标为「judge 要点」 | 与严格口径不同，勿与上面三个并列 |

硬门禁在 50% 与 64.3% 两份报告里均未过（fabrication 2 / false_refusal 1 /
citation_errors 2），**两者都不能当验收结论**。

## C. 复现方式（合并任务卡问题）

旧报告的 `runs_file`/`tasks_file` 指向 `%TEMP%` 下的合并临时文件，
文件消失后报告不可复现。当前 64.3% 报告用的合并任务卡是按 runs 的
`task_id` 顺序，从三套任务卡重建的：

```
backend/evaluation/finance_tasks.jsonl          (主集 30)
backend/evaluation/finance_tasks_complex.jsonl  (复杂 12)
backend/evaluation/finance_tasks_stress.jsonl   (压力 10)
→ 取 runs 里 42 个 task_id 的并集，按原顺序写成一个 JSONL
```

复现命令（在 `backend/` 下，零 LLM 调用）：

```bash
python -m evaluation.score_agent_runs \
  --runs ../docs/evidence/finance_answer_runs_20261103.json \
  --tasks <rebuilt-42-task.jsonl> --corpus finance \
  --reuse-judgments ../docs/evidence/finance_answer_report_20261103.json \
  --out ../docs/evidence/finance_answer_report_20261103_rejudged.json
```

**待办**：让 `run_finance_eval` 直接落盘它实际使用的合并任务卡，
`run_config.tasks_file` 指向仓库内路径，从源头消灭临时文件依赖。

## D. 产物清单

| 文件 | 类型 | 日期 | status | 说明 |
|---|---|---|---|---|
| `agent_baseline_runs3_20261027.json` | runs | 10-27 | historical | 制度库 baseline 30×3 |
| `agent_baseline_report3_20261027.json` | report | 10-27 | historical | 同上判分 |
| `agent_baseline_badcases3_20261027.jsonl` | badcase | 10-27 | historical | |
| `agent_improved_runs3_20261027.json` | runs | 10-27 | historical | 制度库 improved 30×3 |
| `agent_improved_report3_20261027.json` | report | 10-27 | historical | 同上判分 |
| `agent_improved_badcases3_20261027.jsonl` | badcase | 10-27 | historical | |
| `agent_compare3_20261027.json` | compare | 10-27 | historical | 双变体 McNemar，p=0.0625 未达显著 |
| `agent_stability_20261027.json` | stability | 10-27 | historical | |
| `agent_stability_20261027_postfix.json` | stability | 10-27 | historical | 修复后 |
| `eval_phase0_three_layer_20261031.json` / `.md` | 三层报告 | 10-31 | historical | 答案/数值/轨迹/忠实度仍 `no_data` |
| `eval_retrieval_20261101_main/complex/stress.json` + `.md` | 检索层 | 11-01 | superseded | 语料状态不同，读数不代表当前 |
| `eval_retrieval_20261103_main/complex/stress.json` + `.md` | 检索层 | 11-03 | **current** | 重解析后最新快照 |
| `finance_answer_runs_20261103.json` | runs | 11-03 | **current（runs）** | 42 题 dev，唯一答案层 runs |
| `finance_answer_report_20261103.json` | report | 11-03 | historical | 50% 修正前产物 |
| `finance_answer_report_20261103_rejudged.json` | report | 11-04 | **current（答案层）** | 64.3%，含 `rejudge_note` |
| `finance_probe_cache_20261104.json` | probe | 11-04 | historical | **修复前**读数；tool_round 0 命中是客户端漏字段假象，需重跑 |
| `gateway_cache_probe_20261104.json` | probe | 11-04 | **current** | 网关行为判定，8 次短请求 |

## E. 基线登记规则

1. 每个基线绑定 `(corpus_hash, tasks_sha256, git_commit, judge_mode, scoring, cost_basis)`；
   缺任一项标 `incomplete`。
2. `status=current` 每种口径一条；旧条目标 `historical`/`superseded` 并写 `supersedes` 链。
3. **收益不跨基线叠加**：sections→blocks 的机制收益已兑现进 blocks 基线，
   不得在后续优化里再算一遍。
4. 全价 token（预注册 ≤20k 红线口径）与有效输入估算分两列报，不混写「降了 X%」。
5. 32 题 blocks 基线 runs 尚未落盘，`docs/COST_OPTIMIZATION_PLAN_20261104.md` §1
   的对照表暂不可复现；落盘后登记到本表。
