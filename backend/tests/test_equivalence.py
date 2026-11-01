"""答案等价规则测试：单位换算、舍入、方向、四条原子判据。"""

from evaluation.equivalence import (
    direction_consistent,
    direction_of,
    numbers_in,
    point_equivalence,
    same_number,
    task_equivalence,
    value_in_text,
)


def test_numbers_in_handles_commas_percent_and_parentheses_negative():
    values = numbers_in("营业收入 456,451,731 千元，同比 12.11%，调整 (1,234) 元")
    assert [str(item["value"]) for item in values] == ["456451731", "12.11", "-1234"]
    assert values[0]["unit"] == "千元"
    assert values[1]["unit"] == "%"


def test_same_number_tolerance_separates_rounding_from_real_error():
    assert same_number(65.7084, 65.71)
    assert not same_number(9.08, 9.09)


def test_value_in_text_converts_money_units():
    result = value_in_text("456,451,731 千元", "约 4,564.52 亿元")
    assert result["match"] is True
    assert "金额换算" in result["reason"]


def test_value_in_text_does_not_convert_percent_to_fraction():
    assert value_in_text("29.90%", "0.299")["match"] is False


def test_value_in_text_matches_unitless_cell_against_percent():
    # 表格单元格 29.90 没带单位，答案写 29.90%：按原样数值应判等价
    assert value_in_text("29.90", "毛利率 29.90%")["match"] is True


def test_direction_detection():
    assert direction_of("同比下降 33.46%") == "down"
    assert direction_of("同比增长 12.11%") == "up"
    assert direction_of("基本持平") == "flat"
    assert direction_consistent("下降 33.46%", "同比下降 33.46%") is True
    assert direction_consistent("下降 33.46%", "同比增长 33.46%") is False
    assert direction_consistent("营业收入 100 元", "营业收入 100 元") is None


def test_point_equivalence_combines_numeric_source_and_direction():
    point = {
        "text": "营业收入同比增长 12.11%",
        "chunk_ids": ["doc#t0006"],
        "evidence": {"table_id": "doc#t0006", "value": "12.11%"},
    }
    row = point_equivalence(
        point,
        "营业收入同比增长 12.11%，见 [1]",
        cited_base_ids={"doc#t0006"},
    )
    assert row["criteria"] == {"numeric": True, "direction": True, "source": True}
    assert row["equivalent"] is True


def test_point_equivalence_fails_on_wrong_unit_or_direction():
    point = {
        "text": "毛利率比同行高 9.09 个百分点",
        "chunk_ids": ["doc#t0013"],
        "evidence": {"table_id": "doc#t0013", "value": "9.09"},
    }
    wrong_direction = point_equivalence(
        point, "毛利率比同行低 9.09 个百分点", cited_base_ids={"doc#t0013"}
    )
    assert wrong_direction["criteria"]["direction"] is False
    assert wrong_direction["equivalent"] is False


def test_task_equivalence_marks_abstain_reasonable():
    task = {
        "id": "T",
        "should_abstain": True,
        "answer_points": [
            {"text": "资料未披露", "kind": "note"},
        ],
    }
    result = {"status": "abstained", "answer": "根据现有资料无法确定"}
    row = task_equivalence(task, result)
    assert row["abstain_expected"] is True
    assert row["abstain_reasonable"] is True
