"""辅助问答 runner：概念题注入 fake chat，拒答题零模型。全部离线。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from evaluation import assist_fixtures, run_assist_eval
from evaluation.assist_fixtures import DEMO_DOCUMENTS
from app.services.advice_qa import DISCLAIMER_TEXT


FIXTURE = Path(__file__).resolve().parents[1] / "evaluation" / "fixtures" / "assist_web_20261107.json"


def _fake_response(answer: str, *, status="answered", notes=None):
    return SimpleNamespace(
        status=status,
        answer=answer,
        notes=list(notes or []),
        web_sources=[],
        refusal=None,
        resolved_query="",
        token_usage={
            "prompt_tokens": 10,
            "completion_tokens": 20,
            "llm_calls": 1,
            "web_queries": 0,
        },
    )


@pytest.mark.asyncio
async def test_concept_task_calls_product_entry(monkeypatch):
    async def fake_answer_concept(**kwargs):
        return _fake_response(
            "商誉是企业合并中合并成本超过可辨认净资产份额的差额，非同一控制下产生。"
        )

    monkeypatch.setattr(run_assist_eval, "answer_concept", fake_answer_concept)
    task = {
        "id": "CON-01", "suite": "concept", "category": "definition",
        "question": "什么是商誉？", "expected_status": "answered",
        "expected_refusal_code": None, "key_facts": [], "forbidden": ["x"],
        "allow_web": False, "must_web": False, "sources_required": False,
        "holdout": False, "difficulty": "easy", "tags": [],
    }
    result = await run_assist_eval.run_concept_task(task, 1)
    assert result["status"] == "answered"
    assert result["suite"] == "concept"
    assert result["usage"]["llm_calls"] == 1


@pytest.mark.asyncio
async def test_must_web_without_replay_is_invalid(monkeypatch):
    async def fake_answer_concept(**kwargs):  # pragma: no cover - 不应被调用
        raise AssertionError("must_web 无快照时不应调用模型")

    monkeypatch.setattr(run_assist_eval, "answer_concept", fake_answer_concept)
    task = {
        "id": "CON-21", "suite": "concept", "category": "fee_rule",
        "question": "券商佣金的上限是多少？", "expected_status": "answered",
        "expected_refusal_code": None, "key_facts": [], "forbidden": ["x"],
        "allow_web": True, "must_web": True, "sources_required": True,
        "holdout": False, "difficulty": "medium", "tags": [],
    }
    result = await run_assist_eval.run_concept_task(task, 1, provider=None)
    assert result["invalid_reason"] == "no_fixture"
    assert result["usage"]["llm_calls"] == 0


def test_refusal_task_is_zero_model():
    task = {
        "id": "REF-05", "suite": "refusal", "category": "out_of_domain",
        "question": "今天北京天气怎么样？", "previous_query": "",
        "expected_status": "clarified", "expected_refusal_code": "out_of_domain",
        "synthetic_issues": [], "key_facts": [], "forbidden": ["x"],
        "allow_web": False, "must_web": False, "sources_required": False,
        "holdout": False, "difficulty": "easy", "tags": [],
    }
    result = run_assist_eval.run_refusal_task(task, 1)
    assert result["status"] == "clarified"
    assert result["refusal"]["code"] == "out_of_domain"
    assert result["usage"]["llm_calls"] == 0


def test_refusal_synthetic_issue_maps_to_code():
    task = {
        "id": "REF-22", "suite": "refusal", "category": "source_conflict",
        "question": "甲集团 2025 年研发投入金额是多少？", "previous_query": "",
        "expected_status": "abstained", "expected_refusal_code": "source_conflict",
        "synthetic_issues": [{"code": "unit_missing", "reason": "千元与元并存"}],
        "key_facts": [], "forbidden": ["x"],
        "allow_web": False, "must_web": False, "sources_required": False,
        "holdout": False, "difficulty": "hard", "tags": [],
    }
    result = run_assist_eval.run_refusal_task(task, 1)
    assert result["status"] == "abstained"
    assert result["refusal"]["code"] == "source_conflict"


def test_replay_provider_matches_rewritten_query():
    provider = assist_fixtures.load_replay_provider(FIXTURE)
    items = provider.search("印花税 最新规定", domains=[], num_results=10, timeout=5)
    assert items
    assert all("url" in item and item["url"] for item in items)


def test_fixture_sha256_is_stable():
    assert assist_fixtures.fixture_sha256(FIXTURE) == assist_fixtures.fixture_sha256(FIXTURE)


def test_main_writes_runs_and_task_snapshot(monkeypatch, tmp_path):
    async def fake_answer_concept(**kwargs):
        return _fake_response("商誉是企业合并中合并成本超过可辨认净资产份额的差额。")

    monkeypatch.setattr(run_assist_eval, "answer_concept", fake_answer_concept)
    out = tmp_path / "runs.json"
    code = run_assist_eval.main(
        ["--suite", "concept", "--tasks", str(run_assist_eval.DEFAULT_CONCEPT),
         "--ids", "CON-01", "--out", str(out)]
    )
    assert code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["schema"] == "assist_qa_runs/v1"
    assert payload["run_config"]["corpus_hash"] == "n/a (assist domain)"
    assert payload["run_config"]["tasks_sha256"]
    snapshot = Path(payload["run_config"]["tasks_file"])
    assert snapshot.exists()
    assert payload["results"][0]["task_id"] == "CON-01"


def test_fixture_documents_cover_demo_company():
    assert DEMO_DOCUMENTS[0]["company"] == "甲集团"
    assert DEMO_DOCUMENTS[0]["period"] == "2025"


@pytest.mark.asyncio
async def test_advice_task_calls_advice_entry(monkeypatch):
    async def fake_answer_advice(**kwargs):
        return SimpleNamespace(
            status="answered",
            answer="这是我的观点，主要风险是需求波动。\n\n" + DISCLAIMER_TEXT,
            notes=["advice:opinion"],
            refusal=None,
            resolved_query="甲集团值得投资吗？",
            disclaimer=DISCLAIMER_TEXT,
            advisory={"kind": "opinion", "requires_disclaimer": True,
                      "disclaimer_version": "test"},
            token_usage={"prompt_tokens": 10, "completion_tokens": 20, "llm_calls": 1},
        )

    monkeypatch.setattr(run_assist_eval, "answer_advice", fake_answer_advice)
    task = {
        "id": "ADV-01", "suite": "advice", "category": "buy_sell",
        "question": "甲集团值得投资吗？", "expected_status": "answered",
        "expected_refusal_code": None, "key_facts": [], "forbidden": ["稳赚"],
        "allow_web": False, "must_web": False, "sources_required": False,
        "holdout": False, "difficulty": "medium", "tags": [],
    }
    result = await run_assist_eval.run_advice_task(task, 1)
    assert result["status"] == "answered"
    assert result["route_reason"] == "opinion_intent"
    assert result["advisory"]["requires_disclaimer"] is True
    assert DISCLAIMER_TEXT in result["answer"]


@pytest.mark.asyncio
async def test_factual_question_does_not_enter_advice_path(monkeypatch):
    async def fake_answer_advice(**kwargs):  # pragma: no cover - 不应被调用
        raise AssertionError("披露题不应走观点路径")

    monkeypatch.setattr(run_assist_eval, "answer_advice", fake_answer_advice)
    task = {
        "id": "ADV-CTRL", "suite": "advice", "category": "control",
        "question": "甲集团 2025 年营业收入是多少？", "expected_status": "answered",
        "expected_refusal_code": None, "key_facts": [], "forbidden": ["x"],
        "allow_web": False, "must_web": False, "sources_required": False,
        "holdout": False, "difficulty": "easy", "tags": [],
    }
    result = await run_assist_eval.run_advice_task(task, 1)
    assert result["route_reason"] != "opinion_intent"
    assert result["disclaimer"] == ""
