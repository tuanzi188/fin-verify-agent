"""市场综述：联网检索 + 一次生成 + 服务端免责。

与 `finance_rag`（结构检索 + 查证）的分工：

- 查证回答数字，必须带页码/单元格坐标，走 `[n]`；
- 综述回答"市场怎么说"，来源是网页，**永不进 `[n]`**，只用「（来源名，日期）」标注。

纪律靠代码而不是提示词保证：
- 免责声明由服务端追加，模型不写；
- 来源名必须是检索结果里出现过的，模型不能自己造媒体名；
- 没有可用来源时不调用模型（否则它只会用记忆编）。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.agent.prompts import CORE_DISCIPLINE
from app.services import model_provider
from app.services.web_search import (
    WebSearchBudget,
    WebSearchError,
    WebSearchProvider,
    WebSearchResult,
    search_web,
    should_use_web,
)

logger = logging.getLogger(__name__)

DISCLAIMER = (
    "本内容由 AI 汇总公开信息生成，市场归因为各方说法，未经核实；"
    "不构成任何投资建议。"
)

SUMMARY_MAX_CHARS = 120
MAX_BRIEF_TOKENS = 2400
TEMPERATURE = 0.1
#: 来源日期只做展示，不做新旧自动告警：问 2024 年报时，2025 年初的报道
#: 本来就是"正确但已过时"，按天数判会全是噪音。日期异常由模型在「资料缺口」里指出。
MAX_INGEST_CANDIDATES = 5
#: 同一域名最多留几条。实测会出现 3 条新浪财经挤掉其他来源的情况，
#: 来源多样性直接影响"分歧"段能不能写出来。
MAX_PER_DOMAIN = 2
#: 被丢弃来源最多回显几条：取舍要说清楚，但没人在综述卡片里翻十几条链接。
MAX_SKIPPED_SOURCES = 10

#: 来源域名 → 展示名。只用于让模型"抄"正确的来源名；未知域名回退到主域。
SOURCE_LABELS: dict[str, str] = {
    "cninfo.com.cn": "巨潮资讯",
    "sse.com.cn": "上交所",
    "szse.cn": "深交所",
    "bse.cn": "北交所",
    "csrc.gov.cn": "证监会",
    "gov.cn": "中国政府网",
    "mof.gov.cn": "财政部",
    "chinatax.gov.cn": "国家税务总局",
    "stcn.com": "证券时报",
    "cs.com.cn": "中国证券报",
    "cnstock.com": "上海证券报",
    "caixin.com": "财新网",
    "yicai.com": "第一财经",
    "21jingji.com": "21财经",
    "jiemian.com": "界面新闻",
    "cls.cn": "财联社",
    "xinhuanet.com": "新华网",
    "news.cn": "新华网",
    "people.com.cn": "人民网",
    "cnr.cn": "央广网",
    "thepaper.cn": "澎湃新闻",
    "finance.sina.com.cn": "新浪财经",
    "finance.eastmoney.com": "东方财富",
    "wallstreetcn.com": "华尔街见闻",
}

MARKET_BRIEF_MODE = """你是财经信息综述助手。只能使用下面「资料」中的内容，
不能检索、不能补充记忆、不能给出投资建议。

【输出格式】固定五段，标题原样使用。

## 核心判断
1-3 句，≤120 字。第一句给机制结构，再给结论。
涉及营收/利润变化时，优先写成「量增、价减、单位盈利提升」这类结构短语，
或直接用恒等式：营收 = 销量 × 单价；利润 = 销量 × 单位毛利。
不涉及量价关系时，第一句写「多项因素叠加」「分歧较大」或「信息不足」。
不要写「多重共振」「共同作用」「综合影响」这类没有结构的并列。

## 分项
每条以**加粗的判断句**开头，后面跟 1-2 句依据；每条≤60 字（出处括号不计）。
格式：- **判断句**：依据。（来源名，月-日）
只使用资料中的因果与权重；资料没给权重就不写。
需要举例或推算时，必须标「（推算，非披露）」并写出用到的输入；不标注就当作事实。
需要举例时用「示例（非披露数据）」标明；示例里的数字不计入事实。

