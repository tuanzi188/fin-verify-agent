"""失败自动归类：把 CX-10 式的人工 trace 阅读固化成代码。

方案 §3.4 的八类（适配本项目）：

| 类别 | 判据 |
|---|---|
| 模型异常 | 运行崩溃 / status=error / run_error |
| 路由错误 | auto 选错臂（对照 routing_metrics 的金标） |
| 检索无命中 | 检索段一个金标要点都没进 top-K |
| 工具/参数错误 | trace 里出现 bad_args / not_found / invalid_args |
| 无效循环 | 同工具同参数 ≥3 次、重复跳过 ≥3 次、或预算用尽仍在重试 |
| 过早结束/误拒 | 检索已命中却拒答（正样本） |
| 上下文污染 | 引用了与问题点名的公司/期间不符的切片 |
| 权限越界 | 读取了题面范围外的文档，或调用了 allowed_tools 之外的工具 |

**一题可命中多类**；报告同时给"主因"（按固定优先级取第一个）与"共现"。
优先级把结构性原因排在语义性前面：崩溃 > 路由 > 检索 > 工具 > 循环 >
误拒 > 污染 > 越界。

这个模块不替代 `agent_report.classify_causes`：那边是"维度判定"的失败原因，
这里是"过程病征"的自动归类。两套并存，报告里分开列，避免把过程问题
混进答案质量分。
"""

from __future__ import annotations

import json
from typing import Optional

from evaluation.agent_checks import base_chunk_id
from evaluation.finance_corpus import chunk_map

CATEGORIES = (
    "模型异常",
    "路由错误",
    "检索无命中",
    "工具/参数错误",
    "无效循环",
    "过早结束/误拒",
    "上下文污染",
    "权限越界",
)

_TOOL_ERROR_KINDS = {"bad_args", "invalid_args", "not_found", "unavailable", "schema_error"}

#: 题面里出现这些词时，读别的期间是任务要求，不是越界。
_RELATIVE_PERIOD_WORDS = (
    "上年",
    "去年",
    "前年",
    "同比",
    "环比",
    "两年",
    "近三",
    "三年",
    "历年",
    "近五年",
)
#: 题面里出现这些词时，读别的公司/行业是任务要求，不是越界。
_COMPARISON_WORDS = ("同行", "对比", "比较", "相比", "分别", "两家", "三家", "行业", "竞品")


def _safe_chunk_map() -> dict[str, dict]:
    """制度库语料没有金融 document.json：取不到就返回空表，不让归类整体失败。"""
    try:
        return chunk_map()
    except Exception:  # noqa: BLE001 - 归类是附加诊断，不能拖垮判分
        return {}


def _steps(result: dict) -> list[dict]:
    return [step for step in result.get("trace") or [] if step.get("tool") != "__final__"]


def _loop_counts(result: dict) -> dict[tuple[str, str], int]:
    counts: dict[tuple[str, str], int] = {}
    for step in _steps(result):
        key = (
            str(step.get("tool") or ""),
            json.dumps(step.get("args") or {}, sort_keys=True, ensure_ascii=False),
        )
        counts[key] = counts.get(key, 0) + 1
    return counts


def _chunk_scope(chunk_id: str, chunks: dict[str, dict]) -> dict:
    base = base_chunk_id(chunk_id)
    chunk = chunks.get(base) or chunks.get(str(chunk_id)) or {}
    title = str(chunk.get("title") or "")
    return {"chunk_id": base, "title": title, "doc_id": chunk.get("doc_id")}


