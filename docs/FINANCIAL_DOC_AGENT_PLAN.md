# 金融查证 Agent · 方案文档 v1.1

生成日期：2026-10-27
状态：方向已拍板，待落语料与首个垂直切片
关联：`docs/AGENT_EVAL_PROJECT_SPEC.md`（现有 Agent 评测体系）

**v1.1 修订记录（按评审意见收紧）**

- 定位表述改成“不是只会聊天的金融问答机器人，而是以查证、核对和证据回查为核心的金融 Agent”，消除“不是核对工具”与核心模式的矛盾。
- 评测拆成“目标”和“硬门禁”：阈值只作验收方向，不当早期结论；硬门禁只留编造、证据、公式、schema、崩溃。
- 标题树改为**离线构建、落盘、版本化**，运行时只读，不在查询时调 LLM。
- 文档/表格/证据模型补：币种、单位、正负号/括号、合并口径、是否重述、脚注、公告日期、tree_version。
- 证据定位从 `table_id + cell` 改为 `表格标题 + 行名 + 列名 + 单元格原文 + 页码`，`bbox` 可选。
- v1 范围锁死：3 家公司、2024/2025 年报、1 份募集书、30 条题、手动 URL 导入；官方源 adapter 后置。

---

## 0. 一句话

> **金融查证 Agent：从文档、表格、官方数据里查证每一个结论；固定语料做可复现评测，官方来源做产品能力，答案永远回到本地原文证据。**

不是只会聊天的金融问答机器人，而是以**查证、核对和证据回查**为核心的金融 Agent。查证是能力上限：能查证，就一定能问答；能问答，不一定能查证。

---

## 1. 为什么换方向

### 1.1 制度问答的问题

- 作为产品：红海，面试官一年看几十个“企业知识库问答”
- 作为作品：交付物是一段聊天，用户无法验收
- 作为评测载体：语料短、结构简单、没有表格/页码/跨文档，测不出硬能力

### 1.2 GitHub 证据（2026-10-27 扫描）

| 方向 | 头部项目 | 星 | 结论 |
|---|---|---|---|
| contract review | `neo4j-product-examples/graphrag-contract-review` | 122 | 官方示例撑起来的，不是域火 |
| 其余合同/招投标/财报问答 | 一批 | 0–4 | 全是学生 demo |
| security questionnaire | `QResponder` | 6–8 | 商业需求真实，开源稀疏 |
| **agent evaluation** | Rogue / NVIDIA SkillEvaluator / awslabs | 1071 / 605 / 375 | 高星在基础设施 |
| **金融** | OpenBB / FinGPT / FinRobot / ai-hedge-fund | 74.8k / 21.5k / 8.3k / 64.3k | 有生态、有 benchmark、有买家 |
| **金融长文档 QA** | FinRag / AFAC baseline / FinRAGBench-V | 13 / 22 / 24 | 有公开 benchmark，可复现 |

结论：**域不是壁垒，机制才是。** 金融是少数同时具备“真实买家 + 公开 benchmark + 可验证数字”的域。

---

## 2. 目标与非目标

### 2.1 目标

1. 在固定公开语料上跑可复现的金融查证评测（断网、可回放）
2. 支持三种核心模式：**查数、核条款、跨文档对比**
3. 每个结论带证据：`doc_id + 页码 + 章节 + 表格/单元格 + 原文片段`
4. 每个数字带计算过程：公式、输入、结果
5. 证据不足时拒答或标 `needs_review`，不猜
6. 官方来源发现新文档：先下载、快照、解析、入库，再回答

### 2.2 非目标（v1 明确不做）

- 股票预测、交易信号、投资建议
- 实时行情、Level-2、分钟级数据
- 通用全网爬虫、搜索引擎
- 全量财报数据库
- 自动替代律师/审计/分析师下结论
- 多租户 SaaS、计费、企业 SSO

---

## 3. 叫法与定位

**主叫法：金融查证 Agent**

- 不叫“金融文档 Agent”：不锁输入，文档只是第一个证据源
- 不叫“金融长文档 Agent”：长文档是主场景，不是边界
- 不叫“金融分析 Agent”：分析可以拍脑袋，查证必须有证据
- 不叫“金融问答”：那是红海

一句话定位：

> 从文档、表格、官方数据里查证每个结论；财报、募集书、条款是第一批场景。

---

## 4. 产品形态

### 4.1 用户流程

```text
上传/选择文档（或粘贴官方公告 URL）
  → 提问或选择核对任务
  → Agent 定位章节/表格、取数、计算、跨文档对齐
  → 输出：结论 + 证据 + 计算过程 + 状态（answered / abstained / needs_review）
  → 人工 accept / edit / 标缺证据
  → 导出核对报告 / 证据包
```

### 4.2 三种模式

| 模式 | 示例 | 输出 |
|---|---|---|
| 查数 | “2025 年营业收入是多少？同比多少？” | 数值 + 页码/表格 + 公式 |
| 核条款 | “募集书里的担保条款和年报是否一致？” | 差异表 + 条款引用 |
| 跨文档对比 | “两年年报的应收账款政策有什么变化？” | 变化清单 + 证据 |

### 4.3 输出 schema

```json
{
  "status": "answered | abstained | needs_review",
  "answer": "结论文字",
  "claims": [
    {
      "text": "2025 年营业收入为 12.34 亿元",
      "unit": "亿元",
      "currency": "CNY",
      "scope": "consolidated",
      "restated": false,
      "evidence": [
        {
          "doc_id": "cninfo-000001-2025-annual",
          "page": 42,
          "section": "第二节 公司简介和主要财务指标",
          "table_id": "T3",
          "table_caption": "主要会计数据",
          "row": "营业收入",
          "column": "2025",
          "cell_text": "1,234,000,000.00",
          "bbox": [72.0, 312.5, 410.2, 330.1],
          "quote": "营业收入 1,234,000,000.00"
        }
      ],
      "calc": {
        "formula": "(本期 - 上期) / 上期",
        "inputs": ["2024: 10.00 亿", "2025: 12.34 亿"],
        "result": "23.4%"
      }
    }
  ],
  "as_of": "2026-10-27",
  "trace": []
}
```

`table_id + cell` 只是抽取器内部编号，不能作为唯一证据定位；**表格标题、行名、列名、单元格原文、页码必须同时保存**，`bbox` 用于高亮回查。这样即使表格抽取版本变化，证据仍能定位。

---

## 5. 语料方案

### 5.1 两层结构（已拍板）

