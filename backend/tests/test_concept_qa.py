"""金融概念问答：路由判定、服务层兜底与提示词约束。全部离线。"""

from __future__ import annotations

import pytest

from app.finance.query_scope import prepare_finance_scope
from app.finance.routing_policy import concept_intent, initial_decision
from app.services.concept_qa import (
    CONCEPT_SYSTEM,
    RATE_SINGLE_SOURCE,
    RATE_UNAVAILABLE,
    _build_queries,
    _select_sources,
    answer_concept,
)
from app.services.web_search import FixtureProvider, WebSearchBudget, WebSearchError

_DOCS = [{"company": "美的集团", "code": "000333", "period": "2025"}]

_GOOD_ANSWER = "毛利率 = 毛利 ÷ 营业收入，衡量收入扣除营业成本后剩余的比例。"


def _fake_chat(reply: str, captured: dict | None = None):
    async def chat(messages, **kwargs):
        if captured is not None:
            captured["messages"] = messages
            captured["kwargs"] = kwargs
        return reply, None, {"prompt_tokens": 11, "completion_tokens": 22}

    return chat


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "什么是毛利率",
        "商誉是什么意思",
        "可转债是什么",
        "ROE 怎么算",
        "存货周转率反映什么",
        "毛利率和净利率有什么区别",
        "EBITDA 是什么",
        "市盈率怎么算",
        "什么是杜邦分析",
    ],
)
def test_definition_questions_route_to_concept(query: str):
    assert concept_intent(query, _DOCS) is True
    assert initial_decision(query, _DOCS).initial_path == "concept"


@pytest.mark.parametrize(
    "query",
    [
        # 带公司/期间 = 查证题，不是概念题
        "美的集团 2025 年营业收入是多少",
        "美的集团 2025 年营业收入是什么",
        # 索要具体数值
        "毛利率多少",
        # 归因题，走原有路径
        "为什么美的营收增长",
        # 非金融概念
        "什么是量子计算",
        # 观点题
        "格力电器怎么样",
    ],
)
def test_lookup_and_opinion_questions_are_not_concept(query: str):
    assert concept_intent(query, _DOCS) is False
    assert initial_decision(query, _DOCS).initial_path != "concept"


def test_what_is_no_longer_treated_as_a_company():
    """「什么是毛利率」曾把「什么是」当主体，直接拒答（2026-11-06 实测）。"""
    scope = prepare_finance_scope("什么是毛利率", _DOCS)
    assert scope.blocked_reason == ""
    assert list(scope.companies) == []


@pytest.mark.parametrize(
    "query",
    [
        "A股卖出印花税现在是多少",
        "股票卖出印花税是多少",
        "印花税怎么算",
        "现在股票交易过户费是多少",
        "券商佣金一般多少",
    ],
)
def test_rate_questions_force_web_instead_of_refusing(query: str):
    """费率会变（印花税 2023 年减半），必须查当下资料，不能落进 out_of_scope。"""
    decision = initial_decision(query, _DOCS)
    assert decision.initial_path == "concept"
    assert decision.must_web is True
    assert decision.rule_id == "web_fact_question"


def test_rate_terms_do_not_turn_definitions_into_rate_lookups():
    """「什么是印花税」是概念题，不需要强制联网。"""
    decision = initial_decision("什么是印花税", _DOCS)
    assert decision.initial_path == "concept"
    assert decision.must_web is False


# ---------------------------------------------------------------------------
# 服务层
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_answer_concept_returns_plain_answer_without_evidence():
    captured: dict = {}
    response = await answer_concept(
        query="什么是毛利率",
        conversation_id="c1",
        route_reason="金融概念/知识问题，不检索本地材料",
        chat=_fake_chat(_GOOD_ANSWER, captured),
    )
    assert response.status == "answered"
    assert response.strategy == "concept"
    assert response.variant == "concept"
    assert response.citations == []
    assert response.claims == []
    assert response.calc == []
    assert response.answer == _GOOD_ANSWER
    assert "concept:no_retrieval" in response.notes
    # 送进模型的是概念提示词，不是查证规则
    assert captured["messages"][0]["content"] == CONCEPT_SYSTEM


