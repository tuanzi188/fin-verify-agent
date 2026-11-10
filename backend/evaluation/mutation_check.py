"""判分器灵敏度检查（零模型）：把金标答案改错，判分器必须判失败。

为什么需要
----------
评测体系里有一个从未被验证的假设：**判分器能把"看起来对"和"真的对"分开**。
如果判分器只会判"数字出现没出现"，那么单位错、方向错、列错位都会漏过去，
所有正确率读数都会虚高。

这个脚本对现有任务卡的每一个金标要点做四类变异，要求判分器全部拒绝：

| 变异 | 做法 | 期望 |
|---|---|---|
| `digit_drift` | 数值 +0.2%（超出 0.05% 容差） | 判失败 |
| `unit_downgrade` | 千元写成元、亿元写成千元（数字不变） | 判失败 |
| `direction_flip` | 增长 ↔ 下降 | 判失败 |
| `tolerance_edge` | 9.08 个百分点 vs 9.09 个百分点 | 判失败 |

另外用合成表验证列错位与行名错位（`cell_equivalence`）。

零模型、只读任务卡与等价模块，可进 CI。退出码 1 表示有变异没被抓住——
这比少测几道题严重得多。

用法
----
    cd backend
    python -m evaluation.mutation_check --tasks evaluation/finance_tasks.jsonl
    python -m evaluation.mutation_check \
        --tasks evaluation/finance_tasks.jsonl \
        --tasks evaluation/finance_tasks_complex.jsonl \
        --out %TEMP%\\mutation_report.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from evaluation.equivalence import cell_equivalence, point_equivalence

#: 金额单位降级映射：数字不变，单位变小一级或变大一级，跨单位比较必然不等。
_UNIT_DOWNGRADE = {
    "亿元": "千元",
    "万元": "千元",
    "千元": "元",
    "元": "千元",
}

#: 方向词替换表。只换第一个命中的词，保持句子其余部分不变。
_DIRECTION_FLIP = (
    ("增长", "下降"),
    ("增加", "减少"),
    ("上升", "下降"),
    ("提高", "降低"),
    ("高于", "低于"),
    ("高出", "低出"),
    ("下降", "增长"),
    ("减少", "增加"),
    ("降低", "提高"),
    ("低于", "高于"),
    ("低出", "高出"),
)

_NUMBER_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


def load_tasks(paths: Iterable[str]) -> list[dict]:
    """读任务卡 JSONL，跳过注释行。"""
    tasks: list[dict] = []
    for raw in paths:
        path = Path(raw)
        for line in path.read_text(encoding="utf-8").splitlines():
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            tasks.append(json.loads(text))
    return tasks


def _decimal_places(number_text: str) -> int:
    return len(number_text.split(".", 1)[1]) if "." in number_text else 0


def _quantize(value: Decimal, places: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-places))


def _drift_number(raw: str) -> str | None:
    """把文本里的第一个数值放大 0.2%，保留原有小数位。"""
    match = _NUMBER_RE.search(str(raw or ""))
    if not match:
        return None
    number_text = match.group(0).replace(",", "")
    places = _decimal_places(number_text)
    try:
        value = Decimal(number_text)
    except Exception:  # noqa: BLE001 - 无法解析的数值不参与变异
        return None
    drifted = _quantize(value * Decimal("1.002"), places)
    if drifted == value:
        # 小数值乘 1.002 后可能被舍回原值（-1.21 × 1.002 ≈ -1.2124 → -1.21），
        # 退化时按最小刻度跳两格，保证变异一定超出 0.05% 容差。
        step = Decimal(1).scaleb(-places) * 2 if places else Decimal(2)
        drifted = value + step
    return format(drifted, "f")


def _gold_text(point: dict) -> str:
    """用要点自身的文本来构造"正确答案"。"""
    return str(point.get("text") or "")


def _expected_unit(point: dict) -> str:
    evidence = point.get("evidence") if isinstance(point.get("evidence"), dict) else {}
    return str(point.get("unit") or evidence.get("unit") or "").strip()


def _raw_value(point: dict) -> str:
    evidence = point.get("evidence") if isinstance(point.get("evidence"), dict) else {}
    return str(evidence.get("value") or "").strip()


def _unit_mutation(point: dict) -> str | None:
    unit = _expected_unit(point)
    target = _UNIT_DOWNGRADE.get(unit)
    value = _raw_value(point)
    if not target or not value:
        return None
    return f"{value} {target}"


def _direction_mutation(point: dict) -> str | None:
    text = _gold_text(point)
    for source, target in _DIRECTION_FLIP:
        if source in text:
            return text.replace(source, target, 1)
    return None


def _check_point(point: dict, answer: str) -> bool:
    cited = {str(item) for item in point.get("chunk_ids") or [] if item}
    result = point_equivalence(point, answer, cited_base_ids=cited)
    return bool(result.get("equivalent"))


def check_task(task: dict) -> list[dict]:
    """对一条任务的所有要点做变异，返回逐条结果。"""
    rows: list[dict] = []
    for index, point in enumerate(task.get("answer_points") or []):
        if not isinstance(point, dict):
            continue
        if not isinstance(point.get("evidence"), dict):
            continue  # 派生点没有单元格金标，不参与数字变异
        gold = _gold_text(point)
        if not gold:
            continue
        if not _check_point(point, gold):
            rows.append(
                {
                    "task_id": task.get("id"),
                    "point_index": index,
                    "kind": "gold_unstable",
                    "caught": False,
                    "note": "金标自身判不过，先修任务卡",
                }
            )
            continue

        digit = _drift_number(_raw_value(point))
        if digit:
            unit = _expected_unit(point)
            answer = f"{digit} {unit}".strip()
            rows.append(
                {
                    "task_id": task.get("id"),
                    "point_index": index,
                    "kind": "digit_drift",
                    "answer": answer,
                    "caught": not _check_point(point, answer),
                }
            )

        unit_answer = _unit_mutation(point)
        if unit_answer:
            rows.append(
                {
                    "task_id": task.get("id"),
                    "point_index": index,
                    "kind": "unit_downgrade",
                    "answer": unit_answer,
                    "caught": not _check_point(point, unit_answer),
                }
            )

        direction_answer = _direction_mutation(point)
        if direction_answer:
            rows.append(
                {
                    "task_id": task.get("id"),
                    "point_index": index,
                    "kind": "direction_flip",
                    "answer": direction_answer,
                    "caught": not _check_point(point, direction_answer),
                }
            )
    return rows


def tolerance_edge_cases() -> list[dict]:
    """锁住 0.05% 容差的边界：9.08 与 9.09 必须判不等价。"""
    point = {
        "text": "毛利率差 9.09 个百分点",
        "unit": "个百分点",
        "evidence": {"value": "9.09"},
        "chunk_ids": ["t-edge"],
    }
    cases = [
        ("tolerance_edge", "9.08 个百分点", False),
        ("tolerance_inside", "9.09 个百分点", True),
    ]
    rows = []
    for kind, answer, expect_pass in cases:
        actual_pass = _check_point(point, answer)
        rows.append(
            {
                "task_id": "SYNTHETIC-TOLERANCE",
                "point_index": 0,
                "kind": kind,
                "answer": answer,
                "caught": actual_pass == expect_pass,
                "expected_pass": expect_pass,
            }
        )
    return rows


def cell_shift_cases() -> list[dict]:
    """合成表：行名、列、期间任一错位都必须判不匹配。"""
    table = {
        "columns": ["项目", "2024 年", "2025 年"],
        "rows": [["营业收入", "100", "110"]],
    }
    cases = [
        ("cell_exact", "营业收入", "2025 年", "110", True),
        ("cell_column_shift", "营业收入", "2025 年", "100", False),
        ("cell_row_shift", "营业总收入", "2025 年", "110", False),
        ("cell_period_shift", "营业收入", "2024 年", "110", False),
    ]
    rows = []
    for kind, row, column, value, expect_match in cases:
        result = cell_equivalence(row, column, value, table)
        rows.append(
            {
                "kind": kind,
                "matched": bool(result.get("match")),
                "expected_match": expect_match,
                "caught": bool(result.get("match")) == expect_match,
            }
        )
    return rows


def build_report(tasks: list[dict]) -> dict[str, Any]:
    rows: list[dict] = []
    for task in tasks:
        rows.extend(check_task(task))
    rows.extend(tolerance_edge_cases())
    cells = cell_shift_cases()

    by_kind: dict[str, dict[str, int]] = {}
    for row in rows:
        bucket = by_kind.setdefault(row["kind"], {"total": 0, "caught": 0})
        bucket["total"] += 1
        bucket["caught"] += 1 if row["caught"] else 0

    missed = [row for row in rows if not row["caught"]]
    cell_missed = [row for row in cells if not row["caught"]]
    return {
        "tasks": len(tasks),
        "mutations": len(rows),
        "caught": len(rows) - len(missed),
        "missed": missed,
        "by_kind": by_kind,
        "cell_cases": cells,
        "cell_missed": cell_missed,
        "ok": not missed and not cell_missed,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="判分器变异灵敏度检查（零模型）")
    parser.add_argument(
        "--tasks",
        action="append",
        default=None,
        help="任务卡路径，可重复；默认主集 + 复杂集",
    )
    parser.add_argument("--out", default=None, help="报告 JSON 输出路径")
    args = parser.parse_args(argv)

    paths = args.tasks or [
        "evaluation/finance_tasks.jsonl",
        "evaluation/finance_tasks_complex.jsonl",
    ]
    report = build_report(load_tasks(paths))

    print(f"任务：{report['tasks']}　变异：{report['mutations']}　抓住：{report['caught']}")
    for kind, stat in sorted(report["by_kind"].items()):
        print(f"  {kind:<18} {stat['caught']}/{stat['total']}")
    print(f"单元格用例：{len(report['cell_cases'])}，未通过：{len(report['cell_missed'])}")
    if report["missed"]:
        print("未抓住的变异：")
        for row in report["missed"][:10]:
            print(f"  {row.get('task_id')} {row['kind']} -> {row.get('answer')}")

    if args.out:
        Path(args.out).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"报告：{args.out}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
