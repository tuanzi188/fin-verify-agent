# 评测升级实施记录：RAG 评价 + Agent 测评

> 对应方案：`docs/EVAL_UPGRADE_PLAN.md`（原 `D:\a工作空间\KnowledgeMind_评测升级方案_RAG评价与Agent测评.md`）
> 实施日期：2026-10-31
> 状态：Phase 0 已落地并产出真实离线读数；Phase 1/2 的确定性代码与测试已落地，真实模型调用（忠实度与答案层探针）待预算批准。

---

## 0. 一页结论

1. **检索层有了指标**：HitRate@K / MRR / context precision / context recall / read hit，
   双臂同尺（`first_search`）与 Agent 诊断口径（`any_search`）分开报。
2. **路由层有了指标**：任务结构金标 vs router 选臂，准确率、混淆矩阵、Macro-F1、错分代价。
3. **答案层口径升级**：pass@1 / pass@k / pass^k、均值 ± 标准差 + P95、flaky 任务；
   答案等价 rubric 进 judge prompt；失败自动归类 8 类。
4. **闸门层已建**：五类 Release Gate，金融两条红线（数值幻觉率 = 0、误拒率 ≤ 阈值），
   缺数据记 `no_data` / `insufficient_data`，不拿 0 顶替。
5. **Phase 0 真实读数（零模型调用）**：结构检索在复杂集要点命中 69.8% vs 固定窗 41.9%；
   原始路由口径准确率 80.0%（24/30），Macro-F1 0.775。答案层暂无真实 runs，闸门未评估。

题集元数据收紧后，路由报告改为：明确目标题 21/21（100%）单独计分，6 条“可回答但没有明确更优臂”的边界题单列，
拒答/澄清/恢复题继续排除。这个 100% 不能与原始 80% 拼接，只用于说明边界题已从准确率分母移出。

---

## 1. 模块地图

| 文件 | 职责 | 模型调用 |
|---|---|---|
| `backend/evaluation/retrieval_metrics.py` | 检索段 HitRate@K / MRR / precision / recall / read hit；离线一次检索与 runs 打分两种入口 | 0 |
| `backend/evaluation/routing_metrics.py` | router 选臂 vs 任务结构金标；混淆矩阵 / Macro-F1 / 错分代价；也支持 compare_arms 经验金标 | 0 |
| `backend/evaluation/equivalence.py` | 答案等价：千分位、百分号、括号负数、元/千元/万元/亿元、舍入容差；rubric 文本 | 0 |
| `backend/evaluation/numeric_audit.py` | 金标数值覆盖率、数值幻觉事件、未溯源数值 | 0 |
| `backend/evaluation/failure_taxonomy.py` | 8 类失败自动归类（模型异常/路由/检索/工具/循环/误拒/污染/越界） | 0 |
| `backend/evaluation/trajectory_eval.py` | 按序匹配 / 关键工具 / 无效循环 | 0 |
| `backend/evaluation/faithfulness.py` | RAGAS 忠实度（拆陈述 + 判推断） | 2 次/运行 |
| `backend/evaluation/release_gate.py` | 五类闸门 + 预注册阈值 + 红线 | 0 |
| `backend/evaluation/eval_report.py` | 三层报告合成（JSON + Markdown）与失败分岔诊断 | 0 |
| `backend/evaluation/agent_metrics.py` | 新增 `continuous_summary`（均值/标准差/中位/P95） | 0 |
| `backend/evaluation/agent_report.py` | 新增 pass@k、flaky、连续型分布、检索/轨迹/数值/归类汇总 | 0 |
| `backend/evaluation/score_agent_runs.py` | 判分时自动附加检索/轨迹/数值/归类/路由信息 | 0 |
| `backend/scripts/extend_finance_task_schema.py` | 给任务集补路由目标、预算和题型元数据 | 0 |

---

## 2. 命令

### 2.1 检索层（零模型调用）

```bash
cd backend

# 复杂集：结构感知 RAG vs 固定窗，一次检索、同 K
python -m evaluation.retrieval_metrics \
  --tasks evaluation/finance_tasks_complex.jsonl \
  --arm both --top-k 5 --out %TEMP%\retrieval_complex.json

# 主集 / 压力集同理
python -m evaluation.retrieval_metrics --tasks evaluation/finance_tasks.jsonl --arm both --top-k 5
python -m evaluation.retrieval_metrics --tasks evaluation/finance_tasks_stress.jsonl --arm both --top-k 5

# 有真实 runs 时，按 Agent 实际 trace 打分（含多次检索）
python -m evaluation.retrieval_metrics --tasks evaluation/finance_tasks_complex.jsonl \
  --arm agent --runs %TEMP%\fin_runs.json
```