def _scope_violations(
    task: dict,
    result: dict,
    chunks: dict[str, dict],
) -> list[dict]:
    """题面只点名一家公司/一个期间时，读到范围外的文档就是越界。

    跨文档题（点名 ≥2 家公司）不在此列：读另一家本来就是任务要求。
    """
    from app.finance.block_index import companies_for_question, periods_for_question

    question = str(task.get("question") or "")
    companies = companies_for_question(question)
    periods = periods_for_question(question) or set()
    # 题面说了"同比/上年"或"同行/对比"时，读别的期间/公司是任务要求，不是越界。
    if any(word in question for word in _COMPARISON_WORDS):
        companies = []
    if any(word in question for word in _RELATIVE_PERIOD_WORDS):
        periods = set()
    if not companies and not periods:
        return []

    violations: list[dict] = []
    for step in _steps(result):
        tool = str(step.get("tool") or "")
        if tool in ("search_blocks", "search_sections", "search_chunks", "list_documents"):
            continue
        for chunk_id in step.get("result_ids") or []:
            scope = _chunk_scope(str(chunk_id), chunks)
            title = scope["title"]
            if not title:
                continue
            if len(companies) == 1 and companies[0] not in title:
                violations.append(
                    {
                        "tool": tool,
                        "chunk_id": scope["chunk_id"],
                        "reason": f"题面只点名 {companies[0]}，读到 {title[:40]}",
                    }
                )
            elif len(periods) == 1:
                period = next(iter(periods))
                if period and period not in title:
                    violations.append(
                        {
                            "tool": tool,
                            "chunk_id": scope["chunk_id"],
                            "reason": f"题面只点名 {period} 年，读到 {title[:40]}",
                        }
                    )
    return violations


def _citation_pollution(
    task: dict,
    result: dict,
    chunks: dict[str, dict],
) -> list[dict]:
    from app.finance.block_index import companies_for_question, periods_for_question

    question = str(task.get("question") or "")
    companies = companies_for_question(question)
    periods = periods_for_question(question) or set()
    if any(word in question for word in _COMPARISON_WORDS):
        companies = []
    if any(word in question for word in _RELATIVE_PERIOD_WORDS):
        periods = set()
    if not companies and not periods:
        return []
    polluted: list[dict] = []
    for citation in result.get("citations") or []:
        scope = _chunk_scope(str(citation.get("chunk_id") or ""), chunks)
        title = scope["title"]
        if not title:
            continue
        if len(companies) == 1 and companies[0] not in title:
            polluted.append({"chunk_id": scope["chunk_id"], "reason": "引用另一家公司的切片"})
        elif len(periods) == 1:
            period = next(iter(periods))
            if period and period not in title:
                polluted.append({"chunk_id": scope["chunk_id"], "reason": "引用另一期间的切片"})
    return polluted


