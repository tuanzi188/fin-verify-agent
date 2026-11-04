---
status: active
scope: 金融查证 / 成本与缓存记账
authoritative_for: 当前待办与口径（§5 行动项）、有效输入估算定义
read_when: 接手成本优化、引用任何 token 数字前
warning: §3 的 H1/H2 假设已判定不成立，−60% 推算作废；修复后需重跑探针
---

# 成本优化计划（2026-11-04）

**前置声明：三套基线，收益不可叠加。** 下文所有优化项只对"当前 blocks 基线"计收益；
旧 sections 基线（42 题，48.7k/题）上的结论——兜底轮 40%→12.5%、搜索工具二选一、
单轮均价 5,550→4,509——**已经兑现进 blocks 基线，不得再算一遍**。
任何真实模型复跑前按项目纪律先报预算；探针先行，未过 20k 有效成本线不得写成项目结论。

---

## 0. 一页结论

1. 缓存记账已落地（代码 + 12 条测试，后端 567 passed），命中字段可信、
   **miss 字段不可信**（见 §2），有效输入一律按 `miss = tokens_in − hit` 推导。
2. ~~4 题探针实测发现第一优先级在网关侧：工具循环 26 次调用 0 命中~~
   **2026-11-04 判定实验推翻**：H1（写可见性延迟）与 H2（带 tools 键控差异）均不成立，
   带 tools 请求网关照常记账且写入立即可见。0 命中的真因是**客户端漏字段**：
   `_chat_with_tools_impl` 的 `usage_info` 没透传 `cache_hit/miss_tokens`，
   runner 按 `or 0` 记成 0。已修复并加行为测试，判定数据见
   `docs/evidence/gateway_cache_probe_20261104.json`。
3. 因此 §3 的「网关修好可省 60%」推算作废；修复后 tool_round 的真实命中率与省钱幅度
   **需重跑 4 题探针实测**（行动项已更新），不能沿用旧数字。§4-B 压历史的优先级
   也不再因「循环足额全价」自动升到最高，需按实测命中率重估。
   **2026-11-04 补充**：11 题 blocks 子集复测（`docs/evidence/finance_ms11_20261104.json`）
   已给出修复后的真实读数——逐题 `hit+miss==tokens_in`、合计 588,011 token、
   全价命中占比 **65.3%**、有效输入估算 226,093（`0.1` 折算，**估算非实测**）。
   这证明字段透传修复生效、旧「tool_round 0 命中」确属假象；但它是 11 题整体口径，
   `tool_round` 分阶段命中率仍待 4 题探针单独重跑（§4-A / §5-3 不撤）。
4. 产品层最大单项仍是路由分流（§4-D）：5 道金标 structured_rag 题在 Agent 臂
   均值 33.7k，走 RAG 3.4k。已实现，剩调准与真实金标。

---

## 1. 三套基线对照

| | 42 题 sections（旧） | 32 题 blocks（当前基线） | 4 题探针（2026-11-04） |
|---|---:|---:|---:|
| token/题（全价） | 48,743 | **33,898** | 46,403（硬题，不可直接比） |
| tool_round 占比 | 82.4% | 79.4% | 输入侧 **70.5%** |
| 单轮均价 | 5,550 | 4,509 | 4,604 |
| fallback_final | 17/42 题、11.7% token | **4/32 题、5.0%** | 2/4 题 |
| 搜索工具 | sections 98 + blocks 52 并存 | **仅 blocks（100 次）** | 仅 blocks |
| 工具调用/轮 | 1.84 | 1.62 | 1.62 |
| list_documents | 43/42（每题一次） | **34/32（仍每题一次）** | 4/4 |
| 精确重复组 | 14 | 4 | 0 |
| 落盘 | `docs/evidence/finance_answer_runs_20261103.json` | **未落盘（行动项 §5）** | `docs/evidence/finance_probe_cache_20261104.json` |

> 探针 4 题（CX-01/04/07/10）是复杂集最难的桶，token/题天然高于 32 题均值；
**可跨基线迁移的是比例（tool_round 占比、命中率），不是均值。**