## 常见误读
只在资料明确反驳某个说法时才写，最多 3 条，格式：
- ❌ 错误说法 —— 资料中的反证
没有可靠反证就写「无」。

## 分歧
资料之间矛盾或侧重点不同的地方，逐条列出，每条≤100 字；没有写「无」。

## 资料缺口
回答该问题所缺的信息，1-3 条；没有写「无」。

【可读性】
1. 加粗只用于判断句和关键机制词，每条最多 2 处；不要整句加粗。
2. 不使用表格（窄屏会挤成一团）。
3. 出处写在句末括号里，不要插在句子中间。
4. 不用 emoji 装饰，❌/✅ 只用于「常见误读」。

【硬约束】
1. 不得输出目标价、评级、买卖建议、涨跌预测。
2. 不得使用「最核心 / 根本原因 / 主要推手」，除非同一句给出可数的依据。
3. 除明确标注「（推算，非披露）」的推算值外，任何具体数字必须能在资料原文中逐字找到；找不到就不写。
4. 资料日期早于事件时，在论断后标「（旧闻，YYYY-MM）」。
5. 不使用 [n] 编号；来源只用「（来源名，月-日）」标注。
6. 不要复述检索过程。
7. **多条来源说法一致不等于核实**。只能写「N 条来源提到同一数字」，
   不得写「可采信 / 可信 / 已证实 / 确认无误 / 可直接引用」。
