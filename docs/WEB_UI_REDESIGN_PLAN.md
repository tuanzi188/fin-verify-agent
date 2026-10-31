# KnowledgeMind Web 改造方案：从通用 RAG 聊天到金融查证工作台

> 版本：v0.1（草案）  
> 日期：2026-10-30  
> 范围：`frontend/` 与支撑它的后端会话/引用契约；不改 Agent 评测口径  
> 目标：让 Web 页面能真实展示“金融查证 Agent”的差异化能力，而不是通用知识库聊天

---

## 0. 结论

当前 Web 的问题不是“旧”，而是**产品和实现错位**：

- 视觉上像通用 RAG 聊天：上传文档、欢迎页三卡片、气泡回答、引用 Drawer。
- 真正差异化的东西没有成为一等公民：表格行列、页码、单位、脚注、计算过程、跨文档核对、拒答边界。
- 更关键的是：**当前 `/chat` 的 agent 模式没有接金融工具集**，走的是通用 `Toolbox`；引用也只回 `document_id/title/page/content`，没有 `table_id/row/column/unit/calc`。

所以改造顺序不能是“先画右侧证据面板”。必须先冻结数据契约，再让面板有真实数据可渲染。

---

## 1. 现状审计（已核实）

### 1.1 已确认的正面项

- `frontend/src/main.tsx` 已经用 `ErrorBoundary` 包住整个应用，**不再列为待修 P0**。
- 桌面端视觉体系统一：CSS 变量、明暗主题、圆角/阴影/过渡一致。
- 已有行内引用胶囊、悬浮原文预览、引用 Drawer、Agent 轨迹折叠面板。
- 已有空状态、连接状态、加载状态、首次使用引导。
- 前端单测 26 passed。

### 1.2 已确认的问题

| 问题 | 证据 | 影响 |
|---|---|---|
| 移动端不可用 | `ChatSidebar.tsx:38` 固定 `width: 280`；`global.css:223` 的 `.sidebar-collapsed-mobile` 全项目未使用 | 390px 下主内容被压到约 110px，文字逐字换行，Header 溢出 |
| 文档数文案错误 | `ChatBox.tsx:230` 传 `total_chunks`；`WelcomeScreen.tsx:108` 显示“N 篇文档” | 会把 3,956 个片段说成 3,956 篇文档 |
| 引用只是一段通用文本 | `CitationPanel.tsx` 只展示 `document_title/page/content` | 看不到表格行列、单位、脚注、计算 |
| 证据模型缺字段 | `backend/app/models/chat.py:50` 的 `Citation` 只有 `document_id/title/page/paragraph/content/score` | 前端无法可靠渲染表格/单元格/计算 |
| Agent 轨迹缺字段 | `backend/app/services/agent_entry.py:123` 只映射 `step/tool/ok/result_count/latency/args/note` | 看不到读了哪张表、哪些行列、是否新增证据 |
| 金融 Agent 未接 Web | `agent_entry.py:78` 调 `run_agent` 时未传 `FinanceToolset` | Web 的“深度检索”不是金融查证 Agent |
| 引用解析走通用向量库 | `agent_entry.py:87` 用 `app/tools/retrieval.get_citation` | 金融 `chunk_id`（`doc#t0013#...`）不在该库，可能直接丢弃 |
| 统计数据不足 | `backend/app/api/documents.py:397` 只返回 `total_chunks` | 无法显示文档数、公司数、期间、表数 |
| 深度检索开关在普通输入区 | `ChatInput.tsx:119` | 把实验条件暴露给普通用户 |
| `/analytics` 是通用分析页 | `AnalyticsDashboard.tsx` 是文档/对话/任务/图谱统计 | 不能回答“三臂谁更好、成本多少、硬门禁过没过” |

### 1.3 一个必须先讲清的边界

当前数据目录只有解析后的 `document.json`，没有 PDF 原件。因此：

- 可以做“PDF 第 X 页”的页码锚点和解析表格视图；
- **不要承诺 PDF 页面截图/原版 PDF 高亮**，除非后续补 PDF 资产与阅读器。

---

## 2. 目标形态

### 2.1 产品定位

把首页从“知识库聊天”改成“金融查证工作台”：

> 选材料 → 提问 → 看结论 → 点引用 → 验表格/计算 → 看不确定性

产品名可以保留 KnowledgeMind，但工作区标题建议明确为“金融查证台”或“证据工作台”。

### 2.2 两套模式

| 模式 | 路由 | 用户 | 内容 |
|---|---|---|---|
| 查证模式 | `/` | 普通用户 | 问题、结论卡、证据检查器、简化查证过程 |
| 实验模式 | `/lab` | 登录用户（演示与复核） | 三臂对照、trace、token、hard gates、失败案例 |

