"""联网检索机制：来源分级、查询闸门、预算。全部离线，不联网。"""

import pytest

from app.services.web_search import (
    FixtureProvider,
    WebSearchBudget,
    WebSearchError,
    canonical_url,
    clear_web_cache,
    domain_tier,
    normalize_sources,
    query_violations,
    search_web,
    should_use_web,
)


def _item(url, title, published, text="正文"):
    return {"url": url, "title": title, "publishedDate": published, "text": text}


# ---------------------------------------------------------------------------
# 来源分级
# ---------------------------------------------------------------------------


def test_domain_tier_recognizes_official_and_media():
    assert domain_tier("cninfo.com.cn") == 1
    assert domain_tier("www.cninfo.com.cn") == 1
    assert domain_tier("stcn.com") == 2
    assert domain_tier("finance.sina.com.cn") == 3


def test_domain_tier_drops_blocked_and_unknown():
    assert domain_tier("guba.eastmoney.com") is None
    assert domain_tier("xueqiu.com") is None
    assert domain_tier("example.org") is None
    # 黑名单优先于父域分级
    assert domain_tier("tieba.baidu.com") is None


def test_normalize_orders_by_tier_then_recency():
    raw = [
        _item("https://www.stcn.com/a", "媒体旧", "2026-11-01T00:00:00+00:00"),
        _item("https://www.cninfo.com.cn/b", "公告", "2026-10-01T00:00:00+00:00"),
        _item("https://www.stcn.com/c", "媒体新", "2026-11-04T00:00:00+00:00"),
    ]
    sources, skipped = normalize_sources(raw)
    assert [s.title for s in sources] == ["公告", "媒体新", "媒体旧"]
    assert skipped == []


def test_normalize_drops_blocked_undated_and_unknown():
    raw = [
        _item("https://guba.eastmoney.com/x", "股吧", "2026-11-04T00:00:00+00:00"),
        _item("https://stcn.com/no-date", "无日期", None),
        _item("https://example.org/x", "陌生站", "2026-11-04T00:00:00+00:00"),
        _item("https://stcn.com/ok", "正常", "2026-11-04T00:00:00+00:00"),
    ]
    sources, skipped = normalize_sources(raw)
    assert [s.title for s in sources] == ["正常"]
    reasons = {row["reason"] for row in skipped}
    assert reasons == {"blocked_domain", "missing_published_at", "not_whitelisted"}


def test_normalize_dedupes_same_article_across_tracking_params():
    raw = [
        _item("https://www.stcn.com/a?id=1&utm_source=x", "一", "2026-11-04T00:00:00+00:00"),
        _item("https://www.stcn.com/a?id=1&utm_source=y", "二", "2026-11-04T00:00:00+00:00"),
    ]
    sources, skipped = normalize_sources(raw)
    assert len(sources) == 1
    assert skipped[0]["reason"] == "duplicate"
    assert canonical_url("https://A.com/x?utm_medium=1") == "https://a.com/x"


# ---------------------------------------------------------------------------
# 查询闸门
# ---------------------------------------------------------------------------


def test_query_guard_blocks_answer_written_into_query():
    violations = query_violations(
        "宁德时代 2024 年营业收入 362 亿 原因",
        question="宁德时代 2024 年营业收入是多少",
    )
    assert any("数字" in item for item in violations)


def test_query_guard_blocks_wrong_year():
    violations = query_violations(
        "宁德时代 2023 年营业收入",
        question="宁德时代 2024 年营业收入是多少",
    )
    assert any("年份" in item for item in violations)


def test_query_guard_allows_question_terms_and_stock_code():
    assert query_violations(
        "宁德时代 2024 年营业收入 毛利率",
        question="宁德时代 2024 年营业收入和毛利率",
    ) == []
    assert query_violations(
        "300750 2024 年营业收入",
        question="宁德时代 2024 年营业收入",
        extra_allowed_numbers=["300750"],
    ) == []


# ---------------------------------------------------------------------------
# 放行与预算
# ---------------------------------------------------------------------------


def test_should_use_web_only_when_local_is_empty():
    assert should_use_web(local_citable_count=0, allow_web=True) is True
    assert should_use_web(local_citable_count=1, allow_web=True) is False
    assert should_use_web(local_citable_count=0, allow_web=False) is False


