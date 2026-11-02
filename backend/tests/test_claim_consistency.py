"""结论—证据一致性校验测试：单位、百分比、负号、区间、合计、派生值、错配。"""

from app.finance.claim_consistency import (
    check_claims,
    check_response,
    claim_numbers,
    should_review,
    summarize,
)
from app.models.finance import (
    CalcRecord,
    Claim,
    EvidenceKind,
    EvidenceRef,
    FinanceCitation,
    TableCell,
    TablePreview,
    TableRow,
)

TABLE_ID = "cninfo-000333-2025-annual#t0006"


def table_preview(
    *,
    unit: str = "千元",
    columns: tuple[str, ...] = ("项目", "2025 年"),
    rows: tuple[tuple[str, ...], ...] = (("营业收入", "456451731"),),
    table_id: str = TABLE_ID,
) -> TablePreview:
    return TablePreview(
        table_id=table_id,
        caption="主要会计数据",
        page=42,
        unit=unit,
        columns=list(columns),
        display_columns=list(columns),
        rows=[
            TableRow(
                row=index,
                row_name=str(row[0]) if row else "",
                cells=[
                    TableCell(column=columns[position], column_index=position, value=value)
                    for position, value in enumerate(row)
                ],
            )
            for index, row in enumerate(rows)
        ],
    )


def cell_evidence(
    value: str,
    *,
    unit: str = "千元",
    row_name: str = "营业收入",
    column: str = "2025 年",
    preview: TablePreview | None = None,
) -> EvidenceRef:
    return EvidenceRef(
        evidence_id=f"{TABLE_ID}#r0c1",
        doc_id="cninfo-000333-2025-annual",
        doc_title="美的集团 2025 年年度报告",
        company="美的集团",
        period="2025",
        kind=EvidenceKind.table_cell,
        page=42,
        table_id=TABLE_ID,
        row_indices=[0],
        column_indices=[1],
        row_name=row_name,
        column=column,
        cell_text=value,
        unit=unit,
        content=f"主要会计数据｜第 42 页｜{row_name} 的「{column}」= {value}（单位：{unit or '未标注'}）",
        table_preview=preview,
        verified=True,
    )


def rows_evidence(preview: TablePreview, *, unit: str = "千元") -> EvidenceRef:
    return EvidenceRef(
        evidence_id=TABLE_ID,
        doc_id="cninfo-000333-2025-annual",
        doc_title="美的集团 2025 年年度报告",
        kind=EvidenceKind.table_rows,
        page=42,
        table_id=TABLE_ID,
        row_indices=list(range(len(preview.rows))),
        unit=unit,
        content="\n".join(
            " | ".join([row.row_name] + [cell.value or "" for cell in row.cells[1:]])
            for row in preview.rows
        ),
        table_preview=preview,
        verified=True,
    )


def paragraph_evidence(text: str, *, truncated: bool = False) -> EvidenceRef:
    content = text
    if truncated:
        content += "\n…[已截断，完整原文见文档]"
    return EvidenceRef(
        evidence_id="cninfo-000333-2025-annual#b0007",
        doc_id="cninfo-000333-2025-annual",
        kind=EvidenceKind.paragraph,
        page=49,
        section_id="cninfo-000333-2025-annual#s0003",
        path=["第三节", "管理层讨论与分析"],
        content=content,
        verified=False,
    )


def report_for(text: str, *evidence: EvidenceRef, calc_index=None, calcs=()) -> dict:
    citations = [FinanceCitation(marker=index, evidence=item) for index, item in enumerate(evidence, 1)]
    claim = Claim(text=text, citation_markers=list(range(1, len(evidence) + 1)), calc_index=calc_index)
    return check_claims([claim], citations, list(calcs))


def only_detail(report: dict) -> dict:
    judged = [row for row in report["details"] if row["result"] != "skipped"]
    assert len(judged) == 1, judged
    return judged[0]


# ---------------------------------------------------------------------------
# 单格直取与单位
# ---------------------------------------------------------------------------


def test_direct_cell_match_same_unit():
    report = report_for("营业收入 456,451,731 千元[1]", cell_evidence("456451731"))
    assert report["checked"] == 1
    assert report["mismatched"] == 0
    assert only_detail(report)["kind"] == "cell"


def test_cross_unit_kilo_yuan_to_yi_yuan():
    # 单元格裸数字 + 表单位千元；要点写亿元 —— 跨单位必须判一致，否则每道换算题都误报
    report = report_for("营业收入约 4,564.52 亿元[1]", cell_evidence("456451731", unit="千元"))
    assert report["mismatched"] == 0
    assert only_detail(report)["result"] == "ok"