| 层 | 用途 | 数据 | 规则 |
|---|---|---|---|
| **第一层：固定评测语料** | 可复现评测 | 12–40 份真实公开文档 | 本地保存、冻结 manifest、断网评测 |
| **第二层：官方来源发现** | 产品能力 | 交易所/证监会/公司 IR/基金公司 | 白名单、下载快照、解析入库后再回答 |

### 5.2 第一层：固定语料

**来源**

- 上海证券交易所、深圳证券交易所、北京证券交易所公告
- 中国证监会及派出机构公开文件
- 上市公司官网投资者关系页面
- 年报、半年报、募集说明书、监管问询回复
- 基金公司公开合同和产品说明书

**每份文档必须记录**

```json
{
  "doc_id": "cninfo-000001-2025-annual",
  "source": "cninfo",
  "title": "XX公司2025年年度报告",
  "url": "https://...",
  "company": "XX股份",
  "code": "000001",
  "doc_type": "annual_report",
  "period": "2025",
  "announcement_date": "2026-04-20",
  "published_at": "2026-04-20",
  "fetched_at": "2026-10-27T10:00:00+08:00",
  "currency": "CNY",
  "unit": "元",
  "scope": "consolidated",
  "restated": false,
  "negative_format": "parentheses",
  "footnotes": [],
  "content_type": "application/pdf",
  "size": 1234567,
  "sha256": "...",
  "parser_version": "pymupdf4llm-x.y",
  "tree_version": "v1",
  "effective_from": null,
  "effective_to": null
}
```

`effective_from/effective_to` 只用于政策、条款、制度类文档；年报以 `period + announcement_date` 为主，不要硬套生效日。数值还必须记录币种、单位、合并/母公司口径、是否重述和脚注，否则“12.34 亿”和“1,234,000,000 元”会被当成两个答案。

**初始规模建议**

- **v1 锁死**：3 家公司 × 2024/2025 年报 = 6 份 + 1 份募集书 = 7 份文档
- 跑通后再扩到 20–40 份；文档数量不是关键，**题量和题型才是**
- 30 条题，6 个维度各 5 条（见 §7）

**存储纪律**

- 仓库只放：下载脚本、URL、manifest、hash、少量证据片段
- 不把整批 PDF 提交进 git
- 冻结后不逐题改；改题必须升版本号

### 5.3 第二层：官方来源发现

**v1 范围（锁死）**

1. **手动 URL 导入**：用户粘贴官方公告 URL → 下载 → 解析 → 入库
2. **暂不做自动官方源 adapter**：放到第二阶段（约 +1–1.5 天），避免被站点结构和反爬拖住
3. **搜索 API 兜底**：更后，只在产品化时考虑

**铁律**

- 搜索只做“发现”，网页摘要永远不进答案
- 先下载、hash、解析、入库，再从本地快照回答
- 评测模式 `NETWORK_ENABLED=0`；产品模式才联网
- 不做通用爬虫、不绕验证码/登录、不限速乱抓

---

## 6. 技术架构

### 6.1 复用现有代码

| 现有模块 | 复用方式 |
|---|---|
| `app/agent/runner.py` | 保留多轮/重试/自检；增加 `toolset` 参数，注入金融工具 |
| `app/tools/retrieval.py` | 保留 `search_kb` / `get_citation` 作为本地检索 |
| `evaluation/agent_checks.py` | 新增金融检查（页码、表格、计算 trace） |
| `evaluation/agent_judge.py` | 复用 judge；增加数值/证据判据 |
| `evaluation/agent_report.py` | 复用维度聚合、pass@1/pass^3、badcase |
| `evaluation/run_agent_eval.py` / `score_agent_runs.py` | 参数化任务文件与工具集 |
| `evaluation/compare_agent_reports.py` | 复用双变体对比 |

### 6.2 新增模块

```text
app/finance/
├── ingest.py          # PDF → sections/tables + manifest
├── tree.py            # 标题树抽取与补全
├── store.py           # 本地文档库读取（section/table）
└── calc.py            # Decimal 受限计算
app/sources/
├── base.py            # SearchHit / DocumentBlob / OfficialSource
├── manual.py          # 手动 URL 导入
├── cninfo.py          # 巨潮 adapter（首个官方源）
└── registry.py        # 白名单、启停、限流
app/tools/finance.py   # read_section / get_table / calculate / search_official
evaluation/finance_corpus.py
evaluation/finance_tasks.jsonl
```

### 6.3 数据模型

```text
Document {doc_id, title, company, code, doc_type, period, announcement_date,
          published_at, currency, unit, scope, restated, negative_format,
          page_count, tree_version}
Section  {section_id, doc_id, path, page_start, page_end, text}
Table    {table_id, doc_id, section_id, page, caption, columns, rows,
          footnote, bbox}
Evidence {doc_id, page, section, table_id, table_caption,
          row, column, cell_text, bbox, quote}
Task     {id, dimension, question, answer, claims, citations, calc,
          should_abstain, max_steps, unit, currency, scope, restated}
```

### 6.4 检索

1. 解析阶段**离线**抽标题树；opaque heading 用一次 LLM 补全（参考 FinRag），结果落盘并写 `tree_version`
2. 查询时只读冻结的标题树，**运行时不在线建树/补树**——否则同一份文档两次运行可能路由到不同章节，评测不可复现
3. 查询时先让 LLM 在标题树上选 3–5 个候选章节
4. 候选章节内走 BM25；不足时回落全局 BM25/向量
5. 需要数字时读表格，不让 LLM 从正文猜

**A/B 对照**：chunk RAG（现有）vs 结构感知路由（新增）。同一批题、同一模型、同一预算。

### 6.5 工具面

| 工具 | 作用 |
|---|---|
| `search_local(query)` | 本地章节/表格检索 |
| `read_section(section_id)` | 读章节原文 |
| `get_table(table_id)` | 读整表 |
| `get_cell(table_id, row, col)` | 读单元格 |
| `calculate(expression, inputs)` | 受限计算，Decimal，记录公式 |
| `search_official(query, filters)` | 官方源发现，只返回候选元数据 |
| `fetch_document(hit_id)` | 下载/快照/解析/入库 |
| `get_citation(chunk_id)` | 现有回查 |

### 6.6 Agent loop

```text
search_local → 证据不足？→ search_official → fetch_document
→ 回本地 search_local / read_section / get_table / calculate
→ 终轮覆盖自检（数值是否都有证据/公式；子问题是否都覆盖）
→ 输出 claims + evidence + calc + status
```

复用现有 improved 的机制：多轮、空/错重试、覆盖自检；新增“数值自检”：每个数字必须有 `evidence` 或 `calc`，否则降级为 `needs_review`。

