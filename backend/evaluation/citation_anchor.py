"""引用定位的三级刻度：页码 / 表格 / 单元格。

为什么要和"引用是否支持结论"分开记：

- **语义支持**（judge 的 `citation_supports`）回答"这段原文能不能支撑这句话"；
- **精确定位**回答"这个引用有没有指到那张表、那一行、那一列、那一页"。

金融查证要的是后者：同一个数字可能出现在正文、附注、母公司报表里，语义上都能"支撑"，
但审计需要的是可复核的那一格。两者都报，**不允许用语义判分替代精确定位**。

三级定义（对任务卡里带 `evidence` 的要点逐条算）：

| 级别 | 命中条件 |
|---|---|
| 页码 | 被引用的 chunk 解析到的页码 == 证据表所在页 |
| 表格 | 引用的 base id == 证据表 id |
| 单元格 | 引用形如 `doc#table#row_index#column_name`，且行列与 evidence 一致 |

RAG 臂的引用是 `doc#table#cNNNN`（512 字窗口），**单元格级天然为 0**——这正是
"普通 RAG 没有精确到格"的量化表达，不是它答错了。
"""

from __future__ import annotations

from app.finance.store import FinanceStore


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


def _cell_matches(chunk_id: str, evidence: dict, store: FinanceStore) -> bool:
    parts = str(chunk_id or "").split("#")
    # 只有 get_cell 的锚点才是四段且第三段是行号；RAG 的 `#cNNNN` 不算
    if len(parts) != 4 or not parts[2].isdigit():
        return False
    table_id = str(evidence.get("table_id") or "")
    if _base(chunk_id) != table_id:
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
    return int(parts[2]) == want_row and _norm(parts[3]) == _norm(want_column)


def anchor_report(task: dict, result: dict, store: FinanceStore) -> dict:
    """返回 {points, page_hits, table_hits, cell_hits, details}。"""
    citations = [str(item.get("chunk_id") or "") for item in result.get("citations") or []]
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
        page_hit = any(_page_of(cid, store) == table_page and table_page for cid in citations)
        table_hit = any(_base(cid) == table_id for cid in citations)
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
            }
        )
    return {
        "points": points,
        "page_hits": page_hits,
        "table_hits": table_hits,
        "cell_hits": cell_hits,
        "details": details,
    }
