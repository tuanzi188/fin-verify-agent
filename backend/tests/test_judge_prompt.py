"""judge prompt 的组装回归：澄清口径唯一、few-shot 注入、占位符不破。

背景（2026-11-06）：
- `_JUDGE_TEMPLATE` 曾把 `{clarify_instruction}` 与一段硬编码的
  「明确追问也算对」并列，和 `CLARIFY_INSTRUCTIONS["none"]`（反问判 bad）直接打架；
- `rubric_text()` 只给四条原子规则，`ANSWER_EQUIVALENCE_RUBRIC.md` 的 9 条
  few-shot 从没进过 judge prompt。

这两处都只改提示词文本，不跑模型，所以断言全部是确定性的字符串检查。
"""

from evaluation.agent_judge import CLARIFY_INSTRUCTIONS, build_prompt
from evaluation.equivalence import FEWSHOT_TEXT, rubric_text


def _task(clarify_mode: str = "none") -> dict:
    return {
        "id": "T1",
        "question": "2025 年营业收入是多少",
        "clarify_mode": clarify_mode,
        "answer_points": [{"text": "营业收入 456,451,731 千元", "chunk_ids": ["d#t0006"]}],
    }


def _result() -> dict:
    return {
        "task_id": "T1",
        "run_index": 1,
        "answer": "营业收入 456,451,731 千元 [1]",
        "citations": [{"marker": 1, "chunk_id": "d#t0006"}],
    }


def _render(clarify_mode: str = "none") -> str:
    return build_prompt(_task(clarify_mode), _result(), {"d#t0006": {"content": "营业收入 456,451,731 千元"}})


def test_clarify_instruction_is_the_only_authority():
    """none 分支要求直接作答、反问判 bad；模板里不能再有一句「追问都算对」。"""
    rendered = _render("none")
    assert "若回答反问用户或要求用户补充条件，判 bad" in rendered
    assert "明确追问、或把两种情形各自的结论都给出，都算对" not in rendered
    assert "按上面【澄清判定】给出的本题专属口径判断" in rendered


def test_each_clarify_mode_survives_rendering():
    """三个分支都要能 .format 成功，且各自的核心判据出现在 prompt 里。"""
    for mode, marker in (
        ("ask", "把两种情形都说到"),
        ("none", "直接作答"),
        ("assume", "说明所做假设"),
    ):
        rendered = _render(mode)
        assert marker in rendered, mode
        assert "{clarify_instruction}" not in rendered


def test_all_template_placeholders_are_filled():
    rendered = _render()
    for placeholder in (
        "{question}", "{evidence}", "{answer}", "{citation_map}",
        "{points_block}", "{rubric_block}", "{forbidden_block}",
        "{clarify_instruction}",
    ):
        assert placeholder not in rendered, placeholder


def test_fewshot_is_in_rubric_text():
    """judge 实际拿到的是 rubric_text()，不是 markdown 文档本身。"""
    text = rubric_text()
    assert FEWSHOT_TEXT in text
    assert "四条原子判据" in text
    # 代表性正例 / 反例都要在，避免只补了一半
    for sample in (
        "9.09 个百分点", "9.08 个百分点", "百分点 ≠ 百分比",
        "千元 ≠ 元", "误拒", "4,564.52 亿元",
    ):
        assert sample in text, sample


def test_fewshot_is_actually_in_the_judge_prompt():
    assert "【Few-shot" in _render()
    assert "判 fail（要点不通过）" in _render()