### 6.7 时效

- 每个来源带 `published_at / fetched_at`；政策、条款、制度类再带 `effective_from / effective_to`；年报用 `period + announcement_date`
- 回答必须带 `as_of`
- 新旧冲突按生效日取最新；同日冲突转 `needs_review`
- 超过 TTL 标 `stale`，触发刷新，不拿旧数据硬答

---

## 7. 评测方案

### 7.1 六个维度（30–50 条）

| 维度 | 考什么 | 示例 |
|---|---|---|
| 定位与引用 | 章节/页码/表格定位 | “这个数在年报第几页哪张表？” |
| 数值与计算 | 取数、单位、公式、精确匹配 | “毛利率同比变化？” |
| 跨文档对比 | 两年年报/募集书 vs 年报 | “担保条款差异？” |
| 时效与版本 | 新旧规定、生效日、过期检测 | “2024 年当时的标准？” |
| 拒答与边界 | 无证据/超范围 | “公司明年营收预测？” |
| 恢复与澄清 | 解析失败、空表格、缺年份 | “表格抽取失败后怎么办？” |

### 7.2 任务 schema

```json
{
  "id": "F001",
  "dimension": "数值与计算",
  "question": "2025 年营业收入是多少？同比多少？",
  "answer": "12.34 亿元，同比 +23.4%",
  "claims": [
    {
      "text": "营业收入 12.34 亿元",
      "unit": "亿元",
      "currency": "CNY",
      "scope": "consolidated",
      "restated": false,
      "citations": [
        {
          "doc_id": "cninfo-000001-2025-annual",
          "page": 42,
          "table_id": "T3",
          "table_caption": "主要会计数据",
          "row": "营业收入",
          "column": "2025",
          "cell_text": "1,234,000,000.00",
          "bbox": [72.0, 312.5, 410.2, 330.1]
        }
      ],
      "calc": {"formula": "(本期-上期)/上期", "inputs": ["10.00", "12.34"], "result": "23.4%"}
    }
  ],
  "should_abstain": false,
  "max_steps": 4,
  "difficulty": "medium",
  "source": "manual"
}
```

### 7.3 指标

- 数值精确匹配（**币种/单位/合并口径/是否重述/括号负数/千分位/百分号**归一后，允许明确规定的四舍五入）
- 引用准确率（页码 + 表格标题 + 行名 + 列名 + 单元格原文都能对上）
- 计算 trace 完整率
- 答案正确率（judge 只判叙述，不判数字）
- 拒答 precision / recall（分开报）
- `pass@1`（任务均值）、`pass^3`（三次全过）
- 成本：token/次、中位/最大延迟
- 结构感知 vs chunk RAG 的整卷配对 McNemar

**纪律**：分维度只描述，不做 p 值；整卷配对才做显著性。

### 7.4 验收判据

**目标（验收方向，不是早期结论）**

1. 结构感知路由在 30 条题上比 chunk RAG 高 **≥10 个百分点**；若没有，写否定结论
2. 数值精确匹配 ≥90%
3. 引用准确率 ≥90%
4. 拒答 precision ≥90%，误拒 ≤10%
5. `pass^3` 有数，成本差有数
6. 所有数字能指到报告，不手写

**硬门禁（v1 从第一天就执行，不随样本量放宽）**

1. 编造数字 = 0
2. 每个结论必须有 evidence（页码/表格/原文）
3. 每个 calc 必须有 formula + inputs + result
4. status 与输出 schema 合法（answered / abstained / needs_review）
5. 运行崩溃 = 0

> 30 条题、6 个维度，每维只有 5 条；上面的 90% / +10 点只作为目标，不能一开始当结论。真正的门禁只保留确定性、零容忍项。

---

## 8. 实施计划（v1：5–6 天；官方源另算）

| 阶段 | 时间 | 交付物 | 验收 |
|---|---|---|---|
| 0. 冻结口径 | 0.5 天 | v1.1 方案 + 3 家公司 + 1 份募集书清单 | 范围锁死：6 份年报 + 1 份募集书 + 30 题 |
| 1. 固定语料 | 1–2 天 | 7 份 PDF + manifest + 解析 QC（先 QC 3 份） | 标题树/表格/页码可用；tree_version 落盘 |
| 2. 工具层 | 已完成 | `read_section/get_table/get_cell/calculate` + 单测 | 11 条测试通过 |
| 3. 评测集 | 1 天 | 30 条题（6 维 × 5）+ 判分 | lint 0 error；baseline 能跑 |
| 4. 结构检索 | 1 天 | 标题树路由 + A/B | 同批题对比有数 |
| 5. 手动 URL 导入 | 0.5–1 天 | 下载→hash→manifest→解析→入库 | 单文档闭环；**不做自动官方 adapter** |
| 6. 报告/叙事 | 0.5–1 天 | 报告、README、面试口径 | 每个数字可指到报告 |

**v1 锁死**：3 家公司、2024/2025 年报、1 份募集书、30 条题、手动 URL 导入。自动官方源 adapter 放到第二阶段（+1–1.5 天），先验证核心价值，不被站点结构和反爬拖住。

**先做 7 份、30 条题，跑通再加到 20–40 份。** 不要一开始铺 40 份文档再写题。

---

## 9. 风险与边界

| 风险 | 处理 |
|---|---|
| 表格抽取错 | 选有文本层的 PDF；前 3 份人工 QC；表格题先只做规则网格 |
| 解析结果不稳定 | 固定 parser 版本；存 parser_version + hash |
| 标题树运行时漂移 | 离线构建、落盘、版本化；运行时只读，不在线补树 |
| 版权/体积 | 仓库只放 URL/hash/片段；不提交整批 PDF |
| 数据源不可用 | 手动下载兜底；不做验证码绕过 |
| 过拟合 | 题集冻结；按失败类别改，不逐题补 |
| 金融责任 | 定位为“查证/核对助手”，默认 `needs_review`；不构成投资建议 |
| 范围膨胀 | 不做交易/预测/实时行情/通用爬虫 |

---

## 10. 参考项目（可借鉴什么）