@pytest.mark.asyncio
async def test_answer_with_numbers_is_kept_but_noted():
    """例子和公式里的数字是常见写法：留痕即可，不阻断交付。"""
    response = await answer_concept(
        query="什么是毛利率",
        conversation_id="c1",
        chat=_fake_chat("毛利率一般在 30% 左右，越高越好。"),
    )
    assert response.status == "answered"
    assert "concept:contains_number" in response.notes


@pytest.mark.asyncio
async def test_answer_with_reference_marker_is_downgraded_to_review():
    """文档坐标是红线：出现就说明这段文字在冒充查证结论。"""
    response = await answer_concept(
        query="什么是毛利率",
        conversation_id="c1",
        chat=_fake_chat("毛利率 = 毛利 ÷ 营业收入[1]。"),
    )
    assert response.status == "needs_review"
    assert "concept:contains_reference_or_locator" in response.notes


@pytest.mark.asyncio
async def test_empty_answer_is_an_error_not_a_silent_pass():
    """flash 档 reasoning 吃满预算时会返回空正文（2026-11-06 实测）。"""
    response = await answer_concept(
        query="可转债怎么转股",
        conversation_id="c1",
        chat=_fake_chat(""),
    )
    assert response.status == "error"
    assert "concept:empty_answer" in response.notes


@pytest.mark.asyncio
async def test_uncertain_answer_is_abstained():
    response = await answer_concept(
        query="什么是杜邦分析",
        conversation_id="c1",
        chat=_fake_chat("这个概念我无法给出可靠解释"),
    )
    assert response.status == "abstained"
    assert "concept:abstained" in response.notes


@pytest.mark.asyncio
async def test_out_of_scope_answer_is_abstained():
    response = await answer_concept(
        query="什么是量子计算",
        conversation_id="c1",
        chat=_fake_chat("这个问题不在金融概念范围内"),
    )
    assert response.status == "abstained"


@pytest.mark.asyncio
async def test_model_failure_is_reported_as_error():
    async def broken(messages, **kwargs):
        raise RuntimeError("boom")

    response = await answer_concept(
        query="什么是毛利率",
        conversation_id="c1",
        chat=broken,
    )
    assert response.status == "error"
    assert "concept:generation_failed" in response.notes


# ---------------------------------------------------------------------------
# 提示词纪律
# ---------------------------------------------------------------------------


def test_concept_prompt_keeps_the_hard_constraints():
    assert len(CONCEPT_SYSTEM) < 900
    assert "没有资料依据的具体数值一律不写" in CONCEPT_SYSTEM
    assert "不写 [n]、页码、表名、表 id" in CONCEPT_SYSTEM
    assert "无法给出可靠解释" in CONCEPT_SYSTEM
    assert "不在金融概念范围内" in CONCEPT_SYSTEM
    assert "不用标题、表格、代码块" in CONCEPT_SYSTEM


# ---------------------------------------------------------------------------
# 联网增强
# ---------------------------------------------------------------------------


_WEB_ITEMS = [
    {
        "url": "https://www.cs.com.cn/a",
        "title": "可转债转股规则说明",
        "publishedDate": "2025-11-01T00:00:00Z",
        "text": "可转债在转股期内可按约定价格转换为股票。",
    }
]

# 费率题至少要两个独立来源，或有官方源；这里给一条 gov.cn 官方件。
_RATE_ITEMS = [
    {
        "url": "https://www.gov.cn/zhengce/a",
        "title": "证券交易印花税调整公告",
        "publishedDate": "2023-08-28T00:00:00Z",
        "text": "自 2023 年 8 月 28 日起，证券交易印花税由千分之一减半为万分之五。",
    }
]


