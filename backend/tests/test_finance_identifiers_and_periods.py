"""期间与标识符解析的回归。

1. 2026-11-05：数值校验把文档/表 id 与行列坐标里的数字当成结论值，导致按规则写
   表 id 的正常答案被判 needs_review。
2. 2026-11-05：期间过滤只认文档级报告期，问 2022 年（2024 年报的比较期列）时
   allowed 被清空，明明库里有的数被拒答。
3. 2026-11-06：「近三年」「上一季度」这类相对期间不靠模型推断，按语料最新年份与
   当前日历确定性解析；解析结果超出覆盖范围时由同一个边界检查拒答。
"""

from decimal import Decimal

from app.finance import query_scope as qs
from app.finance.block_index import _period_covers
from app.finance.query_scope import prepare_finance_scope
from app.services.finance_validation import finance_claim_values

_DOCUMENTS = [
    {"doc_id": "d1", "company": "甲集团", "code": "000001", "period": "2025", "doc_type": "annual_report"},
    {"doc_id": "d2", "company": "甲集团", "code": "000001", "period": "2024", "doc_type": "annual_report"},
]


def test_recent_years_resolve_backwards_from_corpus_max():
    assert qs._recent_year_span("近三年营业收入", {"2024", "2025"}) == [
        "2023",
        "2024",
        "2025",
    ]
    assert qs._recent_year_span("最近五年", {"2024", "2025"}) == [
        "2021",
        "2022",
        "2023",
        "2024",
        "2025",
    ]
    # 中文数字与阿拉伯数字等价
    assert qs._recent_year_span("过去2年", {"2024", "2025"}) == ["2024", "2025"]
    # 没有语料年份时无从倒推，交给上游澄清
    assert qs._recent_year_span("近三年", set()) == []


def test_recent_years_phrase_rewrites_to_explicit_years():
    scope = prepare_finance_scope("甲集团 近三年营业收入", _DOCUMENTS)
    assert scope.years == ("2023", "2024", "2025")
    # 三个年份都要写进改写后的问题，否则 agent 只能自己猜
    assert "2023" in scope.resolved_query
    assert "2024" in scope.resolved_query
    assert "2025" in scope.resolved_query
    assert "近三年" not in scope.resolved_query


def test_absurd_span_is_not_guessed():
    # 「近二十年」不解析成 20 个年份，留给澄清
    assert qs._recent_year_span("近二十年营业收入", {"2024", "2025"}) == []
    scope = prepare_finance_scope("甲集团 近二十年营业收入", _DOCUMENTS)
    assert scope.years == ()


def test_relative_quarter_maps_to_previous_calendar_quarter():
    assert qs._relative_quarter("上一季度", now=qs.datetime(2026, 11, 6)) == ("2026", "Q3")
    assert qs._relative_quarter("本季度", now=qs.datetime(2026, 11, 6)) == ("2026", "Q4")
    # 一季度往回退要跨年
    assert qs._relative_quarter("上个季度", now=qs.datetime(2026, 2, 10)) == ("2025", "Q4")


def test_relative_quarter_outside_corpus_is_refused(monkeypatch):
    # 固定解析结果，避免用例随真实日期漂移
    monkeypatch.setattr(qs, "_relative_quarter", lambda phrase, now=None: ("2026", "Q3"))
    scope = prepare_finance_scope("甲集团 上一季度净利润", _DOCUMENTS)
    assert scope.years == ("2026",)
    assert scope.quarter == "Q3"
    assert scope.blocked_code == "out_of_scope"
    assert "本地最新覆盖 2025 年" in scope.blocked_reason


def test_relative_quarter_inside_corpus_is_answerable(monkeypatch):
    monkeypatch.setattr(qs, "_relative_quarter", lambda phrase, now=None: ("2025", "Q4"))
    scope = prepare_finance_scope("甲集团 上一季度营业收入", _DOCUMENTS)
    # 季报不存在，但年报的「分季度主要财务指标」披露了营收，可以回答
    assert scope.blocked_reason == ""
    assert scope.years == ("2025",)
    assert scope.quarter == "Q4"


def test_relative_quarter_conflicting_with_explicit_year_clarifies():
    scope = prepare_finance_scope("甲集团 2025 年上一季度净利润", _DOCUMENTS)
    assert scope.clarify is True
    assert scope.blocked_code == "ambiguous_scope"
    assert "季度" in scope.blocked_reason


def test_explicit_quarter_is_untouched():
    scope = prepare_finance_scope("甲集团 2025 年第三季度营业收入", _DOCUMENTS)
    assert scope.years == ("2025",)
    assert scope.quarter == "Q3"
    assert scope.blocked_reason == ""


def test_followup_recent_span_wins_over_previous_year():
    """「那近三年呢」自带期间语义，不能被上一轮的单一年份顶掉。"""
    scope = prepare_finance_scope(
        "那近三年呢", _DOCUMENTS, previous_query="甲集团 2025 年营业收入"
    )
    assert scope.years == ("2023", "2024", "2025")


def test_followup_relative_quarter_wins_over_previous_year(monkeypatch):
    monkeypatch.setattr(qs, "_relative_quarter", lambda phrase, now=None: ("2025", "Q4"))
    scope = prepare_finance_scope(
        "那上一季度呢", _DOCUMENTS, previous_query="甲集团 2025 年营业收入"
    )
    assert scope.quarter == "Q4"
    assert scope.years == ("2025",)


def test_claim_values_ignore_document_and_table_ids():
    text = (
        "美的集团 2025 年营业收入为 456,451,731 千元，"
        "见 PDF 第 9 页，表 cninfo-000333-2025-annual#t0006，"
        "行0「营业收入（千元）」，列「2025 年」，同比 12.11%。"
    )
    values = [value for value, _unit in finance_claim_values(text)]
    assert Decimal("456451731") in values
    assert Decimal("12.11") in values
    # 这些数字只存在于标识符里，不应进入数值校验池
    for noise in (Decimal("-333"), Decimal("6"), Decimal("0"), Decimal("2025")):
        assert noise not in values, f"标识符数字 {noise} 被当成结论值"


def test_claim_values_ignore_bare_block_and_row_coordinates():
    text = "读 #t0006、#s0001、#rows#3-5，行 3–5、行 2、列 2024，数值 987,654 千元"
    values = [value for value, _unit in finance_claim_values(text)]
    assert Decimal("987654") in values
    assert Decimal("3") not in values
    assert Decimal("5") not in values
    assert Decimal("2") not in values


def test_period_covers_exact_year_only_when_document_exists():
    document_years = {"2024", "2025"}
    # 有 2025 年报：问 2025 只匹配 2025
    assert _period_covers("2025", {"2025"}, document_years) is True
    # 有 2024 年报：问 2024 时 2025 年报不参与（保持原有行为）
    assert _period_covers("2025", {"2024"}, document_years) is False


def test_period_covers_allows_comparative_years():
    document_years = {"2024", "2025"}
    # 2024 年报的「主要会计数据」带 2022 年比较列
    assert _period_covers("2024", {"2022"}, document_years) is True
    # 2025 年报只带 2024/2023，够不到 2022
    assert _period_covers("2025", {"2022"}, document_years) is False


def test_period_covers_still_blocks_future_periods():
    document_years = {"2024", "2025"}
    assert _period_covers("2025", {"2026"}, document_years) is False
    assert _period_covers("2024", {"2026"}, document_years) is False


def test_period_covers_rejects_too_far_history():
    document_years = {"2024", "2025"}
    assert _period_covers("2024", {"2019"}, document_years) is False