“深度检索”开关从普通输入区移除，收进 `/lab` 或设置页。普通用户不需要知道 Agent / RAG 的切换。

### 2.3 目标布局

```text
┌────────────────┬──────────────────────────────┬──────────────────┐
│ 材料 / 最近查证  │ 问题 + 结论                   │ 证据检查器        │
│                │                              │                  │
│ 3 家公司        │ 结论卡                       │ 原文 / 表格 / 计算 │
│ 2024 / 2025     │ ├ 要点 1  [t0006 p42]        │ 高亮目标行列      │
│ 7 份文档        │ ├ 要点 2  [t0013 p58]        │ 页码 / 单位 / 脚注 │
│                │ └ 计算过程 / 不确定性          │ 引用链            │
│ 示例问题        │                              │                  │
└────────────────┴──────────────────────────────┴──────────────────┘
```

响应式：

- ≥1280：三栏，280 / 1fr / 420。
- 1024–1279：两栏 + 右侧 Drawer。
- <768：单栏；左侧栏 Drawer，证据检查器 Bottom Sheet。

---

## 3. 数据契约（Phase 0，必须先冻结）

### 3.1 后端现状

当前 `ChatResponse.citations` 是通用引用：

```python
class Citation(BaseModel):
    document_id: str
    document_title: str
    page: Optional[int]
    paragraph: Optional[str]
    content: str
    score: Optional[float]
```

这不够金融证据视图使用，且金融 `chunk_id` 走 `get_citation` 可能取不到。

### 3.2 目标类型（建议）

```ts
type EvidenceKind = 'paragraph' | 'table_rows' | 'table_cell' | 'document'

interface SourceSpan {
  source_id: string
  kind: 'section' | 'table'
  page?: number
  start?: number
  end?: number
  chars?: number
}

interface EvidenceRef {
  evidence_id: string          // block_id / table_id#row#col / section_id
  doc_id: string
  doc_title: string
  company: string
  period: string
  doc_type?: string
  kind: EvidenceKind
  page?: number
  section_id?: string
  path?: string[]
  table_id?: string
  table_caption?: string
  row_indices?: number[]
  column_indices?: number[]
  row_name?: string
  column?: string
  cell_text?: string
  unit?: string
  footnote?: string
  content: string
  source_spans?: SourceSpan[]
}

interface FinanceCitation {
  marker: number
  evidence: EvidenceRef
}

interface CalcRecord {
  formula: string
  inputs: Record<string, number | string>
  result: number | string
  unit?: string
  tolerance?: number
  note?: string
}

interface Claim {
  text: string
  citation_markers: number[]
  calc_index?: number
  uncertainty?: string
}

interface FinanceAnswer {
  status: 'answered' | 'abstained' | 'clarified' | 'error'
  answer: string
  claims: Claim[]
  citations: FinanceCitation[]
  calc: CalcRecord[]
  trace: AgentTraceStep[]
}
```

### 3.3 契约原则

1. **坐标由后端给，前端不解析文本猜行列。**
2. 每个数字要点要么有 `evidence`（单元格），要么有 `calc`（公式+输入），要么明确标 `note`。
3. 单位、页码、表名、脚注必须随证据一起返回；不要让前端从正文里正则提取。
4. 拒答也要返回“查过哪些文档/表、缺什么”，不能只回一句“无法确定”。
5. 所有证据接口必须复用同一个 `UserContext` 权限过滤。

### 3.4 需要新增/调整的接口

| 接口 | 用途 | 关键字段 |
|---|---|---|
| `POST /api/v1/finance/chat` | 金融查证 Agent | `answer`, `claims`, `citations`, `calc`, `trace`, `status` |
| `GET /api/v1/finance/stats` | 首页覆盖范围 | `documents`, `companies`, `periods`, `sections`, `tables`, `blocks`, `chunks` |
| `GET /api/v1/finance/documents` | 文档库 | `doc_id`, `company`, `period`, `doc_type`, `pages`, `tables`, `status` |
| `GET /api/v1/finance/evidence` | 证据检查器 | 按 `evidence_id` 返回 `EvidenceRef`，表格可按 `table_id + rows + columns` 取 |
| `GET /api/v1/lab/runs` | 实验页 | 三臂结果、pass@1/pass^k、hard gates、token/task、trace |

> 不建议把金融字段硬塞进通用 `Citation`。新增 `FinanceCitation` 更清晰，也不会破坏现有 RAG 客户端。

### 3.5 首页统计必须来自接口

`3,956 个结构单元、1,114 张表` 这类数字**不要硬编码在前端**。验收时用 `/api/v1/finance/stats` 对照：

