"""工具集契约与金融工具集适配测试。

守两件事：
1. **runner 不认识具体工具名**：换 toolset 就能换域，靠的是 specs/call/to_evidence 三件；
2. **`search_sections` 只定位不算证据**——它进了编号列表，模型就会引用章节摘要当依据。
"""

import json


from app.agent.runner import run
from app.finance.store import FinanceStore
from app.tools.contract import validate_result
from app.tools.finance import FINANCE_TOOL_SPECS
from app.tools.finance_set import FinanceToolset
from app.tools.mock import Toolbox

DOC_ID = "demo-2025-annual"
SECTION_ID = f"{DOC_ID}#s0001"
TABLE_ID = f"{DOC_ID}#t0001"


def write_store(root):
    document = {
        "doc_id": DOC_ID,
        "title": "XX公司2025年年度报告",
        "company": "XX股份",
        "code": "000001",
        "doc_type": "annual_report",
        "period": "2025",
        "unit": "万元",
        "sections": [
            {
                "section_id": SECTION_ID,
                "path": ["第二节", "主要会计数据"],
                "page_start": 12,
                "page_end": 13,
                "text": "2025 年营业收入 1,234 万元，上年 1,000 万元。" * 400,
                "table_ids": [TABLE_ID],
            }
        ],
        "tables": [
            {
                "table_id": TABLE_ID,
                "section_id": SECTION_ID,
                "page": 12,
                "page_end": 12,
                "caption": "主要会计数据",
                "unit": "万元",
                "columns": ["项目", "2025 年", "2024 年"],
                "rows": [["营业收入", "1,234", "1,000"], ["净利润", "120", "100"]],
            }
        ],
    }
    path = root / DOC_ID / "document.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")


def _usage():
    return {"prompt_tokens": 10, "completion_tokens": 5}


def _patch_llm(monkeypatch, *, rounds, final_answer=""):
    """rounds: 每次 chat_with_tools 的返回 (text, tool_calls)。"""
    captured = {"tools": [], "calls": 0}

    async def fake_chat_with_tools(messages, tools, tool_choice=None, reasoning_mode="flash",
                                   temperature=0.1, max_tokens=None):
        captured["tools"].append(tools)
        index = captured["calls"]
        captured["calls"] += 1
        text, calls = rounds[min(index, len(rounds) - 1)]
        return text, calls, None, _usage()

    async def fake_chat(messages, reasoning_mode="flash", temperature=0.1, max_tokens=None):
        return final_answer or "根据资料：营业收入 1,234 万元 [1]，同比 +23.4%（(1234-1000)/1000）[2]。", None, _usage()

    monkeypatch.setattr("app.services.model_provider.chat_with_tools", fake_chat_with_tools)
    monkeypatch.setattr("app.services.model_provider.chat", fake_chat)
    return captured


def test_retrieval_toolbox_evidence_shape():
    toolbox = Toolbox()
    items = toolbox.to_evidence(
        "search_kb",
        [{"chunk_id": "d2_ch1", "title": "差旅制度", "content": "住宿五百元", "similarity": 0.81}],
    )
    assert items[0]["chunk_id"] == "d2_ch1"
    assert items[0]["similarity"] == 0.81
    assert items[0]["citable"] is True
    assert toolbox.to_evidence("unknown_tool", {}) == []


def test_finance_toolset_search_is_locator_only(tmp_path):
    write_store(tmp_path)
    toolset = FinanceToolset(FinanceStore(tmp_path))
    hits = toolset.toolbox.search_sections("营业收入", doc_id=DOC_ID)
    items = toolset.to_evidence("search_sections", hits)
    assert items and all(item["citable"] is False for item in items)
    assert items[0]["kind"] == "section_locator"
    assert items[0]["chunk_id"] == SECTION_ID


def test_finance_toolset_cell_evidence_carries_page_and_unit(tmp_path):
    write_store(tmp_path)
    toolset = FinanceToolset(FinanceStore(tmp_path))
    cell = toolset.toolbox.get_cell(TABLE_ID, "营业收入", "2024 年")
    item = toolset.to_evidence("get_cell", cell)[0]
    assert item["citable"] is True
    assert item["kind"] == "cell"
    assert item["page"] == 12
    assert "1,000" in item["content"]
    assert "万元" in item["content"]
    assert item["chunk_id"] == f"{TABLE_ID}#{cell['row']}#{cell['column']}"


