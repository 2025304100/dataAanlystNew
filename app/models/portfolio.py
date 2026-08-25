from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, CheckConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

# 自动交易来源模式：portfolio=持仓+候选池+成员（默认），members_only=只执行自动成员，legacy_scan=兼容旧全局扫描结果
AUTO_TRADE_SOURCE_PORTFOLIO = "portfolio"
AUTO_TRADE_SOURCE_MEMBERS_ONLY = "members_only"
AUTO_TRADE_SOURCE_LEGACY_SCAN = "legacy_scan"
AUTO_TRADE_SOURCE_MODES = frozenset(
    {
        AUTO_TRADE_SOURCE_PORTFOLIO,
        AUTO_TRADE_SOURCE_MEMBERS_ONLY,
        AUTO_TRADE_SOURCE_LEGACY_SCAN,
    }
)

# ═══════════════════════════════════════════════════════════════════════════
# T-D1 Q27.1：六状态枚举值（数字越大阻断级别越高；总状态聚合函数按 value 取 max）
# ═══════════════════════════════════════════════════════════════════════════
from enum import Enum  # noqa: E402  (保证在 Base 之后导入，且不打乱 import 顺序)

PORTFOLIO_STATUS_READY = "READY"
PORTFOLIO_STATUS_RUNNING = "RUNNING"
PORTFOLIO_STATUS_SCORE_STALE = "SCORE_STALE"
PORTFOLIO_STATUS_DATA_INCOMPLETE_PAUSED = "DATA_INCOMPLETE_PAUSED"
PORTFOLIO_STATUS_MODEL_INACTIVE = "MODEL_INACTIVE"
PORTFOLIO_STATUS_RECONCILIATION_BLOCKED = "RECONCILIATION_BLOCKED"

PORTFOLIO_STATUS_VALUES = frozenset(
    {
        PORTFOLIO_STATUS_READY,
        PORTFOLIO_STATUS_RUNNING,
        PORTFOLIO_STATUS_SCORE_STALE,
        PORTFOLIO_STATUS_DATA_INCOMPLETE_PAUSED,
        PORTFOLIO_STATUS_MODEL_INACTIVE,
        PORTFOLIO_STATUS_RECONCILIATION_BLOCKED,
    }
)

PORTFOLIO_STATUS_BLOCK_LEVEL: dict[str, int] = {
    PORTFOLIO_STATUS_READY: 1,
    PORTFOLIO_STATUS_RUNNING: 2,
    PORTFOLIO_STATUS_SCORE_STALE: 3,
    PORTFOLIO_STATUS_DATA_INCOMPLETE_PAUSED: 4,
    PORTFOLIO_STATUS_MODEL_INACTIVE: 5,
    PORTFOLIO_STATUS_RECONCILIATION_BLOCKED: 6,
}

# 反向：数字 → 字符串（给 D2 resolve_composite_status 反查用）
_PORTFOLIO_BLOCK_LEVEL_TO_STATUS: dict[int, str] = {
    v: k for k, v in PORTFOLIO_STATUS_BLOCK_LEVEL.items()
}


class PortfolioStatus(str, Enum):
    """WP0-2/TR-04：组合阻断级别六状态。value=阻断级别数值；name=状态字符串名。

    聚合规则 T-D2：总 composite = max(status_score, status_data, status_model, status_reconciliation).value
        → 然后反查表得字符串名。
    """

    READY = PORTFOLIO_STATUS_READY
    RUNNING = PORTFOLIO_STATUS_RUNNING
    SCORE_STALE = PORTFOLIO_STATUS_SCORE_STALE
    DATA_INCOMPLETE_PAUSED = PORTFOLIO_STATUS_DATA_INCOMPLETE_PAUSED
    MODEL_INACTIVE = PORTFOLIO_STATUS_MODEL_INACTIVE
    RECONCILIATION_BLOCKED = PORTFOLIO_STATUS_RECONCILIATION_BLOCKED

    @property
    def block_level(self) -> int:
        return PORTFOLIO_STATUS_BLOCK_LEVEL[self.value]

    @classmethod
    def from_block_level(cls, level: int) -> "PortfolioStatus":
        return cls(_PORTFOLIO_BLOCK_LEVEL_TO_STATUS[int(level)])


