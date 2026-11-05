"""坐标重建列的测试：表头分段错位、追溯调整列、段中表头。

全部离线，用合成 bbox，不依赖真实 PDF。
"""

from app.finance.ingest import (
    _cluster_cells,
    _has_placeholder,
    _is_header_like_row,
    _split_header,
    _split_table_by_cells,
)


def box(x0, x1):
    return (x0, 0.0, x1, 10.0)


def test_cluster_cells_merges_drift_but_not_adjacent_columns():
    cells = [
        (137.1, 217.3, "362,012,554"),
        (222.8, 292.2, "100.00%"),
        (217.3, 297.7, "98.48%"),  # 同一占比列，右边界漂了 5.5pt
        (297.7, 377.9, "400,917,045"),
    ]
    clusters = _cluster_cells(cells)
    assert len(clusters) == 3
    assert abs(clusters[0][0] - 217.3) < 0.1
    assert abs(clusters[1][0] - 295.0) < 0.1
    assert abs(clusters[2][0] - 377.9) < 0.1


def test_split_table_by_cells_repairs_shifted_header():
    """表头和数据行网格索引差一格时，按坐标仍能对到正确年份。"""
    text_rows = [
        ["项目", "", "", "2024 年", "2023 年", "", "本年比上年", ""],
        ["", "", "", "", "", "", "增减", ""],
        ["", "营业收入（千元）", "", "362,012,554", "400,917,045", "-9.70%", "", ""],
        ["", "净利润（千元）", "", "50,744,682", "44,121,248", "15.01%", "", ""],
    ]
    cell_rows = [
        [box(60, 200), None, None, box(200, 280), box(280, 360), None, box(360, 420), None],
        [None, None, None, None, None, None, box(360, 420), None],
        [None, box(60, 200), None, box(200, 280), box(280, 360), box(360, 420), None, None],
        [None, box(60, 200), None, box(200, 280), box(280, 360), box(360, 420), None, None],
    ]

    columns, body = _split_table_by_cells(cell_rows, text_rows)

    assert columns == ["项目", "2024 年", "2023 年", "本年比上年增减"]
    assert body[0] == ["营业收入（千元）", "362,012,554", "400,917,045", "-9.70%"]
    assert body[1] == ["净利润（千元）", "50,744,682", "44,121,248", "15.01%"]


def test_split_table_by_cells_keeps_restated_columns_apart():
    """追溯调整前/后两列数值不同，不能被合并成一列。"""
    text_rows = [
        ["项目", "", "2024 年", "", "2022 年", ""],
        ["", "", "", "", "调整前", "调整后"],
        ["", "基本每股收益", "11.58", "", "12.92", "7.18"],
        ["", "稀释每股收益", "11.58", "", "12.88", "7.16"],
    ]
    cell_rows = [
        [box(60, 200), None, box(200, 280), None, box(280, 400), None],
        [None, None, None, None, box(280, 340), box(340, 400)],
        [None, box(60, 200), box(200, 280), None, box(280, 340), box(340, 400)],
        [None, box(60, 200), box(200, 280), None, box(280, 340), box(340, 400)],
    ]

    columns, body = _split_table_by_cells(cell_rows, text_rows)

    assert columns == ["项目", "2024 年", "2022 年调整前", "2022 年调整后"]
    assert body[0] == ["基本每股收益", "11.58", "12.92", "7.18"]


def test_header_like_row_detects_second_segment_header():
    assert _is_header_like_row(["项目", "2024 年末", "2023 年末", "本年末比上年末增减"])
    assert _is_header_like_row(["", "", "调整前", "调整后"])
    # 数据行有数字，不是表头
    assert not _is_header_like_row(["", "营业收入", "362,012,554", "400,917,045"])
    # 只有一格的正文行不算表头
    assert not _is_header_like_row(["", "分行业", "", ""])


def test_split_header_keeps_grid_result_when_columns_are_clean():
    """没有 colN 占位名时不触发坐标重建，保持既有行为。"""
    rows = [
        ["项目", "2025 年", "2024 年"],
        ["营业收入", "1,234", "1,000"],
        ["净利润", "120", "100"],
    ]
    cell_rows = [
        [box(60, 200), box(200, 280), box(280, 360)],
        [box(60, 200), box(200, 280), box(280, 360)],
        [box(60, 200), box(200, 280), box(280, 360)],
    ]
    columns, body = _split_header(rows, cell_rows)
    assert columns == ["项目", "2025 年", "2024 年"]
    assert body[0] == ["营业收入", "1,234", "1,000"]


def test_split_header_uses_coords_when_grid_has_placeholders():
    text_rows = [
        ["项目", "", "", "2024 年", "2023 年", "", "本年比上年", ""],
        ["", "", "", "", "", "", "增减", ""],
        ["", "营业收入（千元）", "", "362,012,554", "400,917,045", "-9.70%", "", ""],
        ["", "净利润（千元）", "", "50,744,682", "44,121,248", "15.01%", "", ""],
    ]
    cell_rows = [
        [box(60, 200), None, None, box(200, 280), box(280, 360), None, box(360, 420), None],
        [None, None, None, None, None, None, box(360, 420), None],
        [None, box(60, 200), None, box(200, 280), box(280, 360), box(360, 420), None, None],
        [None, box(60, 200), None, box(200, 280), box(280, 360), box(360, 420), None, None],
    ]
    columns, _body = _split_header(text_rows, cell_rows)
    assert not _has_placeholder(columns)
    assert columns[1] == "2024 年"
    assert columns[2] == "2023 年"
