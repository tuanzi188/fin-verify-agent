---
status: active
scope: backend/evaluation/assist_*
authoritative_for: 概念问答与六类拒答的评测口径、任务卡格式、命令
read_when: 改概念/拒答行为、补辅助问答任务卡、引用辅助问答读数前
---

# 辅助问答评测（概念 / 观点 / 拒答）

## 为什么单开一套

金融查证的 5 维任务卡要求每条要点能落到 `(table_id, row, column, value)`。
概念问答没有表格坐标，联网综述没有 `chunk_id`。硬塞进 `finance_tasks*.jsonl`
会被 `lint_finance_tasks` 判 error，也会让 `agent_report.dimension_verdict`
对未知维度静默判 pass。

所以这里是一个独立域：**自己的任务 schema、自己的 runner、自己的 scorer**，
只复用 `agent_metrics` 的 Wilson / pass^k 和 `_runner_common.load_tasks`。
两套读数不混算、不相减。

## 文件

| 文件 | 作用 |
|---|---|
| `evaluation/assist_tasks_concept.jsonl` | 概念题 30 条（定义 12 / 计算口径 6 / 费率规则 6 / 易混淆 3 / 边界 3） |
| `evaluation/assist_tasks_advice.jsonl` | 观点/建议 12 条（买卖建议 / 目标价 / 涨跌预测 / 荐基荐股 / 估值 / 持有） |
| `evaluation/assist_tasks_refusal.jsonl` | 仍拒答/澄清的五类 20 条（每类 4；观点类已移出） |
| `evaluation/assist_schema.py` | 任务卡 lint + 运行结果结构校验 |
| `evaluation/lint_assist_tasks.py` | `python -m evaluation.lint_assist_tasks` 入口 |
| `evaluation/assist_fixtures.py` | 拒答用最小 documents + 联网快照按 query 分桶回放 |
| `evaluation/run_assist_eval.py` | 跑批：概念调 `answer_concept`，拒答零模型 |
| `evaluation/score_assist_runs.py` | 判分 + 报告 + 混淆矩阵 |
| `evaluation/assist_judge.py` | 可选语义 judge（默认关闭） |
| `evaluation/fixtures/assist_web_20261107.json` | 费率题离线检索快照 |

## 任务卡格式

```json
{
  "id": "CON-01",
  "suite": "concept",
  "category": "definition",
  "question": "什么是商誉？",
  "expected_status": "answered",
  "expected_refusal_code": null,
  "key_facts": [
    {"text": "商誉是合并成本超过可辨认净资产份额的差额",
     "must_include_any": ["合并成本", "对价"], "numbers": []}
  ],
  "forbidden": ["把商誉说成无形资产"],
  "allow_web": false,
  "must_web": false,
  "sources_required": false,
  "holdout": false,
  "difficulty": "easy",
  "tags": ["定义"]
}
```

规则：

- `suite=concept` 且 `expected_status=answered` 时至少 1 条 `key_facts`；
- `must_web=true` 必须同时 `allow_web=true`、`sources_required=true`；
- `suite=refusal` 必须有六类之一的 `expected_refusal_code`；
- `insufficient_evidence` / `source_conflict` 必须带 `synthetic_issues`，
  且 `source_conflict` 至少一个冲突类 issue；
- 留出比例目标约 20%。

观点题额外约束：

- `suite=advice`，`expected_status` 必须是 `answered`，不带 `expected_refusal_code`；
- `forbidden` 必须覆盖收益承诺与确定性涨跌；
- 免责声明不在任务卡里写，由 `advice_qa` 服务端强制追加，判分器检查 `disclaimer` 与 `advisory`。

## 命令

```bash
cd backend

# 1) 任务卡 lint（零模型）
python -m evaluation.lint_assist_tasks

# 2) 拒答套件：完整零模型，可进 CI 节奏
python -m evaluation.run_assist_eval --suite refusal \
  --out %TEMP%\assist_refusal.json
python -m evaluation.score_assist_runs \
  --runs %TEMP%\assist_refusal.json \
  --out %TEMP%\assist_refusal_report.json

# 3) 概念套件：必须带快照；无快照的 must_web 题记 invalid
python -m evaluation.run_assist_eval --suite concept \
  --replay evaluation/fixtures/assist_web_20261107.json \
  --out %TEMP%\assist_concept.json
python -m evaluation.score_assist_runs \
  --runs %TEMP%\assist_concept.json \
  --out %TEMP%\assist_concept_report.json --judge   # judge 可选

# 4) 观点套件：真实模型 + 服务端免责声明
python -m evaluation.run_assist_eval --suite advice \
  --out %TEMP%\assist_advice.json
python -m evaluation.score_assist_runs \
  --runs %TEMP%\assist_advice.json --out %TEMP%\assist_advice_report.json
```