def test_real_mismatch_is_caught():
    report = report_for("毛利率比同行高 9.09 个百分点[1]", cell_evidence("9.08", unit="", row_name="毛利率"))
    assert report["mismatched"] == 1
    assert only_detail(report)["result"] == "mismatch"
    assert should_review(report) is True


def test_percent_not_rescaled_to_fraction():
    # 0.299 与 29.9% 不是同一个数：等价规则明确不做 ×100
    report = report_for("毛利率 29.9%[1]", cell_evidence("0.299", unit="%", row_name="毛利率"))
    assert report["mismatched"] == 1


def test_unitless_cell_matches_percent():
    # 表格单元格 29.90 不带单位，要点写 29.90% —— 按原样数值判一致
    report = report_for("毛利率 29.90%[1]", cell_evidence("29.90", unit="", row_name="毛利率"))
    assert report["mismatched"] == 0


def test_percent_column_inside_money_table_is_not_mismatched():
    # 「单位：亿元」的表里也有百分比列：表级单位不能套到裸数字上
    preview = table_preview(
        columns=("项目", "2025 年"),
        rows=(("研发投入", "162.63"), ("研发投入占比", "3.90")),
        unit="亿元",
    )
    report = report_for(
        "研发投入 162.63 亿元，占营业收入 3.90%[1]",
        cell_evidence("162.63", unit="亿元", preview=preview),
    )
    assert report["mismatched"] == 0
    assert report["checked"] == 2


def test_accounting_parentheses_negative():
    report = report_for("净利润 -1,234 元[1]", cell_evidence("(1,234)", unit="元", row_name="净利润"))
    assert only_detail(report)["result"] == "ok"


def test_unicode_minus_sign_normalized():
    report = report_for("营业收入同比 \u221212.11%[1]", cell_evidence("-12.11", unit="%", row_name="增长率"))
    assert only_detail(report)["result"] == "ok"


# ---------------------------------------------------------------------------
# 区间 / 年份 / 页码
# ---------------------------------------------------------------------------


def test_range_is_skipped_not_mismatched():
    report = report_for("毛利率在 29.90%-30.50% 之间[1]", cell_evidence("29.90", unit="%", row_name="毛利率"))
    assert report["checked"] == 0
    assert report["skipped"] == 2
    assert report["mismatched"] == 0


def test_year_and_page_are_not_numbers():
    kept, skipped = claim_numbers("2025 年[1] 第 49 页营业收入 100 千元")
    assert [str(item["value"]) for item in kept] == ["100"]
    assert {item["reason"] for item in skipped} == {"year", "page"}


def test_year_range_is_skipped():
    kept, skipped = claim_numbers("2020-2025 年营业收入 100 千元")
    assert [str(item["value"]) for item in kept] == ["100"]
    assert len(skipped) == 2


# ---------------------------------------------------------------------------
# 跨行合计
# ---------------------------------------------------------------------------


def test_cross_row_sum_matches_when_no_total_cell():
    preview = table_preview(
        rows=(("第一季度", "100"), ("第二季度", "120"), ("第三季度", "180")),
    )
    report = report_for("三个季度合计 400 千元[1]", rows_evidence(preview))
    assert report["mismatched"] == 0
    assert only_detail(report)["kind"] == "sum"


def test_cross_row_sum_excludes_total_row():
    # 表里放一个错误总计 999：求和必须排除它，否则 100+120+180+999 永远对不上
    preview = table_preview(
        rows=(("第一季度", "100"), ("第二季度", "120"), ("第三季度", "180"), ("合计", "999")),
    )
    report = report_for("三个季度合计 400 千元[1]", rows_evidence(preview))
    assert report["mismatched"] == 0
    assert only_detail(report)["kind"] == "sum"


# ---------------------------------------------------------------------------
# 派生值分流
# ---------------------------------------------------------------------------


def test_derived_value_from_calc_is_not_mismatched():
    calc = CalcRecord(formula="研发费用/营业收入*100", inputs={"a": "1", "b": "2"}, result="3.90", unit="%")
    report = report_for(
        "研发费用占营业收入的比例为 3.90%[1]",
        cell_evidence("456451731"),
        calc_index=0,
        calcs=[calc],
    )
    assert report["mismatched"] == 0
    assert only_detail(report)["kind"] == "derived"


def test_calc_result_matched_without_calc_index():
    # build_claims 的 calc_index 匹配不可靠；这里允许按数值直接认领计算记录
    calc = CalcRecord(formula="a/b", inputs={}, result="12.11", unit="%")
    report = report_for("同比增长 12.11%[1]", cell_evidence("456451731"), calcs=[calc])
    assert only_detail(report)["kind"] == "derived"


