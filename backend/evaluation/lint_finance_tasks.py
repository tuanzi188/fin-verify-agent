"""金融任务集专项 lint：结构检查之外，**把每个数字按回 PDF 表格核对一遍**。

为什么必须有这一层：制度库任务卡的错误是"chunk_id 打错一个字母"，顶多让召回
诊断偏低；金融任务卡的错误是"任务卡里的数字和 PDF 不一致"——判分器会拿一个
错的期望值去判 Agent 的对答，Agent 答对了反而判失败，而且**没有任何地方会报错**。
这类错误只能靠机器对着 document.json 现查。

四条硬检查：

| 检查 | 防的是什么 |
|---|---|
| `evidence` 指到真实单元格、值与 `cell_text` 一致 | 任务卡写错数字 / 抄错行 |
| `unit` 与表格单位一致（不一致必须写 `unit_note`） | 千元当元 |
| `derived` 要点必须带 `calc`，且公式跑出的值与 `expected` 同 | 手算 / 拍脑袋填的比率 |
| 压力集任务的 `source` 与 `defect` 必须写清 | "这条题到底在测哪个已知缺陷"说不出来 |

另外两条软检查（warning）：要点文本里没出现该数字；留出集不是 5–7 条。

两个可选硬检查（2026-11-03 起，默认关闭、按需开）：

- `--check-refs`：`chunk_ids`/`relevant_chunk_ids` 必须存在，且引用内容里
  仍能读到金标值——防表 id 重排后的静默漂移（此前只有 evidence.table_id 被机检）；
- `--check-routing`：任务金标 vs 当前 `initial_decision`——clarify 题必须澄清、
  非边界题不得被拦、`route_target` 必须与路由一致，产品规则改了这里会先红。
"""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from app.finance.calc import calculate
from app.finance.store import FinanceStore
from evaluation.calc_schema import calc_specs, has_calc_spec
from evaluation.finance_corpus import DOCUMENT_DIR, chunk_map
from evaluation.lint_agent_tasks import DIMENSIONS, REQUIRED_FIELDS, load_tasks
from evaluation.lint_agent_tasks import lint as base_lint

DEFAULT_PROFILE = "evaluation/finance_tasks.jsonl"
DEFAULT_STRESS = "evaluation/finance_tasks_stress.jsonl"
HOLDOUT_RANGE = (5, 7)
#: 冻结配比（2026-11-03 起 error 级）：主集 5 维×6、压力集 10、复杂集 4 桶×3。
#: 想增减题目先改这里的常量——把"配比漂移"从人眼盯变成 lint 红灯。
PROFILE_TASK_COUNT = 30
PROFILE_DIMENSION_COUNT = 6
STRESS_TASK_COUNT = 10
COMPLEX_BUCKET_COUNT = 3
#: 金融集必填字段 = 通用清单 − `required_args`（2026-11-03）。
#: `required_args` 是通用制度库的工具参数约束，金融线零消费（52 题全写 `{}`），
#: 金融工具层的调用约束由 `tool_constraints` 表达，两套不要混。
FINANCE_REQUIRED_FIELDS = tuple(
    field for field in REQUIRED_FIELDS if field != "required_args"
)
_DIGITS = str.maketrans("", "", " ,\u00a0")


def _norm(value: object) -> str:
    return str(value or "").translate(_DIGITS).strip()


