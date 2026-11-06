"""巨潮资讯网的公告发现：只做「按股票代码 + 年份找到那一年年报」这一条固定路径。

为什么不抓页面：公告列表是前端渲染的，抓 HTML 等于维护一个随时会碎的正则。
巨潮自己有公告检索 JSON 接口，返回字段稳定（公告标题 / 日期 / PDF 地址 / 声明大小），
所以这里只把它当成"发现层"——**不下载、不解析**，只产出可核对的一条候选，
交给 `fetch_finance_corpus` 与 `ingest_finance_corpus` 已有的下载/解析链路。

边界（刻意写死）：
- 只认标题恰为「{year}年年度报告」的公告；摘要 / 英文版 / 更正版 / 取消公告一律排除。
  标题匹配是"是不是这份文件"的唯一判据，宁可找不到也不猜。
- 不翻页猜、不做站点遍历：一次请求最多 30 条，按公告日期倒序取第一条精确命中。
- 单位、口径、合并范围这类人工判断项有默认值但在输出里标「待人工确认」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

ANNUAL_CATEGORY = "category_ndbg_szsh"
STATIC_PREFIX = "http://static.cninfo.com.cn/"
DEFAULT_PAGE_SIZE = 30

#: 标题里出现这些词就不是"那一年年报正文"，即使标题里带年份。
#: 「摘要」「英文」是另一种文件；「取消/更正/补充」意味着原文件已失效或被替换。
EXCLUDE_TITLE_WORDS = (
    "摘要",
    "英文",
    "取消",
    "更正",
    "补充",
    "问询",
    "回复",
    "意见",
    "提示",
    "债券",
    "社会责任",
    "环境",
    "审计报告",
    "财务报表及",
    "已取消",
)


def column_for_code(code: str) -> str:
    """巨潮接口的 column 参数按交易所分：沪市 sse、深市 szse、北交所 bse。"""
    code = str(code or "").strip()
    if code.startswith("6"):
        return "sse"
    if code.startswith(("4", "8")):
        return "bse"
    return "szse"


def announcement_query_payload(
    code: str,
    org_id: str,
    *,
    year: int | str | None = None,
    category: str = ANNUAL_CATEGORY,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> dict[str, str]:
    """巨潮 `hisAnnouncement/query` 的表单参数。

    `stock` 必须是 `代码,orgId`；只给代码（或缺 orgId）会静默返回 0 条，
    所以 orgId 由调用方先解析好再传进来。
    """
    code = str(code).strip()
    se_date = ""
    if year:
        se_date = f"{int(year) - 1}-06-01~{int(year) + 1}-12-31"
    return {
        "pageNum": str(page),
        "pageSize": str(page_size),
        "column": column_for_code(code),
        "tabName": "fulltext",
        "plate": "",
        "stock": f"{code},{org_id}",
        "searchkey": "",
        "secid": "",
        "category": category,
        "trade": "",
        "seDate": se_date,
        "sortName": "",
        "sortType": "",
        "isHLtitle": "true",
    }


#: 巨潮的公告时间戳是北京时间零点的毫秒值（UTC 16:00 前一天），
#: 按 UTC 取日期会整体错一天，也会和公告 PDF 路径里的日期对不上。
_BEIJING = timezone(timedelta(hours=8))


def _epoch_ms_to_date(value: Any) -> str:
    try:
        stamp = int(value)
    except (TypeError, ValueError):
        return ""
    return datetime.fromtimestamp(stamp / 1000, tz=_BEIJING).date().isoformat()


def normalize_announcement(item: dict[str, Any]) -> dict[str, Any]:
    adjunct = str(item.get("adjunctUrl") or "").strip()
    return {
        "announcement_id": str(item.get("announcementId") or ""),
        "code": str(item.get("secCode") or ""),
        "company": str(item.get("secName") or ""),
        "title": str(item.get("announcementTitle") or "").strip(),
        "date": _epoch_ms_to_date(item.get("announcementTime")),
        "pdf_url": (STATIC_PREFIX + adjunct.lstrip("/")) if adjunct else "",
        "declared_size_kb": item.get("adjunctSize"),
    }


def parse_announcement_body(body: dict[str, Any] | None) -> list[dict[str, Any]]:
    raw = (body or {}).get("announcements") or []
    return [normalize_announcement(item) for item in raw if isinstance(item, dict)]


def annual_report_title(year: int | str) -> str:
    return f"{int(year)}年年度报告"


def _is_excluded(title: str) -> bool:
    return any(word in title for word in EXCLUDE_TITLE_WORDS)


def _is_annual_report_title(title: str, wanted: str, company: str) -> bool:
    """深市标题是「2025年年度报告」，沪市带公司前缀「贵州茅台2025年年度报告」。

    两种都只认逐字相等，不做包含匹配——否则「…年度报告摘要」「…年度报告（英文版）」
    会一起被捞进来。
    """
    compact = title.replace(" ", "").replace("\u3000", "")
    return compact == wanted or bool(company) and compact == f"{company}{wanted}"


@dataclass
class AnnualReportChoice:
    chosen: dict[str, Any] | None = None
    rejected: list[dict[str, Any]] = field(default_factory=list)
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.chosen is not None


def pick_annual_report(
    items: list[dict[str, Any]], year: int | str
) -> AnnualReportChoice:
    """在候选公告里挑出那一年的年报正文。

    只接受标题逐字等于「{year}年年度报告」（沪市带公司名前缀）。多份精确命中时
    取公告日期最新的一份：同一份文件反复披露时，后发的版本才是当前有效版本。
    """
    wanted = annual_report_title(year)
    rejected: list[dict[str, Any]] = []
    exact: list[dict[str, Any]] = []
    for item in items:
        title = str(item.get("title") or "")
        if not _is_annual_report_title(
            title, wanted, str(item.get("company") or "")
        ) or _is_excluded(title):
            rejected.append({**item, "why": "标题不是该年度年报正文"})
            continue
        exact.append(item)
    if not exact:
        return AnnualReportChoice(
            rejected=rejected,
            reason=f"未找到标题为「{wanted}」的公告（可能是未披露或标题口径不同）",
        )
    exact.sort(key=lambda item: str(item.get("date") or ""), reverse=True)
    chosen = exact[0]
    for extra in exact[1:]:
        rejected.append({**extra, "why": "同标题较早版本，已取最新一份"})
    return AnnualReportChoice(chosen=chosen, rejected=rejected, reason="")


def doc_id_for(code: str, year: int | str) -> str:
    return f"cninfo-{str(code).strip()}-{int(year)}-annual"


def build_manifest_document(
    choice: dict[str, Any],
    *,
    year: int | str,
    unit: str = "千元",
    company: str = "",
) -> dict[str, Any]:
    """生成可以直接并进 corpus_manifest.json 的条目。

    下载相关的字段（fetched_at/size/sha256/path）由 `fetch_finance_corpus`
    下载后回填；解析相关的字段（page_count/parser_version）由 ingest 回填。
    """
    code = str(choice.get("code") or "").strip()
    company = company or str(choice.get("company") or "").strip()
    if not code or not choice.get("pdf_url"):
        raise ValueError("公告候选缺少代码或 PDF 地址，无法生成 manifest 条目")
    return {
        "doc_id": doc_id_for(code, year),
        "source": "cninfo",
        "title": f"{company}{int(year)}年年度报告",
        "url": choice["pdf_url"],
        "company": company,
        "code": code,
        "doc_type": "annual_report",
        "period": str(int(year)),
        "announcement_date": choice.get("date") or "",
        "published_at": choice.get("date") or "",
        "announcement_id": choice.get("announcement_id") or "",
        "currency": "CNY",
        "unit": unit,
        "scope": "consolidated",
        "restated": False,
        "negative_format": "parentheses",
        "footnotes": [],
        "content_type": "application/pdf",
        "declared_size_kb": choice.get("declared_size_kb"),
        "effective_from": None,
        "effective_to": None,
        "unit_note": "公告自动发现；单位/口径为默认值，首次入库后需人工确认",
    }


def upsert_manifest_document(manifest: dict[str, Any], document: dict[str, Any]) -> str:
    """按 doc_id 覆盖或追加，返回 "replaced" / "added"。保留已有下载与解析字段。"""
    documents = manifest.setdefault("documents", [])
    for index, existing in enumerate(documents):
        if existing.get("doc_id") == document["doc_id"]:
            documents[index] = {**existing, **document}
            return "replaced"
    documents.append(document)
    return "added"
