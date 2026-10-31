"""把 runner 的原始 trace 转成 HTTP 契约。

Agent runner 记录的是“运行时事实”，HTTP 层只需要一个稳定的只读视图。
集中在这里，避免 `/chat` 与 `/finance/chat` 各维护一份映射后逐渐漂移。
"""

from __future__ import annotations

from typing import Any

from app.models.chat import AgentTraceStep

_MAX_ARG_CHARS = 200


def trim_args(arguments: object) -> dict:
    """trace 里的工具参数只保留可读短值，避免把长文本塞进响应。"""
    if not isinstance(arguments, dict):
        return {}
    trimmed = {}
    for key, value in arguments.items():
        if isinstance(value, str) and len(value) > _MAX_ARG_CHARS:
            trimmed[key] = value[:_MAX_ARG_CHARS] + "…"
        else:
            trimmed[key] = value
    return trimmed


def _int_list(values: object) -> list[int]:
    if not isinstance(values, list):
        return []
    rows: list[int] = []
    for value in values:
        try:
            rows.append(int(value))
        except (TypeError, ValueError):
            continue
    return rows


def _table_coordinates(raw: dict, args: dict) -> tuple[str | None, list[int], list[int]]:
    """优先用工具参数，其次用结果锚点。

    `get_rows` 的 args 里是行名/列名，不一定是行号；`result_ids` 里的
    `table_id#rows#1,2#cols#0,1` 才是真实坐标，所以结果锚点优先。
    """
    table_id = raw.get("table_id") or args.get("table_id")
    row_indices = _int_list(raw.get("row_indices"))
    column_indices = _int_list(raw.get("column_indices"))

    for result_id in raw.get("result_ids") or []:
        text = str(result_id or "")
        if "#rows#" in text:
            head, _, rest = text.partition("#rows#")
            row_part, _, column_part = rest.partition("#cols#")
            table_id = table_id or head
            if not row_indices:
                row_indices = _int_list(row_part.split(",") if row_part else [])
            if not column_indices:
                column_indices = _int_list(column_part.split(",") if column_part else [])
        elif "#" in text and not row_indices:
            # cell 锚点：table_id#row#column
            head, _, rest = text.partition("#")
            row_token, _, column_token = rest.partition("#")
            if row_token.isdigit() and column_token:
                table_id = table_id or head
                row_indices = [int(row_token)]

    return (str(table_id) if table_id else None), row_indices, column_indices


def to_trace(trace: list[dict[str, Any]] | None) -> list[AgentTraceStep]:
    steps: list[AgentTraceStep] = []
    for raw in trace or []:
        args = trim_args(raw.get("args"))
        table_id, row_indices, column_indices = _table_coordinates(raw, args)
        steps.append(
            AgentTraceStep(
                step=int(raw.get("step") or 0),
                tool=str(raw.get("tool") or ""),
                ok=bool(raw.get("ok")),
                result_count=int(raw.get("result_count") or 0),
                latency_ms=int(raw.get("latency_ms") or 0),
                injected=raw.get("injected"),
                error_kind=raw.get("error_kind"),
                args=args,
                note=raw.get("note"),
                result_ids=[str(item) for item in (raw.get("result_ids") or [])],
                new_result_count=(
                    int(raw["new_result_count"])
                    if raw.get("new_result_count") is not None
                    else None
                ),
                truncated=bool(raw.get("truncated")),
                table_id=table_id,
                row_indices=row_indices,
                column_indices=column_indices,
                result_chars=int(raw.get("result_chars") or 0),
                payload_chars=int(raw.get("payload_chars") or 0),
                evidence_chars_added=int(raw.get("evidence_chars_added") or 0),
                duplicate_skipped=int(raw.get("duplicate_skipped") or 0),
                policy=raw.get("policy"),
                error_detail=raw.get("error_detail"),
            )
        )
    return steps