- `documents = 7`
- `companies = 3`（美的、比亚迪、贵州茅台/天顺风能按语料实际）
- `periods = 2024/2025`（按语料实际）
- `sections`、`tables`、`blocks`、`chunks` 分别统计，不混用。

如果接口暂时没有这些字段，首页先显示“文档已就绪”，不要写死数字。

---

## 4. 页面与组件改造

### 4.1 首页 / 空状态

替换 `WelcomeScreen.tsx` 的三张营销卡片：

```text
金融查证台
3 家公司 · 2024/2025 年报 · 1 份募集说明书

[查证范围]
7 份文档 · N 个章节 · M 张表 · K 个结构块

[试试这些查证]
- 美的 2025 智能家居收入占比是多少？
- 比亚迪 2025 净利率比 2024 降了多少个百分点？
- 两份年报的 2024 营收是否一致？
- 2026 Q1 数据本地有吗？
```

要求：

- 数字来自 `/finance/stats`，不硬编码。
- 没有文档时显示上传入口；有文档时显示覆盖范围和示例。
- 示例问题覆盖：单表、跨表、跨文档、计算、拒答。
- 移除“智能检索/溯源问答”这种通用营销卡。

### 4.2 查证工作台

新增 `EvidenceWorkbench` 作为 `/` 主体：

- 左栏 `MaterialSidebar`：
  - 当前语料覆盖（公司/期间/文档）
  - 最近查证
  - 示例问题
  - 文档库入口
  - 历史会话（次级）
- 中栏 `AnswerCanvas`：
  - 问题
  - 结论卡
  - 要点列表
  - 计算过程
  - 不确定性
- 右栏 `EvidenceInspector`：
  - 原文
  - 表格
  - 计算
  - 引用链

当前 `ChatBox` 的侧栏 + 主内容 + `CitationPanel` 可以复用，但要把 `CitationPanel` 从“按需 Drawer”升级为“常驻/可折叠检查器”。

### 4.3 答案结论卡

不要只渲染一段 Markdown。建议结构：

```text
结论
智能家居业务占公司营收约 65.71%，毛利率比商业及工业解决方案高 9.09 个百分点。

要点
1. 智能家居业务 2025 年营业收入 299,927,239 千元  [t0013 · p58]
2. 智能家居业务毛利率 29.90%                      [t0013 · p58]
3. 商业及工业解决方案毛利率 20.81%                 [t0013 · p58]
4. 公司 2025 年营业收入 456,451,731 千元           [t0006 · p42]

计算
299,927,239 ÷ 456,451,731 × 100 = 65.7084% → 65.71%
29.90% − 20.81% = 9.09pp

口径
千元；毛利率差为两个披露值直接相减，单位 pp。

不确定性
未发现冲突；两项分母口径均为公司合并营业收入。
```

要求：

- 每个要点后面是引用胶囊，不是普通 Tag。
- 胶囊 hover 显示 `文档 · 页码 · 表/单元格`。
- 计算块展示 formula / inputs / result / tolerance。
- 没有 `calc` 的派生数字要显式标“未提供计算记录”，不要假装已验证。
- 拒答时展示“查过的材料 + 缺失条件”，不要只给一句“无法确定”。

### 4.4 证据检查器

`EvidenceInspector` 四个 Tab：

1. **原文**
   - 章节路径、页码、正文。
   - 保留 `source_spans`，支持后续审计。
2. **表格**
   - 表名、页码、单位、脚注。
   - 高亮 `row_indices` / `column_indices`。
   - 首列固定，数字右对齐，负数/下降用语义色。
   - 大表懒加载/虚拟滚动，不要一次塞满。
3. **计算**
   - formula、inputs、result、tolerance、note。
   - “复算”按钮调用后端 `calculate` 或本地只读展示。
4. **引用链**
   - claim → evidence → table/cell → document/page。
   - 显示 `chunk_id` / `block_id` / `table_id` 方便审计。

验收时必须能：

- 点击 `[t0013]` 定位到表格 Tab；
- 高亮目标行和列；
- 显示单位、页码、脚注；
- 点击计算要点显示输入与结果；
- 不是只高亮一段文本。

### 4.5 查证过程

把 `AgentTracePanel` 从消息气泡里移出：

- 用户版：右栏“查证过程”时间线，默认折叠。
  - 定位 → 读取 → 计算 → 结论
  - 摘要：“查了 2 份文档、3 张表、1 次计算”
- 实验版：`/lab` 显示完整 trace、`result_ids`、`new_result_count`、`result_chars`、token、latency、repair。

当前 `AgentTraceStep` 只有 `step/tool/ok/result_count/latency/args/note`，需要补：

- `result_ids`
- `new_result_count`
- `truncated`
- `table_id`
- `row_indices`
- `column_indices`
- `result_chars`
- `policy`
- `error_detail`

