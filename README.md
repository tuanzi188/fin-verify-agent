# 金融查证 Agent 与检索评测

面向金融文档的查证产品：**材料入库 → 提问 → 结论与证据 → 历史复核**。
产品只保留一条主路径（`/finance/*`），评测独立运行（`/lab` 只读离线产物）。

> **接手 / 引用数字前先读**：[AGENTS.md](AGENTS.md)（工程导航）、
> [docs/CURRENT.md](docs/CURRENT.md)（当前阶段与未闭环项）、
> [docs/evidence/INDEX.md](docs/evidence/INDEX.md)（哪个评测读数有效）。
> `docs/archive/` 内为历史设计与评测记录，只查依据、不引读数。

---

## 项目定位

传统知识库问答只给一段 Markdown，数字对不对、出自哪张表、口径是什么都无法核对。
本项目把一次查证拆成可验证的结构：

- 问题自动选路：单点读数走结构检索，比较/计算/跨文档走多步 Agent；
- 结论拆成要点、引用坐标、计算记录和不确定性；
- 每条引用能定位到文档、页码、表格、目标行列和单位；
- 会话落库时保留结构化载荷，重开还能继续核对；
- 评测台只展示已落盘的实验产物，不在 Web 请求里跑模型。

当前语料是本地预先准备的 **7 份年报/募集说明书（4 家公司）**，入库、校验由脚本完成；
文档页另有上传入口（实验特性），上传件与官方语料分目录存放，评测只读官方语料。

---

## 页面

| 路由 | 功能 |
|---|---|
| `/` 查证 | 左侧语料覆盖 + 当前材料 + 最近查证；中间问题、状态、结论要点、计算；右侧证据检查器（≥1280 常驻，<1280 抽屉，<768 底部 Sheet） |
| `/documents` 语料 | 文档清单：公司、期间、类型、页码、章节数、表数、单位、解析状态 |
| `/lab` 实验台 | Agent vs 结构感知 RAG 的主比较、pass@1 / pass^k、硬门禁、token、延迟、分维度、逐题翻转、McNemar；固定窗 RAG 默认收起为离线基线 |
| `/audit` 审计 | 按用户 / 动作 / 资源 / 状态 / 时间查询审计日志（管理员） |

---

## 一次查证发生什么

1. **自动选路**（`backend/app/finance/routing_policy.py`）
   - 单公司 / 单期间 / 单指标 → 结构感知 RAG；
   - 对比、计算、跨文档、跨期间、未来期间、多个子问题 → 多步 Agent。
2. **结构检索**：块级混合检索（向量 + BM25 + RRF），按问题识别公司与期间过滤，一次 LLM 生成，证据转成 `EvidenceRef`。
3. **多步 Agent**：只读工具循环
   `search_blocks` / `search_tables` / `search_sections`（sections 模式）/ `read_block` / `read_section` / `get_rows` / `get_table` / `get_cell` / `calculate` / `list_documents`；
   空结果与失败会改写重试一次；覆盖检查按「公司 × 期间 × 指标」槽位补读，终轮做覆盖自检。
   多步模式维护结构化任务清单，记录目标行列、缺口与冲突；连续无新增信息时停止重复调用。
   已知待查项未覆盖的答案进入人工复核，清单随 `verification.task_plan` 保存至历史；
   清单覆盖只说明已取得目标证据，不认证答案语义。规则见 [任务清单](docs/AGENT_TASK_PLAN.md)。
4. **结构化输出**：`answer`、`claims`（要点 / 引用编号 / 计算下标 / 不确定性）、
   `citations`（坐标）、`calc`（公式 / 输入 / 结果 / 单位 / 口径）、`trace`、`status`、`route_reason`、`notes`。
5. **状态口径**：已回答 / 资料不足 / 需澄清 / 需人工复核 / 执行失败。
   后端会对引用、数值、单位和计算输入来源做确定性校验，未通过时返回“需人工复核”；
   前端也会因语料版本变化或证据回查失败提示复核。

2026-11-02 查证可靠性修复：组合引用完整解析、去重后同步编号、百分点展示尺度修正、
计算输入坐标溯源、结论数值复核、追问条件补全，以及检索前主体和期间检查。
实现边界及复现方法见 [可靠性修复说明](docs/archive/20261102_finance_reliability.md)。

2026-11-04 多步查证工具层加固：行列名归一化与别名、`get_rows` 部分成功、
`calculate` 参数别名、表目录 `search_tables`、指标槽位覆盖、正文 JSON 工具调用回注。
实现状态与待测项见 [docs/CURRENT.md](docs/CURRENT.md)。

---

## 证据与历史

- 每条证据包含：文档、公司/期间、页码、章节路径、表 id/表名、行/列索引、行名/列名、
  单元格、单位、脚注、来源跨度、`verified`。
