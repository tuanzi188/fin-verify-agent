"""输入门控：观点/行情类请求与口语化查数的区分（routing_policy.initial_decision）。"""

from app.finance.routing_policy import initial_decision

DOCUMENTS = [
    {"company": "美的集团", "period": "2025", "code": "000333"},
    {"company": "比亚迪", "period": "2025", "code": "002594"},
    {"company": "贵州茅台", "period": "2025", "code": "600519"},
]


def decide(query: str, previous: str = ""):
    return initial_decision(query, DOCUMENTS, previous)


def test_opinion_requests_stop_before_retrieval():
    # 明确建议/预测走 opinion（可答 + 免责）；估值类词仍走澄清。
    opinion_queries = (
        "茅台股票现在能买吗？",
        "比亚迪 2025 年股价走势如何？",
    )
    clarify_queries = (
        "你觉得美的集团这家公司怎么样？",
        "美的集团 2025 年市值是多少？",
    )
    for query in opinion_queries:
        decision = decide(query)
        assert decision.initial_path == "opinion", query
        assert decision.rule_id == "opinion_intent", query
    for query in clarify_queries:
        decision = decide(query)
        assert decision.initial_path == "clarify", query
        assert decision.rule_id == "out_of_scope_intent", query


def test_colloquial_lookup_is_not_blocked():
    """「营收怎么样」是口语化查数，不能当成观点拦截。"""
    decision = decide("美的集团 2025 年营业收入怎么样？")
    assert decision.initial_path == "structured_rag"

    decision = decide("比亚迪 2025 年的利润怎么样？")
    assert decision.initial_path == "structured_rag"


def test_existing_boundary_rules_keep_priority():
    """未来期间与口径歧义先于观点规则——任务金标 FIN-14/FIN-20 依赖此顺序。"""
    decision = decide("券商对比亚迪 2026 年的目标价是多少？")
    assert decision.rule_id == "scope_boundary"
    assert decision.initial_path == "abstain"

    decision = decide("比亚迪的利润情况怎么样？")
    assert decision.rule_id == "ambiguous_metric"


def test_regular_lookup_unaffected():
    decision = decide("美的集团 2025 年营业收入是多少？")
    assert decision.initial_path == "structured_rag"


# ── 真实对抗集（2026-11-03）：散户口吻的观点换皮说法 ──────────────


def test_retail_opinion_paraphrases_are_blocked():
    opinion_queries = (
        "比亚迪这波还能追吗",
        "美的值不值得长期持有",
        "茅台后市怎么走",
        "帮我看看茅台有没有投资价值",
        "茅台 2025 年能涨到多少",
        "现在抄底美的合适吗",
    )
    clarify_queries = (
        "茅台股息率高吗",
        "美的和比亚迪哪个好",
    )
    for query in opinion_queries:
        decision = decide(query)
        assert decision.initial_path == "opinion", query
        assert decision.rule_id == "opinion_intent", query
    for query in clarify_queries:
        decision = decide(query)
        assert decision.initial_path == "clarify", query
        assert decision.rule_id == "out_of_scope_intent", query


def test_bond_and_equity_terms_are_not_opinion():
    """债券条款与股权激励是合法披露项，不能因为带「评级/转股/行权」被拦。"""
    for query in (
        "公司债券的信用评级是多少？",
        "股权激励计划的行权价格和授予价格是多少？",
        "可转债的转股价格是多少？",
    ):
        decision = decide(query)
        assert decision.initial_path == "structured_rag", query


def test_unknown_entity_without_year_is_detected():
    """「格力电器的分红派息」没有年份，也不能拿库里别家数据作答。"""
    decision = decide("格力电器的分红派息")
    assert decision.initial_path == "abstain"
    assert "格力电器" in decision.reason


def test_contextless_followup_asks_for_scope():
    decision = decide("那 2024 年呢？")
    assert decision.initial_path == "clarify"
    assert decision.rule_id == "missing_scope"


def test_latest_period_maps_to_corpus_year():
    """「最新一期」确定性映射到语料最新年份，不进 LLM，也不澄清。"""
    decision = decide("比亚迪最新一期的研发投入是多少？")
    assert decision.initial_path == "structured_rag"
    assert "2025 年" in decision.resolved_query


def test_ambiguous_profit_keeps_existing_policy():
    """净利润单独出现仍走既有的口径澄清（产品策略，不是误杀）。"""
    decision = decide("帮我查下比亚迪 2024 年的净利润")
    assert decision.rule_id == "ambiguous_metric"


def test_quarterly_disclosed_metric_uses_annual_report():
    """年报分季度披露的指标（营业收入/归母净利润/现金流）不因缺季报拒答。"""
    decision = decide("比亚迪 2025 年第四季度营业收入占全年营业收入的比重是多少？")
    assert decision.initial_path in ("agent", "structured_rag")


def test_quarterly_undisclosed_metric_still_abstains():
    """毛利率没有按季披露，季度毛利率问题维持边界拒答。"""
    decision = decide("美的集团 2025 年第二季度的毛利率是多少？")
    assert decision.initial_path == "abstain"
    assert decision.rule_id == "scope_boundary"
