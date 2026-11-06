"""联网检索机制：来源分级、预算、查询闸门、Exa 适配器。

这一层只负责"查到什么"，不负责"怎么回答"——结果交给上层（综述模式）消费。
刻意不做的事：

- 不落库：联网结果的时效性极短，落库价值低于维护成本；
- 不做快照：观点题不可复现是可接受的，事实题根本不走这里；
- 不进 `citable` 证据池：网页没有页码和坐标，不能参与 `[n]` 编号。

两条硬规则由本模块保证，而不是靠提示词：

1. **来源分级**：只有白名单域名会被保留，股吧/雪球/微博/贴吧直接丢弃；
2. **查询闸门**：改写后的查询不得出现问题里没有的年份或数字
   （防止模型把答案写进查询、搜回印证自己的内容）。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Protocol
from urllib.parse import urlsplit, urlunsplit

import requests

EXA_SEARCH_URL = "https://api.exa.ai/search"
DEFAULT_TIMEOUT_SECONDS = 12.0
DEFAULT_NUM_RESULTS = 10

#: 单次任务最多联网检索次数：一次主查询 + 一次改写。
MAX_WEB_CALLS = 2

#: 结果短期缓存。费率/规则类问题重复率高，同一 query 10 分钟内只打一次网络。
CACHE_TTL_SECONDS = 600.0
_RESULT_CACHE: dict[tuple, tuple[float, WebSearchResult]] = {}


def clear_web_cache() -> None:
    """测试与运维用：清空结果缓存。"""
    _RESULT_CACHE.clear()


def _cache_key(
    query: str,
    provider: WebSearchProvider | None,
    num_results: int,
    require_published_at: bool,
    undated_max_tier: int,
    allow_unknown: bool,
) -> tuple:
    # 自定义 provider 没声明 cache_tag 时退化为按实例身份缓存，行为与旧版一致。
    if provider is None:
        tag = "default"
    else:
        tag = getattr(provider, "cache_tag", "") or f"{provider.name}:{id(provider)}"
    return (
        tag,
        query,
        int(num_results),
        bool(require_published_at),
        int(undated_max_tier),
        bool(allow_unknown),
    )


def _cache_get(key: tuple) -> WebSearchResult | None:
    entry = _RESULT_CACHE.get(key)
    if entry is None:
        return None
    stored_at, result = entry
    if time.monotonic() - stored_at > CACHE_TTL_SECONDS:
        _RESULT_CACHE.pop(key, None)
        return None
    return result


def _cache_put(key: tuple, result: WebSearchResult) -> WebSearchResult:
    if len(_RESULT_CACHE) > 256:
        _RESULT_CACHE.clear()
    _RESULT_CACHE[key] = (time.monotonic(), result)
    return result

#: 来源分级。加一个源就是加一行；调等级就是改一个数字。
#: T1 = 官方披露（可入库、可作证据）；T2 = 权威财经媒体（只作观点）；
#: T3 = 门户财经（可作线索，需标注）。
SOURCE_TIERS: dict[str, int] = {
    # T1 官方披露
    "cninfo.com.cn": 1,
    "sse.com.cn": 1,
    "szse.cn": 1,
    "bse.cn": 1,
    "csrc.gov.cn": 1,
    # 政府域：印花税、过户费等规则由财政部/税务总局/地方政府发布，
    # 旧白名单只有交易所和媒体，税率类问题会被整批丢掉（2026-11-06 实测）。
    "gov.cn": 1,
    # T2 权威财经媒体
    "stcn.com": 2,
    "cs.com.cn": 2,
    "cnstock.com": 2,
    "caixin.com": 2,
    "yicai.com": 2,
    "21jingji.com": 2,
    "jiemian.com": 2,
    "cls.cn": 2,
    "xinhuanet.com": 2,
    "news.cn": 2,
    "people.com.cn": 2,
    "cnr.cn": 2,
    "thepaper.cn": 2,
    # T3 门户财经
    "finance.sina.com.cn": 3,
    "finance.eastmoney.com": 3,
    "wallstreetcn.com": 3,
}

#: 唯一的一票否决清单。不是"不权威"，是内容本身就是情绪与荐股。
SOURCE_BLOCKED: frozenset[str] = frozenset(
    {
        "guba.eastmoney.com",
        "xueqiu.com",
        "weibo.com",
        "tieba.baidu.com",
        "zhihu.com",
        "douyin.com",
    }
)

_YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
_NUMBER_RE = re.compile(r"(?<![\d.])(\d[\d,]*(?:\.\d+)?)")
_SCHEME_HOST_RE = re.compile(r"^[a-z][a-z0-9+.\-]*://", re.IGNORECASE)


class WebSearchError(RuntimeError):
    """联网检索不可用（未配置 key、请求失败、响应格式异常）。"""


@dataclass(frozen=True)
class WebSource:
    url: str
    domain: str
    title: str
    published_at: str | None
    snippet: str
    tier: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "domain": self.domain,
            "title": self.title,
            "published_at": self.published_at,
            "snippet": self.snippet,
            "tier": self.tier,
        }


@dataclass
class WebSearchResult:
    query: str
    sources: list[WebSource] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    provider: str = ""
    #: 命中的 PDF（多为公司官网/交易所的年报原件）。**不是证据**，是入库候选：
    #: 必须走正常下载 + 解析 + QC 之后才能作为可核对来源。
    ingest_candidates: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "provider": self.provider,
            "sources": [source.to_dict() for source in self.sources],
            "skipped": self.skipped,
            "ingest_candidates": self.ingest_candidates,
        }


class WebSearchProvider(Protocol):
    name: str
    #: 缓存身份。真实 provider 用类级常量，保证"同一个源"跨实例命中缓存；
    #: 测试用的 fixture 每个实例一个独立值，避免不同 fixtures 互相污染。
    cache_tag: str

    def search(
        self,
        query: str,
        *,
        domains: list[str],
        num_results: int,
        timeout: float,
    ) -> list[dict[str, Any]]:
        ...


# ---------------------------------------------------------------------------
# 来源归一化
# ---------------------------------------------------------------------------


def _host_of(url: str) -> str:
    """取小写主机名；容忍不带 scheme 的 URL。"""
    candidate = url.strip()
    if not candidate:
        return ""
    if not _SCHEME_HOST_RE.match(candidate):
        candidate = "https://" + candidate
    try:
        return (urlsplit(candidate).hostname or "").lower()
    except ValueError:
        return ""


def domain_tier(domain: str) -> int | None:
    """按后缀匹配分级：`finance.sina.com.cn` 先精确命中，再退到 `sina.com.cn`。"""
    host = domain.lower().lstrip(".")
    if not host:
        return None
    if host in SOURCE_BLOCKED:
        return None
    if host in SOURCE_TIERS:
        return SOURCE_TIERS[host]
    # 子域继承父域等级，但黑名单优先（guba.eastmoney.com 已被上面挡住）
    parts = host.split(".")
    for size in range(len(parts) - 1, 1, -1):
        parent = ".".join(parts[-size:])
        if parent in SOURCE_BLOCKED:
            return None
        if parent in SOURCE_TIERS:
            return SOURCE_TIERS[parent]
    return None


def canonical_url(url: str) -> str:
    """去掉 fragment 和常见跟踪参数，用于去重。"""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    query = "&".join(
        chunk
        for chunk in parts.query.split("&")
        if chunk and not chunk.lower().startswith(("utm_", "spm", "from="))
    )
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path, query, "")
    )


def _published_at(raw: Any) -> str | None:
    if not raw:
        return None
    text = str(raw).strip()
    if not text:
        return None
    # Exa 返回 ISO 8601（含 Z）。统一成可比较的字符串，不做时区换算。
    return text.replace("Z", "+00:00")


def normalize_sources(
    raw_results: Iterable[dict[str, Any]],
    *,
    require_published_at: bool = True,
    undated_max_tier: int = 0,
    allow_unknown: bool = False,
    unknown_tier: int = 3,
) -> tuple[list[WebSource], list[dict[str, str]]]:
    """过滤 + 分级 + 去重 + 排序。

    `undated_max_tier` 放行"没有发布日期但等级足够高"的来源（默认 0 = 一律丢弃）。
    官方公告即使没有 publishedDate 也是权威件，媒体稿没有日期就没法判断新旧。

    `allow_unknown=True` 时，白名单之外的域名降级为 `unknown_tier`（默认 T3），
    而不是直接丢弃。调用方仍可用 `tier` 限制它只做补充来源。
    """
    kept: list[WebSource] = []
    skipped: list[dict[str, str]] = []
    seen: set[str] = set()

    for item in raw_results:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        domain = _host_of(url)
        if not url or not domain:
            skipped.append({"url": url, "reason": "missing_url"})
            continue
        tier = domain_tier(domain)
        if tier is None:
            if domain in SOURCE_BLOCKED:
                skipped.append({"url": url, "reason": "blocked_domain"})
                continue
            if not allow_unknown:
                skipped.append({"url": url, "reason": "not_whitelisted"})
                continue
            tier = unknown_tier
        published = _published_at(item.get("publishedDate") or item.get("published_at"))
        if require_published_at and not published:
            if not (undated_max_tier and tier <= undated_max_tier):
                skipped.append({"url": url, "reason": "missing_published_at"})
                continue
        canonical = canonical_url(url)
        if canonical in seen:
            skipped.append({"url": url, "reason": "duplicate"})
            continue
        seen.add(canonical)
        kept.append(
            WebSource(
                url=url,
                domain=domain,
                title=str(item.get("title") or "").strip(),
                published_at=published,
                snippet=str(item.get("text") or item.get("snippet") or "").strip()[:400],
                tier=tier,
            )
        )

    # 等级升序；同一等级内时间降序（stable sort，两趟）
    kept.sort(key=lambda source: source.published_at or "", reverse=True)
    kept.sort(key=lambda source: source.tier)
    return kept, skipped


def pdf_ingest_candidates(
    raw_results: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """从检索结果里挑出 PDF 原件，作为入库候选。

    公司官网的年报 PDF 通常不在白名单里（域名没法穷举），但它恰恰是
    最权威的东西。它不能直接当来源——没有解析、没有页码、没有 QC——
    只能作为"要不要入库"的提示。
    """
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        path = url.split("?", 1)[0].lower()
        if not path.endswith(".pdf"):
            continue
        domain = _host_of(url)
        if not domain or domain in SOURCE_BLOCKED:
            continue
        canonical = canonical_url(url)
        if canonical in seen:
            continue
        seen.add(canonical)
        candidates.append(
            {
                "url": url,
                "domain": domain,
                "title": str(item.get("title") or "").strip(),
                "published_at": _published_at(
                    item.get("publishedDate") or item.get("published_at")
                ),
            }
        )
    return candidates


# ---------------------------------------------------------------------------
# 查询闸门
# ---------------------------------------------------------------------------


def _number_tokens(text: str) -> set[str]:
    return {match.replace(",", "") for match in _NUMBER_RE.findall(text or "")}


def query_violations(
    query: str,
    *,
    question: str,
    extra_allowed_numbers: Iterable[str] = (),
) -> list[str]:
    """改写后的查询是否引入了问题里没有的年份或数字。

    "宁德时代 2024 营收 362 亿 原因" 这种把答案写进查询的写法，
    会让检索结果天然印证模型自己的猜想，必须挡在发请求之前。

    调用方可以用 `extra_allowed_numbers` 放行股票代码等已知标识；
    四位数字同时计入年份白名单，避免同一条违规报两遍。
    """
    extras = {str(item).strip() for item in extra_allowed_numbers if str(item).strip()}
    allowed_years = set(_YEAR_RE.findall(question or "")) | {
        item for item in extras if _YEAR_RE.fullmatch(item)
    }
    introduced_years = set(_YEAR_RE.findall(query or "")) - allowed_years

    allowed_numbers = _number_tokens(question) | extras
    introduced_numbers = (_number_tokens(query) - allowed_numbers) - introduced_years

    violations: list[str] = []
    if introduced_years:
        violations.append(
            "查询引入了问题中没有的年份: " + "、".join(sorted(introduced_years))
        )
    if introduced_numbers:
        violations.append(
            "查询引入了问题中没有的数字: " + "、".join(sorted(introduced_numbers))
        )
    return violations


# ---------------------------------------------------------------------------
# 预算与放行
# ---------------------------------------------------------------------------


def should_use_web(*, local_citable_count: int, allow_web: bool) -> bool:
    """联网是兜底，不是并行：本地有可引用证据就不联网。"""
    return bool(allow_web) and local_citable_count <= 0


class WebSearchBudget:
    """单次任务的联网预算。一次主查询 + 一次改写。"""

    def __init__(self, max_calls: int = MAX_WEB_CALLS) -> None:
        self.max_calls = max(0, int(max_calls))
        self.used = 0

    @property
    def remaining(self) -> int:
        return max(0, self.max_calls - self.used)

    def can_search(self) -> bool:
        return self.remaining > 0

    def consume(self) -> None:
        if not self.can_search():
            raise WebSearchError(
                "联网检索预算已用完；请用已有资料作答，或说明查不到。"
            )
        self.used += 1


# ---------------------------------------------------------------------------
# Provider 实现
# ---------------------------------------------------------------------------


class ExaProvider:
    """Exa 检索适配器。key 从环境变量读，不落盘。"""

    name = "exa"
    cache_tag = "exa"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.api_key = (api_key or os.getenv("EXA_API_KEY") or "").strip()
        self.base_url = (base_url or os.getenv("EXA_BASE_URL") or EXA_SEARCH_URL).strip()
        self._session = session or requests.Session()

    def search(
        self,
        query: str,
        *,
        domains: list[str],
        num_results: int = DEFAULT_NUM_RESULTS,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> list[dict[str, Any]]:
        if not self.api_key:
            raise WebSearchError(
                "未配置 EXA_API_KEY；联网检索不可用。"
            )
        payload: dict[str, Any] = {
            "query": query,
            "numResults": max(1, int(num_results)),
            "contents": {"text": True},
        }
        if domains:
            payload["includeDomains"] = list(domains)
        try:
            response = self._session.post(
                self.base_url,
                json=payload,
                headers={
                    "x-api-key": self.api_key,
                    "Content-Type": "application/json",
                },
                timeout=timeout,
            )
        except requests.RequestException as exc:
            raise WebSearchError(f"联网检索请求失败: {exc}") from exc
        if response.status_code >= 400:
            raise WebSearchError(
                f"联网检索返回 {response.status_code}: {response.text[:200]}"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise WebSearchError("联网检索返回的不是 JSON") from exc
        results = body.get("results") if isinstance(body, dict) else None
        if not isinstance(results, list):
            raise WebSearchError("联网检索响应缺少 results 数组")
        return [item for item in results if isinstance(item, dict)]


_MCP_BLOCK_SPLIT = re.compile(r"(?m)^-{3,}\s*$")
_MCP_NA_VALUES = {"", "n/a", "none", "null", "-"}


def parse_mcp_text(text: str) -> list[dict[str, Any]]:
    """把 Exa MCP 的 `content[].text` 文本块拆成结构化结果。

    实测格式（2026-11-05）：

        Title: ...
        URL: ...
        Published: 2025-03-14T04:28:41.000Z 或 N/A
        Author: ...
        Highlights:
        <正文>
        ---
        Title: ...

    「Highlights:」之后全部算正文，所以正文里的冒号不会被误当成字段头。
    """
    items: list[dict[str, Any]] = []
    for block in _MCP_BLOCK_SPLIT.split(text or ""):
        block = block.strip()
        if not block:
            continue
        fields: dict[str, str] = {}
        content_lines: list[str] = []
        in_content = False
        for line in block.splitlines():
            if in_content:
                content_lines.append(line)
                continue
            head, sep, value = line.partition(":")
            key = head.strip().lower()
            if sep and key in ("title", "url", "published", "author", "highlights"):
                if key == "highlights":
                    in_content = True
                    if value.strip():
                        content_lines.append(value.strip())
                else:
                    fields[key] = value.strip()
            else:
                in_content = True
                content_lines.append(line)
        url = fields.get("url", "")
        if not url:
            continue
        published = fields.get("published", "")
        items.append(
            {
                "url": url,
                "title": fields.get("title", ""),
                "publishedDate": None
                if published.lower() in _MCP_NA_VALUES
                else published,
                "text": "\n".join(content_lines).strip(),
            }
        )
    return items


class McporterProvider:
    """通过本地 mcporter 调用 Exa MCP（`mcp.exa.ai/mcp`）。

    不需要 EXA_API_KEY——凭证由 mcporter 管理。代价是拿不到
    `includeDomains` / 日期过滤，只能取回后由 `normalize_sources` 客户端筛选。
    """

    name = "mcporter-exa"
    cache_tag = "mcporter-exa"

    def __init__(self, executable: str | None = None) -> None:
        self.executable = executable or shutil.which("mcporter") or ""

    def search(
        self,
        query: str,
        *,
        domains: list[str],
        num_results: int = DEFAULT_NUM_RESULTS,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> list[dict[str, Any]]:
        if not self.executable:
            raise WebSearchError("未找到 mcporter；无法调用本地 Exa MCP。")
        mcporter_args = [
            "call",
            "exa.web_search_exa",
            f"query={query}",
            # 实测参数名是 schema 里的 camelCase；写 num-results 会被静默忽略（返回默认 10 条）
            f"numResults={max(1, int(num_results))}",
            "--output",
            "json",
            "--timeout",
            str(int(max(1.0, timeout) * 1000)),
        ]
        command = (
            ["cmd", "/c", self.executable, *mcporter_args]
            if os.name == "nt"
            else [self.executable, *mcporter_args]
        )
        try:
            proc = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout + 15,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise WebSearchError(f"调用 mcporter 失败: {exc}") from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()[:200]
            raise WebSearchError(f"mcporter 返回 {proc.returncode}: {detail}")
        try:
            body = json.loads(proc.stdout.strip())
        except ValueError as exc:
            raise WebSearchError("mcporter 返回的不是 JSON") from exc
        blocks = body.get("content") if isinstance(body, dict) else None
        if not isinstance(blocks, list):
            raise WebSearchError("mcporter 响应缺少 content 数组")
        text = "\n".join(
            str(block.get("text") or "")
            for block in blocks
            if isinstance(block, dict)
        )
        return parse_mcp_text(text)


def default_provider() -> WebSearchProvider:
    """本地有 mcporter 就用它（无需 key）；否则退回 REST（需要 EXA_API_KEY）。"""
    if shutil.which("mcporter"):
        return McporterProvider()
    if os.getenv("EXA_API_KEY"):
        return ExaProvider()
    raise WebSearchError("联网检索不可用：未找到 mcporter，且未配置 EXA_API_KEY。")


class FixtureProvider:
    """离线测试与演示用：返回固定结果，不联网；按 query 命中关键字挑子集。"""

    name = "fixture"
    _instance_counter = 0

    def __init__(self, items: Iterable[dict[str, Any]]) -> None:
        self.items = list(items)
        # 每个实例一个独立缓存身份：不同 fixtures 用同一 query 时不能互相命中。
        FixtureProvider._instance_counter += 1
        self.cache_tag = f"fixture:{FixtureProvider._instance_counter}"

    def search(
        self,
        query: str,
        *,
        domains: list[str],
        num_results: int = DEFAULT_NUM_RESULTS,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> list[dict[str, Any]]:
        return self.items[: max(1, int(num_results))]


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def search_web(
    query: str,
    *,
    question: str,
    budget: WebSearchBudget,
    provider: WebSearchProvider | None = None,
    num_results: int = DEFAULT_NUM_RESULTS,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    require_published_at: bool = True,
    undated_max_tier: int = 0,
    extra_allowed_numbers: Iterable[str] = (),
    allow_unknown: bool = False,
) -> WebSearchResult:
    """执行一次联网检索。

    调用方负责两件事：先用 `should_use_web` 判断该不该联网，
    再用 `budget` 确保不超过次数。这里只做查询闸门 + 检索 + 归一化。

    命中 10 分钟缓存时不消耗 budget，也不发起网络请求。
    """
    key = _cache_key(
        query,
        provider,
        num_results,
        require_published_at,
        undated_max_tier,
        allow_unknown,
    )
    cached = _cache_get(key)
    if cached is not None:
        return cached

    violations = query_violations(
        query,
        question=question,
        extra_allowed_numbers=extra_allowed_numbers,
    )
    if violations:
        raise WebSearchError("查询未通过闸门：" + "；".join(violations))

    budget.consume()
    active = provider or default_provider()
    raw = active.search(
        query,
        domains=sorted(SOURCE_TIERS),
        num_results=num_results,
        timeout=timeout,
    )
    sources, skipped = normalize_sources(
        raw,
        require_published_at=require_published_at,
        undated_max_tier=undated_max_tier,
        allow_unknown=allow_unknown,
    )
    result = WebSearchResult(
        query=query,
        sources=sources,
        skipped=skipped,
        provider=active.name,
        ingest_candidates=pdf_ingest_candidates(raw),
    )
    return _cache_put(key, result)


def recent_window(days: int = 7, *, now: datetime | None = None) -> tuple[str, str]:
    """给 Exa 用的时间窗（ISO 8601，UTC）。"""
    end = now or datetime.now(timezone.utc)
    start = end - timedelta(days=max(1, int(days)))
    return (
        start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        end.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    )