### 4.6 文档库

新增 `/documents`：

| 列 | 内容 |
|---|---|
| 公司 | 美的/比亚迪/贵州茅台/天顺风能 |
| 报告期 | 2024/2025 |
| 类型 | 年报/募集说明书 |
| 页数 | 解析页数 |
| 章节数 | sections |
| 表格数 | tables |
| 状态 | parsed/partial/error |
| 操作 | 查看结构、查看表格、设为当前语料 |

不要复用通用 `/documents` 的上传列表；金融语料是固定 7 份，应该有“语料覆盖”视图。

### 4.7 `/analytics` → `/lab`

把 `AnalyticsDashboard` 转成 `LabDashboard`：

- 主比较：Agent / 结构感知 RAG
- 可选辅助基线：固定窗 RAG（不进入产品主路径）
- `pass@1`、`pass^k`、hard gates
- citation precision、page/table/cell anchors
- token/task、latency、steps、repair rounds
- 失败案例 + trace
- run manifest：commit、index fingerprint、model、tasks hash

现有“文档分析/对话分析/任务分析/知识图谱”可以保留为次级 Tab，但不应该是主视图。

### 4.8 移动端

- `ChatSidebar` 改为 `Drawer`；`visible` 在 <768 默认 false。
- Header 只保留：Logo、当前语料、菜单、用户；上传/文档管理/帮助/主题收进“更多”。
- 主内容全宽，输入框吸底。
- 证据检查器改为 Bottom Sheet，默认 50% 高度，可上拉全屏。
- 表格横向滚动，首列 sticky。
- 用真实 390/430 宽度验收，不接受“能滚动就算适配”。

---

## 5. 视觉方向

从“SaaS 蓝聊天”转向“金融研究/审计工具”：

| 维度 | 现在 | 建议 |
|---|---|---|
| 主色 | Ant 默认蓝 `#1677ff` | 藏蓝或深青，绿色只表示“已验证” |
| 语义色 | 主要靠 Tag | `verified` 绿、`warning` 琥珀、`negative` 红、`neutral` 灰 |
| 数字 | 普通字体 | `font-variant-numeric: tabular-nums`，表格右对齐 |
| 单位 | 写在句子里 | `千元`/`%`/`pp` 做成 chip |
| 阴影 | 较多 | 减少阴影，多用 1px 边框和分区 |
| 动效 | 淡入/悬浮 | 保留必要过渡，不做装饰性动画 |
| 信息密度 | 偏松 | 桌面端允许更高密度，金融用户要对比 |

不要先做换色。先把三栏和证据链立起来，再统一 token。

---

## 6. 实施顺序

采用你建议的顺序，但补一个并行的 Phase 0 契约冻结。

### Phase 0：契约冻结（与 P0 并行，不阻塞移动端）

交付：

- `FinanceCitation` / `EvidenceRef` / `CalcRecord` / `Claim` 类型。
- `/finance/chat`、`/finance/stats`、`/finance/documents`、`/finance/evidence` 的字段表。
- `AgentTraceStep` 扩展字段。
- 明确“没有 PDF 原件，只做页码锚点”的边界。

退出标准：

- 前端能用 mock JSON 渲染一张表、一个单元格、一条计算。
- 后端能对 `CX-01` 返回 `table_id + row_indices + column_indices + unit + calc`。

### Phase 1：P0 可用性（先做）

1. 移动端 Drawer + Header 响应式。
2. `total_documents` 或文案改成“片段”。
3. 统一 `ChatSidebar` / `ChatHeader` 折叠逻辑，去掉重复按钮。
4. 确认 ErrorBoundary 已生效，不加重复实现。

退出标准：

- 390px 下主内容全宽，文字不逐字换行，Header 不溢出。
- 首页文档数不再把 chunks 当 documents。
- 1280/390 截图都可用于演示。

### Phase 2：金融工作台骨架

1. 三栏布局 + 响应式降级。
2. 首页覆盖范围 + 示例问题。
3. 结论卡 + 要点列表（先接现有引用，不接表格坐标）。
4. 右栏 `EvidenceInspector` 空壳 + “尚无结构化证据”状态。

退出标准：

- 桌面/移动都能完成“提问 → 看结论 → 打开右栏”。
- 没有结构化数据时，不伪造表格。

### Phase 3：结构化引用与结论卡

1. 后端 `/finance/chat` 返回 `FinanceCitation` + `calc`。
2. 前端表格 Tab 高亮行列、单位、页码、脚注。
3. 计算 Tab 展示 formula/inputs/result。
4. 引用链 Tab。
5. 拒答/不确定性展示。

退出标准（核心验收）：

