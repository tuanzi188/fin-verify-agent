# AGENTS.md — 接手本项目的第一步

> 本文件是给工程 agent 的导航，不是产品文档。产品说明见 [README.md](README.md)，
> 唯一允许写「现在状态」的文件是 [docs/CURRENT.md](docs/CURRENT.md)。
> 所有历史设计/评测文档已归档到 `docs/archive/`，带 `status: historical` frontmatter。

## 1. 这个项目是什么（30 秒）

金融文档查证 Agent：7 份年报/募集说明书（4 家公司）入库 → 自动选路 →
结论拆成 claims + 引用坐标 + 计算记录 + 不确定性 → 会话结构化落库可复核。
前端 React 18 + TS + AntD 5 + Vite；后端 FastAPI；检索为结构块混合索引
（向量 + BM25 + RRF）；模型走 DeepSeek / 百炼，本地网关 `127.0.0.1:7863`（wb2api）。

产品只有一条主路径 `/finance/*`，`/lab` 只读离线评测产物。文档页有上传入口（实验特性），
上传件与 `data/finance` 官方语料分目录存放，评测只读官方语料。

## 2. 接手第一步（不要写死状态，跑命令）

```bash
git status --short                          # 看当前未提交改动
git log --oneline -10                       # 看最近在做什么
git config core.hooksPath scripts/hooks     # 启用文档一致性 pre-commit 钩子（每个 clone 一次）
```

然后按顺序读：

1. [docs/CURRENT.md](docs/CURRENT.md) — 当前阶段判定与未闭环项
2. [docs/COST_OPTIMIZATION_PLAN_20261104.md](docs/COST_OPTIMIZATION_PLAN_20261104.md) §5 — 当前活跃待办
3. [docs/INDEX.md](docs/INDEX.md) — 10 份文档哪份能信
4. [docs/evidence/INDEX.md](docs/evidence/INDEX.md) — 哪个评测读数有效

## 3. 五分钟验证（离线，零模型调用）

```bash
cd backend && python -m pytest -q
cd ../frontend && npm run typecheck && npm test -- --run
python -m evaluation.lint_finance_tasks --check-refs --check-routing   # 在 backend 下
```

CI 跑后端测试、前端测试与检索回归门禁。任务卡 lint 不在 CI：它依赖本地
`data/finance` 语料（语料不入库），改任务卡或路由后必须在本地跑。通过数会变，
不要记在文档里。真实查证需要可用的模型 API。

## 4. 模块地图（改东西去哪）

| 要改 | 文件 |
|---|---|
| 自动选路 / 升级判定 | `backend/app/finance/routing_policy.py`、`docs/ROUTING_POLICY.md` |
| Agent 工具循环 | `backend/app/agent/runner.py`、`app/tools/finance_set.py` |
| 行列名解析 / 表访问 | `backend/app/finance/store.py`（单位后缀、别名、模糊匹配、错位自校验） |
| 覆盖槽位 / 表目录检索 | `backend/app/tools/finance_set.py`、`app/finance/query_scope.py`、`app/tools/finance.py` |
| 正文 JSON 工具调用 / 回答闸门 | `backend/app/agent/runner.py`、`app/agent/prompts.py` |
| 结构检索 / 块索引 | `backend/app/finance/block_index.py`、`app/services/finance_rag.py` |
| 证据 / 引用 / 计算 | `backend/app/finance/references.py`、`calc.py`、`calculation_records.py` |
| 模型客户端 / 缓存记账 | `backend/app/services/openai_client.py`、`evaluation/agent_metrics.py`、`evaluation/audit_run_cost.py` |
| 任务集 / 判分 / 闸门 | `backend/evaluation/finance_tasks*.jsonl`、`score_agent_runs.py`、`release_gate.py`、`lint_finance_tasks.py` |
| 三层评测 / 报告 | `backend/evaluation/retrieval_metrics.py`、`routing_metrics.py`、`eval_report.py`、`agent_report.py` |
| 工作台 / 证据检查器 | `frontend/src/components/EvidenceInspector.tsx`、`WorkbenchTurns.tsx`、`FinanceConclusion.tsx` |
| 语料入库 / 解析 | `backend/scripts/ingest_finance_corpus.py`、`app/finance/ingest.py` |
| 依赖 / 锁文件 | `backend/requirements.txt`（人工输入）、`requirements.lock`（uv 生成，勿手改） |

## 5. 硬性纪律（这个项目反复踩的坑）

