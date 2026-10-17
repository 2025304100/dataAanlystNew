"""LLM 调用客户端（主备降级模型调用，WP-AI.2 / WP-AI.7）。

封装 OpenAI 兼容 chat/completions 调用 + 主备降级：
- 主 Profile 超时/限流/服务端错误 → record_failure + failover → 切换备用 Profile 重试
- 切换信息通过返回值暴露（failover_from/failover_to/failover_happened）
- 永不在日志/返回值中暴露明文 Secret（Authorization header 仅用于 httpx 请求）

project_memory 硬约束：
- 永不返回明文 Secret
- AI 失败不阻塞业务（异常被捕获，返回结构化错误结果）
"""
from __future__ import annotations

import logging
import json
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.models.ai_profile import AIProfile, PURPOSE_ALL
from app.services import ai_profile_service
from app.services.ai_failover import get_failover_manager
from app.services.ai.profile_execution import ProfileExecutionRejected, profile_request_slot
from app.services.ai.profile_capabilities import profile_protocol_error

logger = logging.getLogger(__name__)


@dataclass
class LLMCallResult:
    """LLM 调用结果。

    success=False 时表示主备模型均不可用，上层应返回降级响应。
    failover_happened=True 表示发生过主备切换（无论最终成功与否）。
    """

    success: bool
    raw_response: str = ""
    error_type: str | None = None  # timeout/rate_limit/connection/auth/server_error/unknown
    error_message: str | None = None
    profile_used: AIProfile | None = None
    failover_from: str | None = None
    failover_to: str | None = None
    failover_happened: bool = False
    latency_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


def _classify_error(status_code: int | None, exc: Exception | None = None) -> str:
    """根据 HTTP 状态码/异常类型分类错误。"""
    if status_code in ("quota", "concurrency", "unsupported_protocol", "storage"):
        return status_code
    if status_code == 429:
        return "rate_limit"
    if status_code == 504:
        return "timeout"
    if status_code in (401, 403):
        return "auth"
    if status_code in (500, 502, 503):
        return "server_error"
    if exc is not None and isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if exc is not None and isinstance(exc, httpx.RequestError):
        return "connection"
    return "unknown"


def _token_usage(usage) -> tuple[int, int, int]:
    if not isinstance(usage, dict):
        raise ValueError("Invalid usage object")
    pt = int(usage.get("prompt_tokens", 0) or 0)
    ct = int(usage.get("completion_tokens", 0) or 0)
    tt = int(usage.get("total_tokens", pt + ct) or 0)
    if any(value < 0 or value > 2**31 - 1 for value in (pt, ct, tt)):
        raise ValueError("Invalid token count")
    return pt, ct, tt


def _http_chat_completion(
    profile: AIProfile,
    messages: list[dict[str, Any]],
    *,
    max_tokens: int | None = None,
    temperature: float = 0.2,
):
    """调用单个 Profile 的 chat/completions（OpenAI 兼容）。

    返回元组：
    (success, raw_text, prompt_tokens, completion_tokens, total_tokens,
     status_code, error_msg, latency_ms)

    永不抛异常：所有错误被捕获并返回 success=False。
    Authorization header 仅在 httpx 请求中使用，不记录到日志/返回值。
    """
    # 复用 ai_profile_service 的鉴权/endpoint 构造（不暴露明文 secret）
    headers = ai_profile_service._auth_headers(profile, json_content=True)
    endpoint = ai_profile_service._endpoint_url(profile, "/chat/completions")
    payload = {
        "model": profile.model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens or profile.max_tokens,
    }
    started = time.time()
    try:
        # 非流式调用：统一给 120s 兜底，避免长回复时被默认 30s 截断
        base_timeout = max(float(profile.timeout_seconds), 120.0)
        timeout = httpx.Timeout(
            connect=base_timeout,
            write=base_timeout,
            read=base_timeout,
            pool=base_timeout,
        )
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(endpoint, json=payload, headers=headers)
    except httpx.TimeoutException as exc:
        latency_ms = int((time.time() - started) * 1000)
        return (False, "", 0, 0, 0, None, f"请求超时：{exc}", latency_ms)
    except httpx.RequestError as exc:
        latency_ms = int((time.time() - started) * 1000)
        # 不记录 exc 原文（可能含 URL/header），仅分类信息
        return (False, "", 0, 0, 0, None, f"连接失败：{type(exc).__name__}", latency_ms)
    except Exception as exc:  # noqa: BLE001 - best-effort，不阻塞业务
        latency_ms = int((time.time() - started) * 1000)
        return (False, "", 0, 0, 0, None, f"内部错误：{type(exc).__name__}", latency_ms)

    latency_ms = int((time.time() - started) * 1000)
    status_code = resp.status_code
    if status_code >= 400:
        # 响应体可能含敏感信息，仅截断后返回
        return (False, "", 0, 0, 0, status_code, f"HTTP {status_code}", latency_ms)

    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        return (False, "", 0, 0, 0, status_code, "响应解析失败", latency_ms)

    try:
        choices = data.get("choices") or []
        raw_text = (choices[0].get("message") or {}).get("content") if choices else None
        if not isinstance(raw_text, str) or not raw_text:
            raise ValueError("Missing text content")
        prompt_tokens, completion_tokens, total_tokens = _token_usage(data.get("usage") or {})
    except (AttributeError, TypeError, ValueError, IndexError, KeyError, OverflowError):
        return False, "", 0, 0, 0, status_code, "AI 服务响应格式异常，请检查兼容协议与模型", latency_ms
    return (True, raw_text, prompt_tokens, completion_tokens, total_tokens, status_code, None, latency_ms)


