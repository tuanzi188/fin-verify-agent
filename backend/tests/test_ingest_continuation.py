"""跨页财务报表合并的测试：续页条件、前导空列、防误合并。"""

from app.finance.ingest import _merge_continuations, _trim_leading_empty

HEIGHTS = {1: 842.0, 2: 842.0, 3: 842.0}


def table(tid, page, page_end, bbox, columns, rows, caption=""):
    return {
        "table_id": tid,
        "page": page,
        "page_end": page_end,
        "bbox": bbox,
        "columns": columns,
        "rows": rows,
        "caption": caption,
    }


def test_merges_multi_page_statement():
    first = table(
        "t1", 1, 1, [56.9, 195.4, 538.4, 764.4],
        ["项目", "期末余额", "期初余额"],
        [["货币资金", "1,000", "2,000"]] * 10,
    )
    second = table(
        "t2", 2, 2, [56.9, 72.3, 538.4, 762.1],
        ["项目", "期末余额", "期初余额"],
        [["固定资产", "3,000", "4,000"]] * 10,
    )
    merged = _merge_continuations([first, second], HEIGHTS)
    assert len(merged) == 1
    assert len(merged[0]["rows"]) == 20
    assert merged[0]["page_end"] == 2


def test_does_not_merge_table_with_its_own_caption():
    """「2、母公司资产负债表」是新的一张表，不能接到合并报表后面。"""
    first = table(
        "t1", 1, 1, [56.9, 195.4, 538.4, 764.4],
        ["项目", "期末余额", "期初余额"],
        [["货币资金", "1,000", "2,000"]] * 10,
    )
    second = table(
        "t2", 2, 2, [56.9, 72.3, 538.4, 762.1],
        ["项目", "期末余额", "期初余额"],
        [["固定资产", "3,000", "4,000"]] * 10,
        caption="2、母公司资产负债表",
    )
    assert len(_merge_continuations([first, second], HEIGHTS)) == 2


def test_does_not_merge_when_previous_table_does_not_reach_page_bottom():
    first = table(
        "t1", 1, 1, [56.9, 195.4, 538.4, 400.0],
        ["项目", "期末余额", "期初余额"],
        [["货币资金", "1,000", "2,000"]] * 10,
    )
    second = table(
        "t2", 2, 2, [56.9, 72.3, 538.4, 762.1],
        ["项目", "期末余额", "期初余额"],
        [["固定资产", "3,000", "4,000"]] * 10,
    )
    assert len(_merge_continuations([first, second], HEIGHTS)) == 2


def test_does_not_merge_two_column_long_table():
    """两张只有 2 列的长表不是财务报表，不接——防误合并的底线。"""
    first = table(
        "t1", 1, 1, [56.9, 195.4, 538.4, 764.4], ["项目", "2025"], [["行", "1"]] * 20
    )
    second = table(
        "t2", 2, 2, [56.9, 72.3, 538.4, 762.1], ["项目", "2025"], [["行", "2"]] * 5
    )
    assert len(_merge_continuations([first, second], HEIGHTS)) == 2


def test_trim_leading_empty_moves_misplaced_label():
    fragment = {
        "columns": ["项目", "流动负债合计", "317,171,534", "287,001,069"],
        "rows": [["", f"项目{i}", f"{i}00", f"{i}50"] for i in range(9)]
        + [["股本", "", "", ""]],
    }
    trimmed = _trim_leading_empty(fragment, 3)
    assert trimmed["columns"] == ["流动负债合计", "317,171,534", "287,001,069"]
    assert trimmed["rows"][0] == ["项目0", "000", "050"]
    # 落错位的行名搬回第 1 列，不丢
    assert trimmed["rows"][-1] == ["股本", "", ""]


def test_trim_refuses_when_width_does_not_match():
    fragment = {
        "columns": ["a", "b", "c", "d", "e"],
        "rows": [["", "x", "1", "2", "3"]],
    }
    assert _trim_leading_empty(fragment, 3) is None
