"""引用定位的三级刻度：页码 / 表格 / 单元格。

为什么要和"引用是否支持结论"分开记：

- **语义支持**（judge 的 `citation_supports`）回答"这段原文能不能支撑这句话"；
- **精确定位**回答"这个引用有没有指到那张表、那一行、那一列、那一页"。

金融查证要的是后者：同一个数字可能出现在正文、附注、母公司报表里，语义上都能"支撑"，
但审计需要的是可复核的那一格。两者都报，**不允许用语义判分替代精确定位**。

三级定义（对任务卡里带 `evidence` 的要点逐条算）：

| 级别 | 命中条件 |
|---|---|
| 页码 | 被引用窗口覆盖的任一来源页码 == 证据表所在页（至少 8 字重叠） |
| 表格 | 被引用窗口覆盖的任一来源 id == 证据表 id（至少 8 字重叠） |
| 单元格 | 引用形如 `doc#table#row_index#column_name`，且行列与 evidence 一致 |

RAG 臂的引用是 `doc#table#cNNNN`（512 字窗口），**单元格级天然为 0**——这正是
"普通 RAG 没有精确到格"的量化表达，不是它答错了。
"""

from __future__ import annotations

from app.finance.store import FinanceStore

# 一个窗口扫过某来源 1–2 个字符，不足以支撑“引到了这张表/这一页”。
# 多来源全部保留在 run 文件里，但锚点命中只认至少 8 个字符的重叠；
# 8 字足以覆盖一个金额或指标名，又不会把窗口边界的标点算成证据。
ANCHOR_MIN_SPAN_CHARS = 8


def _base(chunk_id: str) -> str:
    parts = str(chunk_id or "").split("#")
    return "#".join(parts[:2]) if len(parts) >= 2 else str(chunk_id or "")


def _norm(value: object) -> str:
    return str(value or "").translate(str.maketrans("", "", " ,\u00a0")).strip()


def _page_of(chunk_id: str, store: FinanceStore) -> int | None:
    base = _base(chunk_id)
    try:
        if "#t" in base:
            return int(store.get_table(base).get("page") or 0) or None
        if "#s" in base:
            return int(store.read_section(base).get("page_start") or 0) or None
    except Exception:  # noqa: BLE001 - 解析不到就是没命中，不影响其它要点
        return None
    return None


def _rows_cover_cell(chunk_id: str, evidence: dict, store: FinanceStore) -> bool:
    """get_rows 的行组锚点是否覆盖目标单元格。

    行组证据不是"某个单格"，但若它明确携带目标行号、目标列号、页码和单位，
    审计上仍能定位到那一格；不能因为没用 get_cell 就记成 0。
    """
    table_id = str(evidence.get("table_id") or "")
    prefix = f"{table_id}#rows#"
    chunk_id = str(chunk_id or "")
    if not table_id or not chunk_id.startswith(prefix):
        return False
    rest = chunk_id[len(prefix):]
    if "#cols#" not in rest:
        return False
    row_part, column_part = rest.split("#cols#", 1)
    try:
        row_indices = {int(value) for value in row_part.split(",") if value != ""}
        column_indices = {int(value) for value in column_part.split(",") if value != ""}
        table = store.get_table(table_id)
        want_row = store._resolve_row(table.get("rows") or [], evidence.get("row"))
        column = evidence.get("column")
        if isinstance(column, int):
            want_column = column
        else:
            want_column = store._resolve_column(table.get("columns") or [], column)
    except Exception:  # noqa: BLE001
        return False
    return want_row in row_indices and want_column in column_indices


def _cell_matches(chunk_id: str, evidence: dict, store: FinanceStore) -> bool:
    # get_cell 的锚点：表 id 后接行号、列名两段（表 id 自身含 #，所以不是固定四段）。
    if _rows_cover_cell(chunk_id, evidence, store):
        return True
    table_id = str(evidence.get("table_id") or "")
    if not table_id or not str(chunk_id).startswith(f"{table_id}#"):
        return False
    suffix = str(chunk_id)[len(table_id) + 1:]
    parts = suffix.split("#")
    if len(parts) != 2 or not parts[0].isdigit() or parts[1] == "rows":
        return False
    table = store.get_table(table_id)
    try:
        want_row = store._resolve_row(table.get("rows") or [], evidence.get("row"))
        column = evidence.get("column")
        if isinstance(column, int):
            want_column = str((table.get("columns") or [])[column])
        else:
            want_column = str(column)
    except Exception:  # noqa: BLE001
        return False
    return int(parts[0]) == want_row and _norm(parts[1]) == _norm(want_column)


def _cited_sources(result: dict, citation_id: str, store: FinanceStore) -> list[dict]:
    """一个引用锚点实际覆盖的来源；没有多条来源记录时退回单一锚点解析。

    固定窗 chunk 现在带 `source_spans`，窗口跨章节/表格时全部保留。
    Agent 的 section/table/cell 锚点没有该字段，仍按原方式解析。
    """
    evidence_by_id = {
        str(item.get("chunk_id") or ""): item
        for item in result.get("evidence") or []
        if isinstance(item, dict)
    }
    item = evidence_by_id.get(citation_id) or {}
    spans = item.get("source_spans")
    if isinstance(spans, list) and spans:
        return [
            {
                "source_id": str(span.get("source_id") or ""),
                "page": span.get("page"),
                "kind": span.get("kind") or "",
                "chars": span.get("chars"),
            }
            for span in spans
            if isinstance(span, dict)
            and (
                not isinstance(span.get("chars"), (int, float))
                or int(span.get("chars") or 0) >= ANCHOR_MIN_SPAN_CHARS
            )
        ]
    return [
        {
            "source_id": _base(citation_id),
            "page": _page_of(citation_id, store),
            "kind": "",
        }
    ]


def anchor_report(task: dict, result: dict, store: FinanceStore) -> dict:
    """返回 {points, page_hits, table_hits, cell_hits, details}。"""
    citations = [str(item.get("chunk_id") or "") for item in result.get("citations") or []]
    cited_sources = [
        source
        for citation_id in citations
        for source in _cited_sources(result, citation_id, store)
    ]
    points = 0
    page_hits = 0
    table_hits = 0
    cell_hits = 0
    details = []
    for index, point in enumerate(task.get("answer_points") or []):
        if not isinstance(point, dict) or point.get("derived") or point.get("kind") == "note":
            continue
        evidence = point.get("evidence")
        if not isinstance(evidence, dict):
            continue
        table_id = str(evidence.get("table_id") or "")
        if not table_id:
            continue
        points += 1
        try:
            table_page = int(store.get_table(table_id).get("page") or 0)
        except Exception:  # noqa: BLE001
            table_page = 0
        page_hit = any(
            table_page and int(source.get("page") or 0) == table_page
            for source in cited_sources
        )
        table_hit = any(
            str(source.get("source_id") or "") == table_id for source in cited_sources
        )
        cell_hit = any(_cell_matches(cid, evidence, store) for cid in citations)
        page_hits += 1 if page_hit else 0
        table_hits += 1 if table_hit else 0
        cell_hits += 1 if cell_hit else 0
        details.append(
            {
                "point": index,
                "table_id": table_id,
                "page": table_page,
                "page_hit": page_hit,
                "table_hit": table_hit,
                "cell_hit": cell_hit,
                "source_count": len(cited_sources),
            }
        )
    return {
        "points": points,
        "page_hits": page_hits,
        "table_hits": table_hits,
        "cell_hits": cell_hits,
        "details": details,
    }
