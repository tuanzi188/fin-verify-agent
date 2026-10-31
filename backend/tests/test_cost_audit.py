"""成本归因脚本的离线测试：不调用模型，只验证拆解口径。"""

from evaluation import audit_run_cost


def _step(tool, args, *, result_chars=100, payload_chars=120, duplicate_skipped=0):
    return {
        "tool": tool,
        "args": args,
        "result_chars": result_chars,
        "payload_chars": payload_chars,
        "evidence_chars_added": result_chars,
        "duplicate_skipped": duplicate_skipped,
        "ok": True,
    }


def test_legacy_run_still_reports_tokens_and_repeated_tables():
    result = {
        "task_id": "T1",
        "status": "answered",
        "usage": {"tokens_in": 900, "tokens_out": 100, "steps": 2},
        "trace": [
            _step("get_rows", {"table_id": "doc#t1", "rows": [0, 1]}),
            _step("get_rows", {"table_id": "doc#t1", "rows": [2, 3]}),
        ],
    }
    row = audit_run_cost._summarize(result)

    assert row["total_tokens"] == 1000
    assert row["llm"]["available"] is False
    assert row["duplicate_read_groups"] == []
    assert row["repeated_tables"] == [{"table_id": "doc#t1", "reads": 2}]
    assert row["tools"]["get_rows"]["calls"] == 2


def test_new_run_splits_phase_tokens_and_context():
    result = {
        "task_id": "T2",
        "status": "answered",
        "usage": {"tokens_in": 1500, "tokens_out": 300, "steps": 1},
        "llm_calls": [
            {"phase": "tool_round", "tokens_in": 1000, "tokens_out": 100,
             "messages": 2, "messages_chars": 4000},
            {"phase": "fallback_final", "tokens_in": 500, "tokens_out": 200,
             "messages": 4, "messages_chars": 9000},
        ],
        "trace": [
            _step("get_cell", {"table_id": "doc#t1", "row": 0, "column": 1}),
        ],
    }
    row = audit_run_cost._summarize(result)

    assert row["llm"]["available"] is True
    assert row["llm"]["phases"]["tool_round"]["tokens_in"] == 1000
    assert row["llm"]["phases"]["fallback_final"]["share"] == round(700 / 1800, 4)
    assert row["final_context_tokens"] == 500
    assert row["max_messages_chars"] == 9000


def test_aggregate_sums_tools_and_phases():
    rows = [
        audit_run_cost._summarize({
            "task_id": "A",
            "status": "answered",
            "usage": {"tokens_in": 100, "tokens_out": 50, "steps": 1},
            "llm_calls": [
                {"phase": "tool_round", "tokens_in": 100, "tokens_out": 50,
                 "messages": 2, "messages_chars": 100},
            ],
            "trace": [_step("search_blocks", {"query": "x"})],
        }),
        audit_run_cost._summarize({
            "task_id": "B",
            "status": "answered",
            "usage": {"tokens_in": 200, "tokens_out": 100, "steps": 1},
            "llm_calls": [
                {"phase": "rag_generate", "tokens_in": 200, "tokens_out": 100,
                 "messages": 2, "messages_chars": 200},
            ],
            "trace": [_step("search_blocks", {"query": "y"})],
        }),
    ]
    aggregate = audit_run_cost._aggregate(rows)

    assert aggregate["runs"] == 2
    assert aggregate["total_tokens"] == 450
    assert aggregate["phase_totals"]["tool_round"]["share"] == round(150 / 450, 4)
    assert aggregate["tool_totals"]["search_blocks"]["calls"] == 2
