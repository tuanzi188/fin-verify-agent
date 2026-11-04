"""受限计算器：只允许四则运算与括号，数字用 Decimal。

为什么不让 LLM 心算、也不直接 `eval`：

1. 金融数字错一位就是事故，必须可复算；
2. `eval` 能执行任意代码，绝对不能用；
3. 计算过程要进 trace（公式 + 输入 + 结果），审计时能回放。

支持的输入：
- 变量：`calculate("(current - previous) / previous * 100", {"current": "12.34", "previous": "10.00"})`
- 百分号：`"23.4%"` 会按 `0.234` 参与计算
- 数字字符串里的千分位逗号会被去掉：`"1,234.56"` → `1234.56`
"""

from __future__ import annotations

import ast
from decimal import Decimal, DivisionByZero, InvalidOperation, localcontext
from typing import Any

MAX_EXPRESSION_LENGTH = 300
MAX_INPUTS = 50
PRECISION = 28

_ALLOWED_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod)
_ALLOWED_UNARYOPS = (ast.UAdd, ast.USub)


class CalculationError(ValueError):
    """计算表达式或输入不合法（不是模型/网络错误）。"""


def _to_decimal(value: Any, *, name: str = "") -> Decimal:
    if isinstance(value, bool):
        raise CalculationError(f"输入 {name!r} 不能是布尔值")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))

    if isinstance(value, str):
        text = value.strip().replace(",", "").replace("，", "")
        if not text:
            raise CalculationError(f"输入 {name!r} 为空")

        # 财报里负数常用括号表示：`(1,234.56)` = -1234.56；中文全角括号也认。
        negative = False
        if (text.startswith("(") and text.endswith(")")) or (
            text.startswith("（") and text.endswith("）")
        ):
            negative = True
            text = text[1:-1].strip()

        if text.endswith("%"):
            text = text[:-1].strip()
            try:
                result = Decimal(text) / Decimal(100)
            except InvalidOperation as exc:
                raise CalculationError(f"输入 {name!r} 不是合法百分数: {value!r}") from exc
            return -result if negative else result

        try:
            result = Decimal(text)
        except InvalidOperation as exc:
            raise CalculationError(f"输入 {name!r} 不是合法数字: {value!r}") from exc
        return -result if negative else result

    raise CalculationError(f"输入 {name!r} 类型不支持: {type(value).__name__}")


def _eval_node(node: ast.AST, variables: dict[str, Decimal]) -> Decimal:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, variables)

    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise CalculationError(f"表达式里只允许数字，收到 {node.value!r}")
        return Decimal(str(node.value))

    if isinstance(node, ast.Name):
        if node.id not in variables:
            raise CalculationError(f"未知变量: {node.id}")
        return variables[node.id]

    if isinstance(node, ast.UnaryOp) and isinstance(node.op, _ALLOWED_UNARYOPS):
        value = _eval_node(node.operand, variables)
        return value if isinstance(node.op, ast.UAdd) else -value

    if isinstance(node, ast.BinOp) and isinstance(node.op, _ALLOWED_BINOPS):
        left = _eval_node(node.left, variables)
        right = _eval_node(node.right, variables)
        try:
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div):
                return left / right
            if isinstance(node.op, ast.Mod):
                return left % right
        except (DivisionByZero, InvalidOperation, ZeroDivisionError) as exc:
            raise CalculationError("计算失败：除数为 0 或结果非法") from exc

    raise CalculationError(f"表达式包含不允许的语法: {type(node).__name__}")


def _format_decimal(value: Decimal) -> str:
    """输出人类可读的十进制字符串，不用科学计数法。"""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def calculate(expression: str, inputs: dict[str, Any] | None = None) -> dict:
    """执行一次受限计算，返回可直接写进 trace 的记录。

    返回：
    `{"formula": ..., "inputs": {...}, "result": "..."}`

    抛 `CalculationError`：表达式为空/过长、未知变量、非法语法、除零。
    """
    formula = str(expression or "").strip()
    if not formula:
        raise CalculationError(
            "expression 不能为空；请传 expression（也兼容 formula），"
            "例如 (a-b)/b"
        )
    if len(formula) > MAX_EXPRESSION_LENGTH:
        raise CalculationError(f"expression 超过 {MAX_EXPRESSION_LENGTH} 字符")
    if inputs is not None and not isinstance(inputs, dict):
        raise CalculationError("inputs 必须是对象")
    if len(inputs or {}) > MAX_INPUTS:
        raise CalculationError(f"inputs 超过 {MAX_INPUTS} 项")

    variables = {
        str(name): _to_decimal(value, name=str(name))
        for name, value in (inputs or {}).items()
    }
    try:
        tree = ast.parse(formula, mode="eval")
    except SyntaxError as exc:
        raise CalculationError(f"表达式语法错误: {exc.msg}") from exc

    with localcontext() as context:
        context.prec = PRECISION
        result = _eval_node(tree, variables)

    return {
        "formula": formula,
        "inputs": {name: str(value) for name, value in (inputs or {}).items()},
        "result": _format_decimal(result),
    }
