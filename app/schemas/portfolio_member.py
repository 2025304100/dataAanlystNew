"""组合成员 Schema（WP4.4）。"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator
from app.core.storage_limits import validate_mysql_text

# 与 app/models/portfolio_member.py 的 STATUS_*/EXECUTION_*/SOURCE_* 常量保持一致。
# API 层收紧为 Literal，非法值直接 422，避免脏状态写进 SCD2 成员表。
MemberStatus = Literal["active", "paused", "archived"]
MemberExecutionMode = Literal["manual", "confirm", "auto"]
MemberSourceType = Literal["legacy_position", "candidate", "observation", "manual"]


class PortfolioMemberRead(BaseModel):
    """组合成员响应。"""

    id: int
    portfolio_id: int
    symbol_id: int
    # FIX: 附带标的代码/名称，避免前端只看到 #symbol_id（例如未建仓成员在持仓表中没有对应行）
    symbol: str | None = None
    name: str | None = None
    status: str = "active"
    execution_mode: str = "manual"
    source_type: str = "manual"
    source_id: int | None = None
    entry_rule_version_id: int | None = None
    exit_rule_version_id: int | None = None
    effective_from: str | None = None
    effective_to: str | None = None
    manual_lock: bool = False
    priority: int = 0
    note: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    # WP4-FIX：最近信号摘要（联表 SimOrder 取最近一条订单填充）
    latest_signal: str | None = None
    latest_signal_at: str | None = None
    latest_signal_action: str | None = None


class _MemberWrite(BaseModel):
    @field_validator("source_id", "entry_rule_version_id", "exit_rule_version_id", mode="before", check_fields=False)
    @classmethod
    def _reject_boolean_identity(cls, value):
        if isinstance(value, bool):
            raise ValueError("成员关联 ID 不能使用布尔值")
        return value

    @field_validator("note", check_fields=False)
    @classmethod
    def _note_storage_limit(cls, value):
        return validate_mysql_text(value, field="note")


class PortfolioMemberCreate(_MemberWrite):
    """组合成员创建请求。"""

    symbol_id: int = Field(..., strict=True, gt=0)
    status: MemberStatus = Field("active")
    # execution_mode 留在路由层校验：历史契约对 " confirm " 等写法返回 503
    # CAPABILITY_BLOCKED（见 test_unavailable_confirmation_mode_*），Literal 会
    # 把它提前变成 422，改变既定行为。
    execution_mode: str = Field("manual")
    source_type: MemberSourceType = Field("manual")
    source_id: int | None = None
    entry_rule_version_id: int | None = None
    exit_rule_version_id: int | None = None
    effective_from: str | None = None
    manual_lock: bool = False
    priority: int = Field(0, strict=True, ge=0, le=100)
    note: str | None = None


class PortfolioMemberUpdate(_MemberWrite):
    """组合成员更新请求。"""

    status: MemberStatus | None = None
    execution_mode: str | None = None
    entry_rule_version_id: int | None = None
    exit_rule_version_id: int | None = None
    manual_lock: bool | None = None
    priority: int | None = Field(None, strict=True, ge=0, le=100)
    note: str | None = None


class PortfolioMemberArchiveRequest(BaseModel):
    """归档请求。"""

    force: bool = Field(False, description="True 时即使有持仓也强制归档")


class BackfillRequest(BaseModel):
    """回填请求（WP4.3 触发）。"""

    dry_run: bool = False


class BackfillResult(BaseModel):
    """回填结果。"""

    total_positions: int = 0
    created_members: int = 0
    skipped_existing: int = 0
    skipped_zero_quantity: int = 0
    errors: list[dict] = Field(default_factory=list)


class MemberHasPositionErrorSchema(BaseModel):
    """持仓冲突错误响应。"""

    error: str = "member_has_position"
    portfolio_id: int
    symbol_id: int
    quantity: float
    message: str
    suggestion: str = "选择'仅停止买入'(PATCH status=paused)或先卖出后归档"


__all__ = [
    "MemberStatus",
    "MemberExecutionMode",
    "MemberSourceType",
    "PortfolioMemberRead",
    "PortfolioMemberCreate",
    "PortfolioMemberUpdate",
    "PortfolioMemberArchiveRequest",
    "BackfillRequest",
    "BackfillResult",
    "MemberHasPositionErrorSchema",
]
