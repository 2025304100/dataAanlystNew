"""Task 11 (FR-12 / AC-11): 训练不可变权重快照 ORM。

Per-model 聚合表：每次训练成功 INSERT 1 行，按 model_id PK 唯一存储
FactorSet + 成员元数据 + 权重 JSON。即使后续集合废弃/成员修改，
模型详情也能从 snapshot 稳定还原权重→版本→集合的全链血缘。

表名与 factor_model.py 中已被移除的旧 FactorWeightSnapshot（per-factor rows，
rev 006 创建）相同，rev 049 中做了 DROP+CREATE 重建。
"""
from __future__ import annotations

from datetime import date as _date, datetime, timezone

from sqlalchemy import Date, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class FactorWeightSnapshot(Base):
    """不可变训练快照（per-model aggregate row）。

    PK = model_id；同 model 二次训练 INSERT 采用幂等 do-nothing
    （ON CONFLICT DO NOTHING 或 MySQL INSERT IGNORE，见写入层）。
    """

    __tablename__ = "factor_weight_snapshots"

    # ── PK / 稳定关联键 ──
    model_id: Mapped[str] = mapped_column(
        # The primary key already provides the required index. Declaring a
        # second implicit index makes the MySQL auto-aligner attempt a
        # redundant index on legacy schemas.
        String(64), primary_key=True, nullable=False
    )
    factor_set_id: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )
    factor_set_content_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )

    # ── 4 成员元数据 JSON 数组（factor_code→member 顺序一一对应） ──
    factor_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    factor_version_ids_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]"
    )
    roles_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    constraints_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]"
    )
    missing_strategies_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]"
    )

    # ── 5 DATE 列（训练/验证窗口 + 数据截止） ──
    train_start_date: Mapped[_date | None] = mapped_column(
        Date, nullable=True
    )
    train_end_date: Mapped[_date | None] = mapped_column(
        Date, nullable=True
    )
    valid_start_date: Mapped[_date | None] = mapped_column(
        Date, nullable=True
    )
    valid_end_date: Mapped[_date | None] = mapped_column(
        Date, nullable=True
    )
    data_cutoff_date: Mapped[_date] = mapped_column(
        Date, nullable=False
    )

    # ── 权重 JSON：raw = 原始 ridge coef；norm = L1 归一化 ──
    weights_raw_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="{}"
    )
    weights_norm_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]"
    )

    # ── 训练模式 / 创建时间 ──
    train_mode: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow_naive, nullable=False, index=True
    )
