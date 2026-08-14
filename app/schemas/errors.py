"""统一用户错误协议（WP-S.6）。

目标：所有 API 异常返回结构化的可理解错误，普通用户看到中文文案 + 影响 + 下一步，
技术详情折叠在 `technical_details`（前端默认隐藏）。

设计原则：
- 同一 `error_code` 在所有页面使用一致中文文案（来自 `ERROR_CODE_LIBRARY`）
- 普通用户默认只看 `user_message`/`impact`/`next_actions`
- 技术堆栈、HTTP 响应、SQL 错误折叠在 `technical_details`
- 禁止裸露 `Request failed`、`HTTP 500`、`NoneType`、数据库锁或第三方原始 HTML
- 后台日志不得包含 API Key、Webhook、SMTP 密码或完整用户数据（脱敏由 `error_sanitizer` 负责）

模块结构：
- `NextAction`：下一步建议操作
- `TechnicalDetails`：技术详情（前端折叠）
- `UserError`：统一错误响应主体
- `ERROR_CODE_LIBRARY`：预置错误码字典
- `build_user_error()`：根据 error_code 构造 UserError 的工厂函数
- `UnifiedErrorException`：可被 raise 的异常，由全局异常处理器捕获
"""
from __future__ import annotations

import logging
from typing import Any, Literal
import typing
from uuid import uuid4

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ── 子模型 ────────────────────────────────────────────


class NextAction(BaseModel):
    """下一步建议操作。

    前端渲染为按钮，用户点击后执行 `action_type` 对应动作。
    """

    label: str
    action_type: Literal["retry", "redirect", "configure", "dismiss", "view_details", "sync"]
    target: str | None = None
    reason: str | None = None


class TechnicalDetails(BaseModel):
    """技术详情（默认前端折叠，仅高级用户/开发者展开）。

    所有字符串字段必须先经过 `app.schemas.error_sanitizer.sanitize_message` 脱敏。
    """

    exception_type: str | None = None
    status_code: int | None = None
    error_message: str | None = None
    stack_summary: str | None = None
    db_error_code: str | None = None
    upstream_response: str | None = None
    request_id: str | None = None


class UserError(BaseModel):
    """统一用户错误协议（WP-S.6）。

    所有 API 异常返回此结构，普通用户默认只看 `user_message`/`impact`/`next_actions`，
    `technical_details` 折叠在前端"技术详情"中（默认隐藏）。
    """

    error_code: str
    user_message: str
    impact: str
    retryable: bool
    completed: float = 0.0
    next_actions: list[NextAction] = Field(default_factory=list)
    technical_details: TechnicalDetails | None = None
    correlation_id: str
    # P0-AutoTrade：当 HTTPException.detail 为 dict 时，透传的扩展字段（如 blockers、readiness）。
    extras: dict[str, typing.Any] | None = None


# ── 错误码字典 ────────────────────────────────────────

