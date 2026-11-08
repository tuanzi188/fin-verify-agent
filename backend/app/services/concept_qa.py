"""金融概念问答：默认只用通用知识；允许联网时，先检索再作答。

与另外两条回答路径的分工：

- `finance_rag` / Agent：回答「某家公司某一期披露了什么数」，每个数字都要能回查到单元格；
- `market_brief`：回答「市场怎么说」，来源是网页，永不进 `[n]`；
- 本模块：回答「这个术语是什么意思」，定义来自模型通用知识，时效性事实才查公开信息。

联网是可选增强，不是必需：`allow_web=False` 时一次 LLM 调用、零检索；打开后
最多一次检索，把标题/日期/摘要拼进上下文，要求模型只对资料里出现的具体数字
标注「（来源名，月-日）」。定义与计算公式仍由模型自己的知识给出，不标来源。

纪律靠代码兜住的部分：
- 答案里出现 `[n]`、页码、表 id 时降级为 `needs_review`：这类标记会让人误以为
  结论来自本地语料，必须拦住；
- 联网模式下写了资料清单里没有的来源名，同样降级：那是编来源；
- 数字与长度只记 note（flash 档实测会写成带例子的报告体，硬判会让状态失去意义）。
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from app.config import MODEL_CONFIG
from app.models.finance import FinanceChatResponse
from app.services import model_provider
from app.services.market_brief import source_label, unknown_attributions
from app.services.web_search import (
    WebSearchBudget,
    WebSearchError,
    WebSearchProvider,
    WebSearchResult,
    search_web,
)
from app.tools.contract import ABSTAIN_KEYWORDS

logger = logging.getLogger(__name__)

MAX_ANSWER_TOKENS = 4096
TEMPERATURE = 0.2
#: 只用于审计留痕，不阻断交付。提示词已允许通用教学示例与分类型展开，
#: 正常答案会明显超过 400 字；这里只抓异常长文（>1500 字）供排查。
MAX_ANSWER_CHARS = 1500
MAX_CONTEXT_SOURCES = 6

# 概念答案里不该出现的硬信号：引用编号与文档坐标。
# 数字不再算违规——公式和通用示例会正常出现，只记 note 供审计。
_NUMBER = re.compile(r"\d")
_REFERENCE = re.compile(r"\[\s*\d+\s*\]|\[\s*n\s*\]")
_LOCATOR = re.compile(r"#t\d{4}|#rows#|P[.．]\s*\d+|第\s*\d+\s*页|表\s*\w+#")

UNCERTAIN_PHRASE = "无法给出可靠解释"
OUT_OF_SCOPE_PHRASE = "不在金融概念范围内"

_CONCEPT_BASE = """你是金融概念解释助手。用户问的是概念或知识，不是某一家的披露数字。

输出要求：
1. 先给结论，再展开。篇幅以讲清为准，通常 2-4 句；确实需要时可以分点，但不要为凑结构注水。尽量不用标题、表格和代码块。
2. 第一句必须是能独立成立的定论，不超过 60 字，例如「商誉就是并购时多付的那部分溢价」，再展开解释；不要用标题开头。
3. 可以写公式定义，例如「毛利率 = 毛利 ÷ 营业收入」。为帮助理解，可以（不必勉强）配一个通用示例，数字须是假设值、不指向任何具体公司或市场，例如「PE=20 约等于 20 年回本」；具体公司的数字、股价、指数、费率与历史数据一律不写。
4. 不写 [n]、页码、表名、表 id——这些是文档查证的坐标，概念说明用不上。
5. 不编造来源、会计准则条款号、税率、监管比例或历史案例。
6. 不确定就用这一句回答：「这个概念我无法给出可靠解释」。
7. 不属于金融/财报概念时，只回答「这个问题不在金融概念范围内」。
8. 不复述问题，不用「首先/其次/综上所述」，不写免责声明。