| 项目 | 星 | 借什么 |
|---|---|---|
| `openbq-org/OpenBB` | 74.8k | 金融数据平台结构、指标组织 |
| `virattt/ai-hedge-fund` | 64.3k | 多 Agent 编排（不学交易） |
| `AI4Finance-Foundation/FinGPT` | 21.5k | 金融文本任务与数据 |
| `AI4Finance-Foundation/FinRobot` | 8.3k | 金融 Agent 分层 |
| `AsyncBuilds/FinRag` | 13 | **结构感知检索**：FinanceBench 64.4%→82.0%，10-K 91% |
| `li-xiu-qi/afac2026-financial-qa` | 22 | 中文 AFAC 任务组织：5 类长文档、100 题 |
| `TokaiTeio3/financial-document-agent` | 0（B 榜 93.11） | 原子约束、BM25、动态补证、受限计算、证据审计 |
| `zhaosuifeng/FinRAGBench-V` | 24 | 多模态 RAG + 视觉引用 benchmark |
| `Rabyte-Technology/Investment-Research-Agent-Benchmark` | 13 | 中国语境买方投研任务集 |

**引用纪律**：FinRag 的 64.4%→82.0% 是它的结果，不是你的。你只能写“参考结构感知路线，在自己的题集上从 X 到 Y”。

---

## 11. v1 默认口径（已按评审锁死）

1. **首批公司/文档（2026-10-28 已落地）**：美的集团（000333）、比亚迪（002594）、贵州茅台（600519）各 2024/2025 年报 = 6 份；第 7 份为天顺风能（002531）2025 年度向特定对象发行 A 股股票募集说明书（注册稿）。
   - 募集书来自第 4 家公司：美的/比亚迪/茅台近十年没有 A 股募集说明书（巨潮 `category_zf_szsh`/`category_pg_szsh` 实测 0 条），这是清单唯一一处与"3 家公司"不一致的地方，事先没得选。
   - **单位是逐表的，不是逐文档的**：美的年报主表是千元、比亚迪/茅台是元、天顺募集书是万元。文档级 `unit` 只代表主要财务报表口径，取数一律看 `table.unit`。
2. **中文为主**：先做中文年报；FinanceBench（英文）作为可选外部 benchmark，不混入主剖分。
3. **产品形态**：先 CLI + JSON/Markdown 报告，再做 Web 工作台。
4. **旧制度题库**：归档为回归/负样本，不再作为主线。
5. **官方源 adapter**：v1 不做，只做手动 URL 导入；第二阶段 +1–1.5 天。

---

## 12. 当前进度

### 12.1 已完成（2026-10-28）

**语料（Phase 1 完成，7/7 解析成功）**

| doc_id | 公司 | 类型 | 期间 | 公告日 | 页 | sections | tables |
|---|---|---|---|---|---|---|---|
| cninfo-000333-2025-annual | 美的集团 | 年报 | 2025 | 2026-03-31 | 276 | 205 | 96 |
| cninfo-000333-2024-annual | 美的集团 | 年报 | 2024 | 2025-03-29 | 295 | 208 | 108 |
| cninfo-002594-2025-annual | 比亚迪 | 年报 | 2025 | 2026-03-28 | 268 | 557 | 172 |
| cninfo-002594-2024-annual | 比亚迪 | 年报 | 2024 | 2025-03-25 | 290 | 579 | 193 |
| cninfo-600519-2025-annual | 贵州茅台 | 年报 | 2025 | 2026-04-17 | 143 | 542 | 254 |
| cninfo-600519-2024-annual | 贵州茅台 | 年报 | 2024 | 2025-04-03 | 143 | 510 | 256 |
| cninfo-002531-2025-prospectus | 天顺风能 | 募集书 | 2025 | 2026-10-27 | 105 | 241 | 35 |

- 清单与 hash：`docs/finance/corpus_manifest.json`（URL / 公告日 / sha256 / 页数 / parser_version / tree_version）
- 解析 QC：`docs/finance/corpus_qc.json`；数值验收：`docs/finance/corpus_verify.json`
- PDF 与 `document.json` 在 `data/finance/`（已 gitignore，不入库）

**代码**

- ✅ `app/finance/tree.py`：标题树（正则优先、字号只做否决；目录页按"同页 ≥3 个章标题"整页跳过；`第X节` 与标题分行时合并）
- ✅ `app/finance/ingest.py`：PDF → sections/tables；表头 ±1 列对齐、表单位推断、碎片跨页合并
- ✅ `backend/scripts/fetch_finance_corpus.py`：幂等下载 + sha256 + 声明大小校验（>5% 偏差判失败）
- ✅ `backend/scripts/ingest_finance_corpus.py`：批量解析 + 回填 manifest + 生成 QC
- ✅ `backend/scripts/verify_finance_corpus.py`：语料验收（取「营业收入」本期/上期 → `calculate` 算同比 → 与报告自报值比）
- ✅ 5 条解析测试（合成 PDF，离线）；后端全量 **205 passed**

**验收结果**

- 正文覆盖率（section 正文 + 表格单元格 / PDF 原始字符）：**0.847–0.942**
- 6/6 年报「营业收入同比」与报告自报值一致（最大偏差 < 0.01 个百分点），且每条都带页码：
  - 美的 2025 p9：12.109% vs 自报 12.11
  - 美的 2024 p9：9.438% vs 自报 9.44
  - 比亚迪 2025 p11：3.457% vs 自报 3.46
  - 比亚迪 2024 p10：29.019% vs 自报 29.02
  - 茅台 2025 p6：−1.206% vs 自报 −1.21
  - 茅台 2024 p5：15.712% vs 自报 15.71

### 12.2 已知缺陷（会影响出题，出题时必须避开）

1. **跨页表格只合并碎片**：上一页末尾"表头 + ≤4 行"才接回下一页；整表分页不合并，理由是接错会让 `get_cell(row_name)` 命中另一张表的同名行，产出一个**带页码的错误数字**——查证产品最坏的失败形态。已知具体案例：比亚迪 2024 年报《主要会计数据》在 p10 只有表头 + 营业收入一行，净利润/现金流/每股收益在 p11 另成一张表。
2. **募集说明书的无框线表格抽不出来**：105 页只识别 35 张表，且没有一张含「营业收入」。数值仍在正文里，所以 v1 把它当"文本 + 页码"文档用，不出表格取数题。
3. **表格 caption 缺失率 12%–45%**：贴表格上方的那一行常是勾选题/单位行；宁可为空也不猜。section 的 `path` 能补上下文。
4. **表头抽不出名字的表不少**（>30% 列名是 `colN` 的：美的 35–39 张、比亚迪 42–43 张、茅台 43–48 张）。这些表只能整体读，不能按列名取单元格；出题时不要依赖。
5. **茅台主要会计数据表自身没有"单位"字样**：`table.unit` 为空，`get_table/get_cell` 会回落到文档级 `unit=元` 并标 `unit_source="document"`；看到 `unit_source=document` 就必须按推断处理。
6. **页码 = PDF 物理页（1 起）**，不是报告印刷页码；两者差 1–2 页，证据里必须写清是 PDF 页。