def test_finance_toolset_rows_evidence_keeps_row_and_column_coordinates(tmp_path):
    write_store(tmp_path)
    toolset = FinanceToolset(FinanceStore(tmp_path))
    rows = toolset.toolbox.get_rows(
        TABLE_ID, ["营业收入", "净利润"], ["2025 年", "2024 年"]
    )
    item = toolset.to_evidence("get_rows", rows)[0]
    assert item["citable"] is True
    assert item["kind"] == "rows"
    assert item["table_id"] == TABLE_ID
    assert item["row_indices"] == [0, 1]
    assert item["column_indices"] == [1, 2]
    assert item["page"] == 12
    assert "1,234" in item["content"] and "万元" in item["content"]
    assert item["chunk_id"] == f"{TABLE_ID}#rows#0,1#cols#1,2"


def test_finance_toolset_truncates_long_section(tmp_path):
    write_store(tmp_path)
    toolset = FinanceToolset(FinanceStore(tmp_path))
    section = toolset.toolbox.read_section(SECTION_ID)
    item = toolset.to_evidence("read_section", section)[0]
    assert item["truncated"] is True
    assert len(item["content"]) < 6500


def test_finance_toolset_records_calc(tmp_path):
    write_store(tmp_path)
    toolset = FinanceToolset(FinanceStore(tmp_path))

    import asyncio

    outcome = asyncio.run(
        toolset.call("calculate", {"expression": "(cur - prior) / prior * 100",
                                  "inputs": {"cur": 1234, "prior": 1000}})
    )
    assert outcome.ok
    assert toolset.calcs and toolset.calcs[0]["formula"].startswith("(cur - prior)")
    assert toolset.calcs[0]["result"].startswith("23.4")


async def test_runner_uses_finance_toolset(tmp_path, monkeypatch):
    write_store(tmp_path)
    toolset = FinanceToolset(FinanceStore(tmp_path))
    captured = _patch_llm(
        monkeypatch,
        rounds=[
            ("", []),
            ("", []),
        ],
    )
    # 第一轮依次调 search_sections → get_cell → calculate，然后给最终答案
    calls = [
        {"id": "c1", "name": "search_sections", "arguments": {"query": "营业收入", "doc_id": DOC_ID}},
        {"id": "c2", "name": "get_cell", "arguments": {"table_id": TABLE_ID, "row": "营业收入", "column": "2025 年"}},
        {"id": "c3", "name": "calculate", "arguments": {"expression": "(cur - prior) / prior * 100",
                                                        "inputs": {"cur": 1234, "prior": 1000}}},
    ]
    captured["calls"] = 0
    # 重写 fake：第一轮返回三连调用，第二轮无调用
    async def fake_chat_with_tools(messages, tools, tool_choice=None, reasoning_mode="flash",
                                   temperature=0.1, max_tokens=None):
        captured["tools"].append(tools)
        index = captured["calls"]
        captured["calls"] += 1
        return ("", calls, None, _usage()) if index == 0 else ("", [], None, _usage())

    monkeypatch.setattr("app.services.model_provider.chat_with_tools", fake_chat_with_tools)

    result = await run(
        {"id": "F01", "question": "2025 年营业收入是多少？同比多少？", "max_steps": 5},
        toolset=toolset,
    )

    assert result["toolset"] == "finance"
    assert [step["tool"] for step in result["trace"][:-1]] == [
        "search_sections", "get_cell", "calculate"
    ]
    # 工具描述来自金融工具集，不是制度库那两条
    assert [spec["function"]["name"] for spec in captured["tools"][0]] == [
        spec["function"]["name"] for spec in FINANCE_TOOL_SPECS
    ]
    # search_sections 只定位：不进编号证据
    kinds = [item["kind"] for item in result["evidence"]]
    assert "section_locator" not in kinds
    assert "cell" in kinds
    # calc 有公式、输入、结果，硬门禁要的三样都在
    assert result["calc"] and set(result["calc"][0]) == {"formula", "inputs", "result"}
    # 引用能回到单元格锚点
    assert result["citations"][0]["chunk_id"].startswith(f"{TABLE_ID}#")
    assert validate_result(result) == []


