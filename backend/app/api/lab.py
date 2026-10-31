"""实验页数据：只读评测产物，不在 Web 请求里跑模型。

产品主比较固定为 **Agent vs 结构感知 RAG**。固定窗 RAG 只作为可选辅助基线：

- `compare_arms*.json` 里记录 `rag_retrieval_mode`；
- `structured` → 主比较；
- `fixed` → 只放进 `optional_baseline`，不进入主比较；
- 旧文件没记录模式 → 标成“模式未记录”，不冒充结构感知 RAG。
"""

from __future__ import annotations

import json
import logging
import os
import statistics
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query

from app.config import BASE_DIR
from app.models.finance import LabArmSummary, LabDimension, LabRunSummary

logger = logging.getLogger(__name__)
router = APIRouter()


def _evidence_dir() -> Path:
    configured = os.getenv("LAB_EVIDENCE_DIR", "").strip()
    return Path(configured).expanduser().resolve() if configured else (BASE_DIR / "docs" / "evidence")


def _safe_path(name: Optional[str], pattern: str) -> Optional[Path]:
    root = _evidence_dir()
    if name:
        if Path(name).name != name:
            raise HTTPException(status_code=400, detail="artifact 只能是文件名")
        path = root / name
        return path if path.exists() else None
    matches = sorted(root.glob(pattern), key=lambda item: item.stat().st_mtime, reverse=True)
    return matches[0] if matches else None


def _load(path: Optional[Path]) -> dict:
    if path is None:
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("lab artifact read failed %s: %s", path, exc)
        return {}


def _relative(path: Optional[Path]) -> Optional[str]:
    if path is None:
        return None
    try:
        return str(path.relative_to(BASE_DIR)).replace("\\", "/")
    except ValueError:
        return str(path)


def _generated_at(path: Path, *reports: dict) -> str:
    for report in reports:
        if report.get("generated_at"):
            return str(report["generated_at"])
    return datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")


def _rag_label(mode: Optional[str], variant: Optional[str]) -> str:
    if mode == "structured" or variant == "rag_structured":
        return "结构感知 RAG"
    if mode == "fixed" or variant == "rag":
        return "固定窗 RAG"
    return "RAG 对照（模式未记录）"


def _arm_from_compare_stats(name: str, label: str, stats: dict, payload: dict) -> LabArmSummary:
    return LabArmSummary(
        name=name,
        label=label,
        pass_at_1=stats.get("pass_rate"),
        # compare_arms 是配对运行，不提供 pass^k；不要用 pass_rate 冒充。
        pass_pow_k=None,
        tasks=int(stats.get("total") or 0),
        runs=int(stats.get("total") or 0),
        tokens_per_run=stats.get("tokens_mean"),
        latency_mean_ms=stats.get("latency_mean"),
        avg_steps=stats.get("calls_mean"),
        hard_gates={
            "hard_fail_tasks": stats.get("hard_fail_tasks", 0),
            "hard_fail_reasons": stats.get("hard_fail_reasons") or [],
        },
        manifest={
            "tasks_file": payload.get("tasks_file"),
            "paired": payload.get("paired"),
            "wilson": stats.get("wilson"),
            "citation_precision": stats.get("citation_precision"),
            "point_accuracy": stats.get("point_accuracy"),
            "rag_retrieval_mode": payload.get("rag_retrieval_mode"),
            "agent_variant": payload.get("agent_variant"),
            "rag_variant": payload.get("rag_variant"),
            "thresholds": payload.get("thresholds"),
        },
    )


def _bucket_dimensions(pairs: list[dict]) -> list[LabDimension]:
    buckets: dict[str, list[dict]] = {}
    for pair in pairs:
        buckets.setdefault(str(pair.get("complex_type") or "未分桶"), []).append(pair)
    rows: list[LabDimension] = []
    for name, items in sorted(buckets.items()):
        # compare_arms 的 pairs 里：rag 是左臂（对照），agent 是右臂。
        left = sum(1 for item in items if (item.get("rag") or {}).get("pass")) / len(items)
        right = sum(1 for item in items if (item.get("agent") or {}).get("pass")) / len(items)
        rows.append(
            LabDimension(
                name=name,
                tasks=len(items),
                baseline_pass_at_1=left,
                improved_pass_at_1=right,
                delta=right - left,
                delta_exceeds_ci_width=None,
            )
        )
    return rows


