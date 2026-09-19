"""P2：因子域治理 ORM（5 张新表）
- factor_set_snapshots  : 训练时刻因子集只读快照（防止改了因子集查不到当时配置）
- factor_model_members  : 模型→因子关联物化表（避免每次 parse hyperparameters_json，UI/回测直接 join）
- factor_drafts         : 自定义指标 → 因子 Draft→Review→Applied 状态机
- factor_quality_daily  : 日级因子质量快照（覆盖率/IC/IR/换手率/自相关/样本数）
- scoring_lineage       : 每次打分批次的数据血缘（Score batch → 哪个模型、哪个 FactorSet 快照、哪些因子版本）

注意：
- JSON 字段使用 Text+default="{}"（避免 MySQL 旧版本 JSON 类型问题，与项目全局约定一致）
- FactorSet/FactorModelRun 用 String(64) 主键（与 factor_sets / factor_model_runs 表一致）
- 外部通过 Base.metadata 被 init_db.align_model_schema() 自动扫描注册（app/models/__init__.py 有 pkgutil 兜底）
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import (
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ════════════════════════════════════════════════════════════════
# 1. 因子集快照：每次训练/打分时把"当时的 FactorSet 成员"固化一条，
#    保证后续 G5 对账/追溯时不会因为 FactorSet 被修改而失真。
# ════════════════════════════════════════════════════════════════
class FactorSetSnapshot(Base):
    __tablename__ = "factor_set_snapshots"
    __table_args__ = (
        UniqueConstraint("factor_set_id", "hash", name="uq_factor_set_snapshot_unique"),
        Index("ix_fss_factor_set_id_created", "factor_set_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 原始因子集 ID（FK factor_sets.id），注意：因子集被删除后快照仍保留，所以 SET NULL
    factor_set_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("factor_sets.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # 捕获时的因子集版本（如果有），否则 0
    factor_set_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # factor_code → {"version": int, "role": "feature|target|regime", "weight_in_set": float|null}
    member_snapshot_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    # 排序后拼接的 factor_code:version 的 sha256 前 16 位（便于去重）
    hash: Mapped[str] = mapped_column(String(64), default="", nullable=False, index=True)
    # 手动 / training / scoring / shadow
    created_via: Mapped[str] = mapped_column(String(32), default="training", nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow_naive, nullable=False, index=True)


# ════════════════════════════════════════════════════════════════
# 2. 模型-因子 物化成员：训练完成后写入，避免解析 hyperparameters_json 字符串
#    （回退：若 members 表为空，facade.get_model_detail 仍可回退到 parse JSON）
# ════════════════════════════════════════════════════════════════
class FactorModelMember(Base):
    __tablename__ = "factor_model_members"
    __table_args__ = (
        UniqueConstraint("model_run_id", "factor_code", name="uq_factor_model_member"),
        Index("ix_fmm_factor_code", "factor_code"),
        Index("ix_fmm_side", "side"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    model_run_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("factor_model_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    factor_code: Mapped[str] = mapped_column(String(64), nullable=False)
    factor_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    # 原始回归系数
    coefficient: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    # Σ|w|=1 归一化后的权重（展示用）
    normalized_weight: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    # 训练窗口内这个因子的表现（可用于对比面板）
    train_ic: Mapped[float | None] = mapped_column(Float, nullable=True)
    validation_ic: Mapped[float | None] = mapped_column(Float, nullable=True)
    coverage: Mapped[float | None] = mapped_column(Float, nullable=True)
    # long / short / neutral（与 ScoringModelFactorMember.side 对齐）
    side: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow_naive, nullable=False)


# ════════════════════════════════════════════════════════════════
# 3. 因子草稿：自定义指标 → 提升为因子 的中间治理状态
#    P0 facade submit_factor_draft_from_external 已收口入口，这里落表物化
# ════════════════════════════════════════════════════════════════
class FactorDraft(Base):
    __tablename__ = "factor_drafts"
    __table_args__ = (
        Index("ix_factor_drafts_status", "review_status", "submitted_at"),
        Index("ix_factor_drafts_source", "source_module", "source_ref_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 草稿来源：custom_indicators / ai_assisted / imported / manual
    source_module: Mapped[str] = mapped_column(String(64), default="custom_indicators", nullable=False)
    # 来源模块中的原始对象 ID（例如 custom_indicators.id），允许 None
    source_ref_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # 草稿编号（对外展示，避免暴露自增 ID），格式 "fd-<shortid>"
    draft_no: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    # 建议的 factor_code
    suggested_code: Mapped[str] = mapped_column(String(64), nullable=False)
    suggested_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    suggested_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 草稿详情：{formula_expr, params, source_mapping, description, thesis, direction, ...}
    payload_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    # 状态机：submitted → approved / rejected → applied（若通过后 promote 成功则 applied）
    review_status: Mapped[str] = mapped_column(String(24), default="submitted", nullable=False, index=True)
    submitted_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reviewer: Mapped[str | None] = mapped_column(String(128), nullable=True)
    review_msg: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 审批后产生的 factor_id / version（applied 之后回填）
    promoted_factor_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("factors.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    promoted_factor_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    submitted_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow_naive, nullable=False, index=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow_naive, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow_naive, onupdate=_utcnow_naive, nullable=False)


# ════════════════════════════════════════════════════════════════
# 4. 因子日度质量快照：预计算覆盖率/IC/IR/换手率/自相关/样本数
#    供：准入门1（训练准入）、G6 灰度前健康检查、因子中心摘要。
# ════════════════════════════════════════════════════════════════
class FactorQualityDaily(Base):
    __tablename__ = "factor_quality_daily"
    __table_args__ = (
        UniqueConstraint("trade_date", "factor_code", name="uq_factor_quality_daily"),
        Index("ix_fqd_factor_code_date", "factor_code", "trade_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    factor_code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # 该交易日该因子的覆盖率（有效样本 / 全样本）
    coverage: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 该因子该日截面上的 IC（和 next_{n}_return 相关，n 通常 5 日）
    ic_mean: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 20 日滚动 ICIR = 均值 / std（std=0 → NULL）
    ir_20d: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 换手率：该因子截面 top 排序组合前后一期的变动比例
    turnover_20d: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 20 日自相关系数
    autocorr_20d: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 该日纳入计算的股票数
    n_stocks: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 原始细节 JSON：{ic_decay:[1d,5d,10d], deciles_return:[...], missing_count}
    detail_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow_naive, nullable=False)


# ════════════════════════════════════════════════════════════════
# 5. 评分血缘：每次打分批次（calc_batch_id + trade_date 维度）记录一次，
#    解决 G5 双跑对账"这次打分到底用了哪个模型/哪个因子集快照"的溯源问题。
# ════════════════════════════════════════════════════════════════
class ScoringLineage(Base):
    __tablename__ = "scoring_lineage"
    __table_args__ = (
        UniqueConstraint("score_batch_id", "score_trade_date", "weight_mode", "model_run_id", name="uq_scoring_lineage"),
        Index("ix_sl_model_date", "model_run_id", "score_trade_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    score_batch_id: Mapped[str] = mapped_column(String(64), default="", nullable=False, index=True)
    score_trade_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    # manual / ridge / shadow / ensemble
    weight_mode: Mapped[str] = mapped_column(String(16), default="manual", nullable=False, index=True)
    # 可能 None（manual 模式）
    model_run_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("factor_model_runs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # 对应 FactorSetSnapshot.id
    factor_set_snapshot_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("factor_set_snapshots.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # 该次打分会话中，每个实际使用到的因子 → version 映射快照
    # {"turnover_z20": {"version": 1, "used": true, "avg_missing": 0.12}, ...}
    factor_version_ids_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    # 评分批次统计：n_scores / 打分耗时 / 异常数
    stats_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    # shadow / g5_dual / g6_grayscale / production
    run_mode: Mapped[str] = mapped_column(String(32), default="production", nullable=False)
    actor: Mapped[str | None] = mapped_column(String(128), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow_naive, nullable=False, index=True)