@pytest.mark.asyncio
async def test_web_sources_are_injected_with_attribution_rules():
    captured: dict = {}
    response = await answer_concept(
        query="可转债怎么转股",
        conversation_id="c1",
        allow_web=True,
        provider=FixtureProvider(_WEB_ITEMS),
        budget=WebSearchBudget(),
        chat=_fake_chat(
            "可转债在转股期内可按约定价格转换为股票（中国证券报，11-01）。",
            captured,
        ),
    )
    system = captured["messages"][0]["content"]
    # 资料进了上下文，标注规则也进了提示词
    assert "可转债转股规则说明" in system
    assert "（来源名，月-日）" in system
    assert response.status == "answered"
    assert "concept:web" in response.notes
    assert "concept:web_used" in response.notes
    assert response.token_usage["web_queries"] == 1
    # 来源要能回到前端展示，不能只留在上下文里
    assert response.web_sources[0]["label"] == "中国证券报"
    assert response.web_sources[0]["url"] == "https://www.cs.com.cn/a"


def test_lead_is_split_from_heading_and_body():
    """开头先给定论：跳过标题，取第一段；恢复全文由前端负责。"""
    from app.services.concept_qa import _split_lead

    answer = "# 商誉\n\n商誉是并购时多付的那部分溢价。\n\n## 细节\n\n后续解释。"
    assert _split_lead(answer) == "商誉是并购时多付的那部分溢价。"


def test_single_paragraph_answer_has_no_separate_lead():
    from app.services.concept_qa import _split_lead

    assert _split_lead("毛利率是毛利占营业收入的比例。") == ""


@pytest.mark.asyncio
async def test_web_searched_but_unused_is_marked():
    """搜到了资料却没用：不能打「+公开信息」的标，只能算通用知识。"""
    captured: dict = {}
    response = await answer_concept(
        query="可转债怎么转股",
        conversation_id="c1",
        allow_web=True,
        provider=FixtureProvider(_WEB_ITEMS),
        chat=_fake_chat("可转债在转股期内可以转换为股票。", captured),
    )
    assert "concept:web_unused" in response.notes
    assert "concept:web_used" not in response.notes


@pytest.mark.asyncio
async def test_web_disabled_never_calls_the_provider():
    class _Boom:
        name = "boom"

        def search(self, *args, **kwargs):
            raise AssertionError("allow_web=False 时不应联网")

    captured: dict = {}
    response = await answer_concept(
        query="什么是毛利率",
        conversation_id="c1",
        allow_web=False,
        provider=_Boom(),
        chat=_fake_chat(_GOOD_ANSWER, captured),
    )
    assert captured["messages"][0]["content"] == CONCEPT_SYSTEM
    assert "concept:no_retrieval" in response.notes
    assert response.token_usage["web_queries"] == 0


@pytest.mark.asyncio
async def test_web_failure_falls_back_to_knowledge():
    class _Failing:
        name = "failing"

        def search(self, *args, **kwargs):
            raise WebSearchError("offline")

    captured: dict = {}
    response = await answer_concept(
        query="什么是毛利率",
        conversation_id="c1",
        allow_web=True,
        provider=_Failing(),
        chat=_fake_chat(_GOOD_ANSWER, captured),
    )
    assert "concept:web_failed" in response.notes
    assert captured["messages"][0]["content"] == CONCEPT_SYSTEM
    assert response.status == "answered"


@pytest.mark.asyncio
async def test_web_without_sources_falls_back_to_knowledge():
    captured: dict = {}
    response = await answer_concept(
        query="什么是毛利率",
        conversation_id="c1",
        allow_web=True,
        provider=FixtureProvider([]),
        chat=_fake_chat(_GOOD_ANSWER, captured),
    )
    assert "concept:web_empty" in response.notes
    assert captured["messages"][0]["content"] == CONCEPT_SYSTEM


@pytest.mark.asyncio
async def test_invented_source_name_is_downgraded():
    """联网模式下写了资料里没有的来源名，就是编来源。"""
    response = await answer_concept(
        query="可转债怎么转股",
        conversation_id="c1",
        allow_web=True,
        provider=FixtureProvider(_WEB_ITEMS),
        chat=_fake_chat("可转债在转股期内可以转为股票（某某财经，11-01）。"),
    )
    assert response.status == "needs_review"
    assert "concept:unknown_source" in response.notes


