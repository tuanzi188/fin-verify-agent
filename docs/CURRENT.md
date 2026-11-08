---
status: active
scope: 全项目
authoritative_for: 唯一允许写「当前阶段」的文件
read_when: 接手第一步
---

# 当前状态（滚动，不写死数字）

> 数字会过期。所有读数去 [docs/evidence/INDEX.md](evidence/INDEX.md) 查，
> 未提交改动跑 `git status --short`，不要相信本文里的任何历史快照。

## 阶段判定

**能演示、能诊断，不能发布。**

- 产品主线（认证 / 审计 / 会话隔离 / 三栏证据工作台 / 历史恢复）已完成；
- 三层评测框架（检索 / 路由 / 答案 / Release Gate）、任务集 lint、留出机制、
  pass@k / pass^k 口径已完成，后端与前端测试全绿；
- 答案层首次全量跑通（42 题 dev，`runs_per_task=1`），但：
  - **3 条硬红线未过**：fabrication、false_refusal、citation_errors；
  - **无 pass^k**，单次运行不含 run-to-run 方差；
  - **10 条留出题未用于验收**；
  - **没有一份完整三层报告**（数值 / 轨迹 / 忠实度在 phase0 报告里仍是 `no_data`）。

## 当前活跃工作

### 通用 RAG 下线（2026-11-06 已完成）

- 删除 `app/services/{rag_engine,chunker,graph_search,vector_store,keyword_search,hybrid_search,reranker,rerank_api,filter_utils}.py`
  与 `app/tools/{mock,retrieval}.py`，共约 3,100 行；`main.py` 启动不再初始化
  向量库 / BM25 / 图谱 / 重排，深度健康检查同步移除这四项。
- `runner.run()` 的 `toolset` 改为必填：不再有隐式制度库默认值，域由调用方决定
  （线上 `finance_entry` 传 `FinanceToolset`，测试传 `tests/support/injection_toolset.py`）。
- 评测侧：`run_eval.py` / `check_regression.py` / `run_agent_eval.py` / `baseline_retrieval.json` /
  `legacy/*` 一并删除；固定窗基线的分块逻辑内联进 `evaluation/rag_corpus.py`。
- **CI 门禁换向**：旧门禁测的是通用 RAG 在 9 条样例上的召回，与产品无关。
  新门禁 `tests/test_block_retrieval_regression.py` 用合成语料 + hash embedding
  覆盖表格行组切分、核心表加权、公司/期间过滤与排序，进现有 pytest job，
  不需要模型下载或本地语料。
- 依赖同步：`chromadb`、`jieba` 从 `requirements.txt` 移除，锁文件重新生成
  （只减不增，无版本漂移）；`app/config.py` 删除四个随之失效的配置块。
- 测试：802 → 752（净减 50 条，均为被删模块自身的用例），全量耗时 26.5s → 约 12s。

### 评测清理（2026-11-06 已完成）

- **修掉默认口径漂移**：`score_agent_runs` 的 `--corpus` 曾默认 `policy`（制度库，
  已删），`--tasks` 默认指向 `agent_tasks.jsonl`（已删）。照 README 命令跑会拿
  制度库的 `chunk_map` 给金融题判分且不报错。现在两者都固定为金融域，
  `policy` 分支删除；保留 `--corpus finance` 只为兼容历史复现命令。
- 删除 `metrics.py`（唯一调用方已下线，只剩自测引用）与制度库语料链
  （`agent_corpus` / `sample_data` / `lint_agent_tasks` / `agent_tasks*.jsonl`）。
  公共 lint 规则抽到 `lint_common.py`，`lint_finance_tasks` 只依赖这一层。
- `compare_arms` / `check_block_retrieval` 移入 `evaluation/legacy/`：
  仍可手工运行，需要本地语料，不进 CI。
- 新增 `tests/test_score_agent_runs_defaults.py` 锁默认任务集与语料，防止再漂。
- 测试：752 → 738（删掉的是被清模块自身的用例 + 新增 2 条口径锁）。

### 联网综述（2026-11-05 已接线，未评测）

- `app/services/web_search.py`：来源分级（官方 / 权威媒体 / 门户）、黑名单、
  查询闸门（改写后的检索词不得引入问题里没有的年份或数字）、单任务检索预算、
  本地 Exa（mcporter MCP，无需 key）与 REST 双 provider、官方 PDF 入库候选。