口径：

- `first_search`：只看第一次检索调用，**双臂同尺**的主口径；
- `any_search`：Agent 所有检索的并集、每要点取最好排名，**只作诊断**，不能和一次检索的 RAG 直接比；
- `read hit`：金标位置是否被 `read_block/read_section/get_table/get_rows/get_cell` 真正读开。

### 2.2 路由层（零模型调用）

```bash
# 三套任务合并审计（逗号分隔）
python -m evaluation.routing_metrics \
  --tasks "evaluation/finance_tasks.jsonl,evaluation/finance_tasks_complex.jsonl,evaluation/finance_tasks_stress.jsonl" \
  --out %TEMP%\routing_all.json

# 有 compare_arms 结果时，用逐题实测胜负做经验金标
python -m evaluation.routing_metrics --tasks evaluation/finance_tasks_complex.jsonl \
  --compare %TEMP%\compare_arms.json
```

金标只看任务结构：`multi_hop/complex → agent`，`single_hop/no_clarify_control → structured_rag`；
拒答/澄清/假设/单位陷阱/错误前提/恢复（mock）不判更优臂，只报分流分布。

### 2.3 判分（答案层 + 新层自动附加）

```bash
python -m evaluation.score_agent_runs --runs %TEMP%\fin_runs.json \
  --tasks evaluation/finance_tasks_complex.jsonl --corpus finance \
  --out %TEMP%\fin_report.json
```

判分报告新增：

- `retrieval_layer`：检索段汇总（first_search / any_search / read / 检索次数）；
- `trajectory_layer`：按序匹配 / 关键工具 / 无效循环；
- `numeric_layer`：金标覆盖率 / 数值幻觉率 / 未溯源数值率；
- `failure_taxonomy`：8 类主因与共现；
- 每条 verdict 附 `retrieval` / `trajectory` / `numeric` / `taxonomy` / `routing`。

### 2.4 数值审计 / 轨迹 / 忠实度

```bash
python -m evaluation.numeric_audit --runs %TEMP%\fin_runs.json \
  --tasks evaluation/finance_tasks_complex.jsonl --report %TEMP%\fin_report.json

python -m evaluation.trajectory_eval --runs %TEMP%\fin_runs.json \
  --tasks evaluation/finance_tasks_complex.jsonl

# 忠实度：2 次轻调用/运行；只在探针级并跑
python -m evaluation.faithfulness --runs %TEMP%\fin_runs.json --limit 4
```

### 2.5 Release Gate 与三层报告

```bash
python -m evaluation.release_gate \
  --report %TEMP%\fin_report.json \
  --retrieval %TEMP%\retrieval_complex.json --retrieval-arm agent \
  --routing %TEMP%\routing_all.json \
  --numeric %TEMP%\numeric.json --trajectory %TEMP%\trajectory.json \
  --faithfulness %TEMP%\faithfulness.json \
  --out %TEMP%\gate.json

python -m evaluation.eval_report \
  --retrieval %TEMP%\retrieval_complex.json \
  --routing %TEMP%\routing_all.json \
  --answer-report %TEMP%\fin_report.json \
  --numeric %TEMP%\numeric.json --trajectory %TEMP%\trajectory.json \
  --faithfulness %TEMP%\faithfulness.json \
  --out-json %TEMP%\eval_report.json --out-md %TEMP%\eval_report.md
```

---

## 3. Phase 0 真实读数（2026-10-31，零模型调用）

### 3.1 检索层（top-k=5，一次检索，问题原文查询）

| 题集 | 臂 | 要点命中@5 | 整题全中@5 | MRR | context precision@5 |
|---|---|---:|---:|---:|---:|
| 主集（19 条有金标位置） | 结构检索 | **29/37 = 78.4%** | 15/19 = 78.9% | 0.487 | 20.0% |
| 主集（19 条有金标位置） | 固定窗 | 23/37 = 62.2% | 13/19 = 68.4% | 0.473 | 16.8% |
| 复杂集（9 条有金标位置） | 结构检索 | **30/43 = 69.8%** | 3/9 = 33.3% | 0.496 | 17.8% |
| 复杂集（9 条有金标位置） | 固定窗 | 18/43 = 41.9% | 1/9 = 11.1% | 0.200 | 11.1% |
| 压力集（10 条） | 结构检索 | **8/18 = 44.4%** | 5/10 = 50.0% | 0.358 | 12.0% |
| 压力集（10 条） | 固定窗 | 6/18 = 33.3% | 4/10 = 40.0% | 0.153 | 12.0% |

