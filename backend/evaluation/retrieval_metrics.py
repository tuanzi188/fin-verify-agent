"""检索段指标：把"检索对了没有"从黑盒变成可复现的确定性读数。

**为什么要有这一层**：当前评测全部压在答案层。一道题答错时，分不清是
"没检到"还是"检到了没用上"——前者该修切片/过滤/召回，后者该修生成/决策。
这个模块只读 task 与 result，不调模型、不碰数据库，零 judge 成本。

指标定义（适配结构检索：检索单元 = 表/表格行组/章节/固定窗）：

- **point hit@K**：某个金标要点在 top-K 里至少有一个可接受位置 ⇒ 该要点命中。
  一个事实常有 2–3 张表能证明，按"任一处"判，不要求全中。
- **task hit all@K**：该题全部金标要点都命中（严口径）。
- **task hit any@K**：至少命中一个要点（松口径，用来看"有没有摸到边"）。
- **MRR**：每个要点首次出现排名的倒数，对要点取均值。
- **context precision@K**：top-K 检索单元里，命中任一金标位置的比例。
- **context recall@K**：= point hit@K（金标要点被取到的比例）。
- **read hit**：金标位置是否被真正读开（read_block/read_section/get_table/
  get_rows/get_cell），用来区分"检索到了"与"读了"。

**两把尺子必须同时报**：

- `first_search`：只看第一次检索调用。RAG 臂只有一次机会，这是**双臂同尺**的主口径；
- `any_search`：Agent 所有检索调用的并集（每个要点取最好排名）。这是 Agent 的
  "最终有没有找到"诊断口径，**不能拿来和一次检索的 RAG 比 HitRate**——
  多给几次机会本身就会抬高命中率。

**位置折叠**：所有锚点都折回它所属的表/章节（`doc#t0013#rows#...` → `doc#t0013`）。
不折叠会把"读了正确表格里的行组"判成没命中——这是 2026-10-28 已经踩过的坑
（`agent_checks.base_chunk_id` 同一个规则）。

**固定窗要按 source_spans 展开**：512 字窗的 chunk_id 只带一个主来源，
实际覆盖多个来源；只认主来源会系统性低估跨来源窗口的命中。
"""

from __future__ import annotations

import argparse
import json
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from evaluation._runner_common import load_tasks
from evaluation.agent_metrics import ratio, wilson_interval

#: 报告里统一报这几个 K。5 是 Agent search_blocks 的上限，也是"双臂同尺"的主口径；
#: 8 是固定窗 RAG 的默认 top-k；1/3 用来看头部排序质量。
K_LIST = (1, 3, 5, 8)

SEARCH_TOOLS = ("search_blocks", "search_sections", "search_chunks")
READ_TOOLS = ("read_block", "read_section", "get_table", "get_rows", "get_cell")


# ---------------------------------------------------------------------------
# 位置折叠
# ---------------------------------------------------------------------------


def base_position(unit_id: str) -> str:
    """把任意检索/读取锚点折回它所属的表或章节。

    已知格式（`#` 分隔）：

    - 章节 `doc#s0009`
    - 表格 `doc#t0013`
    - 固定窗 chunk `doc#t0013#c0001`
    - 正文段落块 `doc#s0009#b000`
    - 表格行组块 `doc#t0013#rows#0,1,2#cols#0,1`
    - 单元格锚点 `doc#t0013#<row>#<column>`

    前两段永远是"文档 + 表/章节"，所以 ≥3 段一律取前两段；≤2 段原样返回。
    """
    text = str(unit_id or "")
    parts = text.split("#")
    if len(parts) >= 3:
        return "#".join(parts[:2])
    return text