def test_derived_claim_with_wrong_calc_falls_back_to_mismatch():
    calc = CalcRecord(formula="a/b", inputs={}, result="5.00", unit="%")
    report = report_for("同比增长 12.11%[1]", cell_evidence("456451731"), calc_index=0, calcs=[calc])
    assert report["mismatched"] == 1


# ---------------------------------------------------------------------------
# 引用绑定与归因
# ---------------------------------------------------------------------------


def test_wrong_citation_binding_is_caught():
    # 数字其实在 [2] 的表里，只标了 [1]：这正是「结论—证据不一致」要抓的错配
    first = cell_evidence("100", unit="千元", row_name="营业收入")
    second = cell_evidence("200", unit="千元", row_name="营业收入")
    report = report_for("营业收入 200 千元[1]", first)
    assert report["mismatched"] == 1

    # 两条都引上就是一致（多引用允许任一命中）
    citations = [
        FinanceCitation(marker=1, evidence=first),
        FinanceCitation(marker=2, evidence=second),
    ]
    claim = Claim(text="营业收入 200 千元[1][2]", citation_markers=[1, 2])
    ok_report = check_claims([claim], citations)
    assert ok_report["mismatched"] == 0


def test_unattributed_number_is_unverified_not_mismatched():
    report = report_for("营业收入 456,451,731 千元")
    assert report["checked"] == 1
    assert report["mismatched"] == 0
    assert report["unverified"] == 1


def test_truncated_paragraph_is_unverified():
    report = report_for(
        "研发投入 120 亿元[1]",
        paragraph_evidence("报告期内研发投入合计", truncated=True),
    )
    assert report["mismatched"] == 0
    assert report["unverified"] == 1


def test_paragraph_text_match():
    report = report_for(
        "研发投入 162.63 亿元[1]",
        paragraph_evidence("报告期内，公司研发投入 162.63 亿元，同比增长 17.13%。"),
    )
    assert only_detail(report)["result"] == "ok"
    assert only_detail(report)["kind"] == "text"


def test_paragraph_without_number_is_mismatch():
    report = report_for("研发投入 162.63 亿元[1]", paragraph_evidence("报告期内研发投入保持增长。"))
    assert report["mismatched"] == 1


# ---------------------------------------------------------------------------
# 输入形态与汇总
# ---------------------------------------------------------------------------


def test_dict_payload_works():
    payload = {
        "claims": [{"text": "营业收入 200 千元[1]", "citation_markers": [1], "calc_index": None}],
        "citations": [{"marker": 1, "evidence": cell_evidence("100", unit="千元").model_dump()}],
        "calc": [],
    }
    report = check_response(payload)
    assert report["mismatched"] == 1


def test_empty_report_is_consistent():
    report = check_claims([], [], [])
    assert report == {
        "checked": 0,
        "mismatched": 0,
        "unverified": 0,
        "skipped": 0,
        "details": [],
        "claims": [],
    }
    assert should_review(report) is False
    assert summarize(report) == "checked=0 mismatched=0 unverified=0 skipped=0"


# ---------------------------------------------------------------------------
# 误报率：一组「本来就该一致」的写法，一条都不许判成不一致
# ---------------------------------------------------------------------------

SHOULD_STAY_CONSISTENT = [
    ("营业收入 456,451,731 千元[1]", "456451731", "千元"),
    ("营业收入 4,564.52 亿元[1]", "456451731", "千元"),
    ("营业收入 456451731 千元[1]", "456451731", "千元"),
    ("归母净利润 385.37 亿元[1]", "38537100000", "元"),
    ("毛利率 26.44%[1]", "26.44", "%"),
    ("毛利率 26.44%[1]", "26.44", ""),
    ("同比增长 9.08%[1]", "9.08", "%"),
    ("同比下降 9.08%[1]", "9.08", "%"),  # 方向由 judge 判，数值这一层只比绝对值
    ("加权平均净资产收益率 22.15%[1]", "22.15", "%"),
    ("基本每股收益 5.60 元／股[1]", "5.60", "元"),
    ("经营活动现金流量净额 6,121,000 千元[1]", "6121000", "千元"),
    ("总资产 609,578,000 千元[1]", "609578000", "千元"),
]


def test_false_positive_rate_on_consistent_claims():
    false_positives = 0
    for text, cell, unit in SHOULD_STAY_CONSISTENT:
        report = report_for(text, cell_evidence(cell, unit=unit, row_name="指标"))
        if report["mismatched"]:
            false_positives += 1
    rate = false_positives / len(SHOULD_STAY_CONSISTENT)
    assert rate == 0, f"误报率 {rate:.1%}（{false_positives}/{len(SHOULD_STAY_CONSISTENT)}）"
