"""辅助问答的确定性检查。全部纯函数，不调模型、不读语料。

判分边界与金融域一致：机器能判的才判。

| 判 | 不判 |
|---|---|
| 状态是否符合产品契约 | 定义写得是否严谨 |
| 六类拒答编码是否正确 | 拒答理由是否有说服力 |
| 关键事实的可命中词是否出现 | 同义改写是否成立（留给 judge） |
| 数字是否对得上、是否能在来源里找到 | 来源是否真的支撑整句话（judge 可补） |
| 是否出现本地文档坐标、是否编造来源 | 文风与长度 |
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable, Optional

NUMBER_RE = re.compile(r"(?<![\d.])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(\s*[%％])?")
TIME_RE = re.compile(r"(?<!\d)(\d{1,2}:\d{2})(?!\d)")
DATE_RE = re.compile(r"\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?")
YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
LOCATOR_PATTERNS = (
    re.compile(r"\[\d+\]"),
    re.compile(r"#t\d+", re.IGNORECASE),
    re.compile(r"\bP\.\s*\d+", re.IGNORECASE),
    re.compile(r"第\s*\d+\s*页"),
    re.compile(r"表\s*\d+\s*[/·]?"),
)
ATTRIBUTION_RE = re.compile(r"[（(]\s*([^（）()，,。；;]{1,40}?)\s*[，,]\s*[^（）()]{0,20}[）)]")
SENTENCE_RE = re.compile(r"[^。！？!?；;\n]+[。！？!?；;]?")


def normalize(text: object) -> str:
    """全角→半角、去空白、统一小写；用于“是否出现”的宽松匹配。"""
    value = unicodedata.normalize("NFKC", str(text or ""))
    value = value.replace("\u3000", " ")
    return re.sub(r"\s+", "", value).lower()


def split_sentences(text: object) -> list[str]:
    raw = [item.strip() for item in SENTENCE_RE.findall(str(text or "")) if item.strip()]
    # 来源标注常写成句号后的「（来源名，日期）」；它是上一句的归属，不是新句子。
    merged: list[str] = []
    for sentence in raw:
        if merged and sentence.startswith(("（", "(")) and len(sentence) <= 80:
            merged[-1] = merged[-1] + sentence
            continue
        merged.append(sentence)
    return merged


def number_tokens(text: object) -> list[tuple[str, Optional[float], bool]]:
    """返回 [(原始 token, 数值, 是否百分比)]；时间单独作为字符串 token（数值 None）。"""
    value = unicodedata.normalize("NFKC", str(text or ""))
    value = DATE_RE.sub(" ", value)
    tokens: list[tuple[str, Optional[float], bool]] = []
    for match in TIME_RE.finditer(value):
        tokens.append((match.group(1), None, False))
    for match in NUMBER_RE.finditer(value):
        raw = match.group(1).replace(",", "")
        percent = bool(match.group(2))
        try:
            number = float(raw)
        except ValueError:
            continue
        tokens.append((match.group(1) + ("%" if percent else ""), number, percent))
    return tokens


def _same_number(expected: str, actual: tuple[str, Optional[float], bool],
                 tolerance: float = 0.02) -> bool:
    expected_norm = unicodedata.normalize("NFKC", str(expected)).strip()
    if ":" in expected_norm:
        return actual[0] == expected_norm
    expected_percent = expected_norm.endswith("%") or expected_norm.endswith("％")
    expected_raw = expected_norm.rstrip("%％").replace(",", "")
    try:
        expected_value = float(expected_raw)
    except ValueError:
        return False
    raw, value, percent = actual
    if value is None:
        return False
    if expected_percent != percent:
        return False
    if expected_value == 0:
        return abs(value) < tolerance
    return abs(value - expected_value) / abs(expected_value) <= tolerance


def contains_any(answer: object, choices: Iterable[str]) -> list[str]:
    text = normalize(answer)
    return [str(choice) for choice in choices if normalize(choice) and normalize(choice) in text]


def check_key_fact(answer: object, fact: dict) -> dict:
    hits = contains_any(answer, fact.get("must_include_any") or [])
    text_ok = bool(hits)
    numbers = [str(item) for item in (fact.get("numbers") or [])]
    tokens = number_tokens(answer)
    number_hits = [
        expected for expected in numbers
        if any(_same_number(expected, token) for token in tokens)
    ]
    numbers_ok = (not numbers) or bool(number_hits)
    # 数字命中只作诊断：中文写法（千分之三）与百分号写法（0.3%）等价，
    # 硬绑成失败会把正确表述判错。真正的数字红线在 no_ungrounded_number。
    return {
        "ok": bool(text_ok),
        "text_ok": text_ok,
        "numbers_ok": numbers_ok,
        "hits": hits,
        "number_hits": number_hits,
        "expected_numbers": numbers,
    }


def check_forbidden(answer: object, forbidden: Iterable[str]) -> list[str]:
    text = normalize(answer)
    hits: list[str] = []
    for item in forbidden or []:
        raw = str(item)
        if raw.startswith("re:"):
            try:
                if re.search(raw[3:], str(answer or "")):
                    hits.append(raw)
            except re.error:
                continue
            continue
        if normalize(raw) and normalize(raw) in text:
            hits.append(raw)
    return hits


def check_local_locator(answer: object) -> list[str]:
    hits: list[str] = []
    for pattern in LOCATOR_PATTERNS:
        for match in pattern.findall(str(answer or "")):
            hits.append(str(match))
    return hits


def source_labels(result: dict) -> list[str]:
    labels: list[str] = []
    for source in result.get("web_sources") or []:
        if not isinstance(source, dict):
            continue
        for key in ("label", "domain"):
            value = str(source.get(key) or "").strip()
            if value and value not in labels:
                labels.append(value)
    return labels


def source_text(result: dict) -> str:
    parts: list[str] = []
    for source in result.get("web_sources") or []:
        if not isinstance(source, dict):
            continue
        parts.append(str(source.get("title") or ""))
        parts.append(str(source.get("snippet") or ""))
        parts.append(str(source.get("published_at") or ""))
    return " ".join(parts)


def check_attribution(answer: object, result: dict, *, required: bool) -> dict:
    labels = source_labels(result)
    numeric_sentences = [sentence for sentence in split_sentences(answer) if number_tokens(sentence)]
    if not numeric_sentences:
        return {
            "ok": (not required) or bool(labels),
            "required": bool(required),
            "labels": labels,
            "numeric_sentences": 0,
            "attributed_sentences": 0,
            "detail": "答案没有数字，无法判断逐句归属" if required else "未要求来源",
        }
    attributed = [
        sentence for sentence in numeric_sentences
        if any(label and label in sentence for label in labels)
    ]
    ok = bool(labels) and len(attributed) > 0
    if not required and not labels:
        ok = True
    return {
        "ok": ok,
        "required": bool(required),
        "labels": labels,
        "numeric_sentences": len(numeric_sentences),
        "attributed_sentences": len(attributed),
        "detail": f"{len(attributed)}/{len(numeric_sentences)} 个含数字句子带来源",
    }


def check_ungrounded_numbers(answer: object, result: dict, question: object) -> dict:
    """答案里的数字必须能在问题或来源找到；年份豁免。

    这是费率题的硬约束：模型可以不知道税率，但不能凭记忆报一个来源里没有的数。
    """
    allowed_text = normalize(question) + normalize(source_text(result))
    allowed_tokens = number_tokens(question) + number_tokens(source_text(result))
    ungrounded: list[str] = []
    for raw, value, percent in number_tokens(answer):
        if value is None:
            # 时间格式单独比字符串
            if raw not in allowed_text:
                ungrounded.append(raw)
            continue
        if value and 1900 <= value <= 2100 and not percent:
            continue  # 年份不算数值断言
        if raw and raw in allowed_text:
            continue
        if any(_same_number(raw, token) for token in allowed_tokens):
            continue
        ungrounded.append(raw)
    return {"ok": not ungrounded, "ungrounded": ungrounded}


def check_status(result: dict, expected: str) -> dict:
    actual = str(result.get("status") or "")
    return {
        "ok": actual == expected,
        "expected": expected,
        "actual": actual,
        "needs_review": actual == "needs_review",
    }


def check_refusal_code(result: dict, expected: Optional[str]) -> dict:
    refusal = result.get("refusal") or {}
    actual = str(refusal.get("code") or "")
    return {
        "ok": (actual == expected) if expected else (not actual),
        "expected": expected,
        "actual": actual,
        "level": refusal.get("level"),
        "recover_by": refusal.get("recover_by"),
    }


def check_refusal_level(result: dict, category: str) -> dict:
    refusal = result.get("refusal") or {}
    expected_level = {
        "ambiguous_scope": 1,
        "out_of_domain": 2,
        "unsupported_intent": 2,
        "out_of_scope": 3,
        "insufficient_evidence": 3,
        "source_conflict": 3,
    }.get(category)
    expected_recover = {
        "ambiguous_scope": "user",
        "out_of_domain": "none",
        "unsupported_intent": "none",
        "out_of_scope": "corpus",
        "insufficient_evidence": "corpus",
        "source_conflict": "manual",
    }.get(category)
    level = refusal.get("level")
    recover_by = refusal.get("recover_by")
    return {
        "ok": (level == expected_level) and (recover_by == expected_recover),
        "expected_level": expected_level,
        "actual_level": level,
        "expected_recover_by": expected_recover,
        "actual_recover_by": recover_by,
    }
