"""Factor mining core schema (M1a): candidate pools, mining runs/candidates/
generations, locks, formula templates, and the missing test-window columns.

Revision ID: wps_0023_051_factor_mining_core
Revises: wps_0023_050_factor_model_members

设计文档 §5.4 修订 0053。幂等范式对齐现行 0052：
  - upgrade 用 sa.inspect().has_table / get_columns 短路
  - downgrade 逐个 drop_index（try/except）再 drop_table
  - 非空列一律给 server_default，避免既有行 ALTER 失败

⚠️ 落位前确认 alembic head 仍为 wps_0023_050_factor_model_members：
   `.venv/Scripts/python.exe -m alembic heads`
   若 head 已前移，改本文件的 down_revision。
"""
from alembic import op
import sqlalchemy as sa


revision = "wps_0023_051_factor_mining_core"
down_revision = "wps_0023_050_factor_model_members"
branch_labels = None
depends_on = None


# ══════════════════════════════════════════════════════════
# 候选池域（training_candidate_pool*，设计文档 §5.1 第 1~3 项）
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
        )
        op.create_index("ix_tcp_status", "training_candidate_pools", ["status"])

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
        )
        op.create_index("ix_tcpm_pool_id", "training_candidate_pool_members", ["pool_id"])
        op.create_index("ix_tcpm_symbol_id", "training_candidate_pool_members", ["symbol_id"])

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
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["pool_id"], ["training_candidate_pools.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_tcps_pool_id", "training_candidate_pool_snapshots", ["pool_id"])
        op.create_index("ix_tcps_status", "training_candidate_pool_snapshots", ["analysis_status"])


# ══════════════════════════════════════════════════════════
# 挖掘域（factor_mining_*，设计文档 §5.1 第 4~11 项）
# ══════════════════════════════════════════════════════════


def _create_mining_runs(insp) -> None:
    if insp.has_table("factor_mining_runs"):
        return
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
    )
    op.create_index("ix_fmr_status", "factor_mining_runs", ["status"])
    op.create_index("ix_fmr_snapshot", "factor_mining_runs", ["candidate_pool_snapshot_id"])
    op.create_index("ix_fmr_status_created", "factor_mining_runs", ["status", "created_at"])


def _create_mining_candidates(insp) -> None:
    if insp.has_table("factor_mining_candidates"):
        return
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
    )
    op.create_index("ix_fmc_run_id", "factor_mining_candidates", ["run_id"])
    op.create_index("ix_fmc_run_gen", "factor_mining_candidates", ["run_id", "generation"])
    op.create_index("ix_fmc_run_rank", "factor_mining_candidates", ["run_id", "generation_rank"])
    op.create_index("ix_fmc_category", "factor_mining_candidates", ["category"])


def _create_mining_generations(insp) -> None:
    if insp.has_table("factor_mining_generations"):
        return
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
    )


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
            sa.UniqueConstraint("candidate_id", name="uq_prescreen_fingerprint_candidate"),
        )

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
        )

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
        )
        op.create_index("ix_fmd_status", "factor_mining_drafts", ["status"])

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
        )
        op.create_index("ix_fdvr_draft_id", "factor_data_validation_runs", ["draft_id"])
        op.create_index("ix_fdvr_draft_hash", "factor_data_validation_runs", ["draft_id", "config_hash", "status"])

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
        )

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
        )
        op.create_index("ix_fmtv_template_id", "factor_mining_template_versions", ["template_id"])


# ══════════════════════════════════════════════════════════
# 平台：双锁 + 公式模板
# ══════════════════════════════════════════════════════════


def _create_platform_tables(insp) -> None:
    if not insp.has_table("task_locks"):
        op.create_table(
            "task_locks",
            sa.Column("lock_key", sa.String(length=32), nullable=False),
            sa.Column("owner_task_id", sa.String(length=64), nullable=False),
            sa.Column("owner_run_id", sa.String(length=64), nullable=True),
            sa.Column("acquired_at", sa.DateTime(), nullable=False),
            sa.Column("heartbeat_at", sa.DateTime(), nullable=False),
            sa.Column("queue_json", sa.Text(), nullable=True),
            sa.PrimaryKeyConstraint("lock_key"),
        )
        op.create_index("ix_task_locks_owner", "task_locks", ["owner_task_id"])

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
        )
        op.create_index("ix_fft_category", "factor_formula_templates", ["category"])
        op.create_index("ix_fft_scope", "factor_formula_templates", ["scope"])


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

    _drop_table_with_indexes(
        "factor_formula_templates", ("ix_fft_scope", "ix_fft_category")
    )
    _drop_table_with_indexes("task_locks", ("ix_task_locks_owner",))
    _drop_table_with_indexes(
        "factor_mining_template_versions", ("ix_fmtv_template_id",)
    )
    _drop_table_with_indexes("factor_mining_templates", ())
    _drop_table_with_indexes(
        "factor_data_validation_runs", ("ix_fdvr_draft_hash", "ix_fdvr_draft_id")
    )
    _drop_table_with_indexes("factor_mining_drafts", ("ix_fmd_status",))
    _drop_table_with_indexes("factor_training_checkpoints", ())
    _drop_table_with_indexes("factor_mining_prescreen_fingerprints", ())
    _drop_table_with_indexes("factor_mining_generations", ())
    _drop_table_with_indexes(
        "factor_mining_candidates",
        ("ix_fmc_category", "ix_fmc_run_rank", "ix_fmc_run_gen", "ix_fmc_run_id"),
    )
    _drop_table_with_indexes(
        "factor_mining_runs", ("ix_fmr_status_created", "ix_fmr_snapshot", "ix_fmr_status")
    )
    _drop_table_with_indexes("training_candidate_pool_snapshots", ("ix_tcps_status", "ix_tcps_pool_id"))
    _drop_table_with_indexes("training_candidate_pool_members", ("ix_tcpm_symbol_id", "ix_tcpm_pool_id"))
    _drop_table_with_indexes("training_candidate_pools", ("ix_tcp_status",))


def _drop_table_with_indexes(table: str, indexes: tuple[str, ...]) -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(table):
        return
    for name in indexes:
        try:
            op.drop_index(name, table_name=table)
        except Exception:
            pass
    op.drop_table(table)