- `app/services/market_brief.py`：检索 → 五段综述（核心判断 / 分项 / 常见误读 /
  分歧 / 资料缺口）→ 服务端追加免责；来源外数字与来源名校验进 `warnings`；
  加粗、拆条、条数上限由服务端确定性处理，不依赖模型排版。
- 接入 `POST /finance/chat`：请求加 `allow_web`（默认关闭），本地语料未覆盖时
  转综述；响应加 `market_brief`，会话历史同步持久化；前端加「联网」开关与综述卡片。
- **未评测**：联网综述本身仍未接入评测；检索结果不落快照，读数不可复现。
  辅助问答域已落地（见 [ASSIST_EVAL.md](ASSIST_EVAL.md)），覆盖概念、观点、拒答三条路径；
  联网综述留到快照回放补上之后再接。
- 来源透明性：`market_brief.sources[]` 带 `snippet`（来源摘要原文），
  `market_brief.queries` 回显实际发给检索服务的查询词。

### 巨潮公告固定路径入库（2026-11-05 已实现，未批量复测）

- `app/finance/cninfo.py`：只做发现层——按「代码 + 年份」查巨潮公告检索接口，
  只认标题逐字等于「{year}年年度报告」（沪市带公司名前缀），
  摘要 / 英文版 / 更正 / 取消一律排除；多份同标题取公告日期最新。
- `backend/scripts/fetch_cninfo_announcement.py`：`--dry-run` 只打印候选与排除理由；
  正式执行写 manifest → 复用 `fetch_finance_corpus` 的原子下载 + sha256 →
  复用 `ingest_finance_corpus` 的解析函数写 document.json。
- 实测（单份、未落盘）：美的 2025 / 贵州茅台 2025 的公告号、PDF 地址、
  公告日期与既有 manifest 完全一致；美的 2025 走完整链路后
  `sha256 / size / page_count` 与入库值一致。
- **未做**：没有对 7 份语料批量重跑，也没有把新公司并入对外语料；
  单位与合并口径是自动填充的默认值，首次入库后仍需人工核对。

### 六类拒答分级（2026-11-05 已实现，未做分布评测）

- `app/finance/refusal.py`：`ambiguous_scope` / `out_of_domain` /
  `unsupported_intent` / `out_of_scope` / `insufficient_evidence` /
  `source_conflict`，按「谁能把这一轮救回来」分三级（补条件 / 产品边界 / 证据缺口）。
  冲突优先于缺失：有候选但对不上归 `source_conflict`，不说成「没查到」。
- 由 `query_scope.py`、`routing_policy.py` 各返回点打码；`/finance/chat` 响应新增
  `refusal`（含 label / level / action / recover_by / hint / detail），会话历史同步；
  前端状态条用「类别 · 级别」替代原来的「材料不足」泛化文案。
- **已落地（未跑真模型）**：拒答集 20 条（五类各 4；观点类已移出）与零模型 runner 见
  [ASSIST_EVAL.md](ASSIST_EVAL.md)，可产出编码准确率与混淆矩阵；
  真模型跑批与分布读数尚未产生，不写结论。

### 提示词去重与选表字段（2026-11-06 已实现，未做真模型复测）

- 删掉两处重复和一处元规则：规则 7 里的坏样例（「无需改写正文出处」）与重复的
  「公式不复述」句；规则 8 与规则 1 重复的「只定位」尾句。improved/blocks
  从 2638 字符降到 2557。
- `search_tables` 的汇总表标记从拼进 `content` 改为结构化字段 `summary_table`，
  由 `runner._format_locators` 渲染「汇总表」。旧写法**从未到达模型**——定位项
  渲染只取 `chunk_id/title/page`，`content` 被丢弃（2026-11-06 核对）。
- `structured_rag` 规则 4 区分「证据里已给出的比率可直接引用」与「需要自己推导的
  才拒答」，修掉把证据内同比一并拒掉的问题。
- 试过一版 197 字符的 few-shot 形态示例，探针 9 次仍未改善形态（7/9 仍写算式），
  已回滚。结论：排版问题不在提示词层解决。