---

## 2. 本次落地：cache 记账（已完成）

| 文件 | 改动 |
|---|---|
| `app/services/openai_client.py` | `_extract_cache_tokens()`：从 usage 取 `prompt_cache_hit_tokens / prompt_cache_miss_tokens`；取不到返回 (0,0)，全价记账 |
| `app/agent/runner.py` | `_record_llm_call` 逐调用记录 `cache_hit_tokens / cache_miss_tokens`；`_accumulate_tokens` 随调用累加；两处 usage 初始化补零键 |
| `evaluation/agent_metrics.py` | `CACHE_HIT_PRICE_RATIO = 0.1`（**计价假设，非网关实测折扣**）+ `effective_input_tokens(tokens_in, hit)`；签名只接受两个参数，全价部分一律由 `tokens_in − hit` 推导 |
| `evaluation/agent_report.py` | cross 汇总加 `cache_hit_tokens / cache_miss_tokens / effective_input_tokens`，并附 `cache_hit_price_ratio` + `effective_input_is_estimate: true` 自声明（估算 ≠ 实测）；控制台行同步标明折扣为假设 |
| `evaluation/audit_run_cost.py` | 逐题行 + 聚合 + 打印；usage 缺字段时从逐轮调用回退聚合；`--out` JSON 同样带自声明字段 |
| `tests/test_cache_accounting.py` | 15 条，全离线；含"估算值自声明"契约断言 |

**记账口径（2026-11-04 判定实验后定稿）：**

- `hit` 可信：有命中即真实省钱；
- **有效输入一律按 `miss = tokens_in − hit` 推导，不采信网关的 miss 字段**。
  判定实验前误以为「网关把带 tools 调用排除在记账外」（因为旧探针里
  `hit + miss = tokens_in − tool_round_in` 分毫不差），§3 已证伪：那是客户端
  `_chat_with_tools_impl` 漏字段造成的假象。即便如此，**保留按 tokens_in 推导
  的口径仍然更稳**——旧 runs / 不回字段的网关缺 hit 时按全价处理，宁可高估
  成本，不虚报折扣；不同网关的 miss 定义也不必逐个适配；
- 因此**有效输入 = (tokens_in − hit) + 0.1 × hit**；
- `effective_input_tokens()` 签名只收 `tokens_in` 与 `hit`，从签名层面杜绝把网关
  miss 传进来；报告 JSON 附 `cache_hit_price_ratio` 与 `effective_input_is_estimate: true`，
  明确这是**按计价假设折算的估算，不是网关实测成本**；
- 旧 runs 无缓存字段 → hit=0 → 全价，历史读数不受影响。

---

## 3. 探针「循环 0 命中」的判定结果（2026-11-04，已结案）

> 下表是**修复前**的读数，用于还原现象；`tool_round 0 命中` 是客户端漏字段的假象，
> 不是网关行为，也不代表当前代码的缓存命中率。结论见本节末，当前口径见 §2。

4 题探针（全价 185,615 token，含逐调用缓存字段），按阶段：

| 阶段 | 调用 | 输入 token | 命中 | 命中率 |
|---|---:|---:|---:|---:|
| **tool_round** | 26 | **119,693** | **0** | **0.0%** |
| fallback_final | 2 | 21,549 | 12,416 | 57.6% |
| self_check_supplement | 2 | 16,695 | 13,824 | 82.8% |
| post_gate_text:abstain_rewrite | 1 | 9,435 | 8,192 | 86.8% |
| self_check | 2 | 2,242 | 1,152 | 51.4% |

### 判定实验（8 次短请求，约 7k token）

对同一前缀连发两次，四组对照，原始数据 `docs/evidence/gateway_cache_probe_20261104.json`：

| 组 | 第一次 hit | 第二次 hit | 结论 |
|---|---:|---:|---|
| A 带 tools 紧连 | 512 | **640** | 写入立即可见 |
| B 不带 tools 紧连（对照） | 384 | 384 | 网关缓存正常工作 |
| C 带 tools 间隔 15s | 640 | 640 | 与紧连一致，无延迟 |
| D 带 tools → 不带 tools | 640 | 384 | 跨 tools 切换仍各自命中 |

