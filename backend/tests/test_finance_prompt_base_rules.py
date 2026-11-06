"""共享纪律与形态规则的注入回归。

BASE_RULES 是查证/摘录/综述/拒答四种回答形态共用的约束层。它必须同时出现在
两条现有调用路径上——Agent 循环（FINANCE_BASELINE_SYSTEM / FINANCE_IMPROVED_SYSTEM）
与单次结构检索（STRUCTURED_RAG_SYSTEM）。

形态规则（定位写法）只给查证/摘录，不给综述：综述用「来源名，月-日」，
两套坐标不能混在一个共享层里，否则模型会挑错分支（2026-11-06 探针实测）。
"""

import json
from pathlib import Path

from app.agent.prompts import (
    BASE_RULES,
    FINANCE_BASELINE_SYSTEM,
    FINANCE_FINAL_TEMPLATE,
    FINANCE_IMPROVED_SYSTEM,
    FINANCE_TOOL_INTRO,
    VERIFICATION_LAYOUT_RULES,
)
from app.finance.store import FinanceStore
from app.services.finance_rag import STRUCTURED_RAG_SYSTEM
from app.services.market_brief import MARKET_BRIEF_SYSTEM
from app.tools.finance_set import FinanceToolset


def test_base_rules_holds_the_four_invariants():
    """四种成分、禁编造、年份不可替换、免责不由模型写——缺一条就失去约束意义。"""
    assert "事实" in BASE_RULES and "观点" in BASE_RULES and "推断" in BASE_RULES
    assert "不编造" in BASE_RULES
    assert "不得替换问题里的年份" in BASE_RULES
    assert "免责声明由系统追加" in BASE_RULES


def test_base_rules_forbids_inference_introducing_numbers():
    assert "推断不得引入新数字" in BASE_RULES
    assert "[n]" in BASE_RULES


def test_base_rules_injected_into_both_agent_variants():
    assert BASE_RULES in FINANCE_BASELINE_SYSTEM
    assert BASE_RULES in FINANCE_IMPROVED_SYSTEM


def test_base_rules_injected_into_structured_rag():
    assert BASE_RULES in STRUCTURED_RAG_SYSTEM


def test_base_rules_stays_free_of_tool_names():
    """BASE_RULES 会被挂到没有工具的路径上；写了工具名就会误导单次检索。"""
    for tool_name in (
        "search_blocks",
        "search_sections",
        "search_tables",
        "get_rows",
        "get_table",
        "get_cell",
        "read_block",
        "read_section",
        "calculate",
    ):
        assert tool_name not in BASE_RULES


def test_structured_rag_template_still_formats():
    """拼接 BASE_RULES 不能破坏 {evidence}/{question} 占位符。"""
    rendered = STRUCTURED_RAG_SYSTEM.format(evidence="证据块", question="问题")
    assert "证据块" in rendered and "问题" in rendered
    assert BASE_RULES in rendered


# ---------------------------------------------------------------------------
# 形态规则：只给查证/摘录，理由见模块 docstring
# ---------------------------------------------------------------------------


def test_layout_rules_are_injected_only_where_they_apply():
    assert VERIFICATION_LAYOUT_RULES in FINANCE_BASELINE_SYSTEM
    assert VERIFICATION_LAYOUT_RULES in FINANCE_IMPROVED_SYSTEM
    assert VERIFICATION_LAYOUT_RULES in STRUCTURED_RAG_SYSTEM
    # 综述有自己的来源标注格式，不能被「文档 · 表名 · 页码」拉扯
    assert VERIFICATION_LAYOUT_RULES not in MARKET_BRIEF_SYSTEM
    assert VERIFICATION_LAYOUT_RULES not in BASE_RULES


def test_brief_does_not_inherit_verification_only_rules():
    """综述没有页码坐标，也允许标注过的推算值，套查证纪律会直接冲突。"""
    from app.agent.prompts import CORE_DISCIPLINE

    assert CORE_DISCIPLINE in MARKET_BRIEF_SYSTEM
    assert BASE_RULES not in MARKET_BRIEF_SYSTEM
    assert "必须带坐标" not in MARKET_BRIEF_SYSTEM
    assert "推断不得引入新数字" not in MARKET_BRIEF_SYSTEM
    # 但「不编造」「免责由系统写」这两条必须还在
    assert "不编造链接" in MARKET_BRIEF_SYSTEM
    assert "免责声明由系统追加" in MARKET_BRIEF_SYSTEM


def test_layout_names_one_locator_format_and_bans_internal_ids():
    assert "文档简称 · 表名或章节 · P.页码" in VERIFICATION_LAYOUT_RULES
    assert "#t0006" in VERIFICATION_LAYOUT_RULES
    # 旧规则要求正文写表 id / 单列引用，正是冲突来源
    assert "答案里写清表 id" not in FINANCE_IMPROVED_SYSTEM
    assert "表 id，行名，列名" not in FINANCE_FINAL_TEMPLATE


def test_layout_prevents_the_missing_cell_self_block():
    """FIN-11 探针实测：模型把「不写表 id」理解成「不能给坐标」，开头自我否定。"""
    assert "无法给出单元格编号" in VERIFICATION_LAYOUT_RULES
    assert "不等于不能给定位" in VERIFICATION_LAYOUT_RULES


def test_prose_only_facts_have_a_defined_fallback():
    """BASE 允许「章节 + 页码」，FINANCE #8 只认表+行+列——两者必须显式收口。"""
    assert "只存在于章节正文的披露项" in FINANCE_IMPROVED_SYSTEM
    assert "不要伪造单元格" in FINANCE_IMPROVED_SYSTEM