ERROR_CODE_LIBRARY: dict[str, dict[str, Any]] = {
    "DATA_SYNC_TIMEOUT": {
        "user_message": "数据同步超时，部分标的未完成",
        "impact": "已同步数据可正常使用，但本轮新增数据可能不完整",
        "retryable": True,
        "next_actions": [
            {"label": "重试同步", "action_type": "retry", "reason": "继续处理未完成的标的"}
        ],
    },
    "CIRCUIT_BREAKER_OPEN": {
        "user_message": "外部接口暂时不可用",
        "impact": "正在使用本地缓存数据，可能不是最新",
        "retryable": True,
        "next_actions": [{"label": "稍后重试", "action_type": "retry"}],
    },
    "DATA_VALIDATION_FAILED": {
        "user_message": "数据格式不符合预期",
        "impact": "本条数据已跳过，其他数据继续处理",
        "retryable": False,
    },
    "DB_CONNECTION_FAILED": {
        "user_message": "数据库暂时不可用",
        "impact": "本次操作未完成",
        "retryable": True,
        "next_actions": [{"label": "重试", "action_type": "retry"}],
    },
    "DB_LOCK_TIMEOUT": {
        "user_message": "数据库繁忙，请稍后再试",
        "impact": "本次操作未完成",
        "retryable": True,
    },
    "DB_INTEGRITY_VIOLATION": {
        "user_message": "数据冲突，已存在相同记录",
        "impact": "本次写入未完成，请检查是否重复提交",
        "retryable": False,
    },
    "UNAUTHORIZED": {
        "user_message": "未授权或会话已过期",
        "impact": "需要重新登录或配置授权",
        "retryable": False,
        "next_actions": [
            {"label": "去配置", "action_type": "redirect", "target": "/settings"}
        ],
    },
    "NOT_FOUND": {
        "user_message": "请求的资源不存在",
        "impact": "请检查输入或返回列表查看",
        "retryable": False,
        "next_actions": [
            {"label": "返回观察池", "action_type": "dismiss", "reason": "关闭错误提示，返回观察池列表"},
        ],
    },
    "DATA_NOT_READY": {
        "user_message": "数据未准备好，暂时无法加载观察池",
        "impact": "请先完成基础数据同步或运行增量同步",
        "retryable": True,
        "next_actions": [
            {"label": "前往基础数据", "action_type": "redirect", "target": "macro", "reason": "完成基础数据同步"},
            {"label": "运行增量同步", "action_type": "sync", "reason": "同步最新行情数据"},
            {"label": "数据就绪后重试", "action_type": "retry", "reason": "同步完成后重新加载观察池"},
        ],
    },
    "VALIDATION_ERROR": {
        "user_message": "输入参数有误",
        "impact": "请修改后重试",
        "retryable": False,
    },
    "UNKNOWN_ERROR": {
        "user_message": "服务暂时不可用，请稍后重试",
        "impact": "本次操作未完成",
        "retryable": True,
        "next_actions": [{"label": "重试", "action_type": "retry"}],
    },
    "BUSINESS_BLOCKED": {
        "user_message": "当前业务条件未满足，操作被阻止",
        "impact": "本次操作未执行，请按下方提示修复后重试",
        "retryable": True,
        "next_actions": [{"label": "查看 readiness 接口获取详细阻塞原因", "action_type": "dismiss"}],
    },
    "STALE_DATA": {
        "user_message": "本地数据已过期",
        "impact": "正在使用过期数据，结果可能不准确",
        "retryable": True,
    },
    "RATE_LIMITED": {
        "user_message": "请求过于频繁",
        "impact": "已触发上游限流，请稍候",
        "retryable": True,
    },
    "TASK_INTERRUPTED": {
        "user_message": "后台任务被中断",
        "impact": "已完成的部分仍保留，未完成的可恢复",
        "retryable": True,
        "next_actions": [{"label": "恢复任务", "action_type": "retry"}],
    },
    "TASK_STALLED": {
        "user_message": "后台任务在某阶段卡住",
        "impact": "可能因数据量过大或外部接口慢",
        "retryable": True,
        "next_actions": [
            {"label": "恢复", "action_type": "retry"},
            {"label": "取消任务", "action_type": "dismiss"},
        ],
    },
    "PORTFOLIO_RULE_INVALID": {
        "user_message": "组合规则配置不完整",
        "impact": "无法执行自动交易或回测",
        "retryable": False,
        "next_actions": [
            {
                "label": "去配置规则",
                "action_type": "redirect",
                "target": "/portfolio/rules",
            }
        ],
    },
    "AI_CONFIG_MISSING": {
        "user_message": "AI 助手未配置",
        "impact": "AI 功能不可用，其他功能正常",
        "retryable": False,
        "next_actions": [
            {"label": "去配置 AI", "action_type": "redirect", "target": "/settings/ai"}
        ],
    },
    "CAPABILITY_BLOCKED": {
        "user_message": "功能前置条件未满足，当前操作已被阻断",
        "impact": "扫描无法执行，请先完成数据准备",
        "retryable": True,
        "next_actions": [
            {"label": "去基础数据", "action_type": "redirect", "target": "macro"},
            {"label": "运行增量同步", "action_type": "sync", "reason": "同步最新行情数据"},
            {"label": "数据就绪后自动扫描", "action_type": "retry", "reason": "启动数据准备任务，完成后自动扫描"},
        ],
    },
}


# ── 工厂函数 ──────────────────────────────────────────


