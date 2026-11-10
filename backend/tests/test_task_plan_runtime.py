"""任务清单与线上服务入口的离线回归，模型替身不得访问网络。"""

import json
from unittest.mock import AsyncMock

import pytest

from app.core.user_context import UserContext
from app.finance.store import FinanceStore
from app.finance.task_plan import FinanceTaskPlan
from app.services import finance_entry
from app.tools.finance_set import FinanceToolset
from evaluation.agent_checks import collect_checks


@pytest.fixture
def planning_store(tmp_path):
    for doc_id, company, revenue in (("a", "美的集团", "100"), ("b", "比亚迪", "200")):
        destination = tmp_path / doc_id
        destination.mkdir()
        document = {
            "doc_id": doc_id,
            "company": company,
            "title": company + "2025年报",
            "period": "2025",
            "unit": "万元",
            "doc_type": "annual_report",
            "sections": [],
            "tables": [
                {
                    "table_id": doc_id + "#t1",
                    "caption": "合并利润表",
                    "page": 1,
                    "unit": "万元",
                    "statement_scope": "consolidated",
                    "columns": ["项目", "2025年", "2024年"],
                    "rows": [
                        ["营业收入", revenue, "90"],
                        ["净利润", "20", "10"],
                        ["研发费用", "30", "25"],
                    ],
                }
            ],
        }
        (destination / "document.json").write_text(
            json.dumps(document), encoding="utf-8"
        )
    return FinanceStore(tmp_path)


def planning_evidence(anchor, **extra):
    return {"chunk_id": anchor, "kind": "rows", **extra}


def test_plan_tracks_each_company_period_and_metric(planning_store):
    plan = FinanceTaskPlan(
        "美的集团和比亚迪2024年及2025年的营业收入与净利润分别是多少",
        planning_store.list_documents(),
    )
    assert len(plan.items) == 8
    plan.refresh([planning_evidence("a#t1#rows#0,1#cols#1,2")], [], planning_store)
    assert sum(item.status == "evidence_found" for item in plan.items) == 4
    assert plan.snapshot()["status"] == "incomplete"
    assert "比亚迪" in plan.guidance()


def test_plan_rejects_wrong_period_and_related_metric(planning_store):
    plan = FinanceTaskPlan(
        "美的集团2025年营业收入及研发投入是多少", planning_store.list_documents()
    )
    plan.refresh([planning_evidence("a#t1#rows#0,2#cols#2")], [], planning_store)
    assert all(item.status == "pending" for item in plan.items)


def test_plan_preserves_explicit_statement_scope(planning_store):
    plan = FinanceTaskPlan(
        "美的集团母公司2025年营业收入是多少", planning_store.list_documents()
    )
    plan.refresh([planning_evidence("a#t1#rows#0#cols#1")], [], planning_store)
    assert plan.items[0].status == "pending"


@pytest.mark.parametrize("flag", ["truncated", "misaligned"])
def test_plan_does_not_complete_from_unusable_evidence(planning_store, flag):
    plan = FinanceTaskPlan(
        "美的集团2025年营业收入是多少", planning_store.list_documents()
    )
    plan.refresh(
        [planning_evidence("a#t1#rows#0#cols#1", **{flag: True})], [], planning_store
    )
    assert plan.items[0].status == "pending"


def test_plan_does_not_count_search_locators(planning_store):
    plan = FinanceTaskPlan(
        "美的集团2025年营业收入是多少", planning_store.list_documents()
    )
    plan.refresh([planning_evidence("a#t1", citable=False)], [], planning_store)
    assert plan.items[0].status == "pending"


def test_plan_stops_repeated_calls_without_new_information(planning_store):
    plan = FinanceTaskPlan(
        "美的集团2025年营业收入是多少", planning_store.list_documents()
    )
    for _ in range(3):
        plan.observe_progress({"ok": True, "new_result_count": 0}, [])
    assert plan.stop_reason == "no_progress"
    assert "停止重复" in plan.guidance()


def test_new_locators_reset_no_progress_counter(planning_store):
    plan = FinanceTaskPlan(
        "美的集团2025年营业收入是多少", planning_store.list_documents()
    )
    plan.observe_progress({"ok": False}, [])
    plan.observe_progress({"ok": True, "new_result_count": 1}, [])
    assert plan.no_progress_steps == 0


def test_prepare_clears_previous_request_plan(planning_store):
    tools = FinanceToolset(planning_store)
    tools.start_task_plan("美的集团2025年营业收入是多少")
    tools.prepare()
    assert tools.task_plan is None


def test_unknown_metrics_are_not_claimed_complete(planning_store):
    plan = FinanceTaskPlan(
        "美的集团2025年主要业务风险是什么", planning_store.list_documents()
    )
    assert plan.snapshot()["status"] == "untracked"


def test_plan_calculation_requirement_is_separate_from_reading(planning_store):
    plan = FinanceTaskPlan(
        "美的集团2025年营业收入比上年增加多少，请自行计算",
        planning_store.list_documents(),
    )
    plan.refresh([planning_evidence("a#t1#rows#0#cols#1,2")], [], planning_store)
    assert all(item.status == "evidence_found" for item in plan.items)
    assert plan.snapshot()["status"] == "incomplete"


