"""Batch 2：原生 function calling 编排的白盒测试。

覆盖：两轮工具调用出答案 / 未开放工具拒绝 / 工具异常降级 / 轮数上限 /
provider 不支持 tools 时自动降级 / 上下文缺失。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.ai import tool_loop
from app.services.ai.llm_client import LLMCallResult


def _llm(content: str = "", tool_calls=None, **kwargs) -> LLMCallResult:
    return LLMCallResult(
        success=True,
        raw_response=content,
        tool_calls=tool_calls,
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        latency_ms=12,
        **kwargs,
    )


def _call(name: str, args: str = "{}", call_id: str = "c1") -> dict:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": args}}


def test_tool_loop_two_rounds_then_answer(monkeypatch):
    """第一轮返回 tool_calls，第二轮返回答案：中间要回喂 tool 消息。"""
    captured = []

    def fake_call(db, messages, **kwargs):
        captured.append(list(messages))
        if len(captured) == 1:
            return _llm("", tool_calls=[_call("get_data_health")])
        return _llm('{"answer": "数据健康"}')

    monkeypatch.setattr(tool_loop, "call_llm_with_failover", fake_call)
    monkeypatch.setattr(tool_loop, "call_tool", lambda name, db, ctx: {"status": "ok", "data": {"latest_trade_date": "2026-10-24"}})

    result = tool_loop.run_tool_loop(None, [{"role": "user", "content": "数据健康吗"}], context=object())

    assert result.rounds == 2
    assert result.used_tools is True
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "get_data_health"
    assert result.tool_calls[0].status == "ok"
    assert result.result.raw_response == '{"answer": "数据健康"}'
    # 第二轮消息里必须包含 assistant(tool_calls) 与 role=tool 回喂
    second_round = captured[1]
    assert any(m.get("role") == "tool" and m.get("tool_call_id") == "c1" for m in second_round)
    assert any(m.get("role") == "assistant" and m.get("tool_calls") for m in second_round)
    # token 累计两轮
    assert result.total_tokens == 30


def test_tool_loop_rejects_unexposed_tool(monkeypatch):
    """注册表内但本批未开放的 5 个工具必须被拒绝（既定约束 5）。"""
    calls = []

    def fake_call(db, messages, **kwargs):
        calls.append(list(messages))
        if len(calls) == 1:
            return _llm("", tool_calls=[_call("get_symbol_research")])
        return _llm('{"answer": "无法调用该工具"}')

    monkeypatch.setattr(tool_loop, "call_llm_with_failover", fake_call)
    result = tool_loop.run_tool_loop(None, [{"role": "user", "content": "查个股"}], context=object())

    assert result.tool_calls[0].status == "rejected"
    assert "未开放" in result.tool_calls[0].result_preview
    # 拒因作为 tool 结果回喂，模型据此收尾
    tool_msg = next(m for m in calls[1] if m.get("role") == "tool")
    assert "rejected" in tool_msg["content"]


def test_tool_loop_tool_error_does_not_break_loop(monkeypatch):
    """工具抛异常时循环不中断，错误作为结果回喂。"""
    calls = []

    def fake_call(db, messages, **kwargs):
        calls.append(list(messages))
        if len(calls) == 1:
            return _llm("", tool_calls=[_call("get_capabilities")])
        return _llm('{"answer": "能力查询失败，请稍后再试"}')

    def boom(*_args, **_kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(tool_loop, "call_llm_with_failover", fake_call)
    monkeypatch.setattr(tool_loop, "call_tool", boom)

    result = tool_loop.run_tool_loop(None, [{"role": "user", "content": "能做什么"}], context=object())
    assert result.tool_calls[0].status == "error"
    assert result.result.success is True
    assert result.rounds == 2


def test_tool_loop_stops_at_max_rounds(monkeypatch):
    """模型一直要工具时，最后一轮必须不带 tools 收尾。"""
    captured_tools = []

    def fake_call(db, messages, **kwargs):
        captured_tools.append(kwargs.get("tools"))
        return _llm("", tool_calls=[_call("get_data_health", call_id=f"c{len(captured_tools)}")])

    monkeypatch.setattr(tool_loop, "call_llm_with_failover", fake_call)
    monkeypatch.setattr(tool_loop, "call_tool", lambda name, db, ctx: {"status": "ok", "data": {}})

    result = tool_loop.run_tool_loop(None, [{"role": "user", "content": "循环"}], context=object(), max_rounds=3)

    assert result.rounds == 4  # 3 轮工具 + 1 轮收尾
    assert captured_tools[-1] is None  # 收尾轮不带 tools
    assert result.used_tools is True


def test_tool_loop_degrades_when_provider_rejects_tools(monkeypatch):
    """provider 不支持 tools：本轮已自动降级，后续轮不再带 tools。"""
    captured_tools = []

    def fake_call(db, messages, **kwargs):
        captured_tools.append(kwargs.get("tools"))
        return _llm('{"answer": "降级回答"}', tools_unsupported=True)

    monkeypatch.setattr(tool_loop, "call_llm_with_failover", fake_call)
    result = tool_loop.run_tool_loop(None, [{"role": "user", "content": "普通问题"}], context=object())

    assert result.tools_unsupported is True
    assert result.result.raw_response == '{"answer": "降级回答"}'
    assert captured_tools[0] is not None  # 首轮带 tools
    assert result.rounds == 1


def test_execute_tool_call_without_context():
    """上下文缺失时工具返回 no_data，不抛异常。"""
    record = tool_loop.execute_tool_call(None, None, _call("get_data_health"), 1)
    assert record.status == "error"
    assert "上下文不可用" in record.result_preview


def test_execute_tool_call_unknown_name():
    record = tool_loop.execute_tool_call(None, object(), _call("delete_everything"), 1)
    assert record.status == "rejected"
    assert record.name == "delete_everything"
