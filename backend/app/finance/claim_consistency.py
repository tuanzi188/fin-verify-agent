"""结论—证据一致性校验（绑定级、确定性、零模型调用）。

产品能保证「坐标可回查」，但**不保证**「要点里的数字 == 它引用的那个单元格」：
`build_claims` 只按行切分并抓取角标，引用绑定是模型自报的。这个模块补那一环。

判定顺序（固定，先严后宽）：

1. **派生值**：claim 标了 `calc_index`，或数字等于某条 calc 的 `result`
   → 与计算记录比，标 `derived`。派生值本来就不在单元格里，不能判成不一致。
2. **原文直取**：在 claim 自己引用的证据里找
   - `table_cell`：目标单元格（`cell_text` + 证据单位）；
   - `table_rows`：该证据带出的行；出现「合计/总计/之和」时再按列求和试一次；
   - `paragraph/document`：证据正文做文本级匹配，标 `text`（宽口径，能力边界见下）。
3. **无法归因**：引用证据里也没有 → `unverified`，**不计入不一致**。
   误报主要来自这里，宁可漏报也不制造假阳性。

能力边界：段落证据只能做「这段文字里有没有这个数」，无法定位到句内；表格行能做
逐格匹配，但跨行合计依赖 `table_preview` 是否随证据带出。两者都如实标在 `kind` 里。

口径唯一来源是 `evaluation/equivalence.py`（容差、单位换算、千分位、括号负数），
这里不另造一套，避免「人、代码、judge」之外出现第四条口径。
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any, Iterable, Optional, Sequence

from evaluation.equivalence import (
    DEFAULT_REL_TOLERANCE,
    MONEY_ABS_TOLERANCE,
    numbers_in,
    same_number,
    to_base,
)

_MONEY_UNITS = {"元", "千元", "万元", "亿元"}

#: 引用标记：半角/全角，可带页码与表名。判定前整段剔掉，否则页码会被当成数值。
_CITATION_MARKERS = re.compile(r"[\[【]\s*\d{1,2}(?:[，,、\s][^\]】]*)?[\]】]")
_PAGE_REFERENCE = re.compile(r"第\s*\d+\s*页")

#: 区间（`3%-5%`、`3 至 5 个百分点`）：两端各自是合法数值，单点比对必然误判，
#: 整段跳过并计数。金额区间（`100-200 亿元`）同理，但要求两侧都带单位，
#: 否则 `下降-12.11%` 里的负号会被误当区间分隔符。
_RANGE_PATTERN = re.compile(
    r"\d+(?:\.\d+)?\s*(?:%|％|个百分点|亿元|万元|千元|元)"
    r"\s*(?:[-~—–－]|至|到)\s*"
    r"\d+(?:\.\d+)?\s*(?:%|％|个百分点|亿元|万元|千元|元)"
)

#: 合计语义：出现这些词才尝试按列求和，否则逐格匹配已经够了。
_SUM_WORDS = ("合计", "总计", "之和", "总额", "加总", "合共", "小计")
#: 求和时排除的行：合计行本身再加一遍会把结果翻倍。
_SUM_ROW_WORDS = ("合计", "总计", "小计")

#: 年份区间（`2020-2025 年`）：左端没有「年」字，靠 `_is_year` 的窗口兜不住，
#: 单独认出来，否则 2020 会被当成一个数据数值。
_YEAR_RANGE = re.compile(
    r"(?<!\d)(?:19|20)\d{2}\s*[-~—–－至到]\s*(?:19|20)\d{2}\s*年"
)

_CLAIM_CONSISTENT = "consistent"
_CLAIM_MISMATCHED = "mismatched"
_CLAIM_UNVERIFIED = "unverified"

_RESULT_OK = "ok"
_RESULT_MISMATCH = "mismatch"
_RESULT_UNVERIFIED = "unverified"
_RESULT_SKIPPED = "skipped"


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """同时吃 pydantic 模型与 dict，方便运行时和离线产物共用一套判定。"""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _kind_of(evidence: Any) -> str:
    kind = _get(evidence, "kind", "")
    return str(getattr(kind, "value", kind) or "")


def _is_year(text: str, number: dict) -> bool:
    """年份不是数值断言。除了 `2025 年`，也认 `2020-2025 年` 这种区间的左端。"""
    value = number["value"]
    if value != value.to_integral_value() or not (Decimal(1900) <= value <= Decimal(2100)):
        return False
    if "年" in str(number.get("raw") or ""):
        return True
    position = int(number.get("position") or 0)
    window = text[max(0, position - 3) : position + len(str(number.get("raw") or "")) + 3]
    return "年" in window


def _normalize_unicode(text: str) -> str:
    """把常见的非 ASCII 减号折成 `-`，否则 `−12.11%` 会被当成正数而误报。

    U+2013 在区间里也可能出现（`3%–5%`），归一后 `_RANGE_PATTERN` 仍能识别。
    """
    return str(text or "").replace("\u2212", "-").replace("\u2013", "-").replace("\uff0d", "-")


def _is_page_number(text: str, number: dict) -> bool:
    position = int(number.get("position") or 0)
    window = text[max(0, position - 3) : position + len(str(number.get("raw") or "")) + 3]
    return bool(_PAGE_REFERENCE.search(window))


def _range_spans(text: str) -> list[tuple[int, int]]:
    return [match.span() for match in _RANGE_PATTERN.finditer(text)]


def _in_span(position: int, spans: Sequence[tuple[int, int]]) -> bool:
    return any(start <= position < end for start, end in spans)


def claim_numbers(text: str) -> tuple[list[dict], list[dict]]:
    """把一条要点拆成「数据数值」与「跳过项」。

    跳过：区间、年份、页码。这三类不是数值断言，混进来就是假阳性来源。
    """
    cleaned = _CITATION_MARKERS.sub("", _normalize_unicode(str(text or "")))
    spans = _range_spans(cleaned)
    year_spans = [match.span() for match in _YEAR_RANGE.finditer(cleaned)]
    kept: list[dict] = []
    skipped: list[dict] = []

    for number in numbers_in(cleaned):
        if _in_span(int(number.get("position") or 0), spans):
            skipped.append({**number, "reason": "range"})
            continue
        if _in_span(int(number.get("position") or 0), year_spans) or _is_year(cleaned, number):
            skipped.append({**number, "reason": "year"})
            continue
        if _is_page_number(cleaned, number):
            skipped.append({**number, "reason": "page"})
            continue
        kept.append(number)
    return kept, skipped


def _equivalent(
    left_value: Decimal,
    left_unit: str,
    right_value: Decimal,
    right_unit: str,
    *,
    inherited: bool = False,
) -> bool:
    """数值等价。

    `inherited=True` 表示右侧的单位来自表格级标注、不是单元格自带的（`17.13`
    这类裸数字常出现在「单位：亿元」的表里，实际是百分比列）。此时先按原样
    数值比较，再谈单位——否则百分比列会被表单位判成不一致，这是误报头号来源。

    单元格自带单位（`456,451,731 千元`）或文本自带单位时按严格口径：
    金额跨单位换算，非金额不同单位直接判不等（`0.299` 与 `29.9%` 不是一回事）。
    """
    left_unit = str(left_unit or "")
    right_unit = str(right_unit or "")
    if inherited and same_number(left_value, right_value, rel_tolerance=DEFAULT_REL_TOLERANCE):
        return True
    if left_unit and right_unit and left_unit != right_unit:
        if left_unit in _MONEY_UNITS and right_unit in _MONEY_UNITS:
            return same_number(
                to_base(left_value, left_unit),
                to_base(right_value, right_unit),
                rel_tolerance=DEFAULT_REL_TOLERANCE,
                abs_tolerance=MONEY_ABS_TOLERANCE,
            )
        # 百分比与金额、百分点与百分比之间不做换算（0.29 与 29% 不是一回事）。
        return False
    return same_number(left_value, right_value, rel_tolerance=DEFAULT_REL_TOLERANCE)


def _cell_values(preview: Any) -> tuple[list[str], list[list[str]]]:
    """从 `TablePreview` 里取 (columns, rows)。行按单元格自带列索引归位。"""
    columns = [str(item) for item in (_get(preview, "display_columns") or _get(preview, "columns") or [])]
    rows: list[list[str]] = []
    for row in _get(preview, "rows") or []:
        cells = _get(row, "cells") or []
        width = max((int(_get(cell, "column_index", 0)) for cell in cells), default=-1) + 1
        values = [""] * max(width, 1)
        for cell in cells:
            index = int(_get(cell, "column_index", 0))
            if 0 <= index < len(values):
                values[index] = str(_get(cell, "value") or "")
        rows.append(values)
    return columns, rows


def _citation_source(citation: Any) -> dict:
    """把一条引用压成「可用来比对的数值池」。"""
    evidence = _get(citation, "evidence")
    kind = _kind_of(evidence)
    unit = str(_get(evidence, "unit") or "")
    source = {
        "marker": _get(citation, "marker"),
        "kind": kind,
        "unit": unit,
        "cells": [],
        "rows": [],
        "columns": [],
        "text": "",
    }

    if kind in ("table_cell", "table_rows"):
        cell_text = str(_get(evidence, "cell_text") or "")
        if cell_text:
            source["cells"].append(cell_text)
        preview = _get(evidence, "table_preview")
        if preview is not None:
            columns, rows = _cell_values(preview)
            source["columns"] = columns
            source["rows"] = rows
            if not unit:
                source["unit"] = str(_get(preview, "unit") or "")
            for row in rows:
                for value in row:
                    if value:
                        source["cells"].append(value)
        # 表行证据的 content 是渲染后的多行文本，作为兜底文本池
        source["text"] = str(_get(evidence, "content") or "")
    else:
        source["text"] = str(_get(evidence, "content") or "")

    return source


def _match_in_cells(
    number: dict,
    source: dict,
    *,
    as_sum: bool = False,
) -> Optional[dict]:
    """在单元格池里找等价数值；`as_sum=True` 时按列求和后再比。"""
    unit = number.get("unit") or ""
    if as_sum:
        rows = [row for row in source.get("rows") or [] if _summable(row)]
        # 第 0 列通常是行名，`_first_number` 会返回 None 自动跳过，所以从 0 起扫不漏列。
        for index in range(0, max((len(row) for row in rows), default=0)):
            values: list[Decimal] = []
            for row in rows:
                parsed = _first_number(row[index] if index < len(row) else "")
                if parsed is None:
                    values = []
                    break
                values.append(parsed)
            if not values:
                continue
            total = sum(values, Decimal(0))
            if _equivalent(
                number["value"],
                unit,
                total,
                source.get("unit") or "",
                inherited=True,
            ):
                return {
                    "kind": "sum",
                    "matched": f"列 {index} 的 {len(values)} 行合计 {total}",
                    "unit": source.get("unit") or "",
                }
        return None

    for raw in source.get("cells") or []:
        parsed_numbers = numbers_in(_normalize_unicode(raw))
        if not parsed_numbers:
            continue
        candidate = parsed_numbers[0]
        own_unit = str(candidate.get("unit") or "")
        if _equivalent(
            number["value"],
            unit,
            candidate["value"],
            own_unit or (source.get("unit") or ""),
            inherited=not own_unit,
        ):
            return {"kind": "cell", "matched": raw, "unit": source.get("unit") or ""}
    return None


def _summable(row: Sequence[str]) -> bool:
    if not row:
        return False
    name = str(row[0] or "")
    return not any(word in name for word in _SUM_ROW_WORDS)


def _first_number(text: str) -> Optional[Decimal]:
    parsed = numbers_in(_normalize_unicode(text))
    if not parsed:
        return None
    return parsed[0]["value"]


def _match_in_text(number: dict, text: str) -> Optional[str]:
    """文本级匹配：只判「这段文字里有没有这个数」，不定位到句内。"""
    if not text:
        return None
    unit = number.get("unit") or ""
    for candidate in numbers_in(_normalize_unicode(text)):
        own_unit = str(candidate.get("unit") or "")
        if _equivalent(
            number["value"],
            unit,
            candidate["value"],
            own_unit,
            inherited=not own_unit,
        ):
            return str(candidate.get("raw") or "")
    return None


def _calc_pool(calcs: Sequence[Any]) -> list[dict]:
    pool = []
    for index, calc in enumerate(calcs or []):
        result = _get(calc, "result")
        parsed = numbers_in(str(result)) if result is not None else []
        if not parsed:
            continue
        pool.append(
            {
                "index": index,
                "value": parsed[0]["value"],
                "unit": str(_get(calc, "unit") or parsed[0].get("unit") or ""),
                "raw": str(result),
            }
        )
    return pool


def check_claims(
    claims: Sequence[Any],
    citations: Sequence[Any],
    calcs: Sequence[Any] = (),
) -> dict:
    """对一组要点跑绑定级一致性校验。

    :return: `{checked, mismatched, unverified, skipped, details, claims}`
    """
    sources = {_get(citation, "marker"): _citation_source(citation) for citation in citations or []}
    calc_pool = _calc_pool(calcs)
    details: list[dict] = []
    claim_rows: list[dict] = []

    for index, claim in enumerate(claims or []):
        text = str(_get(claim, "text") or "")
        markers = [int(marker) for marker in (_get(claim, "citation_markers") or [])]
        calc_index = _get(claim, "calc_index")
        numbers, skipped = claim_numbers(text)
        wants_sum = any(word in text for word in _SUM_WORDS)

        for item in skipped:
            details.append(
                {
                    "claim_index": index,
                    "raw": str(item.get("raw") or ""),
                    "unit": str(item.get("unit") or ""),
                    "kind": "skip",
                    "result": _RESULT_SKIPPED,
                    "source": "",
                    "reason": str(item.get("reason") or ""),
                }
            )

        claim_status = _CLAIM_CONSISTENT
        for number in numbers:
            row = {
                "claim_index": index,
                "claim": text,
                "raw": str(number.get("raw") or ""),
                "value": str(number.get("value")),
                "unit": str(number.get("unit") or ""),
                "kind": "",
                "result": _RESULT_UNVERIFIED,
                "source": "",
                "reason": "",
            }

            matched = _match_calc(number, calc_pool, calc_index)
            if matched:
                row.update(
                    kind="derived",
                    result=_RESULT_OK,
                    source=f"calc:{matched['index']}",
                    reason=f"与计算记录 {matched['raw']} 一致",
                )
            else:
                verdict = _citation_verdict(number, markers, sources, wants_sum=wants_sum)
                if verdict["status"] == "ok":
                    row.update(
                        kind=verdict["kind"],
                        result=_RESULT_OK,
                        source=f"citation:{verdict['marker']}",
                        reason=verdict["reason"],
                    )
                elif verdict["status"] == "mismatch":
                    row.update(
                        kind=verdict["kind"],
                        result=_RESULT_MISMATCH,
                        source=f"citation:{verdict['marker']}",
                        reason=verdict["reason"],
                    )
                elif not markers and not calc_index:
                    row.update(kind="none", result=_RESULT_UNVERIFIED, reason="要点没有引用也没有计算记录")
                else:
                    row.update(kind=verdict["kind"], result=_RESULT_UNVERIFIED, reason=verdict["reason"])

            if row["result"] == _RESULT_MISMATCH:
                claim_status = _CLAIM_MISMATCHED
            elif row["result"] == _RESULT_UNVERIFIED and claim_status == _CLAIM_CONSISTENT:
                claim_status = _CLAIM_UNVERIFIED
            details.append(row)

        claim_rows.append(
            {
                "index": index,
                "status": claim_status,
                "numbers": len(numbers),
                "skipped": len(skipped),
            }
        )

    judged = [row for row in details if row["result"] in (_RESULT_OK, _RESULT_MISMATCH, _RESULT_UNVERIFIED)]
    return {
        "checked": len(judged),
        "mismatched": sum(1 for row in judged if row["result"] == _RESULT_MISMATCH),
        "unverified": sum(1 for row in judged if row["result"] == _RESULT_UNVERIFIED),
        "skipped": sum(1 for row in details if row["result"] == _RESULT_SKIPPED),
        "details": details,
        "claims": claim_rows,
    }


def _match_calc(number: dict, calc_pool: Sequence[dict], calc_index: Optional[int]) -> Optional[dict]:
    """派生值优先：标了 calc_index 只认那一条，没标才在全部计算记录里找。"""
    if calc_index is not None:
        for calc in calc_pool:
            if calc["index"] == calc_index and _equivalent(
                number["value"], number.get("unit") or "", calc["value"], calc["unit"]
            ):
                return calc
        return None
    for calc in calc_pool:
        if _equivalent(number["value"], number.get("unit") or "", calc["value"], calc["unit"]):
            return calc
    return None


def _citation_verdict(
    number: dict,
    markers: Sequence[int],
    sources: dict,
    *,
    wants_sum: bool,
) -> dict:
    """在要点的全部引用里找这个数。

    三态：
    - `ok`：某条引用里命中；
    - `mismatch`：引用里**有可比的证据**（单元格 / 非空正文），但都对不上；
    - `unverified`：引用里没有可比内容（坐标缺正文、正文被截断、marker 找不到）。
      这一态不算不一致，避免把「证据没带出来」误报成「模型写错数」。
    """
    comparable = False
    kind = "none"
    first_marker = markers[0] if markers else ""

    for marker in markers:
        source = sources.get(marker)
        if source is None:
            continue
        if source["kind"] in ("table_cell", "table_rows"):
            if source["cells"]:
                comparable = True
                kind = "cell"
            hit = _match_in_cells(number, source)
            if hit:
                return {
                    "status": "ok",
                    "marker": marker,
                    "kind": hit["kind"],
                    "reason": f"命中引用 [{marker}] 的单元格 {hit['matched']}",
                }
            if wants_sum and source["kind"] == "table_rows":
                hit = _match_in_cells(number, source, as_sum=True)
                if hit:
                    return {
                        "status": "ok",
                        "marker": marker,
                        "kind": "sum",
                        "reason": f"命中引用 [{marker}] 的跨行合计（{hit['matched']}）",
                    }
            # 表格证据没带出结构化预览时，退回渲染文本（宽口径，单独标 text）。
            text = source["text"]
            if not source["cells"] and text.strip() and "…[已截断，完整原文见文档]" not in text:
                comparable = True
                kind = "text"
                matched = _match_in_text(number, text)
                if matched:
                    return {
                        "status": "ok",
                        "marker": marker,
                        "kind": "text",
                        "reason": f"命中引用 [{marker}] 的表格文本（{matched}）",
                    }
            continue

        text = source["text"]
        if text.strip() and "…[已截断，完整原文见文档]" not in text:
            comparable = True
            kind = "text"
            matched = _match_in_text(number, text)
            if matched:
                return {
                    "status": "ok",
                    "marker": marker,
                    "kind": "text",
                    "reason": f"命中引用 [{marker}] 的证据正文（{matched}）",
                }

    if comparable:
        where = "单元格" if kind != "text" else "证据正文"
        return {
            "status": "mismatch",
            "marker": first_marker,
            "kind": kind,
            "reason": f"引用的{where}里没有这个数值",
        }
    return {
        "status": "unverified",
        "marker": first_marker,
        "kind": kind,
        "reason": "引用证据没有可比内容（缺正文或已截断）",
    }


def should_review(report: dict) -> bool:
    """是否该把这一轮标成「需人工复核」。只认不一致，不认无法归因。"""
    return int(report.get("mismatched") or 0) > 0


def check_response(response: Any) -> dict:
    """便捷入口：直接吃 `FinanceChatResponse`（或同名字段的 dict）。"""
    return check_claims(
        _get(response, "claims") or [],
        _get(response, "citations") or [],
        _get(response, "calc") or [],
    )


def summarize(report: dict) -> str:
    """一行读数，给日志和测试报告用。"""
    return (
        f"checked={report.get('checked', 0)} "
        f"mismatched={report.get('mismatched', 0)} "
        f"unverified={report.get('unverified', 0)} "
        f"skipped={report.get('skipped', 0)}"
    )
