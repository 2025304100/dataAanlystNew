"""BFG 过滤治理 - 预检请求/响应 DTO。

严格对齐 PRD §6.3 的返回结构。
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.backtest import BacktestFilterConfigDTO


# ----------------- Task 12.1: 数据截止日期信息 -----------------
class DataCutoffInfo(BaseModel):
    """回测/预检响应返回的数据截止信息（5+1 要素）。

    三者不一致时，调用方按 unified_earliest 作为回测有效终点。
    """

    model_config = ConfigDict(extra="forbid")

    bars_date: Optional[date] = Field(default=None, description="行情(universe_daily_bars)截止日期")
    status_date: Optional[date] = Field(default=None, description="状态(security_status_daily)截止日期")
    factors_date: Optional[date] = Field(default=None, description="因子/评分(scores/factor_scores)截止日期")
    unified_earliest: Optional[date] = Field(default=None, description="bars/status/factors 三者有效值中的最小值")
    sync_at: Optional[datetime] = Field(default=None, description="本次查询时的数据同步时间(UTC)")
    version: Optional[str] = Field(default=None, description="数据快照版本，格式如 bfg-dataset-YYYYMMDD")


# ----------------- 通用 Blocker/Warning（复用 wp5 契约） -----------------
class PrecheckBlockerItem(BaseModel):
    """结构化阻断项或告警项（code/severity/category/title_zh/detail_zh/evidence/fix_link 六元组）。"""

    model_config = ConfigDict(extra="forbid")

    code: str
    severity: Literal["error", "warning", "info"] = Field(
        description="error=阻断; warning=不阻断提示; info=信息"
    )
    category: Literal["data", "formula", "config", "universe", "filter", "target"] = Field(
        default="data"
    )
    title_zh: str
    detail_zh: str = Field(default="")
    correlation_id: str = Field(
        default_factory=lambda: __import__("uuid").uuid4().hex[:8]
    )
    evidence: dict[str, Any] = Field(default_factory=dict)
    fix_link: Optional[dict[str, Any]] = Field(default=None)
    retryable: bool = Field(default=False)


class TargetCoverageInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    covered_days: int
    requested_days: int


class ExcludedSymbolDays(BaseModel):
    """PRD §6.3 excluded_symbol_days 结构。"""

    model_config = ConfigDict(extra="forbid")

    new_listing: int = 0
    st: int = 0
    suspended: int = 0
    delisting_period: int = 0
    status_unknown: int = 0
    price_or_volume_invalid: int = 0


# ----------------- 回测预检 -----------------
class BacktestPrecheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    portfolio_id: int = Field(ge=1)
    symbol_ids: list[int] = Field(default_factory=list)
    start_date: date
    end_date: date
    filter_config: BacktestFilterConfigDTO | None = Field(default=None)
    minimum_trade_days: int = Field(default=300, ge=1, le=10000)


class BacktestPrecheckResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requested_trade_days: int
    usable_trade_days: int
    minimum_trade_days: int
    excluded_symbol_days: ExcludedSymbolDays = Field(
        default_factory=ExcludedSymbolDays
    )
    target_coverage: TargetCoverageInfo | None = Field(
        default=None, description="回测预检阶段目标覆盖可先 NULL"
    )
    warnings: list[PrecheckBlockerItem] = Field(default_factory=list)
    blocking_reasons: list[PrecheckBlockerItem] = Field(default_factory=list)
    filter_config_hash: str = Field(
        default="", description="当前请求下 BacktestFilterConfig.hash"
    )
    production_fidelity: bool = Field(
        default=True, description="当前请求是否为生产保真"
    )
    non_fidelity_reason: str | None = Field(default=None)
    # ---------- Task 12.1: 数据截止日期信息 ----------
    data_cutoff: DataCutoffInfo | None = Field(
        default=None, description="行情/状态/因子三类数据的截止日期快照"
    )
    cutoff_mismatch: bool = Field(
        default=False, description="若 bars/status/factors 三者不全相等则为 True"
    )
    cutoff_warning_zh: str | None = Field(
        default=None, description="cutoff_mismatch=True 时的中文警告文案"
    )


# ----------------- 因子评价预检 -----------------
class FactorEvaluationPrecheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    factor_code: str | None = Field(default=None)
    factor_version_id: int | None = Field(default=None, ge=1)
    universe: str = Field(default="zz500", min_length=1)
    start_date: date
    end_date: date
    target_horizon: int = Field(
        default=5, ge=1, le=252, description="前瞻收益天数"
    )
    filter_config: BacktestFilterConfigDTO | None = Field(default=None)
    minimum_trade_days: int = Field(default=300, ge=1, le=10000)


class FactorEvaluationPrecheckResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requested_trade_days: int
    usable_trade_days: int
    minimum_trade_days: int
    excluded_symbol_days: ExcludedSymbolDays = Field(
        default_factory=ExcludedSymbolDays
    )
    target_coverage: TargetCoverageInfo
    warnings: list[PrecheckBlockerItem] = Field(default_factory=list)
    blocking_reasons: list[PrecheckBlockerItem] = Field(default_factory=list)
    filter_config_hash: str = Field(default="")
    production_fidelity: bool = Field(default=True)
    non_fidelity_reason: str | None = Field(default=None)
    # ---------- Task 12.1: 数据截止日期信息 ----------
    data_cutoff: DataCutoffInfo | None = Field(default=None)
    cutoff_mismatch: bool = Field(default=False)
    cutoff_warning_zh: str | None = Field(default=None)