@lru_cache(maxsize=1)
def section_tables() -> dict[str, list[str]]:
    """`section_id -> [table_id, ...]`。

    只给 `search_sections` 用：章节定位器命中一个章节，等于把该章节下的表
    放进了候选集（模型下一步可以 get_table）。块检索不展开——表格行组是独立
    检索单元，把同章节的段落块算成命中会虚高。
    """
    from evaluation.finance_corpus import DOCUMENT_DIR

    mapping: dict[str, list[str]] = {}
    if not DOCUMENT_DIR.exists():
        return mapping
    for path in sorted(DOCUMENT_DIR.glob("*/document.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        for section in document.get("sections") or []:
            section_id = str(section.get("section_id") or "")
            if section_id:
                mapping[section_id] = [str(item) for item in section.get("table_ids") or []]
    return mapping


# ---------------------------------------------------------------------------
# 金标位置
# ---------------------------------------------------------------------------


def gold_points(task: dict) -> list[dict]:
    """从任务卡抽金标要点与可接受位置。

    优先级：`answer_points[].evidence.table_id` + `answer_points[].chunk_ids`
    → `expected_source`（新 schema，可选）→ `relevant_chunk_ids`（兜底）。
    `kind=note` 且没有任何位置的定性要点不进检索分母（judge 判，不靠检索）。
    """
    points: list[dict] = []
    expected_source = task.get("expected_source")
    if isinstance(expected_source, dict):
        expected_source = [expected_source]
    extra_positions = [
        str(item.get("id") or item.get("table_id") or item.get("section_id") or "")
        for item in (expected_source or [])
        if isinstance(item, dict)
    ]

    for index, point in enumerate(task.get("answer_points") or []):
        if not isinstance(point, dict):
            continue
        positions: list[str] = []
        evidence = point.get("evidence") if isinstance(point.get("evidence"), dict) else {}
        table_id = str(evidence.get("table_id") or "")
        if table_id:
            positions.append(base_position(table_id))
        for chunk_id in point.get("chunk_ids") or []:
            folded = base_position(str(chunk_id))
            if folded:
                positions.append(folded)
        for position in extra_positions:
            if position:
                positions.append(base_position(position))

        positions = _dedupe(positions)
        if not positions:
            # 定性结论 / 纯拒答要点：没有可检索位置，交给 judge
            continue
        points.append(
            {
                "index": index,
                "positions": positions,
                "derived": bool(point.get("derived")),
                "kind": str(point.get("kind") or "fact"),
                "value": evidence.get("value"),
                "row": evidence.get("row"),
                "column": evidence.get("column"),
            }
        )

    if not points:
        fallback = [
            base_position(str(chunk_id))
            for chunk_id in task.get("relevant_chunk_ids") or []
            if chunk_id
        ]
        fallback = _dedupe(fallback)
        if fallback:
            points.append(
                {
                    "index": 0,
                    "positions": fallback,
                    "derived": False,
                    "kind": "task_level",
                    "value": None,
                    "row": None,
                    "column": None,
                }
            )
    return points


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.append(value)
    return seen


# ---------------------------------------------------------------------------
# 从 trace 抽检索单元
# ---------------------------------------------------------------------------


def _steps(result: dict) -> list[dict]:
    return [step for step in result.get("trace") or [] if step.get("tool") != "__final__"]


def _source_lookup(result: dict) -> dict[str, list[str]]:
    """固定窗 chunk 的 `chunk_id -> source_ids`（来自 evidence 的 source_spans）。"""
    lookup: dict[str, list[str]] = {}
    for item in result.get("evidence") or []:
        if not isinstance(item, dict):
            continue
        chunk_id = str(item.get("chunk_id") or "")
        if not chunk_id:
            continue
        sources: list[str] = []
        for span in item.get("source_spans") or []:
            if isinstance(span, dict) and span.get("source_id"):
                sources.append(str(span["source_id"]))
        for source_id in item.get("source_ids") or []:
            if source_id:
                sources.append(str(source_id))
        if sources:
            lookup[chunk_id] = _dedupe(sources)
    return lookup


def _unit_positions(
    unit_id: str,
    tool: str,
    lookup: dict[str, list[str]],
    section_map: dict[str, list[str]],
) -> list[str]:
    """一个检索单元覆盖哪些金标位置。"""
    positions: list[str] = []
    folded = base_position(unit_id)
    if folded:
        positions.append(folded)
    # 固定窗：以 source_spans 为准（chunk_id 只带主来源）
    for source_id in lookup.get(unit_id) or []:
        positions.append(base_position(source_id))
    # 章节定位：把章节下的表也放进候选集
    if tool == "search_sections":
        for table_id in section_map.get(folded) or []:
            positions.append(base_position(table_id))
    return _dedupe(positions)


def search_units(
    result: dict,
    *,
    section_map: Optional[dict[str, list[str]]] = None,
) -> list[dict]:
    """按检索调用顺序返回带排名的检索单元。

    每个单元：`{step, tool, query, rank, unit_id, positions}`。
    `rank` 是**该次调用内**的排名（1 起）。
    """
    section_map = section_map if section_map is not None else section_tables()
    lookup = _source_lookup(result)
    units: list[dict] = []
    for step in _steps(result):
        tool = str(step.get("tool") or "")
        if tool not in SEARCH_TOOLS:
            continue
        args = step.get("args") or {}
        query = str(args.get("query") or "")
        for rank, unit_id in enumerate(step.get("result_ids") or [], 1):
            unit_id = str(unit_id or "")
            if not unit_id:
                continue
            units.append(
                {
                    "step": step.get("step"),
                    "tool": tool,
                    "query": query,
                    "rank": rank,
                    "unit_id": unit_id,
                    "positions": _unit_positions(unit_id, tool, lookup, section_map),
                }
            )
    return units


def read_units(
    result: dict,
    *,
    section_map: Optional[dict[str, list[str]]] = None,
) -> list[dict]:
    """读取类调用覆盖的位置（不算排名，只用于 read hit）。"""
    section_map = section_map if section_map is not None else section_tables()
    lookup = _source_lookup(result)
    units: list[dict] = []
    for step in _steps(result):
        tool = str(step.get("tool") or "")
        if tool not in READ_TOOLS:
            continue
        for unit_id in step.get("result_ids") or []:
            unit_id = str(unit_id or "")
            if not unit_id:
                continue
            units.append(
                {
                    "step": step.get("step"),
                    "tool": tool,
                    "unit_id": unit_id,
                    "positions": _unit_positions(unit_id, tool, lookup, section_map),
                }
            )
    return units


def search_calls(
    result: dict,
    *,
    section_map: Optional[dict[str, list[str]]] = None,
) -> list[dict]:
    """按检索调用分组，**零结果的检索也保留**。

    "搜了但没搜到"和"根本没搜"是两件事：前者是召回问题，后者是决策问题。
    只从 units 分组会把零结果调用丢掉，报告里的"检索次数"就会偏小。
    """
    section_map = section_map if section_map is not None else section_tables()
    lookup = _source_lookup(result)
    calls: list[dict] = []
    for step in _steps(result):
        tool = str(step.get("tool") or "")
        if tool not in SEARCH_TOOLS:
            continue
        args = step.get("args") or {}
        units = []
        for rank, unit_id in enumerate(step.get("result_ids") or [], 1):
            unit_id = str(unit_id or "")
            if not unit_id:
                continue
            units.append(
                {
                    "step": step.get("step"),
                    "tool": tool,
                    "query": str(args.get("query") or ""),
                    "rank": rank,
                    "unit_id": unit_id,
                    "positions": _unit_positions(unit_id, tool, lookup, section_map),
                }
            )
        calls.append(
            {
                "step": step.get("step"),
                "tool": tool,
                "query": str(args.get("query") or ""),
                "units": units,
            }
        )
    return calls


def split_searches(units: list[dict]) -> list[list[dict]]:
    """按 (step, tool, query) 把单元切成多次检索调用。"""
    groups: list[list[dict]] = []
    keys: list[tuple] = []
    for unit in units:
        key = (unit.get("step"), unit.get("tool"), unit.get("query"))
        if not keys or keys[-1] != key:
            groups.append([])
            keys.append(key)
        groups[-1].append(unit)
    return groups


# ---------------------------------------------------------------------------
# 单题打分
# ---------------------------------------------------------------------------


def _rank_for_point(point: dict, units: Sequence[dict]) -> Optional[int]:
    ranks = [
        int(unit["rank"])
        for unit in units
        if set(unit.get("positions") or []) & set(point.get("positions") or [])
    ]
    return min(ranks) if ranks else None


def _context_precision(units: Sequence[dict], gold_positions: set[str], k: int) -> Optional[float]:
    top = list(units)[: max(1, k)]
    if not top:
        return None
    relevant = sum(
        1 for unit in top if set(unit.get("positions") or []) & gold_positions
    )
    return relevant / len(top)


def _metrics(
    ranks: Sequence[Optional[int]],
    units: Optional[Sequence[dict]],
    gold_positions: set[str],
    k: int,
) -> dict:
    # 没有金标位置的题（纯拒答/纯定性）不进检索分母：
    # 把它记成"整题未命中"会把拒答题的错误算到检索头上。
    if not ranks:
        return {
            "k": k,
            "point_hits": 0,
            "point_total": 0,
            "point_hit_rate": None,
            "task_hit_all": None,
            "task_hit_any": None,
            "mrr": None,
            "context_precision": None,
            "context_recall": None,
        }
    hits = [rank for rank in ranks if rank is not None and rank <= k]
    return {
        "k": k,
        "point_hits": len(hits),
        "point_total": len(ranks),
        "point_hit_rate": ratio(len(hits), len(ranks)),
        "task_hit_all": len(hits) == len(ranks),
        "task_hit_any": bool(hits),
        "mrr": sum(1.0 / rank for rank in ranks if rank) / len(ranks),
        # any_search 把多次调用的结果并起来，没有单一有序列表，
        # context precision 只对第一次检索有意义；并集口径留 None，避免读成同一个数。
        "context_precision": (
            _context_precision(units, gold_positions, k) if units is not None else None
        ),
        "context_recall": ratio(len(hits), len(ranks)),
    }


def score_task(
    task: dict,
    result: dict,
    *,
    k_list: Sequence[int] = K_LIST,
    primary_k: int = 5,
    section_map: Optional[dict[str, list[str]]] = None,
) -> dict:
    """给一条运行打检索段分。"""
    points = gold_points(task)
    gold_positions = {position for point in points for position in point["positions"]}
    calls = search_calls(result, section_map=section_map)
    units = [unit for call in calls for unit in call["units"]]
    groups = [call["units"] for call in calls]
    first_units = groups[0] if groups else []
    read = read_units(result, section_map=section_map)
    read_positions = {position for unit in read for position in unit["positions"]}

    first_ranks = [_rank_for_point(point, first_units) for point in points]
    best_ranks = [
        _rank_for_point(point, units) for point in points
    ]
    point_rows = []
    for point, first_rank, best_rank in zip(points, first_ranks, best_ranks):
        point_rows.append(
            {
                **point,
                "first_rank": first_rank,
                "best_rank": best_rank,
                "read": bool(set(point["positions"]) & read_positions),
            }
        )

    first_metrics = {
        str(k): _metrics(first_ranks, first_units, gold_positions, k) for k in k_list
    }
    any_metrics = {
        str(k): _metrics(best_ranks, None, gold_positions, k) for k in k_list
    }

    search_precisions = [
        value
        for group in groups
        if (value := _context_precision(group, gold_positions, max(k_list))) is not None
    ]

    return {
        "task_id": str(task.get("id")),
        "dimension": str(task.get("dimension") or ""),
        "complex_type": str(task.get("complex_type") or ""),
        "primary_k": primary_k,
        "gold_points": len(points),
        "searches": len(calls),
        "search_tools": _dedupe(str(call.get("tool")) for call in calls),
        "first_search": first_metrics,
        "any_search": any_metrics,
        "mean_search_precision": (
            sum(search_precisions) / len(search_precisions) if search_precisions else None
        ),
        "read_points": sum(1 for row in point_rows if row["read"]),
        "read_rate": ratio(sum(1 for row in point_rows if row["read"]), len(point_rows)),
        "points": point_rows,
        "missed_points": [row["index"] for row in point_rows if row["first_rank"] is None],
    }


# ---------------------------------------------------------------------------
# 聚合
# ---------------------------------------------------------------------------


def _aggregate_metric(rows: list[dict], mode: str, k: int) -> dict:
    metrics = [row[mode][str(k)] for row in rows]
    scored = [item for item in metrics if item["task_hit_all"] is not None]
    point_hits = sum(item["point_hits"] for item in metrics)
    point_total = sum(item["point_total"] for item in metrics)
    task_all = sum(1 for item in scored if item["task_hit_all"])
    task_any = sum(1 for item in scored if item["task_hit_any"])
    mrr_values = [item["mrr"] for item in metrics if item["mrr"] is not None]
    precision_values = [
        item["context_precision"]
        for item in metrics
        if item["context_precision"] is not None
    ]
    task_low, task_high = wilson_interval(task_all, len(scored))
    point_low, point_high = wilson_interval(point_hits, point_total)
    return {
        "k": k,
        "tasks": len(metrics),
        "scored_tasks": len(scored),
        "task_hit_all": task_all,
        "task_hit_all_rate": ratio(task_all, len(scored)),
        "task_hit_all_wilson": [round(task_low, 4), round(task_high, 4)],
        "task_hit_any": task_any,
        "task_hit_any_rate": ratio(task_any, len(scored)),
        "point_hits": point_hits,
        "point_total": point_total,
        "point_hit_rate": ratio(point_hits, point_total),
        "point_hit_wilson": [round(point_low, 4), round(point_high, 4)],
        "mrr": round(sum(mrr_values) / len(mrr_values), 4) if mrr_values else None,
        "context_precision": (
            round(sum(precision_values) / len(precision_values), 4)
            if precision_values
            else None
        ),
        "context_recall": ratio(point_hits, point_total),
    }


def aggregate(
    rows: list[dict],
    *,
    k_list: Sequence[int] = K_LIST,
    primary_k: int = 5,
) -> dict:
    """跨题汇总。`first_search` 是双臂同尺的主口径，`any_search` 只作 Agent 诊断。"""
    if not rows:
        return {
            "tasks": 0,
            "primary_k": primary_k,
            "note": "没有可打分的运行",
        }

    by_bucket: dict[str, list[dict]] = {}
    for row in rows:
        bucket = row.get("complex_type") or row.get("dimension") or "未分桶"
        by_bucket.setdefault(bucket, []).append(row)

    def _bucket_summary(items: list[dict]) -> dict:
        read_points = sum(row.get("read_points") or 0 for row in items)
        read_total = sum(row.get("gold_points") or 0 for row in items)
        return {
            "tasks": len(items),
            "first_search": {
                str(k): _aggregate_metric(items, "first_search", k) for k in k_list
            },
            "any_search": {
                str(k): _aggregate_metric(items, "any_search", k) for k in k_list
            },
            "read": {
                "points_read": read_points,
                "points_total": read_total,
                "read_rate": ratio(read_points, read_total),
            },
        }

    read_hits = sum(row.get("read_points") or 0 for row in rows)
    read_total = sum(row.get("gold_points") or 0 for row in rows)
    search_counts = [row.get("searches") or 0 for row in rows]
    return {
        "tasks": len(rows),
        "primary_k": primary_k,
        "k_list": list(k_list),
        "definition": {
            "first_search": "只看第一次检索调用（与一次检索的 RAG 同尺）",
            "any_search": "所有检索调用的并集，每要点取最好排名（Agent 诊断口径，不可与 RAG 直接比）",
            "point_hit": "每个金标要点至少有一个可接受位置进入 top-K",
            "task_hit_all": "该题全部金标要点都进入 top-K",
            "mrr": "每个要点首次排名的倒数，对要点取均值",
            "context_precision": "top-K 检索单元中命中任一金标位置的比例",
            "read_rate": "金标位置被 read_block/read_section/get_table/get_rows/get_cell 真正读开的比例",
        },
        "first_search": {
            str(k): _aggregate_metric(rows, "first_search", k) for k in k_list
        },
        "any_search": {
            str(k): _aggregate_metric(rows, "any_search", k) for k in k_list
        },
        "read": {
            "points_read": read_hits,
            "points_total": read_total,
            "read_rate": ratio(read_hits, read_total),
        },
        "searches": {
            "mean": round(sum(search_counts) / len(search_counts), 2),
            "max": max(search_counts),
            "multi_search_tasks": sum(1 for count in search_counts if count > 1),
        },
        "by_bucket": {bucket: _bucket_summary(items) for bucket, items in sorted(by_bucket.items())},
    }


# ---------------------------------------------------------------------------
# 离线检索（零模型调用）
# ---------------------------------------------------------------------------


def _hit_chunk_id(hit: dict) -> str:
    return str(hit.get("chunk_id") or hit.get("block_id") or "")


def hits_to_result(hits: list[dict], tool: str, *, task_id: str = "offline") -> dict:
    """把一次离线检索的结果包装成与 runs JSON 同形的 result（只填检索段要的字段）。"""
    return {
        "task_id": task_id,
        "status": "answered",
        "trace": [
            {
                "step": 1,
                "tool": tool,
                "args": {"query": ""},
                "ok": bool(hits),
                "result_ids": [_hit_chunk_id(hit) for hit in hits],
                "result_count": len(hits),
                "injected": None,
            }
        ],
        "evidence": hits,
        "usage": {"steps": 1},
    }


def _structured_retriever(question: str, top_k: int) -> list[dict]:
    from evaluation import structured_corpus

    return structured_corpus.search(
        question,
        top_k=top_k,
        doc_codes=structured_corpus.docs_for_question(question),
    )


def _fixed_retriever(question: str, top_k: int) -> list[dict]:
    from evaluation import rag_corpus

    return rag_corpus.search(
        question,
        top_k=top_k,
        doc_codes=rag_corpus.docs_for_question(question),
    )


RETRIEVERS: dict[str, tuple[Callable[[str, int], list[dict]], str]] = {
    "structured": (_structured_retriever, "search_blocks"),
    "fixed": (_fixed_retriever, "search_chunks"),
}


def score_offline(
    tasks: list[dict],
    *,
    arm: str,
    top_k: int = 5,
    k_list: Sequence[int] = K_LIST,
    retriever: Optional[Callable[[str, int], list[dict]]] = None,
) -> list[dict]:
    """直接用任务问题检索，不调模型。

    这是 CI 与快速回归用的"检索入口"读数：两臂都只问一次、都只用问题原文，
    因此不受模型改写 query 的影响，也不消耗任何 token。
    """
    if retriever is None:
        if arm not in RETRIEVERS:
            raise ValueError(f"未知 arm: {arm}，可选 {sorted(RETRIEVERS)}")
        retriever, tool = RETRIEVERS[arm]
    else:
        tool = "search_blocks"
    rows = []
    for task in tasks:
        question = str(task.get("question") or "")
        hits = retriever(question, top_k)
        result = hits_to_result(hits, tool, task_id=str(task.get("id")))
        rows.append(score_task(task, result, k_list=k_list))
    return rows


# ---------------------------------------------------------------------------
# 从 runs JSON 打分
# ---------------------------------------------------------------------------


def _index_runs(results: Iterable[dict]) -> dict[str, dict]:
    """同一任务多次运行时，检索段只按 run_index=1 打一次（检索与采样无关）。"""
    indexed: dict[str, dict] = {}
    for result in results:
        task_id = str(result.get("task_id") or "")
        if not task_id:
            continue
        run_index = int(result.get("run_index") or 1)
        if task_id not in indexed or run_index < int(indexed[task_id].get("run_index") or 1):
            indexed[task_id] = result
    return indexed


def score_runs(
    tasks: list[dict],
    payload: dict,
    *,
    k_list: Sequence[int] = K_LIST,
) -> list[dict]:
    indexed = _index_runs(payload.get("results") or [])
    rows = []
    for task in tasks:
        task_id = str(task.get("id"))
        result = indexed.get(task_id)
        if result is None:
            continue
        rows.append(score_task(task, result, k_list=k_list))
    return rows


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_table(summary: dict, label: str) -> None:
    print(f"\n{label}（first_search 为主口径）")
    header = (
        f"{'K':<4}{'要点命中':<22}{'整题全中':<22}"
        f"{'MRR':<8}{'ctx precision':<14}"
    )
    print(header)
    print("-" * len(header))
    for key, entry in summary.get("first_search", {}).items():
        point_rate = entry.get("point_hit_rate")
        point = (
            f"{entry['point_hits']}/{entry['point_total']} ({point_rate:.1%})"
            if point_rate is not None
            else "n/a（无金标要点）"
        )
        task_rate = entry.get("task_hit_all_rate")
        task = (
            f"{entry['task_hit_all']}/{entry['scored_tasks']} ({task_rate:.1%})"
            if task_rate is not None
            else "n/a（无金标要点）"
        )
        precision = (
            f"{entry['context_precision']:.1%}"
            if entry.get("context_precision") is not None
            else "n/a"
        )
        mrr = f"{entry['mrr']:.3f}" if entry.get("mrr") is not None else "n/a"
        print(f"{key:<4}{point:<22}{task:<22}{mrr:<8}{precision:<14}")
    any_entry = summary.get("any_search", {}).get("5") or {}
    if any_entry:
        print(
            f"any_search@5（Agent 诊断，不与 RAG 直接比）："
            f"要点 {any_entry['point_hits']}/{any_entry['point_total']} "
            f"({any_entry['point_hit_rate']:.1%})"
        )
    read = summary.get("read") or {}
    if read.get("points_total"):
        print(
            f"read hit：{read['points_read']}/{read['points_total']} "
            f"({read['read_rate']:.1%})"
        )
    searches = summary.get("searches") or {}
    if searches:
        print(
            f"检索次数：均值 {searches.get('mean')} / 最大 {searches.get('max')} / "
            f"多次检索题 {searches.get('multi_search_tasks')}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="检索段指标（HitRate@K / MRR / precision / recall）")
    parser.add_argument("--tasks", default="evaluation/finance_tasks_complex.jsonl")
    parser.add_argument(
        "--arm",
        choices=("structured", "fixed", "both", "agent"),
        default="structured",
        help="structured/fixed 直接离线检索；agent 需要 --runs",
    )
    parser.add_argument("--runs", default="", help="run_finance_eval / run_rag_eval 产出的 JSON")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument(
        "--holdout",
        choices=("include", "only", "exclude"),
        default="exclude",
        help="默认 exclude；最终验收可用 only 只评冻结留出题",
    )
    parser.add_argument(
        "--include-holdout",
        action="store_true",
        help="兼容旧命令，等价于 --holdout include",
    )
    parser.add_argument("--out", default="", help="结果 JSON 路径")
    args = parser.parse_args()

    tasks_path = Path(args.tasks)
    if not tasks_path.is_absolute():
        tasks_path = _BACKEND_DIR / tasks_path
    if not tasks_path.exists():
        raise SystemExit(f"任务文件不存在: {tasks_path}")
    tasks = load_tasks(tasks_path)
    holdout_mode = "include" if args.include_holdout else args.holdout
    if holdout_mode != "include":
        keep = holdout_mode == "only"
        tasks = [task for task in tasks if bool(task.get("holdout")) is keep]
    if args.limit > 0:
        tasks = tasks[: args.limit]
    if not tasks:
        raise SystemExit("筛完没有任务可跑")

    payload: dict[str, Any] = {
        "kind": "retrieval_metrics",
        "tasks_file": str(tasks_path),
        "top_k": args.top_k,
        "k_list": list(K_LIST),
        "arms": {},
    }

    # mock 恢复题单列（2026-11-01 审计 #6）：注入场景的检索分布与真实问题不同
    # （FIN-26 还与 FIN-23 同题），混进主读数会重复计权、高估泛化。
    core_tasks = [task for task in tasks if not task.get("mock")]
    mock_tasks = [task for task in tasks if task.get("mock")]

    if args.arm in ("structured", "fixed", "both"):
        arms = ("structured", "fixed") if args.arm == "both" else (args.arm,)
        for arm in arms:
            rows = score_offline(core_tasks, arm=arm, top_k=args.top_k)
            summary = aggregate(rows, primary_k=args.top_k)
            payload["arms"][arm] = {"summary": summary, "tasks": rows}
            _print_table(summary, f"{arm}（离线一次检索，零模型调用；不含 mock 恢复题）")
            if mock_tasks:
                mock_rows = score_offline(mock_tasks, arm=arm, top_k=args.top_k)
                payload["arms"][arm]["mock_summary"] = aggregate(mock_rows, primary_k=args.top_k)
                payload["arms"][arm]["mock_tasks"] = mock_rows
                _print_table(
                    payload["arms"][arm]["mock_summary"],
                    f"{arm}（mock 恢复题单列：场景与真实分布不同，不进主读数）",
                )

    if args.runs or args.arm == "agent":
        if not args.runs:
            raise SystemExit("--arm agent 需要 --runs")
        runs_path = Path(args.runs)
        if not runs_path.is_absolute():
            runs_path = _BACKEND_DIR / runs_path
        run_payload = json.loads(runs_path.read_text(encoding="utf-8"))
        rows = score_runs(core_tasks, run_payload)
        summary = aggregate(rows, primary_k=args.top_k)
        payload["arms"]["agent"] = {"summary": summary, "tasks": rows}
        _print_table(summary, "agent（实际 trace；不含 mock 恢复题）")
        if mock_tasks:
            mock_rows = score_runs(mock_tasks, run_payload)
            payload["arms"]["agent"]["mock_summary"] = aggregate(mock_rows, primary_k=args.top_k)
            payload["arms"]["agent"]["mock_tasks"] = mock_rows
            _print_table(
                payload["arms"]["agent"]["mock_summary"],
                "agent（mock 恢复题单列：场景与真实分布不同，不进主读数）",
            )
    elif args.arm == "both":
        print("\n（未给 --runs，跳过 agent 臂；结构/固定两臂已离线打分）")

    if args.out:
        out_path = Path(args.out)
        if not out_path.is_absolute():
            out_path = _BACKEND_DIR / out_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n结果：{out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
