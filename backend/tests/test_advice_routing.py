"""观点/建议路由：该走的走 opinion，不该走的不能被吞。全部离线。"""

from __future__ import annotations

import pytest

from app.finance.routing_policy import initial_decision
from evaluation.assist_fixtures import DEMO_DOCUMENTS


@pytest.mark.parametrize(
    "question",
    [
        "甲集团值得投资吗？",
        "甲集团现在能不能买？",
        "甲集团该不该卖？",
        "甲集团 2025 年的目标价是多少？",
        "甲集团股价明年会涨到多少？",
        "推荐一只明年收益最高的基金。",
        "甲集团和乙股份哪个更值得买？",
        "甲集团适合长期持有吗？",
        "甲集团现在估值贵不贵？",
        "我要不要加仓甲集团？",
    ],
)
def test_advice_questions_route_to_opinion(question):
    decision = initial_decision(question, DEMO_DOCUMENTS)
    assert decision.initial_path == "opinion"
    assert decision.rule_id == "opinion_intent"


@pytest.mark.parametrize(
    "question,expected_path",
    [
        ("甲集团 2025 年营业收入是多少？", "structured_rag"),
        ("可转债的转股价是多少？", "structured_rag"),
        ("债券的信用评级是多少？", "structured_rag"),
        ("甲集团 2025 年研发投入占营业收入比例是多少？", "structured_rag"),
        ("市盈率是什么意思？", "concept"),
        ("什么是商誉？", "concept"),
    ],
)
def test_disclosure_and_concept_questions_do_not_enter_opinion(question, expected_path):
    decision = initial_decision(question, DEMO_DOCUMENTS)
    assert decision.initial_path == expected_path
    assert decision.rule_id != "opinion_intent"


def test_ambiguous_stock_question_stays_out_of_opinion():
    # 「帮我看看这只股票」意图不明：可以澄清，但不能直接当观点回答。
    decision = initial_decision("帮我看看这只股票", DEMO_DOCUMENTS)
    assert decision.initial_path != "opinion"