def _as_decimal(value: object) -> Decimal | None:
    try:
        return Decimal(_norm(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _lint_evidence(task: dict, chunks: dict[str, dict], store: FinanceStore) -> list[str]:
    errors: list[str] = []
    tag = task.get("id") or "?"
    table_cache: dict[str, dict] = {}
    for index, point in enumerate(task.get("answer_points") or []):
        if point.get("derived") or point.get("kind") == "note":
            # 派生数值与定性结论没有单元格可对：
            # 前者由 `_lint_calc` 的公式复核接管，后者由 judge 判
            continue
        evidence = point.get("evidence")
        if not isinstance(evidence, dict):
            errors.append(f"[{tag}] answer_points[{index}] 缺少 evidence：数字必须能对回表格单元格")
            continue
        table_id = str(evidence.get("table_id") or "")
        row = evidence.get("row")
        column = evidence.get("column")
        value = evidence.get("value")
        if not (table_id and row is not None and column is not None and value is not None):
            errors.append(f"[{tag}] answer_points[{index}] evidence 缺 table_id/row/column/value")
            continue
        if table_id not in chunks:
            errors.append(f"[{tag}] answer_points[{index}] evidence.table_id 不存在: {table_id}")
            continue
        try:
            cell = store.get_cell(table_id, row, column)
        except Exception as exc:  # noqa: BLE001 - 取不到就是任务卡写错了
            errors.append(
                f"[{tag}] answer_points[{index}] 取不到单元格 {table_id} {row!r}/{column!r}: "
                f"{type(exc).__name__}: {exc}"
            )
            continue

        if _norm(cell.get("cell_text")) != _norm(value):
            errors.append(
                f"[{tag}] answer_points[{index}] 值与表格不一致：任务卡 {value!r} "
                f"vs 表格 {cell.get('cell_text')!r}"
            )

        point_unit = str(point.get("unit") or "")
        if table := table_cache.get(table_id) or store.get_table(table_id):
            table_cache[table_id] = table
            table_unit = str(table.get("unit") or "")
            if point_unit and table_unit and point_unit != table_unit:
                if not point.get("unit_note"):
                    errors.append(
                        f"[{tag}] answer_points[{index}] 单位不一致：要点 {point_unit!r} "
                        f"vs 表格 {table_unit!r}（折算必须在 unit_note 里说明）"
                    )

        # 符号常被写进措辞而不写进数字：单元格 `-33.46` 对答案里的「下降 33.46%」，
        # 拿带负号的串去找必然找不到。只在"有没有提到这个数"这一层去掉符号。
        needle = _norm(value).lstrip("+-")
        if _as_decimal(value) is not None and needle not in _norm(point.get("text")).lstrip("+-"):
            errors.append(
                f"[{tag}] answer_points[{index}] 要点文本里没有出现这个数字："
                f"{value!r} → {point.get('text')!r}"
            )
    return errors


def _lint_calc(task: dict) -> tuple[list[str], list[str]]:
    """派生数值的公式必须能跑出同一个值。

    `calc` 支持单对象或数组（2026-11-01 审计 #2）：数组里每个需要复算的
    数值一条公式，逐条跑 `calculate` 复核；只验证第一条等于没验证其余
    派生数字。calc spec 允许没有派生要点（如 FIN-11 的单位换算）。
    """
    errors: list[str] = []
    warnings: list[str] = []
    tag = task.get("id") or "?"
    derived = [
        index
        for index, point in enumerate(task.get("answer_points") or [])
        if isinstance(point, dict) and point.get("derived")
    ]
    specs = calc_specs(task)
    if derived and not specs:
        errors.append(f"[{tag}] 有派生要点 {derived} 但没有 calc：比率必须给公式与输入")
        return errors, warnings

    for index, spec in enumerate(specs):
        label = "calc" if len(specs) == 1 else f"calc[{index}]"
        formula = spec.get("formula")
        inputs = spec.get("inputs")
        expected = spec.get("expected")
        if not (isinstance(formula, str) and formula.strip() and isinstance(inputs, dict) and inputs):
            errors.append(f"[{tag}] {label} 缺 formula 或 inputs")
            continue
        try:
            outcome = calculate(formula, inputs)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"[{tag}] {label} 公式跑不通: {type(exc).__name__}: {exc}")
            continue

        actual = _as_decimal(outcome.get("result"))
        want = _as_decimal(expected)
        if actual is None or want is None:
            errors.append(f"[{tag}] {label} 结果或 expected 不是数字: {outcome.get('result')!r} / {expected!r}")
            continue
        tolerance = _as_decimal(spec.get("tolerance")) or Decimal("0.02")
        if abs(actual - want) > tolerance:
            errors.append(
                f"[{tag}] {label} 值与 expected 不符：公式算出 {actual} vs expected {want}（容差 {tolerance}）"
            )
        if not spec.get("note"):
            warnings.append(f"[{tag}] {label} 没写 note：报告里说不清这个数与年报披露值的关系")
    return errors, warnings


def _lint_tier(
    tasks: list[dict], tier: str, *, holdout_only: bool = False
) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    if tier == "complex":
        # 复杂集的路数由 `_lint_complex` 单独管：它有四桶冻结留出，不套主集的维度配比
        return errors, warnings
    if tier == "stress":
        if len(tasks) != STRESS_TASK_COUNT:
            errors.append(
                f"压力集必须恰好 {STRESS_TASK_COUNT} 条（10 个已知缺陷各 1），当前 {len(tasks)}"
            )
        for task in tasks:
            tag = task.get("id") or "?"
            if task.get("source") not in ("failure", "synthetic"):
                errors.append(f"[{tag}] 压力集 source 必须是 failure/synthetic，当前 {task.get('source')!r}")
            if not task.get("defect"):
                errors.append(f"[{tag}] 压力集必须写 defect：这条题对应哪个已知缺陷")
        return errors, warnings

    if not holdout_only:
        # 5 维×6 是冻结配比：误删一条要红，而不是只打印。
        if len(tasks) != PROFILE_TASK_COUNT:
            errors.append(
                f"主集必须恰好 {PROFILE_TASK_COUNT} 条（5 维×6 冻结配比），当前 {len(tasks)}"
            )
        counter: dict[str, int] = {}
        for task in tasks:
            name = str(task.get("dimension") or "")
            counter[name] = counter.get(name, 0) + 1
        for name in DIMENSIONS:
            if counter.get(name, 0) != PROFILE_DIMENSION_COUNT:
                errors.append(
                    f"维度「{name}」必须恰好 {PROFILE_DIMENSION_COUNT} 条，当前 {counter.get(name, 0)}"
                )

    holdout = [task.get("id") for task in tasks if task.get("holdout")]
    low, high = HOLDOUT_RANGE
    if not low <= len(holdout) <= high:
        warnings.append(f"留出集 {len(holdout)} 条，设计目标 {low}–{high}：{holdout}")
    for task in tasks:
        if task.get("source") == "failure":
            warnings.append(f"[{task.get('id')}] 主集里出现 source=failure：回归用例放压力集")
    return errors, warnings


COMPLEX_TYPES = ("cross_table", "cross_doc", "calc", "insufficient")
COMPLEX_MIN_TASKS = 12
ROUTE_TARGETS = ("agent", "structured_rag", "indeterminate")
PROMPT_STYLES = ("open_question", "direct_constraint")
ANSWER_OPERATIONS = {
    "lookup",
    "compare",
    "calculate",
    "abstain",
    "clarify",
    "recover",
    "validate",
    "cite",
}

#: `retrieval_hops` 是**证据单元桶**，不是精确跳数（2026-11-01 定稿）：
#: 1 = 所有金标位置落在同一张表/章节；2 = 跨 ≥2 个证据单元；
#: 0 = 无金标位置（纯拒答/纯定性）。FIN-11 涉及 6 张表也记 2——
#: 不要把它当精确值用于任何计算。
HOP_BUCKET_MAX = 3

#: `prompt_style` 判定口径（2026-11-01 定稿）：
#: - `direct_constraint` = 题面出现显式指令（请给出/请确认/请换算/请注明/
#:   请核对/请列出/分别给出/自行算一遍），**或**封闭判定问句
#:   （是否超过/是否一致 且要求给出具体数值）；
#: - `open_question` = 自然提问，无格式/口径/单位约束。
#: 两类分开统计：direct_constraint 考的是"按指令执行 + 校验"，
#: 不得把它的通过率混进"开放场景能力"。改动口径必须同步改这里并留日期。
DIRECT_STYLE_PHRASES = (
    "请给出",
    "请确认",
    "请换算",
    "请注明",
    "请核对",
    "请列出",
    "分别给出",
    "自行算一遍",
)

#: Case Schema v2 允许的工具名（与 FinanceToolset 的 specs 对齐）。
KNOWN_TOOLS = {
    "list_documents",
    "search_sections",
    "search_blocks",
    "read_section",
    "read_block",
    "get_table",
    "get_rows",
    "get_cell",
    "calculate",
}
FINAL_STATE_TYPES = {"numeric", "text", "abstain", "cites"}


def _lint_schema_v2(task: dict) -> tuple[list[str], list[str]]:
    """校验方案 §3.5 的 Case Schema 扩展字段（全部可选，缺省不报错）。

    这些字段不是装饰：`route_target` 给路由层当设计金标，`allowed_tools` /
    `forbidden_actions` 给轨迹层判越界，`token_budget` 给成本闸门，
    `final_state` 给"答案终态可程序化验证"留接口。字段写错比不写更危险，
    所以类型/取值都要在 lint 里卡住。
    """
    errors: list[str] = []
    warnings: list[str] = []
    tag = task.get("id") or "?"

    route_target = task.get("route_target")
    if route_target is not None and route_target not in ROUTE_TARGETS:
        errors.append(
            f"[{tag}] route_target 非法: {route_target!r}，只能是 {ROUTE_TARGETS}"
        )

    # 旧字段保留给历史报告读取，新任务统一使用 route_target。
    expected_arm = task.get("expected_arm")
    if expected_arm is not None and expected_arm not in ("agent", "structured_rag"):
        errors.append(f"[{tag}] expected_arm 非法: {expected_arm!r}，只能是 agent/structured_rag")

    prompt_style = task.get("prompt_style")
    if prompt_style is not None and prompt_style not in PROMPT_STYLES:
        errors.append(
            f"[{tag}] prompt_style 非法: {prompt_style!r}，只能是 {PROMPT_STYLES}"
        )

    retrieval_hops = task.get("retrieval_hops")
    if retrieval_hops is not None and (
        not isinstance(retrieval_hops, int) or not 0 <= retrieval_hops <= HOP_BUCKET_MAX
    ):
        errors.append(
            f"[{tag}] retrieval_hops 必须是 0–{HOP_BUCKET_MAX} 的整数（证据单元桶），当前 {retrieval_hops!r}"
        )

    answer_operations = task.get("answer_operations")
    if answer_operations is not None:
        if not isinstance(answer_operations, list) or not answer_operations:
            errors.append(f"[{tag}] answer_operations 必须是非空数组")
        else:
            unknown = sorted(set(answer_operations) - ANSWER_OPERATIONS)
            if unknown:
                errors.append(f"[{tag}] answer_operations 有未知操作: {unknown}")

    # ------------------------------------------------------------------
    # route_target ↔ 结构字段一致性（2026-11-01 补）：
    # 三态金标如果和题目结构自相矛盾，路由准确率就会被悄悄污染。
    # 现有 52 题全部通过 error 级检查；warning 级只提示人工复核。
    # ------------------------------------------------------------------
    calc_ok = has_calc_spec(task)
    has_derived = any(
        isinstance(point, dict) and point.get("derived")
        for point in task.get("answer_points") or []
    )
    needs_calculator = calc_ok or has_derived
    operations = set(answer_operations or [])

    # answer_operations.calculate ⇔ calc spec/派生要点（2026-11-01 审计 #5 起 error 级）：
    # 纯披露值（如 FIN-04 的 ROE 21.29%）不许标 calculate——分栏会高估计算工作量；
    # 反之带 calc/派生要点却漏标也会低估。要计算（含单位换算）必须给 calc spec。
    if "calculate" in operations and not needs_calculator:
        errors.append(
            f"[{tag}] answer_operations 含 calculate 但无 calc/派生要点："
            "披露值请把 calculate 从 answer_operations 移除；"
            "确需计算（含单位换算）请补 calc spec（参见 FIN-11）"
        )
    if needs_calculator and "calculate" not in operations:
        errors.append(
            f"[{tag}] 带 calc/派生要点但 answer_operations 缺 calculate："
            "分栏会低估计算工作量，请补标签"
        )

    if route_target == "structured_rag":
        if isinstance(retrieval_hops, int) and retrieval_hops >= 2:
            errors.append(
                f"[{tag}] route_target=structured_rag 但 retrieval_hops={retrieval_hops}"
                "（跨证据单元需要多步检索，结构 RAG 只有一次机会）"
            )
        if needs_calculator:
            errors.append(
                f"[{tag}] route_target=structured_rag 但任务带 calc/派生要点"
                "（计算器只给 Agent 臂，两臂工具不同会让对照失效）"
            )

    # 纯拒答/澄清题没有金标位置：证据单元桶必须为 0（2026-11-01 审计 #4）。
    # FIN-22 这类「澄清后按假设作答」的题带着金标 chunk，不在此列。
    if (
        isinstance(retrieval_hops, int)
        and retrieval_hops != 0
        and (task.get("should_abstain") or str(task.get("clarify_mode") or "none") != "none")
        and not (task.get("relevant_chunk_ids") or [])
    ):
        errors.append(
            f"[{tag}] 纯拒答/澄清题（无金标位置）retrieval_hops 应为 0，当前 {retrieval_hops}"
        )
    if route_target == "agent":
        if (
            isinstance(retrieval_hops, int)
            and retrieval_hops <= 1
            and not needs_calculator
            and "calculate" not in operations
        ):
            warnings.append(
                f"[{tag}] route_target=agent 但单证据单元且无计算/派生："
                "两臂都可能做对，建议改 structured_rag 或 indeterminate 并写明理由"
            )

    # prompt_style 口径漂移检查：题面有显式指令却标 open_question
    prompt_style = task.get("prompt_style")
    if prompt_style == "open_question":
        question_text = str(task.get("question") or "")
        hits = [phrase for phrase in DIRECT_STYLE_PHRASES if phrase in question_text]
        if hits:
            warnings.append(
                f"[{tag}] prompt_style=open_question 但题面含显式指令 {hits}："
                "按 2026-11-01 口径应标 direct_constraint"
            )

    expected_source = task.get("expected_source")
    if expected_source is not None:
        items = expected_source if isinstance(expected_source, list) else [expected_source]
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                errors.append(f"[{tag}] expected_source[{index}] 必须是对象")
                continue
            kind = str(item.get("kind") or "")
            if kind not in ("table", "section"):
                errors.append(f"[{tag}] expected_source[{index}].kind 非法: {kind!r}")
            position = item.get("id") or item.get("table_id") or item.get("section_id")
            if not position:
                errors.append(f"[{tag}] expected_source[{index}] 缺少 id/table_id/section_id")

    allowed_tools = task.get("allowed_tools")
    if allowed_tools is not None:
        if not isinstance(allowed_tools, list):
            errors.append(f"[{tag}] allowed_tools 必须是数组")
        else:
            unknown = sorted({str(name) for name in allowed_tools} - KNOWN_TOOLS)
            if unknown:
                errors.append(f"[{tag}] allowed_tools 有未知工具: {unknown}")

    forbidden_actions = task.get("forbidden_actions")
    if forbidden_actions is not None:
        if not isinstance(forbidden_actions, list) or not all(
            isinstance(item, str) and item.strip() for item in forbidden_actions
        ):
            errors.append(f"[{tag}] forbidden_actions 必须是非空字符串数组")

    max_steps = task.get("max_steps")
    if max_steps is not None and (not isinstance(max_steps, int) or not 1 <= max_steps <= 20):
        errors.append(f"[{tag}] max_steps 必须是 1–20 的整数，当前 {max_steps!r}")

    token_budget = task.get("token_budget")
    if token_budget is not None and (
        not isinstance(token_budget, int) or not 1000 <= token_budget <= 200000
    ):
        errors.append(f"[{tag}] token_budget 必须是 1000–200000 的整数，当前 {token_budget!r}")

    final_state = task.get("final_state")
    if final_state is not None:
        if not isinstance(final_state, dict):
            errors.append(f"[{tag}] final_state 必须是对象")
        elif str(final_state.get("type") or "") not in FINAL_STATE_TYPES:
            errors.append(
                f"[{tag}] final_state.type 非法: {final_state.get('type')!r}，"
                f"只能是 {sorted(FINAL_STATE_TYPES)}"
            )

    required_sequence = task.get("required_sequence")
    if required_sequence is not None:
        if not isinstance(required_sequence, list) or not required_sequence:
            errors.append(f"[{tag}] required_sequence 必须是非空数组")
        else:
            for index, item in enumerate(required_sequence):
                name = item.get("tool") if isinstance(item, dict) else item
                if not isinstance(name, str) or not name.strip():
                    errors.append(f"[{tag}] required_sequence[{index}] 缺少工具名")

    key_tools = task.get("key_tools")
    if key_tools is not None:
        if not isinstance(key_tools, list) or not all(isinstance(item, str) for item in key_tools):
            errors.append(f"[{tag}] key_tools 必须是字符串数组")
        else:
            unknown = sorted(set(key_tools) - KNOWN_TOOLS)
            if unknown:
                warnings.append(f"[{tag}] key_tools 含未知工具（可能是伪工具 search/read）: {unknown}")

    return errors, warnings


def _lint_complex(tasks: list[dict]) -> tuple[list[str], list[str]]:
    """复杂集是「Agent 多花的钱值不值」的主判据，跑前必须固定且分桶可核。"""
    errors: list[str] = []
    warnings: list[str] = []
    if len(tasks) != COMPLEX_MIN_TASKS:
        errors.append(
            f"复杂集必须恰好 {COMPLEX_MIN_TASKS} 条（4 桶×{COMPLEX_BUCKET_COUNT} 冻结配比），"
            f"当前 {len(tasks)}：少一题 15 个百分点的分辨率就退化成一道题"
        )
    holdout_counts = {kind: 0 for kind in COMPLEX_TYPES}
    for task in tasks:
        tag = task.get("id") or "?"
        kind = str(task.get("complex_type") or "")
        if kind not in COMPLEX_TYPES:
            errors.append(f"[{tag}] complex_type 非法: {kind!r}，只能是 {COMPLEX_TYPES}")
        if task.get("holdout"):
            if kind in holdout_counts:
                holdout_counts[kind] += 1
        if task.get("tool_constraints"):
            errors.append(f"[{tag}] 复杂集不许写 tool_constraints：两臂工具不同，写工具名会让对照胜负由任务卡决定")
    counts = {kind: 0 for kind in COMPLEX_TYPES}
    for task in tasks:
        kind = str(task.get("complex_type") or "")
        if kind in counts:
            counts[kind] += 1
    for kind, count in counts.items():
        if count == 0:
            errors.append(f"复杂集缺少 {kind} 桶：四桶必须都有，否则回答不了'哪类问题 Agent 才值'")
        elif count != COMPLEX_BUCKET_COUNT:
            errors.append(
                f"复杂集 {kind} 桶必须恰好 {COMPLEX_BUCKET_COUNT} 条（4 桶×3 冻结），当前 {count}"
            )
        if count >= 3 and holdout_counts[kind] != 1:
            errors.append(
                f"复杂集 {kind} 桶必须恰好 1 条冻结留出，当前 {holdout_counts[kind]} 条"
            )
    return errors, warnings


def _lint_refs(task: dict, chunks: dict[str, dict]) -> list[str]:
    """三处坐标一致性：chunk_ids / relevant_chunk_ids 必须存在且支撑金标值。

    以前只有 `evidence.table_id` 被机检；另外两处漂移只能靠人眼——2026-11-03
    重解析后 FIN-05/28/29 的 chunk_ids 曾指向旧表，lint 0 error 却掩盖了它。
    块级 id（带 #rows#/#b 后缀）目前只验锚点存在性；内容校验留给块级检查。
    """
    errors: list[str] = []
    tag = task.get("id") or "?"
    refs = list(
        dict.fromkeys(
            [
                *(str(ref) for ref in (task.get("relevant_chunk_ids") or [])),
                *(
                    str(ref)
                    for point in task.get("answer_points") or []
                    for ref in (point.get("chunk_ids") or [])
                ),
            ]
        )
    )
    anchor_of: dict[str, str] = {}
    for ref in refs:
        if ref in chunks:
            anchor_of[ref] = ref
            continue
        anchor = "#".join(ref.split("#")[:2])
        if "#" in ref and anchor in chunks:
            anchor_of[ref] = anchor
            continue
        errors.append(f"[{tag}] 引用不存在: {ref}")
    for index, point in enumerate(task.get("answer_points") or []):
        value = (point.get("evidence") or {}).get("value")
        if value is None:
            continue
        point_refs = [str(ref) for ref in (point.get("chunk_ids") or [])] or refs
        haystack = " ".join(
            _norm(chunks[anchor_of[ref]].get("content") or "")
            for ref in point_refs
            if ref in anchor_of
        )
        needle = _norm(value).lstrip("+-")
        if haystack and needle not in haystack.lstrip("+-"):
            errors.append(
                f"[{tag}] answer_points[{index}] 引用内容不含金标值 {value!r}："
                f"坐标可能已漂移（表 id 重排后需重绑）"
            )
    return errors


def _lint_routing(task: dict, documents: list[dict]) -> list[str]:
    """任务金标与当前路由策略的一致性——规则改了就让它变红。

    只做两向可判定的断言：`clarify_mode=ask` 必须澄清；非边界任务不得被拦。
    `should_abstain` 任务不判——"证据不足型拒答"需要闸门放行、由 Agent 检索后
    拒答，与"越界型拒答"（闸门直接拦）混在同一字段里，无法自动区分（2026-11-03）。
    """
    from app.finance.routing_policy import initial_decision

    tag = task.get("id") or "?"
    if task.get("mock"):
        return []  # mock 题测行为注入（超时/重试），不对路由作断言
    question = str(task.get("question") or "")
    if not question:
        return []
    decision = initial_decision(question, documents)
    blocked = decision.initial_path in ("clarify", "abstain")
    errors: list[str] = []
    if task.get("clarify_mode") == "ask" and decision.initial_path != "clarify":
        errors.append(
            f"[{tag}] clarify_mode=ask 但当前路由给 {decision.initial_path}"
            f"（{decision.rule_id}）"
        )
    if not task.get("should_abstain") and task.get("clarify_mode") != "ask" and blocked:
        errors.append(
            f"[{tag}] 非边界任务被路由拦截：{decision.initial_path}/{decision.rule_id}"
        )
    target = task.get("route_target")
    if target in ("agent", "structured_rag") and not blocked and decision.initial_path != target:
        errors.append(
            f"[{tag}] route_target={target} 但当前路由是 {decision.initial_path}"
            f"（{decision.rule_id}）"
        )
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="金融任务集 lint")
    parser.add_argument("--tasks", default=DEFAULT_PROFILE)
    parser.add_argument("--tier", choices=("profile", "stress", "complex"), default="profile")
    parser.add_argument("--holdout-only", action="store_true", help="只看留出集")
    parser.add_argument(
        "--check-refs",
        action="store_true",
        help="校验 chunk_ids/relevant_chunk_ids 存在且内容含金标值（坐标漂移检查）",
    )
    parser.add_argument(
        "--check-routing",
        action="store_true",
        help="校验任务金标与当前路由策略一致（规则漂移检查）",
    )
    args = parser.parse_args()

    tasks_path = Path(args.tasks)
    if not tasks_path.is_absolute():
        tasks_path = _BACKEND_DIR / tasks_path
    if not tasks_path.exists():
        raise SystemExit(f"任务文件不存在: {tasks_path}")

    tasks = load_tasks(tasks_path)
    if args.holdout_only:
        tasks = [task for task in tasks if task.get("holdout")]
    chunks = chunk_map()
    store = FinanceStore(DOCUMENT_DIR)

    errors, warnings = base_lint(
        tasks, chunks, profile=args.tier == "profile", required_fields=FINANCE_REQUIRED_FIELDS
    )
    for task in tasks:
        if not task.get("should_abstain"):
            errors.extend(_lint_evidence(task, chunks, store))
        calc_errors, calc_warnings = _lint_calc(task)
        errors.extend(calc_errors)
        warnings.extend(calc_warnings)
        schema_errors, schema_warnings = _lint_schema_v2(task)
        errors.extend(schema_errors)
        warnings.extend(schema_warnings)
    tier_errors, tier_warnings = _lint_tier(tasks, args.tier, holdout_only=args.holdout_only)
    errors.extend(tier_errors)
    warnings.extend(tier_warnings)
    if args.tier == "complex":
        complex_errors, complex_warnings = _lint_complex(tasks)
        errors.extend(complex_errors)
        warnings.extend(complex_warnings)
    if args.check_refs:
        for task in tasks:
            errors.extend(_lint_refs(task, chunks))
    if args.check_routing:
        documents = store.list_documents()
        for task in tasks:
            errors.extend(_lint_routing(task, documents))

    dimension_counter: dict[str, int] = {}
    for task in tasks:
        name = str(task.get("dimension") or "")
        dimension_counter[name] = dimension_counter.get(name, 0) + 1
    holdout_count = sum(1 for task in tasks if task.get("holdout"))

    print(f"任务数: {len(tasks)}    语料 chunk: {len(chunks)}    留出: {holdout_count}    tier: {args.tier}")
    print("维度分布：" + "  ".join(f"{name}={dimension_counter.get(name, 0)}" for name in DIMENSIONS))
    print()
    if warnings:
        print(f"警告 {len(warnings)} 条：")
        for warning in warnings:
            print(f"  ! {warning}")
        print()
    if errors:
        print(f"错误 {len(errors)} 条：")
        for error in errors:
            print(f"  x {error}")
        return 1
    print("lint 通过：0 error")
    return 0


if __name__ == "__main__":
    sys.exit(main())