可选（有则更好，没有也不扣分）：概念存在类型差异时，用「分类型怎么看」说明适用场景，比抽象罗列定义更好懂。

注意：这是通用知识说明，不是本产品的查证结论；用户会看到「通用知识，非文档查证」的标签。
"""

_NO_WEB_NOTE = """

【本次没有外部资料】
定义与解释全部来自你的通用知识，不要标注任何来源，也不要写「据公开信息」。
"""

_WEB_NOTE = """

【本次附有公开信息资料】
资料如下（只是参考，可能过期，也可能与你的知识冲突）：
{context}

9. 定义、公式、判断标准可以用你的通用知识，不必标注来源。
10. 具体费率、期限、监管规则、最新政策、具体数字，只有在资料里出现时才能写，
    并在句末标「（来源名，月-日）」；来源名只能用上面出现过的，不要编。
11. 资料与你的通用知识冲突时，以资料为准，并写「资料显示……」。
12. 资料里找不到的具体数值，宁可不写。
"""

CONCEPT_SYSTEM = _CONCEPT_BASE + _NO_WEB_NOTE
CONCEPT_WEB_SYSTEM = _CONCEPT_BASE + _WEB_NOTE

RATE_UNAVAILABLE = "未检索到可核对的公开资料，请以交易所或券商最新公告为准。"
RATE_SINGLE_SOURCE = (
    "只检索到单一非官方来源，未达到交叉验证要求，请以交易所或券商最新公告为准。"
)

# 费率/规则题专用：这类数字会变（印花税 2023 年从 1‰ 减半到 0.5‰），
# 记忆版答案即使很像事实也可能已经过期，所以必须逐条落到资料上。
RATE_SYSTEM = """你是金融规则/费率查询助手。用户问的是当前有效的规则或费率，不是解释概念。

输出要求：
1. 第一句直接给答案：数值 + 单位 + 适用方向（例如「卖出单边征收，成交金额的 0.5‰」）。
2. 再用 1-2 句补生效时间、适用范围和例外（例如 ETF、可转债免征）。
3. 每个具体数值后面必须标「（来源名，日期）」；来源名只能用下面资料里出现过的，不要编。
4. 资料里没有的数值一律不写，不要用记忆补。
5. 资料不足以回答时，只回答这一句：「未检索到可核对的公开资料，请以交易所或券商最新公告为准」。
6. 只回答被问到的这一项；不要顺带列其他费率、费用表或投资建议。
7. 资料有多条且日期不同时，取发布日期最新的一条；旧版本只在需要对比时提一句。
8. 不用标题、表格、代码块。

