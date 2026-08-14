from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
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


class Portfolio(Base):
    __tablename__ = "portfolios"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    account_type: Mapped[str] = mapped_column(String(16))
    # Asset universe is a hard portfolio constraint: stock / etf / mixed.
    # Existing portfolios are migrated to mixed to avoid silently blocking them.
    asset_scope: Mapped[str] = mapped_column(String(16), default="mixed", index=True)
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
    )
    # P1-FIX: 创建组合时持久化的佣金/风控/基准参数（前端表单曾只存本地状态未发）
    buy_fee_pct: Mapped[float] = mapped_column(Float, default=0.00025)  # 默认 0.025%
    sell_fee_pct: Mapped[float] = mapped_column(Float, default=0.00025)  # 默认 0.025%
    benchmark_code: Mapped[str] = mapped_column(String(32), default="000300")  # 沪深300
    # P1-FIX: 创建时声明的默认单票仓位上限（ratio，例如 0.3=30%），可被 PortfolioRule 覆盖
    default_single_position_pct: Mapped[float] = mapped_column(Float, default=0.30)
    # P2-FIX: 测试组合隔离。默认 0=生产组合，1=验收/研发等测试数据。list 默认过滤 is_test=1。
    is_test: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    rules = relationship("PortfolioRule", back_populates="portfolio_ref", cascade="all, delete-orphan")
    positions = relationship("Position", back_populates="portfolio_ref", cascade="all, delete-orphan")


class PortfolioRule(Base):
    __tablename__ = "portfolio_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolios.id", ondelete="CASCADE"), index=True)
    rule_name: Mapped[str] = mapped_column(String(128))
    max_single_position_pct: Mapped[float] = mapped_column(Float)
    max_sector_position_pct: Mapped[float] = mapped_column(Float)
    max_stock_position_pct: Mapped[float] = mapped_column(Float)
    max_etf_position_pct: Mapped[float] = mapped_column(Float)
    max_loss_per_trade_pct: Mapped[float] = mapped_column(Float)
    max_open_positions: Mapped[int] = mapped_column(Integer)
    stage_limits_json: Mapped[str] = mapped_column(Text)
    is_active: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))

    portfolio_ref = relationship("Portfolio", back_populates="rules")


class Position(Base):
    __tablename__ = "positions"
    __table_args__ = (UniqueConstraint("portfolio_id", "symbol_id", name="uq_portfolio_symbol_position"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolios.id", ondelete="CASCADE"), index=True)
    symbol_id: Mapped[int] = mapped_column(ForeignKey("symbols.id", ondelete="CASCADE"), index=True)
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