1. **任何真实模型调用先报预算上限**；探针先行、全量后置。评测运行尽量用离线层
   （检索 / 路由 / Gate / lint 零模型调用）与 `--reuse-judgments` 零 token 重判。
2. **`--holdout` 默认 `exclude`**，只有最终验收才 `only`；10 条留出题不能拿来调参。
3. **两个成本口径永远分开报**：全价 token（预注册 ≤20k 红线口径）与有效输入
   （按 `CACHE_HIT_PRICE_RATIO=0.1` 折算的**估算**，报告里带
   `effective_input_is_estimate: true`）。「有效成本达标」≠ 预注册达标。
4. **不落盘不算数**：任何对外数字必须能指向 `docs/evidence/` 下具体文件，
   文档表格只是该文件的渲染；只有转述没有产物的读数标 `unverified`。
5. **收益不跨基线叠加**：sections→blocks 的机制收益已兑现进 blocks 基线，
   不得在后续优化里再算一遍。基线登记见 `docs/evidence/INDEX.md`。
6. **runs_per_task=1 时没有 pass^k**，Wilson 区间不含 run-to-run 方差；
   单次能力剖面只能说「这一批长这样」。
7. **缺数据记 `insufficient_data`/`no_data`，不拿 0 顶替**，不算通过。

## 6. 陷阱清单（读文档前先看）

- **三套判分口径**：同一批 42 题 runs 有 50%（修正前产物）、61.9%（文档转述，
  无独立产物）、64.3%（零 token 重判，`..._rejudged.json`）三个数，
  另有 judge 要点 71.4%。引用必须带产物名。详见 `docs/evidence/INDEX.md`。
  **注意**：64.3% 绑定 11-03 旧 runs（`221318d`, `sections` + 42 题卡 + 零 token 重判），
  **不是当前代码**。2026-11-04 已有 11 题 blocks 子集复测产物
  （`finance_ms11_20261104.json` / `finance_ms11_report_20261104.json`，绑定 `d9707b1`、
  真实 judge、`pass@1=7/11`），是**不同条件下的单次剖面**：
  `corpus_hash` 一致但 search_mode / 任务卡 / judge / commit 全不同，
  **不能相减、不能写「提升 X 个百分点」、不能拿 11 题红线全绿宣称 42 题验收通过**。
  两份读数的绑定条件与登记见 `docs/evidence/INDEX.md` §A / §D。
  **judge 口径（2026-11-06）**：`agent_judge._JUDGE_TEMPLATE` 本日改动（澄清判定
  改为只认本题 `{clarify_instruction}`、rubric 注入 9 条 few-shot），上述 64.3% / 7/11
  均为**改动前** judge 产出。改 judge prompt 不重跑就不产生新读数；新旧判分
  **不同口径、不可相减**（§9.6 只写「已实现 / 未复测」）。登记见 `docs/evidence/INDEX.md` §A 补记。
- **提示词实验必须带 `prompt_sha256`**：2026-11-06 起 `run_finance_eval` 把拼接后的
  system prompt 哈希写进 `run_config`。之前的 `format_probe3{,_b,c}_20261106.json`
  三次对照只有提示词不同，但 `run_config.git` 仅 `commit + dirty=true`，
  按 §9.2 标 `binding: incomplete`，不得当可复现基线。详见 `docs/evidence/INDEX.md` §D 补记。
- **合并任务卡**：新产物的 `run_config.tasks_file` 指向 `<out>.tasks.jsonl`
  （`run_finance_eval` 自动落盘的筛选后任务集），可复现；但**历史报告**的
  `tasks_file` 仍指向 `%TEMP%`，临时文件消失后不可复现。重建方式见
  `docs/evidence/INDEX.md` §C。
- **缓存字段两条路径**：`openai_client._chat_impl`（不带 tools）与
  `_chat_with_tools_impl`（带 tools）都必须透传 `cache_hit/miss_tokens`；
  漏一条就会把工具循环误记成 0 命中（2026-11-04 已修复）。
- **检索层 vs 答案层命中率不可混**：离线层用任务问题原文查询（first_search），
  答案层用 Agent 改写后的 query，两个数字口径不同。
- **覆盖率闸门按指标槽位触发**：比率题不拆成基础科目（「净资产收益率」不会要求
  单独读「净资产」行）；`search_tables` 计入 5 次检索预算，读失败最多补 2 次额度。
