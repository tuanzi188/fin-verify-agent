"""市场综述：放行条件、空结果、分段解析、来源名校验。全部离线。"""

import pytest

from app.services.market_brief import (
    DISCLAIMER,
    build_market_brief,
    build_queries,
    fit_summary,
    source_label,
    split_sections,
    unknown_attributions,
)
from app.services.web_search import FixtureProvider, WebSearchBudget

_GOOD_ANSWER = """## 核心判断
多项因素叠加，来源侧重点不同（财联社，03-15）

## 分项
【资金】股票 ETF 连续净流入（财联社，03-15）
【基本面】三季报盈利改善（央广网，03-14）

## 分歧
无

## 资料缺口
缺少全市场成交额数据
"""

_ITEMS = [
    {
        "url": "https://www.cls.cn/a",
        "title": "资金面解读",
        "publishedDate": "2025-03-15T00:00:00Z",
        "text": "ETF 连续净流入，成交额放大。",
    },
    {
        "url": "https://finance.cnr.cn/b",
        "title": "业绩解读",
        "publishedDate": "2025-03-14T00:00:00Z",
        "text": "三季报盈利改善。",
    },
]


def _fake_chat(answer):
    calls = []

    async def chat(messages, **kwargs):
        calls.append(messages)
        return answer, None, {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}

    chat.calls = calls
    return chat


def test_source_label_falls_back_to_registered_domain():
    assert source_label("m.cls.cn") == "财联社"
    assert source_label("finance.cnr.cn") == "央广网"
    assert source_label("unknown.example.com") == "unknown.example.com"


def test_build_queries_keeps_at_most_two():
    assert build_queries("宁德时代为什么跌", company="宁德时代", period="2025") == [
        "宁德时代为什么跌",
        "宁德时代 2025 公告",
    ]
    assert build_queries("", company=None) == []


def test_split_sections_tolerates_missing_headings():
    parsed = split_sections(_GOOD_ANSWER)
    assert "核心判断" in parsed and "分项" in parsed
    assert split_sections("没有标题的纯文本") == {}


def test_unknown_attributions_ignores_old_news_marker():
    labels = {"财联社"}
    text = "资金流入（财联社，03-15）；（旧闻，2024-09）；（某自媒体，03-15）"
    assert unknown_attributions(text, labels) == ["某自媒体"]


@pytest.mark.asyncio
async def test_skips_when_local_evidence_exists():
    chat = _fake_chat(_GOOD_ANSWER)
    brief = await build_market_brief(
        "宁德时代为什么跌",
        local_citable_count=1,
        provider=FixtureProvider(_ITEMS),
        chat=chat,
    )
    assert brief.ok is False
    assert brief.skipped_reason == "local_evidence_exists"
    assert chat.calls == []


@pytest.mark.asyncio
async def test_no_sources_does_not_call_model():
    chat = _fake_chat(_GOOD_ANSWER)
    brief = await build_market_brief(
        "宁德时代为什么跌",
        provider=FixtureProvider(
            [
                {
                    "url": "https://guba.eastmoney.com/x",
                    "title": "股吧",
                    "publishedDate": "2025-03-15T00:00:00Z",
                    "text": "…",
                }
            ]
        ),
        chat=chat,
    )
    assert brief.ok is False
    assert brief.skipped_reason == "no_sources"
    assert chat.calls == []
    # 被丢弃的来源要能说明原因，否则"什么都没搜到"无法复核
    assert brief.skipped_sources == [
        {"url": "https://guba.eastmoney.com/x", "reason": "blocked_domain"}
    ]


@pytest.mark.asyncio
async def test_happy_path_parses_sections_and_appends_disclaimer():
    chat = _fake_chat(_GOOD_ANSWER)
    brief = await build_market_brief(
        "宁德时代为什么跌",
        company="宁德时代",
        provider=FixtureProvider(_ITEMS),
        budget=WebSearchBudget(1),
        chat=chat,
    )
    assert brief.ok is True
    assert brief.summary.startswith("多项因素叠加")
    assert any("ETF" in item for item in brief.sections["分项"])
    assert brief.gaps == ["缺少全市场成交额数据"]
    assert [source.label for source in brief.sources] == ["财联社", "央广网"]
    # 免责由服务端追加，不依赖模型输出
    assert brief.disclaimer == DISCLAIMER
    assert DISCLAIMER not in brief.raw
    assert brief.warnings == []


@pytest.mark.asyncio
async def test_unknown_source_name_is_warned_not_silently_accepted():
    answer = _GOOD_ANSWER.replace("（央广网，03-14）", "（某财经大V，03-14）")
    brief = await build_market_brief(
        "宁德时代为什么跌",
        company="宁德时代",
        provider=FixtureProvider(_ITEMS),
        chat=_fake_chat(answer),
    )
    assert any("某财经大V" in item for item in brief.warnings)


