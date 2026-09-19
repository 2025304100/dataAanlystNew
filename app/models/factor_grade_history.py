"""因子质量分级历史 ORM（T36，设计 §7.10 / 需求 §6.8）。

每次评定（自动定级 / 人工调整 / 季度重评）**落一行**，用于：
- 等级变更可追溯（谁在何时因何理由把等级从 A 改成 B）；
- 季度重评判定所需的历史序列（B 连续 2 季稳定升 A、S/A 连续 2 季下滑降级）；
- `grade_manual_adjusted=1` 的因子**不被季度任务覆盖**（按本表最新版本判定）。

建表归属：alembic 修订 `wps_0023_059`（文件 0059，随 T36）。

数值列纪律（P0）：`icir` / `decay_ratio` 写入前必须过
`app.core.db_numeric.to_db_float`（NaN/±Inf → None，不可归 0）。
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class FactorGradeHistory(Base):
    """因子等级评定历史（每次评定一行，不覆盖历史）。"""

    __tablename__ = "factor_grade_history"
    __table_args__ = (
        Index("ix_factor_grade_history_version_created",
              "factor_version_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)

    #: 被评定的因子版本（factor_versions.id）
    factor_version_id: Mapped[str] = mapped_column(String(64), index=True)

    #: 评定结果：S / A / B / C / D
    grade: Mapped[str] = mapped_column(String(2))

    #: 上一次等级（首次评定为 None）
    previous_grade: Mapped[str | None] = mapped_column(String(2), nullable=True)

    #: 定级理由（8 维度达标情况 + 硬约束生效说明）
    reason: Mapped[str] = mapped_column(Text)

    #: 8 维度指标快照 JSON（评定当时的输入，便于回溯）
    metrics_snapshot_json: Mapped[str] = mapped_column(Text)

    #: 阈值来源：default（默认阈值）/ custom（设置页调整）
    thresholds_source: Mapped[str] = mapped_column(String(16), default="default")

    #: 阈值快照 JSON（custom 时记录当时的阈值，保证可复现）
    threshold_snapshot_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: 评定来源：auto（挖掘/评估后自动）/ manual（人工调整）/ quarterly（季度重评）
    source: Mapped[str] = mapped_column(String(24), default="auto")

    #: 季度重评动作：promote / demote / keep（非季度重评为 None）
    action: Mapped[str | None] = mapped_column(String(16), nullable=True)

    #: 1 = 人工调整过等级，季度重评**不得覆盖**
    manual_adjusted: Mapped[int] = mapped_column(Integer, default=0)

    #: 人工调整的操作人与原因（原因 ≥10 字，由 service 层校验）
    adjusted_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    adjust_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: 降级时是否已移出 FactorSet
    removed_from_factor_set: Mapped[int] = mapped_column(Integer, default=0)

    #: 关键数值（冗余存一份，便于查询与曲线；写入前过 to_db_float）
    icir: Mapped[float | None] = mapped_column(Float, nullable=True)
    decay_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


__all__ = ["FactorGradeHistory"]
