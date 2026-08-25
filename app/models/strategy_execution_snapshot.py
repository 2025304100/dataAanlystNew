"""StrategyExecutionSnapshot（WP0-2 标准入口）。

注意：`app/models/decision_engine.py` 中已存在一个同名 ORM 类，这里用
`__table_args__` 中的 `extend_existing=True`（通过 `useexisting=True` 在
SQLAlchemy 1.4+/2.x 中等价）允许「同一个 MetaData + 同 __tablename__」
两次 declare 不报错。实际 SQLAlchemy 2.x 正确做法是设置：
    __table_args__ = {"extend_existing": True, ...}
这样 import 顺序无论谁先谁后都 OK；字段采用「后声明 = 实际生效的结构」。
WP0-2 契约（测试要求的字段清单）：
    id                  INTEGER/BigInteger PK autoinc
    portfolio_id        INTEGER FK portfolios.id
    usage_binding_id    INTEGER FK portfolio_factor_usage.id （别名：原 decision_engine 中 portfolio_factor_usage_id → 同列不同属性名）
    snapshot_no         INTEGER (portfolio_id, snapshot_no) 组合唯一
    snapshot_json       TEXT  NOT NULL （原 decision_engine 里的完整规则冻结字段集合保留别名）
    content_hash        CHAR(64) NOT NULL (== snapshot_hash canonical)
    factor_set_id       INTEGER FK
    factor_model_run_id INTEGER FK
    created_by          String(128)
    created_at          DateTime NOT NULL
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, Index, MetaData,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class StrategyExecutionSnapshot(Base):
    __tablename__ = "strategy_execution_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "portfolio_id", "snapshot_no",
            name="uq_strategy_execution_snapshot_portfolio_no",
        ),
        {"extend_existing": True},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    portfolio_id: Mapped[int] = mapped_column(
        ForeignKey("portfolios.id", ondelete="RESTRICT"), index=True, nullable=False,
    )
    usage_binding_id: Mapped[int | None] = mapped_column(
        "usage_binding_id",
        ForeignKey("portfolio_factor_usage.id", ondelete="RESTRICT"),
        nullable=True, index=True,
        comment="反查：哪一次 save_and_apply 生成了该快照",
    )
    snapshot_no: Mapped[int] = mapped_column(
        Integer, nullable=False, index=True, default=1, server_default="1",
        comment="同一组合内单调递增（1-based），永不复用；迁移历史回填时按 max(snapshot_no)+1。",
    )
    snapshot_json: Mapped[str] = mapped_column(
        Text, nullable=True, default="{}", server_default="{}",
        comment="规则/权重/模型/信号/风控/成本 全部冻结的 canonical JSON 结构",
    )
    content_hash: Mapped[str] = mapped_column(
        String(64), nullable=True, default="", server_default="", index=True,
        comment="T4a canonical JSON SHA-256 十六进制；任何字段变更哈希必变",
    )
    factor_set_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, index=True, comment="FactorSet.id（纯 INTEGER，兼容老 STRING FK 用外键字段单独做）",
    )
    factor_model_run_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, index=True, comment="FactorModelRun.id（纯 INTEGER）",
    )
    created_by: Mapped[str | None] = mapped_column(
        String(128), nullable=True,
        comment="操作者邮箱/用户名（审计溯源）；系统自动生成=SYSTEM_MIGRATION",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(timezone.utc),
    )


# 兼容老代码 decision_engine.py 的属性名：把 snapshot_hash 属性别名指向 content_hash
def _snapshot_hash_backward_compat() -> None:  # pragma: no cover - runtime alias only
    cls = StrategyExecutionSnapshot
    if not hasattr(cls, "snapshot_hash"):
        def _get(self):  # type: ignore[no-redef]
            return self.content_hash
        def _set(self, value):  # type: ignore[no-redef]
            self.content_hash = value
        cls.snapshot_hash = property(_get, _set)  # type: ignore[attr-defined]
    if not hasattr(cls, "portfolio_factor_usage_id"):
        def _get_u(self):  # type: ignore[no-redef]
            return self.usage_binding_id
        def _set_u(self, value):  # type: ignore[no-redef]
            self.usage_binding_id = value
        cls.portfolio_factor_usage_id = property(_get_u, _set_u)  # type: ignore[attr-defined]

_snapshot_hash_backward_compat()