- 证据检查器四个 Tab：**原文 / 表格 / 计算 / 引用链**。
  表格高亮目标行列、首列冻结，单位与脚注单独展示；计算卡可展开复算。
- 引用胶囊统一显示「文档 · 页码 · 表格 · 目标行列」，点击右栏同步定位。
- 会话每轮落库结构化载荷；重开还原结论卡、引用坐标、计算和查证过程。
- 历史证据按需回查：加载中 / 找不到 / 回查失败可重试 / 语料版本变化，四态明确；
  404 会把该轮标成「需人工复核」。

---

## 评测（独立于产品）

- 任务集：30 条 Agent 任务 × 5 维（多步拆解 / 恢复与纠错 / 拒答与边界 / 检索与引用 / 澄清与追问）；
  金融任务 30 基础 + 12 complex + 10 stress；另有 2 条冒烟。
- `baseline`：最多一轮工具调用，不重试、不自检（对照组）。
- `improved`：多轮工具循环 + 空/错自动重试 + 终轮覆盖自检（产品默认）。
- 指标：`pass@1`、`pass^k`、硬门禁、token/次、延迟、步数；显著性只做整卷配对 McNemar。
- `/lab` 只读 `docs/evidence`，没有产物时显示“主比较未运行 + 复现命令”。

```bash
# 1) 跑原始结果（--skip-index 复用已有语料）
cd backend
python -m evaluation.run_finance_eval --variant improved --runs 3 \
  --out ../docs/evidence/finance_improved_runs3.json
# 2) 判分（含 LLM judge）
python -m evaluation.score_agent_runs --runs ../docs/evidence/finance_improved_runs3.json \
  --out ../docs/evidence/finance_improved_report --concurrency 3
# 3) 双变体对比
python -m evaluation.compare_agent_reports \
  --baseline ../docs/evidence/finance_baseline_report.json \
  --improved ../docs/evidence/finance_improved_report.json
```

> 以下两张表均为**历史快照**，只作机制对照。当前有效读数与口径绑定条件见
> [docs/evidence/INDEX.md](docs/evidence/INDEX.md)；答案层当前为 27/42 = 64.3%（重判产物），
> 硬门禁仍未过，不能当验收结论。

以下为 **2026-10-27 通用制度库 Agent 历史实测**（30 条 × 3 次 × 2 变体 = 180 次运行），
用于比较 Agent 执行机制，**不代表当前金融查证产品的端到端准确率**：

| 指标 | baseline | improved |
|---|---|---|
| pass@1（任务均值） | 80.0% | 95.6% |
| pass^3 | 70.0% | 93.3% |
| 恢复与纠错 | 22.2% | 83.3% |
| 拒答与边界 | 100% | 94.4% |
| token / 次运行 | 2,378 | 5,650（+137.6%） |
| 中位延迟 | 6.05 s | 10.81 s（+78.6%） |

整卷配对 McNemar：修好 5 / 弄坏 0，**p=0.0625（未达 0.05）**。
准确表述是“恢复维度有可分辨改进，整卷显著性因只有 5 个不一致对而不成立”，
不能写成“全卷显著提升”。数据见 `docs/evidence/agent_compare3_20261027.json`。

金融语料最近一次已落盘的检索复测为 2026-11-03（解析双修复重入库后）：主集开发题要点命中率@5 为 69.7%，
复杂集为 50.0%，压力集为 33.3%。掉点均为稀释性挤出（金标块仍在 top-20，rank 3–8），无打空型退化；
方向性收益：美的 2025 资产总计块 rank 18→2。这些是一次检索的离线指标，不能替代答案正确率；
真实金融答案层、数值审计和发布闸门仍需另跑。详见 `docs/evidence/eval_retrieval_20261103.md`，
上一份快照 `docs/evidence/eval_retrieval_20261101.md`（75.8%/68.8%/44.4%，语料状态不同）。

### 三层评测（2026-10-31 升级）

除答案层外，新增检索层、路由层与离线 Release Gate；全部零模型调用：

```bash
# 检索层：结构感知 RAG vs 固定窗，一次检索、同 K
python -m evaluation.retrieval_metrics --tasks evaluation/finance_tasks_complex.jsonl \
  --arm both --top-k 5 --out %TEMP%\retrieval.json
# 路由层：router 选臂 vs 任务结构金标
python -m evaluation.routing_metrics \
  --tasks "evaluation/finance_tasks.jsonl,evaluation/finance_tasks_complex.jsonl,evaluation/finance_tasks_stress.jsonl" \
  --out %TEMP%\routing.json
# 五类闸门 + 三层报告（需先有 answer report / numeric / trajectory）
python -m evaluation.release_gate --report %TEMP%\fin_report.json \
  --retrieval %TEMP%\retrieval.json --routing %TEMP%\routing.json --out %TEMP%\gate.json
python -m evaluation.eval_report --retrieval %TEMP%\retrieval.json \
  --routing %TEMP%\routing.json --answer-report %TEMP%\fin_report.json \
  --out-md %TEMP%\eval_report.md
```