8. 核心判断可以给关键数字，但必须能在资料原文中逐字找到；推算值必须标「（推算，非披露）」。
9. 若问的是单一数值而未找到官方披露，直接说「公开信息未提供可核对的原件」。
"""

MARKET_BRIEF_SYSTEM = CORE_DISCIPLINE + "\n\n" + MARKET_BRIEF_MODE

_SECTION_RE = re.compile(
    r"(?m)^#{1,4}\s*(核心判断|结论|分项|常见误读|分歧|资料缺口)\s*$"
)
_BULLET_RE = re.compile(r"^\s*[-·•*]\s*")
# 「（财联社，03-15）」或「(财联社, 3-15)」
_ATTRIBUTION_RE = re.compile(
    r"[（(]\s*([^（()），,]{1,20}?)\s*[，,]\s*(\d{1,2})\s*[-月]\s*(\d{1,2})\s*日?\s*[）)]"
)
# 旧闻标注单独识别，不参与来源名校验
_OLD_NEWS_RE = re.compile(r"[（(]\s*旧闻[，,]?\s*\d{0,4}-?\d{0,2}\s*[）)]")
_NUMBER_RE = re.compile(r"(?<![\d.])(\d[\d,]*(?:\.\d+)?)")
_BAD_TITLES = {"", "n/a", "none", "null", "-"}
_SUMMARY_KEYS = ("核心判断", "结论")

#: 「多家来源一致」经常被模型当成「已核实」，这是转载链最常见的幻觉。
_CONFIDENCE_WORDS = (
    "采信",  # 覆盖「可采信 / 直接采信 / 可以采信」
    "已证实",
    "确认无误",
    "完全可信",
)
#: 只有归因/解释类问题才走综述；纯数值题在本地缺失时应该拒答，而不是拿媒体数字顶上。
_QUESTION_INTENT_RE = re.compile(
    r"为什么|为何|原因|怎么看|如何看|如何理解|影响|大涨|大跌|上涨|下跌|"
    r"走强|走弱|异动|解读|复盘"
)


def should_brief_on_local_miss(question: str) -> bool:
    """本地没覆盖时，这类问题才允许转综述。

    纯数值题（"某公司某年营收多少"）的正确答案必须是一份可核对的原件；
    用媒体数字顶上去，等于把"未核实"包装成答案。
    """
    return bool(_QUESTION_INTENT_RE.search(question or ""))


@dataclass
class BriefSource:
    label: str
    domain: str
    url: str
    title: str
    published_at: str | None
    tier: int
    snippet: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "domain": self.domain,
            "url": self.url,
            "title": self.title,
            "published_at": self.published_at,
            "tier": self.tier,
            # 摘要原文一并透出：来源是否支撑那句话，用户要能自己看
            "snippet": self.snippet,
        }


@dataclass
class MarketBrief:
    ok: bool
    question: str
    summary: str = ""
    sections: dict[str, list[str]] = field(default_factory=dict)
    misreadings: list[str] = field(default_factory=list)
    divergences: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    sources: list[BriefSource] = field(default_factory=list)
    #: 检索到但因白名单/黑名单/缺日期被丢弃的来源（含原因），用于复核取舍
    skipped_sources: list[dict[str, str]] = field(default_factory=list)
    ingest_candidates: list[dict[str, Any]] = field(default_factory=list)
    raw: str = ""
    as_of: str = ""
    disclaimer: str = DISCLAIMER
    warnings: list[str] = field(default_factory=list)
    #: 实际发生的联网检索次数，用于"已查来源"回显
    queries_used: int = 0
    #: 实际发给检索服务的查询词。透明性要求：用户能复核"到底搜了什么"
    queries: list[str] = field(default_factory=list)
    provider: str = ""
    skipped_reason: str | None = None
    error: str | None = None
    token_usage: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "question": self.question,
            "summary": self.summary,
            "sections": self.sections,
            "misreadings": self.misreadings,
            "divergences": self.divergences,
            "gaps": self.gaps,
            "sources": [source.to_dict() for source in self.sources],
            "skipped_sources": self.skipped_sources,
            "ingest_candidates": self.ingest_candidates,
            "raw": self.raw,
            "as_of": self.as_of,
            "disclaimer": self.disclaimer,
            "warnings": self.warnings,
            "queries_used": self.queries_used,
            "queries": self.queries,
            "provider": self.provider,
            "skipped_reason": self.skipped_reason,
            "error": self.error,
            "token_usage": self.token_usage,
        }


def source_label(domain: str) -> str:
    host = (domain or "").lower().lstrip(".")
    if host in SOURCE_LABELS:
        return SOURCE_LABELS[host]
    parts = host.split(".")
    for size in range(len(parts) - 1, 1, -1):
        parent = ".".join(parts[-size:])
        if parent in SOURCE_LABELS:
            return SOURCE_LABELS[parent]
    return host


def build_queries(
    question: str, *, company: str | None = None, period: str | None = None
) -> list[str]:
    """最多两条：原问题一条、加"公告"一条。预算就是 2，不要多造。"""
    base = (question or "").strip()
    queries = [base] if base else []
    if company:
        variant = f"{company} {period} 公告" if period else f"{company} 公告"
        if variant not in queries:
            queries.append(variant)
    return queries[:2]


def render_sources(sources: Iterable[BriefSource]) -> str:
    blocks: list[str] = []
    for source in sources:
        date = (source.published_at or "")[:10] or "日期未知"
        blocks.append(
            f"- {source.label}｜{source.domain}｜{date}\n  {source.title}\n  {source.snippet}"
        )
    return "\n\n".join(blocks)


def split_sections(text: str) -> dict[str, str]:
    matches = list(_SECTION_RE.finditer(text or ""))
    if not matches:
        return {}
    sections: dict[str, str] = {}
    for position, match in enumerate(matches):
        start = match.end()
        end = matches[position + 1].start() if position + 1 < len(matches) else len(text)
        sections[match.group(1)] = text[start:end].strip()
    return sections


def _normalize_markup(line: str) -> str:
    """修掉模型写坏的加粗标记。

    实测见过 `*营收端：……。**`（开头单星、结尾双星），react-markdown 会渲染成
    一堆星号。宁可朴素，也不要坏渲染。
    """
    text = (line or "").strip()
    if text.endswith("**") and re.match(r"^\*(?!\*)", text):
        text = "**" + text[1:]
    if text.count("**") % 2 == 1:
        text = text.replace("**", "")
    return text


_SENTENCE_SPLIT = re.compile(r"(?<=[。！？；])")


def _split_long(line: str, max_len: int = 90) -> list[str]:
    """把超长段落按句号拆成多条。

    模型不保证"每条≤60 字"，实测会把一整节写成 200 字大块。排版交给代码，
    否则同一份提示词两次运行的可读性完全不同。
    """
    if len(line) <= max_len:
        return [line]
    chunks: list[str] = []
    current = ""
    for sentence in _SENTENCE_SPLIT.split(line):
        if not sentence.strip():
            continue
        if current and len(current) + len(sentence) > max_len:
            chunks.append(current)
            current = sentence
        else:
            current += sentence
    if current:
        chunks.append(current)
    # 拆条后以「；」结尾看起来像没说完，统一收成句号
    return [chunk.rstrip("；") + "。" if chunk.endswith("；") else chunk for chunk in chunks] or [line]


def _auto_bold(line: str) -> str:
    """「营收端：……」这种短前缀自动加粗；已有加粗或前缀过长就不动。"""
    if line.startswith("**"):
        return line
    head, sep, rest = line.partition("：")
    if sep and 2 <= len(head) <= 12 and rest.strip():
        return f"**{head}**：{rest}"
    return line


def _bullets(block: str) -> list[str]:
    lines = [
        _BULLET_RE.sub("", line).strip()
        for line in (block or "").splitlines()
        if line.strip()
    ]
    result: list[str] = []
    for line in lines:
        for piece in _split_long(_normalize_markup(line)):
            result.append(_auto_bold(piece))
    return result


def fit_summary(text: str, limit: int = SUMMARY_MAX_CHARS) -> str:
    """超长时在句末断开，不要在句子中间截断（"……产品结构升…"很难看）。"""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    window = text[:limit]
    cut = max(
        window.rfind("。"), window.rfind("；"), window.rfind("！"), window.rfind("？")
    )
    if cut > 0:
        # 「；」结尾读起来像没说完，统一收成句号；问号/叹号保留原语气
        tail = "。" if window[cut] == "；" else window[cut]
        return window[:cut].rstrip() + tail
    return window.rstrip() + "…"


def unknown_attributions(text: str, labels: set[str]) -> list[str]:
    """找出模型写了、但资料清单里没有的来源名。"""
    cleaned = _OLD_NEWS_RE.sub("", text or "")
    unknown = {
        match.group(1).strip()
        for match in _ATTRIBUTION_RE.finditer(cleaned)
        if match.group(1).strip() not in labels
    }
    return sorted(unknown)


def _is_year(token: str) -> bool:
    return len(token) == 4 and token.isdigit() and token[:2] in ("19", "20")


def ungrounded_numbers(text: str, sources: Iterable[BriefSource], limit: int = 8) -> list[str]:
    """正文里出现、但在来源原文中找不到的数字。

    只查三位以上的数字：一位/两位的计数词噪音太大，年份单独排除。
    这是提示不是硬拦截——综述里的推导数也可能是合理的，但要让人看见。
    """
    corpus = " ".join(
        f"{source.title} {source.snippet}" for source in sources
    )
    allowed = {
        token.replace(",", "")
        for token in _NUMBER_RE.findall(corpus)
    }
    missing: list[str] = []
    for token in _NUMBER_RE.findall(text or ""):
        normalized = token.replace(",", "")
        if len(normalized.replace(".", "")) < 3 or _is_year(normalized):
            continue
        if normalized in allowed or normalized in missing:
            continue
        missing.append(normalized)
        if len(missing) >= limit:
            break
    return missing


async def build_market_brief(
    question: str,
    *,
    company: str | None = None,
    period: str | None = None,
    allow_web: bool = True,
    local_citable_count: int = 0,
    budget: WebSearchBudget | None = None,
    provider: WebSearchProvider | None = None,
    chat: Callable[..., Awaitable[Any]] | None = None,
    extra_allowed_numbers: Iterable[str] = (),
) -> MarketBrief:
    """检索 + 综述生成。所有外部依赖都可注入，便于离线测试。"""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    question = (question or "").strip()

    if not should_use_web(local_citable_count=local_citable_count, allow_web=allow_web):
        reason = "web_not_allowed" if not allow_web else "local_evidence_exists"
        return MarketBrief(
            ok=False, question=question, as_of=now, skipped_reason=reason
        )

    active_budget = budget or WebSearchBudget()
    queries = build_queries(question, company=company, period=period)
    collected: list[WebSearchResult] = []
    for query in queries:
        if not active_budget.can_search():
            break
        try:
            collected.append(
                search_web(
                    query,
                    question=question,
                    budget=active_budget,
                    provider=provider,
                    undated_max_tier=1,
                    extra_allowed_numbers=[period] if period else [],
                )
            )
        except WebSearchError as exc:
            if collected:
                break
            return MarketBrief(
                ok=False,
                question=question,
                as_of=now,
                error=str(exc),
                queries_used=active_budget.used,
                queries=queries,
            )

    # 合并两轮结果：同一 URL 只留一条
    merged: list[BriefSource] = []
    ingest_candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    seen_candidates: set[str] = set()
    domain_counts: dict[str, int] = {}
    #: 舍弃理由要一起带出去，否则"为什么只剩这几条"无法复核
    skipped_sources: list[dict[str, str]] = []
    seen_skipped: set[str] = set()

    def record_skip(url: str, reason: str) -> None:
        if not url or url in seen_skipped:
            return
        seen_skipped.add(url)
        skipped_sources.append({"url": url, "reason": reason})

    for result in collected:
        for item in result.skipped:
            record_skip(str(item.get("url") or ""), str(item.get("reason") or ""))
        for candidate in result.ingest_candidates:
            url = str(candidate.get("url") or "")
            if url and url in seen_candidates:
                continue
            seen_candidates.add(url)
            ingest_candidates.append(candidate)
        for source in result.sources:
            # T1 是官方原件（公告/年报 PDF），属于"入库候选"，不是"市场说法"。
            # 综述只消费媒体与门户，否则来源清单会被无日期的 PDF 占满。
            if source.tier < 2:
                record_skip(source.url, "official_original")
                continue
            if source.title.strip().lower() in _BAD_TITLES:
                record_skip(source.url, "bad_title")
                continue
            if source.url in seen:
                continue
            if domain_counts.get(source.domain, 0) >= MAX_PER_DOMAIN:
                record_skip(source.url, "domain_cap")
                continue
            seen.add(source.url)
            domain_counts[source.domain] = domain_counts.get(source.domain, 0) + 1
            merged.append(
                BriefSource(
                    label=source_label(source.domain),
                    domain=source.domain,
                    url=source.url,
                    title=source.title,
                    published_at=source.published_at,
                    tier=source.tier,
                    snippet=source.snippet,
                )
            )
    # 等级升序；同一等级内时间降序（两趟 stable sort）
    merged.sort(key=lambda item: item.published_at or "", reverse=True)
    merged.sort(key=lambda item: item.tier)
    # 入库候选：有标题的排前面，最多留 5 份，避免把公告列表整个塞进响应
    ingest_candidates.sort(
        key=lambda item: str(item.get("title") or "").strip().lower() in _BAD_TITLES
    )
    ingest_candidates = ingest_candidates[:MAX_INGEST_CANDIDATES]
    skipped_sources = skipped_sources[:MAX_SKIPPED_SOURCES]

    if not merged:
        gaps = ["没有检索到可用的媒体来源（未通过白名单或缺少发布日期）。"]
        if ingest_candidates:
            gaps.append(
                f"另找到 {len(ingest_candidates)} 份官方 PDF，可作为入库候选。"
            )
        return MarketBrief(
            ok=False,
            question=question,
            as_of=now,
            skipped_reason="no_sources",
            sources=[],
            skipped_sources=skipped_sources,
            ingest_candidates=ingest_candidates,
            gaps=gaps,
            queries_used=active_budget.used,
            queries=queries,
            provider=collected[0].provider if collected else "",
        )

    # 只保留前 5 条：综述不需要更多，越多越容易挑花眼
    merged = merged[:5]
    labels = {source.label for source in merged}

    prompt = f"""资料：
{render_sources(merged)}

