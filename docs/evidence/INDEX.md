---
status: active
scope: 评测产物
authoritative_for: 哪个读数有效、产物去哪找
read_when: 引用任何 pass@1 / token / 命中率数字前
---

# 评测产物索引与基线登记

> 规则：**只有指向本目录具体文件的数字才算数**。文档里的裸数、只有转述没有产物的
> 读数标 `unverified`。`status=current` 每种口径同时只能有一条。

## A. 可引用基线（引用这些，但看清绑定条件）

> **重要（2026-11-04 更新）**：答案层 64.3% 绑定 2026-11-03 的 runs（`221318d`）、
> `sections` 模式、42 题合并卡与 `--reuse-judgments` 零 token 重判，**不能代表当前代码**。
> 11 题 blocks 子集复测已落盘（`finance_ms11_20261104.json` +
> `finance_ms11_report_20261104.json`，见 §D），产出一份**绑定当前代码 `d9707b1` 的新剖面**。
> 但两者 `corpus_hash` 虽一致，`search_mode` / 任务卡 / judge 模式 / commit 均不同，
> **不是严格 A/B**：64.3% 标 `unique（旧代码基线）` 保留、不删不回改；
> 引用新剖面时不得写成「从 2/11 提升到 7/11」或「pass@1 提升 X 个百分点」。
> 新剖面也是 `runs_per_task=1` 的单次能力剖面，无 pass^k，不外推全量验收。
>
> **再更新（2026-11-04 逐题修复）**：该 11 题报告的 4 条 badcase 已逐个定位根因并
> 修复代码（见 `docs/CURRENT.md`「逐题修复」小节），但**尚未重跑真模型**——
> 这份报告仍是**修复前**的读数，不能当作修复效果。零模型重判确定性检查只确认了
> FIN-11 / FIN-S06 的 `tool_call_count` 假失败可消除；pass@1 与 token 的实际变化
> 必须等同一 11 题卡、同一 judge、仅 commit 不同的 A/B 复测落盘后登记到 §D。

| 口径 | 产物 | 读数 | 绑定条件 |
|---|---|---|---|
| 答案层 pass@1（**旧代码基线**，非当前代码） | [finance_answer_report_20261103_rejudged.json](finance_answer_report_20261103_rejudged.json) | **27/42 = 64.3%** | 42 题 dev；绑定 `221318d` 的 runs（`sections`）；`--reuse-judgments` 零 token 重判；任务卡含 5 题等价表；`tasks_sha256=c376e1c9`；语料 `42597900ed95`。**只代表改动前代码** |
| 多步 11 题剖面（**当前代码**，非全量验收） | [finance_ms11_report_20261104.json](finance_ms11_report_20261104.json) | **7/11 pass@1**，硬红线全 0 | 11 题 dev 子集；`blocks`；`tasks_sha256=6f514608`；真实 judge；runs `run_config.git=d9707b1`（`dirty=true`）；语料 `42597900ed95`。**单次、不同条件，不与 64.3% 相减** |
| 缓存记账 / 成本口径 | [COST_OPTIMIZATION_PLAN_20261104.md](../COST_OPTIMIZATION_PLAN_20261104.md) §2 | 有效输入 = (tokens_in − hit) + 0.1×hit | `CACHE_HIT_PRICE_RATIO=0.1` 是**计价假设**，报告带 `effective_input_is_estimate: true` |
| 工具循环缓存命中（修复后实测） | [finance_ms11_20261104.json](finance_ms11_20261104.json) | 全价命中占比 65.3%，有效输入估算 226,093 | 11 题逐题 `hit+miss==tokens_in`；合计 588,011 token 未超 ≤1.2M 预算；**估算非网关实测** |
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
| 64.3%（27/42） | [finance_answer_report_20261103_rejudged.json](finance_answer_report_20261103_rejudged.json) | 当前**判分口径**重判、但绑定旧 runs（`221318d`），非当前代码；比 61.9% 多 FIN-02 一题（blocks 模式下工具路径约束判 pass） |
| 71.4%（30/42） | 文档 §1 标为「judge 要点」 | 与严格口径不同，勿与上面三个并列 |

硬门禁在 50% 与 64.3% 两份报告里均未过（fabrication 2 / false_refusal 1 /
citation_errors 2），**两者都不能当验收结论**。

