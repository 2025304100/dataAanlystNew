"""Factor mining core schema (M1a): candidate pools, mining runs/candidates/
generations, locks, formula templates, and the missing test-window columns.

Revision ID: wps_0023_051_factor_mining_core
Revises: wps_0023_050_factor_model_members

设计文档 §5.4 修订 0053。

幂等范式对齐现行 0052：
  - upgrade 用 sa.inspect().has_table / get_columns 短路
  - downgrade 逐个 drop_index（try/except）再 drop_table
  - 非空列一律给 server_default，避免既有行 ALTER 失败

⚠️ 三个「只在真实库里才会暴露」的坑（2026-09-16 在 MySQL gpfx 实测补齐，勿回退）：
  1. **必须显式 `mysql_engine="InnoDB"`**。本机 MySQL server 默认引擎是 MyISAM，
     不指定就建成 MyISAM。而 `task_locks` 的原子获取依赖「唯一约束 + 事务」，
     MyISAM 无事务 → 双锁失效；其余表也需 InnoDB（行锁 / 外键 / 崩溃恢复）。
     实测：不加此参数时 `init_db()` 会把 14 张表全部 CONVERT 一次才可用。
  2. **索引名必须与 ORM 一致**。ORM 里 `mapped_column(index=True)` 生成默认名
     `ix_<表名>_<列名>`；迁移若另起短名（如 `ix_fmr_status`）会**重复建两遍**
     （实测多出 20 个冗余索引）。故本文件不手写索引名，统一由 `_ORM_INDEXES` 驱动。
  3. **列必须与 ORM 完全对齐**。首版漏了 `training_candidate_pool_snapshots` 的
     3 个行业列（industry_value / industry_source / industry_observed_at），
     实测靠 `_auto_align_all_schema` 自动补上才没出事。

⚠️ `_auto_align_all_schema`（`app/db/init_db.py:1535`）会把上述缺陷**静默补齐**，
   从而掩盖迁移自身的错误。改完本文件后必须跑漂移检查，确认
   `新增表0 / 新增列0 / 新增索引0`。

落位前确认 alembic head 仍为 wps_0023_050_factor_model_members：
   `.venv/Scripts/python.exe -m alembic heads`
若 head 已前移，改本文件的 down_revision。
"""
from alembic import op
import sqlalchemy as sa


revision = "wps_0023_051_factor_mining_core"
down_revision = "wps_0023_050_factor_model_members"
branch_labels = None
depends_on = None

#: MySQL 建表统一参数（见文件头坑 #1）
MYSQL_KW = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}

#: 索引定义，由 ORM metadata 反推生成（2026-09-16）。**勿手改**；改 ORM 后重新生成。
#: 结构：表名 -> [(索引名, (列, ...), 是否唯一), ...]
_ORM_INDEXES: dict[str, list[tuple[str, tuple[str, ...], bool]]] = {
    "training_candidate_pools": [
        ("ix_training_candidate_pools_status", ("status",), False),
    ],
    "training_candidate_pool_members": [
        ("ix_training_candidate_pool_members_is_deleted", ("is_deleted",), False),
        ("ix_training_candidate_pool_members_pool_id", ("pool_id",), False),
        ("ix_training_candidate_pool_members_symbol_id", ("symbol_id",), False),
    ],
    "training_candidate_pool_snapshots": [
        ("ix_training_candidate_pool_snapshots_analysis_status", ("analysis_status",), False),
        ("ix_training_candidate_pool_snapshots_pool_id", ("pool_id",), False),
    ],
    "factor_mining_runs": [
        ("ix_factor_mining_runs_candidate_pool_snapshot_id", ("candidate_pool_snapshot_id",), False),
        ("ix_factor_mining_runs_status", ("status",), False),
        ("ix_mining_runs_status_created", ("status", "created_at"), False),
    ],
    "factor_mining_candidates": [
        ("ix_factor_mining_candidates_category", ("category",), False),
        ("ix_factor_mining_candidates_run_id", ("run_id",), False),
        ("ix_mining_candidate_run_gen", ("run_id", "generation"), False),
        ("ix_mining_candidate_run_rank", ("run_id", "generation_rank"), False),
    ],
    "factor_mining_generations": [
        ("ix_factor_mining_generations_run_id", ("run_id",), False),
    ],
    "factor_mining_prescreen_fingerprints": [
        ("ix_factor_mining_prescreen_fingerprints_candidate_id", ("candidate_id",), True),
    ],
    "factor_training_checkpoints": [
        ("ix_factor_training_checkpoints_run_id", ("run_id",), False),
    ],
    "factor_mining_drafts": [
        ("ix_factor_mining_drafts_status", ("status",), False),
    ],
    "factor_data_validation_runs": [
        ("ix_factor_data_validation_runs_draft_id", ("draft_id",), False),
        ("ix_factor_data_validation_runs_status", ("status",), False),
        ("ix_validation_draft_hash", ("draft_id", "config_hash", "status"), False),
    ],
    "factor_mining_templates": [],
    "factor_mining_template_versions": [
        ("ix_factor_mining_template_versions_template_id", ("template_id",), False),
    ],
    "task_locks": [
        ("ix_task_locks_owner_task_id", ("owner_task_id",), False),
    ],
    "factor_formula_templates": [
        ("ix_factor_formula_templates_category", ("category",), False),
        ("ix_factor_formula_templates_scope", ("scope",), False),
    ],
}

