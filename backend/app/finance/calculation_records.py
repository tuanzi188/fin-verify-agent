"""计算结果的展示尺度、结论绑定和输入坐标溯源。"""

from __future__ import annotations

import ast
import logging
import re
from decimal import Decimal, InvalidOperation

from app.finance.calc import CalculationError, calculate
from app.finance.references import calculation_markers, strip_references
from app.models.finance import CalcRecord, FinanceCitation

logger = logging.getLogger(__name__)

_FINANCIAL_NUMBER = re.compile(
    # 收尾括号要一起匹配：`(5,728,446) 千元` 的单位在括号外面，
    # 不把括号吃掉，单位就丢了，校验会因为"结论有单位、单元格没单位"判不一致。
    r"(?<![\d.])(?P<value>[-−]?\d[\d,，]*(?:\.\d+)?)\s*(?P<close>[)）])?\s*"
    r"(?P<unit>亿元|百万元|万元|千元|元|个百分点|pp|%|％|倍)?"
)
_DERIVATION_WORDS = (
    "计算",
    "增长",
    "下降",
    "增加",
    "减少",
    "差额",
    "相差",
    "占比",
    "比例",
    "百分点",
    "合计",
    "比重",
    "同比",
    "环比",
)


def record_number_tokens(text: str) -> list[tuple[Decimal, str]]:
    """按完整数字取值，避免把 112 匹配到结果 12。

    财报用括号表示负数（`(5,728,446)`、`（12,345.00）`），这是文档级
    `negative_format=parentheses` 的口径；不翻符号会让「-5728446」与单元格
    对不上，校验会误判 numeric_mismatch。

    但括号也用来写序号：`（1）`、`（2）`。所以只有带千分位或小数点时才当负数，
    纯整数括号保持原值——否则编号会被读成 -1。
    """
    text = str(text or "")
    values: list[tuple[Decimal, str]] = []
    for match in _FINANCIAL_NUMBER.finditer(text):
        token = match.group("value")
        raw = token.replace(",", "").replace("，", "").replace("−", "-")
        try:
            value = Decimal(raw)
        except InvalidOperation:
            continue
        start = match.start()
        # 括号和数字之间常带空格：`(1,474,894 )`、`（ 12,345 ）`
        before = text[:start].rstrip()[-1:]
        parenthesized = before in ("(", "（") and bool(match.group("close"))
        if parenthesized and any(symbol in token for symbol in (",", "，", ".")):
            value = -abs(value)
        if value.is_finite():
            values.append((value, (match.group("unit") or "").replace("％", "%")))
    return values


def calculation_display_unit(formula: str, inputs: dict) -> tuple[str, Decimal]:
    """百分数相减按百分点展示；已经乘百的公式不再重复放大。"""
    try:
        expression = ast.parse(formula, mode="eval").body
    except (SyntaxError, ValueError):
        return "", Decimal(1)
    used = {node.id for node in ast.walk(expression) if isinstance(node, ast.Name)}
    percent_inputs = bool(used) and all(
        str(inputs.get(name, "")).strip().endswith(("%", "％")) for name in used
    )
    operators = [
        node.op for node in ast.walk(expression) if isinstance(node, ast.BinOp)
    ]
    additive = any(isinstance(operator, (ast.Add, ast.Sub)) for operator in operators)
    divided = any(isinstance(operator, ast.Div) for operator in operators)
    multipliers = [
        node
        for node in ast.walk(expression)
        if isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.Mult)
        and any(
            isinstance(side, ast.Constant) and side.value == 100
            for side in (node.left, node.right)
        )
    ]
    # 只对纯加减的百分数自动推导百分点；复杂式保留无单位结果，交给调用方明确口径。
    if (
        percent_inputs
        and additive
        and not divided
        and all(
            isinstance(operator, (ast.Add, ast.Sub, ast.Mult)) for operator in operators
        )
    ):
        if all(isinstance(operator, (ast.Add, ast.Sub)) for operator in operators):
            return "pp", Decimal(100)
        if isinstance(expression, ast.BinOp) and isinstance(expression.op, ast.Mult):
            sides = (expression.left, expression.right)
            if any(
                isinstance(side, ast.Constant) and side.value == 100 for side in sides
            ):
                remainder = (
                    expression.right
                    if isinstance(expression.left, ast.Constant)
                    else expression.left
                )
                if all(
                    not isinstance(node, ast.BinOp)
                    or isinstance(node.op, (ast.Add, ast.Sub))
                    for node in ast.walk(remainder)
                ):
                    return "pp", Decimal(1)
    if multipliers and divided:
        return "%", Decimal(1)
    return "", Decimal(1)