**另有一份不是同一批 runs 的剖面**：`finance_ms11_report_20261104.json` 的 7/11
是 11 题 `blocks` 子集、真实 judge、新代码（`d9707b1`）的单次结果，硬红线
fabrication / false_refusal / citation_errors / crashes 全为 0、数值幻觉率 0.0%。
它与上表四个数字**不同语料状态以外的绑定条件也全不同**（见 §A 告警），
不要并进同一张表算「提升」，也不要拿 11 题子集的全绿去宣称 42 题红线已过。

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

**已解决（新产物）**：`run_finance_eval` 现在默认把筛选后的实际任务集落盘为
`<out>.tasks.jsonl`（与 runs 同目录），`run_config.tasks_file` 指向它，
并新增 `tasks_source_file` / `tasks_selection` / `task_snapshot` 三个溯源字段。
正式产物落 `docs/evidence/` 时快照自动进 git；临时探针写 `%TEMP%` 不污染仓库。
可用 `--no-task-snapshot` 关闭（不推荐）。回归测试：
`backend/tests/test_run_finance_eval_snapshot.py`。

**历史产物仍需重建**：上面的 42 题合并卡是 2026-11-03 旧报告留下的，
不受新逻辑追溯影响；复现方式保留在本节。

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
| `finance_answer_runs_20261103.json` | runs | 11-03 | **unique（历史，绑定 `221318d`）** | 42 题 dev，唯一答案层 runs；**不能代表当前代码**，见 §A 上方告警 |
| `finance_answer_report_20261103.json` | report | 11-03 | historical | 50% 修正前产物 |
| `finance_answer_report_20261103_rejudged.json` | report | 11-04 | **unique（旧代码基线）** | 64.3%，含 `rejudge_note`；绑定 `221318d` 的 runs，**不代表当前代码** |
| `finance_probe_cache_20261104.json` | probe | 11-04 | historical | **修复前**读数；tool_round 0 命中是客户端漏字段假象，需重跑 |
| `gateway_cache_probe_20261104.json` | probe | 11-04 | **current** | 网关行为判定，8 次短请求 |
| `ms11_tasks_20261104.jsonl` | tasks | 11-04 | **current（复测输入）** | 11 道多步题合并输入卡，由 `evaluation/rebuild_ms11_tasks.py` 生成；供 blocks 子集复测用 |
| `finance_ms11_20261104.json` | runs | 11-04 | **current（11 题剖面）** | 11 题全 `answered`；`run_config.git=d9707b1`（`dirty=true`）；逐题 `hit+miss==tokens_in`；合计 588,011 token |
| `finance_ms11_20261104.tasks.jsonl` | tasks snapshot | 11-04 | current | 随 runs 自动落盘的实际任务集快照，`run_config.tasks_file` 指向它 |
| `finance_ms11_report_20261104.json` | report | 11-04 | **current（11 题剖面）** | `pass@1=7/11`、真实 judge、硬红线全 0；**单次、不同条件，不与 64.3% 相减** |
| `finance_ms11_badcases_20261104.jsonl` | badcase | 11-04 | current | 4 条 badcase，主因「参数错误 / 检索无命中」 |

## E. 基线登记规则

1. 每个基线绑定 `(corpus_hash, tasks_sha256, git_commit, judge_mode, scoring, cost_basis)`；
   缺任一项标 `incomplete`。
2. `status=current` 每种口径一条；旧条目标 `historical`/`superseded` 并写 `supersedes` 链。
   **没有绑定当前代码的产物时，不要硬标 `current`**：用 `unique（旧基线）` 并写明
   绑定的 commit。产物清单的 `run_config.git`（新产物自带）是判断依据；
   旧产物看 `manifest.code.commit`。2026-11-04 的 11 题 runs 就靠这个字段判定
   绑定 `d9707b1`，而 11-03 旧 runs 缺该字段、只能靠 `manifest.code.commit=221318d`。
   `dirty=true` 也要照记：说明运行时工作区有未跟踪产物，不是干净 checkout。
3. **收益不跨基线叠加**：sections→blocks 的机制收益已兑现进 blocks 基线，
   不得在后续优化里再算一遍。
4. 全价 token（预注册 ≤20k 红线口径）与有效输入估算分两列报，不混写「降了 X%」。
5. 32 题 blocks 基线 runs 尚未落盘，`docs/COST_OPTIMIZATION_PLAN_20261104.md` §1
   的对照表暂不可复现；落盘后登记到本表。