### 金融概念问答（2026-11-06 已实现，未评测）

- 新增路径 `strategy="concept"`：回答金融/财报术语。默认只用模型通用知识；
  请求带 `allow_web=true` 时先做一次公开信息检索，**定义仍来自通用知识**，
  时效性数字只在资料里出现时才写，并标「（来源名，月-日）」。
  不检索本地语料，不产生 `[n]`。由 `initial_path="concept"` 分派。
- 路由条件四条同时成立：定义句式（什么是 / 是什么意思 / 怎么算 / 有什么区别）、
  含金融术语（`CONCEPT_TERMS` 或已有指标词）、不带公司/年份/季度、不索要具体数值。
  带主体的同类问法仍走查证。
- 答案纪律由代码兜底：正文出现 `[n]`、页码或表 id 时降级为 `needs_review`
  （这类标记会让人误以为结论来自本地语料）；联网模式下写了资料清单里没有的
  来源名（编来源）同样降级。数字与长度只记 note，不阻断交付。
  实测 flash 档会先花上千 token 推理，正文仍写成 Markdown 报告体，
  因此 `max_tokens` 取 4096，否则 reasoning 吃满预算会返回空正文。
- 联网结果只作参考，不进证据池：搜到资料但没用时记 `concept:web_unused`，
  前端只打「通用知识」；真正引用了资料才打「通用知识 + 公开信息」。
  检索到的来源随响应 `web_sources` 返回（来源名/日期/标题/链接），前端以
  「参考资料 · N 篇」折叠行展示，默认收起、点开看编号列表，即使未被逐条引用
  也能看见「查过什么」。
- 答案首段由服务端切成 `answer_lead`：跳过 Markdown 标题，取第一段不超过 120 字的
  定论单独放大显示，正文里去掉该段避免重复；切不到就退回普通排版。
- 费率/规则类问题（印花税、过户费、佣金、涨跌停等）识别为 `web_fact_question`：
  这类数字会变，**强制联网**（不看「联网」开关），第二轮加「最新规定」提高官方来源命中；
  查不到就回「未检索到可核对的公开资料」，绝不凭记忆报数。答案有数字但未标来源时
  只记 `concept:unattributed_number`，不降级——「未核实」由标签和来源列表承担。
- 来源白名单补 `gov.cn`（财政部/税务总局/地方政府）与 `news.cn`。旧白名单只有
  交易所和财经媒体，税率类问题的结果会被整批丢弃。
- 检索策略（2026-11-06 补齐）：
  - **Query 改写**：费率题去口语词后拼「最新规定 / 官方公告」两条；概念题保持原句。
  - **并行检索**：两条查询用线程池并发执行。实测串行 9.1s → 并行 6.4s
    （两条查询各起一个 mcporter 子进程，并发会互相抢 CPU，拿不到理论上的 4.5s）。
  - **10 分钟结果缓存**：同 query 不重复打网络，命中缓存不消耗检索预算。
  - **T3 降级使用**：概念题允许白名单外域名按 T3 补位（最多 2 条、需有日期）；
    费率题仍只认 T1/T2。
  - **交叉验证**：费率题无官方源时，必须有 ≥2 个独立域名，否则统一回
    「只检索到单一非官方来源…」。
- 模型没把握时固定回「这个概念我无法给出可靠解释」，状态记 `abstained`。
- 顺带修复：`什么是` 曾被 `unknown_scope_company` 当成公司名，概念题直接拒答；
  疑问词已加入主体排除表。
- 前端：概念答案打「通用知识 · 未核实」标记，不渲染结论卡与引用面板；
  技术详情显示「概念问答不引用本地材料」；历史 payload 不写语料指纹，
  语料变更时不会被升级成「需人工复核」。
- 延迟构成（2026-11-06 单次本地实测，`A股卖出印花税现在是多少`）：
  检索 6.4s + 生成 4.1s ≈ 10.5s；缓存命中后只剩生成 4.1s。
  检索慢在 `mcporter` 每次起子进程（单条约 4.5s），不是 Exa 本身。
- **未做流式输出**：检索在生成之前，首字仍要等检索完成（约 6.5s），
  收益约 4s，不足以抵消新增 SSE 端点与前端流式解析的契约改动。