async def test_improved_retry_uses_toolset_search_tool(tmp_path, monkeypatch):
    write_store(tmp_path)
    toolset = FinanceToolset(FinanceStore(tmp_path))
    captured = {"calls": 0, "tools": []}

    async def fake_chat_with_tools(messages, tools, tool_choice=None, reasoning_mode="flash",
                                   temperature=0.1, max_tokens=None):
        captured["tools"].append(tools)
        index = captured["calls"]
        captured["calls"] += 1
        if index == 0:
            # 查一个库里不存在的词 → 空结果 → 触发自动重试
            return (
                "",
                [{"id": "c1", "name": "search_sections",
                  "arguments": {"query": "完全不存在的东西"}, "parse_error": None}],
                None,
                _usage(),
            )
        return "", [], None, _usage()

    async def fake_chat(messages, reasoning_mode="flash", temperature=0.1, max_tokens=None):
        return "根据现有资料无法确定。", None, _usage()

    monkeypatch.setattr("app.services.model_provider.chat_with_tools", fake_chat_with_tools)
    monkeypatch.setattr("app.services.model_provider.chat", fake_chat)
    monkeypatch.setattr("app.agent.runner._coverage_check", lambda *a, **k: _no_check())

    result = await run(
        {"id": "F02", "question": "2025 年毛利率是多少？", "max_steps": 5},
        variant="improved",
        toolset=toolset,
    )

    retry_steps = [step for step in result["trace"] if step.get("policy") == "auto_retry"]
    assert retry_steps, "空结果应触发自动重试"
    assert retry_steps[0]["tool"] == "search_sections"
    assert result["usage"]["auto_retries"] == 1


async def test_locator_does_not_swallow_later_read(tmp_path, monkeypatch):
    """先定位、后读原文：同一个 id 不能被去重掉。

    实测踩过：search_sections 返回 `doc#s0003`（不可引用），随后 read_section
    读到同一节，若与定位共用 seen 集合，原文会被判成重复丢弃，
    证据永远为空，模型只能拒答。
    """
    write_store(tmp_path)
    toolset = FinanceToolset(FinanceStore(tmp_path))
    rounds = [
        ("", [{"id": "c1", "name": "search_sections",
               "arguments": {"query": "营业收入"}, "parse_error": None}], None, _usage()),
        ("", [{"id": "c2", "name": "read_section",
               "arguments": {"section_id": SECTION_ID}, "parse_error": None}], None, _usage()),
        ("", [], None, _usage()),
    ]
    state = {"i": 0}

    async def fake_chat_with_tools(messages, tools, tool_choice=None, reasoning_mode="flash",
                                   temperature=0.1, max_tokens=None):
        index = min(state["i"], len(rounds) - 1)
        state["i"] += 1
        return rounds[index]

    async def fake_chat(messages, reasoning_mode="flash", temperature=0.1, max_tokens=None):
        return "营业收入见 [1]。", None, _usage()

    monkeypatch.setattr("app.services.model_provider.chat_with_tools", fake_chat_with_tools)
    monkeypatch.setattr("app.services.model_provider.chat", fake_chat)
    monkeypatch.setattr("app.agent.runner._coverage_check", lambda *a, **k: _no_check())

    result = await run(
        {"id": "F03", "question": "2025 年营业收入是多少？", "max_steps": 5},
        variant="improved",
        toolset=toolset,
    )

    kinds = [item["kind"] for item in result["evidence"]]
    assert "section" in kinds, "读到原文后必须进证据，不能被定位去重吞掉"
    assert "section_locator" not in kinds
    assert result["citations"][0]["chunk_id"] == SECTION_ID


async def test_read_section_exposes_table_ids(tmp_path):
    """正文为空的小节要把 table_ids 交出去，否则模型不知道去哪拿数。"""
    write_store(tmp_path)
    toolset = FinanceToolset(FinanceStore(tmp_path))
    section = toolset.toolbox.read_section(SECTION_ID)
    item = toolset.to_evidence("read_section", section)[0]
    assert item["table_ids"] == [TABLE_ID]
    assert TABLE_ID in item["content"]


async def _no_check():
    return None, {}