### 12.3 Phase 2 已落地（2026-10-28）：工具集注入 + 一次真实跑通

**代码**

- `app/tools/toolset.py`：工具集契约 `specs() / system_prompt(variant) / call() / to_evidence()`。runner 不认识任何具体工具名。
- `app/tools/finance_set.py`：`FinanceToolset`（`name="finance"`、`search_tool="search_sections"`、`max_tool_rounds=5`）。
  - `search_sections` → `citable=False`（只定位，不进编号证据）
  - `read_section / get_table / get_cell` → 可引用证据，带 `page / unit / table_caption`
  - `calculate` → 进 `result["calc"]`，字段恰好是硬门禁要的 `formula / inputs / result`
- `app/agent/prompts.py`：`FINANCE_BASELINE_SYSTEM / FINANCE_IMPROVED_SYSTEM / FINANCE_FINAL_TEMPLATE`。
- `runner.run(task, variant, toolset=...)`：传 `FinanceToolset` 即切域；result 增加 `toolset / evidence / calc`（schema 向后兼容）。
- 9 条工具集测试（含 3 条回归）；后端全量 **214 passed**。

**跑通过的实例**（美的 2025 营业收入，improved，8 步）

```
list_documents → search_sections ×2 → read_section(s0009) → search_sections
→ get_table(t0006) → get_table(t0007) → calculate((cur-prior)/prior*100)
→ answer: 456,451,731 千元（约 4,564.52 亿元）[2]（PDF 第 9 页）；同比 12.11%
   calc: {"formula": "(current - previous) / previous * 100",
          "inputs": {"current": "456451731", "previous": "407149600"},
          "result": "12.109…"}
```

与 §12.1 的语料验收（12.11%）一致 —— 数字、单位换算、页码、公式全对。

**过程中修掉的三个真 bug（都已加回归测试）**

1. **定位结果对模型不可见**：`search_sections` 的结果不进证据列表，模型只看到"返回 10 条"，拿不到 section_id，于是反复换关键词搜、最后拒答。修法＝定位项单独渲染成"可读取章节"清单，给 id、不给编号。
2. **同一 id 被去重吞掉**：先定位 `doc#s0009`、再 `read_section` 同一节，共用 seen 集合会把原文判成重复丢弃，证据永远为空。修法＝定位与证据分两个 seen 集合；读到原文时把同名定位项撤掉。
3. **只搜不读**：模型连搜 6 次不 `read_section`。修法＝回合消息末尾给明确下一步；`search_sections` 无新增时提示"别再同义检索"；金融工具集把工具轮次上限从 3 提到 5。

**检索打分同时修了**（原先是裸词频，长附录小节靠字数霸榜）

- `FinanceStore.search_sections` 改为：中文二元组 + IDF 加权 + 路径命中 ×3 + 长度归一。
- 章节新增 `table_keywords`（表名 + 首列行标签，来自原文）：正文为空、内容全在表里的小节才能被检索到——否则搜"营业收入"永远找不到主要会计数据表。
- 实测：`主要会计数据和财务指标` → 正确命中 s0009；`美的集团 2025 年营业收入` → 命中「营业收入构成」(p49)。

**待校准**：`max_steps_exceeded` 现在把"模型请求了第 9 次调用但预算只有 8 步"也算超限，会让一条答案正确的运行被确定性检查判失败（本次实测踩到）。出题时把 `max_steps` 留出余量，或后续把"预算内收口"与"请求超额"拆成两个 note。

### 12.4 Phase 3：任务集与分档（2026-10-28 落地）

**先改一个口径**：原计划写的是「6 维 × 5」，落地改成 **5 维 × 6**，理由两条——

1. 五维（检索与引用 / 多步拆解 / 拒答与边界 / 澄清与追问 / 恢复与纠错）是制度库那套
   完全相同的能力剖面。**同一套剖面换成金融语料也能跑**，这本身就是"评测引擎与域解耦"
   的证据；另起第六维就把这个证据毁了。
2. "数值正确性"确实是个独立的失败面，但它不该做成一维去和别的维抢 6 条题：
   它应该**对每一条题都成立**。所以它下沉成横切硬门禁（编造 0 容忍、单位必须与表一致、
   比率必须有公式）＋ 任务卡数值 lint，而不是第六个维度。

#### 12.4.1 分档：一套题集不能同时当尺子和护栏

| 档 | 文件 | 条数 | 用途 | 统计口径 |
|---|---|---|---|---|
| 主集（能力剖面） | `evaluation/finance_tasks.jsonl` | 30（5 维 × 6） | A/B、维度剖面 | 五维各自算，进 overall |
| 压力/回归集 | `evaluation/finance_tasks_stress.jsonl` | 10 | 已知缺陷回归 | **单列一栏**，不进五维剖面 |
| 留出集 | 主集里 `holdout: true` | 6 | 防自己调过拟合 | 调参阶段 exclude，验收 only |

为什么压力集必须单列：这 10 条全是"已知有坑"的题（分季度表列错位、跨页碎片表、
正文为空只有表、千元与元相加……）。混进主集，五维剖面就变成"防御力分数"，
A/B 也说不清到底改善了什么——它测的是同一批缺陷有没有复发，不是能力水平。

每条压力题带 `defect` 字段写明对应哪个缺陷，`source: "failure"`。

#### 12.4.2 主集 30 条测什么

| 维度 | 条数 | 测的能力 | 典型题 |
|---|---|---|---|
| 检索与引用 | 6 | 单跳取数 + 页码 + 单位 | 美的 2025 营收 / 比亚迪 2024 研发占比 |
| 多步拆解 | 6 | 跨表、跨文档、跨年、换算、口径对比 | 净利率两年变化、三家公司营收排序、研发投入 vs 研发费用 |
| 拒答与边界 | 6 | 该拒就拒、错前提纠错、单位陷阱 | 格力（语料无）、2026Q1（期间外）、营收"下降 12.11%"（错前提） |
| 澄清与追问 | 6 | 该问就问、不该问就别问 | "美的营收"（缺年份）vs "比亚迪 2025 年营业收入"（明确，反问即错） |
| 恢复与纠错 | 6 | 空结果/超时/500/截断后能否重取 | 注入 `empty`/`timeout`/`500`/`truncated` |

出题约束：只在 §12.2 那六条解析缺陷之外出题；`max_steps` 给 16–20（实测一条两跳题 9 步）。

