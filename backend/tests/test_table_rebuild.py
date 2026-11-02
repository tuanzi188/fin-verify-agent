"""坐标重建表格的测试：矩形画线的 PDF（美的/比亚迪）靠文本层坐标还原网格。"""

from app.finance.ingest import _grid_from_words, _is_amount, extract_tables

HEADER_Y = 100.0
ROW_START = 120.0
ROW_STEP = 14.0


def word(text: str, x0: float, y0: float, x1: float, y1: float) -> tuple:
    return (x0, y0, x1, y1, text, 0, 0, 0)


def row(y: float, name: str, note: str, left: str, right: str) -> list[tuple]:
    return [
        word(name, 96, y, 140, y + 10),
        word(note, 231, y, 260, y + 10),
        word(left, 282, y, 343, y + 10),
        word(right, 350, y, 400, y + 10),
    ]


def balance_sheet_words() -> list[tuple]:
    words = [
        word("资产", 138, HEADER_Y, 158, HEADER_Y + 10),
        word("附注", 232, HEADER_Y, 252, HEADER_Y + 10),
        word("2025 年", 297, HEADER_Y, 330, HEADER_Y + 10),
        word("2024 年", 350, HEADER_Y, 390, HEADER_Y + 10),
        # 表头第二行：没有金额，应该被并入表头而不是当数据行
        word("12 月31 日", 282, HEADER_Y + 14, 330, HEADER_Y + 24),
        word("12 月31 日", 350, HEADER_Y + 14, 390, HEADER_Y + 24),
    ]
    data = [
        ("货币资金", "四(1)", "85,247,150", "140,410,308"),
        ("交易性金融资产", "四(2)", "710,717", "6,936,113"),
        ("应收账款", "四(5)", "40,450,097", "35,798,974"),
        ("存货", "四(9)", "38,412,000", "33,100,000"),
        ("流动资产合计", "", "338,853,000", "322,000,000"),
        ("资产总计", "", "608,791,766", "604,351,853"),
    ]
    for index, (name, note, left, right) in enumerate(data):
        y = ROW_START + index * ROW_STEP
        words.extend(row(y, name, note, left, right))
    return words


def test_rebuild_balance_sheet_grid():
    result = _grid_from_words(balance_sheet_words())
    assert result is not None
    grid, bbox = result

    assert grid[0][0] == "项目"
    assert "2025 年" in grid[0]
    assert "2024 年" in grid[0]
    # 表头第二行（12 月31 日）不能漏成数据行
    assert grid[1][0] == "货币资金"

    rows = {row[0]: row for row in grid[1:]}
    assert len(rows) == 6
    assert rows["资产总计"][2] == "608,791,766"
    assert rows["资产总计"][3] == "604,351,853"
    assert bbox[1] < HEADER_Y < bbox[3]


def test_rebuild_rejects_body_text_page():
    # 正文页：每行词多、列散，没有稳定的“两列金额”
    words = []
    for index in range(12):
        y = 100 + index * 14
        for column in range(10):
            words.append(word(f"文中片段{index}{column}", 60 + column * 45, y, 90 + column * 45, y + 10))
    assert _grid_from_words(words) is None


def test_rebuild_rejects_short_page():
    words = []
    for index in range(3):
        y = 100 + index * 14
        words.extend(row(y, "项目" + str(index), "注", "1,000", "2,000"))
    assert _grid_from_words(words) is None


def test_is_amount_excludes_year_headers():
    assert _is_amount("85,247,150") is True
    assert _is_amount("1,609") is True
    assert _is_amount("(1,234)") is True
    assert _is_amount("2025") is False
    assert _is_amount("2025 年") is False
    assert _is_amount("附注") is False


def test_year_label_moves_to_the_amount_column():
    """比亚迪形状：`2025 年12` 和 `月31 日` 被拆到两列，年份必须落到有数据的那列。

    否则 `get_cell(column="2025 年")` 永远找不到，整张表只能按行读。
    """
    words = [
        word("资产", 57, 100, 75, 110),
        word("附注七", 297, 100, 324, 110),
        word("2025 年12", 354, 100, 394, 110),
        word("月31 日", 394, 100, 420, 110),
        word("2024 年12", 477, 100, 517, 110),
        word("月31 日", 517, 100, 545, 110),
    ]
    data = [
        ("货币资金", "1", "75,424,747", "102,738,734"),
        ("交易性金融资产", "2", "54,532,541", "40,511,496"),
        ("应收账款", "3", "37,004,685", "62,298,988"),
        ("存货", "7", "138,420,580", "116,036,237"),
        ("流动资产合计", "", "332,401,000", "291,000,000"),
        ("资产总计", "", "883,729,883", "783,355,855"),
    ]
    for index, (name, note, left, right) in enumerate(data):
        y = 120 + index * 14
        words.extend(
            [
                word(name, 66, y, 140, y + 10),
                word(note, 305, y, 311, y + 10),
                word(left, 383, y, 450, y + 10),
                word(right, 488, y, 555, y + 10),
            ]
        )

    result = _grid_from_words(words)
    assert result is not None
    grid, _ = result
    rows = {row[0]: row for row in grid[1:]}
    values = rows["资产总计"]
    labels = {label for label in grid[0] if label.endswith("年")}
    assert {"2025 年", "2024 年"} <= labels

    for index, value in enumerate(values):
        if value == "883,729,883":
            assert grid[0][index] == "2025 年"
        if value == "783,355,855":
            assert grid[0][index] == "2024 年"


class _EmptyTableResult:
    tables: list = []


class _FakePage:
    """只实现 extract_tables 用到的三个接口：rect / find_tables / get_text。"""

    class rect:  # noqa: N801 - 模拟 fitz.Page.rect.height
        height = 800.0

    def find_tables(self):
        return _EmptyTableResult()

    def get_text(self, mode: str):
        assert mode == "words"
        return balance_sheet_words()


class _FakeDoc:
    page_count = 1

    def __getitem__(self, index: int):
        assert index == 0
        return _FakePage()


def test_rebuilt_tables_stay_out_of_body_exclusion_boxes():
    """重建表的 bbox 接近整页，不能拿去剔除正文——否则同页小节标题被一起吞掉。"""
    tables, boxes = extract_tables(_FakeDoc(), [])
    assert [table["rebuilt"] for table in tables] == [True]
    assert boxes == {}
