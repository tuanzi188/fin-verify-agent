"""正式响应的确定性复核：只核实数值、单位及绑定，不替代语义审计。"""

from __future__ import annotations

import re
from decimal import Decimal

from app.finance.calculation_records import citation_numeric_cells, record_number_tokens
from app.finance.references import (
    calculation_markers,
    reference_markers,
    strip_references,
)
from app.models.finance import FinanceChatResponse

_VALIDATION_MONEY = {
    "元": Decimal(1),
    "千元": Decimal(1000),
    "万元": Decimal(10000),
    "百万元": Decimal(1000000),
    "亿元": Decimal(100000000),
}


def finance_values_match(left: tuple[Decimal, str], right: tuple[Decimal, str]) -> bool:
    left_value, left_unit = left
    right_value, right_unit = right
    left_unit = "个百分点" if left_unit == "pp" else left_unit
    right_unit = "个百分点" if right_unit == "pp" else right_unit
    if left_unit and right_unit and left_unit != right_unit:
        if left_unit not in _VALIDATION_MONEY or right_unit not in _VALIDATION_MONEY:
            return False
        left_value *= _VALIDATION_MONEY[left_unit]
        right_value *= _VALIDATION_MONEY[right_unit]
    # 有单位的结论不能由缺单位的裸数字自动认证。
    if left_unit and not right_unit:
        return False
    return abs(left_value - right_value) <= max(
        abs(right_value), Decimal("0.000001")
    ) * Decimal("0.0005")


# 标识符不是数值。文档/表/节/块 id 和行列坐标里都含数字，剥离后不参与数值校验。
# 2026-11-05 实测：答案按规则写「表 cninfo-000333-2025-annual#t0006，行0」时，
# 校验器把 -333 / 2025 / 6 / 0 当成结论值，正常答案被误判 needs_review。
_IDENTIFIER_RE = re.compile(
    r"cninfo-[0-9a-z\-]+(?:#[a-z0-9#\-]+)*"  # 文档 id 及其 #t0006 / #rows#3-5 后缀
    r"|#rows#[\d,\-]+"  # 裸块 id 的行组后缀
    r"|#[a-z]{1,2}\d+(?:-\d+)?"  # 裸表 / 节 / 块后缀
    r"|(?:行|列)\s*\d+(?:\s*[-–~]\s*\d+)?"  # 行0、列 1、行0-2
    r"|(?<![A-Za-z])[pP]\.\s*\d+",  # p.9
    re.IGNORECASE,
)


def finance_claim_values(text: str) -> list[tuple[Decimal, str]]:
    cleaned = strip_references(text)
    cleaned = _IDENTIFIER_RE.sub("", cleaned)
    cleaned = re.sub(
        r"(?<!\d)(?:19|20)\d{2}\s*(?:年|Q[1-4])", "", cleaned, flags=re.IGNORECASE
    )
    cleaned = re.sub(
        r"第\s*\d+\s*页|PDF\s*P\.?\s*\d+", "", cleaned, flags=re.IGNORECASE
    )
    return record_number_tokens(cleaned)


def validate_finance_response(response: FinanceChatResponse) -> None:
    """无法校验与明确冲突均要求复核；保留原回答以供人工查证。"""
    if response.status not in ("answered", "needs_review"):
        return
    citations = {citation.marker: citation for citation in response.citations}
    issues: list[dict] = []
    checked = 0
    for calculation_index in calculation_markers(response.answer):
        if not 0 <= calculation_index < len(response.calc):
            issues.append(
                {"code": "citation_missing", "reason": "计算编号无法回查", "calculation_index": calculation_index}
            )
    for marker in reference_markers(response.answer):
        if marker not in citations:
            issues.append({"code": "citation_missing", "reason": "引用编号无法回查", "marker": marker})
    for index, claim in enumerate(response.claims):
        values = finance_claim_values(claim.text)
        pool: list[tuple[Decimal, str]] = []
        for marker in claim.citation_markers:
            citation = citations.get(marker)
            if citation is None:
                continue
            evidence = citation.evidence
            if evidence.table_id:
                pool.extend(
                    (cell["value"], cell["unit"])
                    for cell in citation_numeric_cells(citation)
                )
            else:
                pool.extend(record_number_tokens(evidence.content))
        calc = None
        if claim.calc_index is not None and 0 <= claim.calc_index < len(response.calc):
            calc = response.calc[claim.calc_index]
            if not calc.recomputed or calc.grounding_status != "complete":
                issues.append(
                    {"code": "input_source_missing", "claim_index": index, "reason": "计算公式或输入来源尚未完整验证"}
                )
            pool.extend(
                (value, calc.unit or unit)
                for value, unit in record_number_tokens(str(calc.result))
            )
        if not claim.citation_markers and calc is None:
            issues.append(
                {"code": "citation_missing", "claim_index": index, "reason": "要点缺少明确引用或计算记录"}
            )
        for value, unit in values:
            checked += 1
            candidates = [(value, unit)]
            # “下降 18.97%”与表中“-18.97%”语义一致，金额不采用此规则。
            if (
                unit == "%"
                and value > 0
                and re.search(
                    r"(?:下降|减少|降低)\s*" + re.escape(format(value, "f")), claim.text
                )
            ):
                candidates.append((-value, unit))
            if not any(
                finance_values_match(wanted, source)
                for wanted in candidates
                for source in pool
            ):
                issues.append(
                    {
                        "claim_index": index,
                        "code": "numeric_mismatch",
                        "reason": "引用或计算记录中缺少同单位等价数值",
                        "value": str(value),
                        "unit": unit,
                    }
                )
        if any(issue.get("claim_index") == index for issue in issues):
            warning = "本要点的数值、引用或计算来源需要人工复核。"
            claim.uncertainty = "；".join(
                part for part in (claim.uncertainty, warning) if part
            )
    if not response.claims:
        issues.append({"reason": "回答未形成可核验要点"})
    # 要点卡最多展示部分内容；完整回答中的表格或后续数字也不能绕过校验。
    for answer_line in response.answer.splitlines():
        plain_line = (
            strip_references(answer_line)
            .replace("**", "")
            .replace("__", "")
            .replace("`", "")
            .strip()
        )
        if finance_claim_values(plain_line) and not any(
            claim.text in plain_line for claim in response.claims
        ):
            issues.append(
                {
                    "reason": "完整回答包含未进入要点校验的数值文本",
                    "text": plain_line[:200],
                }
            )
    response.verification = {
        "scope": "数值与单位一致性、引用完整性、计算输入坐标；不代表语义结论已验证",
        "checked_numbers": checked,
        "issues": issues,
        "status": "needs_review" if issues else "checked",
    }
    if issues:
        response.status = "needs_review"
        response.notes.append(
            f"自动校验发现 {len(issues)} 项待复核内容；请核对要点引用及计算输入。"
        )