#### 12.4.3 数值纪律：任务卡的数字必须能按回 PDF

这是金融任务集最危险的静默错误：任务卡把期望值写错，判分器就拿错值去判，
**Agent 答对了反被判失败，而且没有任何地方会报错**。所以
`evaluation/lint_finance_tasks.py` 除了复用制度库那套结构检查，还做四条硬检查：

| 检查 | 防什么 |
|---|---|
| `answer_points[].evidence = {table_id,row,column,value}` 且值与表格 `cell_text` 一致 | 抄错数字 / 抄错行 |
| `unit` 与表格单位一致，不一致必须写 `unit_note` | 千元当元 |
| `derived: true` 的要点必须带 `calc`，公式真跑一遍与 `expected` 同（默认容差 0.02） | 手算 / 拍脑袋填比率 |
| 压力集必须写 `defect` | "这题在测什么"说不出来 |

当前状态：主集 **0 error**，1 条 warning（美的/比亚迪主表各被 5 条题引用，到上限）。

#### 12.4.4 留出集纪律

6 条（FIN-05 / 10 / 16 / 21 / 27 / 30，每维都有一条）。规则只有一条：
**调 judge prompt、调检索权重、改提示词的阶段，这 6 条一律不跑；最后验收跑一次。**

跑与判两侧都有开关，不靠自觉：

```bash
python -m evaluation.run_finance_eval --holdout exclude   # 日常（默认）
python -m evaluation.score_agent_runs --corpus finance --holdout exclude
python -m evaluation.run_finance_eval --holdout only      # 最后验收
```

配套的诚实边界：n=30 时 Wilson 半宽 ±11–17pp（15/30 → [0.332,0.668]）。
所以**不要用两个边际区间比 A/B**——同批题上 baseline 过、improved 不过（b）
与反向（c）用 McNemar 精确检验，10 条翻转 0 条反向时 p≈0.001。

#### 12.4.5 judge 正确性：把"一致率"补成"准确率"

报告里现成的 `mean_self_agreement` 只说明"同一个 judge 多跑几遍结论稳不稳"。
它答不了"judge 判得对不对"，而这两件事可以背离：一个稳定的错判者一致率能到 80%、
准确率只有 55%，**看起来比不稳的 judge 更可信**。

新增 `evaluation/judge_audit.py`，两步：

```bash
# 1) 生成标注表（人工填 human_pass = true / false / skip）
python -m evaluation.judge_audit --report <报告.json> --tasks evaluation/finance_tasks.jsonl \
    --out %TEMP%\judge_audit.jsonl
# 2) 标完比对：准确率 / 判松 / 判严 / 分维度
python -m evaluation.judge_audit --compare %TEMP%\judge_audit.jsonl
```

**判松（judge 放过、人工判错）是最要命的那个数**：报告高估 Agent 能力就高估在这里。
验收要求：主集至少标 60 个点，判松率单独写进报告，不和一致率合并成一个数。

**位置偏差（2026-10-28 补）**：本项目的 judge 是 pointwise，偏差不表现为"A 位/B 位"，
而是**先出现的要点、先给出的原文更容易被判 pass**。做法：`agent_judge._reverse_for()`
按 `(task_id, run_index, 重复序号)` 做确定性散列，决定这一轮正序还是倒序；
**只换展示顺序、不换 index 标签**，判分结果照样按 index 归位，不多花一次调用。
重复判分（`--judge-repeats ≥2`）时正反各来一次，顺序记进 verdict 的 `judge_orders`，
以后可以直接比 forward 与 reversed 的 pass 率差——差值就是位置偏差的量级。

#### 12.4.6 信号 → 优化通道（评测不是为了打分）

| 信号 | 根因 | 通道 | 本项目的具体动作 |
|---|---|---|---|
| 某维 N/A 占比高 | judge 挂了 | 评估器 | 调 `JUDGE_MAX_TOKENS` / 重试 / 换判分模型 |
| `retrieval_hit` 反复 false | 召回 | 检索 | `search_sections` 权重、`table_keywords` 覆盖、query 改写 |
| `citation_scope` / 引用正确率低 | 呈现 | 工具返回 | `read_section` 表 id 前置、`get_cell` 引用串补页码与单位 |
| `fabrication` 门禁红 | 约束 | 提示词 | `FINANCE_RULES` 加"只用单元格数字"、禁替代口径 |
| `参数错误` / `预算超限` | 工具与预算 | schema / 预算 | 工具参数描述、`max_tool_rounds`、`max_steps` |
| pass@1 高但 pass^k 低 | 稳定性 | 提示词 / 温度 | 消歧表述、降温度、固定工具顺序 |
| 压力集通过率掉 | 回归 | 修 bug | 按 `defect` 定位，修完重跑这 10 条 |
| 业务效果类指标 | —— | —— | **本项目不适用**（没有线上流量，别照搬线上 A/B 那套） |

#### 12.4.7 变更绑定回归：改了什么就必须重跑什么

不跑全量是常态，但**回归范围要和变更的影响域对齐**：估窄了漏风险，估全了每次都跑一天。
下表是准入规则，不是建议。

| 改了什么 | 必须重跑 | 为什么 |
|---|---|---|
| 提示词（`FINANCE_*_SYSTEM` / `FINAL_TEMPLATE`） | 主集 30（`--holdout exclude`）+ 压力集 10 | 指令遵循是全局的，答法会整体漂移 |
| 检索（打分、`table_keywords`、IDF） | 主集 30 + 压力集 S01/S02/S04/S06 | 召回变了，命中与干扰一起变 |
| 工具 schema / 工具返回格式 | 主集 30 全套 | 模型对返回结构的依赖最强 |
| 解析器（`tree.py` / `ingest.py`，重解析语料） | 全部 + **重跑任务集 lint**（数值按新 `document.json` 复核）+ 语料验收 | 页码与单元格可能整体漂移，任务卡会静默对不上 |
| judge（ prompt / 判分模型 / `max_tokens`） | **不重跑 Agent**，只重判（`--reuse-judgments`）+ 重新跑 `judge_audit` | judge 换了，所有历史结论的可信度都变 |
| 换模型 / 调温度 | 主集 30 × k≥3（pass^k 才成立）+ 最后留出集 `only` | 模型替换是全局变更 |
| 只改报告 / 指标算法 | 不重跑，只重算分（`--no-judge` 或复用 judge） | 答案没变，变的是读数方式 |

一票否决（任一即阻断）：`fabrication > 0`、压力集通过率下降、judge 判松率上升。

#### 12.4.8 命令清单