> 纯拒答/纯定性题没有金标位置，**不进检索分母**（主集 3 条拒答 + 2 条澄清、
> 复杂集 3 条拒答）。把它们记成“整题未命中”会把拒答/澄清题的错误算到检索头上。

读数要点：

1. **结构切片在 K=5 上全面优于固定窗**，复杂集差距最大（+27.9pp 要点命中，
   +22.2pp 整题全中）。这给“切片/表格结构本身值多少”提供了第一个独立于答案层的证据。
2. **主集上固定窗在 K=1 反而更高**（35.1% vs 24.3%）：结构块的头部排序不如固定窗集中，
   优势在召回不在头名。这意味着“把 top-1 做得更准”和“把 top-5 的召回做全”是两个不同优化方向。
3. **context precision 很低（11%–20%）**：top-5 里大多数块不是金标位置。复杂题需要跨表/跨文档，
   一个要点有多个可接受位置，这个数会被低估；但它仍然说明**检索结果里噪声占比高**，
   是成本（把无关块喂给模型）和注意力（要点被淹没）的双重问题。
4. **read hit 在离线模式下为 0** 是预期的：离线只做一次检索，没有读取动作。
   真实 runs 上这个数才有意义（区分“检到”与“读了”）。

### 3.2 路由层（三套任务合并，30 条可判）

- 准确率 **24/30 = 80.0%**，Wilson [62.7%, 90.5%]，Macro-F1 **0.775**。
  默认排除 2 条留出任务；含留出时 **25/32 = 78.1%**（差异来自 FIN-05/FIN-10 两条留出题）。
- 混淆矩阵（行=金标，列=router）：结构→结构 7、结构→Agent 5；Agent→结构 1、Agent→Agent 17。
- 错分代价：**简单题误走 Agent 5 条**（成本浪费），**复杂题误走结构检索 1 条**（质量风险）。
- 16 条不参与打分（拒答/澄清/假设/单位陷阱/错误前提/恢复 mock），实际分流：结构 10 / Agent 6。

错分根因（全部来自 router 现有规则，不是新 bug）：

| 任务 | 金标 | 实际 | router 理由 | 根因 |
|---|---|---|---|---|
| FIN-01/02/03 | 结构 | Agent | 问题包含多个子问题 | 两个问号但都指向同一张表；年报已披露同比，不需要多步 |
| FIN-S04 | 结构 | Agent | 命中“占营业” | 占比可能是表内已披露值，不等于要计算 |
| FIN-S05 | 结构 | Agent | 命中“同比” | 同上：同比可能是披露值 |
| FIN-S03 | Agent | 结构 | 单公司/单期间/单指标 | “亿元换算”没有触发信号 |

**结论**：router 的关键词表把“披露值”误当成“需要计算”。下一步应把信号改成
“是否必须跨表/跨文档/调用计算器”，而不是“问题里有没有同比/占比字样”。
本轮只做度量，不改产品路由——避免把度量与优化混在一轮里。

### 3.3 闸门层

没有真实答案层 runs，`release_gate` 返回 `status=insufficient_data`，
`overall_pass=null`。这是设计行为：**缺数据不等于通过**。
拿到答案层报告后，五类闸门才会给出可拦截的通过/不通过。

---

## 4. Phase 1 / 2 落地内容

### 4.1 答案等价 rubric

- 文档：`docs/ANSWER_EQUIVALENCE_RUBRIC.md`；
- 代码：`evaluation/equivalence.py`（容差、单位、方向、四条原子判据）；
- judge prompt 已拼入 `RUBRIC_TEXT`，judge 与代码用同一套口径；
- 数值审计用同一套 `value_in_text`，避免“judge 说对、代码说错”的两套尺子。

### 4.2 失败自动归类

`evaluation/failure_taxonomy.py` 八类，按 trace/checks/retrieval/routing 判：
模型异常、路由错误、检索无命中、工具/参数错误、无效循环、过早结束/误拒、
上下文污染、权限越界。报告同时给主因（固定优先级）与共现。

### 4.3 Case Schema v2

新增可选字段（`lint_finance_tasks` 校验类型/取值）：