def citation_numeric_cells(citation: FinanceCitation) -> list[dict]:
    """仅枚举引用明确指向的单元格，不把周边预览当目标证据。"""
    evidence = citation.evidence
    cells: list[dict] = []
    preview = evidence.table_preview
    if preview:
        for row in preview.rows:
            if evidence.row_indices and row.row not in evidence.row_indices:
                continue
            for cell in row.cells:
                if cell.column_index == 0:
                    continue
                if (
                    evidence.column_indices
                    and cell.column_index not in evidence.column_indices
                ):
                    continue
                for value, unit in record_number_tokens(cell.value or ""):
                    column_label = (
                        (preview.display_columns or preview.columns)[cell.column_index]
                        if cell.column_index
                        < len(preview.display_columns or preview.columns)
                        else cell.column
                    )
                    contextual_unit = (
                        "%"
                        if any(
                            symbol in str(column_label) + row.row_name
                            for symbol in ("%", "％")
                        )
                        else evidence.unit
                    )
                    cells.append(
                        {
                            "value": value,
                            "unit": unit or contextual_unit,
                            "evidence_id": evidence.evidence_id,
                            "table_id": evidence.table_id,
                            "row": row.row,
                            "column": cell.column_index,
                            "cell_text": cell.value,
                            "marker": citation.marker,
                        }
                    )
    elif evidence.cell_text is not None:
        for value, unit in record_number_tokens(evidence.cell_text):
            cells.append(
                {
                    "value": value,
                    "unit": unit or evidence.unit,
                    "evidence_id": evidence.evidence_id,
                    "table_id": evidence.table_id,
                    "row": evidence.row_indices[0] if evidence.row_indices else None,
                    "column": evidence.column_indices[0]
                    if evidence.column_indices
                    else None,
                    "cell_text": evidence.cell_text,
                    "marker": citation.marker,
                }
            )
    return cells


def build_calculation_records(
    raw_records: list[dict], citations: list[FinanceCitation]
) -> list[CalcRecord]:
    pool = [cell for citation in citations for cell in citation_numeric_cells(citation)]
    records: list[CalcRecord] = []
    for index, raw in enumerate(raw_records or []):
        if not isinstance(raw, dict):
            continue
        formula = str(raw.get("formula") or "")
        inputs = raw.get("inputs") if isinstance(raw.get("inputs"), dict) else {}
        unit, scale = calculation_display_unit(formula, inputs)
        result = raw.get("result")
        recomputed = False
        try:
            result = calculate(formula, inputs)["result"]
            recomputed = True
        except (CalculationError, InvalidOperation) as exc:
            # 复算失败时保留模型给的 result，只记诊断信息，不改变降级语义。
            logger.debug("calc recompute failed: formula=%r err=%s", formula, exc)
        if result is not None and scale != 1:
            try:
                result = format(Decimal(str(result)) * scale, "f")
            except InvalidOperation:
                unit, scale = "", Decimal(1)
        sources: dict[str, dict] = {}
        for name, raw_value in inputs.items():
            parsed = record_number_tokens(str(raw_value))
            if len(parsed) != 1:
                continue
            value, input_unit = parsed[0]
            candidates: dict[tuple, dict] = {}
            for cell in pool:
                if value != cell["value"] or (
                    input_unit and input_unit != cell["unit"]
                ):
                    continue
                key = (cell["table_id"], cell["row"], cell["column"])
                candidates[key] = {
                    key_name: item
                    for key_name, item in cell.items()
                    if key_name != "value"
                }
            if len(candidates) == 1:
                sources[name] = next(iter(candidates.values()))
        grounded = bool(inputs) and len(sources) == len(inputs)
        if not unit and grounded:
            source_units = {source.get("unit") for source in sources.values()}
            try:
                arithmetic = ast.parse(formula, mode="eval")
                if len(source_units) == 1 and all(
                    not isinstance(node, ast.BinOp)
                    or isinstance(node.op, (ast.Add, ast.Sub))
                    for node in ast.walk(arithmetic)
                ):
                    unit = str(next(iter(source_units)) or "")
            except (SyntaxError, ValueError) as exc:
                # 单位推断是 best-effort，失败时留空；保留日志便于排查异常公式。
                logger.debug("unit inference failed: formula=%r err=%s", formula, exc)
        records.append(
            CalcRecord(
                formula=formula,
                inputs=inputs,
                result=result,
                unit=unit,
                calculation_id=f"calc-{index + 1}",
                result_scale=str(scale),
                input_sources=sources,
                grounding_status="complete"
                if grounded
                else ("partial" if sources else "unverified"),
                recomputed=recomputed,
                note=(
                    "已按公式复算；输入来源已定位。"
                    if grounded and recomputed
                    else "输入来源或公式尚未完整验证，需人工复核。"
                )
                + (
                    " 展示值为原始计算结果乘以 100，单位为百分点。"
                    if scale != 1
                    else ""
                ),
            )
        )
    return records


def claim_calculation_index(text: str, records: list[CalcRecord]) -> int | None:
    """显式编号优先；旧格式仅对唯一、完整、同单位的派生值兼容绑定。"""
    explicit = calculation_markers(text)
    if explicit:
        return (
            explicit[0]
            if len(explicit) == 1 and 0 <= explicit[0] < len(records)
            else None
        )
    if not any(word in text for word in _DERIVATION_WORDS):
        return None
    tokens = record_number_tokens(strip_references(text))
    candidates = []
    for index, record in enumerate(records):
        result_tokens = record_number_tokens(str(record.result))
        if len(result_tokens) != 1:
            continue
        result_value = result_tokens[0][0]
        expected_unit = "个百分点" if record.unit == "pp" else record.unit
        if any(
            value == result_value and (unit == expected_unit or unit == record.unit)
            for value, unit in tokens
        ):
            candidates.append(index)
    return candidates[0] if len(candidates) == 1 else None