_PORTFOLIO_STATUS_IN_LIST_CLAUSE = (
    "'READY','RUNNING','SCORE_STALE','DATA_INCOMPLETE_PAUSED','MODEL_INACTIVE','RECONCILIATION_BLOCKED'"
)


class Portfolio(Base):
    __tablename__ = "portfolios"
    __table_args__ = (
        CheckConstraint(
            f"status_score IN ({_PORTFOLIO_STATUS_IN_LIST_CLAUSE})",
            name="ck_portfolios_status_score_values",
        ),
        CheckConstraint(
            f"status_data IN ({_PORTFOLIO_STATUS_IN_LIST_CLAUSE})",
            name="ck_portfolios_status_data_values",
        ),
        CheckConstraint(
            f"status_model IN ({_PORTFOLIO_STATUS_IN_LIST_CLAUSE})",
            name="ck_portfolios_status_model_values",
        ),
        CheckConstraint(
            f"status_reconciliation IN ({_PORTFOLIO_STATUS_IN_LIST_CLAUSE})",
            name="ck_portfolios_status_reconciliation_values",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    account_type: Mapped[str] = mapped_column(String(16))
    # Asset universe is a hard portfolio constraint: stock / etf / mixed.
    # Existing portfolios are migrated to mixed to avoid silently blocking them.
    asset_scope: Mapped[str] = mapped_column(
        String(16), default="mixed", server_default="mixed", index=True,
    )
    total_capital: Mapped[float] = mapped_column(Float)
    investable_ratio: Mapped[float] = mapped_column(Float)
    cash_reserve_ratio: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(16), default="CNY")
    is_default: Mapped[int] = mapped_column(Integer, default=0)
    # P2-3：自动交易开关（0=关闭，1=开启）。开启后定时任务才会扫描该组合。
    auto_trade_enabled: Mapped[int] = mapped_column(Integer, default=0)
    # P2-3：自动交易最后执行时间（用于审计与展示）
    auto_trade_last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # P0-AutoTrade：组合级自动交易标的来源模式，持久化存储，不依赖环境变量
    auto_trade_source_mode: Mapped[str] = mapped_column(
        String(32),
        default=AUTO_TRADE_SOURCE_PORTFOLIO,
        server_default=AUTO_TRADE_SOURCE_PORTFOLIO,
    )
    # ════════════════════════════════════════════════════════════════════════
    # T-D1 Q27.2：四维度分表/字段存储（阻断级别独立计算；总状态 composite_status 不在 DB 存，实时算）
    # 默认 READY，历史组合自动保持"就绪"语义。
    # ════════════════════════════════════════════════════════════════════════
    status_score: Mapped[str] = mapped_column(
        String(32),
        default=PORTFOLIO_STATUS_READY,
        server_default=PORTFOLIO_STATUS_READY,
        nullable=False,
        index=True,
        comment="T-D1: Score 维度状态（缺 Score/超新鲜度 → SCORE_STALE）",
    )
    status_data: Mapped[str] = mapped_column(
        String(32),
        default=PORTFOLIO_STATUS_READY,
        server_default=PORTFOLIO_STATUS_READY,
        nullable=False,
        index=True,
        comment="T-D1: 数据完整度维度状态（DATA_BLOCKED/缺口 → DATA_INCOMPLETE_PAUSED）",
    )
    status_model: Mapped[str] = mapped_column(
        String(32),
        default=PORTFOLIO_STATUS_READY,
        server_default=PORTFOLIO_STATUS_READY,
        nullable=False,
        index=True,
        comment="T-D1: 模型/因子维度状态（active_model_run_id 缺失/切换未确认 → MODEL_INACTIVE）",
    )
    status_reconciliation: Mapped[str] = mapped_column(
        String(32),
        default=PORTFOLIO_STATUS_READY,
        server_default=PORTFOLIO_STATUS_READY,
        nullable=False,
        index=True,
        comment="T-D1: 对账一致性维度状态（差异≠0/人工未复核通过 → RECONCILIATION_BLOCKED）",
    )
    # P1-FIX: 创建组合时持久化的佣金/风控/基准参数（前端表单曾只存本地状态未发）
    buy_fee_pct: Mapped[float] = mapped_column(Float, default=0.00025, server_default="0.00025")  # 默认 0.025%
    sell_fee_pct: Mapped[float] = mapped_column(Float, default=0.00025, server_default="0.00025")  # 默认 0.025%
    benchmark_code: Mapped[str] = mapped_column(String(32), default="000300", server_default="000300")  # 沪深300
    # P1-FIX: 创建时声明的默认单票仓位上限（ratio，例如 0.3=30%），可被 PortfolioRule 覆盖
    default_single_position_pct: Mapped[float] = mapped_column(Float, default=0.30, server_default="0.30")
    # P2-FIX: 测试组合隔离。默认 0=生产组合，1=验收/研发等测试数据。list 默认过滤 is_test=1。
    is_test: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # Q5.3: 关键成员 symbol_id 列表 JSON(list[int])；NULL/空=全组合默认关键
    key_members_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_decision_trade_date: Mapped[date | None] = mapped_column(Date, nullable=True, comment="决策完成日=order_plan全落库+DecisionRun=SUCCEEDED后UPDATE；NULL=从未接管/从未决策成功")
    last_reconciled_trade_date: Mapped[date | None] = mapped_column(Date, nullable=True, comment="对账完成日=T+1撮合成交+持仓刷新+对账差异=0后UPDATE；NULL=从未对账完成")
    lease_key: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True, comment="租约持有标识（字符串随机值）；分布式调度抢锁用")
    lease_expire_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="租约到期UTC naive时间；超过即可被其他worker抢锁")
    effective_start_date: Mapped[date | None] = mapped_column(Date, nullable=True, comment="组合生效日；首次自动接管时从此日期后首个交易日开始；NULL=兼容老组合（使用created_at推断）")
    auto_schedule_hour: Mapped[int] = mapped_column(Integer, default=20, server_default="20", comment="正式auto_simulation调度小时；仅允许 20-23（应用层SCHEDULE_TOO_EARLY拦截）")
    auto_schedule_minute: Mapped[int] = mapped_column(Integer, default=30, server_default="30", comment="正式auto_simulation调度分钟；0-59")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    rules = relationship("PortfolioRule", back_populates="portfolio_ref", cascade="all, delete-orphan")
    positions = relationship("Position", back_populates="portfolio_ref", cascade="all, delete-orphan")