- **已落地（未跑真模型）**：概念题集 30 条与 runner/scorer 见
  [ASSIST_EVAL.md](ASSIST_EVAL.md)；费率题用离线快照，缺快照记 `invalid`。
  真模型跑批未做，不写任何 pass@1 或提升幅度。
  真模型冒烟（2026-11-06）：`A股卖出印花税现在是多少` 命中 4 条来源
  （中国政府网、国家税务总局、人民网、证券时报），答案 213–346 字且数值正确；
  `什么是商誉` 命中 3 条来源，答案约 1000 字。两次都没有在正文里逐条标注来源
  （`concept:web_unused`），逐句归属仍未验证；覆盖度与正确率都没有量化。

### 观点/建议类（2026-11-07 已接线，未评测）

- 路由新增 `opinion` 路径：明确要买卖建议/目标价/涨跌预测/荐基荐股的问题
  不再一律澄清，转 `app/services/advice_qa.py`。
- 免责声明由服务端强制追加：`disclaimer` 字段 + `advisory.requires_disclaimer`，
  同时写进正文，前端不解析也不会丢；模型不负责写免责声明。
- 披露题优先：带公司 + 指标 + 期间的问法仍走查证；`tests/test_advice_routing.py`
  有正反例锁。
- 评测侧：`assist_tasks_advice.jsonl` 12 条，判分含 `disclaimer_present`、
  `no_return_promise`、`target_price_labeled`、`route_opinion`。
- **未做**：真实模型跑批、前端观点卡、合规确认。免责声明是否构成法律免责
  由法务判断，技术侧只保证"每次都带、不承诺收益、目标价标来源或推测"。

### 财报口径与相对期间（2026-11-06 已实现，未做真模型复测）

- **括号负数归一**：语料里 1,268 个单元格用括号表示负数（美的两期 993、
  比亚迪两期 275），此前 `record_number_tokens` 只在括号紧贴数字时才翻符号，
  带空格的 `(1,474,894 )` 和带单位的 `(5,728,446) 千元` 都读成正数——校验会把
  正确的负数答案判成 `numeric_mismatch`。现在按文档声明的
  `negative_format=parentheses` 口径翻符号并保留括号外的单位（该字段仍只是声明，
  读取层固定实现这一种口径；7 份语料全是 parentheses）；`（1）`、`（2）` 这类
  序号不翻（它们没有千分位/小数点），此前会被误读成 -1。
  真实语料冒烟：美的 2024 `t0110` 第 25 行第 2 列 `(5,728,446)` → `-5728446 千元`，
  与答案一致时不再报 issue。
- **相对期间确定性解析**：新增「近/最近/过去 N 年」→ 从语料最新覆盖年份倒推
  （近三年 → 2023、2024、2025，并写进改写后的问题）；新增「上一季度/本季度」→
  按当前日历解析，再交给原有覆盖检查（今天解析出 2026 Q3，本地只覆盖 2025 →
  `out_of_scope`）。「上一季度」若与指定年份同时出现（如「2025 年上一季度」）
  判为自相矛盾，直接澄清。超过十年的跨度不猜，留给澄清。
- **未做**：没有跑真模型；这两项只改变解析与校验的确定性行为，不改变检索策略。

### 输出排版（2026-11-06：渲染层已修；提示词实验阴性，已回退）

- **已修**：前端从未装 `remark-gfm`，而查证答案常带定位表——
  11 题历史 runs 里 10 题含 Markdown 表格，此前全部渲染成竖线文本，`.markdown-body table`
  的样式等于死代码。已装 `remark-gfm` 并在 `MessageItem` / `MarketBriefCard` 启用，
  表格外包横向滚动容器（右侧工作台只有几百像素，4~5 列会撑破气泡）。
- **已修**：要点卡把「交叉核对（来源另一张表「（1） 营业收入构成」）：」这类引导语
  当成一条要点（它带序号所以通过了"有数字"的过滤）。`build_claims` 现在丢弃以冒号收尾的行。