```bash
# 出题/改题后先过 lint（0 error 才算题卡可用）
python -m evaluation.lint_finance_tasks
python -m evaluation.lint_finance_tasks --tasks evaluation/finance_tasks_stress.jsonl --tier stress

# 跑 + 判（只跑不判 → 判分脚本）
python -m evaluation.run_finance_eval --variant improved --runs 3 --out %TEMP%\fin_runs.json
python -m evaluation.score_agent_runs --corpus finance --runs %TEMP%\fin_runs.json \
    --tasks evaluation/finance_tasks.jsonl --out %TEMP%\fin_report.json
```

#### 12.4.9 首跑记录（2026-10-28，1 条 smoke）

`FIN-01` improved：answered，9 步，15.2s，答案 `456,451,731 千元`
（≈4,564.52 亿元）、同比 12.11%、带页码；judge 判 `检索与引用 1/1`，
引用正确率 2/2，有据可依 1/1，`fabrication` 0。数据源 `finance` 语料 3956 chunk。

已知口径问题：`latency_median_ms / latency_max_ms` 两条硬门禁是制度库示例阈值
（3000 / 6000ms），金融一条题 15s，**必然红**。它们已标"未校准·不拦截"，
下一步按 baseline 重设，不要当成产品不合格。

#### 12.4.10 复杂题集：Agent vs 普通 RAG 的预注册（2026-10-28，跑前固定）

要回答的问题只有一个：**Agent 多花的 token，是否买到了足够的查证质量提升？**

**预注册纪律**（先写死，再跑；跑完不许改题改阈值）：

| 项 | 固定值 |
|---|---|
| 题集 | `evaluation/finance_tasks_complex.jsonl`，12 题 = 4 桶 × 3（跨表 / 跨文档 / 计算 / 证据不足） |
| 文件 sha256 | `32cc6ad8089400ad1a3317f1c4a675380ea2eecf26205f04b1c45b83a1ac94f7` |
| 判据 1 | 复杂题通过率提升 **≥15 个百分点** |
| 判据 2 | 每题增量 token **≤20,000**（Agent − RAG，不是 Agent 总量） |
| 硬门禁 | 编造（`forbidden_hit` / `grounded=false`）、引用硬错误（标记无法解析 / 引到非证据 / 引用不支持要点）、运行崩溃 → 该题直接不通过，单独计数，不被平均分掩盖 |

为什么不用主集 30 条：里面多步题只有 6 条，15 个百分点 = 0.9 题，分辨率不够。
12 条上 15pp ≈ 1.8 题，仍然粗，所以结论同时报 Wilson 区间与逐题翻转。

**两臂定义**（同一模型、同一语料、同一问题、逐题配对）：

| | Agent 臂 | RAG 臂 |
|---|---|---|
| 切片 | 章节 + 整张表（结构单元，3956） | 512 字固定窗 + 20% 重叠（4115 段，表格拍平成文字） |
| 工具 | `search_sections` / `read_section` / `get_table` / `get_cell` / `calculate` | 无（检索由流水线做，一次 `search_chunks`） |
| 轮次 | 最多 5 轮工具 + 收口 + 补算 | 检索一次 → 生成一次 |
| 检索 | IDF + 路径加权（表内单元格可精确取） | bge-small-zh 向量 + BM25，RRF 融合；按公司元数据过滤；top-8 |
| 计算 | `calculate` 留痕、可复算 | 模型心算，不许写公式 |

RAG 臂**不是稻草人**：向量 + 关键词混合、metadata 过滤、top-8 片段（≈2.5k token）
都是正常配置；它真正缺的是"表格结构 + 多轮重取 + 可复算"。

命令：

```bash
python -m evaluation.run_finance_eval --tasks evaluation/finance_tasks_complex.jsonl --variant improved --runs 1 --out %TEMP%\agent_cx.json
python -m evaluation.run_rag_eval --tasks evaluation/finance_tasks_complex.jsonl --runs 1 --out %TEMP%\rag_cx.json
python -m evaluation.score_agent_runs --corpus finance --tasks evaluation/finance_tasks_complex.jsonl --runs %TEMP%\agent_cx.json --out %TEMP%\agent_cx_report.json
python -m evaluation.score_agent_runs --corpus finance --tasks evaluation/finance_tasks_complex.jsonl --runs %TEMP%\rag_cx.json --out %TEMP%\rag_cx_report.json
python -m evaluation.compare_arms --agent-report %TEMP%\agent_cx_report.json --agent-runs %TEMP%\agent_cx.json \
    --rag-report %TEMP%\rag_cx_report.json --rag-runs %TEMP%\rag_cx.json \
    --tasks evaluation/finance_tasks_complex.jsonl --out %TEMP%\compare_cx.json
```

#### 12.4.11 第一轮配对结果（2026-10-28，k=1）

⚠️ k=1、n=12：以下是**方向性读数**，不是能力结论。Wilson 区间宽 ±20pp 以上。

| | Agent | RAG 基线 | 差 |
|---|---|---|---|
| 任务级通过（评分契约：要点 + 锚点 + 硬门禁） | 5/12 = 41.7% [19.3, 68.0] | 3/12 = 25.0% [8.9, 53.2] | **+16.7pp** |
| 语义全对（只看 judge 要点，不看引用锚点） | 9/12 = 75.0% | 7/12 = 58.3% | **+16.7pp** |
| 要点级正确率 | 38/43 = 88.4% | 27/43 = 62.8% | +25.6pp |
| 引用精度（被引段落是否支撑要点） | 95.4% | 69.8% | +25.6pp |
| 命中硬门禁的题 | 2 | 6 | −4 |
| token/题 | 45,127 | 3,824 | **+41,303** |
| 模型调用/题 | 7 | 1 | +6 |
| 延迟中位 | 25.2s | 7.6s | +17.6s |
| 每 1 万 token 答对 | 0.09 题 | **0.65 题** | −0.56 |

分桶（语义全对口径；括号内为评分契约口径）：

| 桶 | Agent | RAG | token 增量/题 |
|---|---|---|---|
| cross_table（3） | 2/3（2/3） | 2/3（2/3） | +42,443 |
| cross_doc（3） | 1/3（0/3） | 0/3（0/3） | +31,841 |
| calc（3） | 3/3（2/3） | 2/3（1/3） | +49,785 |
| insufficient（3） | 3/3（3/3） | 3/3（2/3） | +41,144 |

**按预注册判据：不划算。**
- 质量臂：+16.7pp ≥ 15pp，**勉强达标**；但只靠 2 题翻转（RAG 赢 Agent 的 0 题），
  精确 McNemar p=0.50，统计上分不出。
