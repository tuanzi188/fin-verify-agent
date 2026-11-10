"""独立核对回答数字的来源，不允许金标或预期计算结果替模型补证据。"""

from __future__ import annotations

import ast
import re

from app.finance.calc import CalculationError, calculate
from app.finance.calculation_records import (
    calculation_display_unit,
    record_number_tokens,
)
from app.services.finance_validation import finance_claim_values, finance_values_match
from evaluation.judge_context import JudgeContextError, judge_coordinate_content

NUMERIC_SUPPORT_VERSION = "numeric-support-v1"
_SUPPORT_READ_TOOLS = {
    "read_block",
    "read_section",
    "get_table",
    "get_rows",
    "get_cell",
    "get_citation",
}


def evaluate_numeric_support(result: dict, chunks: dict[str, dict]) -> dict:
    """仅查实际读取的原文及有输入来源的计算，缺材料时显式标记审计不完整。"""
    source_values = []
    unresolved: list[str] = []
    anchors: dict[str, dict] = {}
    for entry in result.get("evidence") or []:
        if (
            isinstance(entry, dict)
            and entry.get("citable") is not False
            and entry.get("chunk_id")
        ):
            anchors[str(entry["chunk_id"])] = entry
    for step in result.get("trace") or []:
        if step.get("ok") and step.get("tool") in _SUPPORT_READ_TOOLS:
            for anchor in step.get("result_ids") or []:
                anchors.setdefault(str(anchor), {})
    for anchor, snapshot in anchors.items():
        try:
            text = judge_coordinate_content(anchor, chunks, numeric_only=True)
        except JudgeContextError:
            text = str(snapshot.get("content") or "")
            if (
                not text
                or snapshot.get("truncated")
                or snapshot.get("snapshot_truncated")
            ):
                unresolved.append(anchor)
        unit_match = re.search(r"单位\s*[:：]\s*(亿元|百万元|万元|千元|元)", text)
        default_unit = str(
            snapshot.get("unit") or (unit_match[1] if unit_match else "")
        )
        source_values.extend(
            (value, unit or default_unit) for value, unit in finance_claim_values(text)
        )

    calculation_values = []
    for record in result.get("calc") or []:
        inputs = record.get("inputs") or {}
        if not isinstance(inputs, dict):
            continue
        input_units: set[str] = set()
        grounded = bool(inputs)
        for raw_value in inputs.values():
            parsed = record_number_tokens(str(raw_value))
            if len(parsed) != 1:
                grounded = False
                break
            matching_units = {
                unit
                for value, unit in source_values
                if finance_values_match(parsed[0], (value, unit))
            }
            if len(matching_units) != 1:
                grounded = False
                break
            input_units.update(matching_units)
        if not grounded:
            continue
        formula = str(record.get("formula") or "")
        try:
            computed = calculate(formula, inputs)
            unit, scale = calculation_display_unit(formula, inputs)
            if not unit and len(input_units) == 1:
                expression = ast.parse(formula, mode="eval")
                if all(
                    not isinstance(node, ast.BinOp)
                    or isinstance(node.op, (ast.Add, ast.Sub))
                    for node in ast.walk(expression)
                ):
                    unit = next(iter(input_units))
            calculation_values.extend(
                (value * scale, unit or parsed_unit)
                for value, parsed_unit in record_number_tokens(computed["result"])
            )
        except (CalculationError, SyntaxError, ValueError, TypeError):
            continue

    answer_text = re.sub(
        r"(?m)^\s*(?:\d+[.、)]|[-*])\s+", "", str(result.get("answer") or "")
    )
    answer_values = finance_claim_values(answer_text)
    unsupported = [
        {"value": str(value), "unit": unit}
        for value, unit in answer_values
        if not any(
            finance_values_match((value, unit), source)
            for source in source_values + calculation_values
        )
    ]
    total = len(answer_values)
    return {
        "numeric_audit_version": NUMERIC_SUPPORT_VERSION,
        "support_audit_complete": not unresolved,
        "unresolved_evidence": unresolved,
        "audited_number_count": total,
        "unsupported_number_count": len(unsupported),
        "unsupported_number_details": unsupported,
        "unsupported_number_rate": len(unsupported) / total if total else None,
        "evidence_numeric_support_rate": (total - len(unsupported)) / total
        if total
        else None,
        "support_scope": "数值与单位可溯源性，不认证指标归属、语义或财务口径；缺口不等于已证明幻觉",
    }