资料：
{context}
"""


def _leading_line(answer: str) -> str:
    for raw in (answer or "").splitlines():
        line = raw.strip()
        if line:
            return line
    return ""


_BAD_TITLES = {"", "n/a", "none", "null", "undefined"}


def _display_title(source: Any) -> str:
    title = str(getattr(source, "title", "") or "").strip()
    return "" if title.lower() in _BAD_TITLES else title


def _render_context(result: WebSearchResult) -> str:
    blocks: list[str] = []
    for source in result.sources[:MAX_CONTEXT_SOURCES]:
        date = (source.published_at or "")[:10] or "日期未知"
        title = _display_title(source) or source_label(source.domain)
        blocks.append(
            f"- {source_label(source.domain)}｜{date}\n"
            f"  {title}\n"
            f"  {source.snippet[:300]}"
        )
    return "\n\n".join(blocks)


# 首段遇到这些开头就停：标题、表格、代码块、列表都不是“一句话定论”。
_LEAD_STOP = re.compile(r"^\s*(#{1,6}\s|\||```|[-*]\s|\d+\.\s)")
MAX_LEAD_CHARS = 120


def _split_lead(answer: str) -> str:
    """切出首段定论。

    模型常以 `# 标题` 开头，标题不能当定论；跳过标题和空行后取第一段连续文本。
    过长的段落不提升——那不是「一句话定论」，放大反而更吵。切不到就返回空串，
    前端退回普通排版。
    """
    lines = (answer or "").splitlines()
    index = 0
    while index < len(lines) and (
        not lines[index].strip() or lines[index].lstrip().startswith("#")
    ):
        index += 1
    start = index
    while (
        index < len(lines)
        and lines[index].strip()
        and not _LEAD_STOP.match(lines[index])
    ):
        index += 1
    lead = "\n".join(lines[start:index]).strip()
    # 整段答案就是这一句时不必单独提升：正文会空掉，放大也失去意义。
    if not lead or len(lead) > MAX_LEAD_CHARS or lead == (answer or "").strip():
        return ""
    return lead


def _public_sources(result: WebSearchResult | None) -> list[dict]:
    if result is None:
        return []
    return [
        {
            "label": source_label(source.domain),
            "domain": source.domain,
            "title": _display_title(source),
            "url": source.url,
            "published_at": (source.published_at or "")[:10],
        }
        for source in result.sources[:MAX_CONTEXT_SOURCES]
    ]


def _decide_status(
    answer: str, *, unknown_source: bool = False
) -> tuple[str, list[str]]:
    """状态只区分「能不能交付」，不区分文风。

    红线有两类：文档坐标（冒充查证结论）与编造来源名（冒充有出处）。
    数字和长度只记 note——实测 flash 档会先花上千 token 推理，正文仍写成
    带例子的报告体；把它们都判成 needs_review 会让状态失去意义。
    """
    notes: list[str] = []
    if not answer.strip():
        return "error", ["concept:empty_answer"]
    if len(answer) > MAX_ANSWER_CHARS:
        notes.append("concept:over_length")
    leading = _leading_line(answer)
    if any(keyword in leading for keyword in ABSTAIN_KEYWORDS):
        notes.append("concept:abstained")
        return "abstained", notes
    if UNCERTAIN_PHRASE in answer or OUT_OF_SCOPE_PHRASE in answer:
        notes.append("concept:abstained")
        return "abstained", notes
    if RATE_UNAVAILABLE in answer:
        notes.append("concept:abstained")
        return "abstained", notes
    if _REFERENCE.search(answer) or _LOCATOR.search(answer):
        notes.append("concept:contains_reference_or_locator")
        return "needs_review", notes
    if unknown_source:
        notes.append("concept:unknown_source")
        return "needs_review", notes
    if _NUMBER.search(answer):
        # 例子和公式里的数字是常见写法；只留痕供审计，不阻断交付。
        notes.append("concept:contains_number")
    return "answered", notes


# 口语噪音：去掉之后剩下的才是可检索的关键词。
_NOISE = re.compile(
    r"请问|麻烦|帮我|我想知道|想问一下|想问|想了解|一下|现在|目前|当前|"
    r"是多少|多少|怎么收|怎么算|如何收|如何算|呢|吗|啊|呀|吧"
)
_PUNCT = re.compile(r"[？?，,。.！!、；;：:]")


def _keywords(query: str) -> str:
    text = _NOISE.sub(" ", query or "")
    text = _PUNCT.sub(" ", text)
    return " ".join(text.split())


def _build_queries(query: str, *, must_web: bool) -> list[str]:
    """费率题改写成关键词式；概念题保持自然问句，改写反而伤定义召回。"""
    if not must_web:
        return [query]
    core = _keywords(query) or str(query or "").strip()
    return [f"{core} 最新规定", f"{core} 官方公告"]


def _select_sources(sources: list, *, allow_t3: bool) -> list:
    """T1/T2 优先；不够两条时才用 T3 补位，最多补两条。"""
    primary = [source for source in sources if source.tier <= 2]
    selected = primary[:MAX_CONTEXT_SOURCES]
    if allow_t3 and len(selected) < 2:
        selected.extend([source for source in sources if source.tier >= 3][:2])
    return selected[:MAX_CONTEXT_SOURCES]


def _rate_source_gate(sources: list) -> str:
    """费率题：有官方源就够；没有官方源时必须两个独立域名互相印证。"""
    if any(source.tier == 1 for source in sources):
        return ""
    if len({source.domain for source in sources}) >= 2:
        return ""
    return RATE_SINGLE_SOURCE


def _search(
    query: str,
    *,
    provider: WebSearchProvider | None,
    budget: WebSearchBudget | None,
    queries: list[str] | None = None,
    allow_unknown: bool = False,
) -> WebSearchResult | None:
    """执行 1-2 条查询并合并结果。

    两条查询并行：单次检索要起一个 mcporter 子进程（约 4.5s），串行就是 9s，
    并行后接近单条耗时。费率题固定两条；概念题只有一条，不受影响。
    """
    active = budget or WebSearchBudget()
    items = list(queries or [query])
    results: list[WebSearchResult] = []

    def _run(item: str) -> WebSearchResult:
        return search_web(
            item,
            question=query,
            budget=active,
            provider=provider,
            undated_max_tier=2,
            allow_unknown=allow_unknown,
        )

    if len(items) == 1:
        try:
            results.append(_run(items[0]))
        except WebSearchError as exc:
            logger.info("concept web search failed: %s", exc)
    else:
        with ThreadPoolExecutor(max_workers=len(items)) as pool:
            futures = [pool.submit(_run, item) for item in items]
            for future in futures:
                try:
                    results.append(future.result())
                except WebSearchError as exc:
                    logger.info("concept web search failed: %s", exc)

    if not results:
        return None
    provider_name = next((result.provider for result in results if result.provider), "")
    sources: list = []
    skipped: list = []
    for result in results:
        sources.extend(result.sources)
        skipped.extend(result.skipped)
    seen: set[str] = set()
    unique = []
    for source in sources:
        if source.url in seen:
            continue
        seen.add(source.url)
        unique.append(source)
    return WebSearchResult(
        query=query,
        sources=unique,
        skipped=skipped,
        provider=provider_name,
    )


def _abstained_response(
    *,
    message: str,
    conversation_id: str,
    query: str,
    route_reason: str,
    notes: list[str],
    started_at: float,
    web_queries: int,
) -> FinanceChatResponse:
    """没拿到可核对资料时的统一出口：不调模型，也不拿记忆补数。"""
    return FinanceChatResponse(
        status="abstained",
        answer=message,
        conversation_id=conversation_id,
        model_used=MODEL_CONFIG["flash"]["model"],
        variant="concept",
        strategy="concept",
        route_reason=route_reason,
        duration_ms=int((time.perf_counter() - started_at) * 1000),
        resolved_query=query,
        notes=notes,
        token_usage={
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "llm_calls": 0,
            "web_queries": web_queries,
        },
    )


async def answer_concept(
    *,
    query: str,
    conversation_id: str,
    route_reason: str = "",
    allow_web: bool = False,
    must_web: bool = False,
    provider: WebSearchProvider | None = None,
    budget: WebSearchBudget | None = None,
    chat: Callable[..., Awaitable[Any]] | None = None,
) -> FinanceChatResponse:
    """回答一条金融概念问题。

    `must_web=True` 表示费率/规则类事实：即使请求没开联网也强制检索，
    查不到就直说查不到——这类数字靠记忆答，错了比拒答更糟。
    `chat`、`provider` 与 `budget` 都可注入，便于离线测试。
    """
    started_at = time.perf_counter()
    notes: list[str] = []

    if must_web:
        notes.append("concept:web_fact")
        if not allow_web:
            allow_web = True
            notes.append("concept:web_forced")

    web: WebSearchResult | None = None
    active_budget = budget or WebSearchBudget()
    if allow_web:
        web = _search(
            query,
            provider=provider,
            budget=active_budget,
            queries=_build_queries(query, must_web=must_web),
            allow_unknown=not must_web,
        )
        if web is None:
            notes.append("concept:web_failed")
        elif not web.sources:
            notes.append("concept:web_empty")
            web = None

    if web is not None:
        selected = _select_sources(web.sources, allow_t3=not must_web)
        if must_web:
            reason = _rate_source_gate(selected)
            if reason:
                notes.append("concept:single_source")
                return _abstained_response(
                    message=reason,
                    conversation_id=conversation_id,
                    query=query,
                    route_reason=route_reason,
                    notes=notes,
                    started_at=started_at,
                    web_queries=active_budget.used,
                )
        if selected:
            web = WebSearchResult(
                query=web.query,
                sources=selected,
                skipped=web.skipped,
                provider=web.provider,
            )
        else:
            notes.append("concept:web_empty")
            web = None

    if must_web and web is None:
        # 已经查过但没拿到可核对资料：不用记忆补数字，直接说查不到。
        return _abstained_response(
            message=RATE_UNAVAILABLE,
            conversation_id=conversation_id,
            query=query,
            route_reason=route_reason,
            notes=notes,
            started_at=started_at,
            web_queries=active_budget.used,
        )

    if web is not None:
        notes.append("concept:web")
        context = _render_context(web)
        system = (
            RATE_SYSTEM.format(context=context)
            if must_web
            else CONCEPT_WEB_SYSTEM.format(context=context)
        )
    else:
        notes.append("concept:no_retrieval")
        system = CONCEPT_SYSTEM

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": query},
    ]
    caller = chat or model_provider.chat
    answer = ""
    usage: dict = {}
    failed = False
    try:
        answer, _, usage = await caller(
            messages,
            reasoning_mode="flash",
            temperature=TEMPERATURE,
            max_tokens=MAX_ANSWER_TOKENS,
        )
        answer = (answer or "").strip()
    except Exception as exc:  # 生成失败要落成 error 而不是丢请求
        failed = True
        logger.exception("concept answer failed")
        answer = f"概念生成失败：{type(exc).__name__}"

    if failed:
        status, status_notes = "error", ["concept:generation_failed"]
    else:
        extra: list[str] = []
        unknown_source = False
        used_source = False
        if web is not None:
            labels = {source_label(source.domain) for source in web.sources}
            unknown_source = bool(unknown_attributions(answer, labels))
            # 括号在中文里到处都在用，不能拿「有没有括号」判断是否标注了来源；
            # 只认「来源名，」这一种形态。
            used_source = any(
                f"{label}，" in answer or f"{label}," in answer for label in labels
            )
            if unknown_source:
                pass
            elif used_source:
                extra.append("concept:web_used")
            else:
                extra.append("concept:web_unused")
        status, status_notes = _decide_status(answer, unknown_source=unknown_source)
        status_notes.extend(extra)
        # 费率题没标来源只记 note：来源列表已经展示，标签也写明「公开信息 · 未核实」。
        # 不再为此降级——否则每条费率答案都挂「需人工复核」，状态就失去区分度。
        if must_web and status == "answered" and _NUMBER.search(answer) and not used_source:
            status_notes.append("concept:unattributed_number")
    notes.extend(status_notes)

    tokens_in = int((usage or {}).get("prompt_tokens") or 0)
    tokens_out = int((usage or {}).get("completion_tokens") or 0)
    return FinanceChatResponse(
        status=status,
        answer=answer,
        conversation_id=conversation_id,
        model_used=MODEL_CONFIG["flash"]["model"],
        variant="concept",
        strategy="concept",
        route_reason=route_reason,
        duration_ms=int((time.perf_counter() - started_at) * 1000),
        resolved_query=query,
        notes=notes,
        answer_lead="" if failed else _split_lead(answer),
        web_sources=_public_sources(web),
        token_usage={
            "prompt_tokens": tokens_in,
            "completion_tokens": tokens_out,
            "total_tokens": tokens_in + tokens_out,
            "llm_calls": 1,
            "web_queries": active_budget.used,
        },
    )
