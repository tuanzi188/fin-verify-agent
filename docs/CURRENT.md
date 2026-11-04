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

## 已知未修的产品缺陷

- FIN-S01 `t0008` 分季度表列错位（解析层）；
- `t0007` 表内嵌第二段表头，下半段资产负债类指标无法按列名取数；
- 多步拆解是当前最弱维度，尾部两题（FIN-11 / CX-10）打转明显；
- 路由 v2 已改规则，但按项目纪律**没有真实问题金标**，未报告新准确率。

## 接手路径

[AGENTS.md](../AGENTS.md) → 本文 → [docs/INDEX.md](INDEX.md) →
[docs/evidence/INDEX.md](evidence/INDEX.md) → 具体文件。