- `CX-01` 跨表计算题：点击引用能定位到 `t0013` 的目标行/列和 `t0006` 的营收行。
- 计算块显示 `299,927,239 ÷ 456,451,731 × 100 = 65.7084% → 65.71%`。
- 显示单位“千元”、页码、脚注。
- 不出现“只高亮一段文本”的假定位。

### Phase 4：`/analytics` → `/lab`

1. 三臂对照 + pass@1/pass^k。
2. hard gates、citation anchors、token/task、latency、steps。
3. 失败案例 + trace。
4. run manifest。

退出标准：

- 能在一个页面回答“哪条臂更好、贵多少、为什么失败”。
- 不再用通用文档/对话统计冒充评测。

---

## 7. 验收标准

### 7.1 功能

- [ ] 390px 可用：侧栏 Drawer、Header 不溢出、输入框可用。
- [ ] 首页数字来自 `/finance/stats`，不硬编码。
- [ ] 引用胶囊 hover 显示文档/页码/表/单元格。
- [ ] 点击引用打开证据检查器并定位到表格行列。
- [ ] 计算要点展示 formula / inputs / result / tolerance。
- [ ] 拒答展示查过的材料和缺失条件。
- [ ] 点击“更多”能访问上传/文档管理/帮助/主题。

### 7.2 视觉

- [ ] 1280 三栏不拥挤，主答案列可读宽度 ≥ 640。
- [ ] 390 单栏，无横向滚动（表格内部除外）。
- [ ] 数字使用 tabular-nums，表格右对齐。
- [ ] 单位/百分比/百分点有明确 chip。
- [ ] 不出现“营销卡片冒充功能入口”。

### 7.3 数据

- [ ] `documents / sections / tables / blocks / chunks` 分开统计。
- [ ] 引用坐标来自后端，不由前端解析正文。
- [ ] 表格证据包含 `table_id`、`row_indices`、`column_indices`、`unit`、`page`、`footnote`。
- [ ] 计算记录包含 `formula`、`inputs`、`result`。
- [ ] 权限过滤与现有 `UserContext` 一致。

### 7.4 回归

- [ ] 前端单测通过。
- [ ] 新增 `tsc --noEmit` 类型检查（当前 Vite build 不做类型检查）。
- [ ] 新增 Playwright 视觉回归：390 / 768 / 1280。
- [ ] `CX-01` 截图验收：结论、引用、表格高亮、计算块四者同屏可见。
- [ ] 后端契约测试：`/finance/chat` 返回字段完整；缺字段时前端显示“无结构化证据”而不是崩溃。

---

## 8. 风险与回退

| 风险 | 处理 |
|---|---|
| 后端契约迟迟不齐 | Phase 2 只做骨架，不伪造表格；用 mock JSON 做组件测试，不接生产 |
| 表格解析有缺陷 | 证据检查器显示“解析质量/脚注/页码”，并允许回退到原文 |
| PDF 原件缺失 | 只做页码锚点；不承诺 PDF 高亮，后续再补资产 |
| 移动端改动影响桌面 | 断点分层，先 1280 回归再 390 验收 |
| 过度设计 | 先保证“跨表计算题”可验收，再扩展 Lab |
| 旧 `/chat` 客户端兼容 | 新增 `/finance/chat`，不改通用 `ChatResponse`；旧客户端无感 |

---

## 9. 不做清单

- 不做通用“AI 聊天”换皮。
- 不在前端解析正文猜表格行列。
- 不把 PDF 页面截图当已有能力。
- 不把 `total_chunks` 当文档数。
- 不在普通输入区暴露 Agent/RAG 实验开关。
- 不继续扩 `/analytics` 的通用统计图。
- 不在没有结构化契约时先画“证据面板”。

---

## 10. 代码落点

| 文件 | 改动 |
|---|---|
| `frontend/src/main.tsx` | 保持 ErrorBoundary；确认不再重复包 |
| `frontend/src/App.tsx` | 新增 `/lab`、`/documents` 路由 |
| `frontend/src/components/ChatBox.tsx` | 改为 `EvidenceWorkbench` 或拆分三栏 |
| `frontend/src/components/ChatSidebar.tsx` | 移动端 Drawer；宽度断点 |
| `frontend/src/components/ChatHeader.tsx` | 响应式溢出菜单 |
| `frontend/src/components/WelcomeScreen.tsx` | 覆盖范围 + 示例问题；修文档数文案 |
| `frontend/src/components/MessageItem.tsx` | 结论卡、要点、计算块；把 AgentTracePanel 移出 |
| `frontend/src/components/AgentTracePanel.tsx` | 用户版/实验版拆分 |
| `frontend/src/components/EvidenceInspector.tsx` | 取代 `CitationPanel`：原文 / 表格 / 计算 / 引用链四 Tab，移动端 Bottom Sheet |
| `frontend/src/pages/AnalyticsDashboard.tsx` | 转为 `LabDashboard` |
| `frontend/src/api/client.ts` | 新增 Finance 类型与接口 |
| `backend/app/models/chat.py` | 新增 `FinanceCitation` / `EvidenceRef` / `CalcRecord`，不改旧 `Citation` |
| `backend/app/services/agent_entry.py` | 新增 finance 入口，接 `FinanceToolset`，不再用通用 `get_citation` 解析金融引用 |
| `backend/app/api/documents.py` | `/stats` 增加 `total_documents`；新增 `/finance/stats` |
| `backend/app/api/` | 新增 `finance.py` 路由 |