@pytest.mark.asyncio
async def test_official_pdf_is_ingest_candidate_not_brief_source():
    items = _ITEMS + [
        {
            "url": "http://static.cninfo.com.cn/finalpage/2024/1234.PDF",
            "title": "2024 年年度报告",
            "publishedDate": "2025-03-15T00:00:00Z",
            "text": "……",
        }
    ]
    brief = await build_market_brief(
        "宁德时代为什么跌",
        company="宁德时代",
        provider=FixtureProvider(items),
        chat=_fake_chat(_GOOD_ANSWER),
    )
    # 官方 PDF 不进"市场说法"来源清单
    assert all(source.tier >= 2 for source in brief.sources)
    assert [item["domain"] for item in brief.ingest_candidates] == [
        "static.cninfo.com.cn"
    ]
    # 但取舍要写清楚：它被排除的原因是"官方原件"，不是"不相关"
    assert {
        "url": "http://static.cninfo.com.cn/finalpage/2024/1234.PDF",
        "reason": "official_original",
    } in brief.skipped_sources


@pytest.mark.asyncio
async def test_accepts_conclusion_heading_as_summary():
    answer = """## 结论
一句话：多项因素叠加（财联社，03-15）

## 分项
【资金】ETF 净流入（财联社，03-15）
"""
    brief = await build_market_brief(
        "宁德时代为什么跌",
        company="宁德时代",
        provider=FixtureProvider(_ITEMS),
        chat=_fake_chat(answer),
    )
    assert brief.ok is True
    assert brief.summary == "多项因素叠加（财联社，03-15）"


@pytest.mark.asyncio
async def test_numbers_missing_from_sources_are_warned():
    answer = _GOOD_ANSWER.replace(
        "【资金】股票 ETF 连续净流入（财联社，03-15）",
        "【资金】股票 ETF 净流入 9876.54 亿元（财联社，03-15）",
    )
    brief = await build_market_brief(
        "宁德时代为什么跌",
        company="宁德时代",
        provider=FixtureProvider(_ITEMS),
        chat=_fake_chat(answer),
    )
    assert any("9876.54" in item for item in brief.warnings)


def test_fit_summary_cuts_at_sentence_boundary():
    text = "第一句结论。" + "后面还有很长的解释" * 20
    fitted = fit_summary(text, limit=20)
    assert fitted == "第一句结论。"


def test_normalize_markup_fixes_model_broken_bold():
    from app.services.market_brief import _normalize_markup

    # 实测形态：开头单星、结尾双星
    assert _normalize_markup("*营收端：价格拖累。**") == "**营收端：价格拖累。**"
    # 万一没配对，宁可去掉加粗也不留星号
    assert _normalize_markup("**没配对的加粗") == "没配对的加粗"
    assert _normalize_markup("**正常加粗**：内容") == "**正常加粗**：内容"


def test_split_long_breaks_at_sentence_boundaries():
    from app.services.market_brief import _split_long

    pieces = _split_long("第一句。" * 10, max_len=30)
    assert len(pieces) > 1
    assert all(len(piece) <= 30 for piece in pieces)
    assert "".join(pieces) == "第一句。" * 10


def test_split_long_turns_trailing_semicolon_into_period():
    from app.services.market_brief import _split_long

    pieces = _split_long("甲；" * 40, max_len=10)
    assert all(not piece.endswith("；") for piece in pieces)


def test_auto_bold_only_short_prefix():
    from app.services.market_brief import _auto_bold

    assert _auto_bold("营收端：内容") == "**营收端**：内容"
    assert _auto_bold("这是一个非常非常长的前缀名称：内容") == "这是一个非常非常长的前缀名称：内容"


def test_only_attribution_questions_go_to_brief_on_local_miss():
    from app.services.market_brief import should_brief_on_local_miss

    assert should_brief_on_local_miss("宁德时代为什么跌") is True
    assert should_brief_on_local_miss("这两天大盘大涨的原因") is True
    assert should_brief_on_local_miss("格力电器 2025 年营业收入是多少") is False


@pytest.mark.asyncio
async def test_same_domain_is_capped_for_source_diversity():
    items = [
        {
            "url": f"https://finance.sina.com.cn/{index}",
            "title": f"新浪{index}",
            "publishedDate": "2025-03-15T00:00:00Z",
            "text": "正文",
        }
        for index in range(4)
    ] + _ITEMS
    brief = await build_market_brief(
        "宁德时代为什么跌",
        company="宁德时代",
        provider=FixtureProvider(items),
        chat=_fake_chat(_GOOD_ANSWER),
    )
    sina = [source for source in brief.sources if source.domain == "finance.sina.com.cn"]
    assert len(sina) == 2
    assert any(source.domain == "www.cls.cn" for source in brief.sources)


@pytest.mark.asyncio
async def test_consensus_wording_is_warned_not_accepted():
    answer = _GOOD_ANSWER.replace(
        "多项因素叠加，来源侧重点不同（财联社，03-15）",
        "四家来源表述一致，可直接采信（财联社，03-15）",
    )
    brief = await build_market_brief(
        "宁德时代为什么跌",
        company="宁德时代",
        provider=FixtureProvider(_ITEMS),
        chat=_fake_chat(answer),
    )
    assert any("采信" in warning for warning in brief.warnings)


@pytest.mark.asyncio
async def test_parses_misreadings_section():
    answer = _GOOD_ANSWER.replace(
        "## 分歧",
        "## 常见误读\n- ❌ 不是卖得更少 —— 销量是增长的\n\n## 分歧",
    )
    brief = await build_market_brief(
        "宁德时代为什么跌",
        company="宁德时代",
        provider=FixtureProvider(_ITEMS),
        chat=_fake_chat(answer),
    )
    assert brief.misreadings == ["❌ 不是卖得更少 —— 销量是增长的"]
