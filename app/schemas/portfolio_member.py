"""组合成员 Schema（WP4.4）。"""
from __future__ import annotations

from pydantic import BaseModel, Field


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


class PortfolioMemberCreate(BaseModel):
    """组合成员创建请求。"""

    symbol_id: int
    status: str = Field("active", description="active/paused/archived")
    execution_mode: str = Field("manual", description="manual/confirm/auto")
    source_type: str = Field("manual", description="legacy_position/candidate/observation/manual")
    source_id: int | None = None
    entry_rule_version_id: int | None = None
    exit_rule_version_id: int | None = None
    effective_from: str | None = None
    manual_lock: bool = False
    priority: int = Field(0, ge=0, le=100)
    note: str | None = None


class PortfolioMemberUpdate(BaseModel):
    """组合成员更新请求。"""

    status: str | None = None
    execution_mode: str | None = None
    entry_rule_version_id: int | None = None
    exit_rule_version_id: int | None = None
    manual_lock: bool | None = None
    priority: int | None = None
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
    "PortfolioMemberRead",
    "PortfolioMemberCreate",
    "PortfolioMemberUpdate",
    "PortfolioMemberArchiveRequest",
    "BackfillRequest",
    "BackfillResult",
    "MemberHasPositionErrorSchema",
]