---

## 10.5 产品路径与固定窗 RAG 的边界（2026-10-30 追加）

产品 UI 不提供固定窗 RAG 选项，也不提供 Agent/RAG 切换开关。后端按问题自动选路：

| 问题类型 | 产品路径 | 例子 |
|---|---|---|
| 简单查数 | 结构感知 RAG，一次检索 + 一次生成 | 美的 2025 智能家居收入是多少 |
| 复杂查证 | Agent 多步查证 | 跨表计算、跨文档核对、拒答边界、多子问题 |

自动选路规则在 `backend/app/services/finance_router.py`，命中比较/计算信号、多公司、多期间、多个子问题或本地未覆盖期间时升级到 Agent；其余走 `backend/app/services/finance_rag.py`。响应里的 `strategy` / `route_reason` 会显示“结构检索”或“多步查证”，但用户不需要选择实现。

固定窗 RAG 的定位：

- **不在产品主路径**；
- 保留在离线评测 `evaluation/run_rag_eval --retrieval-mode fixed`；
- 历史结果和评测代码不删除；
- Lab 页主比较固定为 **Agent vs 结构感知 RAG**；
- 固定窗只作为 `optional_baseline` 辅助基线展示，不进入主比较。

---

## 11. 实施状态（2026-10-30）

本轮已按 Phase 0 → Phase 4 落地，未提交 Git。

| 阶段 | 状态 | 产出 |
|---|---|---|
| Phase 0 契约 | 已完成 | `backend/app/models/finance.py`：`EvidenceRef` / `FinanceCitation` / `CalcRecord` / `Claim` / `FinanceStats` / `LabRunSummary`；`AgentTraceStep` 新增 `result_ids/new_result_count/table_id/row_indices/column_indices/result_chars/policy` |
| Phase 1 P0 | 已完成 | 移动端 `ChatSidebar` Drawer；`ChatHeader` compact 溢出菜单；`/stats` 返回 `total_documents`；首页改为文档/章节/表/结构块分开统计 |
| Phase 2 骨架 | 已完成 | 首页覆盖范围 + 5 类示例问题；`FinanceConclusion` 结论卡；≥1280 三栏、<1280 Drawer、<768 Bottom Sheet |
| Phase 3 结构化引用 | 已完成 | `/finance/chat`、`/finance/stats`、`/finance/documents`、`/finance/evidence`；表格 Tab 高亮行列、单位、脚注、页码；计算 Tab；引用链 + 查证过程 |
| Phase 4 Lab | 已完成 | `/lab` 主比较为 Agent vs 结构感知 RAG；固定窗 RAG 只作为 `optional_baseline`。读取 `compare_arms`/`agent_compare` 与 report，展示 pass@1、硬门禁、翻转数、token/延迟、manifest；没有主比较时明确显示“未运行”，不伪造 |
| 产品选路 | 已完成 | `finance_router.py` 自动选路：简单查数走结构感知 RAG，复杂查证走 Agent；UI 不暴露固定窗或 Agent/RAG 开关 |

已验证：

- 后端 `tests/test_finance_api.py` 9 项通过；真实语料 `7 文档 / 4 公司 / 2842 章节 / 1114 表 / 5296 结构块`。
- 前端 `npx tsc --noEmit` 通过；`npm test` 28 项通过；`npm run build` 在把 `TEMP/TMP` 指到项目内 `.tmp` 后通过（本机 esbuild 临时文件权限问题，不是代码错误）。
- 390px：主内容全宽，无横向滚动；侧栏 Drawer；底部 Sheet 证据检查器。
- 1500px：三栏，主列 860px，证据列 380px。
- 真实 `/finance/chat` 冒烟：返回 `claims + citations + trace`，点击引用可打开表格证据并显示 `t0013 / P.50 / 千元 / 19 行`。
- 产品选路冒烟（2026-10-30）：
  - 简单查数自动走 `structured_rag`：5.1s、3,360 token、1 次 LLM 调用、4 条证据；
  - 复杂查证自动走 `agent`：route_reason 命中“毛利率、百分点”；
  - 复杂题的一次真实运行遇到上游网关 503，接口如实返回 `status=error`，没有伪造答案；同一路径此前已有成功样本。
