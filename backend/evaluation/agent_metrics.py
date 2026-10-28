"""评测统计工具：Wilson 区间、比率、聚合。全部是纯函数。

为什么不用正态近似（Wald）：n=6 时 Wald 区间会算出负数下界，还会在 6/6 时给出
宽度为 0 的区间——"全对所以零误差"是典型的统计错觉。Wilson 在极端比例和小子样
下仍然给出有意义的宽度，这正是本项目最需要的性质（每维只有 6 条）。
"""

import math
from math import comb
from typing import Iterable, Optional, Sequence

# 95% 双侧正态分位数
Z_95 = 1.959963984540054


def wilson_interval(successes: int, total: int, z: float = Z_95) -> tuple[float, float]:
    """Wilson score 区间。total <= 0 时返回 (0.0, 0.0)。"""
    if total <= 0:
        return (0.0, 0.0)
    successes = max(0, min(int(successes), int(total)))
    proportion = successes / total
    denominator = 1.0 + z * z / total
    centre = (proportion + z * z / (2 * total)) / denominator
    margin = (
        z
        * math.sqrt(proportion * (1 - proportion) / total + z * z / (4 * total * total))
        / denominator
    )
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def wilson_lower(successes: int, total: int, z: float = Z_95) -> float:
    return wilson_interval(successes, total, z)[0]


def wilson_upper(successes: int, total: int, z: float = Z_95) -> float:
    return wilson_interval(successes, total, z)[1]


def interval_width(successes: int, total: int, z: float = Z_95) -> float:
    lower, upper = wilson_interval(successes, total, z)
    return upper - lower


def ratio(numerator: int, denominator: int) -> Optional[float]:
    """分母为 0 时返回 None，不用 0 代替——0 会被读成"全错"。"""
    if denominator <= 0:
        return None
    return numerator / denominator


def percentage(numerator: int, denominator: int, digits: int = 1) -> str:
    value = ratio(numerator, denominator)
    if value is None:
        return "N/A"
    return f"{value * 100:.{digits}f}%"


def format_interval(successes: int, total: int, digits: int = 1) -> str:
    """把 `5/6 (83.3%) [43.6%, 97.0%]` 这个口径固定下来，报告里各处复用。"""
    if total <= 0:
        return "N/A"
    lower, upper = wilson_interval(successes, total)
    return (
        f"{successes}/{total} ({percentage(successes, total, digits)}) "
        f"[{lower * 100:.{digits}f}%, {upper * 100:.{digits}f}%]"
    )


def median(values: Sequence[float]) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return float(ordered[middle])
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def percentile(values: Sequence[float], fraction: float) -> Optional[float]:
    """线性插值分位数。n=30 时 P95 由 1–2 个样本决定，报告里只进看板。"""
    if not values:
        return None
    if not 0.0 <= fraction <= 1.0:
        raise ValueError("fraction 必须在 [0, 1] 内")
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = fraction * (len(ordered) - 1)
    lower_index = math.floor(position)
    upper_index = math.ceil(position)
    if lower_index == upper_index:
        return float(ordered[lower_index])
    weight = position - lower_index
    return ordered[lower_index] * (1 - weight) + ordered[upper_index] * weight


def count_by(items: Iterable, key) -> dict:
    counts: dict = {}
    for item in items:
        label = key(item)
        counts[label] = counts.get(label, 0) + 1
    return counts


def agreement(values: Sequence[object]) -> Optional[float]:
    """重复判分的一致率：众数占比。全一致 = 1.0，两两不同 = 0.5（n=2 时）。"""
    if not values:
        return None
    counts = count_by(values, lambda value: value)
    return max(counts.values()) / len(values)


# ---------------------------------------------------------------------------
# 分辨率的两个检验：回答"这套题集能看见多大的差异"
# ---------------------------------------------------------------------------


def _hypergeom_pmf(k: int, total: int, successes: int, draws: int) -> float:
    if k < max(0, draws - (total - successes)) or k > min(draws, successes):
        return 0.0
    return comb(successes, k) * comb(total - successes, draws - k) / comb(total, draws)


def fisher_exact_two_sided(a_success: int, a_total: int, b_success: int, b_total: int) -> float:
    """两独立比例（各 n 条任务）的 Fisher 精确检验，双侧。

    用途：判断"两批独立运行的维度差"是不是真差异。n=6 时会很失望——
    5/6 vs 2/6 也只有 p≈0.24，只有 6/6 vs 1/6 才过 0.05。
    """
    total = a_total + b_total
    successes = a_success + b_success
    p_observed = _hypergeom_pmf(a_success, total, successes, a_total)
    low = max(0, a_total - (total - successes))
    high = min(a_total, successes)
    p_value = sum(
        _hypergeom_pmf(k, total, successes, a_total)
        for k in range(low, high + 1)
        if _hypergeom_pmf(k, total, successes, a_total) <= p_observed + 1e-12
    )
    return min(1.0, p_value)


def mcnemar_exact(fixed: int, broke: int) -> float:
    """配对设计的精确 McNemar：同一批任务上，改进修好 fixed 条、弄坏 broke 条。"""
    pairs = fixed + broke
    if pairs == 0:
        return 1.0
    tail = sum(comb(pairs, i) for i in range(0, min(fixed, broke) + 1)) / 2 ** pairs
    return min(1.0, 2 * tail)


def fisher_min_split(total: int, alpha: float = 0.05) -> Optional[tuple[int, int]]:
    """`total/total` 对上 `k/total`，找**仍显著的最大 k**（k 越大越接近无差异）。

    返回的是"最小可检出差异"，不是"最极端差异"。
    """
    for other in range(total, -1, -1):
        if fisher_exact_two_sided(total, total, other, total) < alpha:
            return (total, other)
    return None


def mcnemar_min_split(pairs: int, alpha: float = 0.05) -> Optional[tuple[int, int]]:
    """`pairs` 个不一致对里，最多允许几条"退步"仍显著。"""
    best = None
    for broke in range(pairs // 2 + 1):
        if mcnemar_exact(pairs - broke, broke) < alpha:
            best = (pairs - broke, broke)
    return best
