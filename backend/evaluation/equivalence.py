"""答案等价规则：把"65.7084 / 65.71 / 约 65.7 算不算同一个答案"写死。

评测集审计的已知缺口之一是**没有答案等价规则**：judge prompt 里没有容差口径，
同一个数写成 65.7084、65.71、约 65.7 可能被判成三个不同答案。这个模块提供：

1. **四条原子判据**（全有或全无，与方案 §3.2 对齐）：
   - 数值一致（含单位与口径）；
   - 来源一致（指向同一张表/同一份披露文件）；
   - 结论方向一致（增/减/持平）；
   - 拒答合理性（证据不足时拒答属正确行为）。
2. **确定性数值等价**：千分位、百分号、括号负数、元/千元/万元/亿元换算、
   合理舍入（默认相对容差 0.05%）。
3. **给 judge 的 rubric 文本 + few-shot**：`RUBRIC_TEXT` 会被拼进 judge prompt，
   保证"人、代码、judge"三处用同一套口径。

这不是要替代 judge：方向、来源支撑、拒答合理性仍需语义判断；这里做的是
**先把能确定性判的判掉**，让 judge 只处理它真正擅长的部分。
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Iterable, Optional

#: 数值等价的默认相对容差。65.7084 → 65.71 的相对差约 2.4e-5，稳过；
#: 9.08 vs 9.09 的相对差约 1.1e-3，会被判成不同值（这是任务卡要抓的真错误）。
DEFAULT_REL_TOLERANCE = Decimal("0.0005")
#: 金额换算允许的绝对容差（元）。只用于跨单位比较，不用于同单位判等。
MONEY_ABS_TOLERANCE = Decimal("1")

_UNIT_SCALES = {
    "元": Decimal(1),
    "千元": Decimal(1000),
    "万元": Decimal(10000),
    "亿元": Decimal(100000000),
    "%": Decimal(1),
    "％": Decimal(1),
    "个百分点": Decimal(1),
    "倍": Decimal(1),
}

_NUMBER_PATTERN = re.compile(
    r"(?<![\w.])"
    r"(?P<sign>[-+−])?\s*"
    r"(?P<number>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"\s*"
    r"(?P<unit>亿元|万元|千元|元|个百分点|%|％|倍|年)?"
)

_UP_WORDS = ("增长", "增加", "上升", "提高", "增幅", "同比增", "正增长", "高于", "高出", "高")
_DOWN_WORDS = ("下降", "减少", "降低", "下滑", "负增长", "减幅", "同比减", "低于", "低出", "低")
_FLAT_WORDS = ("持平", "不变", "基本一致", "没有变化")


# ---------------------------------------------------------------------------
# 数值解析
# ---------------------------------------------------------------------------


def _decimal(text: str) -> Optional[Decimal]:
    cleaned = str(text or "").replace(",", "").replace("，", "").replace(" ", "")
    cleaned = cleaned.replace("−", "-").replace("—", "-")
    try:
        return Decimal(cleaned)
    except (InvalidOperation, TypeError, ValueError):
        return None


def numbers_in(text: str) -> list[dict]:
    """抽出文本里的数值 token（保留原文、单位、符号）。"""
    results: list[dict] = []
    text = str(text or "")
    for match in _NUMBER_PATTERN.finditer(text):
        value = _decimal(match.group("number"))
        if value is None:
            continue
        sign = match.group("sign")
        if sign in ("-", "−"):
            value = -value
        # 会计写法 `(1,234)` 是负数：正则只认数字本身，括号要看前后字符。
        start, end = match.span()
        if start > 0 and end < len(text) and text[start - 1] == "(" and text[end] == ")":
            value = -abs(value)
        unit = match.group("unit") or ""
        results.append(
            {
                "raw": match.group(0).strip(),
                "value": value,
                "unit": unit,
                "position": match.start(),
            }
        )
    return results


def unit_scale(unit: str) -> Decimal:
    return _UNIT_SCALES.get(str(unit or ""), Decimal(1))


def to_base(value: Decimal, unit: str) -> Decimal:
    """金额换算到元；百分比/倍数原样。"""
    return value * unit_scale(unit)


def same_number(
    left: Decimal,
    right: Decimal,
    *,
    rel_tolerance: Decimal = DEFAULT_REL_TOLERANCE,
    abs_tolerance: Decimal = Decimal(0),
) -> bool:
    """两个数是否等价：先看绝对容差，再看相对容差。

    允许传 float/int：内部统一转 Decimal(str(...))，避免 Decimal×float 的
    TypeError，也避免二进制浮点把 65.71 变成 65.709999…。
    """
    left = left if isinstance(left, Decimal) else Decimal(str(left))
    right = right if isinstance(right, Decimal) else Decimal(str(right))
    if left == right:
        return True
    difference = abs(left - right)
    if difference <= abs_tolerance:
        return True
    scale = max(abs(left), abs(right), Decimal(1))
    return difference <= rel_tolerance * scale


def value_in_text(
    expected: str,
    text: str,
    *,
    rel_tolerance: Decimal = DEFAULT_REL_TOLERANCE,
) -> dict:
    """期望值是否出现在文本里（含跨单位换算）。

    跨单位比较只对金额生效：`456,451,731 千元` 与 `4,564.52 亿元` 是同一个数。
    百分比不做 ×100 换算——那会把 0.29 和 29% 混成一个。
    """
    expected_numbers = numbers_in(expected)
    if not expected_numbers:
        return {"match": False, "expected": expected, "matched_raw": None, "reason": "期望值不是数字"}
    candidates = numbers_in(text)
    for want in expected_numbers:
        want_unit = want["unit"]
        for candidate in candidates:
            candidate_unit = candidate["unit"]
            # 同单位（或都没单位）：直接比
            if want_unit == candidate_unit or not want_unit or not candidate_unit:
                if same_number(want["value"], candidate["value"], rel_tolerance=rel_tolerance):
                    return {
                        "match": True,
                        "expected": expected,
                        "matched_raw": candidate["raw"],
                        "reason": "同单位数值一致",
                    }
            # 金额跨单位：都换到元再比
            money_units = {"元", "千元", "万元", "亿元"}
            if want_unit in money_units and candidate_unit in money_units:
                left = to_base(want["value"], want_unit)
                right = to_base(candidate["value"], candidate_unit)
                if same_number(
                    left,
                    right,
                    rel_tolerance=rel_tolerance,
                    abs_tolerance=MONEY_ABS_TOLERANCE,
                ):
                    return {
                        "match": True,
                        "expected": expected,
                        "matched_raw": candidate["raw"],
                        "reason": f"金额换算一致（{want_unit} → {candidate_unit}）",
                    }
    return {
        "match": False,
        "expected": expected,
        "matched_raw": None,
        "reason": "文本里没有等价数值",
    }


def direction_of(text: str) -> Optional[str]:
    value = str(text or "")
    if any(word in value for word in _FLAT_WORDS):
        return "flat"
    if any(word in value for word in _DOWN_WORDS):
        return "down"
    if any(word in value for word in _UP_WORDS):
        return "up"
    return None


def direction_consistent(expected_text: str, answer: str) -> Optional[bool]:
    """方向一致：期望文本里没写方向时返回 None（不适用，不是失败）。"""
    expected = direction_of(expected_text)
    if expected is None:
        return None
    actual = direction_of(answer)
    if actual is None:
        return None
    return expected == actual


# ---------------------------------------------------------------------------
# 四条原子判据
# ---------------------------------------------------------------------------


def point_equivalence(
    point: dict,
    answer: str,
    *,
    cited_base_ids: Optional[Iterable[str]] = None,
    rel_tolerance: Decimal = DEFAULT_REL_TOLERANCE,
) -> dict:
    """对单个答案要点跑四条判据（拒答合理性在 task 级判，不在这里）。"""
    point = point or {}
    evidence = point.get("evidence") if isinstance(point.get("evidence"), dict) else {}
    expected_value = evidence.get("value")
    numeric = None
    numeric_detail = None
    if expected_value is not None:
        # 金标值常常只写数字，单位写在 point.unit / evidence.unit 里；
        # 不把单位拼回去，`456,451,731` 与 `4,564.52 亿元` 就无法做跨单位等价。
        unit = str(point.get("unit") or evidence.get("unit") or "")
        expected_text = f"{expected_value} {unit}".strip()
        result = value_in_text(expected_text, answer, rel_tolerance=rel_tolerance)
        numeric = bool(result["match"])
        numeric_detail = result

    direction = direction_consistent(str(point.get("text") or ""), answer)

    source = None
    expected_chunks = {str(item) for item in point.get("chunk_ids") or [] if item}
    if expected_chunks and cited_base_ids is not None:
        cited = {str(item) for item in cited_base_ids}
        source = bool(expected_chunks & cited)

    criteria = {"numeric": numeric, "direction": direction, "source": source}
    applicable = [value for value in criteria.values() if value is not None]
    return {
        "criteria": criteria,
        "applicable": len(applicable),
        "passed": sum(1 for value in applicable if value),
        "equivalent": bool(applicable) and all(applicable),
        "numeric_detail": numeric_detail,
    }


_LABEL_NOISE = re.compile(r"[（(][^）)]*[）)]|\s+")
_ROW_QUALIFIERS = ("金额", "合计", "净额", "总额", "本期", "上年")
#: 利润表把费用/成本列成负数，研发投入表列成正数；只在这类行上允许符号翻转。
_EXPENSE_ROW_HINTS = ("费用", "成本", "支出", "损失", "流出", "税金")


def _normalize_label(text: object) -> str:
    value = _LABEL_NOISE.sub("", str(text or ""))
    return value.strip(" ：:，,。.、-—_")


def _row_label_match(expected: str, actual: str) -> bool:
    """行名匹配：先精确；只有“金额/合计/净额…”这类量词差异才放宽。

    不能写成 substring：`净利润` 是 `归属于上市公司股东的净利润` 的子串，
    但两者不是同一口径；`研发费用金额` 与 `研发费用` 才是同一行。
    """
    if expected == actual:
        return True
    return any(
        expected == actual + suffix or actual == expected + suffix
        for suffix in _ROW_QUALIFIERS
    )


def _column_label_match(expected: str, actual: str) -> bool:
    if expected == actual:
        return True
    year = re.compile(r"20\d{2}年?")
    return bool(
        year.fullmatch(expected)
        and year.fullmatch(actual)
        and expected[:4] == actual[:4]
    )


def cell_equivalence(
    expected_row: object,
    expected_column: object,
    expected_value: object,
    table: dict,
    *,
    rel_tolerance: Decimal = DEFAULT_REL_TOLERANCE,
) -> dict:
    """判断一张表里是否存在与要点等价的单元格。

    用于“同一会计科目出现在另一张表”的场景（如研发费用同时出现在利润表和
    研发投入表）。只放宽**表**，不放宽行/列/值：行名必须同一口径、列必须是
    同一期间、数值必须落在容差内。合并/母公司口径由调用方另判。
    """
    target_row = _normalize_label(expected_row)
    target_column = _normalize_label(expected_column)
    if not target_row or not target_column or expected_value is None:
        return {"match": False, "reason": "缺少行/列/值"}

    rows = table.get("rows") or []
    columns = table.get("columns") or []
    row_index = None
    for index, values in enumerate(rows):
        label = _normalize_label(values[0] if values else "")
        if label and _row_label_match(target_row, label):
            row_index = index
            break
    if row_index is None:
        return {"match": False, "reason": f"找不到同一口径的行: {expected_row}"}

    column_index = None
    for index, name in enumerate(columns):
        label = _normalize_label(name)
        if label and _column_label_match(target_column, label):
            column_index = index
            break
    if column_index is None:
        return {"match": False, "reason": f"找不到同一期间的列: {expected_column}"}

    row = rows[row_index]
    cell_text = str(row[column_index]) if column_index < len(row) else ""
    compare_text = cell_text
    row_label = _normalize_label(row[0] if row else "")
    if any(hint in row_label for hint in _EXPENSE_ROW_HINTS):
        # 费用/成本在利润表常写成 (17,787,624)，在投入表写成 17,787,624。
        stripped = cell_text.strip()
        if (stripped.startswith("(") and stripped.endswith(")")) or (
            stripped.startswith("（") and stripped.endswith("）")
        ):
            compare_text = stripped[1:-1]
        elif stripped.startswith(("-", "−")):
            compare_text = stripped[1:]
    numeric = value_in_text(str(expected_value), compare_text, rel_tolerance=rel_tolerance)
    return {
        "match": bool(numeric.get("match")),
        "row_index": row_index,
        "column_index": column_index,
        "row_label": row[0] if row else "",
        "column_label": columns[column_index],
        "cell_text": cell_text,
        "numeric": numeric,
    }


def task_equivalence(
    task: dict,
    result: dict,
    *,
    rel_tolerance: Decimal = DEFAULT_REL_TOLERANCE,
) -> dict:
    """对一条运行跑答案等价（只覆盖可确定性判的部分）。"""
    answer = str(result.get("answer") or "")
    cited_base_ids = {
        str(citation.get("chunk_id") or "") for citation in result.get("citations") or []
    }
    rows = [
        point_equivalence(
            point,
            answer,
            cited_base_ids=cited_base_ids,
            rel_tolerance=rel_tolerance,
        )
        for point in task.get("answer_points") or []
        if isinstance(point, dict)
    ]
    judged = [row for row in rows if row["applicable"] > 0]
    return {
        "points": rows,
        "judged_points": len(judged),
        "equivalent_points": sum(1 for row in judged if row["equivalent"]),
        "equivalent_rate": (
            sum(1 for row in judged if row["equivalent"]) / len(judged) if judged else None
        ),
        "abstain_expected": bool(task.get("should_abstain")),
        "abstain_reasonable": (
            str(result.get("status") or "") == "abstained"
            if task.get("should_abstain")
            else None
        ),
    }


# ---------------------------------------------------------------------------
# 给 judge 的 rubric
# ---------------------------------------------------------------------------

RUBRIC_TEXT = """【答案等价规则】（四条原子判据，全部满足才算"要点通过"）