> 四组共享同一前缀，第一次请求已预热，故本实验只判别「带 tools 是否记账 / 写入是否可见」，
> **不用于测量绝对命中率**。

**H1（写可见性延迟）与 H2（带 tools 键控差异）均不成立。**

### 真因：客户端漏字段，不是网关行为

- runner 的工具循环走 `model_provider.chat_with_tools`（`runner.py:991`）；
- `openai_client._chat_with_tools_impl` 的 `usage_info` 此前只有
  prompt/completion/total/finish_reason/has_reasoning，**没有** `cache_hit/miss_tokens`
  （不带 tools 的 `_chat_impl` 有）；
- 于是 `round_usage` 缺这两项 → `_record_llm_call` 按 `or 0` 记 0 →
  `_accumulate_tokens` 累加 0 → 26 次 tool_round 全 0；
- 后置调用（fallback/self_check）走不带 tools 的 `chat()`，字段齐全，故显示 51–87% 命中。

**修复**：`_chat_with_tools_impl` 补 `_extract_cache_tokens(response.usage)` 与两个字段，
与 `_chat_impl` 对齐；新增行为测试
`tests/test_cache_accounting.py::test_chat_with_tools_impl_propagates_cache_fields`。

**作废的结论**：原「网关修好后 38.4k → 15.5k/题（−60%）、Agent 代码零改动」建立在
「循环足额全价」的错误前提上，不再成立。修复后 tool_round 会有真实命中，
但**命中率与省钱幅度必须重跑 4 题探针实测**，不能推算。§4-B 的优先级按实测重估。

---

## 4. 优化项（重排后）

### A. 客户端缓存字段透传（已完成）+ 探针重跑（待做）

- **已完成**：`_chat_with_tools_impl` 补字段 + 行为测试（见 §3）。
- **已有修复后实测（11 题复测，2026-11-04）**：`docs/evidence/finance_ms11_20261104.json`
  的 11 题逐题 `cache_hit_tokens + cache_miss_tokens == tokens_in`，全价命中占比
  65.3%（hit 357,504 / in 547,847），有效输入估算 226,093。这是字段透传修复稳定生效的
  直接证据，旧探针「tool_round 26 次调用 0 命中」的假象不再出现。
- **待做**：修复后重跑 4 题探针，读 `tool_round` 分阶段真实命中率与有效输入：
  `python -m evaluation.run_finance_eval --tasks evaluation/finance_tasks_complex.jsonl --ids CX-01,CX-04,CX-07,CX-10 --search-mode blocks --runs 1 --out docs/evidence/finance_probe_cache_20261104_v2.json`
  再用 §2 记账口径跑 `audit_run_cost`。真实模型调用，按纪律先报预算上限。
- **回滚**：字段透传是纯记账改动，revert 即回，不影响答案。

### B. 循环历史压缩（优先级按 A 重跑后的实测命中率决定）

- **动作**：保留最近 1–2 轮工具结果原文，更早的 `read_section/get_table` payload
  压成一行摘要 + 坐标（数字已进证据卡，`finance_history.py` 落盘同款原则）。
- **验证**：`python -m evaluation.audit_run_cost --runs <探针/全量 runs>` 前后对照
  `tool_round` 输入均值（4,604/轮 → 目标 ≤3,500）。
- **回滚**：压缩深度做成 runner 参数（默认关），出问题一键回退。
- **优先级**：原「A 不可行 → 循环足额全价 → 此项升到最高」的前提已作废。
  A 重跑后若 tool_round 命中率低，压历史的收益按全价计、优先级高；命中率高则
  按折扣计、收益变小。先拿实测数，再排。

### C. 固定开销瘦身

- **动作**：`list_documents` 仪式轮（34/32，仍每题一次）→ 7 份文档元数据进
  system prompt，省一整轮；`self_check` 0.9 次/题维持现状（便宜且买质量）。