- **judge 与金标同模型**，存在同源偏好；未做 judge 扰动或外部抽审。
- **网关对带 tools 请求照常缓存**，不要重提 H1/H2 写延迟/键控假设（已证伪）。

## 7. 文档与产物导航

- [docs/INDEX.md](docs/INDEX.md) — 10 份文档状态总表（哪份 normative / historical / active）
- [docs/evidence/INDEX.md](docs/evidence/INDEX.md) — 产物清单 + 当前有效基线登记
- `docs/archive/*` — 设计史与历史读数，**只查依据，不引读数**

## 8. 不要做的事

- 不要把 `docs/archive/` 里的历史读数写进摘要或对外表述。
- 不要因为「让数字一致」去回改历史文档：它们绑定不同条件，靠索引表区分。
- 不要删 `evaluation/legacy/` 与历史实验目录：它们是回归对照。
- 不要在未跑 `lint_finance_tasks` 前改任务卡的 `answer_points[].chunk_ids`。

## 9. 文档回填纪律（其他 agent 必读）

改代码可以只改一个文件，回填文档必须按下面的映射走；**写错位置比不写更糟**。

### 9.1 先判类型，再动笔

| 你要回填的内容 | 写到哪里 | 不要写到哪里 |
|---|---|---|
| 当前阶段、活跃待办、未闭环项 | `docs/CURRENT.md` | README、archive |
| 产品行为、页面、API、工具清单 | `README.md` | CURRENT |
| 接手导航、模块地图、硬纪律、陷阱 | `AGENTS.md` | archive |
| 评测读数、基线、产物状态 | `docs/evidence/INDEX.md` + 具体产物 | CURRENT、README |
| 设计史、已被取代的方案 | `docs/archive/`（只增不改） | 顶层活文件 |
| 路由 / 等价判据等 normative 规则 | `docs/ROUTING_POLICY.md`、`docs/ANSWER_EQUIVALENCE_RUBRIC.md` | 随意新建文档 |

### 9.2 数字三件套（缺一不可）

任何对外数字必须同时带：

1. **产物路径**（`docs/evidence/...` 或具体 run 文件）；
2. **绑定条件**（`corpus_hash` / `tasks_sha256` / git commit / `runs_per_task` / judge mode）；
3. **状态**（`current` / `historical` / `superseded`）。

没有产物就写 `unverified`，不要写进结论；缺数据记 `insufficient_data`，**不要拿 0 顶替**。

### 9.3 一个状态只有一个入口

- 活跃待办只在 `docs/CURRENT.md` 有索引；`COST_OPTIMIZATION_PLAN` 等 active 文档只放细节，
  并在 CURRENT 留一行链接。
- 更新待办时同步改 CURRENT 的那一行；不要只改细节文件。
- `CURRENT.md` 不写死 `pass@1`、`token/题`、百分比读数；要写就链接到 `docs/evidence/INDEX.md`。

### 9.4 改动代码时的同步清单

新增 / 删除工具或 API 路由时，至少同步四处：

1. `backend/app/tools/finance.py` 的 `FINANCE_TOOL_SPECS`；
2. `README.md` 的工具循环 / API 表；
3. `AGENTS.md` 的模块地图；
4. 跑 `python -m pytest tests/test_docs_consistency.py -q`，让链接 / 清单 / frontmatter 检查兜底。

### 9.5 归档只增不改

- 历史文档不回改数字；需要更正时在原文加「补记」，并标 `superseded` / `historical`。
- 新文档必须带 frontmatter：`status`、`scope`、`authoritative_for`、`read_when`。
- 新文档登记到 `docs/INDEX.md`；未登记的文档视为不可信。

### 9.6 未测就不写收益

代码合入但没跑真模型时，只能写「已实现 / 未复测」，不能写「提升 X%」。
真实模型调用先报预算，探针先行、全量后置；收益不跨基线叠加。

### 9.7 提交前的最小检查

新 clone 先启用仓库内钩子（只跑文档一致性，约 1 秒）：

```bash
sh scripts/setup-hooks.sh        # git config core.hooksPath scripts/hooks
```

完整检查：

```bash
cd backend && python -m pytest -q
python -m pytest tests/test_docs_consistency.py -q
python -m evaluation.lint_finance_tasks --check-refs --check-routing
cd ../frontend && npm run typecheck && npm test -- --run
```

文档改动也要跑一致性测试；不要只在本地打开 Markdown 看一眼。CI 对**所有分支**的 push
与 PR 都跑上述检查，feature 分支不再有豁免。