def build_user_error(
    error_code: str,
    *,
    correlation_id: str | None = None,
    completed: float = 0.0,
    technical_details: TechnicalDetails | None = None,
    override_user_message: str | None = None,
    override_impact: str | None = None,
    extra_next_actions: list[NextAction] | None = None,
    extras: dict[str, typing.Any] | None = None,
) -> UserError:
    """根据 `error_code` 从字典构造 `UserError`，允许覆盖部分字段。

    不存在的 `error_code` 默认降级到 `UNKNOWN_ERROR`（不抛 `KeyError`），
    保证全局异常处理器不会因字典缺失而二次崩溃。

    Args:
        error_code: 稳定错误码（如 "DATA_SYNC_TIMEOUT"）
        correlation_id: 调用链追踪 ID；None 时自动生成 uuid4.hex
        completed: 已完成比例（0.0~1.0），用于部分完成场景
        technical_details: 折叠的技术详情；通常包含脱敏后的异常消息
        override_user_message: 覆盖默认中文文案（仅在业务确有定制需要时使用）
        override_impact: 覆盖默认影响说明
        extra_next_actions: 追加到默认 `next_actions` 之后的额外操作
        extras: 透传扩展字典（readiness/blockers 等业务字段）。

    Returns:
        构造好的 `UserError` 实例
    """
    template = ERROR_CODE_LIBRARY.get(error_code)
    if template is None:
        # 降级到 UNKNOWN_ERROR，避免字典缺失导致二次崩溃
        logger.warning(
            "Unknown error_code '%s' fallback to UNKNOWN_ERROR", error_code,
        )
        template = ERROR_CODE_LIBRARY["UNKNOWN_ERROR"]
        # 保留原始 error_code 用于业务方排查，但文案使用 UNKNOWN_ERROR
        # 这里仍然使用 template 的文案，error_code 字段单独保留
        user_message = template["user_message"]
        impact = template["impact"]
        retryable = template["retryable"]
        next_actions_data = list(template.get("next_actions", []))
    else:
        user_message = template["user_message"]
        impact = template["impact"]
        retryable = template["retryable"]
        next_actions_data = list(template.get("next_actions", []))

    if override_user_message is not None:
        user_message = override_user_message
    if override_impact is not None:
        impact = override_impact

    # 合并默认 next_actions 与 extra_next_actions
    next_actions: list[NextAction] = [NextAction(**na) for na in next_actions_data]
    if extra_next_actions:
        next_actions.extend(extra_next_actions)

    # 限制 completed 在 [0.0, 1.0]
    try:
        completed_clamped = max(0.0, min(1.0, float(completed)))
    except (TypeError, ValueError):
        completed_clamped = 0.0

    return UserError(
        error_code=error_code,
        user_message=user_message,
        impact=impact,
        retryable=retryable,
        completed=completed_clamped,
        next_actions=next_actions,
        technical_details=technical_details,
        correlation_id=correlation_id or uuid4().hex,
        extras=extras,
    )


# ── 可被 raise 的异常 ─────────────────────────────────


class UnifiedErrorException(Exception):
    """可被 raise 的统一错误异常，由全局异常处理器捕获并转为 `UserError` 响应。

    业务方在需要返回结构化错误时使用：

        raise UnifiedErrorException(
            "DATA_SYNC_TIMEOUT",
            status_code=503,
            completed=0.5,
            technical_details=TechnicalDetails(
                exception_type="TimeoutError",
                error_message=sanitize_message(str(exc)),
            ),
        )

    全局异常处理器 `unified_error_handler` 会捕获此异常并返回
    `JSONResponse(status_code=exc.status_code, content=user_error.model_dump(mode="json"))`。
    """

    def __init__(
        self,
        error_code: str,
        *,
        status_code: int = 500,
        completed: float = 0.0,
        technical_details: TechnicalDetails | None = None,
        override_user_message: str | None = None,
        override_impact: str | None = None,
        extra_next_actions: list[NextAction] | None = None,
        correlation_id: str | None = None,
    ) -> None:
        super().__init__(error_code)
        self.error_code = error_code
        self.status_code = status_code
        self.completed = completed
        self.technical_details = technical_details
        self.override_user_message = override_user_message
        self.override_impact = override_impact
        self.extra_next_actions = extra_next_actions
        self.correlation_id = correlation_id or uuid4().hex


__all__ = [
    "UserError",
    "NextAction",
    "TechnicalDetails",
    "ERROR_CODE_LIBRARY",
    "build_user_error",
    "UnifiedErrorException",
]
