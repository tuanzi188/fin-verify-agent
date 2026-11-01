"""失败自动归类测试：八类判据与主因/共现分布。"""

from evaluation.failure_taxonomy import classify, distribution


CHUNKS = {
    "doc#t0001": {
        "chunk_id": "doc#t0001",
        "doc_id": "cninfo-000333-2025-annual",
        "title": "美的集团 2025 年报｜主要会计数据",
        "content": "美的集团 营业收入",
    },
    "doc#t0002": {
        "chunk_id": "doc#t0002",
        "doc_id": "cninfo-002594-2025-annual",
        "title": "比亚迪 2025 年报｜主要会计数据",
        "content": "比亚迪 营业收入",
    },
}


def make_task(**overrides):
    task = {
        "id": "T",
        "question": "美的集团 2025 年营业收入是多少？",
        "should_abstain": False,
        "max_steps": 16,
        "mock": False,
        "answer_points": [
            {"text": "营业收入", "chunk_ids": ["doc#t0001"], "evidence": {"table_id": "doc#t0001"}}
        ],
    }
    task.update(overrides)
    return task


def make_result(trace=None, **overrides):
    result = {
        "task_id": "T",
        "status": "answered",
        "answer": "456,451,731 千元",
        "citations": [],
        "trace": trace or [],
        "usage": {"steps": len(trace or [])},
    }
    result.update(overrides)
    return result


def test_model_error_from_crash():
    row = classify(make_task(), make_result(status="error"), {}, chunks=CHUNKS)
    assert row["primary"] == "模型异常"


def test_retrieval_miss():
    retrieval = {
        "primary_k": 5,
        "first_search": {"5": {"point_total": 2, "point_hits": 0}},
        "missed_points": [0, 1],
        "searches": 1,
    }
    row = classify(make_task(), make_result(), {}, retrieval=retrieval, chunks=CHUNKS)
    assert "检索无命中" in row["categories"]


def test_tool_argument_error():
    trace = [
        {"step": 1, "tool": "get_table", "args": {}, "ok": False, "error_kind": "bad_args", "result_ids": []}
    ]
    row = classify(make_task(), make_result(trace), {}, chunks=CHUNKS)
    assert "工具/参数错误" in row["categories"]


def test_invalid_loop_from_repeated_calls():
    trace = [
        {"step": index, "tool": "get_table", "args": {"table_id": "doc#t0001"}, "ok": True, "result_ids": ["doc#t0001"]}
        for index in range(1, 4)
    ]
    row = classify(make_task(), make_result(trace), {}, chunks=CHUNKS)
    assert "无效循环" in row["categories"]


def test_false_refusal_requires_retrieval_hit():
    retrieval = {
        "primary_k": 5,
        "first_search": {"5": {"point_total": 1, "point_hits": 1}},
        "missed_points": [],
        "searches": 1,
    }
    checks = {"false_refusal": {"ok": False}}
    row = classify(
        make_task(),
        make_result(status="abstained"),
        checks,
        retrieval=retrieval,
        chunks=CHUNKS,
    )
    assert "过早结束/误拒" in row["categories"]


def test_context_pollution_and_scope_violation():
    trace = [
        {"step": 1, "tool": "get_table", "args": {"table_id": "doc#t0002"}, "ok": True, "result_ids": ["doc#t0002"]}
    ]
    result = make_result(
        trace,
        citations=[{"marker": 1, "chunk_id": "doc#t0002"}],
    )
    row = classify(make_task(), result, {}, chunks=CHUNKS)
    assert "上下文污染" in row["categories"]
    assert "权限越界" in row["categories"]


def test_allowed_tools_violation():
    trace = [
        {"step": 1, "tool": "calculate", "args": {"expression": "1+1"}, "ok": True, "result_ids": []}
    ]
    task = make_task(allowed_tools=["search_blocks", "read_block"])
    row = classify(task, make_result(trace), {}, chunks=CHUNKS)
    assert "权限越界" in row["categories"]


def test_relative_period_read_is_not_scope_violation():
    task = make_task(question="美的集团 2025 年营业收入比上年增减多少？")
    trace = [
        {"step": 1, "tool": "get_table", "args": {"table_id": "doc#t0003"}, "ok": True, "result_ids": ["doc#t0003"]}
    ]
    chunks = {
        "doc#t0003": {
            "chunk_id": "doc#t0003",
            "title": "美的集团 2024 年报｜主要会计数据",
            "doc_id": "cninfo-000333-2024-annual",
        }
    }
    row = classify(task, make_result(trace), {}, chunks=chunks)
    assert "权限越界" not in row["categories"]
    assert "上下文污染" not in row["categories"]


def test_distribution_separates_primary_and_cooccurrence():
    rows = [
        {"outcome": "fail", "taxonomy": {"primary": "检索无命中", "categories": ["检索无命中", "过早结束/误拒"]}},
        {"outcome": "fail", "taxonomy": {"primary": "过早结束/误拒", "categories": ["过早结束/误拒"]}},
    ]
    summary = distribution(rows)
    assert summary["primary"] == {"检索无命中": 1, "过早结束/误拒": 1}
    assert summary["cooccurrence"] == {"检索无命中": 1, "过早结束/误拒": 2}