def _mcnemar_from_compare(payload: dict) -> dict:
    flips = payload.get("flips") or {}
    right_win = int(flips.get("rag_wrong_agent_right") or 0)
    right_loss = int(flips.get("rag_right_agent_wrong") or 0)
    return {
        "right_win": right_win,
        "right_loss": right_loss,
        "discordant_pairs": right_win + right_loss,
        "p_value": None,
        "significant": None,
        "note": "compare_arms 记录的是逐题配对翻转数；显著性需另行做整卷检验。",
    }


def _verdict_from_compare(payload: dict) -> str:
    thresholds = payload.get("thresholds") or {}
    pass_ok = thresholds.get("pass_ok")
    cost_ok = thresholds.get("cost_ok")
    delta = payload.get("delta_pass_rate")
    tokens = payload.get("delta_tokens_mean")
    parts = []
    if delta is not None:
        parts.append(f"通过率差 {delta:+.1%}")
    if tokens is not None:
        parts.append(f"token 增量/题 {tokens:+,.0f}")
    if pass_ok and cost_ok:
        parts.append("两项判据均达标")
    elif pass_ok:
        parts.append("通过率达标，成本超支")
    elif cost_ok:
        parts.append("成本达标，通过率未达标")
    else:
        parts.append("两项判据均未达标")
    return "；".join(parts)


def _summary_from_compare_arms(path: Path, payload: dict) -> LabRunSummary:
    mode = payload.get("rag_retrieval_mode")
    rag_label = _rag_label(mode, payload.get("rag_variant"))
    rag_arm = _arm_from_compare_stats("rag", rag_label, payload.get("rag") or {}, payload)
    agent_arm = _arm_from_compare_stats("agent", "Agent", payload.get("agent") or {}, payload)
    pairs = payload.get("pairs") or []
    base = dict(
        generated_at=_generated_at(path),
        verdict=_verdict_from_compare(payload),
        metric="paired",
        tasks_compared=int(payload.get("paired") or 0),
        dimensions=_bucket_dimensions(pairs),
        mcnemar=_mcnemar_from_compare(payload),
        artifact=_relative(path),
        compare_artifact=_relative(path),
    )

    if mode == "structured":
        return LabRunSummary(
            available=True,
            comparison_kind="agent_vs_structured_rag",
            left_label="结构感知 RAG",
            right_label="Agent",
            arms=[rag_arm, agent_arm],
            **base,
        )

    if mode == "fixed":
        return LabRunSummary(
            available=False,
            comparison_kind="agent_vs_fixed_rag",
            left_label="固定窗 RAG",
            right_label="Agent",
            arms=[],
            optional_baseline=rag_arm,
            optional_baseline_label="固定窗 RAG（可选辅助基线）",
            message=(
                "这份 compare_arms 是固定窗 RAG 的可选基线，不作为产品主比较。"
                "请用 --retrieval-mode structured 重跑 RAG 臂后再生成主比较。"
            ),
            **base,
        )

    return LabRunSummary(
        available=True,
        comparison_kind="agent_vs_rag_unknown",
        left_label=rag_label,
        right_label="Agent",
        arms=[rag_arm, agent_arm],
        message=(
            "旧 compare_arms 没有记录 rag_retrieval_mode，无法确认这是结构感知 RAG 还是固定窗 RAG；"
            "请用 run_rag_eval --retrieval-mode structured 重跑并记录模式。"
        ),
        **base,
    )


def _arm_from_agent_compare(name: str, label: str, block: dict, report: dict, cost: dict) -> LabArmSummary:
    return LabArmSummary(
        name=name,
        label=label,
        pass_at_1=block.get("pass_at_1"),
        pass_pow_k=block.get("pass_pow_k"),
        tasks=int(block.get("tasks") or 0),
        runs=int(cost.get("runs") or 0),
        tokens_per_run=cost.get("tokens_per_run"),
        median_latency_ms=cost.get("median_latency_ms"),
        avg_steps=cost.get("avg_steps"),
        hard_gates=report.get("hard_gates") or {},
        manifest={
            "model": report.get("model"),
            "provider": report.get("provider"),
            "corpus_version": report.get("corpus_version"),
            "corpus_hash": report.get("corpus_hash"),
            "tasks_sha256": report.get("tasks_sha256"),
            "runs_sha256": report.get("runs_sha256"),
            "runs_per_task": report.get("runs_per_task"),
            "judge_enabled": report.get("judge_enabled"),
            "git": report.get("git"),
            "agent_run_config": report.get("agent_run_config"),
        },
    )


def _dimensions_from_agent_compare(payload: dict) -> list[LabDimension]:
    rows: list[LabDimension] = []
    for name, item in (payload.get("by_dimension") or {}).items():
        rows.append(
            LabDimension(
                name=str(name),
                tasks=int(item.get("tasks") or 0),
                baseline_pass_at_1=item.get("baseline_pass_at_1"),
                improved_pass_at_1=item.get("improved_pass_at_1"),
                delta=item.get("delta"),
                delta_exceeds_ci_width=item.get("delta_exceeds_ci_width"),
            )
        )
    return rows


