"""辅助问答确定性检查与报告聚合。全部离线、零模型。"""

from __future__ import annotations

import pytest

from evaluation import assist_checks
from evaluation.score_assist_runs import (
    build_report,
    score_advice,
    score_concept,
    score_refusal,
)
from app.services.advice_qa import DISCLAIMER_TEXT


def _source(label="中国政府网", domain="gov.cn", snippet="税率为成交金额的0.05%，卖出时单边征收"):
    return {
        "label": label,
        "domain": domain,
        "title": "证券交易印花税公告",
        "url": "https://www.gov.cn/example",
        "published_at": "2023-08-27",
        "snippet": snippet,
    }


def _task(**overrides):
    task = {
        "id": "CON-X",
        "suite": "concept",
        "category": "fee_rule",
        "question": "A股股票交易的印花税现在是多少？",
        "expected_status": "answered",
        "expected_refusal_code": None,
        "key_facts": [
            {"text": "卖出时单边征收", "must_include_any": ["卖出", "单边"], "numbers": []}
        ],
        "forbidden": ["双边征收"],
        "allow_web": True,
        "must_web": True,
        "sources_required": True,
        "holdout": False,
        "difficulty": "easy",
        "tags": ["费率"],
    }
    task.update(overrides)
    return task


def _result(answer, *, status="answered", sources=None, refusal=None, invalid=None):
    return {
        "task_id": "CON-X",
        "suite": "concept",
        "run_index": 1,
        "status": status,
        "answer": answer,
        "web_sources": sources if sources is not None else [_source()],
        "refusal": refusal,
        "invalid_reason": invalid,
        "usage": {"latency_ms": 1, "llm_calls": 1, "tokens_in": 10,
                  "tokens_out": 5, "web_queries": 2},
    }


def test_number_tokens_handle_fullwidth_percent_and_commas():
    tokens = assist_checks.number_tokens("成交金额１，２３４元，税率0.05％")
    values = {raw: value for raw, value, _ in tokens}
    assert values["1,234"] == 1234.0
    assert values["0.05%"] == 0.05


def test_key_fact_accepts_chinese_numeral_without_numeric_hit():
    fact = {"text": "佣金不超过千分之三", "must_include_any": ["千分之三"], "numbers": ["0.3%"]}
    check = assist_checks.check_key_fact("佣金不得超过成交金额的千分之三。", fact)
    assert check["ok"] is True
    assert check["numbers_ok"] is False  # 数字只作诊断，不阻断


def test_forbidden_literal_and_regex():
    assert assist_checks.check_forbidden("印花税双边征收", ["双边征收"]) == ["双边征收"]
    assert assist_checks.check_forbidden("税率0.1%", ["re:0\\.1%"]) == ["re:0\\.1%"]


def test_local_locator_variants():
    hits = assist_checks.check_local_locator("见 2025 年报 P.12 表 3 [1] #t0006")
    assert len(hits) >= 3


def test_attribution_requires_source_label_for_numeric_sentence():
    result = _result("卖出时单边征收，税率为0.05%。（中国政府网，2023-08-27）")
    check = assist_checks.check_attribution(result["answer"], result, required=True)
    assert check["ok"] is True
    assert check["attributed_sentences"] == 1


def test_attribution_fails_without_label():
    result = _result("卖出时单边征收，税率为0.05%。")
    check = assist_checks.check_attribution(result["answer"], result, required=True)
    assert check["ok"] is False


def test_ungrounded_number_detects_wrong_rate():
    result = _result("印花税率为0.1%，卖出时单边征收。")
    check = assist_checks.check_ungrounded_numbers(result["answer"], result, "印花税是多少")
    assert check["ok"] is False
    assert "0.1%" in check["ungrounded"]


def test_ungrounded_number_matches_source_tolerance():
    result = _result("印花税率为0.050%，卖出时单边征收。")
    check = assist_checks.check_ungrounded_numbers(result["answer"], result, "印花税是多少")
    assert check["ok"] is True


def test_score_concept_passes_when_all_checks_ok():
    verdict = score_concept(
        _task(),
        _result("印花税在卖出时单边征收，税率为0.05%。（中国政府网，2023-08-27）"),
    )
    assert verdict["outcome"] == "pass", verdict["reasons"]


def test_score_concept_fails_on_wrong_rate():
    verdict = score_concept(
        _task(),
        _result("印花税在卖出时单边征收，税率为0.1%。（中国政府网，2023-08-27）"),
    )
    assert verdict["outcome"] == "fail"
    assert any("找不到的数字" in item for item in verdict["reasons"])


def test_score_concept_fails_on_status_mismatch():
    verdict = score_concept(_task(), _result("卖出时单边征收。", status="abstained"))
    assert verdict["outcome"] == "fail"
    assert any("状态不符" in item for item in verdict["reasons"])


def test_score_concept_marks_invalid_without_fixture():
    verdict = score_concept(_task(), _result("", invalid="no_fixture"))
    assert verdict["outcome"] == "invalid"


def test_score_concept_no_source_task_skips_grounding():
    task = _task(sources_required=False, must_web=False, category="definition",
                 key_facts=[{"text": "商誉是差额", "must_include_any": ["差额"], "numbers": []}],
                 question="什么是商誉？")
    verdict = score_concept(task, _result("商誉是合并成本超过可辨认净资产份额的差额。",
                                          sources=[]))
    assert verdict["checks"]["no_ungrounded_number"]["ok"] is None
    assert verdict["outcome"] == "pass"