@pytest.mark.asyncio
@pytest.mark.parametrize("complete", [False, True])
async def test_live_entry_enforces_plan_and_persists_verification(
    planning_store, monkeypatch, complete
):
    rounds = [
        (
            "",
            [
                {
                    "id": "read-a",
                    "name": "get_rows",
                    "arguments": {
                        "table_id": "a#t1",
                        "rows": [0],
                        "columns": [1],
                    },
                }
            ],
            None,
            {},
        ),
        ("美的集团2025年营业收入为100万元[1]。", [], None, {}),
    ]
    if complete:
        rounds.extend(
            [
                (
                    "",
                    [
                        {
                            "id": "read-b",
                            "name": "get_rows",
                            "arguments": {
                                "table_id": "b#t1",
                                "rows": [0],
                                "columns": [1],
                            },
                        }
                    ],
                    None,
                    {},
                ),
                (
                    "美的集团2025年营业收入为100万元[1]。\n比亚迪2025年营业收入为200万元[2]。",
                    [],
                    None,
                    {},
                ),
            ]
        )
    else:
        rounds.append(rounds[-1])
    monkeypatch.setattr(finance_entry, "get_finance_store", lambda: planning_store)
    monkeypatch.setattr(finance_entry, "get_block_index", lambda: object())
    monkeypatch.setattr(
        "app.services.model_provider.chat_with_tools", AsyncMock(side_effect=rounds)
    )
    monkeypatch.setattr(
        "app.services.model_provider.chat",
        AsyncMock(
            return_value=(
                '{"covered":true,"missing":[],"next_query":""}',
                None,
                {},
            )
        ),
    )
    memory = AsyncMock()
    monkeypatch.setattr(finance_entry, "conversation_memory", memory)
    response = await finance_entry.run_finance_query(
        query="分别列出美的集团和比亚迪2025年的营业收入",
        conversation_id="plan-test",
        user_context=UserContext(user_id="plan-test"),
    )
    assert response.status == ("answered" if complete else "needs_review")
    assert response.verification["task_plan"]["status"] == (
        "covered" if complete else "incomplete"
    )
    assert "coverage_repair" in str(response.trace)
    assert (
        memory.add_turn.call_args.kwargs["payload"]["verification"]["task_plan"]
        == response.verification["task_plan"]
    )


def test_evaluation_does_not_pass_an_incomplete_answer():
    checks = collect_checks(
        {"id": "plan-test"},
        {
            "status": "answered",
            "answer": "部分答案",
            "trace": [],
            "citations": [],
            "task_plan": {"status": "incomplete"},
        },
    )
    assert checks["task_plan_complete"]["ok"] is False


@pytest.mark.parametrize(
    "alternate_value,alternate_unit,expected_status",
    [
        ("99", "万元", "conflict"),
        ("1000000", "元", "evidence_found"),
    ],
)
def test_plan_detects_conflicts_after_unit_normalization(
    planning_store,
    tmp_path,
    alternate_value,
    alternate_unit,
    expected_status,
):
    document_path = tmp_path / "a" / "document.json"
    document = json.loads(document_path.read_text(encoding="utf-8"))
    second_table = {
        **document["tables"][0],
        "table_id": "a#t2",
        "unit": alternate_unit,
        "rows": [["营业收入", alternate_value, "90"]],
    }
    document["tables"].append(second_table)
    document_path.write_text(json.dumps(document), encoding="utf-8")
    conflict_store = FinanceStore(tmp_path)
    plan = FinanceTaskPlan(
        "美的集团2025年营业收入是多少", conflict_store.list_documents()
    )
    plan.refresh(
        [
            planning_evidence("a#t1#rows#0#cols#1"),
            planning_evidence("a#t2#rows#0#cols#1"),
        ],
        [],
        conflict_store,
    )
    assert plan.items[0].status == expected_status


@pytest.mark.asyncio
async def test_live_loop_stops_no_progress_and_keeps_missing_items(
    planning_store, monkeypatch
):
    repeated_tools = AsyncMock(
        return_value=(
            "",
            [
                {
                    "id": "same-read",
                    "name": "get_rows",
                    "arguments": {
                        "table_id": "a#t1",
                        "rows": [0],
                        "columns": [1],
                    },
                }
            ],
            None,
            {},
        )
    )
    monkeypatch.setattr(finance_entry, "get_finance_store", lambda: planning_store)
    monkeypatch.setattr(finance_entry, "get_block_index", lambda: object())
    monkeypatch.setattr("app.services.model_provider.chat_with_tools", repeated_tools)
    monkeypatch.setattr(
        "app.services.model_provider.chat",
        AsyncMock(
            return_value=(
                "美的集团2025年营业收入为100万元[1]。",
                None,
                {},
            )
        ),
    )
    response = await finance_entry.run_finance_query(
        query="分别列出美的集团和比亚迪2025年的营业收入",
        conversation_id="no-progress-test",
        user_context=UserContext(),
        persist=False,
    )
    assert repeated_tools.await_count == 4
    assert response.verification["task_plan"]["stop_reason"] == "no_progress"
    assert response.status == "needs_review"
