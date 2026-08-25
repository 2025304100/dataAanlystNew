"""幂等执行记录（保证任何入口同一操作不重复执行，WP0-2 TR-02.6 + FR-P0-3 双重契约兼容）。

双模式兼容：
  - idempotency_key 仍是主键（业务幂等键）；
  - 新增 request_hash / response_json（TR-02.6 需要，对应参数冲突校验与原响应返回）：
    老字段 result_json → WP0-2 统一别名 response_json（同表两列共存，老代码不破坏）；
    request_hash 从 nullable 变为 NOT NULL（服务层在写入时保证）。
  - 新增 correlation_id / portfolio_id / expire_at 以对齐 OutboxEvent + 审计事件 三链路。
"""
from __future__ import annotations
from datetime import datetime, timezone
from sqlalchemy import (
    DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, Index,
)
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (
        Index("ix_idem_correlation_id_v2", "correlation_id"),
        Index("ix_idem_expire_at_v2", "expire_at"),
        Index("ix_idem_portfolio_id_v2", "portfolio_id"),
    )

    # ═════════════════════════════════════════════════════════════════════════
    # 主键：WP0-2 契约需要一个独立 autoincrement id 列 + idempotency_key 再做 UNIQUE
    # ═════════════════════════════════════════════════════════════════════════
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    idempotency_key: Mapped[str] = mapped_column(
        String(128), nullable=False, unique=True, index=True,
        comment="业务幂等键（如 pid:trade_date:run_type:req_hash 或前端 UUID 头透传值）",
    )
    entity_type: Mapped[str] = mapped_column(
        String(32), nullable=False, index=True,
        comment="decision_run / portfolio_factor_usage / outbox / alert / order_plan",
    )
    entity_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True,
        comment="已创建实体ID（重复请求返回此实体，不重复创建）",
    )

    # ═════════════════════════════════════════════════════════════════════════
    # WP0-2 TR-02.6 契约新增列
    # ═════════════════════════════════════════════════════════════════════════
    request_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default="",
        comment="请求体 canonical JSON SHA256 —— 同 key 不同 hash → HTTP 409 参数冲突",
    )
    response_json: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="{}",
        comment="WP0-2 标准列：首次成功响应 canonical JSON（重放时原封不动返回）",
    )
    # 保留老代码兼容别名 result_json ≡ response_json
    @property
    def result_json(self) -> str:  # pragma: no cover - backward compat alias
        return self.response_json

    correlation_id: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default="", index=True,
        comment="与 OutboxEvent / 审计事件 correlation_id 对齐，跨请求/事务/事件三链路串联",
    )
    portfolio_id: Mapped[int | None] = mapped_column(
        ForeignKey("portfolios.id", ondelete="RESTRICT"), nullable=True, index=True,
    )
    expire_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, index=True,
        comment="过期时间；NULL=与组合生命周期同长；短幂等可设为 created_at+24h",
    )
    executed_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, index=True,
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None),
        comment="实际首次执行时间（通常=created_at；如果服务有延迟则≈首次落库时间）",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, index=True,
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None),
        comment="WP0-2 标准列：幂等记录创建时间（≈executed_at）",
    )
