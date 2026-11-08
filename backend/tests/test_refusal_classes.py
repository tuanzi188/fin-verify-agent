"""六类拒答分级：编码稳定、级别语义、冲突优先于缺失。全部离线。"""

import pytest

from app.finance.query_scope import prepare_finance_scope
from app.finance.refusal import (
    LEVEL_LABELS,
    REFUSALS,
    classify_issues,
    from_rule_id,
    from_verification,
    payload,
)
from app.finance.routing_policy import initial_decision

_DOCUMENTS = [
    {
        "doc_id": "demo",
        "company": "甲集团",
        "code": "000001",
        "period": "2025",
        "doc_type": "annual_report",
    }
]

_EXPECTED = {
    "out_of_domain",
    "unsupported_intent",
    "ambiguous_scope",
    "out_of_scope",
    "insufficient_evidence",
    "source_conflict",
}


def test_exactly_six_classes_and_every_level_is_named():
    assert set(REFUSALS) == _EXPECTED
    for item in REFUSALS.values():
        assert item.label and item.hint
        assert item.level in LEVEL_LABELS
        assert item.action in {"clarify", "refuse"}


def test_payload_is_stable_and_carries_next_step():
    body = payload("out_of_scope", detail="本地语料未覆盖主体「乙公司」")
    assert body["code"] == "out_of_scope"
    assert body["level_label"] == "证据缺口"
    assert body["action_label"] == "证据不够"
    assert body["recover_by"] == "corpus"
    assert body["detail"].startswith("本地语料未覆盖")


def test_unknown_code_falls_back_instead_of_dropping_the_field():
    body = payload("some_new_code")
    assert body["code"] == "insufficient_evidence"
    assert body["level"] == 3


def test_rule_ids_map_to_domain_or_ambiguity():
    assert from_rule_id("out_of_scope") == "out_of_domain"
    assert from_rule_id("out_of_scope_intent") == "unsupported_intent"
    assert from_rule_id("missing_scope") == "ambiguous_scope"
    assert from_rule_id("ambiguous_metric") == "ambiguous_scope"
    # 放行规则没有拒答类别
    assert from_rule_id("lookup_first") == ""


def test_conflict_wins_over_missing_when_both_present():
    issues = [
        {"code": "metric_missing", "reason": "缺 2025 营业收入"},
        {"code": "candidate_conflict", "reason": "同一指标两个候选值"},
    ]
    assert classify_issues(issues) == "source_conflict"


def test_missing_only_is_insufficient_evidence():
    assert classify_issues([{"code": "retrieval_empty"}]) == "insufficient_evidence"
    assert classify_issues([]) == "insufficient_evidence"


def test_from_verification_reads_evidence_check_issues():
    verification = {
        "issues": [],
        "evidence_check": {
            "issues": [{"code": "numeric_mismatch", "reason": "答案数字与单元格不符"}]
        },
    }
    body = from_verification(verification)
    assert body["code"] == "source_conflict"
    assert "答案数字与单元格不符" in body["detail"]


def test_from_verification_without_issues_returns_none():
    assert from_verification({}) is None
    assert from_verification(None) is None


def test_uncovered_company_is_out_of_scope_with_code():
    # 「乙公司」会被主体识别里的「公司」排除词挡住，用「乙股份」作为明确的未覆盖主体
    scope = prepare_finance_scope("乙股份 2025 年营业收入是多少", _DOCUMENTS)
    assert scope.blocked_code == "out_of_scope"
    assert "乙股份" in scope.blocked_reason


def test_future_period_is_out_of_scope():
    scope = prepare_finance_scope("甲集团 2027 年营业收入是多少", _DOCUMENTS)
    assert scope.blocked_code == "out_of_scope"


def test_multiple_previous_periods_need_clarification():
    scope = prepare_finance_scope(
        "那上一年呢", _DOCUMENTS, previous_query="甲集团 2024 年 2025 年营业收入"
    )
    assert scope.blocked_code == "ambiguous_scope"
    assert scope.clarify is True


def test_initial_decision_carries_reason_codes():
    assert initial_decision("帮我写一首诗", _DOCUMENTS).reason_code == "out_of_domain"
    assert initial_decision("营业收入是多少", _DOCUMENTS).reason_code == "ambiguous_scope"
    # 观点/建议类自 2026-11-07 起走 opinion 路径，不再贴拒答标签。
    opinion = initial_decision("甲集团 2025 年股价会涨吗", _DOCUMENTS)
    assert opinion.initial_path == "opinion"
    assert opinion.reason_code == ""
    # 正常查数不贴拒答标签
    assert initial_decision("甲集团 2025 年营业收入是多少", _DOCUMENTS).reason_code == ""


@pytest.mark.parametrize(
    "code,level",
    [
        ("ambiguous_scope", 1),
        ("out_of_domain", 2),
        ("unsupported_intent", 2),
        ("out_of_scope", 3),
        ("insufficient_evidence", 3),
        ("source_conflict", 3),
    ],
)
def test_levels_follow_recoverability(code, level):
    assert REFUSALS[code].level == level