- 检索层：`HitRate@K` / `MRR` / `context precision` / `context recall` / `read hit`；
  `first_search` 双臂同尺，`any_search` 只作 Agent 诊断。
- 路由层：准确率、混淆矩阵、Macro-F1、错分代价（简单题烧钱 / 复杂题拿差答案）。
- 稳定性：`pass@k`（至少一次）与 `pass^k`（全部）分开报，连续型指标报均值 ± 标准差 + P95。
- 闸门：金融两条红线——数值幻觉率 = 0、误拒率 ≤ 阈值；缺数据记 `insufficient_data`，不算通过。
- 实施记录与首轮读数：`docs/archive/20261101_eval_upgrade_implementation.md`；等价规则：`docs/ANSWER_EQUIVALENCE_RUBRIC.md`。

### 辅助问答评测（概念 / 六类拒答）

与金融 5 维分开的独立域，任务卡与判分口径见 [docs/ASSIST_EVAL.md](docs/ASSIST_EVAL.md)。

```bash
# 任务卡 lint（零模型）
python -m evaluation.lint_assist_tasks
# 六类拒答：完整零模型
python -m evaluation.run_assist_eval --suite refusal --out %TEMP%\assist_refusal.json
python -m evaluation.score_assist_runs --runs %TEMP%\assist_refusal.json --out %TEMP%\assist_refusal_report.json
# 概念题：必须带检索快照；--judge 可选
python -m evaluation.run_assist_eval --suite concept \
  --replay evaluation/fixtures/assist_web_20261107.json --out %TEMP%\assist_concept.json
python -m evaluation.score_assist_runs --runs %TEMP%\assist_concept.json --out %TEMP%\assist_concept_report.json
# 观点/建议：真实模型 + 服务端免责声明
python -m evaluation.run_assist_eval --suite advice --out %TEMP%\assist_advice.json
python -m evaluation.score_assist_runs --runs %TEMP%\assist_advice.json --out %TEMP%\assist_advice_report.json
```

### 任务集维护（防漂移）

```bash
# 语料重解析或路由规则变更后：三套都跑，0 error 才算过
python -m evaluation.lint_finance_tasks --check-refs --check-routing
python -m evaluation.lint_finance_tasks --tier complex --tasks evaluation/finance_tasks_complex.jsonl --check-refs --check-routing
python -m evaluation.lint_finance_tasks --tier stress --tasks evaluation/finance_tasks_stress.jsonl --check-refs --check-routing

# 坐标漂移：按内容签名重绑（默认 dry-run，--write 才落盘；拿不准的列人工处理）
python -m evaluation.rebind_coords --tasks evaluation/finance_tasks.jsonl

# 留出题是否被调参消耗过
python -m evaluation.holdout_registry --check --tasks evaluation/finance_tasks.jsonl

# 可读视图（评审 diff 用）
python -m evaluation.render_tasks --tasks evaluation/finance_tasks.jsonl --out docs/tasks_review.md
```

---

## 技术栈

- 前端：React 18 + TypeScript + Ant Design 5 + Vite 5
- 后端：FastAPI + Uvicorn
- 检索：结构块索引（向量 + BM25 + RRF），金融语料专用
- 存储：会话 JSON/Redis 可选；审计日志落盘
- 模型：DeepSeek / 阿里云百炼，按配置切换

---

## 快速开始

### 前置要求

- Python 3.10+（CI 覆盖 3.10 / 3.12）
- Node.js 20+（`react-router-dom@7` 要求 20.0.0 以上）
- 可用的模型 API（DeepSeek 或阿里云百炼）
- Redis 7+（可选，仅用于会话持久化）

### 1. 配置

```bash
cp backend/.env.example backend/.env
# 编辑 backend/.env，填入 DS_API_KEY / 模型出口
```

### 2. 准备金融语料

```bash
cd backend
pip install -r requirements.lock            # 可复现安装；改依赖请改 requirements.txt
python -m scripts.fetch_finance_corpus      # 可选：拉取年报/募集说明书
python -m scripts.ingest_finance_corpus     # 解析、建块索引
python -m scripts.verify_finance_corpus     # 校验文档/章节/表格数量
```

语料默认放在仓库根目录 `data/finance/`，已加入 `.gitignore`；
文档页上传入口仅用于本地补充材料，上传件单独存放，评测只读官方语料，避免把通用文档混进查证库。

### 3. 启动后端

