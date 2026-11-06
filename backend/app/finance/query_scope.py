"""基于语料元数据解析主体、期间和追问条件，全部本地执行。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

_SCOPE_YEAR = re.compile(r"(?<!\d)(20\d{2})(?!\d)")
_SCOPE_QUARTER = re.compile(
    r"Q\s*([1-4])|第?([一二三四1234])季度|([一二三四1234])季报", re.IGNORECASE
)
_SCOPE_METRIC = re.compile(
    r"营业收入|营业成本|归母净利润|净利润|经营活动(?:产生的)?现金流量净额|现金流|毛利率|净利率|净资产收益率|每股收益|研发投入|研发费用|总资产|总负债|资产负债率|应收账款|存货|商誉|股权激励|分红|派息|股息|转股|收入|利润|资产|负债"
)

# 槽位级覆盖用的「可直接落到表格行」的指标词表。
# 顺序有意义：归母净利润必须排在净利润前面，命中前者后不再重复生成后者。
_SLOT_METRIC_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("研发投入", ("研发投入", "研发费用", "研发支出", "研发相关", "研发")),
    ("归母净利润", ("归母净利润", "归属于上市公司股东的净利润", "归属于母公司所有者的净利润")),
    ("净利润", ("净利润",)),
    ("营业收入", ("营业收入", "营业总收入")),
    ("营业成本", ("营业成本", "营业总成本")),
    ("经营活动产生的现金流量净额", ("经营活动产生的现金流量净额", "经营现金流", "经营活动现金流")),
    ("总资产", ("总资产", "资产总计")),
    ("净资产", ("净资产",)),
    ("每股收益", ("每股收益",)),
)


def _all_occurrences_in_ratio_phrase(text: str, keyword: str) -> bool:
    """关键词的每一次出现是否都落在「X 率 / X 比例 / X 占比」里。

    「净资产收益率」不该生成「净资产」槽位，「研发投入占营业收入比例」也不该
    生成「研发投入」「营业收入」两个槽位——这些题要的是比率行，不是基础科目。
    但「归母净利率（归母净利润 ÷ 营业收入）」里的构成项不在比率短语内，要保留。
    """
    total = 0
    ratio = 0
    for match in re.finditer(re.escape(keyword), text):
        total += 1
        tail = text[match.end(): match.end() + 14]
        tail = re.split(r"[，。；？?]", tail, maxsplit=1)[0]
        if any(word in tail for word in ("率", "比例", "占比", "比重")):
            ratio += 1
    return total > 0 and ratio == total


def scope_metrics(question: str) -> list[str]:
    """问题点名、且能直接对应表格行的指标（去重、保留词表顺序）。

    只收可验证的报表科目，不把「净利率/毛利率」这类派生指标单独当槽位：
    它们由构成科目算出来，强行要求一行「净利率」会制造假缺口。
    """
    text = str(question or "")
    found: list[str] = []
    for metric, keywords in _SLOT_METRIC_KEYWORDS:
        matched_keywords = [word for word in keywords if word in text]
        if not matched_keywords:
            continue
        if all(
            _all_occurrences_in_ratio_phrase(text, word)
            for word in matched_keywords
        ):
            continue
        if metric == "净利润" and "归母净利润" in found:
            continue
        if metric == "研发投入" and "研发费用" in found:
            continue
        found.append(metric)
    return found


@dataclass(frozen=True)
class FinanceQuestionScope:
    resolved_query: str
    companies: tuple[str, ...] = ()
    years: tuple[str, ...] = ()
    quarter: str = ""
    blocked_reason: str = ""
    clarify: bool = False
    #: 拒答分级编码（见 app/finance/refusal.py）；空串表示本层放行。
    blocked_code: str = ""


def scope_years(question: str) -> set[str]:
    return set(_SCOPE_YEAR.findall(str(question or "")))


def scope_company_aliases(documents: list[dict]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for document in documents:
        company = str(document.get("company") or "").strip()
        if not company:
            continue
        aliases[company] = company
        shortened = re.sub(r"(?:股份有限公司|有限责任公司|有限公司)$", "", company)
        if len(shortened) >= 2:
            aliases[shortened] = company
        if shortened.endswith("集团") and len(shortened) > 3:
            aliases[shortened[:-2]] = company
        if shortened == "贵州茅台":
            aliases["茅台"] = company
        if shortened == "天顺风能":
            aliases["天顺"] = company
        code = str(document.get("code") or "")
        if re.fullmatch(r"\d{6}", code):
            aliases[code] = company
    return aliases


def scope_companies(question: str, documents: list[dict]) -> list[str]:
    return list(
        dict.fromkeys(
            company
            for alias, company in scope_company_aliases(documents).items()
            if (
                re.search(r"(?<!\d)" + re.escape(alias) + r"(?!\d)", question)
                if alias.isdigit()
                else alias in question
            )
        )
    )


def scope_quarter(question: str) -> str:
    matched = _SCOPE_QUARTER.search(question or "")
    if not matched:
        return ""
    value = next(value for value in matched.groups() if value)
    return "Q" + str("一二三四".index(value) + 1 if value in "一二三四" else value)


def unknown_scope_company(question: str, documents: list[dict]) -> str:
    """仅对明确的公司前缀、六位代码和公司全称做越界判断，不猜模糊实体。"""
    aliases = scope_company_aliases(documents)
    known = scope_companies(question, documents)
    unknown_codes = [
        code
        for code in re.findall(r"(?<!\d)\d{6}(?!\d)", question)
        if code not in aliases
    ]
    if unknown_codes:
        return unknown_codes[0]
    if known:
        remaining = question
        for alias in sorted(aliases, key=len, reverse=True):
            remaining = remaining.replace(alias, "")
        # 多公司问题仍检查剩余的明确主体，不因命中一家而跳过其他公司。
        extra = re.search(
            r"(?:和|与|及|、)\s*([\u4e00-\u9fffA-Za-z]{2,16})\s*(?=20\d{2})", remaining
        )
        return (
            extra.group(1) if extra and not _SCOPE_METRIC.search(extra.group(1)) else ""
        )
    prefix = re.split(r"20\d{2}", question, maxsplit=1)[0]
    prefix = re.sub(
        r"^(?:请问|请查一下|请查|帮我查一下|帮我查|查询|核查|查证|那|那么|关于)\s*",
        "",
        prefix,
    ).strip()
    # 主体紧贴年份且不含业务动词时，可以明确识别为未覆盖主体。
    if (
        scope_years(question)
        and re.fullmatch(r"[\u4e00-\u9fffA-Za-z]{2,20}", prefix)
        and not _SCOPE_METRIC.search(prefix)
        and not any(
            word in prefix
            for word in (
                "根据",
                "报告",
                "材料",
                "文档",
                "数据",
                "比较",
                "计算",
                "请",
                "本地",
                "公司",
                "最近",
            )
        )
    ):
        return prefix
    named = re.search(
        r"([\u4e00-\u9fffA-Za-z]{2,18}(?:股份有限公司|有限责任公司|有限公司))", question
    )
    if named:
        return named.group(1)
    metric = _SCOPE_METRIC.search(question)
    if metric:
        before_metric = question[: metric.start()].strip()
        before_metric = re.sub(r"^(?:请问|请查|查询|核查|查证)\s*", "", before_metric)
        # 「格力电器的分红派息」——主体和指标之间常带「的」，先剥掉再判。
        before_metric = before_metric.rstrip("的").strip()
        if re.fullmatch(r"[\u4e00-\u9fffA-Za-z]{2,16}", before_metric) and not any(
            word in before_metric
            for word in (
                "的",
                "年",
                "公司",
                "合并",
                "母公司",
                "本期",
                "上期",
                "当前",
                "本地",
                "各家",
                "全部",
                "材料",
                "报告",
                "增长",
                "减少",
                "分别",
                "比较",
                "最近",
                "一下",
                "总",
                "它",
                "那",
                "这",
                "该",
                "去年",
                "今年",
                "上年",
                "上一年",
                "前年",
                "本年",
                "当年",
                "最新",
                "同期",
                "可转债",
                "转债",
                "债券",
                "债",
                "股权激励",
                "期权",
                "行权",
                "授予",
                "回购",
                "分红",
                "派息",
                "股息",
                # 疑问词不是主体。「什么是毛利率」曾把「什么是」当成公司名，
                # 直接拒答「本地语料未覆盖主体『什么是』」（2026-11-06 实测）。
                "什么",
                "啥",
                "如何",
                "怎么",
                "为什么",
                "为何",
            )
        ):
            return before_metric
    return ""


# 年报「分季度主要财务指标」按季披露的指标：营业收入/归母净利润/经营现金流。
# 这类季度问题可以直接用年报回答；毛利率等未按季披露的才走"没有季报"边界。
_QUARTER_DISCLOSED_METRIC = re.compile(
    r"营业收入|营业总收入|归母净利润|归属于上市公司股东的净利润|"
    r"经营活动(?:产生的)?现金流量净额|经营现金流"
)


def _relative_year(question: str, available_years: set[str]) -> str:
    """把相对期间解析成具体年份；解析不了返回空串，交给上游澄清。"""
    match = re.search(r"去年|上一年|前年|今年|本年", question)
    if match:
        token = match.group()
        offset = {"前年": 2, "今年": 0, "本年": 0}.get(token, 1)
        return str(datetime.now().year - offset)
    if re.search(r"最新一期|最近一期|最新|最近", question) and available_years:
        return max(available_years)
    return ""


# 「近三年」「最近五年」「过去两年」：按语料最新覆盖年份倒推，不按模型训练截止时间。
# 倒推出的区间原样保留，不做裁剪——缺哪一年由证据层报缺口，比静默换成能查的年份诚实。
_RECENT_YEARS = re.compile(r"(?:近|最近|过去)\s*([一二三四五六七八九十\d]{1,2})\s*个?年")


def _small_int(token: str) -> int:
    """把「三」「十二」「20」这类个位到两位的中文/阿拉伯数字转成整数。"""
    token = (token or "").strip()
    if token.isdigit():
        return int(token)
    digits = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if token == "十":
        return 10
    if "十" in token:
        left, _, right = token.partition("十")
        return digits.get(left, 1 if not left else 0) * 10 + digits.get(right, 0)
    return digits.get(token, 0)


def _recent_year_span(question: str, available_years: set[str]) -> list[str]:
    """「近三年」→ ['2023', '2024', '2025']；超过十年不猜，交给上游澄清。"""
    match = _RECENT_YEARS.search(question or "")
    numeric_years = sorted(int(year) for year in available_years if year.isdigit())
    if not match or not numeric_years:
        return []
    count = _small_int(match.group(1))
    if not 1 <= count <= 10:
        return []
    end = numeric_years[-1]
    return [str(year) for year in range(end - count + 1, end + 1)]


# 相对季度：按当前日历解析，再交给覆盖检查决定能否回答。
# 「上一季度」会先被 _SCOPE_QUARTER 误读成「一季」= Q1，所以必须在它之前替换掉。
_RELATIVE_QUARTER = re.compile(r"上一?季度|上个季度|上季度|本季度|这个季度|当季")


def _relative_quarter(phrase: str, now: datetime | None = None) -> tuple[str, str]:
    moment = now or datetime.now()
    current = (moment.month - 1) // 3 + 1
    if phrase.startswith("本") or phrase in ("这个季度", "当季"):
        return str(moment.year), f"Q{current}"
    if current == 1:
        return str(moment.year - 1), "Q4"
    return str(moment.year), f"Q{current - 1}"


def prepare_finance_scope(
    query: str, documents: list[dict], previous_query: str = ""
) -> FinanceQuestionScope:
    """补全明确追问；主体或报告期越界时先返回边界说明。"""
    resolved = query.strip()
    companies = scope_companies(resolved, documents)
    years = scope_years(resolved)
    previous_companies = scope_companies(previous_query, documents)
    previous_years = scope_years(previous_query)
    relative = re.search(r"上一年|上年|去年|前一年|下一年|次年", resolved)
    followup = bool(
        re.search(
            r"^(?:那|那么|它|该公司|这家公司|再|还有|按|用|是|对)|上一年|上年|去年|前一年|下一年|次年",
            resolved,
        )
    )
    followup = followup or bool(
        previous_query and not _SCOPE_METRIC.search(query) and (companies or years)
    )
    current_unknown = unknown_scope_company(resolved, documents)
    followup = followup or bool(
        previous_query and re.search(r"同比|环比|增长率|占比|差额", query)
    )
    if previous_query and followup and not current_unknown:
        if relative and not years:
            if len(previous_years) != 1:
                return FinanceQuestionScope(
                    query,
                    blocked_reason="请确认基于哪一个报告期查询上一年或下一年。",
                    clarify=True,
                    blocked_code="ambiguous_scope",
                )
            offset = 1 if relative.group() in ("下一年", "次年") else -1
            inherited_year = str(int(next(iter(previous_years))) + offset)
            resolved = (
                resolved[: relative.start()]
                + inherited_year
                + " 年"
                + resolved[relative.end() :]
            )
            years = {inherited_year}
        if not companies:
            if len(previous_companies) > 1:
                return FinanceQuestionScope(
                    query,
                    blocked_reason="上一轮涉及多家公司，请确认本轮要查哪一家。",
                    clarify=True,
                    blocked_code="ambiguous_scope",
                )
            if previous_companies:
                companies = previous_companies
                resolved = companies[0] + " " + resolved
        # 「那近三年呢」「那上一季度呢」自带期间语义，不能被上一轮年份顶掉
        if (
            not years
            and len(previous_years) == 1
            and not _RECENT_YEARS.search(resolved)
            and not _RELATIVE_QUARTER.search(resolved)
        ):
            years = previous_years
            resolved = next(iter(years)) + " 年 " + resolved
        if not _SCOPE_METRIC.search(query):
            previous_metrics = list(
                dict.fromkeys(_SCOPE_METRIC.findall(previous_query))
            )
            if len(previous_metrics) == 1:
                resolved += "；查询指标：" + previous_metrics[0]
            elif len(previous_metrics) > 1:
                return FinanceQuestionScope(
                    query,
                    blocked_reason="上一轮涉及多个指标，请确认本轮要继续查询哪一个。",
                    clarify=True,
                    blocked_code="ambiguous_scope",
                )
    # 相对季度必须在 scope_quarter 之前替换：否则「上一季度」会被读成「一季」= Q1。
    relative_quarter = _RELATIVE_QUARTER.search(resolved)
    if relative_quarter:
        # 「2025 年上一季度」本身矛盾（相对季度以今天为基准），不猜，先澄清。
        if scope_years(query):
            return FinanceQuestionScope(
                query,
                blocked_reason="「上一季度」这类相对季度不能与指定年份同时使用，请确认要查的季度。",
                clarify=True,
                blocked_code="ambiguous_scope",
            )
        quarter_year, quarter_label = _relative_quarter(relative_quarter.group(0))
        resolved = (
            resolved[: relative_quarter.start()]
            + f"{quarter_year} 年 {quarter_label} "
            + resolved[relative_quarter.end() :]
        )
        years = {quarter_year}
    quarter = scope_quarter(resolved)
    base = {
        "resolved_query": resolved,
        "companies": tuple(companies),
        "years": tuple(sorted(years)),
        "quarter": quarter,
    }
    if current_unknown:
        return FinanceQuestionScope(
            **base,
            blocked_reason=f"本地语料未覆盖主体「{current_unknown}」，请提供对应材料后再查证。",
            blocked_code="out_of_scope",
        )
    covered = [
        document
        for document in documents
        if not companies or str(document.get("company")) in companies
    ]
    if companies and not covered:
        return FinanceQuestionScope(
            **base,
            blocked_reason="本地语料未覆盖本轮指定的公司。",
            blocked_code="out_of_scope",
        )
    # 年报可包含上年对比值，不因缺上年独立文档就拒答；只拦截超出最新覆盖的未来期间。
    available_years = (
        set().union(
            *(scope_years(str(document.get("period") or "")) for document in covered)
        )
        if covered
        else set()
    )
    # 相对期间确定性解析（零成本、可复现）：去年/前年按当前年份推算，
    # 「最新一期」映射到语料最新覆盖年份。真实用户高频省略年份，不能一律澄清。
    if not years:
        # 「近三年」先于「最近」：否则「最近」会把它解析成单一年份。
        span = _recent_year_span(resolved, available_years)
        if span:
            years = set(span)
            resolved = _RECENT_YEARS.sub("、".join(span) + " 年", resolved, count=1)
            base["resolved_query"] = resolved
            base["years"] = tuple(sorted(years))
        else:
            relative_year = _relative_year(resolved, available_years)
            if relative_year:
                years = {relative_year}
                resolved = f"{relative_year} 年 " + resolved
                base["resolved_query"] = resolved
                base["years"] = tuple(sorted(years))
    if years and available_years and max(years) > max(available_years):
        return FinanceQuestionScope(
            **base,
            blocked_reason=f"本地最新覆盖 {max(available_years)} 年，无法查证指定期间。",
            blocked_code="out_of_scope",
        )
    if quarter:
        quarter_documents = [
            document
            for document in covered
            if scope_quarter(
                str(document.get("period") or "")
                + " "
                + str(document.get("doc_type") or "")
                + " "
                + str(document.get("title") or "")
            )
            == quarter
            and (
                not years
                or bool(scope_years(str(document.get("period") or "")) & years)
            )
        ]
        # 季报没有，但年报的「分季度主要财务指标」披露了当季营业收入/
        # 归母净利润/经营现金流——这类问题能用年报回答；只有未按季披露的
        # 指标（如毛利率）才拒答。2026-11-03 修正：原规则一票拒答，把
        # FIN-09/CX-02 这类可答题也拦了。
        if not quarter_documents and not _QUARTER_DISCLOSED_METRIC.search(resolved):
            return FinanceQuestionScope(
                **base,
                blocked_reason=f"本地没有与指定期间匹配的 {quarter} 季报；现有年报不能直接替代该季度披露。",
                blocked_code="insufficient_evidence",
            )
    return FinanceQuestionScope(**base)