- 前端界面明确显示“结构检索 / 多步查证”，不出现固定窗 RAG 入口；`/lab` 主比较未运行时显示“主比较未运行”，固定窗只在 `optional_baseline` 出现。

仍未完成/已知边界：

- PDF 原件截图高亮仍未做，只提供解析页码。
- 结构感知 RAG 的 report / compare_arms 还没放进 `docs/evidence`，Lab 主比较显示“未运行”；固定窗 RAG 即使有报告也只作为可选辅助基线。
- 真实模型在 CX-01 上仍可能选择引用披露值而不调用 `calculate`；这是 Agent 行为问题，UI 会如实显示“没有计算记录”，不补假计算。
- 表格解析的部分列名仍是 `col2/col4`；证据检查器用前几行表头做了 `display_columns` 展示补全，原始列名保留在 `columns` 供审计。

---

## 12. 第二轮：工作台骨架 + 历史结构化恢复（2026-10-30）

针对源码评审的三点（聊天中心、历史丢结构化、视觉未完成），本轮只做前两项，视觉留待下一轮。

**A. 主列从聊天流改为轮次工作台**

- `utils/turns.ts` 把消息按「问—答」配对；主列只渲染当前一轮：问题头（第 N 次查证 / 状态 / 选路 / 时间）→ 结论卡 → 完整回答 → 查证过程。
- 历史轮次收进顶部 `WorkbenchTurns.tsx` 轮次条（每轮一个 chip，带状态点），点击切换；不再把历史气泡铺在正文里。
- `MessageItem` 增加 `plain` 模式：去掉头像与气泡，只保留结论卡正文，避免“工作台里还是聊天气泡”。
- 证据检查器默认显示**本轮证据集**（`marker + 文档 + 表格/页码/单位`），点击进入单条详情；不再是空态等用户点胶囊。

**B. 历史会话结构化恢复**

- `ConversationTurn` 增加可选 `payload`（`models/chat.py` + `conversation_memory.py`），旧会话无该字段时行为不变。
- 新增 `services/finance_history.py`：把一次查证压成紧凑载荷（claims/calc/trace/引用坐标），**不存 `content` 与 `table_preview`**，避免一张 200 行表的预览撑大会话文件。
- 两个金融入口（`run_finance_query`、`run_structured_rag`）在写会话时带上 payload；结构检索的“检索为空”拒答也入历史，重开能看到查过什么。
- `/conversations/{id}` 原样返回 payload；前端 `utils/financeHistory.ts` 做容错解析与默认值补齐，`useChat.loadHistoryMessages` 还原为结论卡消息；旧轮次仍按 Markdown。
- 证据正文按需回查：`EvidenceInspector` 检测到缺 `content`/`table_preview` 时调用 `/finance/evidence?evidence_id=...`，成功后进内存缓存。
- 会话保留轮次从 10 提到 30（`max_turns_per_session`），避免长查证会话把最早几轮丢掉。

**验证**

- 后端 `pytest`：299 通过（新增 payload 落盘/读回、旧文件兼容、API 返回 payload 三条）。
- 前端 `tsc --noEmit` 通过；`npm test` 42 项通过（新增 `financeHistory`、`turns`、历史还原与 plain 渲染用例）；`npm run build` 通过。
- 未做：真实浏览器视觉验收与视觉主题（阶段 C）。

**已知边界**

- 本轮之前的旧会话没有 payload，重开仍只有 Markdown；不做回溯重算。
- 左栏材料清单是只读盘点（公司/期间/表数 + 全部入口），暂不支持“按材料过滤检索”——后端 `BlockIndex.search` 已支持 `doc_codes`，下一步可接。

**同轮瘦身（死代码清理）**

- 下线 `/analytics/{overview,documents,conversations,tasks,graph}` 五个端点：前端 `AnalyticsDashboard` 删除后已无调用方。
- 删除 `backend/app/services/analytics.py`（518 行）与 `models/analytics.py` 中的对应模型；`/analytics/audit` 与 AuditLogs 页保留。
- 前端删除 5 个未调用的 analytics 客户端函数与类型；`openapi.json`、`generated.ts` 重新生成。
- 净减约 740 行；后端 299、前端 42 项测试仍全绿。

## 13. 第三轮：证据可核验 + 状态口径 + 视觉 tokens（2026-10-30）

**1. 历史证据可重新核验**

