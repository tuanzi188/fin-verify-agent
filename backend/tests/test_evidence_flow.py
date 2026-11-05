"""证据流分析测试：实体归一、转化率、重复读取、聚合。全部离线。"""

from evaluation.evidence_flow import (
    aggregate,
    analyze_payload,
    analyze_run,
    entity_key,
)


def step(index, tool, ids=None, evidence_chars=0, ok=True):
    return {
        "step": index,
        "tool": tool,
        "args": {},
        "ok": ok,
        "result_ids": ids or [],
        "evidence_chars_added": evidence_chars,
    }


def test_entity_key_normalizes_to_table_or_section_level():
    assert entity_key("doc#t0006#rows#0#cols#0,1") == "doc#t0006"
    assert entity_key("doc#t0006#0#2025 年") == "doc#t0006"
    assert entity_key("doc#t0006") == "doc#t0006"
    assert entity_key("doc#s0016#b000") == "doc#s0016"
    assert entity_key("calc-1") == "calc-1"
    assert entity_key("") == ""
    assert entity_key(None) == ""


def test_analyze_run_counts_locator_to_reader_conversion():
    result = {
        "task_id": "T",
        "trace": [
            step(1, "search_tables", ["doc#t1", "doc#t2"]),
            step(2, "get_rows", ["doc#t1#rows#0#cols#0,1"], evidence_chars=100),
            step(3, "__final__"),
        ],
        "citations": [{"marker": 1, "chunk_id": "doc#t1#0#2025 年"}],
    }
    row = analyze_run(result)
    assert row["steps"] == 2  # __final__ 不算工具步
    assert row["located_entities"] == 2
    assert row["read_entities"] == 1
    assert row["cited_entities"] == 1
    assert row["locator_to_reader_rate"] == 0.5
    assert row["read_evidence_rate"] == 1.0
    assert row["uncited_read_rate"] == 0.0
    assert row["first_locator_hit"] is True
    assert row["first_read_hit"] is True
    assert row["redundant_read_steps"] == 0


def test_analyze_run_without_reader_returns_none_rates():
    result = {
        "task_id": "T",
        "trace": [step(1, "search_tables", ["doc#t1", "doc#t2"]), step(2, "__final__")],
        "citations": [],
    }
    row = analyze_run(result)
    assert row["reader_calls"] == 0
    assert row["locator_to_reader_rate"] == 0.0
    assert row["read_evidence_rate"] is None
    assert row["uncited_read_rate"] is None
    assert row["first_read_hit"] is None
    assert row["first_locator_hit"] is False


def test_repeated_read_without_new_evidence_is_redundant():
    result = {
        "task_id": "T",
        "trace": [
            step(1, "get_rows", ["doc#t1#rows#0#cols#0"], evidence_chars=100),
            step(2, "get_cell", ["doc#t1#0#2025 年"], evidence_chars=0),
        ],
        "citations": [{"marker": 1, "chunk_id": "doc#t1#0#2025 年"}],
    }
    row = analyze_run(result)
    assert row["redundant_read_steps"] == 1
    assert row["read_evidence_rate"] == 0.5


def test_repeated_read_that_adds_evidence_is_not_redundant():
    result = {
        "task_id": "T",
        "trace": [
            step(1, "get_rows", ["doc#t1#rows#0#cols#0"], evidence_chars=100),
            step(2, "get_cell", ["doc#t1#0#2025 年"], evidence_chars=50),
        ],
        "citations": [{"marker": 1, "chunk_id": "doc#t1#0#2025 年"}],
    }
    row = analyze_run(result)
    assert row["redundant_read_steps"] == 0


def test_missing_citations_field_is_not_scored_as_waste():
    result = {
        "task_id": "T",
        "trace": [step(1, "get_rows", ["doc#t1#rows#0#cols#0"], evidence_chars=100)],
    }
    row = analyze_run(result)
    assert row["uncited_read_rate"] is None
    assert row["first_read_hit"] is None


