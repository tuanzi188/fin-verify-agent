"""基于语料元数据解析主体、期间和追问条件，全部本地执行。"""

from __future__ import annotations

import re
from dataclasses import dataclass

_SCOPE_YEAR = re.compile(r"(?<!\d)(20\d{2})(?!\d)")
_SCOPE_QUARTER = re.compile(
    r"Q\s*([1-4])|第?([一二三四1234])季度|([一二三四1234])季报", re.IGNORECASE
)
_SCOPE_METRIC = re.compile(
    r"营业收入|营业成本|归母净利润|净利润|经营活动(?:产生的)?现金流量净额|现金流|毛利率|净利率|净资产收益率|每股收益|研发投入|总资产|总负债|收入|利润|资产|负债"
)


@dataclass(frozen=True)
class FinanceQuestionScope:
    resolved_query: str
    companies: tuple[str, ...] = ()
    years: tuple[str, ...] = ()
    quarter: str = ""
    blocked_reason: str = ""
    clarify: bool = False


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
            )
        ):
            return before_metric
    return ""


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
                )
            if previous_companies:
                companies = previous_companies
                resolved = companies[0] + " " + resolved
        if not years and len(previous_years) == 1:
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
                )
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
        )
    covered = [
        document
        for document in documents
        if not companies or str(document.get("company")) in companies
    ]
    if companies and not covered:
        return FinanceQuestionScope(
            **base, blocked_reason="本地语料未覆盖本轮指定的公司。"
        )
    # 年报可包含上年对比值，不因缺上年独立文档就拒答；只拦截超出最新覆盖的未来期间。
    available_years = (
        set().union(
            *(scope_years(str(document.get("period") or "")) for document in covered)
        )
        if covered
        else set()
    )
    if years and available_years and max(years) > max(available_years):
        return FinanceQuestionScope(
            **base,
            blocked_reason=f"本地最新覆盖 {max(available_years)} 年，无法查证指定期间。",
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
        if not quarter_documents:
            return FinanceQuestionScope(
                **base,
                blocked_reason=f"本地没有与指定期间匹配的 {quarter} 季报；现有年报不能直接替代该季度披露。",
            )
    return FinanceQuestionScope(**base)