- 成本臂：+41,303 token/题 > 20,000，**超支 2 倍**。每多答对 1 题花 247,820 token。

**这轮暴露的四件事（比结论本身重要）：**

1. **两种口径给出的差值完全一致（+16.7pp），但绝对值差很多**：评分契约（41.7% vs 25.0%）
   把"引用锚点没落在真值表"也算失败，而 RAG 的证据单位是 512 字窗口，
   经常引到"含同一数字的相邻章节"。锚点检查对 RAG 结构性偏严——**引用准确率要按语义判
   （引用精度 95% vs 70%），不能只看锚点命中**。
2. **Agent 的优势不在"多过几题"，在"少翻车"**：硬门禁 2 vs 6、引用精度 95% vs 70%、
   要点级 88% vs 63%。任务级全对才算过把 6 要点题变成全或无：CX-01 五对一错
   （9.08 vs 9.09 的舍入）整题判败。**下一步要么减要点数，要么引入要点级记分**。
3. **翻转的两题都在"多文档 / 计算"**：CX-05（跨 2024/2025 两份年报比增速）与
   CX-09（茅台两率差）。跨表三题两边打平——**"跨表"不是 Agent 的差异化场景，
   至少这一版不是**。CX-04 反而是 Agent 的真失败：只找到 2025 年报就宣称 2024 数据不可得。
4. **成本结构与预期相反**：45k/题里，重取表格与证据回填占大头，不是系统提示词。
   要压到 20k 以内，得同时砍轮次（7→4）和表格重复读取，换个短 prompt 不够。

**简单题对照（RAG 单跑 6 条 single_hop，另算）**：要点级 9/9 全对，
但评分契约只过 3/6——失败全是"引用锚点不在真值表"，不是答案错。
这条支持"简单题走 RAG"，但前提是把引用检查改成语义判，否则报表会把 RAG 的
正确率压掉一半。

#### 12.4.12 回答闸门（2026-10-29）

第一轮复杂题暴露的四类行为问题，各加了一道**确定性闸门**。闸门不判答案对错，
只判“这一步做了没有”；触发后最多追加一轮补救，补不回来就在 trace 里留痕。

| 闸门 | 触发条件 | 动作 | trace 标记 |
|---|---|---|---|
| 期间不支持 | 问题点名晚于本地语料最新报告期的年份（2026 Q1） | 直接拒答，禁止给相邻期间数字 | `unsupported_period` |
| 跨文档覆盖 | 问题点名 ≥2 家公司，或明确写了两份年报/两个年度，但证据缺文档 | 补检索/补读缺失文档 | `coverage_repair` / `coverage_unresolved` |
| 拒答数值泄漏 | 结论是拒答（无法确定/未披露/不包含），正文却有金额或比率 | 只重写文字，删掉具体数值 | `abstain_rewrite` / `abstain_numeric_leak` |
| 补算 | 答案里的比率在证据原文中找不到，且没有 `calculate` 记录 | 调 `calculate` 后重写 | `calc_repair` |
| 单元格引用 | 答案引用了表级证据且有数值，但没引用任何 cell | 调 `get_cell` 后重写 | `cell_repair` / `cell_citation_missing` |

**轮次语义修正**：`max_tool_rounds` 现在指“工具回合”，收口单独留一轮；
最后一轮用 `tool_choice="none"` 强制出答案。闸门批下的补救轮重新允许工具，
补救完再收口。回合用尽后 fallback 生成的答案，也会在循环外补跑一遍闸门。

**检索预算**：`search_sections` 每条任务最多 5 次；覆盖修复时临时加 2 次。
超预算返回 `unavailable`，逼模型去 `read_section / get_table / get_cell`。

**2026-10-29 冒烟（2 题，各 1 次）**：

- `CX-04`：answered，结论“一致”，4 条单元格引用（两份年报的 `t0006` 行/列都能对上），
  13 步 / 40.5k token / 1 次 `cell_repair`。修复前是同题 5 轮工具后 fallback、
  只读一份年报就判“无法确定”。
- `FIN-15`：abstained，答案不含任何具体数值，8 步 / 21.1k token，
  触发 `unsupported_period`。修复前会补一句 2025 Q1 的 127,838,538 千元。
- 后端全量 **255 passed**。

**边界**：覆盖闸门只到“文档级”，不保证读到文档里的正确表格；CX-04 的
真正修复来自 `cell_repair` 逼出的 `get_cell`——模型借这个动作读到了此前没打开的
2025 年 `t0006`。12 题两臂复跑尚未做，成本增量未知。

### 12.5 下一步

- 12 题两臂复跑（先跑 4 题探针）：验证 §12.4.12 的闸门效果。验收：硬门禁不劣于第一轮；
  单元格命中从 0/24 起量；负样本拒答不再夹带相邻期间数字；记录每题的补救轮与 token 增量
- 主集跑满 30 条 × 3 次（pass^k 需要 k≥3），出第一份五维剖面
- chunk RAG baseline（纯文本切块、无表结构、无 calc）→ 与结构化路由做成对 A/B（McNemar）
- 60 query 工具层回归夹具（与 30 条能力剖面分开维护）
- 手动 URL 导入（`app/sources/`）

### 12.6 历史

- ✅ v1.1 方案文档完成（已按评审收紧定位/阈值/字段/复现规则）
- ✅ 最小工具层：`app/finance/calc.py`、`app/finance/store.py`、`app/tools/finance.py`
- ✅ 12 条金融工具测试；后端全量 200 passed
- ✅ 语料 7 份解析 + 6/6 年报同比自洽验收（§12.2 / §12.3）
- ✅ `toolset` 注入 + 三个真 bug 修复 + 检索 IDF 重写（§12.3）
- ✅ Phase 3 任务集：主集 30 + 压力集 10 + 留出 6；lint 0 error（§12.4）
- ⬜ 未实现：`app/sources/`（官方源）、chunk RAG baseline、60 query 回归夹具

## 附录 A：官方来源发现流程

```text
用户问题
  → search_local
  → 证据不足 && NETWORK_ENABLED
  → search_official(query, company, doc_type, date)
  → fetch_document(top 1–3)
       download → sha256 → manifest → parse → sections/tables
  → search_local / read_section / get_table / calculate
  → claims + evidence + calc + status
```

## 附录 B：不做清单

- 不做通用爬虫/搜索引擎
- 不绕验证码、不抓登录页
- 不用网页摘要当证据
- 不把联网搜索混进固定语料评测
- 不做实时行情/交易/预测
- 不把整批 PDF 提交进 git
