"""统一解析、清理和重映射回答中的证据与计算编号。"""

from __future__ import annotations

import re

_REFERENCE_GROUP = re.compile(
    r"[\[【]\s*(?P<ids>\d+(?:\s*[,，、]\s*\d+)*)"
    r"(?P<detail>\s*[,，、]\s*[^\d\]】][^\]】]*)?\s*[\]】]"
)
_CALC_REFERENCE = re.compile(r"[\[【]\s*计算\s*(\d+)\s*[\]】]")


def reference_markers(text: str) -> list[int]:
    """保留出现顺序；页码等描述性数字不作为引用编号。"""
    markers: list[int] = []
    for group in _REFERENCE_GROUP.finditer(text or ""):
        for number in re.findall(r"\d+", group.group("ids")):
            marker = int(number)
            if marker not in markers:
                markers.append(marker)
    return markers


def strip_references(text: str) -> str:
    return _CALC_REFERENCE.sub("", _REFERENCE_GROUP.sub("", text or ""))


def calculation_markers(text: str) -> list[int]:
    return list(
        dict.fromkeys(int(value) - 1 for value in _CALC_REFERENCE.findall(text or ""))
    )


def remap_references(text: str, mapping: dict[int, int]) -> str:
    """去重后同步改写正文编号，未知编号原样保留供校验发现。"""

    def replace_reference(group: re.Match) -> str:
        values = list(
            dict.fromkeys(
                mapping.get(int(value), int(value))
                for value in re.findall(r"\d+", group.group("ids"))
            )
        )
        detail = group.group("detail") or ""
        return "[" + ",".join(str(value) for value in values) + detail + "]"

    return _REFERENCE_GROUP.sub(replace_reference, text or "")