1. **数值一致（含单位与口径）**：允许千分位、百分号、括号负数、元/千元/万元/亿元
   换算，以及合理舍入（相对差 ≤0.05%）。例：`456,451,731 千元` 与 `4,564.52 亿元`
   等价；`65.7084%` 与 `65.71%` 等价；`9.08 个百分点` 与 `9.09 个百分点` **不等价**。
2. **来源一致**：指向同一张表或同一份披露文件。同一数字出现在不同表里且口径不同
   （合并/母公司、本期/上期）不算来源一致。
3. **结论方向一致**：增长/下降/持平必须一致；把"下降 33.46%"说成"增长 33.46%"
   即使数字对也不通过。
4. **拒答合理性**：资料确实不足时，明确说明"现有资料无法确定"属正确行为，计通过；
   但资料里明明有答案却拒答，不计通过（误拒）。

反例（判 fail）：
- 问"智能家居毛利率比商业及工业高多少个百分点"，答"高 9.09%"——数字对但口径错
  （应为 9.09 个百分点，不是 9.09%）。
- 问"2025 年营业收入"，答"456,451,731 元"——数字对但单位错（表单位是千元）。
- 问"研发投入同比"，答"增长 17.13%"，但资料显示的是下降——方向错。

正例（判 pass）：
- 答"约 65.71%（456,451,731 千元中的 299,927,239 千元）"——舍入与单位换算都合理。
- 答"资料中未披露 2026 年数据，无法确定"——证据不足时的诚实拒答。
"""


#: judge 的 few-shot。与 `docs/ANSWER_EQUIVALENCE_RUBRIC.md` 的「Few-shot」表同源，
#: 但这里才是**唯一维护点**：文档只描述规则，judge 实际拿到的是 `rubric_text()`
#: 拼出来的 `RUBRIC_TEXT + FEWSHOT_TEXT`。容差仍只改 `DEFAULT_REL_TOLERANCE` 一处，
#: 不在这里写死任何数字（除例子里的期望值/回答值）。
FEWSHOT_TEXT = """【Few-shot：照着判，不要凭感觉】

