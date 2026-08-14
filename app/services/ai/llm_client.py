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
        with httpx.Client(timeout=profile.timeout_seconds) as client:
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

    # OpenAI 兼容格式：choices[0].message.content
    choices = data.get("choices") or []
    raw_text = ""
    if choices and isinstance(choices, list):
        msg = choices[0].get("message") or {}
        raw_text = msg.get("content") or ""
    usage = data.get("usage") or {}
    prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
    completion_tokens = int(usage.get("completion_tokens", 0) or 0)
    total_tokens = int(
        usage.get("total_tokens", prompt_tokens + completion_tokens) or 0
    )
    return (True, raw_text, prompt_tokens, completion_tokens, total_tokens, status_code, None, latency_ms)


def stream_llm_completion(
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
    try:
        with httpx.Client(timeout=profile.timeout_seconds) as client:
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
                        break
                    try:
                        data = json.loads(data_text)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    usage = data.get("usage") or usage
                    choices = data.get("choices") or []
                    delta = ((choices[0].get("delta") or {}).get("content") or "") if choices else ""
                    if delta:
                        chunks.append(delta)
                        yield {"type": "delta", "content": delta}
    except Exception as exc:  # noqa: BLE001
        latency = int((time.time() - started) * 1000)
        yield {"type": "result", "result": LLMCallResult(success=False, error_type=_classify_error(None, exc), error_message=type(exc).__name__, profile_used=profile, latency_ms=latency)}
        return
    latency = int((time.time() - started) * 1000)
    pt = int(usage.get("prompt_tokens", 0) or 0)
    ct = int(usage.get("completion_tokens", 0) or 0)
    tt = int(usage.get("total_tokens", pt + ct) or 0)
    yield {"type": "result", "result": LLMCallResult(success=True, raw_response="".join(chunks), profile_used=profile, latency_ms=latency, prompt_tokens=pt, completion_tokens=ct, total_tokens=tt)}


def call_llm_with_failover(
    db,
    messages: list[dict[str, Any]],
    *,
    profile_id: int | None = None,
    purpose: str = PURPOSE_ALL,
    max_tokens: int | None = None,
    temperature: float = 0.2,
) -> LLMCallResult:
    """调用 LLM，主备降级。

    流程：
    1. 选择 Profile：profile_id 指定则用之，否则 select_profile 选默认
    2. 调用主 Profile：成功 → record_success → 返回
    3. 失败（timeout/rate_limit/server_error/connection）→ record_failure → failover
    4. 调用备用 Profile：成功 → record_success → 返回（标注 failover）
    5. 备用也失败或无备用 → 返回失败结果

    永不抛异常。
    """
    mgr = get_failover_manager()

    # ── 1. 选择主 Profile ──
    if profile_id is not None:
        profile = ai_profile_service.get_profile(db, profile_id)
        if profile is None or not profile.is_enabled:
            return LLMCallResult(
                success=False,
                error_type="unknown",
                error_message="指定的 AI Profile 不存在或未启用",
            )
    else:
        profile = mgr.select_profile(db, purpose=purpose)
        if profile is None:
            return LLMCallResult(
                success=False,
                error_type="unknown",
                error_message="无可用 AI Profile",
            )

    primary_name = profile.name

    # ── 2. 调用主 Profile ──
    ok, raw, pt, ct, tt, sc, err, latency = _http_chat_completion(
        profile, messages, max_tokens=max_tokens, temperature=temperature
    )
    if ok:
        try:
            mgr.record_success(db, profile.id, latency_ms=latency, tokens=tt)
            db.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("record_success failed: %s", exc)
        return LLMCallResult(
            success=True,
            raw_response=raw,
            profile_used=profile,
            failover_happened=False,
            latency_ms=latency,
            prompt_tokens=pt,
            completion_tokens=ct,
            total_tokens=tt,
        )

    # ── 3. 主 Profile 失败 → 记录 + failover ──
    error_type = _classify_error(sc)
    try:
        mgr.record_failure(db, profile.id, error_type=error_type, error_msg=err or "")
        db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("record_failure failed: %s", exc)

    logger.warning(
        "LLM primary profile %s failed: type=%s msg=%s, attempting failover",
        primary_name, error_type, (err or "")[:120],
    )

    fallback = mgr.failover(db, profile.id, purpose=purpose)
    if fallback is None:
        return LLMCallResult(
            success=False,
            error_type=error_type,
            error_message=err or "主备模型均不可用",
            profile_used=profile,
            failover_from=primary_name,
            failover_happened=False,
            latency_ms=latency,
        )

    fallback_name = fallback.name

    # ── 4. 调用备用 Profile ──
    ok2, raw2, pt2, ct2, tt2, sc2, err2, latency2 = _http_chat_completion(
        fallback, messages, max_tokens=max_tokens, temperature=temperature
    )
    if ok2:
        try:
            mgr.record_success(db, fallback.id, latency_ms=latency2, tokens=tt2)
            db.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("record_success (fallback) failed: %s", exc)
        return LLMCallResult(
            success=True,
            raw_response=raw2,
            profile_used=fallback,
            failover_from=primary_name,
            failover_to=fallback_name,
            failover_happened=True,
            latency_ms=latency2,
            prompt_tokens=pt2,
            completion_tokens=ct2,
            total_tokens=tt2,
        )

    # ── 5. 备用也失败 ──
    error_type2 = _classify_error(sc2)
    try:
        mgr.record_failure(db, fallback.id, error_type=error_type2, error_msg=err2 or "")
        db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("record_failure (fallback) failed: %s", exc)

    return LLMCallResult(
        success=False,
        error_type=error_type2,
        error_message=(
            f"主备模型均不可用：主[{primary_name}] {err or '失败'}; "
            f"备[{fallback_name}] {err2 or '失败'}"
        ),
        profile_used=fallback,
        failover_from=primary_name,
        failover_to=fallback_name,
        failover_happened=True,
        latency_ms=latency + latency2,
    )


__all__ = [
    "LLMCallResult",
    "call_llm_with_failover",
    "stream_llm_completion",
]
