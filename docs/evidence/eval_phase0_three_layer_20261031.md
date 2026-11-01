# 金融查证 Agent · 三层评测报告

生成时间：2026-10-31T22:28:15

## 1. 检索层
主口径 K=5（first_search，双臂同尺）

| 臂 | 要点命中 | 整题全中 | MRR | context precision | read rate | 平均检索次数 |
|---|---|---|---|---|---|---|
| structured | 30/43 (69.8%) | 33.3% | 0.4963 | 17.8% | 0.0% | 1.0 |
| fixed | 18/43 (41.9%) | 11.1% | 0.2 | 11.1% | 0.0% | 1.0 |

> first_search 是主口径；any_search（Agent 所有检索并集）只在 JSON 里作诊断。

## 2. 路由层
- 准确率 80.0% （30 题，Wilson [0.6269, 0.9049]）
- Macro-F1：0.775
- 错分代价：简单题误走 Agent 5 题；复杂题误走结构检索 1 题

| 任务 | 金标 | 实际 | 代价 | 理由 |
|---|---|---|---|---|
| FIN-01 | structured_rag | agent | cost_waste:simple_to_agent | 问题包含多个子问题，需要多步拆解 |
| FIN-02 | structured_rag | agent | cost_waste:simple_to_agent | 问题包含多个子问题，需要多步拆解 |
| FIN-03 | structured_rag | agent | cost_waste:simple_to_agent | 问题包含多个子问题，需要多步拆解 |
| FIN-S03 | agent | structured_rag | quality_risk:complex_to_rag | 单公司/单期间/单指标的查数，一次结构检索即可 |
| FIN-S04 | structured_rag | agent | cost_waste:simple_to_agent | 问题包含比较/计算信号：占营业 |
| FIN-S05 | structured_rag | agent | cost_waste:simple_to_agent | 问题包含比较/计算信号：同比 |

## 3. 答案层
- no_data：没有答案层报告；先跑 evaluation.score_agent_runs

## 4. 数值审计
- no_data：没有数值审计；先跑 evaluation.numeric_audit

## 5. 轨迹层
- no_data：没有轨迹评测；先跑 evaluation.trajectory_eval

## 6. 忠实度（RAGAS）
- no_data：没有忠实度；探针级可选并跑 evaluation.faithfulness

## 7. Release Gate
- 总体：未评估（缺可拦截项）
- 拦截失败：无
- 提示失败：['business.routing_accuracy']
- 金融红线：safety.numeric_hallucination_rate, safety.fabrication, business.false_refusal_rate

## 8. 分岔诊断（失败题先过检索段）
- no_data：需要同时有答案层报告与检索层结果

## 9. 读数口径
- 检索层 first_search 是双臂同尺口径；any_search 是 Agent 诊断口径，不能与一次检索的 RAG 直接比。
- 答案层的 pass@1 / pass@k / pass^k 只在完整任务上计算；Wilson 区间只反映样本量，不含 run-to-run 方差。
- 路由层的金标来自任务卡元数据；拒答/澄清/假设类不判更优臂，只报分流分布。
- 数值幻觉率只统计 judge 判 pass 但金标数值缺失的要点；未溯源数值是提示项，不直接当红线。
- 缺层的指标写 no_data，不拿 0 顶替；no_data 不等于通过。