def test_calc_formula_is_not_required_in_prose():
    assert "在答案里写出公式与输入值" not in FINANCE_IMPROVED_SYSTEM
    assert "正文不复述" in FINANCE_IMPROVED_SYSTEM
    # 能力约束不能被一起删掉
    assert "一律调用 calculate" in FINANCE_IMPROVED_SYSTEM


def _prompt_for_mode(tmp_path: Path, mode: str, variant: str = "improved") -> str:
    document = {
        "doc_id": "d",
        "title": "t",
        "company": "c",
        "period": "2025",
        "doc_type": "annual_report",
        "sections": [],
        "tables": [],
    }
    (tmp_path / "d").mkdir(exist_ok=True)
    (tmp_path / "d" / "document.json").write_text(
        json.dumps(document, ensure_ascii=False), encoding="utf-8"
    )
    return FinanceToolset(
        FinanceStore(tmp_path), search_mode=mode
    ).system_prompt(variant)


def test_calc_marker_demand_is_stated_once(tmp_path: Path):
    """[计算n] 之前只写在工具集后缀里、与主规则分离，实测 11 题无一遵守。

    现在收敛到 FINANCE 规则第 2 条一处；后缀不再重复要求。
    """
    prompt = _prompt_for_mode(tmp_path, "blocks")
    assert prompt.count("[计算") == 1
    assert "[计算1]" not in prompt


def test_blocks_mode_never_instructs_a_missing_tool(tmp_path: Path):
    """blocks 模式没有 search_sections，规则里就不能出现"退回 search_sections"。"""
    blocks = _prompt_for_mode(tmp_path, "blocks")
    assert "结构块检索不到再退回 search_sections" not in blocks
    assert "search_sections / search_blocks / search_tables" not in blocks
    assert "本模式只提供 search_blocks" in blocks

    sections = _prompt_for_mode(tmp_path, "sections")
    assert "结构块检索不到再退回 search_sections" in sections
    assert "本模式只提供 search_blocks" not in sections


def test_no_rule_asks_for_formulas_in_the_answer_body():
    assert "直接给结论、公式和引用" not in FINANCE_IMPROVED_SYSTEM
    assert "公式与输入随计算记录展示" in FINANCE_IMPROVED_SYSTEM


def test_rules_forbid_narrating_compliance():
    """实测模型会在正文写「因此无需改写正文出处」——那是复述规则，不是结论。

    规则保留，但坏样例不再贴进提示词：把错误输出放进上下文等于再喂一遍坏样本。
    """
    assert "不要解释你遵守了哪条规则" in FINANCE_IMPROVED_SYSTEM
    assert "无需改写正文出处" not in FINANCE_IMPROVED_SYSTEM


def test_scope_wording_allows_parent_company(tmp_path: Path):
    """口径不能写死「合并」：用户点名母公司时，规则 4 与旧后缀曾互相打架。"""
    assert "合并或母公司口径" in FINANCE_IMPROVED_SYSTEM
    assert "默认取合并口径" in FINANCE_IMPROVED_SYSTEM
    prompt = _prompt_for_mode(tmp_path, "blocks")
    # 后缀不再重复一遍口径规则（一件事只说一次）
    assert "同一指标同时出现在合并报表与母公司报表时" not in prompt


def test_rule13_has_no_dangling_count(tmp_path: Path):
    """blocks 模式替换掉 search_sections 后，「三者」就指不清了。"""
    assert "三者都只定位" not in _prompt_for_mode(tmp_path, "blocks")
    assert "三者都只定位" not in _prompt_for_mode(tmp_path, "sections")
    assert "这些检索工具都只定位" in _prompt_for_mode(tmp_path, "blocks")


def test_repair_hints_do_not_require_absent_tools():
    from app.agent.prompts import COVERAGE_REPAIR_HINT

    assert "search_tables / search_sections" not in COVERAGE_REPAIR_HINT
    assert "sections 模式下也可用" in COVERAGE_REPAIR_HINT


def test_structured_rag_refusal_phrase_is_recognized():
    """规则 4 让模型说「无法确定：需要多步查证」，这句话必须命中 ABSTAIN_KEYWORDS。

    2026-11-06 之前它只说「不要心算」，把证据里已经给出的同比也一起拒掉；
    现在要区分「证据已有的比率可直接引用」与「需要自己推导的才拒答」。
    """
    from app.tools.contract import ABSTAIN_KEYWORDS

    rule_line = next(
        line for line in STRUCTURED_RAG_SYSTEM.splitlines() if "多步查证" in line
    )
    assert any(keyword in rule_line for keyword in ABSTAIN_KEYWORDS)
    assert "可以直接引用" in STRUCTURED_RAG_SYSTEM


def test_brief_allows_labeled_estimates():
    """综述硬约束 3 与「分项」段的推算规则不能互相否决。"""
    assert "除明确标注" in MARKET_BRIEF_SYSTEM
    assert "（推算，非披露）" in MARKET_BRIEF_SYSTEM


def test_tool_intro_does_not_restate_function_schemas():
    """工具说明曾是提示词里最大的冗余：和 function schema 讲同一件事，还各用一套措辞。"""
    assert len(FINANCE_TOOL_INTRO) < 200
    assert "见函数定义" in FINANCE_TOOL_INTRO
    for signature in (
        "list_documents()",
        "search_blocks(query",
        "search_tables(query",
        "get_rows(table_id",
        "get_cell(table_id",
        "calculate(expression",
    ):
        assert signature not in FINANCE_TOOL_INTRO


def test_system_prompt_stays_within_budget(tmp_path: Path):
    """增长警报，不是规范：工具说明并入 schema 后应稳定在 2.7k 字符以内。"""
    assert len(_prompt_for_mode(tmp_path, "blocks")) < 2800
    assert len(_prompt_for_mode(tmp_path, "sections")) < 2800
