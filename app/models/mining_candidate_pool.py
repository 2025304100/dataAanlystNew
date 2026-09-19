"""候选池域 ORM（设计文档 §5.1 第 1~3 项 / §6.3）。

⚠️ 隔离模型（需求 §3.8）：候选池**不写回** `symbols` / 行情 / 财报主表。
   成员表只存 `symbol_id` 关联 + 纳入校验摘要；名称/行业不回写主数据。

⚠️ 行业：首期仅读 `symbols.industry` **当前标签**，快照记录
   `industry_value` / `industry_source` / `industry_observed_at`，
   **严禁用于历史 PIT 或中性化**。历史行业能力需新增
   `security_industry_classifications`（含 effective_from/to/announced_at）→ 二期。
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class TrainingCandidatePool(Base):
    """候选池定义。

    `source_type`（`import` / `filter`）在创建后**不可切换**（需求 §3.1）。
    """

    __tablename__ = "training_candidate_pools"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    source_type: Mapped[str] = mapped_column(String(16))  # import / filter
    filter_config_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    rule_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    import_batch_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # draft / frozen / needs_recheck / invalidated
    status: Mapped[str] = mapped_column(String(24), default="draft", index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    member_count: Mapped[int] = mapped_column(Integer, default=0)

    created_by: Mapped[str] = mapped_column(String(64), default="local_user")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


class TrainingCandidatePoolMember(Base):
    """池-标的关联。只存 `symbol_id`，不复制主数据。"""

    __tablename__ = "training_candidate_pool_members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pool_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("training_candidate_pools.id", ondelete="CASCADE"), index=True
    )
    symbol_id: Mapped[int] = mapped_column(Integer, index=True)

    #: 纳入时的校验摘要（来源/纳入原因/匹配结果）
    inclusion_summary_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    included_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    #: 软删除：批量删除只断关联，不删 symbols/行情/财报；必须写审计
    is_deleted: Mapped[int] = mapped_column(Integer, default=0, index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("pool_id", "symbol_id", name="uq_tcpm_pool_symbol"),
    )


class TrainingCandidatePoolSnapshot(Base):
    """冻结快照 + 看板分析。

    挖掘只引用 `snapshot_id`，**永不实时重筛**；
    同一候选池多次挖掘各生成独立快照（需求 §3.8）。
    """

    __tablename__ = "training_candidate_pool_snapshots"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    pool_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("training_candidate_pools.id", ondelete="CASCADE"), index=True
    )

    #: 冻结成员（一次性载入内存分析表，避免逐行查主库）
    members_json: Mapped[str] = mapped_column(Text)
    rule_hash: Mapped[str] = mapped_column(String(64))
    data_cutoff_at: Mapped[datetime] = mapped_column(DateTime)
    stats_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: 看板：市值分布 / 行业分布 / 风格暴露 / 市场环境 / 数据质量 / 因子类型建议
    #: 作为 Prompt 变量传入 Step4 的 AI 生成（向导 §3.7.5）
    analysis_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: not_analyzed / analyzing / analyzed / reset
    analysis_status: Mapped[str] = mapped_column(String(16), default="not_analyzed", index=True)
    analyzed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    #: 分析完成后锁定筛选/导入/批量删除（向导 §3.7.3）
    is_locked: Mapped[int] = mapped_column(Integer, default=0)
    member_count: Mapped[int] = mapped_column(Integer, default=0)

    #: 行业快照（仅供当前标签，禁用于历史 PIT / 中性化）
    industry_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    industry_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    industry_observed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


__all__ = [
    "TrainingCandidatePool",
    "TrainingCandidatePoolMember",
    "TrainingCandidatePoolSnapshot",
]
