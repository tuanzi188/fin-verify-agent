"""关联表格下方紧邻的脚注，保留页码及原始区域供人工核验。"""

from __future__ import annotations

import re


def annotate_finance_tables(tables: list[dict], lines: list) -> list[dict]:
    """返回副本；只取同页、横向相交、距离不超过 72 点的注释段。"""
    annotated: list[dict] = []
    for table in tables:
        updated = dict(table)
        if table.get("footnote") or table.get(
            "page_end", table.get("page")
        ) != table.get("page"):
            annotated.append(updated)
            continue
        bbox = table.get("bbox") or []
        if len(bbox) != 4:
            annotated.append(updated)
            continue
        x0, _, x1, bottom = bbox
        next_table_top = min(
            (
                other["bbox"][1]
                for other in tables
                if other is not table
                and other.get("page") == table.get("page")
                and len(other.get("bbox") or []) == 4
                and other["bbox"][1] >= bottom
            ),
            default=bottom + 72,
        )
        candidates = sorted(
            (
                line
                for line in lines
                if line.page == table.get("page")
                and bottom <= line.y0 < min(bottom + 72, next_table_top)
                and line.x1 >= x0
                and line.x0 <= x1
            ),
            key=lambda line: (line.y0, line.x0),
        )
        notes: list = []
        for line in candidates:
            if not notes:
                if re.match(
                    r"^(?:注(?:\d+|[一二三四五六七八九十]+)?[：:、.．]|说明[：:]|备注[：:]|\*|※)",
                    line.text.strip(),
                ):
                    notes.append(line)
                else:
                    break
            elif line.y0 - notes[-1].y1 <= 12 and not re.match(
                r"^(?:第[一二三四五六七八九十]+[章节]|[一二三四五六七八九十]+[、.]|[（(][一二三四五六七八九十]+[）)])",
                line.text.strip(),
            ):
                notes.append(line)
            else:
                break
        if notes:
            updated["footnote"] = "\n".join(line.text for line in notes)
            updated["footnote_page"] = table["page"]
            updated["footnote_bbox"] = [
                min(line.x0 for line in notes),
                min(line.y0 for line in notes),
                max(line.x1 for line in notes),
                max(line.y1 for line in notes),
            ]
        annotated.append(updated)
    return annotated
