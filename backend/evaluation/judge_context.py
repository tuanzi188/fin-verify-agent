"""按真实坐标构造完整判分资料，超出预算时显式停止判分。"""

from __future__ import annotations

import re

JUDGE_CONTEXT_VERSION = "coordinate-full-v1"
JUDGE_CONTEXT_MAX_CHARS = 60000


class JudgeContextError(ValueError):
    """证据缺失、坐标不合法或完整上下文超过预算。"""


def judge_coordinate_content(
    anchor: str,
    chunks: dict[str, dict],
    *,
    numeric_only: bool = False,
) -> str:
    """细粒度引用只读取目标行列；整表和段落引用保留完整资料。"""
    exact = chunks.get(anchor)
    base_id = anchor if exact else "#".join(anchor.split("#")[:2])
    source = exact or chunks.get(base_id)
    if not source:
        raise JudgeContextError(f"判分证据不存在：{anchor}")
    table = source.get("table")
    if isinstance(table, dict):
        columns = [str(value) for value in table.get("columns") or []]
        rows = table.get("rows") or []
        row_ids = list(range(len(rows)))
        column_ids = list(range(len(columns)))
        suffix = anchor[len(base_id) :] if anchor.startswith(base_id) else ""
        if suffix.startswith("#rows#"):
            row_part, separator, column_part = suffix[6:].partition("#cols#")
            row_ids = judge_index_list(row_part, len(rows))
            if separator and column_part:
                column_ids = judge_index_list(column_part, len(columns))
        elif suffix:
            row_token, separator, column_token = suffix[1:].partition("#")
            if not separator:
                raise JudgeContextError(f"单元格坐标不完整：{anchor}")
            row_ids = [
                judge_named_index(
                    row_token, [str(row[0]) if row else "" for row in rows]
                )
            ]
            column_ids = [judge_named_index(column_token, columns)]
        selected_columns = list(dict.fromkeys([0, *column_ids]))
        if numeric_only:
            numeric_lines: list[str] = []
            for row_id in row_ids:
                values = rows[row_id]
                label = str(values[0]) if values else ""
                for column_id in selected_columns:
                    if column_id == 0 or column_id >= len(values):
                        continue
                    raw_value = str(values[column_id] or "")
                    unit_match = re.search(
                        r"亿元|百万元|万元|千元|元/股|元|%|％|倍",
                        label + columns[column_id],
                    )
                    unit = unit_match[0] if unit_match else str(table.get("unit") or "")
                    numeric_lines.append(raw_value + " " + unit)
            return "\n".join(numeric_lines)
        header = (
            f"表 {base_id}｜{table.get('caption') or source.get('title') or ''}"
            f"｜单位：{table.get('unit') or '未标注'}"
            f"｜口径：{table.get('statement_scope') or '未标注'}"
        )
        lines = [header, " | ".join(columns[index] for index in selected_columns)]
        for row_id in row_ids:
            values = rows[row_id]
            lines.append(
                f"[行{row_id}] "
                + " | ".join(
                    str(values[index]) if index < len(values) else ""
                    for index in selected_columns
                )
            )
        if table.get("footnote"):
            lines.append(f"脚注：{table['footnote']}")
        return "\n".join(lines)
    content = str(source.get("full_content") or source.get("content") or "")
    if source.get("truncated") and not source.get("full_content"):
        raise JudgeContextError(f"判分证据只有截断快照：{anchor}")
    if not content.strip():
        raise JudgeContextError(f"判分证据为空：{anchor}")
    return content


def judge_index_list(value: str, size: int) -> list[int]:
    """解析明确索引与闭区间，不对无效坐标猜测。"""
    indices: list[int] = []
    for token in value.split(","):
        match = re.fullmatch(r"(\d+)(?:-(\d+))?", token.strip())
        if not match:
            raise JudgeContextError(f"无效行列索引：{value}")
        first, last = int(match[1]), int(match[2] or match[1])
        if first > last or last >= size:
            raise JudgeContextError(f"行列索引越界：{value}")
        indices.extend(range(first, last + 1))
    return list(dict.fromkeys(indices))


def judge_named_index(value: str, labels: list[str]) -> int:
    """名称精确优先，数字索引兜底，拒绝模糊替代财务科目。"""
    normalized = re.sub(r"\s+", "", value)
    matches = [
        index
        for index, label in enumerate(labels)
        if re.sub(r"\s+", "", label) == normalized
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches and value.isdigit() and int(value) < len(labels):
        return int(value)
    raise JudgeContextError(f"行列名称缺失或不唯一：{value}")


def validate_judge_context_size(prompt: str) -> str:
    """完整性优先；预算不足记为无法判分，不默默删掉答案末尾。"""
    if len(prompt) > JUDGE_CONTEXT_MAX_CHARS:
        raise JudgeContextError(
            f"完整判分上下文 {len(prompt)} 字符超过 {JUDGE_CONTEXT_MAX_CHARS} 上限"
        )
    return prompt
