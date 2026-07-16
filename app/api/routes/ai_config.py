"""AI 接口配置与对话代理 API

提供 AI 服务地址/API Key 的读写，以及将公式编辑器的对话请求转发到配置的 AI 服务。
配置存储在 config/ai_config.json，与 db_config.json 同模式。
"""
from __future__ import annotations

import json
import logging
import stat
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.core.config import settings

logger = logging.getLogger(__name__)

router = APIRouter()

# ── 配置存储 ───────────────────────────────────────────────

AI_CONFIG_PATH = settings.base_dir / "config" / "ai_config.json"

_DEFAULT_AI_CONFIG: dict = {
    "service_url": "",
    "api_key": "",
    "model": "",
    "enabled": False,
}


def _load_ai_config() -> dict:
    if not AI_CONFIG_PATH.exists():
        return json.loads(json.dumps(_DEFAULT_AI_CONFIG))
    try:
        data = json.loads(AI_CONFIG_PATH.read_text(encoding="utf-8"))
        for key in _DEFAULT_AI_CONFIG:
            if key not in data:
                data[key] = _DEFAULT_AI_CONFIG[key]
        return data
    except (json.JSONDecodeError, OSError):
        return json.loads(json.dumps(_DEFAULT_AI_CONFIG))


def _save_ai_config(config: dict) -> None:
    AI_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    AI_CONFIG_PATH.write_text(
        json.dumps(config, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    try:
        os_chmod = stat.S_IRUSR | stat.S_IWUSR
        import os
        os.chmod(str(AI_CONFIG_PATH), os_chmod)
    except OSError:
        pass


# ── Schemas ────────────────────────────────────────────────

class AiConfigRead(BaseModel):
    service_url: str = ""
    api_key: str = ""
    model: str = ""
    enabled: bool = False


class AiConfigUpdate(BaseModel):
    service_url: str = ""
    api_key: str = ""
    model: str = ""
    enabled: bool = False


class TestConnectionRequest(BaseModel):
    """测试连接时前端传来的当前表单值（不依赖磁盘配置）。"""
    service_url: str = ""
    api_key: str = ""
    model: str = ""


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


# ── 端点 ───────────────────────────────────────────────────

@router.get("/settings/ai-config", response_model=AiConfigRead)
def get_ai_config():
    """获取 AI 接口配置（api_key 脱敏）。"""
    cfg = _load_ai_config()
    api_key = cfg.get("api_key", "")
    masked = ""
    if api_key:
        masked = api_key[:4] + "****" + api_key[-2:] if len(api_key) > 6 else "****"
    return AiConfigRead(
        service_url=cfg.get("service_url", ""),
        api_key=masked,
        model=cfg.get("model", ""),
        enabled=cfg.get("enabled", False),
    )


@router.put("/settings/ai-config")
def update_ai_config(config: AiConfigUpdate):
    """保存 AI 接口配置。"""
    cfg = _load_ai_config()
    # 如果 api_key 是脱敏格式，保留磁盘上的原值
    new_key = config.api_key
    if new_key and "****" in new_key:
        new_key = cfg.get("api_key", "")
    cfg["service_url"] = config.service_url.rstrip("/")
    cfg["api_key"] = new_key
    cfg["model"] = config.model
    cfg["enabled"] = config.enabled
    _save_ai_config(cfg)
    return {"status": "ok", "message": "AI 配置已保存"}


@router.post("/settings/ai-config/test")
def test_ai_connection(req: TestConnectionRequest):
    """测试 AI 接口连通性（使用前端传来的当前表单值，不依赖磁盘配置）。"""
    # 如果 api_key 是脱敏格式，从磁盘读取原值
    service_url = req.service_url.rstrip("/") if req.service_url else ""
    api_key = req.api_key
    if api_key and "****" in api_key:
        api_key = _load_ai_config().get("api_key", "")
    if not service_url:
        raise HTTPException(400, "请先配置 AI 服务地址")
    if not api_key:
        raise HTTPException(400, "请先配置 API Key")
    try:
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {
            "model": req.model or "gpt-4o-mini",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 5,
        }
        with httpx.Client(timeout=15) as client:
            resp = client.post(f"{service_url}/chat/completions", json=payload, headers=headers)
        if resp.status_code >= 400:
            return {"success": False, "message": f"HTTP {resp.status_code}: {resp.text[:200]}"}
        return {"success": True, "message": "连接成功"}
    except httpx.TimeoutException:
        return {"success": False, "message": "请求超时，请检查服务地址"}
    except Exception as e:
        logger.exception("AI 连接测试失败")
        return {"success": False, "message": f"连接失败：{str(e)[:100]}"}


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
    service_url = cfg.get("service_url", "")
    api_key = cfg.get("api_key", "")
    if not service_url or not api_key:
        raise HTTPException(400, "AI 服务地址或 API Key 未配置")

    model = cfg.get("model", "") or "gpt-4o-mini"

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
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": 1024,
            "temperature": 0.3,
        }
        with httpx.Client(timeout=30) as client:
            resp = client.post(f"{service_url}/chat/completions", json=payload, headers=headers)

        if resp.status_code >= 400:
            logger.warning("AI chat proxy error: %s %s", resp.status_code, resp.text[:300])
            return AiChatResponse(ok=False, error=f"AI 服务返回错误 (HTTP {resp.status_code})")

        data = resp.json()
        # 兼容 OpenAI / 通用 chat completions 格式
        choices = data.get("choices", [])
        if not choices:
            return AiChatResponse(ok=False, error="AI 服务未返回有效回复")
        reply = choices[0].get("message", {}).get("content", "")
        if not reply:
            reply = choices[0].get("text", "")

        # 校验 AI 回复中的公式是否在项目框架内
        invalid_formulas = _extract_and_validate_formulas(reply)
        if invalid_formulas:
            warning = "\n\n⚠️ 注意：AI 回复中包含以下不在系统函数范围内的公式，请谨慎使用：\n" + "\n".join(f"  - {f}" for f in invalid_formulas)
            reply = reply + warning

        return AiChatResponse(ok=True, reply=reply or "AI 未返回内容")
    except httpx.TimeoutException:
        return AiChatResponse(ok=False, error="AI 服务请求超时")
    except Exception as e:
        logger.exception("AI chat proxy failed")
        return AiChatResponse(ok=False, error=f"请求失败：{str(e)[:100]}")
