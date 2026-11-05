"""表单位识别的测试：`单位：千元` 不能被当成页眉剔掉。"""

from app.finance.tree import Line, count_repeated_lines


def line(page: int, text: str) -> Line:
    return Line(
        page=page, y0=100.0, y1=110.0, x0=60.0, x1=200.0,
        text=text, size=9.0, bold=False,
    )


def test_unit_declaration_is_not_treated_as_page_furniture():
    lines = [line(page, "单位：千元") for page in range(1, 15)]
    lines += [line(page, "www.cninfo.com.cn") for page in range(1, 15)]

    repeated = count_repeated_lines(lines)

    # 页脚照样剔掉
    assert "www.cninfo.com.cn" in repeated
    # 单位声明是内容，必须留下——否则数字回退成文档默认单位，差 1000 倍
    assert "单位：千元" not in repeated


def test_parenthesised_unit_declaration_is_kept():
    lines = [line(page, "（单位：万元）") for page in range(1, 12)]
    assert "（单位：万元）" not in count_repeated_lines(lines)