def test_budget_allows_two_calls_then_refuses():
    budget = WebSearchBudget()
    budget.consume()
    budget.consume()
    assert budget.remaining == 0
    with pytest.raises(WebSearchError):
        budget.consume()


def test_violation_does_not_consume_budget():
    budget = WebSearchBudget()
    with pytest.raises(WebSearchError):
        search_web(
            "宁德时代 2023 年营业收入 999 亿",
            question="宁德时代 2024 年营业收入是多少",
            budget=budget,
            provider=FixtureProvider([]),
        )
    assert budget.used == 0


# ---------------------------------------------------------------------------
# 端到端（fixture provider）
# ---------------------------------------------------------------------------


def test_search_web_normalizes_fixture_results():
    provider = FixtureProvider(
        [
            _item("https://www.cninfo.com.cn/a", "公告", "2026-10-01T00:00:00+00:00"),
            _item("https://guba.eastmoney.com/b", "股吧帖", "2026-11-04T00:00:00+00:00"),
        ]
    )
    budget = WebSearchBudget()
    result = search_web(
        "宁德时代 2024 年营业收入",
        question="宁德时代 2024 年营业收入是多少",
        budget=budget,
        provider=provider,
    )
    assert result.provider == "fixture"
    assert [s.domain for s in result.sources] == ["www.cninfo.com.cn"]
    assert result.skipped[0]["reason"] == "blocked_domain"
    assert budget.used == 1


def test_exa_provider_requires_key(monkeypatch):
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    from app.services.web_search import ExaProvider

    provider = ExaProvider(api_key="")
    with pytest.raises(WebSearchError):
        provider.search("宁德时代", domains=["cninfo.com.cn"])


# ---------------------------------------------------------------------------
# Exa MCP 文本解析
# ---------------------------------------------------------------------------

_MCP_TEXT = """Title: 宁德时代2024年营收3620.13亿元
URL: https://finance.cnr.cn/a.shtml
Published: 2025-03-14T04:28:41.000Z
Author: 刘家怡
Highlights:
2024年实现营业收入3620.13亿元，同比下降9.7%。

---

Title: 公司公告_新浪财经
URL: https://finance.sina.com.cn/b.php?id=1
Published: N/A
Author: N/A
Highlights:
| 营业收入（千元） | 362,012,554 |
| 注：尾数不符系四舍五入。 |
"""


def test_parse_mcp_text_splits_results_and_handles_na():
    from app.services.web_search import parse_mcp_text

    items = parse_mcp_text(_MCP_TEXT)
    assert len(items) == 2
    assert items[0]["url"] == "https://finance.cnr.cn/a.shtml"
    assert items[0]["publishedDate"] == "2025-03-14T04:28:41.000Z"
    assert "3620.13" in items[0]["text"]
    # N/A 统一成 None，交给 normalize 决定去留
    assert items[1]["publishedDate"] is None
    # 正文里的冒号和表格不会被当成字段头
    assert "362,012,554" in items[1]["text"]


def test_undated_official_source_can_be_kept_while_media_is_dropped():
    raw = [
        _item("https://www.cninfo.com.cn/a", "公告", None),
        _item("https://finance.sina.com.cn/b", "转载", None),
    ]
    sources, skipped = normalize_sources(
        raw, require_published_at=True, undated_max_tier=1
    )
    assert [s.domain for s in sources] == ["www.cninfo.com.cn"]
    assert skipped == [{"url": "https://finance.sina.com.cn/b", "reason": "missing_published_at"}]


def test_pdf_on_company_site_becomes_ingest_candidate():
    from app.services.web_search import pdf_ingest_candidates

    raw = [
        # 实测第一名：宁德时代官网年报 PDF，不在白名单里
        _item("https://www.catl.com/uploads/a/20250317_report.pdf", "2024年报", "2025-03-17T00:00:00Z"),
        _item("https://guba.eastmoney.com/x.pdf", "股吧附件", "2026-11-04T00:00:00Z"),
        _item("https://www.stcn.com/article.html", "媒体", "2026-11-04T00:00:00Z"),
    ]
    candidates = pdf_ingest_candidates(raw)
    assert [item["domain"] for item in candidates] == ["www.catl.com"]
    assert candidates[0]["title"] == "2024年报"


