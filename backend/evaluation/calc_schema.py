"""`calc` 字段的统一口径（2026-11-01 审计 #2 起）。

`calc` 允许两种形状：
- 单对象（历史形状，继续兼容）：`{"formula": ..., "inputs": {...}, "expected": ...}`；
- 对象数组：**每个需要复算的数值一条公式**，顺序与 answer_points 里
  对应数值首次出现的顺序一致。

为什么允许数组：一条任务里多个派生数值（如 CX-07 的 2025 净利率、
2024 净利率、下降百分点）原先只有一条 calc，其余派生数字没有可复算
金标，lint 只验证了第一条。数组形状让 `_lint_calc` 逐条复算。

配套口径（lint 按 error 级互检）：
- `answer_operations` 含 `calculate` ⇔ 存在 calc spec 或派生要点；
  纯披露值（如 FIN-04 的 ROE 21.29%）不许标 calculate；
- calc spec 允许没有派生要点（如 FIN-11 的千元→元换算：答案只要求
  排序，但换算本身是必须执行且可复算的计算）。
"""

from __future__ import annotations


def calc_specs(task: dict) -> list[dict]:
    """把 `calc` 归一化成 spec 列表；历史单对象形状自动包装。"""
    spec = task.get("calc")
    if isinstance(spec, dict):
        return [spec]
    if isinstance(spec, list):
        return [item for item in spec if isinstance(item, dict)]
    return []


def has_calc_spec(task: dict) -> bool:
    return bool(calc_specs(task))
