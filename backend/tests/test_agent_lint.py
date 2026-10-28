"""任务集 lint 测试。

lint 现在是承重件（文档 §9.4 把它列为阶段验收项），所以它自己也要被测。
每个用例只构造一处缺陷，断言"能被抓到"，避免 lint 变成永远返回 0 error 的摆设。

注意：澄清配比是**全局检查**，所以用例都基于一个"本来合法"的 6 条集合，
再只改一处 —— 单条任务永远过不了配比检查，那样测出来的只是噪声。
"""

from evaluation.lint_agent_tasks import lint

CHUNKS = {
    "d1_ch1": {"chunk_id": "d1_ch1", "content": "x"},
    "d1_ch2": {"chunk_id": "d1_ch2", "content": "y"},
}


def make_task(**overrides) -> dict:
    task = {
        "id": "T1",
        "dimension": "检索与引用",
        "secondary_tags": [],
        "difficulty": "easy",
        "question": "问题？",
        "tool_constraints": {"must_include": ["search_kb"], "call_count": {}, "order": [], "must_not_include": []},
        "required_args": ["x"],
        "answer_points": [{"text": "事实一", "chunk_ids": ["d1_ch1"]}],
        "citation_required": True,
        "should_abstain": False,
        "clarify_mode": "none",
        "forbidden": ["编造"],
        "max_steps": 3,
        "relevant_chunk_ids": ["d1_ch1"],
        "query_type": "精确查找",
        "source": "synthetic",
        "mock": False,
        "_line": 1,
    }
    task.update(overrides)
    return task


def make_valid_set(**overrides) -> list[dict]:
    """一个满足澄清配比的最小集合：ask 2、none 3、assume 1。"""
    modes = ["ask", "ask", "none", "none", "assume", "none"]
    tasks = [
        make_task(id=f"T{index}", dimension="澄清与追问", clarify_mode=mode, citation_required=False)
        for index, mode in enumerate(modes)
    ]
    if overrides:
        base = {"id": "T0", "dimension": "澄清与追问", "clarify_mode": "ask", "citation_required": False}
        base.update(overrides)
        tasks[0] = make_task(**base)
    return tasks


def errors_of(tasks) -> list[str]:
    errors, _ = lint(tasks, CHUNKS)
    return errors


def warnings_of(tasks) -> list[str]:
    _, warnings = lint(tasks, CHUNKS)
    return warnings


def test_valid_set_has_no_errors():
    assert errors_of(make_valid_set()) == []


def test_bundled_answer_point_is_rejected():
    """一个要点里塞两个事实 → judge 会翻脸，lint 必须拦住。"""
    tasks = make_valid_set(answer_points=[
        {"text": "离开工位超过十分钟须锁屏，办公电脑需设置自动锁屏", "chunk_ids": ["d1_ch1"]},
    ])
    errors = errors_of(tasks)
    assert any("句读分隔" in error for error in errors)
    assert any("一个要点只能是一个事实" in error for error in errors)


def test_missing_chunk_reference_is_error():
    tasks = make_valid_set(relevant_chunk_ids=["d9_ch9"], answer_points=[
        {"text": "事实", "chunk_ids": ["d9_ch9"]},
    ])
    assert any("chunk 不存在" in error for error in errors_of(tasks))


def test_answer_point_without_chunk_is_error():
    tasks = make_valid_set(answer_points=[{"text": "事实", "chunk_ids": []}], relevant_chunk_ids=[])
    assert any("没有 chunk_ids" in error for error in errors_of(tasks))


def test_reverse_coverage_violation():
    """摆设 chunk：relevant 里有，但没有任何要点引用它。"""
    tasks = make_valid_set(
        answer_points=[{"text": "事实", "chunk_ids": ["d1_ch1"]}],
        relevant_chunk_ids=["d1_ch1", "d1_ch2"],
    )
    errors = errors_of(tasks)
    assert any("反向覆盖失败" in error for error in errors)
    assert any("锚定不一致" in error for error in errors)


def test_negative_task_must_be_clean():
    tasks = make_valid_set(
        dimension="拒答与边界",
        should_abstain=True,
        citation_required=False,
    )
    errors = errors_of(tasks)
    assert any("负样本不应有 answer_points" in error for error in errors)
    assert any("负样本不应有 relevant_chunk_ids" in error for error in errors)


def test_mock_must_pair_with_inject():
    assert any("mock=true 但没有 inject" in error for error in errors_of(make_valid_set(mock=True)))


def test_inject_without_mock_is_error():
    tasks = make_valid_set(inject=[{"call_index": 1, "mode": "timeout"}])
    assert any("有注入计划但 mock 不为 true" in error for error in errors_of(tasks))


def test_mock_only_allowed_in_recovery_dimension():
    tasks = make_valid_set(
        dimension="检索与引用",
        mock=True,
        inject=[{"call_index": 1, "mode": "timeout"}],
    )
    assert any("只用于恢复与纠错维度" in error for error in errors_of(tasks))


def test_illegal_injection_mode_and_call_index():
    tasks = make_valid_set(
        dimension="恢复与纠错",
        mock=True,
        inject=[{"call_index": 0, "mode": "explode"}],
    )
    errors = errors_of(tasks)
    assert any("mode 非法" in error for error in errors)
    assert any("call_index 必须是 ≥1 的整数" in error for error in errors)


def test_clarify_balance_requires_all_three_modes():
    only_ask = [
        make_task(id=f"T{i}", dimension="澄清与追问", clarify_mode="ask") for i in range(3)
    ]
    errors = errors_of(only_ask)
    assert any("反向对照不足" in error for error in errors)
    assert any("缺少 clarify_mode=assume" in error for error in errors)


def test_duplicate_id_is_error():
    tasks = make_valid_set()
    tasks.append(make_task(id="T0"))
    assert any("id 重复" in error for error in errors_of(tasks))


def test_empty_forbidden_is_error():
    assert any("forbidden 为空" in error for error in errors_of(make_valid_set(forbidden=[])))


def test_dimension_count_and_total_are_warnings_not_errors():
    tasks = make_valid_set()
    assert errors_of(tasks) == []
    warnings = warnings_of(tasks)
    assert any("维度条数不是 6" in warning for warning in warnings)
    assert any("任务总数 6" in warning for warning in warnings)
