"""缓存记账测试：client 字段提取、runner 累加、有效输入折算、成本归因回退。

全部离线：不调模型，不依赖网关。核心口径——
缓存字段缺失时按全价记账（宁可高估成本，不虚报折扣）。
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from evaluation.agent_metrics import CACHE_HIT_PRICE_RATIO, effective_input_tokens
from evaluation.agent_report import aggregate as aggregate_report
from evaluation.audit_run_cost import _summarize, _aggregate as aggregate_run_cost
from app.agent.runner import _accumulate_tokens
from app.services.openai_client import OpenAICompatibleClient, _extract_cache_tokens


# ---------- client: _extract_cache_tokens ----------

def test_extract_cache_tokens_from_deepseek_style_usage():
    usage = SimpleNamespace(
        prompt_tokens=1000,
        prompt_cache_hit_tokens=800,
        prompt_cache_miss_tokens=200,
    )
    assert _extract_cache_tokens(usage) == (800, 200)


def test_extract_cache_tokens_missing_fields_returns_zero():
    usage = SimpleNamespace(prompt_tokens=1000)
    assert _extract_cache_tokens(usage) == (0, 0)


def test_extract_cache_tokens_none_and_junk_are_full_price():
    assert _extract_cache_tokens(None) == (0, 0)
    junk = SimpleNamespace(prompt_cache_hit_tokens=None, prompt_cache_miss_tokens="x")
    assert _extract_cache_tokens(junk) == (0, 0)


@pytest.mark.asyncio
async def test_chat_with_tools_impl_propagates_cache_fields():
    """带 tools 路径必须同样透传缓存字段。

    2026-11-04 判定实验：网关对带 tools 请求照常返回 hit/miss 且写入立即可见；
    此前 `_chat_with_tools_impl` 漏了这两项，导致 26 次 tool_round 全被记成 0 命中。
    """
    usage = SimpleNamespace(
        prompt_tokens=863, completion_tokens=16, total_tokens=879,
        prompt_cache_hit_tokens=640, prompt_cache_miss_tokens=223,
    )
    message = SimpleNamespace(content="好", tool_calls=None)
    choice = SimpleNamespace(message=message, finish_reason="stop")
    response = SimpleNamespace(choices=[choice], usage=usage)

    fake_sdk = MagicMock()
    fake_sdk.chat.completions.create = AsyncMock(return_value=response)

    obj = object.__new__(OpenAICompatibleClient)
    obj._config = {}
    obj._model_config = {
        "flash": {"model": "m", "max_tokens": 16, "temperature": 0.0},
        "pro": {"model": "m", "max_tokens": 16, "temperature": 0.0},
    }
    obj._provider_name = "test"
    obj.clients = {"primary": fake_sdk}
    obj.provider_order = ["primary"]

    content, tool_calls, reasoning, info = await obj._chat_with_tools_impl(
        messages=[{"role": "user", "content": "x"}],
        tools=[{"type": "function", "function": {"name": "f", "parameters": {}}}],
        tool_choice=None,
        reasoning_mode="flash",
        temperature=0.0,
        max_tokens=16,
    )
    assert content == "好"
    assert info["cache_hit_tokens"] == 640
    assert info["cache_miss_tokens"] == 223


# ---------- runner: _accumulate_tokens ----------

def test_accumulate_tokens_sums_cache_fields():
    usage = {"steps": 0, "tokens_in": 0, "tokens_out": 0, "latency_ms": 0}
    _accumulate_tokens(usage, {"prompt_tokens": 1000, "completion_tokens": 50,
                               "cache_hit_tokens": 800, "cache_miss_tokens": 200})
    _accumulate_tokens(usage, {"prompt_tokens": 600, "completion_tokens": 30,
                               "cache_hit_tokens": 0, "cache_miss_tokens": 600})
    assert usage["tokens_in"] == 1600
    assert usage["cache_hit_tokens"] == 800
    assert usage["cache_miss_tokens"] == 800


def test_accumulate_tokens_without_cache_fields_stays_zero():
    usage = {"steps": 0, "tokens_in": 0, "tokens_out": 0, "latency_ms": 0}
    _accumulate_tokens(usage, {"prompt_tokens": 500, "completion_tokens": 20})
    _accumulate_tokens(usage, None)
    assert usage["tokens_in"] == 500
    assert usage["cache_hit_tokens"] == 0
    assert usage["cache_miss_tokens"] == 0


# ---------- effective_input_tokens ----------

def test_effective_input_full_price_when_no_cache_data():
    assert effective_input_tokens(1000, 0) == 1000


def test_effective_input_discounts_hit():
    # miss 全价 + hit 按 1/10
    assert effective_input_tokens(1000, 800) == int(200 + 800 * CACHE_HIT_PRICE_RATIO)


def test_effective_input_derives_miss_from_tokens_in():
    # 不再接受显式 miss：全价部分一律由 tokens_in − hit 推导
    assert effective_input_tokens(1000, 800) == int(200 + 800 * CACHE_HIT_PRICE_RATIO)


def test_effective_input_clamps_hit_to_tokens_in():
    # hit 异常大于 tokens_in 时不产生负成本
    assert effective_input_tokens(1000, 2000) == int(2000 * CACHE_HIT_PRICE_RATIO)


def test_effective_input_estimates_not_real_cost():
    # 返回值是估算：不能当成实测成本，也不能等于全价 tokens_in
    assert effective_input_tokens(1000, 500) < 1000
    assert effective_input_tokens(1000, 500) != 1000


# ---------- audit_run_cost: 逐题行与回退聚合 ----------

def test_summarize_uses_usage_cache_fields():
    result = {
        "task_id": "T1",
        "usage": {"tokens_in": 1000, "tokens_out": 100,
                  "cache_hit_tokens": 800, "cache_miss_tokens": 200},
    }
    row = _summarize(result)
    assert row["cache_hit_tokens"] == 800
    assert row["cache_miss_tokens"] == 200
    assert row["effective_input_tokens"] == int(200 + 800 * CACHE_HIT_PRICE_RATIO)


def test_summarize_falls_back_to_call_records():
    result = {
        "task_id": "T2",
        "usage": {"tokens_in": 1000, "tokens_out": 100},
        "llm_calls": [
            {"tokens_in": 600, "cache_hit_tokens": 500, "cache_miss_tokens": 100},
            {"tokens_in": 400, "cache_hit_tokens": 0, "cache_miss_tokens": 400},
        ],
    }
    row = _summarize(result)
    assert row["cache_hit_tokens"] == 500
    assert row["cache_miss_tokens"] == 500
    assert row["effective_input_tokens"] == int(500 + 500 * CACHE_HIT_PRICE_RATIO)


def test_summarize_legacy_runs_report_full_price():
    result = {
        "task_id": "T3",
        "usage": {"tokens_in": 1000, "tokens_out": 100},
        "llm_calls": [{"tokens_in": 600}, {"tokens_in": 400}],
    }
    row = _summarize(result)
    assert row["cache_hit_tokens"] == 0
    assert row["cache_miss_tokens"] is None
    # 全价口径 = tokens_in（输出侧不参与输入折算）
    assert row["effective_input_tokens"] == 1000


# ---------- 报告自声明：估算值不能被当成实测成本 ----------

def test_summarize_marks_effective_input_as_estimate():
    row = _summarize({
        "task_id": "T4",
        "usage": {"tokens_in": 1000, "tokens_out": 100,
                  "cache_hit_tokens": 800, "cache_miss_tokens": 200},
    })
    assert row["effective_input_is_estimate"] is True
    assert row["cache_hit_price_ratio"] == CACHE_HIT_PRICE_RATIO


def test_cost_aggregate_marks_effective_input_as_estimate():
    aggregate = aggregate_run_cost([_summarize({
        "task_id": "T5",
        "usage": {"tokens_in": 1000, "tokens_out": 100,
                  "cache_hit_tokens": 500, "cache_miss_tokens": 500},
    })])
    assert aggregate["effective_input_is_estimate"] is True
    assert aggregate["cache_hit_price_ratio"] == CACHE_HIT_PRICE_RATIO


def test_agent_report_cross_marks_effective_input_as_estimate():
    verdict = {
        "task_id": "T6", "run_index": 1, "dimension": "检索与引用",
        "outcome": "pass", "reasons": [], "causes": [], "checks": {},
        "judgment": None,
        "usage": {"tokens_in": 1000, "tokens_out": 100, "steps": 2,
                  "latency_ms": 1000, "cache_hit_tokens": 500,
                  "cache_miss_tokens": 500},
    }
    cross = aggregate_report([verdict])["cross"]
    assert cross["effective_input_is_estimate"] is True
    assert cross["cache_hit_price_ratio"] == CACHE_HIT_PRICE_RATIO
    # 折扣比是假设：有效输入必须低于全价 tokens_in，且不等于网关 miss
    assert cross["effective_input_tokens"] == int(500 + 500 * CACHE_HIT_PRICE_RATIO)
    assert cross["effective_input_tokens"] != cross["cache_miss_tokens"]