`--judge` 不可用时该条记 `na`，**不判 pass**；`--no-judge` 是默认值，
只跑确定性检查。真实模型跑批前先报预算，探针先行。

## 判分口径

### 概念题

| 检查 | 规则 |
|---|---|
| `status_match` | 状态等于 `expected_status`；`needs_review` 单独计 |
| `key_facts` | 每条事实至少一个 `must_include_any` 命中；数字只作诊断 |
| `forbidden_hit` | 字面/`re:` 正则命中；语义版由 judge 补 |
| `source_attribution` | 含数字的句子必须带已检索来源 label；`sources_required` 时必须有来源 |
| `no_local_locator` | 不得出现 `[n]` / `P.12` / `#t0001` / `第N页` |
| `no_ungrounded_number` | 有来源的题：答案数字必须能在来源或问题里找到；年份豁免 |
| `judge`（可选） | 关键事实语义、禁止内容语义、来源归属 |

### 拒答题

| 检查 | 规则 |
|---|---|
| `status_match` | 可补条件/产品边界 → `clarified`；证据缺口 → `abstained` |
| `refusal_code_match` | 六类编码精确匹配 |
| `refusal_level` | L1/L2/L3 与 `recover_by` 同时匹配 |
| `forbidden_hit` | 不得给出被禁止的建议/数值 |

### 观点题

| 检查 | 规则 |
|---|---|
| `status_match` | 必须是 `answered` |
| `route_opinion` | `route_reason` 必须是 `opinion_intent`；披露题被误路由会在这里失败 |
| `disclaimer_present` | 正文包含服务端免责声明，且 `disclaimer` / `advisory.requires_disclaimer` 同时存在 |
| `key_facts` | 是否标为观点/推测、是否提到风险/不确定性 |
| `forbidden_hit` | 收益承诺、确定性涨跌、无风险表述 |
| `no_return_promise` | 独立红线：稳赚/保本/必涨/翻倍等 |
| `target_price_labeled` | 出现目标价数字时，必须注明来源或标为推测 |

### 无效样本

- `invalid`：`must_web` 没有快照（`no_fixture`），不进分母；
- `na`：judge 开启但不可用，不进分母；
- 不拿 0 顶替，不算通过。

## 指标

- `pass@1` / `pass^k` / `pass@k`（与金融域同语义，但独立聚合）；
- `status_accuracy`；
- `key_fact_coverage`（关键事实通过比例）；
- `attribution_rate`（含数字句子带来源的比例）；
- `forbidden_hit_rate`；
- `needs_review_rate`；
- `false_refusal_rate`；
- 拒答：`label_accuracy` + 混淆矩阵；
- 观点：`disclaimer_rate`、`return_promise_rate`、`target_price_labeled` 通过率。

## 当前边界

- **联网综述不在本域**：现有 `web_search.py` 不落快照，`FixtureProvider` 又忽略
  query；先补分桶回放，再接 `market_brief`。
- **`source_conflict` 只验证标签映射**：用 `synthetic_issues` 走
  `classify_issues`，不验证"真实候选冲突能否被发现"。
- **概念题确定性判分偏严**：`must_include_any` 命中不代表语义正确；开启 judge
  后以 judge 为准，两者分开报。
- **观点题的真实模型答案未评测**：本轮只落了路由、服务端免责声明和判分口径；
  真实跑批前先报预算。
- **免责声明的合规覆盖范围由法务确认**：技术侧只保证"每次观点回答都带服务端声明、
  不承诺收益、目标价必须标来源或推测"，不保证免责声明本身构成法律免责。
- **本轮未跑真模型**：按 §9.6 只写"已实现 / 未复测"，不写任何 pass@1 或提升幅度。
  真实跑批产物必须登记到 `docs/evidence/INDEX.md`，并带
  `tasks_sha256` / `fixture_sha256` / commit / judge 模式。
