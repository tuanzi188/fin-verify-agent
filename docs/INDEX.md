---
status: active
scope: 文档体系
authoritative_for: 哪份文档可信
read_when: 接手时、引用任何设计/评测结论前
---

# 文档索引（10 份 + 导航）

`status: normative` = 当前权威口径，改代码前必读。
`status: active` = 当前工作记录。
`status: historical` = 设计史与历史读数，**只查依据，不引读数**。

## 顶层（活文件）

| 文件 | status | 一句话 | 什么时候读 |
|---|---|---|---|
| [CURRENT.md](CURRENT.md) | active | 唯一滚动状态：阶段判定 + 未闭环项 | 接手第一步 |
| [ANSWER_EQUIVALENCE_RUBRIC.md](ANSWER_EQUIVALENCE_RUBRIC.md) | normative | 数值/单位/方向等价判据 | 改判分容差或 rubric 前 |
| [ROUTING_POLICY.md](ROUTING_POLICY.md) | normative | 路由判据 v2、证据充分清单、升级白名单 | 改 `finance_router.py` 前 |
| [COST_OPTIMIZATION_PLAN_20261104.md](COST_OPTIMIZATION_PLAN_20261104.md) | active | 缓存记账 + 当前待办（§5） | 接手成本优化、引用 token 数字前 |
| [evidence/INDEX.md](evidence/INDEX.md) | active | 产物清单 + 当前有效基线登记 | 引用任何评测读数前 |

根目录另有 [AGENTS.md](../AGENTS.md)（接手导航）与 [README.md](../README.md)（产品说明）。

## archive/（历史，按日期）

| 文件 | status | 内容 | 注意 |
|---|---|---|---|
| [20261027_agent_eval_project_spec.md](archive/20261027_agent_eval_project_spec.md) | historical | 制度库时代 Agent 评测设计史（1107 行） | 标题与 §6.6 HTTP 入口已下线；指标口径仍有效 |
| [20261030_web_ui_redesign_plan.md](archive/20261030_web_ui_redesign_plan.md) | historical | 三栏工作台 / 证据检查器设计史 | §10–15 是当日实施日报，不是待办 |
| [20261031_eval_upgrade_plan.md](archive/20261031_eval_upgrade_plan.md) | historical | 三层评测升级方案（计划） | 已被实施记录取代 |
| [20261031_financial_doc_agent_plan.md](archive/20261031_financial_doc_agent_plan.md) | historical | 金融查证产品与实施总方案 v1.1（980 行） | §8 周计划、§11 v1 结论属历史排期 |
| [20261101_eval_upgrade_implementation.md](archive/20261101_eval_upgrade_implementation.md) | historical | 三层评测落地记录与首轮离线读数 | Phase 0 语料状态已变；答案层当时 `no_data` |
| [20261102_finance_reliability.md](archive/20261102_finance_reliability.md) | historical | 2026-11-02 可靠性修复说明 | 实现边界仍可查 |
| [20261103_finance_answer_eval.md](archive/20261103_finance_answer_eval.md) | historical | 答案层首次全量评测、6 个缺陷、检索加权实验 | **含三套判分口径，引用必须带产物名** |

## 使用规则

1. 引用数字前先去 `evidence/INDEX.md` 确认哪份产物是 `current`；
   文档正文里的数字一律视为该文档生成时的历史快照。
2. `archive/` 的文档不删、不合并、不回改数字——它们记录「为什么这么定」，
   与「现在是多少」是两件事，靠本表与 frontmatter 区分。
3. 新增文档必须带 frontmatter（status / scope / read_when），并登记到本表。
4. 实施记录按日期只增不改；需要更正时在原文加「补记」段落，不静默改写。
5. 其他 agent 回填内容前先读 [AGENTS.md](../AGENTS.md) §9「文档回填纪律」：
   类型映射、数字三件套、单点状态入口、代码同步清单都在那里；
   提交前跑 `backend/tests/test_docs_consistency.py` 兜底。