- **注意**：system 属于缓存前缀，其成本近似一次性——此项收益要在 A 重跑给出
  真实命中率后重算，别提前下结论。
- **验证**：同 B，看平均轮数（7.3 → 6.3）与 `llm_calls`。
- **回滚**：工具清单进 prompt 是纯提示词改动，revert 即回。

### D. 路由分流（产品层最大单项，已实现，剩调准）

- **现状**：5 道金标 structured_rag 题在 Agent 臂均值 33.7k vs RAG 3.4k；
  phase0 边界题 5/6 过度升级到 Agent。
- **动作**：按 ROUTING_POLICY v2 把"同比/占比/多问号"从硬信号降级为
  "是否必须跨表/跨文档/计算"；改完 `routing_metrics` 回归。
- **回滚**：router 规则集中在 `routing_policy.py`，git revert 即回。

### E. 尾部两题（FIN-11 + CX-10，占 14.3%）

- **动作**：`token_budget` 接行为（超 80% 注入收口提示，硬顶前压缩再答）。
- **边界**（用户已警告）：这两题是打转题，再压会直接伤正确率——目标是封顶
  浪费，不是砍轮次预算。
- **回滚**：预算参数默认值不变即回。

### 明确不做

- **拆修复闸门**（cell_repair/calc_repair/coverage）：全量仅 12,493 token（0.6%），
  CX-04 从"读一份年报就拒答"到 4 条单元格引用全对靠的是它们。省钱靠让第一遍
  就对（A/B/C），不拆保险丝。

---

## 5. 行动项与纪律

1. **32 题 blocks 基线 runs 落盘** `docs/evidence/`（当前只在本地审计过，
   不落盘则 §1 的对照表不可复现）；
2. ✅ H1/H2 判定实验已完成并记回 §3，数据 `docs/evidence/gateway_cache_probe_20261104.json`；
   结论是两个假设都不成立，真因是客户端漏字段，已修复；
2a. ✅ **11 题 blocks 子集复测已完成**（2026-11-04）：runs / 报告 / badcase 落
   `docs/evidence/finance_ms11_20261104*`，合计 588,011 token 未超 ≤1.2M 预算；
   修复后逐题 `hit+miss==tokens_in`、全价命中占比 65.3%。读数登记见
   `docs/evidence/INDEX.md` §A / §D。注意它与 11-03 旧基线条件不同，不写「提升」。
3. **A 重跑 4 题探针**（修复字段透传后），拿 tool_round 真实命中率与有效输入；
   旧的 38.4k→15.5k 推算作废，不写进任何结论。11 题复测已给出整体命中率，
   但分阶段 tool_round 数仍需这道探针补齐，不因 11 题数据而跳过；
4. 预注册的 ≤20k 红线是**全价增量 token** 口径（Agent − RAG）。缓存生效后
   "有效成本达标" ≠ 预注册达标——两个口径分开报，不混写；
5. 任何真实模型复跑先报预算上限；探针先行，全量后置。

---

## 附：探针原始读数（2026-11-04）

- runs：`docs/evidence/finance_probe_cache_20261104.json`（4 题 × 1 次，blocks 模式，无 judge）
- 全价 185,615（in 169,614 / out 16,001）；hit 35,584；网关报 miss 14,337。
  **注意**：此处 hit/miss 只覆盖非循环调用，不是网关选择性记账，而是
  `_chat_with_tools_impl` 漏了字段透传（见 §3 判定结果）；该产物是修复前的读数，
  修复后必须重跑，不代表当前客户端的真实缓存命中。
- 逐题：CX-01 in 49,322 hit 7,296；CX-04 in 35,337 hit 7,296；CX-07 in 36,078 hit 7,680；CX-10 in 48,877 hit 13,312
- 状态：3 answered + 1 abstained（CX-10 拒答正确：语料无智能家居研发费用分项）
- 记账代码版本：工作区快照（含本次 cache 记账改动，未提交）
