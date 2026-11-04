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
零 token 重判报告落盘。

未闭环（按优先级）：

1. 修复字段透传后**重跑 4 题探针**（真实模型，先报预算），拿 tool_round
   真实缓存命中率与有效输入；旧的「−60%」推算已作废。
2. **32 题 blocks 基线 runs 落盘**（当前只在本地审计过，三套基线对照不可复现）。
3. 检索结构化匹配（指标词 → 行名先验）：直接打多步短板。
4. 全量重跑 + 留出集验收 + pass^k（约 3 倍预算），此后才谈发布闸门。

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
- 离线验证：backend `pytest` 602 passed，任务卡 lint 0 error。
- **尚未跑真模型**：多步拆解是否从 18.2% 提升、覆盖率闸门带来的 token/延迟增量，
  都必须等 11 题 blocks 子集复测后再写结论。

## 已知未修的产品缺陷

- FIN-S01 `t0008` 分季度表列错位（解析层）；
- `t0007` 表内嵌第二段表头，下半段资产负债类指标无法按列名取数；
- 多步拆解是当前最弱维度，尾部两题（FIN-11 / CX-10）打转明显；
- 路由 v2 已改规则，但按项目纪律**没有真实问题金标**，未报告新准确率。

## 接手路径

[AGENTS.md](../AGENTS.md) → 本文 → [docs/INDEX.md](INDEX.md) →
[docs/evidence/INDEX.md](evidence/INDEX.md) → 具体文件。
