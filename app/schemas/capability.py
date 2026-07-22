"""功能就绪状态 Schema（WP-S.7 前置条件引导与页面门禁）。

定义 `GET /api/v1/system/capabilities` 聚合接口的响应结构：
- `CapabilityPrerequisite`：单项前置条件
- `CapabilityAction`：推荐操作
- `CapabilityStatus`：单项功能就绪状态
- `CapabilitiesResponse`：所有功能就绪状态聚合响应

设计原则：
- 同一 `reason_code` 在所有页面使用一致中文文案
- `prerequisites` 用于前端"去完成前置条件"入口跳转
- `recommended_actions` 用于按钮门禁的推荐操作
- `data_cutoff_at` 用于 degraded 状态下展示数据日期与限制
- `last_checked_at` / `checked_at` 用于前端判断是否需要刷新
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class CapabilityPrerequisite(BaseModel):
    """单项前置条件。"""

    key: str  # 稳定标识（如 "symbols_initialized"）
    label: str  # 用户可读文案
    satisfied: bool  # 是否已满足
    detail: str | None = None  # 当前状态详情


class CapabilityAction(BaseModel):
    """推荐操作。"""

    label: str  # 按钮文案
    action_type: Literal["redirect", "configure", "sync", "retry", "dismiss"]
    target: str | None = None  # 跳转目标路径
    reason: str | None = None


class CapabilityStatus(BaseModel):
    """单项功能就绪状态。"""

    key: str  # 功能键（如 "market_data" / "scoring"）
    label: str  # 用户可读功能名
    status: Literal["ready", "degraded", "blocked"]
    reason_code: str | None = None  # 阻塞/降级原因码（如 "symbols_empty" / "stale_bars"）
    user_message: str  # 用户可读中文消息
    prerequisites: list[CapabilityPrerequisite] = Field(default_factory=list)
    recommended_actions: list[CapabilityAction] = Field(default_factory=list)
    data_cutoff_at: datetime | None = None  # 数据截止时间
    last_checked_at: datetime  # 检查时间


class CapabilitiesResponse(BaseModel):
    """所有功能就绪状态聚合响应。"""

    overall_status: Literal["ready", "degraded", "blocked"]
    capabilities: list[CapabilityStatus]
    checked_at: datetime


__all__ = [
    "CapabilityPrerequisite",
    "CapabilityAction",
    "CapabilityStatus",
    "CapabilitiesResponse",
]
