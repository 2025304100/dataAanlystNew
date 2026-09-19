"""标的关联状态响应 Schema（WP1.5）。

为机会中心信息架构壳层提供富读模型：一次查询返回候选/观察/组合成员/持仓/告警
五类状态，前端 selector useSymbolRelationships(symbolId) 直接消费此结构。
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class CandidateRelationship(BaseModel):
    """候选状态。"""
    has_candidate: bool = Field(False, description="是否为候选")
    candidate_id: int | None = None
    scope: str | None = None
    stage: str | None = None  # new/reviewed/watched/portfolio/excluded/expired
    action: str | None = None  # executable/caution/observe/reject
    priority_score: float | None = None
    quality_score: float | None = None
    timing_score: float | None = None
    data_credibility: str | None = None
    generated_at: str | None = None  # ISO 8601 datetime
    scan_run_id: int | None = None
    snapshot_id: int | None = None
    snapshot_generated_at: str | None = None


class ObservationRelationship(BaseModel):
    """观察项状态。"""
    has_observation: bool = Field(False, description="是否在观察池")
    watchlist_id: int | None = None
    watchlist_name: str | None = None
    watchlist_item_id: int | None = None
    origin_type: str | None = None  # manual/candidate/scan_result/alert/legacy_manual_unknown
    status: str | None = None  # watching/ready/invalid/archived
    priority: int | None = None
    tags: list[str] = Field(default_factory=list)
    target_portfolio_id: int | None = None
    added_at: str | None = None  # ISO 8601 datetime


class PortfolioMemberRelationship(BaseModel):
    """组合成员状态（第一阶段只读，预留字段）。"""
    has_portfolio_membership: bool = Field(False, description="是否为组合成员")
    portfolio_id: int | None = None
    portfolio_name: str | None = None
    member_id: int | None = None  # WP4 后填充
    member_status: str | None = None  # active/paused/archived
    execution_mode: str | None = None  # manual/confirm/auto
    source_type: str | None = None
    effective_from: str | None = None
    note: str | None = None


class PositionRelationship(BaseModel):
    """持仓状态。"""
    has_position: bool = Field(False, description="是否持仓")
    portfolio_id: int | None = None
    portfolio_name: str | None = None
    position_id: int | None = None
    quantity: float | None = None
    cost_price: float | None = None
    latest_price: float | None = None
    market_value: float | None = None
    opened_at: str | None = None  # ISO 8601 datetime


class AlertRelationship(BaseModel):
    """告警状态。"""
    has_active_alert: bool = Field(False, description="是否有激活的告警规则")
    alert_rule_ids: list[int] = Field(default_factory=list)
    active_alert_events: int = Field(0, description="未处理告警事件数")
    latest_alert_severity: str | None = None  # info/warning/error/critical
    latest_alert_at: str | None = None  # ISO 8601 datetime


class SymbolRelationships(BaseModel):
    """标的统一关联状态（WP1.5）。

    前端 selector useSymbolRelationships(symbolId) 使用此结构。
    今日决策、候选列表、组合页使用统一徽标。
    """
    symbol_id: int
    symbol: str | None = None
    candidate: CandidateRelationship = Field(default_factory=CandidateRelationship)
    observation: ObservationRelationship = Field(default_factory=ObservationRelationship)
    portfolio_member: PortfolioMemberRelationship = Field(default_factory=PortfolioMemberRelationship)
    position: PositionRelationship = Field(default_factory=PositionRelationship)
    alert: AlertRelationship = Field(default_factory=AlertRelationship)
    fetched_at: str  # ISO 8601 datetime
    degraded: bool = Field(False, description="接口部分失败时降级标记")
    degraded_reason: str | None = None


__all__ = [
    "CandidateRelationship",
    "ObservationRelationship",
    "PortfolioMemberRelationship",
    "PositionRelationship",
    "AlertRelationship",
    "SymbolRelationships",
]