class PortfolioRule(Base):
    __tablename__ = "portfolio_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolios.id", ondelete="RESTRICT"), index=True)
    rule_name: Mapped[str] = mapped_column(String(128))
    max_single_position_pct: Mapped[float] = mapped_column(Float)
    max_sector_position_pct: Mapped[float] = mapped_column(Float)
    max_stock_position_pct: Mapped[float] = mapped_column(Float)
    max_etf_position_pct: Mapped[float] = mapped_column(Float)
    max_loss_per_trade_pct: Mapped[float] = mapped_column(Float)
    max_open_positions: Mapped[int] = mapped_column(Integer)
    stage_limits_json: Mapped[str] = mapped_column(Text)
    exit_config_json: Mapped[str | None] = mapped_column(
        Text, nullable=True,
        comment="T-B6 Q11.2: 退出策略配置。默认 NULL/空等价 {\"phased_exit_enabled\": false}（一次性清仓）；"
                "仅显式 {\"phased_exit_enabled\": true, \"phases\": [{\"pct\": 0.5, \"delay_days\": 5}, ...]} 才走分阶段。",
    )
    is_active: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    portfolio_ref = relationship("Portfolio", back_populates="rules")


class Position(Base):
    __tablename__ = "positions"
    __table_args__ = (UniqueConstraint("portfolio_id", "symbol_id", name="uq_portfolio_symbol_position"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolios.id", ondelete="RESTRICT"), index=True)
    symbol_id: Mapped[int] = mapped_column(ForeignKey("symbols.id", ondelete="RESTRICT"), index=True)
    quantity: Mapped[float] = mapped_column(Float, default=0)
    avg_cost: Mapped[float] = mapped_column(Float, default=0)
    latest_price: Mapped[float] = mapped_column(Float, default=0)
    market_value: Mapped[float] = mapped_column(Float, default=0)
    position_pct: Mapped[float] = mapped_column(Float, default=0)
    asset_type: Mapped[str] = mapped_column(String(16))
    theme: Mapped[str | None] = mapped_column(String(64), nullable=True)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    portfolio_ref = relationship("Portfolio", back_populates="positions")