def test_pdf_candidates_dedupe_tracking_params():
    from app.services.web_search import pdf_ingest_candidates

    raw = [
        _item("https://a.example.com/r.pdf?utm_source=1", "一", None),
        _item("https://a.example.com/r.pdf?utm_source=2", "二", None),
    ]
    assert len(pdf_ingest_candidates(raw)) == 1


# ---------------------------------------------------------------------------
# 未知域名分级与结果缓存
# ---------------------------------------------------------------------------


def test_unknown_domain_is_dropped_by_default():
    raw = [_item("https://some-blog.example.com/a", "科普", "2025-01-01T00:00:00Z")]
    sources, skipped = normalize_sources(raw, undated_max_tier=2)
    assert sources == []
    assert skipped[0]["reason"] == "not_whitelisted"


def test_unknown_domain_becomes_tertiary_when_allowed():
    raw = [_item("https://some-blog.example.com/a", "科普", "2025-01-01T00:00:00Z")]
    sources, _ = normalize_sources(raw, undated_max_tier=2, allow_unknown=True)
    assert [source.tier for source in sources] == [3]


def test_blacklist_still_wins_when_unknown_is_allowed():
    raw = [_item("https://guba.eastmoney.com/a", "股吧", "2025-01-01T00:00:00Z")]
    sources, skipped = normalize_sources(raw, undated_max_tier=3, allow_unknown=True)
    assert sources == []
    assert skipped[0]["reason"] == "blocked_domain"


def test_search_result_is_cached_for_repeated_query():
    class CountingProvider:
        name = "counting"

        def __init__(self):
            self.calls = 0

        def search(self, *args, **kwargs):
            self.calls += 1
            return []

    clear_web_cache()
    provider = CountingProvider()
    first = search_web("同一问题", question="同一问题", budget=WebSearchBudget(), provider=provider)
    second = search_web("同一问题", question="同一问题", budget=WebSearchBudget(), provider=provider)
    assert provider.calls == 1
    assert first is second


def test_cache_does_not_consume_budget_on_hit():
    class CountingProvider:
        name = "counting-budget"

        def __init__(self):
            self.calls = 0

        def search(self, *args, **kwargs):
            self.calls += 1
            return []

    clear_web_cache()
    provider = CountingProvider()
    budget = WebSearchBudget(max_calls=2)
    search_web("缓存预算", question="缓存预算", budget=budget, provider=provider)
    search_web("缓存预算", question="缓存预算", budget=budget, provider=provider)
    assert budget.used == 1
    assert provider.calls == 1


def test_provider_with_stable_cache_tag_hits_cache_across_instances():
    """真实 provider 每次新建实例：缓存身份必须由 cache_tag 决定，不能靠 id()。"""

    class TaggedProvider:
        name = "tagged"
        cache_tag = "tagged-provider"

        def __init__(self):
            self.calls = 0

        def search(self, *args, **kwargs):
            self.calls += 1
            return []

    clear_web_cache()
    first_provider = TaggedProvider()
    second_provider = TaggedProvider()
    search_web("跨实例缓存", question="跨实例缓存", budget=WebSearchBudget(), provider=first_provider)
    search_web("跨实例缓存", question="跨实例缓存", budget=WebSearchBudget(), provider=second_provider)
    assert first_provider.calls == 1
    assert second_provider.calls == 0


def test_fixture_providers_do_not_share_cache():
    """不同 fixtures 用同一 query 不能互相命中，否则测试会拿到别人的结果。"""
    first = FixtureProvider([
        {
            "title": "官方公告",
            "url": "https://www.gov.cn/a",
            "published_at": "2025-01-01",
            "snippet": "卖出印花税 0.05%",
        }
    ])
    second = FixtureProvider([])
    clear_web_cache()
    first_result = search_web("印花税", question="印花税", budget=WebSearchBudget(), provider=first)
    second_result = search_web("印花税", question="印花税", budget=WebSearchBudget(), provider=second)
    assert first_result.sources
    assert not second_result.sources

