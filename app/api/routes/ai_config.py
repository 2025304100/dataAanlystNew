"""AI 接口配置与对话代理 API

提供 AI 服务地址/API Key 的读写，以及将公式编辑器的对话请求转发到配置的 AI 服务。
配置存储在 config/ai_config.json，与 db_config.json 同模式。
"""
from __future__ import annotations

import json
import logging
import os
import stat
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.core.config import settings

logger = logging.getLogger(__name__)

router = APIRouter()

# ── 配置存储 ───────────────────────────────────────────────

AI_CONFIG_PATH = Path(
    os.getenv(
        "AI_CONFIG_PATH",
        str(settings.base_dir / "config" / "ai_config.json"),
    )
).expanduser().resolve()
_CONFIG_LOCK = threading.RLock()

_DEFAULT_AI_CONFIG: dict = {
    "version": 2,
    "provider": "openai_compatible",
    "service_url": "",
    "api_key": "",
    "model": "",
    "enabled": False,
    "auth_type": "bearer",
    "auth_header": "Authorization",
    "chat_path": "/chat/completions",
    "models_path": "/models",
    "timeout_seconds": 30,
    "temperature": 0.3,
    "max_tokens": 1024,
    "extra_headers": {},
    "updated_at": None,
}


def _load_ai_config() -> dict:
    with _CONFIG_LOCK:
        if not AI_CONFIG_PATH.exists():
            return json.loads(json.dumps(_DEFAULT_AI_CONFIG))
        try:
            data = json.loads(AI_CONFIG_PATH.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("AI config root must be an object")
            merged = json.loads(json.dumps(_DEFAULT_AI_CONFIG))
            merged.update(data)
            if not isinstance(merged.get("extra_headers"), dict):
                merged["extra_headers"] = {}
            return merged
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            logger.error("AI config load failed from %s: %s", AI_CONFIG_PATH, exc)
            return json.loads(json.dumps(_DEFAULT_AI_CONFIG))


def _save_ai_config(config: dict) -> None:
    with _CONFIG_LOCK:
        AI_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(config, indent=2, ensure_ascii=False)
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=AI_CONFIG_PATH.parent,
                prefix=f".{AI_CONFIG_PATH.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
                temp_path = Path(handle.name)
            os.replace(temp_path, AI_CONFIG_PATH)
            persisted = json.loads(AI_CONFIG_PATH.read_text(encoding="utf-8"))
            if persisted != config:
                raise OSError("AI config write verification failed")
            try:
                os.chmod(
                    str(AI_CONFIG_PATH),
                    stat.S_IRUSR | stat.S_IWUSR,
                )
            except OSError:
                pass
        except Exception:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink(missing_ok=True)
            logger.exception("AI config save failed: %s", AI_CONFIG_PATH)
            raise


# ── Schemas ────────────────────────────────────────────────

ProviderType = Literal["openai_compatible", "anthropic", "ollama", "custom"]
AuthType = Literal["bearer", "x-api-key", "api-key", "custom", "none"]


class AiConfigFields(BaseModel):
    provider: ProviderType = "openai_compatible"
    service_url: str = ""
    api_key: str | None = None
    model: str = ""
    enabled: bool = False
    auth_type: AuthType = "bearer"
    auth_header: str = "Authorization"
    chat_path: str = "/chat/completions"
    models_path: str = "/models"
    timeout_seconds: int = Field(default=30, ge=3, le=300)
    temperature: float = Field(default=0.3, ge=0, le=2)
    max_tokens: int = Field(default=1024, ge=1, le=32768)
    extra_headers: dict[str, str] = Field(default_factory=dict)


class AiConfigRead(AiConfigFields):
    api_key: str = ""
    api_key_set: bool = False
    persisted: bool = False
    updated_at: str | None = None


class AiConfigUpdate(AiConfigFields):
    pass


class TestConnectionRequest(AiConfigFields):
    """Current form values used for a connection test."""


class ListModelsRequest(AiConfigFields):
    """Current form values used to query provider models."""


class AiChatRequest(BaseModel):
    """前端发来的对话请求。"""
    message: str = Field(..., min_length=1, max_length=2000)
    formula: str = Field(default="", max_length=500)
    history: list[dict] = Field(default_factory=list)


class AiChatResponse(BaseModel):
    ok: bool
    reply: str = ""
    error: str = ""


# ── 系统函数文档（注入到 system prompt） ───────────────────

# 从 custom_indicators 路由复用已定义的允许列表，保持单一来源
from app.api.routes.custom_indicators import (
    _ALLOWED_FORMULA_FUNCTIONS,
    _ALLOWED_FORMULA_VARIABLES,
    _validate_formula_expr,
)

_SYSTEM_FUNCTIONS_DOC = """
【严格约束 — 必须遵守】
1. 你只能使用下方列出的函数和变量来编写公式，绝对不允许编造、猜测或使用任何未列出的函数/变量。
2. 如果用户的需求无法用下方函数实现，直接告知用户"当前函数无法实现该需求"，不要尝试用其他方式绕过。
3. 公式语法必须严格符合下方示例格式。
4. 回答必须简短，直接给公式 + 一句话说明。不要展开讲解理论。
5. 如果用户的问题与公式无关，回复"我只负责公式编写，请提问与选股/择时公式相关的问题。"

【系统函数清单】（后续可能扩展，以最新版本为准）
- 均线类：sma(period), ema(period)
- 动量类：rsi(period), macd(fast,slow,signal), macd_signal(fast,slow,signal), macd_hist(fast,slow,signal)
- 波动类：atr(period), boll_upper(period), boll_mid(period), boll_lower(period)
- KDJ类：kdj_k(period), kdj_d(period), kdj_j(period)
- 极值类：highest(period), lowest(period)
- 引用类：ref(field, period)
- 变化类：pct_change(period), volume_ratio(period)
- 交叉类：cross_over(a, b), cross_under(a, b)
- 数学类：abs(x), min(a,b), max(a,b), round(x, decimals)

【可用变量】
- K线字段：open, high, low, close, volume, amount, turnover_rate, prev_close
- 评分字段：quality_score, timing_score, trend_score, momentum_score
- 布尔常量：True, False

【公式语法示例】
- 布尔公式：close > sma(20) and sma(20) > sma(60)
- 数值公式：round(100 * (close / sma(20) - 1), 2)
- 组合条件：rsi(14) < 40 and close > sma(20) and volume_ratio(5) > 1.5
- 上穿信号：cross_over(close, sma(20))
- 引用前值：ref(close, 5) > sma(20)
"""


# ── AI 回复公式校验 ────────────────────────────────────────

def _extract_and_validate_formulas(text: str) -> list[str]:
    """从 AI 回复中提取所有公式片段并校验，返回不合法的公式列表。"""
    import re
    invalid: list[str] = []
    # 匹配 ``` 代码块中的内容
    for block_match in re.finditer(r"```(?:formula)?\s*\n?([\s\S]*?)```", text):
        candidate = block_match.group(1).strip()
        if candidate and not _is_valid_formula(candidate):
            invalid.append(candidate)
    # 匹配行内可能的公式（包含函数调用和比较运算符的表达式）
    for inline_match in re.finditer(r"([a-z_][\w]*(?:\([^)]*\))?\s*[><=!]=?\s*[a-z_][\w]*(?:\([^)]*\))?[\s\S]*?(?:\n|$))", text):
        candidate = inline_match.group(1).strip()
        # 只校验看起来像公式的内容（包含括号函数调用）
        if "(" in candidate and ")" in candidate and len(candidate) > 5 and not _is_valid_formula(candidate):
            if candidate not in invalid:
                invalid.append(candidate)
    return invalid


def _is_valid_formula(formula: str) -> bool:
    """校验单个公式是否只使用了允许的函数和变量。"""
    formula = formula.strip()
    if not formula:
        return True
    valid, _ = _validate_formula_expr(formula)
    return valid


def _masked_secret(secret: str) -> str:
    if not secret:
        return ""
    return secret[:4] + "****" + secret[-2:] if len(secret) > 6 else "****"


def _resolve_secret(value: str | None, saved: dict) -> str:
    if value is None or "****" in value:
        return str(saved.get("api_key", ""))
    return value.strip()


def _normalized_config(values: dict, *, saved: dict | None = None) -> dict:
    current = saved or _load_ai_config()
    cfg = json.loads(json.dumps(_DEFAULT_AI_CONFIG))
    cfg.update(current)
    cfg.update({key: value for key, value in values.items() if value is not None})
    cfg["service_url"] = str(cfg.get("service_url", "")).strip().rstrip("/")
    cfg["model"] = str(cfg.get("model", "")).strip()
    cfg["chat_path"] = str(cfg.get("chat_path", "")).strip()
    cfg["models_path"] = str(cfg.get("models_path", "")).strip()
    cfg["auth_header"] = str(cfg.get("auth_header", "Authorization")).strip()
    cfg["api_key"] = _resolve_secret(values.get("api_key"), current)
    headers = cfg.get("extra_headers")
    cfg["extra_headers"] = {
        str(key).strip(): str(value).strip()
        for key, value in (headers.items() if isinstance(headers, dict) else [])
        if str(key).strip()
        and "\r" not in str(key)
        and "\n" not in str(key)
        and "\r" not in str(value)
        and "\n" not in str(value)
    }
    return cfg


def _endpoint_url(cfg: dict, path_key: str) -> str:
    path = str(cfg.get(path_key, "")).strip()
    if path.startswith(("http://", "https://")):
        return path
    base_url = str(cfg.get("service_url", "")).rstrip("/")
    return f"{base_url}/{path.lstrip('/')}"


def _auth_headers(cfg: dict, *, json_content: bool = False) -> dict[str, str]:
    headers = dict(cfg.get("extra_headers") or {})
    api_key = str(cfg.get("api_key", ""))
    auth_type = cfg.get("auth_type", "bearer")
    if auth_type == "bearer" and api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    elif auth_type == "x-api-key" and api_key:
        headers["x-api-key"] = api_key
    elif auth_type == "api-key" and api_key:
        headers["api-key"] = api_key
    elif auth_type == "custom" and api_key:
        headers[str(cfg.get("auth_header") or "Authorization")] = api_key
    if cfg.get("provider") == "anthropic":
        headers.setdefault("anthropic-version", "2023-06-01")
    if json_content:
        headers.setdefault("Content-Type", "application/json")
    return headers


def _default_model(provider: str) -> str:
    return {
        "anthropic": "claude-3-5-haiku-latest",
        "ollama": "llama3.2",
    }.get(provider, "gpt-4o-mini")


def _validate_runtime_config(cfg: dict, *, require_model: bool = False) -> None:
    if not cfg.get("service_url"):
        raise HTTPException(400, "请先配置 AI 服务地址")
    if cfg.get("auth_type") != "none" and not cfg.get("api_key"):
        raise HTTPException(400, "当前鉴权方式需要 API Key")
    if require_model and not cfg.get("model"):
        raise HTTPException(400, "请先配置模型名称")


def _build_chat_payload(cfg: dict, messages: list[dict], *, test: bool = False) -> dict:
    provider = cfg.get("provider", "openai_compatible")
    model = cfg.get("model") or _default_model(provider)
    max_tokens = min(int(cfg.get("max_tokens", 1024)), 8) if test else int(cfg.get("max_tokens", 1024))
    temperature = float(cfg.get("temperature", 0.3))
    if provider == "anthropic":
        system_parts = [
            item.get("content", "") for item in messages
            if item.get("role") == "system" and item.get("content")
        ]
        payload = {
            "model": model,
            "messages": [
                item for item in messages if item.get("role") != "system"
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        return payload
    if provider == "ollama":
        return {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
            },
        }
    return {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }


def _extract_ai_reply(data: dict) -> str:
    choices = data.get("choices") or []
    if choices:
        content = choices[0].get("message", {}).get("content")
        if isinstance(content, list):
            content = "".join(
                str(item.get("text", "")) for item in content
                if isinstance(item, dict)
            )
        return str(content or choices[0].get("text", ""))
    content = data.get("content")
    if isinstance(content, list):
        return "".join(
            str(item.get("text", "")) for item in content
            if isinstance(item, dict) and item.get("type") in (None, "text")
        )
    message = data.get("message")
    if isinstance(message, dict):
        return str(message.get("content", ""))
    return str(data.get("response", ""))


def _parse_model_list(data: object) -> list[dict]:
    if isinstance(data, list):
        raw_models = data
    elif isinstance(data, dict):
        raw_models = data.get("data") or data.get("models") or []
    else:
        raw_models = []
    models: list[dict] = []
    for item in raw_models:
        if isinstance(item, str):
            model_id = item
            owned_by = ""
            created = 0
        elif isinstance(item, dict):
            model_id = item.get("id") or item.get("name") or item.get("model") or ""
            owned_by = item.get("owned_by") or item.get("details", {}).get("family", "")
            created = item.get("created") or item.get("modified_at") or 0
        else:
            continue
        if model_id:
            models.append({"id": str(model_id), "owned_by": str(owned_by), "created": created})
    return models


# ── 端点 ───────────────────────────────────────────────────

@router.get("/settings/ai-config", response_model=AiConfigRead)
def get_ai_config():
    """获取 AI 接口配置（api_key 脱敏）。"""
    cfg = _load_ai_config()
    api_key = str(cfg.get("api_key", ""))
    public = {
        key: cfg.get(key, default)
        for key, default in _DEFAULT_AI_CONFIG.items()
        if key not in ("version", "api_key", "updated_at")
    }
    return AiConfigRead(
        **public,
        api_key=_masked_secret(api_key),
        api_key_set=bool(api_key),
        persisted=AI_CONFIG_PATH.exists(),
        updated_at=cfg.get("updated_at"),
    )


@router.put("/settings/ai-config")
def update_ai_config(config: AiConfigUpdate):
    """保存 AI 接口配置。"""
    saved = _load_ai_config()
    cfg = _normalized_config(config.model_dump(exclude_unset=True), saved=saved)
    if cfg.get("enabled"):
        _validate_runtime_config(cfg)
    cfg["version"] = 2
    cfg["updated_at"] = datetime.now(timezone.utc).isoformat()
    try:
        _save_ai_config(cfg)
    except Exception as exc:
        raise HTTPException(500, f"AI 配置持久化失败: {exc}") from exc
    reloaded = _load_ai_config()
    if reloaded.get("updated_at") != cfg["updated_at"]:
        raise HTTPException(500, "AI 配置写入后校验失败")
    return {
        "status": "ok",
        "message": "AI 配置已持久化保存",
        "persisted": True,
        "updated_at": cfg["updated_at"],
        "provider": cfg["provider"],
    }


@router.post("/settings/ai-config/test")
def test_ai_connection(req: TestConnectionRequest):
    """测试 AI 接口连通性（使用前端传来的当前表单值，不依赖磁盘配置）。"""
    cfg = _normalized_config(req.model_dump(exclude_unset=True))
    _validate_runtime_config(cfg)
    try:
        headers = _auth_headers(cfg, json_content=True)
        payload = _build_chat_payload(
            cfg, [{"role": "user", "content": "Reply with OK."}], test=True
        )
        endpoint = _endpoint_url(cfg, "chat_path")
        with httpx.Client(timeout=cfg["timeout_seconds"]) as client:
            resp = client.post(endpoint, json=payload, headers=headers)
        if resp.status_code >= 400:
            return {"success": False, "message": f"HTTP {resp.status_code}: {resp.text[:200]}"}
        return {
            "success": True,
            "message": "连接成功",
            "provider": cfg["provider"],
            "endpoint": endpoint,
        }
    except httpx.TimeoutException:
        return {"success": False, "message": "请求超时，请检查服务地址"}
    except httpx.RequestError as exc:
        logger.warning("AI connection test request failed: %s", exc)
        return {"success": False, "message": f"连接失败：{str(exc)[:100]}"}
    except Exception as e:
        logger.exception("AI 连接测试失败")
        return {"success": False, "message": f"连接失败：{str(e)[:100]}"}


def _fetch_ai_models(cfg: dict) -> dict:
    _validate_runtime_config(cfg)
    if not cfg.get("models_path"):
        return {"models": [], "error": "当前提供商未配置模型列表路径"}
    try:
        endpoint = _endpoint_url(cfg, "models_path")
        with httpx.Client(timeout=cfg["timeout_seconds"]) as client:
            response = client.get(endpoint, headers=_auth_headers(cfg))
        if response.status_code >= 400:
            return {
                "models": [],
                "error": f"HTTP {response.status_code}: {response.text[:160]}",
            }
        models = _parse_model_list(response.json())
        models.sort(key=lambda item: str(item.get("created", "")), reverse=True)
        return {"models": models, "provider": cfg["provider"], "endpoint": endpoint}
    except httpx.TimeoutException:
        return {"models": [], "error": "请求超时"}
    except httpx.RequestError as exc:
        logger.warning("AI model list request failed: %s", exc)
        return {"models": [], "error": str(exc)[:160]}
    except Exception as exc:
        logger.exception("AI model list response parsing failed")
        return {"models": [], "error": str(exc)[:160]}


@router.post("/settings/ai-config/models")
def list_ai_models_post(req: ListModelsRequest):
    """Fetch models without exposing the API key in the URL or access log."""
    return _fetch_ai_models(
        _normalized_config(req.model_dump(exclude_unset=True))
    )


@router.get("/settings/ai-config/models")
def list_ai_models(
    service_url: str = "",
    api_key: str = "",
):
    """从 AI 服务获取可用模型列表（使用前端传来的参数，不依赖磁盘配置）。"""
    # 如果 api_key 是脱敏格式，从磁盘读取原值
    service_url = service_url.rstrip("/") if service_url else ""
    if api_key and "****" in api_key:
        api_key = _load_ai_config().get("api_key", "")
    if not service_url:
        raise HTTPException(400, "请先配置 AI 服务地址")
    if not api_key:
        raise HTTPException(400, "请先配置 API Key")
    try:
        headers = {"Authorization": f"Bearer {api_key}"}
        with httpx.Client(timeout=15) as client:
            resp = client.get(f"{service_url}/models", headers=headers)
        if resp.status_code >= 400:
            return {"models": [], "error": f"HTTP {resp.status_code}"}
        data = resp.json()
        # OpenAI 格式：{ "data": [{ "id": "gpt-4o", ... }, ...] }
        raw_models = data.get("data", [])
        models = []
        for m in raw_models:
            mid = m.get("id", "")
            if mid:
                models.append({
                    "id": mid,
                    "owned_by": m.get("owned_by", ""),
                    "created": m.get("created", 0),
                })
        # 按创建时间倒序，常用模型排前面
        models.sort(key=lambda x: x.get("created", 0), reverse=True)
        return {"models": models}
    except httpx.TimeoutException:
        return {"models": [], "error": "请求超时"}
    except Exception as e:
        logger.exception("获取模型列表失败")
        return {"models": [], "error": str(e)[:100]}


@router.post("/settings/ai-chat", response_model=AiChatResponse)
def ai_chat(req: AiChatRequest):
    """将公式编辑器的对话请求转发到配置的 AI 服务。"""
    cfg = _load_ai_config()
    if not cfg.get("enabled"):
        raise HTTPException(400, "AI 功能未启用，请先在设置中配置并启用 AI 接口")
    _validate_runtime_config(cfg)

    # 构建 system prompt（严格限定项目框架）
    system_prompt = _SYSTEM_FUNCTIONS_DOC

    # 构建 messages
    messages = [{"role": "system", "content": system_prompt}]

    # 添加历史对话
    for item in (req.history or []):
        role = item.get("role", "user")
        content = item.get("content", "")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})

    # 添加当前消息（附带当前公式上下文）
    user_content = req.message
    if req.formula:
        user_content += f"\n\n[当前公式] {req.formula}"
    messages.append({"role": "user", "content": user_content})

    try:
        headers = _auth_headers(cfg, json_content=True)
        payload = _build_chat_payload(cfg, messages)
        endpoint = _endpoint_url(cfg, "chat_path")
        with httpx.Client(timeout=cfg["timeout_seconds"]) as client:
            resp = client.post(endpoint, json=payload, headers=headers)

        if resp.status_code >= 400:
            logger.warning("AI chat proxy error: %s %s", resp.status_code, resp.text[:300])
            return AiChatResponse(ok=False, error=f"AI 服务返回错误 (HTTP {resp.status_code})")

        data = resp.json()
        # 兼容 OpenAI / 通用 chat completions 格式
        reply = _extract_ai_reply(data)
        if not reply:
            return AiChatResponse(ok=False, error="AI 服务未返回有效回复")

        # 校验 AI 回复中的公式是否在项目框架内
        invalid_formulas = _extract_and_validate_formulas(reply)
        if invalid_formulas:
            warning = "\n\n⚠️ 注意：AI 回复中包含以下不在系统函数范围内的公式，请谨慎使用：\n" + "\n".join(f"  - {f}" for f in invalid_formulas)
            reply = reply + warning

        return AiChatResponse(ok=True, reply=reply or "AI 未返回内容")
    except httpx.TimeoutException:
        return AiChatResponse(ok=False, error="AI 服务请求超时")
    except httpx.RequestError as exc:
        logger.warning("AI chat request failed: %s", exc)
        return AiChatResponse(ok=False, error=f"连接失败：{str(exc)[:100]}")
    except Exception as e:
        logger.exception("AI chat proxy failed")
        return AiChatResponse(ok=False, error=f"请求失败：{str(e)[:100]}")
