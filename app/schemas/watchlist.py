from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class WatchlistCreate(BaseModel):
    name: str
    list_type: str
    description: str | None = None


class WatchlistRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    list_type: str
    description: str | None = None
    created_at: datetime


class WatchlistItemCreate(BaseModel):
    symbol_id: int
    note: str | None = None


class WatchlistItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    watchlist_id: int
    symbol_id: int
    note: str | None = None
    added_at: datetime


# ============================================================================
# WP2.3 观察池 Schema
# ============================================================================


class ObservationRead(BaseModel):
    """观察项富读响应（对齐 app.services.observations.ObservationRich.to_dict）。"""

    watchlist_item_id: int
    watchlist_id: int
    watchlist_name: str | None = None
    symbol_id: int
    symbol: str | None = None
    added_at: str | None = None
    updated_at: str | None = None
    archived_at: str | None = None
    origin_type: str = "manual"
    origin_id: int | None = None
    reason: dict | None = None
    score_snapshot: dict | None = None
    status: str = "watching"
    priority: int = 0
    tags: list[str] = Field(default_factory=list)
    note: str | None = None
    target_portfolio_id: int | None = None
    target_portfolio_name: str | None = None
    latest_price: float | None = None
    latest_price_date: str | None = None
    price_change_pct: float | None = None
    latest_total_score: float | None = None
    latest_quality_score: float | None = None
    latest_timing_score: float | None = None
    latest_score_date: str | None = None
    data_credibility: str | None = None
    bar_count: int | None = None
    has_position: bool = False
    position_portfolio_name: str | None = None
    degraded: bool = False
    degraded_reason: str | None = None


class ObservationCreate(BaseModel):
    """观察项创建请求。"""

    watchlist_id: int = Field(..., description="目标名单 ID")
    symbol_id: int = Field(..., description="标的 ID")
    origin_type: str = Field(
        "manual", description="来源类型：manual/candidate/scan_result/alert"
    )
    origin_id: int | None = Field(None, description="来源对象 ID")
    reason: dict | None = Field(None, description="加入原因 JSON")
    score_snapshot: dict | None = Field(None, description="加入时评分快照 JSON")
    priority: int = Field(0, ge=0, le=100)
    tags: list[str] = Field(default_factory=list)
    target_portfolio_id: int | None = None
    note: str | None = None


class ObservationCandidateCreate(BaseModel):
    """候选加入观察池请求。"""

    candidate_id: int = Field(..., description="候选 ID（来源）")
    watchlist_id: int = Field(..., description="目标名单 ID")
    note: str | None = None
    priority: int = Field(0, ge=0, le=100)
    tags: list[str] = Field(default_factory=list)
    target_portfolio_id: int | None = None


class ObservationUpdate(BaseModel):
    """观察项更新请求。"""

    priority: int | None = Field(None, ge=0, le=100)
    tags: list[str] | None = None
    reason: dict | None = None
    target_portfolio_id: int | None = None
    note: str | None = None
    status: str | None = Field(
        None, description="watching/ready/invalid/archived"
    )


class ObservationBatchImportItem(BaseModel):
    """批量导入单项。"""

    symbol_id: int
    origin_type: str = "manual"
    origin_id: int | None = None
    reason: dict | None = None
    priority: int = 0
    tags: list[str] = Field(default_factory=list)
    target_portfolio_id: int | None = None
    note: str | None = None


class ObservationBatchImport(BaseModel):
    """观察项批量导入请求。"""

    watchlist_id: int
    items: list[ObservationBatchImportItem]


class ObservationBatchImportResult(BaseModel):
    """批量导入结果。"""

    imported: int = Field(0, description="新导入数量")
    existing: int = Field(0, description="已存在数量")
    failed: int = Field(0, description="失败数量")
    errors: list[dict] = Field(default_factory=list)