def _stream_profile_completion(
    db,
    messages: list[dict[str, Any]],
    *,
    profile_id: int,
    max_tokens: int | None = None,
    temperature: float = 0.2,
):
    """Yield provider text deltas followed by one terminal LLMCallResult."""
    profile = ai_profile_service.get_profile(db, profile_id)
    if profile is None or not profile.is_enabled:
        yield {"type": "result", "result": LLMCallResult(success=False, error_message="指定的 AI Profile 不存在或未启用")}
        return
    headers = ai_profile_service._auth_headers(profile, json_content=True)
    endpoint = ai_profile_service._endpoint_url(profile, "/chat/completions")
    payload = {
        "model": profile.model, "messages": messages, "stream": True,
        "temperature": temperature, "max_tokens": max_tokens or profile.max_tokens,
    }
    started = time.time()
    chunks: list[str] = []
    usage: dict[str, Any] = {}
    completed = False
    try:
        # 流式响应：connect/write 用配置超时，read 阶段不设上限
        base_timeout = float(profile.timeout_seconds)
        timeout = httpx.Timeout(
            connect=base_timeout,
            write=base_timeout,
            read=None,
            pool=base_timeout,
        )
        with httpx.Client(timeout=timeout) as client:
            with client.stream("POST", endpoint, json=payload, headers=headers) as resp:
                if resp.status_code >= 400:
                    result = LLMCallResult(success=False, error_type=_classify_error(resp.status_code), error_message=f"HTTP {resp.status_code}", profile_used=profile)
                    yield {"type": "result", "result": result}
                    return
                for line in resp.iter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data_text = line[5:].strip()
                    if data_text == "[DONE]":
                        completed = True
                        break
                    try:
                        data = json.loads(data_text)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    usage = data.get("usage") or usage
                    choices = data.get("choices") or []
                    if choices and choices[0].get("finish_reason") is not None:
                        completed = True
                    delta = ((choices[0].get("delta") or {}).get("content") or "") if choices else ""
                    if delta and not isinstance(delta, str):
                        raise ValueError("Unsupported stream content shape")
                    if delta:
                        chunks.append(delta)
                        yield {"type": "delta", "content": delta}
    except Exception as exc:  # noqa: BLE001
        latency = int((time.time() - started) * 1000)
        yield {"type": "result", "result": LLMCallResult(success=False, error_type=_classify_error(None, exc), error_message=type(exc).__name__, profile_used=profile, latency_ms=latency)}
        return
    latency = int((time.time() - started) * 1000)
    if not completed:
        yield {"type": "result", "result": LLMCallResult(success=False, error_type="stream_interrupted", error_message="AI 流式连接在完成标记前结束，回复可能不完整，请重试", profile_used=profile, latency_ms=latency)}
        return
    if not chunks:
        yield {"type": "result", "result": LLMCallResult(success=False, error_type="protocol", error_message="AI 服务没有返回可解析的流式内容", profile_used=profile, latency_ms=latency)}
        return
    try:
        pt, ct, tt = _token_usage(usage)
    except (TypeError, ValueError, OverflowError):
        yield {"type": "result", "result": LLMCallResult(success=False, error_type="protocol", error_message="AI 服务返回的用量信息格式异常，请检查兼容协议", profile_used=profile, latency_ms=latency)}
        return
    yield {"type": "result", "result": LLMCallResult(success=True, raw_response="".join(chunks), profile_used=profile, latency_ms=latency, prompt_tokens=pt, completion_tokens=ct, total_tokens=tt)}