问题：{question}

只输出以下五段，标题原样照抄，不要增加别的小节：
## 核心判断
## 分项
## 常见误读
## 分歧
## 资料缺口
"""
    messages = [
        {"role": "system", "content": MARKET_BRIEF_SYSTEM},
        {"role": "user", "content": prompt},
    ]

    caller = chat or model_provider.chat
    try:
        answer, _extra, usage = await caller(
            messages,
            reasoning_mode="flash",
            temperature=TEMPERATURE,
            max_tokens=MAX_BRIEF_TOKENS,
        )
    except Exception as exc:
        logger.exception("market brief generation failed")
        return MarketBrief(
            ok=False,
            question=question,
            as_of=now,
            sources=merged,
            ingest_candidates=ingest_candidates,
            error=f"{type(exc).__name__}: {exc}",
            queries_used=active_budget.used,
            provider=collected[0].provider if collected else "",
        )

    text = (answer or "").strip()
    if not text:
        # 思考型模型可能把 max_tokens 全部花在 reasoning_content 上，content 为空。
        # 这时不能把空串当答案，也不能回退到 reasoning（那不是正文）。
        return MarketBrief(
            ok=False,
            question=question,
            as_of=now,
            sources=merged,
            error="模型未产出正文（可能被 max_tokens 截断）",
            queries_used=active_budget.used,
            provider=collected[0].provider if collected else "",
            token_usage={
                "prompt_tokens": int((usage or {}).get("prompt_tokens") or 0),
                "completion_tokens": int((usage or {}).get("completion_tokens") or 0),
                "total_tokens": int((usage or {}).get("total_tokens") or 0),
            },
        )

    parsed = split_sections(text)
    warnings: list[str] = []

    summary = ""
    for key in _SUMMARY_KEYS:
        if parsed.get(key):
            summary = parsed[key].strip()
            break
    summary = re.sub(r"^(?:一句话|结论)[：:]\s*", "", summary).strip()
    summary = fit_summary(summary)

    sections = {"分项": _bullets(parsed.get("分项", ""))}
    # 段落条数按提示词的上限截断：模型会超额，靠代码兜底比靠自觉可靠
    misreadings = _bullets(parsed.get("常见误读", ""))[:3]
    divergences = _bullets(parsed.get("分歧", ""))
    gaps = _bullets(parsed.get("资料缺口", ""))[:3]

    if not parsed:
        warnings.append("模型未按四段格式输出，已保留原文")
    else:
        if not summary:
            warnings.append("缺少「核心判断」段落")
        if not sections["分项"]:
            warnings.append("缺少「分项」段落")

    unknown = unknown_attributions(text, labels)
    if unknown:
        warnings.append("来源名不在资料清单中: " + "、".join(unknown))

    missing_numbers = ungrounded_numbers(text, merged)
    if missing_numbers:
        warnings.append("来源中没有这些数字: " + "、".join(missing_numbers))

    confident = [word for word in _CONFIDENCE_WORDS if word in text]
    if confident:
        warnings.append(
            "出现「可采信」类措辞（多条来源一致不等于核实）: " + "、".join(confident)
        )

    return MarketBrief(
        ok=bool(summary or sections["分项"]),
        question=question,
        summary=summary,
        sections=sections,
        misreadings=misreadings,
        divergences=divergences,
        gaps=gaps,
        sources=merged,
        skipped_sources=skipped_sources,
        ingest_candidates=ingest_candidates,
        raw=text,
        as_of=now,
        warnings=warnings,
        queries_used=active_budget.used,
        queries=queries,
        provider=collected[0].provider if collected else "",
        token_usage={
            "prompt_tokens": int((usage or {}).get("prompt_tokens") or 0),
            "completion_tokens": int((usage or {}).get("completion_tokens") or 0),
            "total_tokens": int((usage or {}).get("total_tokens") or 0),
        },
    )