判 pass（要点通过）：
- 期望 `456,451,731 千元`，答 `约 4,564.52 亿元` —— 金额跨单位换算一致，舍入在容差内。
- 期望 `65.7084%`，答 `约 65.71%` —— 相对差 2.4e-5，合理舍入。
- 期望 `9.09 个百分点`，答 `高 9.09 个百分点` —— 数值、单位、方向都一致。
- 期望 `资料未披露`，答 `根据现有资料无法确定` —— 证据不足时的诚实拒答。

判 fail（要点不通过）：
- 期望 `9.09 个百分点`，答 `高 9.09%` —— 数字对但口径错（百分点 ≠ 百分比）。
- 期望 `456,451,731 千元`，答 `456,451,731 元` —— 数字对但单位错（千元 ≠ 元）。
- 期望 `下降 33.46%`，答 `增长 33.46%` —— 方向错。
- 期望 `9.09 个百分点`，答 `高 9.08 个百分点` —— 超出舍入容差，是真错误。
- 期望 `营业收入 100 元`，答 `资料不足，无法确定` —— 资料里有答案却拒答（误拒）。
"""


def rubric_text() -> str:
    """judge prompt 里的等价判据：四条原子规则 + few-shot，单一来源。"""
    return RUBRIC_TEXT + "\n" + FEWSHOT_TEXT