- **提示词实验（未采纳，已回退）**：在 `BASE_RULES` 加「三段骨架 / 不写内部表 id /
  不重复数字 / 单位照抄」四条，跑 3 题探针 `FIN-07 / FIN-09 / FIN-11`。
  产物与读数见 [evidence/INDEX.md](evidence/INDEX.md) §D（`format_probe3_20261106.json`）。
  - 好的部分：正文内部表 id 12/14 处 → 0；定位变成「年报 · 表名 · P.页码」。
  - 坏的部分：FIN-11 把「不要写表 id」误读成「不能给单元格坐标」，开头写了一段
    「本轮无法给出单元格编号」的自我否定；FIN-09（单点查数）从 294 字无结构变成
    841 字三段 + 7 行表格，恰好违反新加的「一个数字不造表」；FIN-07 段落 4→5、
    结论段仍贴 `inputs ...` 计算记录；FIN-09 末尾自建「引用」清单又把表 id 写回来。
  - 结论：**模型输出形态靠提示词反复调不稳，且与「必须精确到表+行+列」的旧规则冲突**。
    四条规则已全部回退，工作区不带这次改动。下一步方向改为服务端从 citations
    渲染「依据表」组件，正文只留结论（未开工）。
- **提示词冲突清理（2026-11-06，未跑真模型）**：把「形态规则」按形态拆开注入，
  并清掉 6 处自相矛盾：
  1. 「正文必须写表 id」与「内部 id 不该进正文」并存 → 只留一套定位写法
     （`VERIFICATION_LAYOUT_RULES`：文档简称 · 表名/章节 · P.页码），注入查证与摘录，
     **不进综述**；同时写明「缺单元格不等于不能给定位」，堵住 FIN-11 那种自我否定。
  2. 查证规则 1「任何数字必须来自表格」与规则 8「正文披露项写到章节+页码」并存 →
     规则 1 收窄为「表格里有的财务数值」，正文披露项按规则 8 走，不再伪造单元格。
  3. 规则 7「给结论、公式和引用」与规则 2「公式不在正文复述」并存 → 规则 7 去掉「公式」；
     `CALC_REPAIR_HINT` 同步改成「正文数值与 result 一致，公式留在计算记录」。
  4. blocks 模式仍指导「退回 search_sections」（该模式不提供这个工具）→ 规则改成条件句，
     并在该模式下把工具清单与规则正文里的 `search_sections` 一并替换掉。
  5. 联网综述套用查证纪律：综述没有坐标、且允许标注过的推算值，与「事实必须带坐标」
     「推断不得引入新数字」直接冲突 → 拆出 `CORE_DISCIPLINE`（只保留不编造、免责由系统写），
     综述改挂它，提示词从 1427 字降到 1115 字。
  6. `[计算n]` 只写在工具集后缀里，与主规则分离，11 题实测 0 次遵守 → 收敛到查证规则第 2 条
     一处，后缀不再重复；绑定仍有确定性回退（按唯一同单位结果值匹配）。
  - 结论：模块边界改成「通用纪律 / 查证纪律 / 定位写法 / 综述格式」四层，
    一条规则只出现在一层。
- **复测（2026-11-06，3 题，单次）**：产物 `format_probe3b_20261106.json`，
  读数登记在 [evidence/INDEX.md](evidence/INDEX.md) §D。
  - 达成：正文内部表 id 6/7/0 → **0/0/0**；FIN-11 的「本轮无法给出单元格编号」
    自我否定消失；定位统一成「年报 · 表名 · P.页码」。
  - 未达成：FIN-07 仍把算式写进正文（`增加额 = … − … = 49,302,131`）；FIN-09
    单点查数由 294 字增至 793 字（分三个小节）；FIN-11/FIN-09 出现复述规则的句子
    （「因此无需改写正文出处」）。已就此补一条禁令（不要解释遵守了哪条规则），**未复测**。
  - 三题合计 199,813 token（探针 A 185,056、旧 214,758）。单次、`runs_per_task=1`，
    不作收益结论。