def test_failed_first_read_counts_as_miss_not_unknown():
    """第一次读取失败（result_ids 为空）应记 False：它确实没读到东西。"""
    result = {
        "task_id": "T",
        "trace": [
            step(1, "search_tables", ["doc#t1"]),
            step(2, "get_rows", [], ok=False),
            step(3, "get_rows", ["doc#t1#rows#0#cols#0"], evidence_chars=100),
        ],
        "citations": [{"marker": 1, "chunk_id": "doc#t1#0#2025 年"}],
    }
    row = analyze_run(result)
    assert row["first_read_hit"] is False
    assert row["failed_calls"] == 1
    assert row["failure_recovered"] is True


def test_failure_without_recovery():
    result = {
        "task_id": "T",
        "trace": [
            step(1, "search_tables", ["doc#t1"]),
            step(2, "get_rows", [], ok=False),
            step(3, "get_rows", [], ok=False),
        ],
        "citations": [],
    }
    row = analyze_run(result)
    assert row["failed_calls"] == 2
    assert row["failure_recovered"] is False


def test_no_failures_means_recovery_is_unknown():
    result = {
        "task_id": "T",
        "trace": [step(1, "get_rows", ["doc#t1#rows#0#cols#0"], evidence_chars=1)],
        "citations": [{"marker": 1, "chunk_id": "doc#t1#0#x"}],
    }
    row = analyze_run(result)
    assert row["failed_calls"] == 0
    assert row["failure_recovered"] is None


def test_uncited_read_rate_flags_reads_that_never_reach_the_answer():
    result = {
        "task_id": "T",
        "trace": [
            step(1, "get_rows", ["doc#t1#rows#0#cols#0"], evidence_chars=100),
            step(2, "get_table", ["doc#t2"], evidence_chars=200),
        ],
        "citations": [{"marker": 1, "chunk_id": "doc#t1#0#2025 年"}],
    }
    row = analyze_run(result)
    assert row["read_entities"] == 2
    assert row["uncited_read_rate"] == 0.5


def test_aggregate_sums_counts_and_divides_entities():
    runs = [
        {
            "task_id": "A",
            "trace": [
                step(1, "search_tables", ["doc#t1", "doc#t2"]),
                step(2, "get_rows", ["doc#t1#rows#0#cols#0"], evidence_chars=10),
            ],
            "citations": [{"marker": 1, "chunk_id": "doc#t1#0#2025 年"}],
        },
        {
            "task_id": "B",
            "trace": [step(1, "get_rows", ["doc#t3#rows#0#cols#0"], evidence_chars=10)],
            "citations": [{"marker": 1, "chunk_id": "doc#t3#0#2025 年"}],
        },
    ]
    cross = aggregate([analyze_run(run) for run in runs])
    assert cross["runs"] == 2
    assert cross["located_entities_total"] == 2
    assert cross["read_entities_total"] == 2
    assert cross["locator_to_reader_rate"] == 0.5
    assert cross["read_evidence_rate"] == 1.0
    assert cross["first_locator_hit_rate"] == 1.0


def test_aggregate_uncited_rate_skips_runs_without_citations_field():
    """缺 citations 字段的旧产物不能进"读了没引"的分母，否则会被算成全浪费。"""
    with_citations = {
        "task_id": "A",
        "trace": [step(1, "get_rows", ["doc#t1#rows#0#cols#0"], evidence_chars=1)],
        "citations": [{"marker": 1, "chunk_id": "doc#t1#0#x"}],
    }
    without_citations = {
        "task_id": "B",
        "trace": [step(1, "get_rows", ["doc#t2#rows#0#cols#0"], evidence_chars=1)],
    }
    cross = aggregate([analyze_run(r) for r in (with_citations, without_citations)])
    assert cross["uncited_read_entities_total"] == 0
    assert cross["uncited_read_rate"] == 0.0


def test_analyze_payload_accepts_list_and_dict():
    run = {
        "task_id": "T",
        "trace": [step(1, "get_rows", ["doc#t1#rows#0#cols#0"], evidence_chars=1)],
        "citations": [{"marker": 1, "chunk_id": "doc#t1#0#x"}],
    }
    assert analyze_payload([run])["cross"]["runs"] == 1
    assert analyze_payload({"results": [run]})["cross"]["runs"] == 1
    assert analyze_payload({"results": []})["cross"] == {
        "runs": 0,
        "note": "没有可分析的运行",
    }
