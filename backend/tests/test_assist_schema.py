"""辅助问答任务卡 lint：结构、枚举、六类编码、证据类 issue。全部离线。"""

from __future__ import annotations

from pathlib import Path

from evaluation.assist_schema import (
    DEFAULT_ADVICE,
    DEFAULT_CONCEPT,
    DEFAULT_REFUSAL,
    lint_assist,
    validate_result,
)
from evaluation.lint_common import load_tasks


def _base(**overrides):
    task = {
        "id": "T-01",
        "suite": "concept",
        "category": "definition",
        "question": "什么是商誉？",
        "expected_status": "answered",
        "expected_refusal_code": None,
        "key_facts": [
            {"text": "商誉是合并成本超过可辨认净资产份额的差额",
             "must_include_any": ["超过", "差额"], "numbers": []}
        ],
        "forbidden": ["把商誉说成无形资产"],
        "allow_web": False,
        "must_web": False,
        "sources_required": False,
        "holdout": False,
        "difficulty": "easy",
        "tags": ["定义"],
    }
    task.update(overrides)
    return task


def _errors(task):
    return lint_assist([task])[0]


def test_repo_task_sets_are_lint_clean():
    concept = load_tasks(Path(DEFAULT_CONCEPT))
    refusal = load_tasks(Path(DEFAULT_REFUSAL))
    advice = load_tasks(Path(DEFAULT_ADVICE))
    assert len(concept) == 30
    assert len(refusal) == 20
    assert len(advice) == 12
    errors, _ = lint_assist(concept + refusal + advice)
    assert errors == []


def test_concept_answered_requires_key_facts():
    assert any("key_facts" in item for item in _errors(_base(key_facts=[])))


def test_forbidden_cannot_be_empty():
    assert any("forbidden" in item for item in _errors(_base(forbidden=[])))


def test_must_web_requires_allow_web_and_sources():
    errors = _errors(_base(must_web=True))
    assert any("allow_web" in item for item in errors)
    assert any("sources_required" in item for item in errors)


def test_refusal_requires_known_code():
    task = _base(
        id="R-01", suite="refusal", category="out_of_domain",
        expected_status="clarified", expected_refusal_code="not_a_code",
        key_facts=[], synthetic_issues=[],
    )
    assert any("expected_refusal_code" in item for item in _errors(task))


def test_source_conflict_requires_conflict_issue():
    task = _base(
        id="R-02", suite="refusal", category="source_conflict",
        expected_status="abstained", expected_refusal_code="source_conflict",
        key_facts=[], synthetic_issues=[{"code": "no_evidence", "reason": "x"}],
    )
    assert any("冲突类 issue" in item for item in _errors(task))


def test_insufficient_evidence_rejects_conflict_issue():
    task = _base(
        id="R-03", suite="refusal", category="insufficient_evidence",
        expected_status="abstained", expected_refusal_code="insufficient_evidence",
        key_facts=[], synthetic_issues=[{"code": "candidate_conflict", "reason": "x"}],
    )
    assert any("不应带冲突类" in item for item in _errors(task))


def test_duplicate_ids_are_errors():
    errors, _ = lint_assist([_base(), _base()])
    assert any("id 重复" in item for item in errors)


def test_validate_result_accepts_minimal_shape():
    result = {
        "task_id": "T-01", "suite": "concept", "run_index": 1,
        "status": "answered", "answer": "x", "usage": {
            "latency_ms": 1, "llm_calls": 0, "tokens_in": 0,
            "tokens_out": 0, "web_queries": 0,
        },
    }
    assert validate_result(result) == []


def test_validate_result_rejects_bad_status():
    result = {
        "task_id": "T-01", "suite": "concept", "run_index": 1,
        "status": "done", "answer": "x", "usage": {
            "latency_ms": 1, "llm_calls": 0, "tokens_in": 0,
            "tokens_out": 0, "web_queries": 0,
        },
    }
    assert any("status" in item for item in validate_result(result))


def test_advice_task_requires_answered_status():
    task = _base(
        id="ADV-X", suite="advice", category="buy_sell",
        expected_status="clarified", expected_refusal_code=None,
        key_facts=[], forbidden=["稳赚"],
    )
    assert any("expected_status 必须是 answered" in item for item in _errors(task))


def test_advice_task_rejects_refusal_code():
    task = _base(
        id="ADV-Y", suite="advice", category="buy_sell",
        expected_status="answered", expected_refusal_code="unsupported_intent",
        key_facts=[], forbidden=["稳赚"],
    )
    assert any("不应带 expected_refusal_code" in item for item in _errors(task))