- **第二轮自查（2026-11-06，未跑真模型）**：又清掉 6 处互相打架的地方：
  1. blocks 模式删掉 `search_sections` 后，规则 13 仍写「三者都只定位」——指代悬空；
     改为「这些检索工具都只定位」。
  2. 规则 4 把口径写死「合并报表口径」，与后缀「用户点名母公司时用母公司」冲突——
     规则 4 改为「默认合并、点名母公司并写明口径」，后缀的重复句删除。
  3. 综述硬约束 3「找不到就不写」与「分项」段允许「（推算，非披露）」互相否决——
     硬约束 3 加「除明确标注的推算值外」。
  4. 结构检索规则 4 让模型说「需要多步查证」，但这句话不在 `ABSTAIN_KEYWORDS` 里，
     也没有 `requires_calculation` 发出（它只挂在 `REPAIRABLE_ISSUES` 上），
     结果会被当成正常答案交付——改为「无法确定：需要多步查证」。
  5. `COVERAGE_REPAIR_HINT` 无条件指导用 `search_sections`（blocks 模式没有该工具）。
  6. `CALC_REPAIR_HINT` 引用旧规则原文「任何数字都必须来自单元格」，与收窄后的规则 1 不一致。
  另把规则 8 首句改成「能落到表格的数值」，与三行后的正文披露项例外对齐。
- **提示词瘦身（2026-11-06，已复测）**：`FINANCE_TOOL_INTRO` 与
  `FINANCE_TOOL_SPECS` 的 function schema 重复（同一句「只定位不是证据」写两遍，措辞还不一致），
  641 字压到 129 字，只保留「见函数定义 + 检索类只定位 / 读原文才算证据」。
  Agent improved 提示词 3074 → 2638 字符（约 2475 → 2201 token，−11%）；
  同时删掉 blocks 模式下针对旧工具清单的 `.replace`（目标字符串已不存在）。
  新增体量上限测试（<2800 字符）防止再长回去。
  - 同 3 题单次对照（仅提示词不同，产物 `format_probe3c_20261106.json`）：
    失败工具 1→0、空结果 3→3、步数 15.0→12.7、合计 130,882 token（探针 B 199,813）；
    三题数值与金标一致，`numeric_audit` 金标覆盖 5/5、未溯源数值 15/90。
  - **不作收益结论**：单次、`runs_per_task=1`、未跑 judge；FIN-09 反而更长
    （793 → 1064 字），FIN-07 仍把算式写进正文。
- **未动**：答案骨架不统一（11 题里出现 `## 结论`+分节 / `### 一、二、三` /
  单标题 / 无标题四种）；元 / 千元 / 亿元混用；结论段与"派生计算"段重复。
  这几项只在探针里试过一次（阴性），现改为服务端从 citations 渲染「依据表」的方向，
  未开工。要点卡覆盖原文 32%~91%（多数 35%~65%），其余在折叠区。


成本与缓存记账（2026-11-04）：见
[docs/COST_OPTIMIZATION_PLAN_20261104.md](COST_OPTIMIZATION_PLAN_20261104.md) §5 行动项。

已完成：缓存字段提取 / 逐调用累加 / 有效输入折算 / 报告自声明、
H1/H2 网关判定实验（两假设均证伪，真因是客户端漏字段，已修复）、
零 token 重判报告落盘、**11 题 blocks 子集复测（runs + 真实 judge 报告）落盘**。

未闭环（按优先级）：