#: downgrade 顺序（= 建表顺序的逆序）
_ALL_TABLES: tuple[str, ...] = (
    "factor_formula_templates",
    "task_locks",
    "factor_mining_template_versions",
    "factor_mining_templates",
    "factor_data_validation_runs",
    "factor_mining_drafts",
    "factor_training_checkpoints",
    "factor_mining_prescreen_fingerprints",
    "factor_mining_generations",
    "factor_mining_candidates",
    "factor_mining_runs",
    "training_candidate_pool_snapshots",
    "training_candidate_pool_members",
    "training_candidate_pools",
)


def _create_indexes(table: str) -> None:
    """按 `_ORM_INDEXES` 建索引；已存在的跳过（幂等）。

    ⚠️ 必须**就地新建 inspector**。`sa.inspect()` 返回的是 schema 快照，
    若复用 `upgrade()` 开头创建的那个，此时该表尚未存在 → `has_table` 为 False
    → 静默跳过所有索引（实测：14 张表索引全丢）。
    """
    insp = sa.inspect(op.get_bind())
    if not insp.has_table(table):
        return
    existing = {i.get("name") for i in insp.get_indexes(table)}
    for name, cols, unique in _ORM_INDEXES.get(table, []):
        if name in existing:
            continue
        op.create_index(name, table, list(cols), unique=unique)


# ══════════════════════════════════════════════════════════
# 候选池域（training_candidate_pool*）
# ══════════════════════════════════════════════════════════