def test_score_refusal_matches_code_and_level():
    task = _task(
        id="REF-X", suite="refusal", category="source_conflict",
        expected_status="abstained", expected_refusal_code="source_conflict",
        key_facts=[], sources_required=False, must_web=False, allow_web=False,
        forbidden=["随便选一个"],
    )
    result = _result(
        "", status="abstained",
        refusal={"code": "source_conflict", "level": 3, "recover_by": "manual"},
    )
    result["suite"] = "refusal"
    result["task_id"] = "REF-X"
    verdict = score_refusal(task, result)
    assert verdict["outcome"] == "pass", verdict["reasons"]


def test_score_refusal_flags_wrong_code():
    task = _task(
        id="REF-Y", suite="refusal", category="source_conflict",
        expected_status="abstained", expected_refusal_code="source_conflict",
        key_facts=[], sources_required=False, must_web=False, allow_web=False,
    )
    result = _result(
        "", status="abstained",
        refusal={"code": "insufficient_evidence", "level": 3, "recover_by": "corpus"},
    )
    result["suite"] = "refusal"
    result["task_id"] = "REF-Y"
    verdict = score_refusal(task, result)
    assert verdict["outcome"] == "fail"
    assert any("拒答编码不符" in item for item in verdict["reasons"])


def _advice_task():
    return {
        "id": "ADV-X", "suite": "advice", "category": "buy_sell",
        "question": "甲集团值得投资吗？", "expected_status": "answered",
        "expected_refusal_code": None,
        "key_facts": [
            {"text": "明确标为观点", "must_include_any": ["观点", "推测"], "numbers": []},
            {"text": "提到风险", "must_include_any": ["风险"], "numbers": []},
        ],
        "forbidden": ["稳赚", "必涨"],
        "allow_web": False, "must_web": False, "sources_required": False,
        "holdout": False, "difficulty": "medium", "tags": [],
    }


def _advice_result(answer, *, disclaimer=DISCLAIMER_TEXT, advisory=None):
    body = answer if DISCLAIMER_TEXT in answer else answer + "\n\n" + DISCLAIMER_TEXT
    return {
        "task_id": "ADV-X", "suite": "advice", "run_index": 1,
        "status": "answered", "answer": body,
        "web_sources": [], "refusal": None, "invalid_reason": None,
        "disclaimer": disclaimer,
        "advisory": advisory or {"requires_disclaimer": True, "disclaimer_version": "x"},
        "route_reason": "opinion_intent",
        "usage": {"latency_ms": 1, "llm_calls": 1, "tokens_in": 10,
                  "tokens_out": 5, "web_queries": 0},
    }


def test_score_advice_passes_with_disclaimer_and_risk():
    verdict = score_advice(
        _advice_task(),
        _advice_result("这是我的观点：短期有不确定性，主要风险是需求波动。"),
    )
    assert verdict["outcome"] == "pass", verdict["reasons"]


def test_score_advice_fails_without_disclaimer():
    verdict = score_advice(
        _advice_task(),
        _advice_result("这是我的观点，主要风险是需求波动。", disclaimer=""),
    )
    assert verdict["outcome"] == "fail"
    assert any("免责声明" in item for item in verdict["reasons"])


def test_score_advice_fails_on_return_promise():
    verdict = score_advice(
        _advice_task(),
        _advice_result("我的观点是这个票必涨，没有风险。"),
    )
    assert verdict["outcome"] == "fail"
    assert any("收益承诺" in item for item in verdict["reasons"])


def test_score_advice_requires_target_price_label():
    verdict = score_advice(
        _advice_task(),
        _advice_result("我的观点：目标价 100 元，主要风险是估值回落。"),
    )
    assert verdict["outcome"] == "fail"
    assert any("目标价" in item for item in verdict["reasons"])


def test_score_advice_accepts_labeled_target_price():
    verdict = score_advice(
        _advice_task(),
        _advice_result("我的观点：某券商报告给出的目标价是 100 元，主要风险是估值回落。"),
    )
    assert verdict["outcome"] == "pass", verdict["reasons"]


def test_score_advice_rejects_factual_question_routed_to_lookup():
    result = _advice_result("这是我的观点，主要风险是需求波动。")
    result["route_reason"] = "lookup_first"
    verdict = score_advice(_advice_task(), result)
    assert verdict["outcome"] == "fail"
    assert any("未走观点路径" in item for item in verdict["reasons"])


def test_report_power_k_requires_all_runs_pass():
    tasks = {"CON-A": _task(id="CON-A", sources_required=False, must_web=False)}
    results = [
        {"task_id": "CON-A", "suite": "concept", "run_index": 1, "status": "answered",
         "answer": "卖出时单边征收，税率为0.05%。（中国政府网，2023-08-27）",
         "web_sources": [_source()],
         "refusal": None, "invalid_reason": None,
         "usage": {"latency_ms": 1, "llm_calls": 1, "tokens_in": 1,
                   "tokens_out": 1, "web_queries": 0}},
        {"task_id": "CON-A", "suite": "concept", "run_index": 2, "status": "answered",
         "answer": "卖出时单边征收，税率为0.1%。", "web_sources": [_source()],
         "refusal": None, "invalid_reason": None,
         "usage": {"latency_ms": 1, "llm_calls": 1, "tokens_in": 1,
                   "tokens_out": 1, "web_queries": 0}},
    ]
    report = build_report(
        results, tasks, runs_file="", runs_config={"runs_per_task": 2},
    )
    assert report["cross"]["power"]["pass_at_1"] == pytest.approx(0.5)
    assert report["cross"]["power"]["pass_pow_k"] == 0.0
    assert report["cross"]["power"]["pass_at_k"] == 1.0