1. **11 题 blocks 子集复测**（已完成，2026-11-04）：产物与读数见
   [docs/evidence/INDEX.md](evidence/INDEX.md) §A / §D。
   - runs：`finance_ms11_20261104.json`（11 题全 `answered`）、判分：`finance_ms11_report_20261104.json`、
     badcase：`finance_ms11_badcases_20261104.jsonl`，任务集快照随 runs 落盘。
   - 合计 588,011 token（in 547,847 / out 40,164），**未超 ≤1.2M 预算**；
     11 题逐题 `hit + miss == tokens_in`，缓存字段透传修复稳定生效；
     全价命中占比、有效输入估算等具体读数见
     [docs/evidence/INDEX.md](evidence/INDEX.md) §A（**估算非实测**）。
   - 这批 11 题 `pass@1 = 7/11`，3 条硬红线（fabrication / false_refusal /
     citation_errors）与数值幻觉率全部为 0。
   - **可比性限制（务必照写）**：旧 2/11 来自 11-03 `sections` + 42 题卡 +
     `--reuse-judgments` 零 token 重判；新剖面是 `blocks` + 11 题卡 + 真实 judge +
     含工具层改动的新代码（`run_config.git=d9707b1`，`dirty=true`）。`corpus_hash` 一致但
     search_mode / 任务卡 / judge 模式 / commit 均不同，**不是严格 A/B，不写「提升 X 个百分点」**，
     只作不同条件下的定性观察。且 `runs_per_task=1`，无 pass^k，不外推全量验收。
   - 复现命令（已执行，保留备查）：

   ```bash
   cd backend
   # 1) 输入卡（零模型）：docs/evidence/ms11_tasks_20261104.jsonl
   #    由 python -m evaluation.rebuild_ms11_tasks 从三套任务卡按固定 id 生成
   # 2) 只跑不判（blocks 模式，排除留出集）
   python -m evaluation.run_finance_eval \
     --tasks ../docs/evidence/ms11_tasks_20261104.jsonl \
     --search-mode blocks --holdout exclude --runs 1 \
     --out ../docs/evidence/finance_ms11_20261104.json
   # 3) 判分：答案已变，走真实 judge（不能 --reuse-judgments）
   python -m evaluation.score_agent_runs \
     --runs ../docs/evidence/finance_ms11_20261104.json \
     --tasks ../docs/evidence/ms11_tasks_20261104.jsonl --corpus finance \
     --holdout exclude --out ../docs/evidence/finance_ms11_report_20261104.json \
     --badcases ../docs/evidence/finance_ms11_badcases_20261104.jsonl
   ```

   逐题对比用 `compare_agent_reports` 前先确认两份报告 `tasks_sha256` /
   `corpus_hash` 一致，否则标 `incomparable`（旧报告绑定 11-03 语料状态与不同判分口径）。
2. 修复字段透传后**重跑 4 题探针**（真实模型，先报预算），拿 tool_round
   真实缓存命中率与有效输入；旧的「−60%」推算已作废。
   注：11 题复测已顺带证明工具循环缓存记账生效（逐题 hit+miss 恒等），
   但 4 题探针的 `tool_round` 分阶段命中率仍需单独重跑。
3. **32 题 blocks 基线 runs 落盘**（当前只在本地审计过，三套基线对照不可复现）。
4. 检索结构化匹配（指标词 → 行名先验）：直接打多步短板。
5. 全量重跑 + 留出集验收 + pass^k（约 3 倍预算），此后才谈发布闸门。

已解决：`run_finance_eval` 现在把筛选后的实际任务集落盘为 `<out>.tasks.jsonl`
并让 `run_config.tasks_file` 指向它（可用 `--no-task-snapshot` 关闭），
不再依赖 `%TEMP%` 下的合并临时文件。历史报告仍需按
[docs/evidence/INDEX.md](evidence/INDEX.md) §C 的方式重建。

### 2026-11-04 多步查证工具层加固（已完成，未做真模型复测）

- 行列解析：单位后缀 / 年份 / 编号前缀 / 别名 / 模糊匹配，失败时回显候选；
  `get_rows` 单行越界改为部分成功；`get_cell` 增加错位自校验。
- `calculate` 接受 `formula` / `expr` 别名与 JSON 字符串 `inputs`。
- 覆盖检查从文档级扩展到「公司 × 期间 × 指标」槽位；新增 `search_tables`
  表目录定位；读失败补一次检索预算；章节正文数字触发补单元格。
- 正文 JSON 工具调用（如 `{"tool":"search_kb"}`）不再落进答案：检测到后回注一次，
  要求使用真正的 function call；未知工具名会回显可用工具清单。
- 离线验证：后端全量 `pytest` 通过、三套任务卡 lint 0 error（通过数会变，
  跑 `python -m pytest -q` 看当前值）。
- **已做 11 题 blocks 子集复测**（见上文第 1 项）：11 题全 `answered`、
  `pass@1 = 7/11`、3 条硬红线转绿、数值幻觉率 0.0%。
  但这是**不同条件下的单次剖面**（`sections`→`blocks`、任务卡 / judge / commit 均变），
  不能写成「工具层改动使多步提升 X」；仍 fail 的 FIN-11 / CX-08 / FIN-S06 / FIN-S10
  是下一轮检索结构化匹配的靶点，FIN-11 仍是打转题（20 步 / 126,897 in）。
  覆盖率闸门 / `search_tables` 的 token 与延迟增量：11 题合计 588,011、
  中位延迟 29.5s、平均 13.73 步，与旧 42 题均值口径不同，不直接相减。

