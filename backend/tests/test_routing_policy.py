"""规则边界单测：使用临时证据，不构建真实问题金标集。"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.finance.routing_policy import (
    assess_evidence,
    initial_decision,
    response_upgrade,
    upgrade_decision,
)
from app.finance.store import FinanceStore
from app.models.finance import EvidenceRef, FinanceChatResponse, FinanceCitation
from app.services import finance_rag
from tests.test_finance_api import write_demo_document


def policy_inventory() -> list[dict]:
    return [
        {"company": "甲集团", "period": "2025", "code": "000001"},
        {"company": "乙集团", "period": "2025", "code": "000002"},
    ]


def policy_cell(
    value: str = "12.34",
    *,
    year: str = "2025 年",
    row: str = "营业收入",
    unit: str = "千元",
    company: str = "甲集团",
) -> FinanceCitation:
    return FinanceCitation(
        marker=1,
        evidence=EvidenceRef(
            evidence_id="doc#t1#0#1",
            doc_id="doc",
            kind="table_cell",
            company=company,
            period="2025",
            table_id="doc#t1",
            row_name=row,
            column=year,
            cell_text=value,
            unit=unit,
        ),
    )


@pytest.mark.parametrize(
    "query, expected",
    [
        ("甲集团2025年营业收入是多少？", "structured_rag"),
        ("甲集团2025年营业收入是多少？比上年增减多少？", "structured_rag"),
        ("甲集团2025年披露的毛利率是多少？", "structured_rag"),
        ("甲集团2025年收入和归母净利润分别是多少？", "structured_rag"),
        ("甲集团2024年和2025年营业收入分别是多少？", "structured_rag"),
        ("甲集团2025年营业收入请自行计算同比增长率", "agent"),
        ("甲集团2025年营业收入折合多少亿元？", "agent"),
        ("甲集团2025年营业收入两处披露是否一致？", "agent"),
        ("甲集团与乙集团2025年营业收入比较", "agent"),
        ("甲集团营业收入是多少？", "clarify"),
        ("甲集团2025年净利润是多少？", "clarify"),
        ("丙集团2025年营业收入是多少？", "abstain"),
        ("甲集团2026年营业收入是多少？", "abstain"),
    ],
)
def test_policy_initial_operations(query, expected):
    assert initial_decision(query, policy_inventory()).initial_path == expected


@pytest.mark.parametrize(
    "query",
    ["今天天气怎么样", "帮我写一首诗", "你好", "讲个笑话"],
)
def test_policy_out_of_scope_is_clarified_before_retrieval(query):
    decision = initial_decision(query, policy_inventory())
    assert decision.initial_path == "clarify"
    assert decision.rule_id == "out_of_scope"


@pytest.mark.parametrize(
    "query, expected",
    [
        ("这份年报有多少页", "structured_rag"),
        ("帮我总结一下这份材料", "structured_rag"),
        ("2024 年情况怎么样", "structured_rag"),
    ],
)
def test_policy_finance_anchor_is_not_treated_as_out_of_scope(query, expected):
    decision = initial_decision(query, policy_inventory())
    assert decision.rule_id != "out_of_scope"
    assert decision.initial_path == expected


def test_policy_exact_cell_sufficient_but_wrong_period_or_metric_not_sufficient():
    assert (
        assess_evidence("甲集团2025年营业收入是多少", [policy_cell()])["sufficient"]
        is True
    )
    assert (
        assess_evidence("甲集团2024年营业收入是多少", [policy_cell()])["sufficient"]
        is False
    )
    assert (
        assess_evidence("甲集团2025年归母净利润是多少", [policy_cell()])["sufficient"]
        is False
    )


def test_policy_multiple_metrics_must_all_be_supported():
    check = assess_evidence(
        "甲集团2025年营业收入和归母净利润分别是多少", [policy_cell()]
    )
    assert check["sufficient"] is False
    assert any("归母净利润" in issue["reason"] for issue in check["issues"])


def test_policy_wrong_company_is_not_sufficient_with_inventory():
    check = assess_evidence("甲集团2025年营业收入是多少", [policy_cell(company="乙集团")], policy_inventory())
    assert check["sufficient"] is False
    assert any("甲集团" in issue["reason"] for issue in check["issues"])


def test_policy_company_alias_is_checked_against_inventory():
    assert assess_evidence("甲2025年营业收入是多少", [policy_cell()], policy_inventory())["sufficient"] is True


def test_policy_bare_growth_number_does_not_inherit_money_unit():
    check = assess_evidence("甲集团2025年营业收入同比增速是多少", [policy_cell("12.11", year="本年比上年增减")])
    assert check["sufficient"] is False
    assert "unit_missing" in {issue["code"] for issue in check["issues"]}


def test_policy_growth_requires_disclosed_growth_column():
    assert (
        assess_evidence("甲集团2025年营业收入同比增速是多少", [policy_cell()])[
            "sufficient"
        ]
        is False
    )
    growth = policy_cell("12.11%", year="本年比上年增减")
    assert (
        assess_evidence("甲集团2025年营业收入同比增速是多少", [growth])["sufficient"]
        is True
    )


def test_policy_conflicts_unit_and_scope_need_review():
    check = assess_evidence(
        "甲集团2025年营业收入是多少", [policy_cell(), policy_cell("99")]
    )
    assert check["sufficient"] is False
    assert "candidate_conflict" in {issue["code"] for issue in check["issues"]}
    assert (
        assess_evidence("甲集团2025年营业收入是多少", [policy_cell(unit="")])[
            "sufficient"
        ]
        is False
    )
    assert (
        assess_evidence("甲集团2025年合并营业收入是多少", [policy_cell()])["sufficient"]
        is False
    )


def test_policy_unknown_text_is_not_claimed_verified_or_automatically_upgraded():
    check = assess_evidence("甲集团2025年审计意见是什么", [policy_cell()])
    assert check["sufficient"] is None
    assert not upgrade_decision("answered", check["issues"])["upgrade"]


@pytest.mark.parametrize(
    "status, code, blocked, upgrade",
    [
        ("answered", "numeric_mismatch", False, True),
        ("needs_review", "input_source_missing", False, True),
        ("answered", "retrieval_empty", False, True),
        ("error", "retrieval_empty", False, False),
        ("clarified", "citation_missing", False, False),
        ("abstained", "retrieval_empty", True, False),
        ("needs_review", "unknown_issue", False, False),
    ],
)
def test_policy_upgrade_allowlist(status, code, blocked, upgrade):
    result = upgrade_decision(
        status, [{"code": code, "reason": "具体原因"}], scope_blocked=blocked
    )
    assert result["upgrade"] is upgrade


def test_policy_generation_failure_does_not_upgrade():
    response = FinanceChatResponse(
        status="error", answer="生成失败", citations=[policy_cell()]
    )
    assert not response_upgrade(response)["upgrade"]


@pytest.mark.asyncio
async def test_policy_missing_evidence_skips_generation_and_does_not_save_intermediate(
    tmp_path, monkeypatch
):
    store = FinanceStore(write_demo_document(tmp_path))

    class PolicyEmptyIndex:
        def search(self, *args, **kwargs):
            return []

    memory = AsyncMock()
    chat = AsyncMock()
    monkeypatch.setattr(finance_rag, "get_finance_store", lambda: store)
    monkeypatch.setattr(finance_rag, "get_block_index", lambda: PolicyEmptyIndex())
    monkeypatch.setattr(finance_rag, "conversation_memory", memory)
    monkeypatch.setattr(finance_rag.model_provider, "chat", chat)
    response = await finance_rag.run_structured_rag(
        query="XX股份2025年营业收入是多少", conversation_id="test", persist=False
    )
    assert response.token_usage["llm_calls"] == 0
    assert response.verification["evidence_check"]["sufficient"] is False
    memory.add_turn.assert_not_awaited()
    chat.assert_not_awaited()