- `route_target`：`agent` / `structured_rag` / `indeterminate`；旧 `expected_arm` 只兼容历史报告；
- `expected_source`：`{kind: table|section, id}` 或数组；
- `allowed_tools` / `forbidden_actions`：轨迹层判越界；
- `max_steps` / `token_budget`：逐 case 过程与成本上限；
- `final_state`：`numeric|text|abstain|cites`；
- `required_sequence` / `key_tools`：轨迹按序匹配与关键工具。

任务集题型元数据还记录 `retrieval_hops`、`answer_operations` 与 `prompt_style`：
`single_hop` 表示一个证据单元足够，不表示答案只能有一个字段；带直接换算、验算或页码要求的题
与自然提问分开报告，避免把题面提示误读成开放场景能力。

复杂集现为 8 条开发题 + 4 条冻结留出题，每个复杂桶各 1 条留出；压力集 10 条保持独立回归集。

已回填：`route_target` 设计目标 23 条（明确走结构 RAG 或 Agent），其余适用题标为
`indeterminate` 或不参与路由判分；`token_budget` 52 条。复杂集 12 条中 8 条开发、4 条冻结留出。
压力集 10 条保持独立回归，不从主集总分中混算。

`expected_source` 与 `final_state` **不逐条回填**：`retrieval_metrics.gold_points()`
从 `answer_points[].evidence` + `chunk_ids` 派生位置，`numeric_audit` 从
`evidence.value` 派生终态；重复写一份会让任务卡两处真相不一致。字段保留为
**显式覆盖**入口，lint 校验类型与取值。

### 4.4 轨迹评测

两档落地：按序匹配（`required_sequence`，默认拒答前必须检索、引用题必须检索+读取）
与关键工具（派生数值必须 `calculate`）；外加无效循环检测。
没有声明序列的题不会凭空判失败。

### 4.5 Release Gate

五类闸门与预注册阈值见 `evaluation/release_gate.py::GATE_CONFIG`：

- 质量：pass@1 / pass^k / 忠实度 / 金标覆盖率（未校准，提示）；
- 轨迹：按序 / 关键工具（未校准，提示）；无效循环 = 0、预算不超（已校准，拦截）；
- 成本：token 均值 / P95 / 调用数（未校准，提示）；
- 安全：数值幻觉率 = 0、编造 = 0（红线，拦截）；引用错误 ≤ 1、崩溃 = 0；
  PII 命中（提示，公开公司联系方式不算个人隐私）；
- 业务：误拒率 ≤ 10%（红线，默认提示，`--strict` 拦截）、拒答准确率、路由准确率（未校准，提示）。

---

## 5. 测试与回归

- 新增 11 个测试文件、72 条测试：`test_retrieval_metrics` / `test_routing_metrics` /
  `test_equivalence` / `test_numeric_audit` / `test_failure_taxonomy` /
  `test_trajectory_eval` / `test_faithfulness` / `test_release_gate` /
  `test_eval_report` / `test_stability_metrics` / `test_finance_corpus`。
- 后端全量：**364 passed**。
- 三套任务集 lint：**0 error**（`--tier profile/complex/stress`）。
- 检索一致性：`check_block_retrieval` **12/12**（本轮未改 BlockIndex）。

---

## 6. 成本账

| 项 | 调用 | token |
|---|---|---:|
| Phase 0 全部指标 | 0 | 0 |
| 忠实度并跑（4 题 × 2 臂） | 16 次轻调用 | 约 10k–20k |
| 答案层 4 题探针（补齐三层报告） | 约 36–44 次 | 约 0.16M–0.18M |
| 12 题复杂集 ×1 | 约 84–108 次 | 约 0.5M |
| 52 题 ×1 | — | 1.7M–2.3M |

任何真实模型复跑前仍按项目纪律先报预算上限。本轮没有跑任何真实模型调用。

---

## 7. 已知边界与下一步

**边界**：

1. 检索层离线读数用**任务问题原文**查询；真实 Agent 会改写 query，
   所以离线数只代表“检索入口”质量，不代表 Agent 实际检索行为。有 runs 后用 `--arm agent`。
2. `context precision` 在“一个要点多个可接受位置”时偏低，读数时要和 `point_hit` 一起看。
3. 路由金标来自任务结构，不是实测胜负；有 `compare_arms` 后应并报经验金标口径。
4. 忠实度、数值幻觉率需要真实 runs；本轮没有数据，报告里是 `no_data`。
5. 检索段按 `run_index=1` 打一次；不同 run 的 query 改写可能不同，多次运行的检索方差未计入。
   router 本轮**只度量不优化**；5 条成本浪费 + 1 条质量风险已定位到具体规则。

**下一步（建议顺序）**：