- 会话载荷新增 `corpus_fingerprint`（入口层传入，`finance_corpus.index_fingerprint` 带缓存）；前端把 `payload.corpus_fingerprint` 与 `/finance/stats.index_fingerprint` 比对，判断语料是否变化。
- `EvidenceInspector` 回查状态四态：加载中 / 404 找不到 / 回查失败（可重试）/ 语料版本变化（琥珀提示）。
- 404 时回调父组件，把该轮从「已回答」降级为「需人工复核」，不改后端原始 status。

**2. 工作台信息顺序**

- 左栏改为「语料覆盖（文档/公司/表/块 + 公司·期间 chips）→ 当前材料（只列本轮引用到的文档）→ 最近查证」，删掉全量文档列表，避免文档树。
- 中列保持「问题头（第 N 次 / 状态 / 选路 / 时间）→ 结论卡 → 完整回答 → 查证过程」。

**3. 引用与状态统一**

- 新增 `utils/status.ts`：五状态唯一口径「已回答 / 资料不足 / 需澄清 / 需人工复核 / 执行失败」；`needs_review` 由证据可核验性派生（语料变化、有结论无引用、回查 404）。
- 新增 `utils/citation.ts`：胶囊与证据清单统一输出「文档 · 页码 · 表格 · 目标行列」，点击与右栏联动。
- 计算卡保留公式/输入/结果/单位，并显示 `calc.note` 作为口径说明。

**4. 视觉收敛（渐进）**

- `global.css` 增加语义色（success/warning/error/review/info 及其底色）、间距 `--space-1..6`、`--border-width`，断点写在注释里（media query 不能用 var）。
- 轮次条、问题头、状态点改用 class；新代码不再写色值。
- 表格数字保持右对齐 + `tabular-nums`；单位与脚注单独成行。
- 只替换了本轮触及的组件，其余内联样式留待后续，不一次性大重写。

**5. Lab 边界**

- 固定窗 RAG 基线默认收起，标题明确「可选离线基线」，并标注「不进入主比较」。
- 普通查证页移除工具名、参数、延迟、注入、错误码：`查证过程` 只显示动作（检索/读取/计算）、目标（文档/表/查询）和结果条数；实验数据只留在 `/lab`。

**验证**：后端 299 passed；前端 `tsc --noEmit` 通过、`npm test` 53 passed（新增状态口径、引用描述、证据回查三态）；`npm run build` 通过。

## 14. 第四轮：入口收缩（2026-10-30）

产品只保留金融主线，通用知识库入口下线。

**删除的 HTTP 入口**：`/chat`、`/chat/stream`、`/search`、`/classify`、`/upload`、
`/documents`、`/documents/{id}`、`/stats`、`/tasks`、`/tasks/{id}`。

**删除的实现**：`app/api/documents.py`、`app/services/agent_entry.py`、
`app/services/task_queue.py`、`app/services/document_parser.py`、`app/mcp/`；
前端 `DocumentUpload.tsx`、`DocumentManager.tsx`、旧聊天分支与旧引用渲染。

**保留**：`/finance/*`、`/conversations*`、`/auth/me`、`/lab/runs`、`/analytics/audit`、
`/health*`。底层 `rag_engine`、`Toolbox`、向量库、BM25、重排仍保留：
`agent/runner` 默认工具集与 `evaluation/legacy` 实验还在用，属于“底层能力”，
不在本轮入口收缩范围内。

**保留的依据**：`evaluation/run_eval.py` 直接依赖 `vector_store` / `keyword_search` / `reranker`；
`rag_engine` 被 `evaluation/legacy` 的实验脚本引用。删它们需要同时迁移离线实验，另起一轮。

**验证**：后端 `pytest` 292 passed；前端 `tsc --noEmit` 通过、`npm test` 46 passed；`npm run build` 通过。

## 15. 第五轮：`/lab` 权限口径（2026-10-30）

项目按个人演示定位：评测台对所有登录用户开放，导航与接口权限保持一致。

- 前端：`ChatHeader` / `PageShell` 移除 `/lab` 的 `adminOnly`，登录用户都能看到「实验」；`/audit` 仍仅管理员。
- 后端不变：`/lab/runs` 只要求登录（路由级 `verify_api_key`）；`/analytics/audit` 用 `require_administrator`。
- 回归测试：`ChatHeader.test.tsx` 断言普通用户可见实验、不可见审计；管理员两者可见。

**验证**：前端 `tsc --noEmit` 通过、`npm test` 48 passed。

## 16. 一句话

**先把“移动端 + 计数文案”修到可用，再用三栏工作台承载结论与证据；结构化引用必须等后端返回稳定的 `table_id/row/column/unit/calc` 后再做，最后把 `/analytics` 换成 `/lab`。**  
这样每一阶段都有可演示的产出，而不是先画一个没有数据的证据面板。
