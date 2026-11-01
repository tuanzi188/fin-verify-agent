"""检索段指标测试：位置折叠、单题打分、双臂口径、零结果检索。"""

from evaluation.retrieval_metrics import (
    aggregate,
    base_position,
    gold_points,
    hits_to_result,
    score_offline,
    score_task,
)


def make_task():
    return {
        "id": "T1",
        "dimension": "检索与引用",
        "complex_type": "cross_table",
        "answer_points": [
            {
                "text": "A",
                "chunk_ids": ["doc#t0001"],
                "evidence": {"table_id": "doc#t0001", "value": "1"},
            },
            {
                "text": "B",
                "chunk_ids": ["doc#t0002"],
                "evidence": {"table_id": "doc#t0002", "value": "2"},
            },
        ],
        "relevant_chunk_ids": ["doc#t0001", "doc#t0002"],
    }


def make_result(trace=None):
    return {"task_id": "T1", "status": "answered", "trace": trace or [], "evidence": []}


def test_base_position_folds_every_anchor_format():
    assert base_position("doc#t0013") == "doc#t0013"
    assert base_position("doc#s0009#b000") == "doc#s0009"
    assert base_position("doc#t0013#c0001") == "doc#t0013"
    assert base_position("doc#t0013#rows#0,1#cols#0,1") == "doc#t0013"
    assert base_position("doc#t0013#营业收入#2025 年") == "doc#t0013"


def test_gold_points_prefer_evidence_and_skip_note_only():
    task = {
        "id": "T",
        "answer_points": [
            {"text": "fact", "evidence": {"table_id": "doc#t1"}, "chunk_ids": ["doc#t1"]},
            {"text": "note", "kind": "note"},
            {"text": "derived", "derived": True, "chunk_ids": ["doc#t2"]},
        ],
    }
    points = gold_points(task)
    assert [point["index"] for point in points] == [0, 2]
    assert points[0]["positions"] == ["doc#t1"]
    assert points[1]["positions"] == ["doc#t2"]


def test_single_search_metrics_and_read_hit():
    trace = [
        {
            "step": 1,
            "tool": "search_blocks",
            "args": {"query": "q"},
            "ok": True,
            "result_ids": ["doc#t0002#rows#0#cols#0", "doc#t0001#rows#0#cols#0"],
            "result_count": 2,
        },
        {
            "step": 2,
            "tool": "read_block",
            "args": {"block_id": "doc#t0001#rows#0#cols#0"},
            "ok": True,
            "result_ids": ["doc#t0001#rows#0#cols#0"],
            "result_count": 1,
        },
    ]
    row = score_task(make_task(), make_result(trace))

    at1 = row["first_search"]["1"]
    assert at1["point_hits"] == 1
    assert at1["point_total"] == 2
    assert at1["task_hit_all"] is False
    assert at1["task_hit_any"] is True
    assert at1["mrr"] == 0.75
    assert at1["context_precision"] == 1.0

    at5 = row["first_search"]["5"]
    assert at5["point_hits"] == 2
    assert at5["task_hit_all"] is True
    assert at5["context_precision"] == 1.0

    assert row["read_points"] == 1
    assert row["read_rate"] == 0.5
    assert row["searches"] == 1


def test_any_search_uses_best_rank_but_first_search_stays_strict():
    trace = [
        {
            "step": 1,
            "tool": "search_blocks",
            "args": {"query": "first"},
            "ok": True,
            "result_ids": ["doc#t0002#rows#0#cols#0"],
            "result_count": 1,
        },
        {
            "step": 2,
            "tool": "search_blocks",
            "args": {"query": "second"},
            "ok": True,
            "result_ids": ["doc#t0001#rows#0#cols#0"],
            "result_count": 1,
        },
    ]
    row = score_task(make_task(), make_result(trace))
    assert row["first_search"]["1"]["point_hits"] == 1
    assert row["any_search"]["1"]["point_hits"] == 2
    assert row["first_search"]["1"]["mrr"] == 0.5
    assert row["any_search"]["1"]["mrr"] == 1.0


def test_zero_result_search_still_counts_as_a_search():
    trace = [
        {
            "step": 1,
            "tool": "search_blocks",
            "args": {"query": "empty"},
            "ok": True,
            "result_ids": [],
            "result_count": 0,
        }
    ]
    row = score_task(make_task(), make_result(trace))
    assert row["searches"] == 1
    assert row["first_search"]["5"]["point_hits"] == 0
    assert row["missed_points"] == [0, 1]


def test_fixed_window_chunk_expands_via_source_spans():
    task = {
        "id": "T",
        "answer_points": [
            {"text": "A", "chunk_ids": ["doc#t0001"], "evidence": {"table_id": "doc#t0001"}}
        ],
    }
    result = {
        "task_id": "T",
        "trace": [
            {
                "step": 1,
                "tool": "search_chunks",
                "args": {"query": "q"},
                "ok": True,
                "result_ids": ["doc#s0009#c0001"],
                "result_count": 1,
            }
        ],
        "evidence": [
            {
                "chunk_id": "doc#s0009#c0001",
                "content": "x",
                "source_spans": [
                    {"source_id": "doc#s0009", "page": 1},
                    {"source_id": "doc#t0001", "page": 2},
                ],
            }
        ],
    }
    row = score_task(task, result)
    assert row["first_search"]["1"]["point_hits"] == 1


def test_offline_scoring_with_fake_retriever():
    def retriever(question, top_k):
        return [{"chunk_id": "doc#t0001#c0001", "content": "x"}]

    rows = score_offline([make_task()], arm="fixed", retriever=retriever)
    assert len(rows) == 1
    assert rows[0]["first_search"]["1"]["point_hits"] == 1
    assert rows[0]["searches"] == 1


def test_hits_to_result_preserves_order():
    result = hits_to_result(
        [{"block_id": "doc#t2"}, {"block_id": "doc#t1"}], "search_blocks"
    )
    assert result["trace"][0]["result_ids"] == ["doc#t2", "doc#t1"]


def test_aggregate_reports_both_yardsticks():
    rows = [
        score_task(make_task(), make_result([])),
        score_task(make_task(), make_result([])),
    ]
    summary = aggregate(rows)
    assert summary["tasks"] == 2
    assert summary["first_search"]["5"]["point_hits"] == 0
    assert summary["any_search"]["5"]["point_hits"] == 0
    assert summary["read"]["points_total"] == 4