1. 用 4 题探针（复杂集 CX-01/04/07/10）补齐答案层，跑 `score_agent_runs` →
   `numeric_audit` → `trajectory_eval` → `release_gate` → `eval_report`，得到首份完整三层报告：

   ```bash
   python -m evaluation.run_finance_eval \
     --tasks evaluation/finance_tasks_complex.jsonl \
     --ids CX-01,CX-04,CX-07,CX-10 --search-mode blocks \
     --out %TEMP%\probe_runs.json
   python -m evaluation.score_agent_runs --runs %TEMP%\probe_runs.json \
     --tasks evaluation/finance_tasks_complex.jsonl --corpus finance \
     --out %TEMP%\probe_report.json
   python -m evaluation.numeric_audit --runs %TEMP%\probe_runs.json \
     --tasks evaluation/finance_tasks_complex.jsonl --report %TEMP%\probe_report.json \
     --out %TEMP%\probe_numeric.json
   python -m evaluation.trajectory_eval --runs %TEMP%\probe_runs.json \
     --tasks evaluation/finance_tasks_complex.jsonl --out %TEMP%\probe_trajectory.json
   ```

2. 按路由审计结果改 `finance_router`：把“同比/占营业/多个问号”从硬信号降级，
   增加“折合/换算”信号；改完用 `routing_metrics` 回归；
3. 用真实 runs 校准 `GATE_CONFIG` 的成本与质量阈值，把 `calibrated` 逐项转 True；
4. 忠实度在探针上与 judge 并跑，不一致样本进人工复核队列。

---

## 8. 题集用途标注与样本分组（2026-11-01 补）

**任务卡标注（人工完成，题面/答案/证据未动）**：

- `route_target` 三态（32/52 显式标注；20 条拒答/澄清/假设/mock/unit_trap 留空）：
  `structured_rag` 8 / `agent` 20 / `indeterminate` 6（FIN-01/02/03、FIN-S03/04/05）；
- `prompt_style`（52/52）：direct_constraint 13 / open_question 39；
- `retrieval_hops`（52/52，**证据单元桶**不是精确跳数）：1=单证据单元，2=跨证据单元，0=无金标位置；
- `answer_operations`（52/52）：lookup / compare / calculate / validate / abstain / clarify / recover / cite；
- 复杂集切出 **8 dev + 4 冻结留出**（CX-03/06/09/12，每桶恰好 1 条），lint 强制校验；
  `run_rag_eval` / `run_finance_eval` 默认 `--holdout exclude`，最终验收显式 `only`。
  **这 4 条是 post-hoc 留出**（复杂集此前被调参用过），只能做「不退化」护栏；
  15pp 的价值判定仍用全量 12 题。

**本轮代码补齐（三处「标了但没接上」）**：

1. `populations` 三组（core_financial_qa / refusal_clarification / mock_recovery）
   真正接入 `agent_report.build_populations` → console 与三层报告各自出
   pass@1 / pass@k / pass^k；mock 恢复不再混进核心总分；
2. verdict 附 `route_target` / `prompt_style` / `retrieval_hops`，
   报告新增 prompt_style 与 retrieval_hops 两个分栏（各自 Wilson 区间，未标注落「未标注」桶）；
3. lint 一致性检查（`lint_finance_tasks._lint_schema_v2`）：
   - error：`structured_rag` + hops≥2；`structured_rag` + calc/派生要点（RAG 臂没有计算器）；
   - warning：披露值误标 calculate（**FIN-04 命中**：ROE 是披露值，应把 calculate 从 ops 移除）、
     单跳无计算的 agent、open_question 题面含显式指令；
   - `retrieval_hops` 桶语义与 `prompt_style` 判定口径已写成注释常量（2026-11-01 定稿）。

**效果读数（路由层，三集合并、默认排除留出）**：
明确题 **21/21 = 100%** [84.5%, 100%]，Macro-F1 1.0；
6 条边界题单列（router 实际 5 agent / 1 structured_rag），过度触发可见但不污染准确率。
复杂集 dev（6 条有金标）：结构检索要点@5 72.4% vs 固定窗 48.3%，整题全中 2/6 vs 1/6。

**测试**：后端 **375 passed**；三套 lint 0 error（profile 含 1 条 FIN-04 warning）。

**仍开放的小项**：`routing_metrics` 顶层 basis 标签未区分显式/推导；`excluded` 打印描述
未覆盖 false_premise/unit_trap/mock；`cite` 是否留在 answer_operations 待定；
4 条留出的 post-hoc 性质未写进任务卡注释。