def _unavailable_result(profile, purpose):
    protocol_error = profile_protocol_error(profile.provider, profile.base_url) if profile else None
    return LLMCallResult(success=False, error_type="unsupported_protocol" if protocol_error else "quota", error_message=protocol_error or "AI Profile 不可用、用途不匹配或当日额度已用完，请检查配置或选择可用备用模型", profile_used=profile)


def stream_llm_completion(db, messages, *, profile_id, max_tokens=None, temperature=0.2, purpose=PURPOSE_ALL):
    """Apply the same quota and health policy to streamed assistant requests."""
    manager = get_failover_manager()
    profile = ai_profile_service.get_profile(db, profile_id)
    primary_name = profile.name if profile else ""
    for attempt in range(2):
        result = _unavailable_result(profile, purpose)
        emitted = False
        if profile is not None and manager.profile_available(profile, purpose):
            try:
                with profile_request_slot(db, profile):
                    for event in _stream_profile_completion(db, messages, profile_id=profile.id, max_tokens=max_tokens, temperature=temperature):
                        if event["type"] == "delta":
                            emitted = True
                            yield event
                        else:
                            result = event["result"]
            except ProfileExecutionRejected as exc:
                result = LLMCallResult(success=False, error_type=exc.code, error_message=str(exc), profile_used=profile)
            if result.success:
                manager.record_success(db, profile.id, latency_ms=result.latency_ms, tokens=result.total_tokens, count_request=False)
            elif result.error_type not in ("concurrency", "quota", "unsupported_protocol", "storage"):
                manager.record_failure(db, profile.id, error_type=result.error_type or "unknown", error_msg=result.error_message or "")
            db.commit()
        if attempt:
            result.failover_happened = True
            result.failover_from = primary_name
            result.failover_to = profile.name if profile else None
        if result.success or emitted or attempt or profile is None:
            yield {"type": "result", "result": result}
            return
        backup = manager.failover(db, profile.id, purpose=purpose)
        if backup is None:
            yield {"type": "result", "result": result}
            return
        profile = backup


def _admitted_chat_completion(db, profile, messages, **kwargs):
    try:
        with profile_request_slot(db, profile):
            return _http_chat_completion(profile, messages, **kwargs)
    except ProfileExecutionRejected as exc:
        return False, "", 0, 0, 0, exc.code, str(exc), 0


def call_llm_with_failover(
    db,
    messages: list[dict[str, Any]],
    *,
    profile_id: int | None = None,
    purpose: str = PURPOSE_ALL,
    max_tokens: int | None = None,
    temperature: float = 0.2,
) -> LLMCallResult:
    """Run at most one primary and one available backup under shared admission."""
    manager = get_failover_manager()
    profile = ai_profile_service.get_profile(db, profile_id) if profile_id is not None else manager.select_profile(db, purpose=purpose)
    primary_name = profile.name if profile else ""
    for attempt in range(2):
        result = _unavailable_result(profile, purpose)
        if profile is not None and manager.profile_available(profile, purpose):
            ok, raw, pt, ct, tt, status, error, latency = _admitted_chat_completion(
                db, profile, messages, max_tokens=max_tokens, temperature=temperature,
            )
            error_type = None if ok else _classify_error(status)
            result = LLMCallResult(
                success=ok, raw_response=raw, profile_used=profile,
                error_type=error_type, error_message=error,
                latency_ms=latency, prompt_tokens=pt, completion_tokens=ct, total_tokens=tt,
            )
            if ok:
                manager.record_success(db, profile.id, latency_ms=latency, tokens=tt, count_request=False)
            elif error_type not in ("quota", "concurrency", "unsupported_protocol", "storage"):
                manager.record_failure(db, profile.id, error_type=error_type or "unknown", error_msg=error or "")
            db.commit()
        if attempt:
            result.failover_happened = True
            result.failover_from = primary_name
            result.failover_to = profile.name if profile else None
        if result.success or attempt or profile is None:
            return result
        backup = manager.failover(db, profile.id, purpose=purpose)
        if backup is None:
            return result
        profile = backup
    return result


__all__ = [
    "LLMCallResult",
    "call_llm_with_failover",
    "stream_llm_completion",
]
