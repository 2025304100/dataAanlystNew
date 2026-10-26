"""原生 function calling 编排（Batch 2）。

约束与设计：
- 最多 `TOOL_MAX_ROUNDS` 轮：每轮 LLM 可返回 tool_calls → 执行 → 结果以
  `role="tool"` 消息回喂 → 再问一轮；直到模型给出最终 answer。
- 工具白名单 = EXPOSED_TOOL_NAMES ∩ TOOL_REGISTRY；模型只能选择「调用哪个工具」，
  不能传自由参数（参数绑定会话受控上下文，见 tool_schemas 说明）。
  未开放 / 未知工具名直接拒绝，拒因回喂让模型自知。
- 工具异常不中断：错误作为工具结果回喂，循环继续（对齐「AI 失败不阻塞业务」）。
- provider 不支持 tools：`call_llm_with_failover` 内部已自动降级重试并可回传
  tools_unsupported；此后各轮不再带 tools，行为退回单轮问答（既定约束 1）。
- 达到轮数上限：追加一次不带 tools 的调用收尾，用已收集信息作答，避免死循环。

审计：本条链只负责执行与记录，落库由调用方（ai_sessions）在 assistant 消息
持久化后统一写入，保证 audit.message_id 有真实锚点。
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.models.ai_profile import PURPOSE_ALL
from app.services.ai.llm_client import LLMCallResult, call_llm_with_failover
from app.services.ai.tool_schemas import EXPOSED_TOOL_NAMES, exposed_tools_or_none
from app.services.ai.tools import TOOL_REGISTRY, call_tool

logger = logging.getLogger(__name__)

TOOL_MAX_ROUNDS = 3
_PREVIEW_CHARS = 400


@dataclass
class ToolCallRecord:
    """一次工具调用的执行记录（用于审计与前端元数据）。"""

    name: str
    arguments: str
    status: str  # ok / no_data / error / rejected
    latency_ms: int
    result_preview: str
    round_index: int
    message_content: str = ""  # 回喂给模型的完整结果（不进审计）

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "arguments": self.arguments,
            "status": self.status,
            "latency_ms": self.latency_ms,
            "result_preview": self.result_preview,
            "round_index": self.round_index,
        }


@dataclass
class ToolLoopResult:
    """工具循环的最终结果。

    - result：最后一轮（产出答案或无工具调用）的 LLM 结果，字段与既有单轮链路兼容
    - total_*：所有轮次 token 累计（中间工具轮也会消耗）
    """

    result: LLMCallResult
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    rounds: int = 1
    used_tools: bool = False
    tools_unsupported: bool = False
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_tokens: int = 0


def _dump(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def execute_tool_call(
    db: Session,
    context,
    call: dict[str, Any],
    round_index: int,
) -> ToolCallRecord:
    """执行一次工具调用（永不抛异常）。"""
    function = call.get("function") if isinstance(call.get("function"), dict) else {}
    name = str(function.get("name") or "")
    arguments = _dump(function.get("arguments") or "{}")
    started = time.time()

    if name not in EXPOSED_TOOL_NAMES or name not in TOOL_REGISTRY:
        reason = "工具未开放或不存在；本批仅允许 get_data_health / get_capabilities。"
        return ToolCallRecord(
            name=name or "(unknown)",
            arguments=arguments,
            status="rejected",
            latency_ms=0,
            result_preview=reason,
            round_index=round_index,
            message_content=json.dumps({"status": "rejected", "message": reason}, ensure_ascii=False),
        )

    if context is None:
        reason = "受控上下文不可用，无法查询该工具。"
        return ToolCallRecord(
            name=name,
            arguments=arguments,
            status="error",
            latency_ms=0,
            result_preview=reason,
            round_index=round_index,
            message_content=json.dumps({"status": "no_data", "message": reason}, ensure_ascii=False),
        )

    try:
        payload = call_tool(name, db, context)
        latency_ms = int((time.time() - started) * 1000)
        content = _dump(payload)
        tool_status = "ok"
        if isinstance(payload, dict) and payload.get("status") == "no_data":
            tool_status = "no_data"
        return ToolCallRecord(
            name=name,
            arguments=arguments,
            status=tool_status,
            latency_ms=latency_ms,
            result_preview=content[:_PREVIEW_CHARS],
            round_index=round_index,
            message_content=content,
        )
    except Exception as exc:  # noqa: BLE001 - 工具失败不中断对话
        latency_ms = int((time.time() - started) * 1000)
        logger.warning("tool %s failed: %s", name, exc)
        reason = f"工具执行失败：{type(exc).__name__}"
        return ToolCallRecord(
            name=name,
            arguments=arguments,
            status="error",
            latency_ms=latency_ms,
            result_preview=reason,
            round_index=round_index,
            message_content=json.dumps({"status": "error", "message": reason}, ensure_ascii=False),
        )


def run_tool_loop(
    db: Session,
    messages: list[dict[str, Any]],
    *,
    profile_id: int | None = None,
    purpose: str = PURPOSE_ALL,
    max_tokens: int | None = None,
    context=None,
    tools_enabled: bool = True,
    max_rounds: int = TOOL_MAX_ROUNDS,
    llm_call=None,
) -> ToolLoopResult:
    """执行「LLM → 工具 → 回喂」循环，返回最终结果与调用记录。

    llm_call：LLM 调用入口，默认 call_llm_with_failover。显式注入便于上层
    （ai_sessions）与测试替换——否则上层对 ai_sessions.call_llm_with_failover
    的 monkeypatch 不会生效。
    """
    call_llm = llm_call or call_llm_with_failover
    working: list[dict[str, Any]] = list(messages)
    records: list[ToolCallRecord] = []
    tools = exposed_tools_or_none(tools_enabled)
    tools_active = bool(tools)
    tools_unsupported = False
    rounds = 0
    total_pt = total_ct = total_tt = 0

    while rounds < max_rounds:
        rounds += 1
        result = call_llm(
            db,
            working,
            profile_id=profile_id,
            purpose=purpose,
            max_tokens=max_tokens,
            tools=tools if tools_active else None,
        )
        total_pt += result.prompt_tokens
        total_ct += result.completion_tokens
        total_tt += result.total_tokens
        if result.tools_unsupported and tools_active:
            # 本次调用已由 llm_client 自动降级（不带 tools 重试），后续轮不再带 tools
            tools_unsupported = True
            tools_active = False
        if not result.success:
            return ToolLoopResult(
                result=result,
                tool_calls=records,
                rounds=rounds,
                used_tools=bool(records),
                tools_unsupported=tools_unsupported,
                total_prompt_tokens=total_pt,
                total_completion_tokens=total_ct,
                total_tokens=total_tt,
            )
        if not result.tool_calls:
            return ToolLoopResult(
                result=result,
                tool_calls=records,
                rounds=rounds,
                used_tools=bool(records),
                tools_unsupported=tools_unsupported,
                total_prompt_tokens=total_pt,
                total_completion_tokens=total_ct,
                total_tokens=total_tt,
            )

        # 有工具调用：按 OpenAI 协议把 assistant(tool_calls) + 每个 tool 结果回喂
        working.append({
            "role": "assistant",
            "content": result.raw_response or "",
            "tool_calls": result.tool_calls,
        })
        for call in result.tool_calls:
            record = execute_tool_call(db, context, call, rounds)
            records.append(record)
            working.append({
                "role": "tool",
                "tool_call_id": (call.get("id") or ""),
                "content": record.message_content,
            })

    # 轮数用尽：最后一次不带 tools 收尾
    final = call_llm(
        db,
        working,
        profile_id=profile_id,
        purpose=purpose,
        max_tokens=max_tokens,
        tools=None,
    )
    total_pt += final.prompt_tokens
    total_ct += final.completion_tokens
    total_tt += final.total_tokens
    return ToolLoopResult(
        result=final,
        tool_calls=records,
        rounds=rounds + 1,
        used_tools=True,
        tools_unsupported=tools_unsupported,
        total_prompt_tokens=total_pt,
        total_completion_tokens=total_ct,
        total_tokens=total_tt,
    )


__all__ = [
    "TOOL_MAX_ROUNDS",
    "ToolCallRecord",
    "ToolLoopResult",
    "execute_tool_call",
    "run_tool_loop",
]