def _summary_from_agent_compare(compare_path: Path, payload: dict, baseline_path: Optional[Path], improved_path: Optional[Path]) -> LabRunSummary:
    baseline_data = _load(baseline_path)
    improved_data = _load(improved_path)
    arms = [
        _arm_from_agent_compare(
            "baseline", "Agent baseline", payload.get("baseline") or {}, baseline_data,
            (payload.get("cost") or {}).get("baseline") or {},
        ),
        _arm_from_agent_compare(
            "improved", "Agent improved", payload.get("improved") or {}, improved_data,
            (payload.get("cost") or {}).get("improved") or {},
        ),
    ]
    return LabRunSummary(
        available=True,
        comparison_kind="agent_vs_agent",
        left_label="Agent baseline",
        right_label="Agent improved",
        generated_at=_generated_at(compare_path, improved_data, baseline_data),
        verdict=payload.get("verdict"),
        metric=payload.get("metric"),
        tasks_compared=int(payload.get("tasks_compared") or 0),
        arms=arms,
        dimensions=_dimensions_from_agent_compare(payload),
        mcnemar=payload.get("mcnemar") or {},
        artifact=_relative(improved_path or baseline_path),
        compare_artifact=_relative(compare_path),
        message=(
            "Agent 双变体对照，不是产品主比较。"
            "主比较（Agent vs 结构感知 RAG）请运行 structured RAG 报告与 compare_arms。"
        ),
    )


def _compare_arms_candidates() -> list[tuple[Path, dict]]:
    root = _evidence_dir()
    paths = sorted(
        list(root.glob("compare_arms*.json")) + list(root.glob("agent_vs_*.json")),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    seen: set[Path] = set()
    rows: list[tuple[Path, dict]] = []
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        payload = _load(path)
        if payload.get("agent") and payload.get("rag"):
            rows.append((path, payload))
    return rows


@router.get("/lab/runs", response_model=LabRunSummary)
async def get_lab_runs(
    compare: Optional[str] = Query(default=None, description="compare JSON 文件名"),
    baseline: Optional[str] = Query(default=None, description="baseline report 文件名"),
    improved: Optional[str] = Query(default=None, description="improved report 文件名"),
):
    # 显式指定旧的 agent 双变体 compare 时，仍然按 agent-vs-agent 返回。
    explicit_compare = _safe_path(compare, "agent_compare*.json") if compare else None
    if compare and explicit_compare is None:
        raise HTTPException(status_code=404, detail="compare artifact 不存在")

    candidates = _compare_arms_candidates()
    structured = next((row for row in candidates if row[1].get("rag_retrieval_mode") == "structured"), None)
    if structured:
        return _summary_from_compare_arms(*structured)

    unknown = next((row for row in candidates if not row[1].get("rag_retrieval_mode")), None)
    if unknown:
        return _summary_from_compare_arms(*unknown)

    fixed = next((row for row in candidates if row[1].get("rag_retrieval_mode") == "fixed"), None)

    compare_path = explicit_compare or _safe_path(None, "agent_compare*.json")
    if compare_path is None:
        summary = LabRunSummary(
            available=False,
            comparison_kind="agent_vs_fixed_rag" if fixed else "unavailable",
            message=(
                "没有找到 Agent vs 结构感知 RAG 的 compare_arms。"
                "先运行 run_rag_eval --retrieval-mode structured、score_agent_runs、compare_arms。"
                + (" 检测到固定窗 RAG 对照，已放在可选辅助基线，不进入主比较。" if fixed else "")
            ),
        )
        if fixed:
            fixed_summary = _summary_from_compare_arms(*fixed)
            summary.optional_baseline = fixed_summary.optional_baseline
            summary.optional_baseline_label = "固定窗 RAG（可选辅助基线）"
            summary.artifact = fixed_summary.artifact
            summary.compare_artifact = fixed_summary.compare_artifact
        return summary

    baseline_path = _safe_path(baseline, "agent_baseline_report*.json")
    improved_path = _safe_path(improved, "agent_improved_report*.json")
    summary = _summary_from_agent_compare(compare_path, _load(compare_path), baseline_path, improved_path)
    if fixed:
        fixed_summary = _summary_from_compare_arms(*fixed)
        summary.optional_baseline = fixed_summary.optional_baseline
        summary.optional_baseline_label = "固定窗 RAG（可选辅助基线）"
    return summary
