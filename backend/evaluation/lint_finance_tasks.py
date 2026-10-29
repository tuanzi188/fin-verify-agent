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
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from app.finance.calc import calculate
from app.finance.store import FinanceStore
from evaluation.finance_corpus import DOCUMENT_DIR, chunk_map
from evaluation.lint_agent_tasks import DIMENSIONS, lint as base_lint, load_tasks

DEFAULT_PROFILE = "evaluation/finance_tasks.jsonl"
DEFAULT_STRESS = "evaluation/finance_tasks_stress.jsonl"
HOLDOUT_RANGE = (5, 7)
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
    """派生数值的公式必须能跑出同一个值。"""
    errors: list[str] = []
    warnings: list[str] = []
    tag = task.get("id") or "?"
    derived = [
        index
        for index, point in enumerate(task.get("answer_points") or [])
        if isinstance(point, dict) and point.get("derived")
    ]
    spec = task.get("calc")
    if derived and not isinstance(spec, dict):
        errors.append(f"[{tag}] 有派生要点 {derived} 但没有 calc：比率必须给公式与输入")
        return errors, warnings
    if not isinstance(spec, dict):
        return errors, warnings

    formula = spec.get("formula")
    inputs = spec.get("inputs")
    expected = spec.get("expected")
    if not (isinstance(formula, str) and formula.strip() and isinstance(inputs, dict) and inputs):
        errors.append(f"[{tag}] calc 缺 formula 或 inputs")
        return errors, warnings
    try:
        outcome = calculate(formula, inputs)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"[{tag}] calc 公式跑不通: {type(exc).__name__}: {exc}")
        return errors, warnings

    actual = _as_decimal(outcome.get("result"))
    want = _as_decimal(expected)
    if actual is None or want is None:
        errors.append(f"[{tag}] calc 结果或 expected 不是数字: {outcome.get('result')!r} / {expected!r}")
        return errors, warnings
    tolerance = _as_decimal(spec.get("tolerance")) or Decimal("0.02")
    if abs(actual - want) > tolerance:
        errors.append(
            f"[{tag}] calc 值与 expected 不符：公式算出 {actual} vs expected {want}（容差 {tolerance}）"
        )
    if not spec.get("note"):
        warnings.append(f"[{tag}] calc 没写 note：报告里说不清这个数与年报披露值的关系")
    return errors, warnings


def _lint_tier(tasks: list[dict], tier: str) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    if tier == "complex":
        # 复杂集的路数由 `_lint_complex` 单独管：它不设留出，也不套主集的维度配比
        return errors, warnings
    if tier == "stress":
        for task in tasks:
            tag = task.get("id") or "?"
            if task.get("source") not in ("failure", "synthetic"):
                errors.append(f"[{tag}] 压力集 source 必须是 failure/synthetic，当前 {task.get('source')!r}")
            if not task.get("defect"):
                errors.append(f"[{tag}] 压力集必须写 defect：这条题对应哪个已知缺陷")
        return errors, warnings

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


def _lint_complex(tasks: list[dict]) -> tuple[list[str], list[str]]:
    """复杂集是「Agent 多花的钱值不值」的主判据，跑前必须固定且分桶可核。"""
    errors: list[str] = []
    warnings: list[str] = []
    if len(tasks) < COMPLEX_MIN_TASKS:
        errors.append(
            f"复杂集只有 {len(tasks)} 条，少于 {COMPLEX_MIN_TASKS}：15 个百分点的分辨率会退化成一题"
        )
    for task in tasks:
        tag = task.get("id") or "?"
        kind = str(task.get("complex_type") or "")
        if kind not in COMPLEX_TYPES:
            errors.append(f"[{tag}] complex_type 非法: {kind!r}，只能是 {COMPLEX_TYPES}")
        if task.get("holdout"):
            errors.append(f"[{tag}] 复杂集不设留出：这一集本身就是固定判据")
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
    return errors, warnings


def main() -> int:
    parser = argparse.ArgumentParser(description="金融任务集 lint")
    parser.add_argument("--tasks", default=DEFAULT_PROFILE)
    parser.add_argument("--tier", choices=("profile", "stress", "complex"), default="profile")
    parser.add_argument("--holdout-only", action="store_true", help="只看留出集")
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

    errors, warnings = base_lint(tasks, chunks, profile=args.tier == "profile")
    for task in tasks:
        if not task.get("should_abstain"):
            errors.extend(_lint_evidence(task, chunks, store))
        calc_errors, calc_warnings = _lint_calc(task)
        errors.extend(calc_errors)
        warnings.extend(calc_warnings)
    tier_errors, tier_warnings = _lint_tier(tasks, args.tier)
    errors.extend(tier_errors)
    warnings.extend(tier_warnings)
    if args.tier == "complex":
        complex_errors, complex_warnings = _lint_complex(tasks)
        errors.extend(complex_errors)
        warnings.extend(complex_warnings)

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
