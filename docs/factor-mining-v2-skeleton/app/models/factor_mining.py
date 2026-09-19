"""因子挖掘域 ORM 模型（设计文档 §5.1 第 4~7、10 项）。

⚠️ 落位提醒：本文件一旦放入 `app/models/`，`app/models/__init__.py:84` 的
   `_auto_discover_models()` 会在下次启动时自动导入它，随后
   `app/db/init_db.py:1535 _auto_align_all_schema()` 会**在用户库中创建下列表**。
   请优先通过 alembic 修订 0053 建表（设计文档 §5.4），本文件只承担 metadata 注册。

表清单：
  factor_mining_runs                   批次（枢纽表）
  factor_mining_candidates             候选个体
  factor_mining_generations            每代汇总 + 性能探针 + 缓存校验
  factor_mining_prescreen_fingerprints 预筛三层指纹
  factor_training_checkpoints          断点续跑
  factor_mining_drafts                 向导草稿
  factor_data_validation_runs          字段异步校验
  factor_mining_templates              实验模板
  factor_mining_template_versions      实验模板版本
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
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


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ══════════════════════════════════════════════════════════
# 1. 批次（枢纽表）
# ══════════════════════════════════════════════════════════


class FactorMiningRun(Base):
    __tablename__ = "factor_mining_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)

    # draft/queued/running/paused/cancel_requested/validating
    # succeeded/failed/cancelled/converged/invalidated
    status: Mapped[str] = mapped_column(String(24), index=True, default="draft")

    # ── 复现所需的模板绑定 ──
    draft_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    template_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    template_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    rule_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # ── 快照与时间边界 ──
    # 必须存 snapshot_id，不能只存 pool_id（需求 §3.8）
    candidate_pool_snapshot_id: Mapped[str] = mapped_column(String(64), index=True)
    data_cutoff_at: Mapped[datetime] = mapped_column(DateTime)
    start_date: Mapped[datetime] = mapped_column(DateTime)
    end_date: Mapped[datetime] = mapped_column(DateTime)

    # ── 目标与频率 ──
    rebalance_frequency: Mapped[str] = mapped_column(String(8), default="weekly")
    risk_monitor_frequency: Mapped[str | None] = mapped_column(String(16), nullable=True)
    risk_exit_config_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_horizon: Mapped[int] = mapped_column(Integer, default=5)

    # ── 切分（★ purge/embargo 为归一化后的「调仓点数」，设计文档 §7.2） ──
    split_method: Mapped[str] = mapped_column(String(16), default="ratio")
    split_config_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    purge_points: Mapped[int] = mapped_column(Integer, default=5)
    embargo_points: Mapped[int] = mapped_column(Integer, default=5)
    split_algorithm_version: Mapped[str] = mapped_column(String(16), default="split-1.0.0")

    # ── 冻结配置 ──
    filter_config_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    evolution_params_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    random_seed: Mapped[int] = mapped_column(Integer, default=42)

    # ── 进度 ──
    current_generation: Mapped[int] = mapped_column(Integer, default=0)
    max_generation: Mapped[int] = mapped_column(Integer, default=20)
    converged: Mapped[int] = mapped_column(Integer, default=0)

    # ── DSR 校正输入 + 多重检验次数唯一事实来源（设计文档 §5.2） ──
    total_trials: Mapped[int] = mapped_column(Integer, default=0)

    created_by: Mapped[str] = mapped_column(String(64), default="local_user")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        Index("ix_mining_runs_status_created", "status", "created_at"),
    )


# ══════════════════════════════════════════════════════════
# 2. 候选个体
# ══════════════════════════════════════════════════════════


class FactorMiningCandidate(Base):
    __tablename__ = "factor_mining_candidates"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("factor_mining_runs.id", ondelete="CASCADE"), index=True
    )

    factor_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    factor_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # ── 公式与执行计划 ──
    formula_expr: Mapped[str] = mapped_column(Text)
    canonical_formula: Mapped[str] = mapped_column(Text)
    formula_hash: Mapped[str] = mapped_column(String(64))
    execution_plan_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    dependency_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── 血缘 ──
    generation: Mapped[int] = mapped_column(Integer, default=0)  # 0 = 初始种群
    parent_ids_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # elite/mutation/crossover/random/ai_generated/enumerated
    operation: Mapped[str] = mapped_column(String(16), default="enumerated")

    # ── 当代指标 ──
    generation_icir: Mapped[float | None] = mapped_column(Float, nullable=True)
    generation_coverage: Mapped[float | None] = mapped_column(Float, nullable=True)
    generation_turnover: Mapped[float | None] = mapped_column(Float, nullable=True)
    generation_complexity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    category: Mapped[str | None] = mapped_column(String(24), index=True, nullable=True)
    generation_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    crowding_distance: Mapped[float | None] = mapped_column(Float, nullable=True)

    # ── 状态与淘汰 ──
    dedup_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    review_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    latest_evaluation_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    latest_ic: Mapped[float | None] = mapped_column(Float, nullable=True)
    latest_evaluation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    elimination_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    elimination_reason: Mapped[str | None] = mapped_column(String(48), nullable=True)
    similar_to_candidate_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    similar_to_factor_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    similarity_score: Mapped[float | None] = mapped_column(Float, nullable=True)

    # ── AI 强制输出 4 字段（设计文档 §7.5，缺一不入库） ──
    economic_logic: Mapped[str | None] = mapped_column(Text, nullable=True)
    expected_direction: Mapped[str | None] = mapped_column(String(16), nullable=True)
    interpretability_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    logic_source: Mapped[str | None] = mapped_column(String(16), nullable=True)  # ai/template/manual

    # ── 远期岛屿模型预留（本期恒 NULL，设计文档 §7.7） ──
    island_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    parent_island_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    __table_args__ = (
        UniqueConstraint("run_id", "formula_hash", name="uq_mining_candidate_hash"),
        Index("ix_mining_candidate_run_gen", "run_id", "generation"),
        Index("ix_mining_candidate_run_rank", "run_id", "generation_rank"),
    )


# ══════════════════════════════════════════════════════════
# 3. 每代汇总（含性能探针 + G2 缓存校验）
# ══════════════════════════════════════════════════════════


class FactorMiningGeneration(Base):
    __tablename__ = "factor_mining_generations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("factor_mining_runs.id", ondelete="CASCADE"), index=True
    )
    generation: Mapped[int] = mapped_column(Integer)

    population_size: Mapped[int] = mapped_column(Integer, default=0)
    best_icir: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_icir: Mapped[float | None] = mapped_column(Float, nullable=True)
    median_icir: Mapped[float | None] = mapped_column(Float, nullable=True)

    # ── D3 多样性三层 ──
    diversity_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    category_distribution_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    category_evenness: Mapped[float | None] = mapped_column(Float, nullable=True)
    pareto_front_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    diversity_genotype: Mapped[float | None] = mapped_column(Float, nullable=True)
    diversity_phenotype: Mapped[float | None] = mapped_column(Float, nullable=True)
    diversity_health: Mapped[float | None] = mapped_column(Float, nullable=True)

    # ── D1 停滞 ──
    stall_count: Mapped[int] = mapped_column(Integer, default=0)
    convergence_delta: Mapped[float | None] = mapped_column(Float, nullable=True)

    # ── C1 实际策略（可复现） ──
    actual_mutation_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    actual_crossover_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    actual_random_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    mutation_type_distribution_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    cross_category_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    adaptive_state: Mapped[str | None] = mapped_column(String(24), nullable=True)

    # ── 计数 ──
    elite_count: Mapped[int] = mapped_column(Integer, default=0)
    mutation_count: Mapped[int] = mapped_column(Integer, default=0)
    crossover_count: Mapped[int] = mapped_column(Integer, default=0)
    random_count: Mapped[int] = mapped_column(Integer, default=0)
    eliminated_count: Mapped[int] = mapped_column(Integer, default=0)
    evaluation_duration_ms: Mapped[int] = mapped_column(Integer, default=0)

    # ── 性能探针（M1 必埋，为 M3 的 G1 决策提供依据） ──
    probe_data_load_ms: Mapped[int] = mapped_column(Integer, default=0)
    probe_ast_eval_ms: Mapped[int] = mapped_column(Integer, default=0)
    probe_subexpr_compute_ms: Mapped[int] = mapped_column(Integer, default=0)
    probe_factor_assemble_ms: Mapped[int] = mapped_column(Integer, default=0)
    probe_metric_calc_ms: Mapped[int] = mapped_column(Integer, default=0)
    probe_db_write_ms: Mapped[int] = mapped_column(Integer, default=0)
    probe_subexpr_total: Mapped[int] = mapped_column(Integer, default=0)
    probe_subexpr_unique: Mapped[int] = mapped_column(Integer, default=0)
    probe_g2_hit_rate: Mapped[float] = mapped_column(Float, default=0.0)

    # ── G2 抽样校验（每代必写；0 = 未通过，需前端标红） ──
    cache_validation_passed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_validation_max_diff: Mapped[float | None] = mapped_column(Float, nullable=True)

    island_id: Mapped[str | None] = mapped_column(String(32), nullable=True)  # 远期预留
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    __table_args__ = (
        UniqueConstraint("run_id", "generation", name="uq_mining_generation"),
    )


# ══════════════════════════════════════════════════════════
# 4. 预筛三层指纹
# ══════════════════════════════════════════════════════════


class FactorMiningPrescreenFingerprint(Base):
    __tablename__ = "factor_mining_prescreen_fingerprints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    candidate_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    # 第 1 层：语义类别（由 candidate.category 承载，此处冗余便于单表查询）
    semantic_category: Mapped[str | None] = mapped_column(String(24), nullable=True)
    # 第 2 层：统计指纹（任意 3 项差异超阈值即判定不相似）
    long_short_direction: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ic_mean_short: Mapped[float | None] = mapped_column(Float, nullable=True)
    factor_std: Mapped[float | None] = mapped_column(Float, nullable=True)
    turnover_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 第 3 层：Top20% 成分 MinHash 签名
    top_overlap_vector: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


# ══════════════════════════════════════════════════════════
# 5. 断点
# ══════════════════════════════════════════════════════════


class FactorTrainingCheckpoint(Base):
    __tablename__ = "factor_training_checkpoints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("factor_mining_runs.id", ondelete="CASCADE"), index=True
    )
    stage: Mapped[str] = mapped_column(String(24))  # data_prep/initial_pop/evolution/final_eval
    generation: Mapped[int] = mapped_column(Integer, default=0)
    input_snapshot_hash: Mapped[str] = mapped_column(String(64))
    random_seed: Mapped[int] = mapped_column(Integer)
    result_location: Mapped[str | None] = mapped_column(Text, nullable=True)
    resumable: Mapped[int] = mapped_column(Integer, default=1)
    retention_deadline: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    __table_args__ = (
        UniqueConstraint("run_id", "stage", "generation", name="uq_mining_checkpoint"),
    )


# ══════════════════════════════════════════════════════════
# 6. 向导草稿
# ══════════════════════════════════════════════════════════


class FactorMiningDraft(Base):
    __tablename__ = "factor_mining_drafts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # draft / waiting_data_recheck / invalidated
    status: Mapped[str] = mapped_column(String(32), default="draft", index=True)
    current_step: Mapped[int] = mapped_column(Integer, default=1)
    step1_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    step2_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    step3_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    step4_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    candidate_pool_snapshot_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    owner: Mapped[str] = mapped_column(String(64), default="local_user")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


# ══════════════════════════════════════════════════════════
# 7. 字段异步校验
# ══════════════════════════════════════════════════════════


class FactorDataValidationRun(Base):
    __tablename__ = "factor_data_validation_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    draft_id: Mapped[str] = mapped_column(String(64), index=True)
    # 同一 draft_id + config_hash 的活动任务唯一
    config_hash: Mapped[str] = mapped_column(String(64))
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # queued/running/passed/blocked/warning/failed
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    fields_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    report_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    blockers_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    warnings_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── 分片进度（支持断点续跑） ──
    total_shards: Mapped[int] = mapped_column(Integer, default=0)
    done_shards: Mapped[int] = mapped_column(Integer, default=0)
    shard_state_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # 默认 24h
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)

    __table_args__ = (
        Index("ix_validation_draft_hash", "draft_id", "config_hash", "status"),
    )


# ══════════════════════════════════════════════════════════
# 8/9. 实验模板 + 版本
# ══════════════════════════════════════════════════════════


class FactorMiningTemplate(Base):
    __tablename__ = "factor_mining_templates"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    scope: Mapped[str] = mapped_column(String(16), default="personal")  # system/personal
    owner: Mapped[str] = mapped_column(String(64), default="local_user")
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


class FactorMiningTemplateVersion(Base):
    __tablename__ = "factor_mining_template_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    template_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("factor_mining_templates.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[str] = mapped_column(String(32))
    change_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    rule_config_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 与模板版本绑定，改模板不影响历史任务
    rule_hash: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(64), default="local_user")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    __table_args__ = (
        UniqueConstraint("template_id", "version", name="uq_mining_template_version"),
    )


__all__ = [
    "FactorMiningRun",
    "FactorMiningCandidate",
    "FactorMiningGeneration",
    "FactorMiningPrescreenFingerprint",
    "FactorTrainingCheckpoint",
    "FactorMiningDraft",
    "FactorDataValidationRun",
    "FactorMiningTemplate",
    "FactorMiningTemplateVersion",
]