# ---------------------------------------------------------------------------
# 费率 / 规则题：必须联网，不许凭记忆
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_question_searches_even_when_web_switch_is_off():
    captured: dict = {}
    response = await answer_concept(
        query="股票卖出印花税是多少",
        conversation_id="c1",
        allow_web=False,
        must_web=True,
        provider=FixtureProvider(_RATE_ITEMS),
        chat=_fake_chat(
            "卖出单边征收，成交金额的 0.5‰（中国政府网，08-28）。",
            captured,
        ),
    )
    assert "concept:web_fact" in response.notes
    assert "concept:web_forced" in response.notes
    assert "证券交易印花税调整公告" in captured["messages"][0]["content"]
    assert response.status == "answered"


@pytest.mark.asyncio
async def test_rate_question_with_single_unofficial_source_abstains():
    """没有官方源、又只有一家媒体时，不算交叉验证通过。"""

    async def boom(messages, **kwargs):
        raise AssertionError("交叉验证不通过时不应调用模型")

    response = await answer_concept(
        query="股票卖出印花税是多少",
        conversation_id="c1",
        must_web=True,
        provider=FixtureProvider(_WEB_ITEMS),  # 只有 cs.com.cn 一条
        chat=boom,
    )
    assert response.status == "abstained"
    assert response.answer == RATE_SINGLE_SOURCE
    assert "concept:single_source" in response.notes


def test_rate_query_is_rewritten_to_keywords():
    queries = _build_queries("A股卖出印花税现在是多少", must_web=True)
    assert queries[0] == "A股卖出印花税 最新规定"
    assert queries[1] == "A股卖出印花税 官方公告"
    # 概念题保持自然问句
    assert _build_queries("什么是商誉", must_web=False) == ["什么是商誉"]


def test_tertiary_sources_only_fill_when_primary_is_short():
    from app.services.web_search import WebSource

    def source(tier: int, name: str) -> WebSource:
        return WebSource(
            url=f"https://{name}.example/a",
            domain=f"{name}.example",
            title=name,
            published_at="2025-01-01",
            snippet="",
            tier=tier,
        )

    alone = _select_sources([source(2, "media")], allow_t3=True)
    assert [s.tier for s in alone] == [2]

    with_tertiary = _select_sources(
        [source(2, "media"), source(3, "blog"), source(3, "blog2")],
        allow_t3=True,
    )
    assert [s.tier for s in with_tertiary] == [2, 3, 3]
    # 费率题不接受 T3
    assert [s.tier for s in _select_sources(
        [source(2, "media"), source(3, "blog")], allow_t3=False
    )] == [2]


@pytest.mark.asyncio
async def test_rate_question_without_sources_abstains_without_model_call():
    """查不到就直说查不到；绝不拿记忆补费率。"""

    async def boom(messages, **kwargs):
        raise AssertionError("费率题没资料时不应调用模型")

    response = await answer_concept(
        query="股票卖出印花税是多少",
        conversation_id="c1",
        must_web=True,
        provider=FixtureProvider([]),
        chat=boom,
    )
    assert response.status == "abstained"
    assert response.answer == RATE_UNAVAILABLE
    assert "concept:web_empty" in response.notes
    assert response.token_usage["llm_calls"] == 0


@pytest.mark.asyncio
async def test_rate_answer_without_source_stays_answered_but_noted():
    """费率答案没标来源不降级：未核实由标签和来源列表承担，状态只反映能否交付。"""
    response = await answer_concept(
        query="股票卖出印花税是多少",
        conversation_id="c1",
        must_web=True,
        provider=FixtureProvider(_RATE_ITEMS),
        chat=_fake_chat("卖出单边征收，成交金额的 0.5‰。"),
    )
    assert response.status == "answered"
    assert "concept:unattributed_number" in response.notes
