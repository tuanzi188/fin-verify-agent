"""括号负数的归一：单元格 `(5,728,446)` 与答案 `-5,728,446` 必须能对上。

财报用括号表示负数（文档级 `negative_format=parentheses`），但括号也用来写序号
`（1）`、`（2）`。这两种在文本上长得一样，取舍必须由"带不带千分位/小数点"决定，
否则要么负号丢失（校验误判），要么编号被读成 -1。
"""

from decimal import Decimal

from app.finance.calculation_records import record_number_tokens
from app.models.finance import (
    Claim,
    EvidenceKind,
    EvidenceRef,
    FinanceChatResponse,
    FinanceCitation,
)
from app.services.finance_validation import (
    finance_claim_values,
    finance_values_match,
    validate_finance_response,
)


def test_parenthesized_amount_is_negative():
    assert record_number_tokens("(5,728,446)") == [(Decimal("-5728446"), "")]
    assert record_number_tokens("（8,151,117）") == [(Decimal("-8151117"), "")]


def test_parenthesized_amount_tolerates_spaces():
    # 比亚迪 t0127 的原始形态：右括号前有空格
    assert record_number_tokens("(1,474,894 )") == [(Decimal("-1474894"), "")]
    assert record_number_tokens("（ 12,345.00 ）") == [(Decimal("-12345.00"), "")]


def test_unit_after_the_closing_paren_is_kept():
    # 单位在括号外面；丢了单位，校验会按"结论有单位、证据没单位"判不一致
    assert record_number_tokens("(5,728,446) 千元") == [
        (Decimal("-5728446"), "千元")
    ]
    assert record_number_tokens("减少（8,151,117）万元") == [
        (Decimal("-8151117"), "万元")
    ]


def test_enumeration_numbers_stay_positive():
    # `（1）`、`（2）说明` 是序号，不是负数
    assert record_number_tokens("（1）") == [(Decimal("1"), "")]
    assert record_number_tokens("（2）说明") == [(Decimal("2"), "")]
    assert record_number_tokens("(2025)") == [(Decimal("2025"), "")]


def test_minus_sign_and_parentheses_agree():
    assert record_number_tokens("-5,728,446") == record_number_tokens("(5,728,446)")
    assert record_number_tokens("−5,728,446") == [(Decimal("-5728446"), "")]


def test_claim_values_normalize_the_same_way():
    assert finance_claim_values("减少 (5,728,446) 千元") == [
        (Decimal("-5728446"), "千元")
    ]
    assert finance_values_match(
        (Decimal("-5728446"), "千元"), (Decimal("-5728446"), "千元")
    )


def _response(claim_text: str, cell_text: str) -> FinanceChatResponse:
    evidence = EvidenceRef(
        evidence_id="doc#t1#3#2",
        doc_id="doc",
        doc_title="测试年报",
        kind=EvidenceKind.table_cell,
        table_id="doc#t1",
        row_indices=[3],
        column_indices=[2],
        cell_text=cell_text,
        unit="千元",
        verified=True,
    )
    return FinanceChatResponse(
        status="answered",
        answer=f"{claim_text} [1]",
        claims=[Claim(text=claim_text, citation_markers=[1])],
        citations=[FinanceCitation(marker=1, evidence=evidence)],
    )


def test_negative_answer_matches_parenthesized_cell():
    response = _response("投资收益减少 -5,728,446 千元", "(5,728,446)")
    validate_finance_response(response)
    codes = [issue["code"] for issue in response.verification.get("issues") or []]
    assert "numeric_mismatch" not in codes


def test_positive_answer_against_negative_cell_is_flagged():
    """符号是信息：写成正数就是与原文相反，不能被放过。"""
    response = _response("投资收益减少 5,728,446 千元", "(5,728,446)")
    validate_finance_response(response)
    codes = [issue["code"] for issue in response.verification.get("issues") or []]
    assert "numeric_mismatch" in codes