def classify(
    task: dict,
    result: dict,
    checks: dict,
    judgment: Optional[dict] = None,
    *,
    retrieval: Optional[dict] = None,
    routing: Optional[dict] = None,
    chunks: Optional[dict[str, dict]] = None,
) -> dict:
    """给一条运行打八类标签。`retrieval` 是 retrieval_metrics.score_task 的结果。"""
    chunks = chunks if chunks is not None else _safe_chunk_map()
    categories: list[str] = []
    evidence: dict[str, dict] = {}

    # 1. 模型异常
    status = str(result.get("status") or "")
    note = str((result.get("trace") or [{}])[-1].get("note") or "")
    if status == "error" or (checks.get("no_crash") or {}).get("ok") is False or "run_error" in note:
        categories.append("模型异常")
        evidence["模型异常"] = {"status": status, "note": note[:200]}

    # 2. 路由错误
    if routing and routing.get("misrouted"):
        categories.append("路由错误")
        evidence["路由错误"] = {
            "expected": routing.get("expected_arm"),
            "actual": routing.get("actual_arm"),
            "reason": routing.get("route_reason"),
        }

    # 3. 检索无命中
    if retrieval and not task.get("mock"):
        primary = str(retrieval.get("primary_k") or 5)
        first = (retrieval.get("first_search") or {}).get(primary) or {}
        if first.get("point_total") and first.get("point_hits") == 0:
            categories.append("检索无命中")
            evidence["检索无命中"] = {
                "gold_points": first.get("point_total"),
                "searched": retrieval.get("searches"),
                "missed_points": retrieval.get("missed_points"),
            }

    # 4. 工具/参数错误
    tool_errors = [
        {
            "tool": step.get("tool"),
            "error_kind": step.get("error_kind"),
            "error_detail": str(step.get("error_detail") or "")[:160],
        }
        for step in _steps(result)
        if str(step.get("error_kind") or "") in _TOOL_ERROR_KINDS
    ]
    if tool_errors:
        categories.append("工具/参数错误")
        evidence["工具/参数错误"] = {"errors": tool_errors[:5], "count": len(tool_errors)}

    # 5. 无效循环
    loop_counts = _loop_counts(result)
    repeated = [
        {"tool": tool, "args": args, "count": count}
        for (tool, args), count in loop_counts.items()
        if count >= 3
    ]
    usage = result.get("usage") or {}
    steps = int(usage.get("steps") or 0)
    max_steps = int(task.get("max_steps") or 0)
    exhausted = bool(max_steps and steps >= max_steps and "budget_truncated" in note)
    if repeated or int(usage.get("duplicate_skipped") or 0) >= 3 or exhausted:
        categories.append("无效循环")
        evidence["无效循环"] = {
            "repeated": repeated[:5],
            "duplicate_skipped": usage.get("duplicate_skipped"),
            "steps": steps,
            "max_steps": max_steps or None,
            "budget_truncated": "budget_truncated" in note,
        }

    # 6. 过早结束/误拒
    false_refusal = (checks.get("false_refusal") or {}).get("ok") is False
    retrieval_hit = bool(
        retrieval
        and (retrieval.get("first_search") or {}).get(str(retrieval.get("primary_k") or 5), {}).get("point_hits")
    )
    if false_refusal or (retrieval_hit and status == "abstained" and not task.get("should_abstain")):
        categories.append("过早结束/误拒")
        evidence["过早结束/误拒"] = {
            "status": status,
            "retrieval_hit": retrieval_hit,
            "false_refusal_check": false_refusal,
        }

    # 7. 上下文污染
    polluted = _citation_pollution(task, result, chunks)
    if polluted:
        categories.append("上下文污染")
        evidence["上下文污染"] = {"citations": polluted[:5], "count": len(polluted)}

    # 8. 权限越界
    violations = _scope_violations(task, result, chunks)
    allowed_tools = task.get("allowed_tools")
    if isinstance(allowed_tools, list) and allowed_tools:
        used = {str(step.get("tool") or "") for step in _steps(result)}
        disallowed = sorted(used - {str(name) for name in allowed_tools} - {"__final__"})
        if disallowed:
            violations.append({"tool": ",".join(disallowed), "reason": "调用了 allowed_tools 之外的工具"})
    if violations:
        categories.append("权限越界")
        evidence["权限越界"] = {"violations": violations[:5], "count": len(violations)}

    ordered = [category for category in CATEGORIES if category in categories]
    return {
        "primary": ordered[0] if ordered else None,
        "categories": ordered,
        "evidence": evidence,
    }


def distribution(rows: list[dict]) -> dict:
    """按主因与共现两栏统计，只统计 `outcome == "fail"` 的运行。

    `na`（judge 不可用）与 `invalid`（注入未触发）不是失败，不能进失败分布；
    2026-10-31 用 fixture 验证时，na 运行被算进"上下文污染"就是这条没守住。
    """
    primary_counts: dict[str, int] = {}
    cooccurrence_counts: dict[str, int] = {}
    failed_runs = 0
    for row in rows:
        if row.get("outcome") != "fail":
            continue
        failed_runs += 1
        taxonomy = row.get("taxonomy") or {}
        primary = taxonomy.get("primary")
        if primary:
            primary_counts[primary] = primary_counts.get(primary, 0) + 1
        for category in taxonomy.get("categories") or []:
            cooccurrence_counts[category] = cooccurrence_counts.get(category, 0) + 1
    return {
        "failed_runs": failed_runs,
        "primary": primary_counts,
        "cooccurrence": cooccurrence_counts,
        "note": "主因按固定优先级取第一个；共现允许一题多类，合计可超过失败条数。",
    }
