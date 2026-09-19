from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class DiscoveryTaskCreate(BaseModel):
    scope: Literal["cn-stock", "cn-etf", "us-stock", "us-etf"] = Field(default="cn-stock")
    min_score: float = Field(default=55, ge=0, le=100)
    include_news: bool = True
    portfolio_id: int | None = None
    portfolio_rule_id: int | None = None
    start_date: date | None = None
    end_date: date | None = None
    adjust: str = "qfq"
    symbol_limit: int | None = Field(default=None, ge=1, le=10000)
    batch_size: int = Field(default=20, ge=1, le=100)
    delay_seconds: float = Field(default=0.25, ge=0, le=5)
    # 并发线程数：1=串行（默认，最稳定），>1 时分批并发同步标的，提升挖掘速度
    # 上限 3：东财 push2 接口有 IP 频次风控，并发过高会触发断连
    # 并发模式下 adaptive_delay 不生效（并发本身已分摊请求频率）
    max_workers: int = Field(default=1, ge=1, le=3)
    news_limit: int = Field(default=30, ge=0, le=100)
    global_mode: str = "library"
    refresh_universe: bool = True
    use_cached_bars_first: bool = True
    use_cached_symbols_only: bool = True
    warning_days: int = Field(default=3, ge=1, le=60)
    valid_days: int = Field(default=5, ge=1, le=365)


class DiscoveryTaskRead(BaseModel):
    id: str
    status: str
    stage: str
    percent: float
    message: str
    scope: str
    min_score: float
    include_news: bool
    total: int
    processed: int
    ok_count: int
    failed_count: int
    empty_count: int
    scored_count: int
    current_symbol: str | None = None
    batch_size: int
    delay_seconds: float
    adaptive_delay_seconds: float
    symbol_limit: int | None = None
    scan_run_id: int | None = None
    executable_count: int = 0
    cleanup_count: int = 0
    news_symbols_total: int = 0
    errors: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime
    started_at: datetime | None = None
    paused_at: datetime | None = None
    cancelled_at: datetime | None = None
    finished_at: datetime | None = None
    updated_at: datetime | None = None
    can_resume: bool = False
    can_retry: bool = False


class DiscoveryScopeStatsRead(BaseModel):
    scope: str
    total_symbols: int
    cached_symbols: int
    active_symbols: int


class DiscoveryResultUpdate(BaseModel):
    is_frozen: bool | None = None
    warning_days: int | None = Field(default=None, ge=1, le=60)
    valid_days: int | None = Field(default=None, ge=1, le=365)


class DiscoveryIndicatorEvaluateRequest(BaseModel):
    scan_result_ids: list[int] = Field(default_factory=list)
    indicator_keys: list[str] = Field(default_factory=list)


class DiscoveryIndicatorEvaluationRow(BaseModel):
    scan_result_id: int
    symbol_id: int
    values: dict[str, bool | float | None] = Field(default_factory=dict)


# ── WP-P.4 数据准备与用户扫描分离 ──────────────────────────────


DiscoveryScopeLiteral = Literal["cn-stock", "cn-etf", "us-stock", "us-etf"]


class FastScanRequest(BaseModel):
    """用户快速扫描请求（同步执行，目标 < 5 分钟）。

    约束：扫描路径不发起任何第三方 HTTP 请求；
    只读 ready 状态快照，绝不读 building 半成品。
    """

    scope: DiscoveryScopeLiteral = Field(default="cn-stock")
    min_score: float = Field(default=55, ge=0, le=100)
    asset_types: list[str] | None = None
    stages: list[str] | None = None
    actions: list[str] | None = None
    # 自定义指标计划，WP-P.5 完整实现向量化；上限 5 个
    indicator_plan: dict[str, Any] | None = None
    portfolio_id: int | None = None
    portfolio_rule_id: int | None = None
    # SQL 粗筛 Top-K 上限（WP-P.5 完整实现）
    limit: int = Field(default=300, ge=1, le=300)


class FastScanResponse(BaseModel):
    """用户快速扫描响应。"""

    snapshot_id: int | None = None
    snapshot_generated_at: datetime | None = None
    scope: str
    total_in_snapshot: int = 0
    coarse_match_count: int = 0
    advanced_match_count: int = 0
    result_rows_written: int = 0
    cache_key: str | None = None
    cache_hit: bool = False
    results: list[dict[str, Any]] = Field(default_factory=list)
    duration_ms: float = 0.0
    degraded_reason: str | None = None
    recommended_action: str | None = None
    # WP-P-FIX.2: 无快照时自动启动的数据准备任务 ID（供前端轮询状态）
    data_prep_task_id: str | None = None
    # WP-P-FIX.2: 历史快照数据截止时间（using_stale_snapshot 时提供）
    data_cutoff_at: str | None = None


class DataPrepRequest(BaseModel):
    """后台数据准备任务创建请求（异步执行，不受 5 分钟 SLA 约束）。

    链路：行情增量同步 → 外部因子/宏观更新 → 因子与评分增量计算 →
          dirty 集合计算 → ready 评分快照生成 → 可选触发快速扫描。
    """

    scope: DiscoveryScopeLiteral = Field(default="cn-stock")
    trade_date: date | None = None
    force_full_rebuild: bool = False
    # 数据就绪后是否自动触发快速扫描
    trigger_fast_scan_after_ready: bool = False
    # 自动触发快速扫描时使用的参数（透传给 run_fast_scan）
    fast_scan_params: dict[str, Any] | None = None


class SnapshotStatusRead(BaseModel):
    """快照状态查询响应（GET /discovery/snapshot/status）。"""

    scope: str
    has_ready_snapshot: bool = False
    ready_snapshot_id: int | None = None
    ready_snapshot_generated_at: datetime | None = None
    ready_snapshot_trade_date: date | None = None
    ready_snapshot_symbol_count: int | None = None
    ready_snapshot_dirty_symbol_count: int | None = None
    has_building_snapshot: bool = False
    building_snapshot_id: int | None = None
    building_snapshot_created_at: datetime | None = None
    last_data_prep_task_id: str | None = None
    last_data_prep_status: str | None = None
    recommended_action: str | None = None
    # WP-P.8：最近一次 fast_scan 的 timings / status（供前端展示具体慢在哪一步）
    # 数据来源：AsyncTaskRecord.result_json.timings（task_type="discovery_fast_scan"）
    # 当前 fast_scan 为同步执行，无 task 记录时为 None
    last_fast_scan_timings: dict[str, Any] | None = None
    last_fast_scan_status: str | None = None