### 2026-11-04 逐题修复 4 条 badcase（已实现，**未做真模型复测**）

对 `finance_ms11_badcases_20261104.jsonl` 的 4 题逐个核实 verdict/checks/证据映射后修复：

| 题 | 真实根因 | 修复 |
|---|---|---|
| FIN-11 | `search_tables` 未计入检索别名 → `tool_call_count` 假失败；跨 3 家公司检索预算 5 次不够 | 别名纳入（P1-3）；预算 5→8 且耗尽时回显已定位 chunk 清单（P1-2） |
| FIN-S06 | `get_rows` 把「营业收入 / 营业总收入」解析到同一行直接抛错；且 `search_tables` 未计入别名 | 不同行名撞同一行改为跳过+回显（P1-1）；别名纳入（P1-3）。答案原本就对 |
| FIN-S10 | 合计 + 千元换算元心算对了但一轮没调 `calculate` | `needs_calc_repair` 扩到合计/换算，复用已有 `calc_repair` 闸门（P2-1） |
| CX-08 | 放着金标汇总表不读、去读营业收入构成表 → 4 要点只 1 个被引用 | `search_tables` 结果给「主要会计数据」表加【推荐】标记 + `FINANCE_RULES` 选表原则（P2-2，不写死表号、不改判分） |

- **零模型验证**：用新 `check_tool_constraints` 重判旧 runs，FIN-11 / FIN-S06 的
  `tool_call_count` 由 False→True（确定性检查，与 judge 无关，合法复用）；
  FIN-S10 仍缺 `calculate`，正是 P2-1 要修的能力项；CX-08 无工具约束，
  转绿与否只能靠真实复测。新增 13 条离线单测，后端全量 `pytest` 通过。
- **按 `AGENTS.md §9.6`：只写「已实现 / 未复测」，不写任何提升幅度**。
  P1-1/P1-2/P2-1/P2-2 会改变模型行为与答案，pass@1 与 token 的实际变化
  必须等**同条件 A/B 复测**（同一 11 题卡、同一 judge、仅 commit 不同）才下结论。
  复测预算上限 ≤0.6M token，跑前先报，未批准不执行。

### 2026-11-05 解析层：列对齐 / 跨页报表 / 表单位（已实现，未在原语料复测）

- **列对齐**：表头行与数据行的网格索引不一致时（宁德时代 t0007 的「2024 年」
  落到 2023 的值上，产出「有页码的错误数字」），改为按单元格 bbox 的 x 区间
  重建列；只在网格结果出现 `colN` 占位名时触发，规整的表行为不变。
- **跨页报表**：资产负债表 / 利润表 / 现金流量表每页几十行，旧规则只接 ≤4 行碎片，
  于是每页变成无表头的独立表。新增「无标题 + x 对齐 + 页边紧邻 + 首行是数据行 +
  列数可对齐」的合并规则。
- **表单位**：`单位：千元` 在几十页重复出现，被 `count_repeated_lines` 当页眉剔除，
  单位回退成文档默认（差 1000 倍）。已改为保留单位声明行，并让续页继承。
- 新增零 token 证据流分析器 `evaluation/evidence_flow.py`（定位→读取→引用转化）。
- 详细根因、验证范围与未验证项见
  [archive/20261105_parse_layer_session.md](archive/20261105_parse_layer_session.md)。
- **按 §9.6**：只在宁德时代 2024 年报上验证过，原 7 份语料未重跑，
  不写任何「解析准确率提升」的结论；文中数字均为本地单次实测、未落盘。

## 已知未修的产品缺陷

- FIN-S01 `t0008` 分季度表列错位、`t0007` 内嵌第二段表头：2026-11-05 已实现修复，
  **未在原语料复测**（见上一节）；
- 多步拆解是当前最弱维度，尾部两题（FIN-11 / CX-10）打转明显；
- 路由 v2 已改规则，但按项目纪律**没有真实问题金标**，未报告新准确率。

## 接手路径

[AGENTS.md](../AGENTS.md) → 本文 → [docs/INDEX.md](INDEX.md) →
[docs/evidence/INDEX.md](evidence/INDEX.md) → 具体文件。
