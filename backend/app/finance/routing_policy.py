"""分层路由判据：问题操作、证据充分性与可修复升级分别判断。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.finance.calculation_records import record_number_tokens
from app.finance.query_scope import (
    prepare_finance_scope,
    scope_companies,
    scope_quarter,
    scope_years,
)
from app.finance.refusal import from_rule_id
from app.models.finance import FinanceChatResponse, FinanceCitation

POLICY_VERSION = "finance-routing-v2"

# 明确操作与指标名称分开；只问披露的同比、占比不要求计算。
OPERATION_RULES = (
    (
        "calculate",
        r"自行|验算|计算|算一遍|推算|折合|换算|÷|平均每年|合计是多少|增加多少|减少多少|多多少|差多少|相差|高多少|低多少|变化多少|下降多少|超过\s*\d|比重",
    ),
    (
        "reconcile",
        r"勾稽|是否一致|是否相同|核对|核实|确认这个说法|解释.*(?:原因|差异)|差异.*原因",
    ),
    ("rank", r"排名|怎么排|从高到低|从低到高|哪.*(?:更快|更高|最高|最低|唯一)"),
)

METRIC_RULES = (
    ("营业收入", ("营业收入", "营收")),
    ("营业成本", ("营业成本",)),
    (
        "归母净利润",
        ("归母净利润", "归属于上市公司股东的净利润", "归属于母公司所有者的净利润"),
    ),
    ("净利润", ("净利润",)),
    ("经营现金流", ("经营活动产生的现金流量净额", "经营活动现金流量净额")),
    ("每股收益", ("基本每股收益",)),
    ("净资产收益率", ("加权平均净资产收益率",)),
    ("研发费用", ("研发费用",)),
    ("研发投入", ("研发投入",)),
    ("毛利率", ("毛利率",)),
    ("净利率", ("净利率",)),
)

# 财报查证要素：命中任一即视为本产品目标域的问题。
# 判据刻意放宽——门控只挡"完全与财报无关"的输入（闲聊、写诗、天气），
# 不负责精确分类；放行后检索不到证据仍会走拒答。
# 2026-11-03 补：债券条款（募集说明书已在语料里）与公司治理/股本类披露词，
# 否则"债券的信用评级""股权激励行权价格"这类合法问题会被域门控误杀。
FINANCE_DOMAIN_HINTS = (
    "年报", "财报", "报表", "披露", "附注", "审计", "募集", "招股",
    "财务", "会计", "营收", "营业收入", "营业成本", "成本", "利润",
    "现金流", "资产", "负债", "所有者权益", "毛利率", "净利率",
    "每股收益", "净资产收益率", "研发", "分红", "股息", "回购",
    "商誉", "存货", "应收", "应付", "周转率", "资产负债率",
    "流动比率", "速动比率", "同比", "环比", "占比", "勾稽",
    "债券", "票面利率", "信用评级", "增信", "担保", "募集资金",
    "发行规模", "赎回", "回售", "转股", "可转债", "行权", "期权",
    "股权激励", "授予价格", "增持", "减持", "股本", "股东",
    "董事", "监事", "高管", "股东大会", "员工人数",
)

_DOC_REFERENCE = re.compile(
    r"这份|该(?:文档|材料|报告|年报|报表)|本地(?:材料|文档|语料)|材料里|文档里|报告里|年报里"
)
# 追问前缀：没有上文时这类输入缺主体/指标，先问清楚再检索。
_FOLLOWUP_PREFIX = re.compile(r"^(?:那|那么|还有|它|该公司|这家|再|另外|并且)")

# 不可查证的请求形态：行情、估值、预测、投资建议。产品只回查定期报告披露事实，
# 观点类问题即使带公司名也不该进检索——下游没有任何证据能支撑观点。
# 词表按真实散户口吻扩过（2026-11-03 对抗集）：上车/追高/值不值得/后市/抄底…
# 两处刻意留口：「转股价」不算「股价」（可转债条款是合法披露项）；
# 「评级」只有不带信用/债券/主体/债项时才按观点处理（债券评级是可查事实）。
_UNAVAILABLE_INTENT = re.compile(
    r"能买|该买|值得买|值得投资|值不值得|值得持有|值得参与|能不能买|可不可以买|"
    r"该不该买|适不适合买|买入|卖出|加仓|减仓|建仓|仓位|止损|止盈|目标价|"
    r"(?<!转)股价|走势|会涨|会跌|涨跌|能涨|涨到|跌到|预测|估值|市值|市盈率|"
    r"市净率|股息率|投资建议|推荐买|推荐基金|推荐股票|投资价值|"
    r"怎么看|你觉得|上车|追高|追不追|还能追|后市|怎么走|抄底|梭哈|"
    r"持有还是卖|买哪只|选哪只|哪个好|哪家好|谁更好"
)

#: 明确要“观点/建议/预测”的问法。它们现在走 `opinion` 路径：
#: 可以答，但必须带服务端免责声明，且不得承诺收益或把推测写成披露事实。
#: 与 `_UNAVAILABLE_INTENT` 分开：后者还包含估值类词，仍走澄清。
_ADVICE_INTENT = re.compile(
    r"能买|该买|值得买|值得投资|值不值得|值得持有|值得参与|能不能买|可不可以买|"
    r"该不该买|该不该卖|适不适合买|买入|卖出|加仓|减仓|建仓|仓位|止损|止盈|目标价|"
    r"走势|会涨|会跌|涨跌|能涨|涨到|跌到|预测|投资建议|推荐买|推荐.{0,20}(?:基金|股票)|"
    r"选一只|挑一只|投资价值|上车|追高|追不追|还能追|后市|怎么走|抄底|梭哈|"
    r"持有还是卖|买哪只|选哪只|适合.{0,6}持有|长期持有|"
    r"估值(?:贵不贵|高不高|贵吗|便宜吗)|贵不贵|高估|低估"
)


# ---------------------------------------------------------------------------
# 概念 / 知识类问题（不检索、不引用）
# ---------------------------------------------------------------------------
# 判定必须四条同时成立，缺一条就可能把查证题误判成概念题：
# 1. 定义句式；2. 含金融术语；3. 不带公司/期间；4. 不索要具体数值。
# 只有第 1 条不够——「美的 2025 年营业收入是什么」是查证题，不是概念题。
_DEFINITION_PATTERN = re.compile(
    r"什么是|是什么|啥是|是啥|什么意思|啥意思|的含义|的定义|"
    r"怎么算|如何计算|怎么计算|如何算|"
    r"的区别|的差异|有何不同|有什么区别|"
    r"反映什么|说明什么|代表什么|指什么|包括哪些|包括什么"
)

# 概念题专用词表：补 FINANCE_DOMAIN_HINTS 没覆盖的估值与会计术语。
# 只收能给出稳定定义、且不需要落到具体报表行的词。
CONCEPT_TERMS = (
    "毛利率", "净利率", "毛利润", "营业利润", "利润总额",
    "市盈率", "市净率", "市销率", "股息率", "分红率", "派息率",
    "ROE", "ROA", "ROIC", "EPS", "EBITDA", "EBIT", "PE", "PB",
    "资产负债率", "流动比率", "速动比率", "存货周转率", "应收账款周转率",
    "总资产周转率", "权益乘数", "杜邦分析", "经营杠杆", "财务杠杆",
    "商誉", "无形资产", "递延所得税", "少数股东权益", "资本公积",
    "未分配利润", "盈余公积", "坏账准备", "存货跌价准备", "资产减值",
    "可转债", "转股价", "转股溢价率", "纯债价值", "回售", "赎回",
    "除权", "除息", "摊薄", "权益法", "成本法", "合并报表", "母公司报表",
    "经营活动现金流", "自由现金流", "净现比", "收现比",
    "同比", "环比", "复合增长率", "CAGR",
)

# 索要具体数值的表达：概念题可以问「怎么算」，不能问「是多少」。
_VALUE_ASK = re.compile(r"多少|几[个位年]|金额|数值|具体数字|数据是多少")

# 费率 / 交易规则类词表。这类事实会随时间变化（印花税 2023 年就从 1‰ 减半到 0.5‰），
# 只能联网核实，不能拿模型记忆作答。
RULE_TERMS = (
    "印花税", "过户费", "佣金", "手续费", "规费", "证管费", "经手费",
    "涨跌停", "涨跌幅限制", "T+1", "T+0", "交易时间", "集合竞价",
    "停牌", "复牌", "除权", "除息", "股权登记日", "分红派息",
    "转股期", "强赎", "回售", "下修",
    "科创板", "创业板", "北交所", "退市整理",
)
# 费率题的诉求形态：问数值、问怎么收、问现行规定。
_RULE_ASK = re.compile(
    r"多少|几[个位]|怎么收|怎么算|如何收|如何算|收费标准|费率|比例|"
    r"最新|现在|目前|现行|规定|规则|要交|交多少|收多少"
)


def web_fact_intent(query: str, documents: list[dict]) -> bool:
    """是否为「费率/规则现在是多少」这类公开事实题。

    与概念题的区别：定义是稳定的，可以靠知识；费率、期限、规则会变，
    只有检索到当下资料才算回答。带公司或报告期的问题仍走本地查证。
    """
    text = str(query or "").strip()
    if not text:
        return False
    lowered = text.lower()
    if not any(term.lower() in lowered for term in RULE_TERMS):
        return False
    if not _RULE_ASK.search(text):
        return False
    return not (
        scope_companies(text, documents) or scope_years(text) or scope_quarter(text)
    )


def concept_intent(query: str, documents: list[dict]) -> bool:
    """是否为金融概念/知识问题。

    这类问题没有可检索的披露对象：进检索只会拿回一堆无关段落再拒答，
    所以必须在范围检查之前单独分流。
    """
    text = str(query or "").strip()
    if not text or not _DEFINITION_PATTERN.search(text):
        return False
    if _VALUE_ASK.search(text):
        return False
    if scope_companies(text, documents) or scope_years(text) or scope_quarter(text):
        return False
    lowered = text.lower()
    if any(term.lower() in lowered for term in CONCEPT_TERMS):
        return True
    if any(term.lower() in lowered for term in RULE_TERMS):
        return True
    if requested_metrics(text):
        return True
    return any(word in text for word in FINANCE_DOMAIN_HINTS)


def has_finance_anchor(query: str, documents: list[dict]) -> bool:
    """问题是否带任何财报查证要素。

    要素 = 公司 / 报告期 / 指标 / 明确操作 / 对本地材料的指代 / 财报术语。
    一个都没有的输入（闲聊、写诗、天气）不应进入检索链路。
    """
    text = str(query or "").strip()
    if not text:
        return False
    if scope_companies(text, documents) or scope_years(text) or scope_quarter(text):
        return True
    if requested_metrics(text):
        return True
    if any(re.search(pattern, text) for _, pattern in OPERATION_RULES):
        return True
    if _DOC_REFERENCE.search(text):
        return True
    return any(word in text for word in FINANCE_DOMAIN_HINTS)


REPAIRABLE_ISSUES = frozenset(    {
        "retrieval_empty",
        "metric_missing",
        "period_missing",
        "unit_missing",
        "candidate_conflict",
        "numeric_mismatch",
        "citation_missing",
        "input_source_missing",
        "scope_ambiguous",
        "requires_calculation",
    }
)


@dataclass(frozen=True)
class InitialDecision:
    initial_path: str
    reason: str
    rule_id: str
    resolved_query: str
    #: 拒答分级编码；放行路径为空串（见 app/finance/refusal.py）
    reason_code: str = ""
    #: 费率/规则类事实题：会随时间变化，必须联网核实，不许凭记忆作答。
    must_web: bool = False


def requested_metrics(query: str) -> list[tuple[str, tuple[str, ...]]]:
    matches = [
        (name, aliases)
        for name, aliases in METRIC_RULES
        if any(alias in query for alias in aliases)
    ]
    if any(name == "归母净利润" for name, _ in matches):
        matches = [(name, aliases) for name, aliases in matches if name != "净利润"]
    if re.search(r"研发(?:投入|费用).*占营业收入", query):
        matches = [(name, aliases) for name, aliases in matches if name != "营业收入"]
    return matches


def initial_decision(
    query: str, documents: list[dict], previous_query: str = ""
) -> InitialDecision:
    # 概念题先于范围检查：prepare_finance_scope 会把「什么是」当成主体，
    # 一旦先跑它，概念题永远到不了这里（2026-11-06 实测）。
    # 费率/规则题优先：印花税「怎么算」既像定义又依赖现行费率，
    # 必须以联网结果为准，不能走纯知识路径。
    if web_fact_intent(query, documents):
        return InitialDecision(
            "concept",
            "费率/规则类事实，需公开信息核实",
            "web_fact_question",
            str(query or "").strip(),
            must_web=True,
        )
    if concept_intent(query, documents):
        return InitialDecision(
            "concept",
            "金融概念/知识问题，不检索本地材料",
            "concept_question",
            str(query or "").strip(),
        )
    scope = prepare_finance_scope(query, documents, previous_query)
    text = scope.resolved_query
    if scope.blocked_reason:
        return InitialDecision(
            "clarify" if scope.clarify else "abstain",
            scope.blocked_reason,
            "scope_boundary",
            text,
            scope.blocked_code or "out_of_scope",
        )
    # 明确要观点/建议/预测：可以答，但走独立 opinion 路径，必须带服务端免责声明。
    # 注意：带公司+指标+期间的披露题不会命中这里，仍走查证。
    if _ADVICE_INTENT.search(text):
        return InitialDecision(
            "opinion",
            "观点/建议类问题：回答为分析意见，不构成投资建议",
            "opinion_intent",
            text,
        )
    # 范围性拒答：问题没有公司、报告期、指标、操作或材料指代，说明不在本产品
    # 的目标域（闲聊/写作/通用问答），不进检索，直接引导到可查的句式。
    if not has_finance_anchor(text, documents):
        return InitialDecision(
            "clarify",
            "这里只查证本地财报材料；请提供公司、报告期或具体指标，"
            "例如「甲集团 2025 年营业收入」。",
            "out_of_scope",
            text,
            from_rule_id("out_of_scope"),
        )
    metrics = requested_metrics(text)
    # 查某一数值时不能静默选择公司、期间或含混的利润/现金流口径。
    if metrics and (not scope_companies(text, documents) or not scope_years(text)):
        return InitialDecision(
            "clarify", "请明确公司及报告期。", "missing_scope", text,
            from_rule_id("missing_scope"),
        )
    if re.search(r"利润情况|现金流是多少|(?<!净)利润是多少", text) or (
        "净利润" in text
        and "归母" not in text
        and "归属于" not in text
        and not any(word in text for word in ("合并", "母公司", "净利率"))
    ):
        return InitialDecision(
            "clarify", "请明确利润或现金流的指标口径。", "ambiguous_metric", text,
            from_rule_id("ambiguous_metric"),
        )
    # 观点/行情类问题哪怕带了公司名也没有证据可回查，先进边界说明而不是检索。
    # 「怎么样」单独出现时只在没有指标、没有期间时按观点处理——「营收怎么样」
    # 是口语化查数，不能拦。
    opinion_word = bool(_UNAVAILABLE_INTENT.search(text)) or (
        "评级" in text and not any(word in text for word in ("信用", "债券", "主体", "债项"))
    )
    if opinion_word or ("怎么样" in text and not metrics and not scope_years(text)):
        return InitialDecision(
            "clarify",
            "本产品只查证定期报告中的披露数字与原文，不提供行情、估值或投资建议；"
            "请改问具体的披露项，例如「甲集团 2025 年营业收入」。",
            "out_of_scope_intent",
            text,
            from_rule_id("out_of_scope_intent"),
        )
    # 追问前缀但没有主体和指标（"那 2024 年呢？"）——没东西可查，先问清楚。
    # 普通无主体查询（"2024 年情况怎么样"）维持既有策略放行，不在这里拦。
    if _FOLLOWUP_PREFIX.match(text) and not scope_companies(text, documents) and not metrics:
        return InitialDecision(
            "clarify", "请说明要查哪家公司、哪个指标。", "missing_scope", text,
            from_rule_id("missing_scope"),
        )
    for rule_id, pattern in OPERATION_RULES:
        if re.search(pattern, text):
            return InitialDecision(
                "agent",
                "问题明确要求"
                + {
                    "calculate": "计算或换算",
                    "reconcile": "核对或解释差异",
                    "rank": "排序或综合比较",
                }[rule_id],
                rule_id,
                text,
            )
    if len(scope_companies(text, documents)) > 1:
        return InitialDecision(
            "agent", "需要对齐多家公司披露口径。", "multiple_subjects", text
        )
    # 多期读数不天然是计算；先检索，是否覆盖全部期间由证据层决定。
    return InitialDecision(
        "structured_rag",
        "先一次结构检索读取披露值；指标名和问号数量不触发升级。",
        "lookup_first",
        text,
    )


def assess_evidence(query: str, citations: list[FinanceCitation], documents: list[dict] | None = None) -> dict:
    """只检查已给的证据，绝不查询金标；文本语义及未知指标返回 unknown。"""
    metrics = requested_metrics(query)
    years = scope_years(query)
    issues: list[dict] = []
    if not citations:
        issues.append({"code": "retrieval_empty", "reason": "一次检索未取得可用证据。"})
        return {"sufficient": False, "issues": issues, "rule_version": POLICY_VERSION}
    if not metrics or not years:
        return {
            "sufficient": None,
            "issues": [
                {
                    "code": "unsupported_evidence_form",
                    "reason": "当前确定性规则未覆盖该文本或指标，需要进一步读取与核对。",
                }
            ],
            "rule_version": POLICY_VERSION,
        }
    inventory = documents if documents is not None else [
        {"company": citation.evidence.company} for citation in citations
        if citation.evidence.company
    ]
    subjects = set(scope_companies(query, inventory))
    complete = True
    for required_scope in ("合并", "母公司", "调整后", "调整前"):
        if required_scope in query and not any(
            required_scope
            in (
                " ".join(citation.evidence.path)
                + (citation.evidence.table_caption or "")
                + (citation.evidence.row_name or "")
                + (citation.evidence.column or "")
            )
            for citation in citations
        ):
            issues.append(
                {
                    "code": "scope_ambiguous",
                    "reason": f"候选证据未明确标出所需的「{required_scope}」口径。",
                }
            )
    for metric_name, aliases in metrics:
        found: dict[str, set[tuple[str, str, str]]] = {}
        for citation in citations:
            evidence = citation.evidence
            if subjects and evidence.company not in subjects:
                continue
            preview = evidence.table_preview
            rows = []
            if preview:
                for row in preview.rows:
                    if evidence.row_indices and row.row not in evidence.row_indices:
                        continue
                    columns = preview.display_columns or preview.columns
                    for cell in row.cells:
                        if cell.column_index == 0 or (
                            evidence.column_indices
                            and cell.column_index not in evidence.column_indices
                        ):
                            continue
                        column = (
                            columns[cell.column_index]
                            if cell.column_index < len(columns)
                            else cell.column
                        )
                        rows.append(
                            (
                                row.row_name,
                                column,
                                cell.value,
                                evidence.unit or preview.unit,
                            )
                        )
            elif evidence.cell_text is not None:
                rows.append(
                    (
                        evidence.row_name or "",
                        evidence.column or "",
                        evidence.cell_text,
                        evidence.unit,
                    )
                )
            for label, column, value, unit in rows:
                compact_label = re.sub(r"\s+", "", label)
                if not any(alias in compact_label for alias in aliases):
                    continue
                # 不能拿收入行的同比列回答收入金额，反之亦然。
                wants_growth = any(
                    word in query for word in ("同比", "增减", "变动比例", "增速")
                )
                is_growth = any(word in column for word in ("同比", "增减", "变动"))
                wants_ratio = any(
                    word in query for word in ("占比", "比重", "比例", "占营业")
                )
                row_ratio = any(word in label for word in ("占", "比重", "比例"))
                if row_ratio != wants_ratio and metric_name in ("研发费用", "研发投入"):
                    continue
                cell_numbers = record_number_tokens(value or "")
                if len(cell_numbers) != 1 or (is_growth and not wants_growth):
                    continue
                contextual_unit = "%" if any(token in column + label for token in ("%", "％")) else unit
                value_unit = cell_numbers[0][1] or contextual_unit
                if is_growth and value_unit != "%":
                    value_unit = ""
                if not value_unit:
                    issues.append(
                        {
                            "code": "unit_missing",
                            "reason": f"{metric_name}候选单元格缺少明确单位。",
                        }
                    )
                    continue
                cell_years = scope_years(column)
                if is_growth:
                    cell_years = {evidence.period} if wants_growth else set()
                for year in cell_years or ({evidence.period} if not years else set()):
                    if years and year not in years:
                        continue
                    key = f"{evidence.company}:{year}:{'growth' if is_growth else 'value'}"
                    found.setdefault(key, set()).add(
                        (str(cell_numbers[0][0]), value_unit, str(evidence.doc_id))
                    )
        needed_years = years or {""}
        needed_subjects = subjects or {""}
        needs_growth = any(
            word in query for word in ("同比", "增减", "变动比例", "增速")
        )
        growth_only = needs_growth and not re.search(
            r"收入是多少|利润是多少|净额是多少", query
        )
        kinds = (
            ["growth"]
            if growth_only
            else (["value", "growth"] if needs_growth else ["value"])
        )
        for subject in needed_subjects:
            for year in needed_years:
                for kind in kinds:
                    matches = [
                        value
                        for key, values in found.items()
                        if key.endswith(f":{year}:{kind}")
                        and (not subject or key.startswith(subject + ":"))
                        for value in values
                    ]
                    if not matches:
                        complete = False
                        issues.append(
                            {
                                "code": "metric_missing",
                                "reason": f"证据尚未覆盖{subject} {year} {metric_name}（{'披露增减幅度' if kind == 'growth' else '披露值'}）。",
                            }
                        )
                    if len({(value, unit) for value, unit, _ in matches}) > 1:
                        complete = False
                        issues.append(
                            {
                                "code": "candidate_conflict",
                                "reason": f"{subject} {year} {metric_name}存在不同候选值或单位，需核对口径。",
                            }
                        )
    return {
        "sufficient": complete and not issues,
        "issues": issues,
        "rule_version": POLICY_VERSION,
    }


def upgrade_decision(
    status: str, issues: list[dict], *, scope_blocked: bool = False
) -> dict:
    """只有已枚举的可修复问题触发；故障、澄清和范围外材料不自动重跑。"""
    if scope_blocked or status in ("error", "clarified"):
        return {
            "upgrade": False,
            "reason": "范围缺失、待澄清或执行故障不由 Agent 自动重跑。",
            "codes": [],
        }
    repairable = [issue for issue in issues if issue.get("code") in REPAIRABLE_ISSUES]
    return {
        "upgrade": bool(repairable),
        "reason": "；".join(
            dict.fromkeys(str(issue["reason"]) for issue in repairable)
        ),
        "codes": list(dict.fromkeys(issue["code"] for issue in repairable)),
    }


def response_upgrade(response: FinanceChatResponse) -> dict:
    if not response.verification and response.status == "answered":
        from app.services.finance_validation import validate_finance_response

        validate_finance_response(response)
    issues = list(response.verification.get("issues") or [])
    evidence_check = response.verification.get("evidence_check") or {}
    issues.extend(evidence_check.get("issues") or [])
    # 兼容旧响应：明确的空检索可修复，生成/接口错误一律不重跑。
    if response.status == "abstained" and not response.citations:
        issues.append(
            {"code": "retrieval_empty", "reason": "结构检索没有取得可用证据。"}
        )
    return upgrade_decision(response.status, issues)