```bash
cd backend
python -m uvicorn app.main:app --host 127.0.0.1 --port 8002 --reload
# Swagger: http://localhost:8002/docs
```

### 4. 启动前端

```bash
cd frontend
npm ci
npm run dev
# 访问 http://localhost:5173
```

### 5. Docker Compose（可选）

```bash
docker compose up -d --build
# 网页：http://localhost:5173
# API：http://localhost:8002/docs
```

Docker 使用 `DATA_DIR=/app/data`；需要先把 `data/finance` 挂载进容器。

---

## API

| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/v1/auth/me` | GET | 当前身份 |
| `/api/v1/finance/chat` | POST | 金融查证；`strategy=auto\|structured_rag\|agent` |
| `/api/v1/finance/stats` | GET | 语料覆盖与索引指纹 |
| `/api/v1/finance/documents` | GET | 文档清单 |
| `/api/v1/finance/documents` | POST | 上传并解析文档（实验特性；`file` + `company` + `period`） |
| `/api/v1/finance/evidence` | GET | 按 `evidence_id` 或表格坐标回查证据 |
| `/api/v1/conversations` | GET | 会话列表 |
| `/api/v1/conversations/{id}` | GET | 会话详情（含结构化 payload） |
| `/api/v1/conversations/{id}` | DELETE | 删除会话 |
| `/api/v1/lab/runs` | GET | 只读评测产物 |
| `/api/v1/analytics/audit` | POST | 审计日志查询（管理员） |
| `/api/v1/health` | GET | 基础健康检查 |
| `/api/v1/health/deep` | GET | 子系统深度检查（需认证） |

通用 `/chat`、`/chat/stream`、`/search`、`/classify`、`/upload`、`/documents`、`/stats`、`/tasks`
以及 MCP 示例服务已下线；产品统一走金融入口。

---

## 项目结构

```
fin-verify-agent/
├── frontend/
│   └── src/
│       ├── components/          # 工作台、证据检查器、结论卡、轮次条
│       ├── hooks/               # useChat（仅金融）、useConversations
│       ├── pages/               # LabDashboard / FinanceDocuments / AuditLogs
│       ├── utils/               # 状态口径、引用描述、历史载荷解析
│       └── styles/              # CSS tokens 与工作台样式
├── backend/
│   ├── app/
│   │   ├── api/                 # chat(认证/会话)、finance、lab、analytics(审计)
│   │   ├── finance/             # 文档库、结构块索引、入库解析
│   │   ├── services/            # 金融入口、检索、会话、审计、模型层
│   │   ├── agent/               # Agent runner（baseline / improved）
│   │   └── tools/               # 金融只读工具层
│   ├── evaluation/              # 任务集、判分、pass^k、双变体对比
│   └── scripts/                 # 语料抓取 / 入库 / 校验
├── docs/                        # 评测设计与证据
└── docker-compose.yml
```

---

## 身份与权限

- 单用户开发模式：未配置认证时使用固定 `anonymous` 身份，仅适合本机。
- 生产/预发模式：`APP_ENV=production` 时若既无 `API_KEY` 也无 `AUTH_USERS_JSON`，服务拒绝启动。
- 多用户模式：在 `backend/.env` 配置 `AUTH_USERS_JSON`（独立随机令牌、用户 ID、角色、部门）。
- 角色只由服务端授予；实验台对所有登录用户开放（便于演示与复核），审计仅管理员。
- 会话按用户命名空间隔离，列表、详情、删除均隔离。
- 令牌只存在当前标签页的会话存储；公网部署必须 HTTPS。

---

## 数据与升级

- 本地数据默认在仓库根目录 `data/`；Docker 使用 `DATA_DIR=/app/data`。
- 金融语料在 `data/finance/`，会话在 `data/conversations/`，审计在 `data/audit_logs/`。
- 语料重新入库后索引指纹会变化；历史会话会提示“语料版本已变化”，证据按新语料回查。
- 无外部模型凭据时可以跑单元测试；真实查证仍需可用的模型 API。

---

## 开发验证

```bash
cd backend
pip install -r requirements.lock
pip install pytest pytest-asyncio
python -m pytest -q

cd ../frontend
npm ci
npm run typecheck            # tsc --noEmit
npm test
npm run build
```

通过数会随迭代变化，不在此写死；接手时的验证命令见 [AGENTS.md](AGENTS.md) §3。
依赖锁由 `uv pip compile requirements.txt --python-version 3.10 --no-header -o requirements.lock`
生成；CI 与 Docker 只安装 lock，改依赖请改 `requirements.txt` 后重新生成。
CI 在所有分支的 push 与 PR 上运行后端测试、任务卡 lint、检索回归门禁、
前端测试/类型检查/生产构建。

## License

MIT