def _create_candidate_pools(insp) -> None:
    if not insp.has_table("training_candidate_pools"):
        op.create_table(
            "training_candidate_pools",
            sa.Column("id", sa.String(length=64), nullable=False),
            sa.Column("name", sa.String(length=128), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            # import / filter —— 创建后不可切换
            sa.Column("source_type", sa.String(length=16), nullable=False),
            sa.Column("filter_config_json", sa.Text(), nullable=True),
            sa.Column("rule_hash", sa.String(length=64), nullable=True),
            sa.Column("import_batch_id", sa.String(length=64), nullable=True),
            # draft / frozen / needs_recheck / invalidated
            sa.Column("status", sa.String(length=24), nullable=False, server_default="draft"),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("member_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_by", sa.String(length=64), nullable=False, server_default="local_user"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            **MYSQL_KW,
        )
    _create_indexes("training_candidate_pools")

    if not insp.has_table("training_candidate_pool_members"):
        op.create_table(
            "training_candidate_pool_members",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("pool_id", sa.String(length=64), nullable=False),
            sa.Column("symbol_id", sa.Integer(), nullable=False),
            sa.Column("inclusion_summary_json", sa.Text(), nullable=True),
            sa.Column("included_at", sa.DateTime(), nullable=False),
            # 软删除；批量删除只断关联，不删 symbols/行情/财报主数据
            sa.Column("is_deleted", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("deleted_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["pool_id"], ["training_candidate_pools.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("pool_id", "symbol_id", name="uq_tcpm_pool_symbol"),
            **MYSQL_KW,
        )
    _create_indexes("training_candidate_pool_members")

    if not insp.has_table("training_candidate_pool_snapshots"):
        op.create_table(
            "training_candidate_pool_snapshots",
            sa.Column("id", sa.String(length=64), nullable=False),
            sa.Column("pool_id", sa.String(length=64), nullable=False),
            sa.Column("members_json", sa.Text(), nullable=False),
            sa.Column("rule_hash", sa.String(length=64), nullable=False),
            sa.Column("data_cutoff_at", sa.DateTime(), nullable=False),
            sa.Column("stats_json", sa.Text(), nullable=True),
            # 看板：市值/行业/风格/市场环境/数据质量/因子类型建议
            sa.Column("analysis_json", sa.Text(), nullable=True),
            # not_analyzed / analyzing / analyzed / reset
            sa.Column("analysis_status", sa.String(length=16), nullable=False, server_default="not_analyzed"),
            sa.Column("analyzed_at", sa.DateTime(), nullable=True),
            sa.Column("is_locked", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("member_count", sa.Integer(), nullable=False, server_default="0"),
            # ── 行业快照（坑 #3：首版遗漏，务必保留）──
            # 仅记录当前标签，严禁用于历史 PIT 或行业内中性化
            sa.Column("industry_value", sa.Text(), nullable=True),
            sa.Column("industry_source", sa.String(length=32), nullable=True),
            sa.Column("industry_observed_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["pool_id"], ["training_candidate_pools.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            **MYSQL_KW,
        )
    _create_indexes("training_candidate_pool_snapshots")


# ══════════════════════════════════════════════════════════
# 挖掘域（factor_mining_*）
# ══════════════════════════════════════════════════════════


def _create_mining_runs(insp) -> None:
    if not insp.has_table("factor_mining_runs"):
        op.create_table(
            "factor_mining_runs",
            sa.Column("id", sa.String(length=64), nullable=False),
            sa.Column("status", sa.String(length=24), nullable=False, server_default="draft"),
            sa.Column("draft_id", sa.String(length=64), nullable=True),
            sa.Column("template_id", sa.String(length=64), nullable=True),
            sa.Column("template_version", sa.String(length=32), nullable=True),
            sa.Column("rule_hash", sa.String(length=64), nullable=True),
            sa.Column("candidate_pool_snapshot_id", sa.String(length=64), nullable=False),
            sa.Column("data_cutoff_at", sa.DateTime(), nullable=False),
            sa.Column("start_date", sa.DateTime(), nullable=False),
            sa.Column("end_date", sa.DateTime(), nullable=False),
            sa.Column("rebalance_frequency", sa.String(length=8), nullable=False, server_default="weekly"),
            sa.Column("risk_monitor_frequency", sa.String(length=16), nullable=True),
            sa.Column("risk_exit_config_json", sa.Text(), nullable=True),
            sa.Column("target_horizon", sa.Integer(), nullable=False, server_default="5"),
            sa.Column("split_method", sa.String(length=16), nullable=False, server_default="ratio"),
            sa.Column("split_config_json", sa.Text(), nullable=True),
            # ★ 归一化后的「调仓点数」，不是交易日数（设计文档 §7.2）
            sa.Column("purge_points", sa.Integer(), nullable=False, server_default="5"),
            sa.Column("embargo_points", sa.Integer(), nullable=False, server_default="5"),
            sa.Column("split_algorithm_version", sa.String(length=16), nullable=False, server_default="split-1.0.0"),
            sa.Column("filter_config_json", sa.Text(), nullable=True),
            sa.Column("evolution_params_json", sa.Text(), nullable=True),
            sa.Column("random_seed", sa.Integer(), nullable=False, server_default="42"),
            sa.Column("current_generation", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("max_generation", sa.Integer(), nullable=False, server_default="20"),
            sa.Column("converged", sa.Integer(), nullable=False, server_default="0"),
            # DSR 校正输入 + 多重检验次数唯一事实来源
            sa.Column("total_trials", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_by", sa.String(length=64), nullable=False, server_default="local_user"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("error_code", sa.String(length=64), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            **MYSQL_KW,
        )
    _create_indexes("factor_mining_runs")


def _create_mining_candidates(insp) -> None:
    if not insp.has_table("factor_mining_candidates"):
        op.create_table(
            "factor_mining_candidates",
            sa.Column("id", sa.String(length=64), nullable=False),
            sa.Column("run_id", sa.String(length=64), nullable=False),
            sa.Column("factor_code", sa.String(length=64), nullable=True),
            sa.Column("factor_version_id", sa.Integer(), nullable=True),
            sa.Column("formula_expr", sa.Text(), nullable=False),
            sa.Column("canonical_formula", sa.Text(), nullable=False),
            sa.Column("formula_hash", sa.String(length=64), nullable=False),
            sa.Column("execution_plan_json", sa.Text(), nullable=True),
            sa.Column("dependency_json", sa.Text(), nullable=True),
            sa.Column("generation", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("parent_ids_json", sa.Text(), nullable=True),
            sa.Column("operation", sa.String(length=16), nullable=False, server_default="enumerated"),
            sa.Column("generation_icir", sa.Float(), nullable=True),
            sa.Column("generation_coverage", sa.Float(), nullable=True),
            sa.Column("generation_turnover", sa.Float(), nullable=True),
            sa.Column("generation_complexity", sa.Integer(), nullable=True),
            sa.Column("category", sa.String(length=24), nullable=True),
            sa.Column("generation_rank", sa.Integer(), nullable=True),
            sa.Column("crowding_distance", sa.Float(), nullable=True),
            sa.Column("dedup_status", sa.String(length=24), nullable=True),
            sa.Column("review_status", sa.String(length=24), nullable=True),
            sa.Column("latest_evaluation_status", sa.String(length=24), nullable=True),
            sa.Column("latest_ic", sa.Float(), nullable=True),
            sa.Column("latest_evaluation_id", sa.String(length=64), nullable=True),
            sa.Column("elimination_status", sa.String(length=24), nullable=True),
            sa.Column("elimination_reason", sa.String(length=48), nullable=True),
            sa.Column("similar_to_candidate_id", sa.String(length=64), nullable=True),
            sa.Column("similar_to_factor_id", sa.Integer(), nullable=True),
            sa.Column("similarity_score", sa.Float(), nullable=True),
            # AI 强制输出 4 字段（缺一不入库）
            sa.Column("economic_logic", sa.Text(), nullable=True),
            sa.Column("expected_direction", sa.String(length=16), nullable=True),
            sa.Column("interpretability_score", sa.Float(), nullable=True),
            sa.Column("logic_source", sa.String(length=16), nullable=True),
            # 远期岛屿模型预留，本期恒 NULL
            sa.Column("island_id", sa.String(length=32), nullable=True),
            sa.Column("parent_island_id", sa.String(length=32), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["run_id"], ["factor_mining_runs.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("run_id", "formula_hash", name="uq_mining_candidate_hash"),
            **MYSQL_KW,
        )
    _create_indexes("factor_mining_candidates")


def _create_mining_generations(insp) -> None:
    if not insp.has_table("factor_mining_generations"):
        op.create_table(
            "factor_mining_generations",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("run_id", sa.String(length=64), nullable=False),
            sa.Column("generation", sa.Integer(), nullable=False),
            sa.Column("population_size", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("best_icir", sa.Float(), nullable=True),
            sa.Column("avg_icir", sa.Float(), nullable=True),
            sa.Column("median_icir", sa.Float(), nullable=True),
            sa.Column("diversity_score", sa.Float(), nullable=True),
            sa.Column("category_distribution_json", sa.Text(), nullable=True),
            sa.Column("category_evenness", sa.Float(), nullable=True),
            sa.Column("pareto_front_count", sa.Integer(), nullable=True),
            sa.Column("diversity_genotype", sa.Float(), nullable=True),
            sa.Column("diversity_phenotype", sa.Float(), nullable=True),
            sa.Column("diversity_health", sa.Float(), nullable=True),
            sa.Column("stall_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("convergence_delta", sa.Float(), nullable=True),
            sa.Column("actual_mutation_rate", sa.Float(), nullable=True),
            sa.Column("actual_crossover_rate", sa.Float(), nullable=True),
            sa.Column("actual_random_rate", sa.Float(), nullable=True),
            sa.Column("mutation_type_distribution_json", sa.Text(), nullable=True),
            sa.Column("cross_category_ratio", sa.Float(), nullable=True),
            sa.Column("adaptive_state", sa.String(length=24), nullable=True),
            sa.Column("elite_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("mutation_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("crossover_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("random_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("eliminated_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("evaluation_duration_ms", sa.Integer(), nullable=False, server_default="0"),
            # 性能探针（M1 必埋）
            sa.Column("probe_data_load_ms", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("probe_ast_eval_ms", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("probe_subexpr_compute_ms", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("probe_factor_assemble_ms", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("probe_metric_calc_ms", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("probe_db_write_ms", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("probe_subexpr_total", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("probe_subexpr_unique", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("probe_g2_hit_rate", sa.Float(), nullable=False, server_default="0"),
            # G2 抽样校验（每代必写；0 = 未通过，前端标红）
            sa.Column("cache_validation_passed", sa.Integer(), nullable=True),
            sa.Column("cache_validation_max_diff", sa.Float(), nullable=True),
            sa.Column("island_id", sa.String(length=32), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["run_id"], ["factor_mining_runs.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("run_id", "generation", name="uq_mining_generation"),
            **MYSQL_KW,
        )
    _create_indexes("factor_mining_generations")


def _create_mining_support(insp) -> None:
    if not insp.has_table("factor_mining_prescreen_fingerprints"):
        op.create_table(
            "factor_mining_prescreen_fingerprints",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("candidate_id", sa.String(length=64), nullable=False),
            sa.Column("semantic_category", sa.String(length=24), nullable=True),
            sa.Column("long_short_direction", sa.Integer(), nullable=True),
            sa.Column("ic_mean_short", sa.Float(), nullable=True),
            sa.Column("factor_std", sa.Float(), nullable=True),
            sa.Column("turnover_rate", sa.Float(), nullable=True),
            sa.Column("top_overlap_vector", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            **MYSQL_KW,
        )
        # 唯一性由 _ORM_INDEXES 的 unique 索引提供；**不再**另建 UniqueConstraint
        # （首版多建了 uq_prescreen_fingerprint_candidate，造成重复唯一约束）
    _create_indexes("factor_mining_prescreen_fingerprints")

    if not insp.has_table("factor_training_checkpoints"):
        op.create_table(
            "factor_training_checkpoints",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("run_id", sa.String(length=64), nullable=False),
            sa.Column("stage", sa.String(length=24), nullable=False),
            sa.Column("generation", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("input_snapshot_hash", sa.String(length=64), nullable=False),
            sa.Column("random_seed", sa.Integer(), nullable=False),
            sa.Column("result_location", sa.Text(), nullable=True),
            sa.Column("resumable", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("retention_deadline", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["run_id"], ["factor_mining_runs.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("run_id", "stage", "generation", name="uq_mining_checkpoint"),
            **MYSQL_KW,
        )
    _create_indexes("factor_training_checkpoints")

    if not insp.has_table("factor_mining_drafts"):
        op.create_table(
            "factor_mining_drafts",
            sa.Column("id", sa.String(length=64), nullable=False),
            sa.Column("name", sa.String(length=128), nullable=True),
            sa.Column("status", sa.String(length=32), nullable=False, server_default="draft"),
            sa.Column("current_step", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("step1_json", sa.Text(), nullable=True),
            sa.Column("step2_json", sa.Text(), nullable=True),
            sa.Column("step3_json", sa.Text(), nullable=True),
            sa.Column("step4_json", sa.Text(), nullable=True),
            sa.Column("candidate_pool_snapshot_id", sa.String(length=64), nullable=True),
            sa.Column("owner", sa.String(length=64), nullable=False, server_default="local_user"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            **MYSQL_KW,
        )
    _create_indexes("factor_mining_drafts")

    if not insp.has_table("factor_data_validation_runs"):
        op.create_table(
            "factor_data_validation_runs",
            sa.Column("id", sa.String(length=64), nullable=False),
            sa.Column("draft_id", sa.String(length=64), nullable=False),
            sa.Column("config_hash", sa.String(length=64), nullable=False),
            sa.Column("task_id", sa.String(length=64), nullable=True),
            sa.Column("status", sa.String(length=16), nullable=False, server_default="queued"),
            sa.Column("fields_json", sa.Text(), nullable=True),
            sa.Column("report_json", sa.Text(), nullable=True),
            sa.Column("blockers_json", sa.Text(), nullable=True),
            sa.Column("warnings_json", sa.Text(), nullable=True),
            sa.Column("total_shards", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("done_shards", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("shard_state_json", sa.Text(), nullable=True),
            sa.Column("expires_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            **MYSQL_KW,
        )
    _create_indexes("factor_data_validation_runs")

    if not insp.has_table("factor_mining_templates"):
        op.create_table(
            "factor_mining_templates",
            sa.Column("id", sa.String(length=64), nullable=False),
            sa.Column("name", sa.String(length=128), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("scope", sa.String(length=16), nullable=False, server_default="personal"),
            sa.Column("owner", sa.String(length=64), nullable=False, server_default="local_user"),
            sa.Column("enabled", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            **MYSQL_KW,
        )
    _create_indexes("factor_mining_templates")

    if not insp.has_table("factor_mining_template_versions"):
        op.create_table(
            "factor_mining_template_versions",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("template_id", sa.String(length=64), nullable=False),
            sa.Column("version", sa.String(length=32), nullable=False),
            sa.Column("change_note", sa.Text(), nullable=True),
            sa.Column("rule_config_json", sa.Text(), nullable=True),
            sa.Column("rule_hash", sa.String(length=64), nullable=False),
            sa.Column("created_by", sa.String(length=64), nullable=False, server_default="local_user"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["template_id"], ["factor_mining_templates.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("template_id", "version", name="uq_mining_template_version"),
            **MYSQL_KW,
        )
    _create_indexes("factor_mining_template_versions")


# ══════════════════════════════════════════════════════════
# 平台：双锁 + 公式模板
# ══════════════════════════════════════════════════════════


def _create_platform_tables(insp) -> None:
    if not insp.has_table("task_locks"):
        # ⚠️ 必须 InnoDB：双锁的原子获取依赖「唯一约束 + 事务」（见文件头坑 #1）
        op.create_table(
            "task_locks",
            sa.Column("lock_key", sa.String(length=32), nullable=False),
            sa.Column("owner_task_id", sa.String(length=64), nullable=False),
            sa.Column("owner_run_id", sa.String(length=64), nullable=True),
            sa.Column("acquired_at", sa.DateTime(), nullable=False),
            sa.Column("heartbeat_at", sa.DateTime(), nullable=False),
            sa.Column("queue_json", sa.Text(), nullable=True),
            sa.PrimaryKeyConstraint("lock_key"),
            **MYSQL_KW,
        )
    _create_indexes("task_locks")

    if not insp.has_table("factor_formula_templates"):
        op.create_table(
            "factor_formula_templates",
            sa.Column("id", sa.String(length=64), nullable=False),
            sa.Column("name", sa.String(length=128), nullable=False),
            sa.Column("category", sa.String(length=24), nullable=False),
            sa.Column("formula_template", sa.Text(), nullable=False),
            sa.Column("param_definitions_json", sa.Text(), nullable=True),
            sa.Column("economic_logic", sa.Text(), nullable=True),
            sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
            sa.Column("scope", sa.String(length=16), nullable=False, server_default="system"),
            sa.Column("source", sa.String(length=24), nullable=True),
            sa.Column("complexity", sa.Integer(), nullable=True),
            sa.Column("enabled", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            **MYSQL_KW,
        )
    _create_indexes("factor_formula_templates")


# ══════════════════════════════════════════════════════════
# 复用表迁移（最小侵入）
# ══════════════════════════════════════════════════════════


def _alter_factor_evaluation_runs(insp) -> None:
    """TimeSplit 已算出 test_start/test_end，但表中缺列（开发文档 §3.3）。

    历史记录留 NULL 即可，无需回填。
    """
    if not insp.has_table("factor_evaluation_runs"):
        return
    cols = {c["name"] for c in insp.get_columns("factor_evaluation_runs")}
    if "test_start_date" not in cols:
        op.add_column(
            "factor_evaluation_runs",
            sa.Column("test_start_date", sa.DateTime(), nullable=True),
        )
    if "test_end_date" not in cols:
        op.add_column(
            "factor_evaluation_runs",
            sa.Column("test_end_date", sa.DateTime(), nullable=True),
        )


# ══════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    _create_candidate_pools(insp)
    _create_mining_runs(insp)
    _create_mining_candidates(insp)
    _create_mining_generations(insp)
    _create_mining_support(insp)
    _create_platform_tables(insp)
    _alter_factor_evaluation_runs(insp)


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    # factor_evaluation_runs：只回滚本修订新增的列
    if insp.has_table("factor_evaluation_runs"):
        cols = {c["name"] for c in insp.get_columns("factor_evaluation_runs")}
        for col in ("test_end_date", "test_start_date"):
            if col in cols:
                op.drop_column("factor_evaluation_runs", col)

    for table in _ALL_TABLES:
        _drop_table_with_indexes(table, insp)


def _drop_table_with_indexes(table: str, insp) -> None:
    if not insp.has_table(table):
        return
    existing = {i.get("name") for i in insp.get_indexes(table)}
    for name, _cols, _unique in _ORM_INDEXES.get(table, []):
        if name not in existing:
            continue
        try:
            op.drop_index(name, table_name=table)
        except Exception:
            pass
    op.drop_table(table)
