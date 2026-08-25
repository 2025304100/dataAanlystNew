# ===========================================================================
#  G1-WP0-3 补充迁移(3): decision_runs / decision_evidence 全量扩展列补齐
# ===========================================================================
#  Purpose: DecisionRun / DecisionEvidence ORM 中声明了大量 Q2/Q6/Q7
#  契约列，但 migration 0024_decision_engine_contract 只建了最小集合。
#  本迁移一次性补齐所有缺失列，保证 8 tests 和后续 WP0-4/5 不用再 chase column。
#
#  新增列清单（决策表）:
#    - summary / checks_json / result_hash
#    - universe_count / member_count
#    - score_count_expected / score_count_actual / score_coverage_pct / score_max_age_days
#    - blocking_status / blocking_reasons_json
#    - versions_json
#    - started_at / finished_at / duration_ms
#    - is_result_production_eligible / match_mode
#    - correlation_id / task_id
#
#  新增列清单（证据表）:
#    - action_subtype
#    - target_position_pct / min_lot_size / target_quantity / target_qty_delta
#    - intended_price / executed_price / slippage_bps
#    - rejection_reason / rejection_detail / blocking_reason
#    - roll_forward_days / rejections_trace_json
#    - exit_rules_hit_json
#    - score_id / score_value / score_rank / score_published_at / pit_safe_flag
#    - constraints_json / versions_json / reason_codes_json / factor_contributions_json
#    - legacy_fallback_flag / stop_loss_verified_price_source / stop_loss_triggered
#    - match_mode / manual_price_flag
#    - idempotency_key
# ===========================================================================
"""wps_0023_031_wp03_decision_runs_evidence_full_cols.

Revision ID: wps_0023_031_wp03_decision_runs_evidence_full_cols
Revises: wps_0023_030_wp03_manual_price_overrides
Create Date: 2026-08-19 12:00:00.000000
"""
from alembic import op
from sqlalchemy import (
    CheckConstraint, Column, Date, DateTime, Float, ForeignKey, Index, Integer,
    String, Text, inspect,
)


revision = 'wps_0023_031_wp03_decision_runs_evidence_full_cols'
down_revision = 'wps_0023_030_wp03_manual_price_overrides'
branch_labels = None
depends_on = None


def _column_exists(table_name: str, column_name: str) -> bool:
    conn = op.get_bind()
    inspector = inspect(conn)
    columns = {c["name"] for c in inspector.get_columns(table_name)}
    return column_name in columns


def _drop_indexes_referencing_columns(table_name: str, column_names: list[str]) -> None:
    """Drop all indexes touching columns removed by this revision.

    Metadata-first databases can contain ORM-generated indexes whose names do
    not appear in the historical migration.  SQLite recreates a table during
    ``batch_alter_table`` and would otherwise try to recreate those indexes
    after their source column has been removed (``no such column``).
    """
    conn = op.get_bind()
    inspector = inspect(conn)
    try:
        indexes = inspector.get_indexes(table_name)
    except Exception:
        return
    removed = set(column_names)
    for index in indexes:
        index_name = index.get("name")
        index_columns = set(index.get("column_names") or ())
        if index_name and index_columns.intersection(removed):
            try:
                op.drop_index(index_name, table_name=table_name)
            except Exception:
                pass


def upgrade() -> None:
    # ───────── (1) decision_runs 追加列 ─────────
    run_new_cols = [
        ("summary", Text, None, True),
        ("checks_json", Text, None, True),
        ("result_hash", String(64), None, True),
        ("universe_count", Integer, "0", False),
        ("member_count", Integer, "0", False),
        ("score_count_expected", Integer, None, True),
        ("score_count_actual", Integer, None, True),
        ("score_coverage_pct", Float, None, True),
        ("score_max_age_days", Integer, None, True),
        ("blocking_status", String(32), "READY", False),
        ("blocking_reasons_json", Text, None, True),
        ("versions_json", Text, None, True),
        ("started_at", DateTime, None, True),
        ("finished_at", DateTime, None, True),
        ("duration_ms", Integer, None, True),
        ("is_result_production_eligible", Integer, "1", False),
        ("match_mode", String(16), "NEXT_OPEN", False),
        ("correlation_id", String(64), None, True),
        ("task_id", String(64), None, True),
    ]
    with op.batch_alter_table("decision_runs") as batch_op:
        for col_name, col_type, srv_default, nullable in run_new_cols:
            if not _column_exists("decision_runs", col_name):
                kwargs = {"nullable": nullable}
                if srv_default is not None:
                    kwargs["server_default"] = str(srv_default)
                batch_op.add_column(Column(col_name, col_type, **kwargs))

    # 索引追加（幂等 + 关联查询）
    for idx_cols, idx_name in [
        (["result_hash"], "ix_decision_runs_result_hash"),
        (["blocking_status"], "ix_decision_runs_blocking_status"),
        (["correlation_id"], "ix_decision_runs_correlation_id"),
        (["match_mode"], "ix_decision_runs_match_mode"),
    ]:
        try:
            op.create_index(idx_name, "decision_runs", idx_cols, unique=False)
        except Exception:
            pass
    # match_mode CHECK（PostgreSQL/MySQL 生效；SQLite env.py 降级）
    try:
        op.create_check_constraint(
            "ck_decision_runs_match_mode_values",
            "decision_runs",
            "match_mode IN ('NEXT_OPEN','T_CLOSE')",
        )
    except Exception:
        pass

    # ───────── (2) decision_evidence 追加列 ─────────
    ev_new_cols = [
        ("action_subtype", String(32), None, True),
        ("target_position_pct", Float, None, True),
        ("min_lot_size", Integer, "100", False),
        ("target_quantity", Float, None, True),
        ("target_qty_delta", Float, None, True),
        ("intended_price", Float, None, True),
        ("executed_price", Float, None, True),
        ("slippage_bps", Float, None, True),
        ("rejection_reason", String(64), None, True),
        ("rejection_detail", Text, None, True),
        ("blocking_reason", Text, None, True),
        ("roll_forward_days", Integer, None, True),
        ("rejections_trace_json", Text, None, True),
        ("exit_rules_hit_json", Text, None, True),
        ("score_id", None, None, True),  # FK，单独用 add_column + ForeignKey
        ("score_value", Float, None, True),
        ("score_rank", Integer, None, True),
        ("score_published_at", DateTime, None, True),
        ("pit_safe_flag", String(16), "UNKNOWN", False),
        ("constraints_json", Text, None, True),
        ("versions_json", Text, None, True),
        ("reason_codes_json", Text, None, True),
        ("factor_contributions_json", Text, None, True),
        ("legacy_fallback_flag", Integer, "0", False),
        ("stop_loss_verified_price_source", String(32), None, True),
        ("stop_loss_triggered", Integer, "0", False),
        ("match_mode", String(16), "NEXT_OPEN", False),
        ("manual_price_flag", Integer, "0", False),
        ("idempotency_key", String(64), None, True),
    ]
    with op.batch_alter_table("decision_evidence") as batch_op:
        for col_name, col_type, srv_default, nullable in ev_new_cols:
            if _column_exists("decision_evidence", col_name):
                continue
            kwargs = {"nullable": nullable}
            if srv_default is not None:
                kwargs["server_default"] = str(srv_default)
            # score_id FK: 不在 batch 里加 ForeignKey（SQLite batch 要求约束有名字
            # 而 Column(ForeignKey(...)) 简写不会自动命名），改为先加裸列，
            # 退出 batch 后用 create_foreign_key 显式命名追加。
            if col_name == "score_id":
                batch_op.add_column(Column("score_id", Integer, nullable=True))
            else:
                batch_op.add_column(Column(col_name, col_type, **kwargs))
    # 为 score_id 追加显式命名的 FK 约束（脱离 batch，防止 ValueError Constraint must have a name）
    if _column_exists("decision_evidence", "score_id"):
        try:
            op.create_foreign_key(
                "fk_decision_evidence_score_id",
                "decision_evidence",
                "scores",
                ["score_id"],
                ["id"],
                ondelete="SET NULL",
            )
        except Exception:
            pass

    # 追加索引
    for idx_cols, idx_name in [
        (["action_subtype"], "ix_decision_evidence_action_subtype"),
        (["rejection_reason"], "ix_decision_evidence_rejection_reason"),
        (["roll_forward_days"], "ix_decision_evidence_roll_forward_days"),
        (["score_id"], "ix_decision_evidence_score_id"),
        (["pit_safe_flag"], "ix_decision_evidence_pit_safe_flag"),
        (["idempotency_key"], "ix_decision_evidence_idempotency_key"),
        (["match_mode"], "ix_decision_evidence_match_mode"),
        (["manual_price_flag"], "ix_decision_evidence_manual_price_flag"),
    ]:
        try:
            op.create_index(idx_name, "decision_evidence", idx_cols, unique=False)
        except Exception:
            pass
    # pit_safe_flag CHECK + match_mode CHECK + action 6 values CHECK（ORM 已存在，避免重复用 try）
    for ck_name, ck_expr in [
        ("ck_decision_evidence_pit_safe_flag_values",
         "pit_safe_flag IN ('PIT_SAFE','NOT_PIT_SAFE','UNKNOWN')"),
        ("ck_decision_evidence_match_mode_values",
         "match_mode IN ('NEXT_OPEN','T_CLOSE')"),
    ]:
        try:
            op.create_check_constraint(ck_name, "decision_evidence", ck_expr)
        except Exception:
            pass


def downgrade() -> None:
    # ── (2) decision_evidence 反向 ──
    ev_drop_cols = [
        "idempotency_key", "manual_price_flag", "match_mode",
        "stop_loss_triggered", "stop_loss_verified_price_source",
        "legacy_fallback_flag", "factor_contributions_json",
        "reason_codes_json", "versions_json", "constraints_json",
        "pit_safe_flag", "score_published_at", "score_rank",
        "score_value", "score_id", "exit_rules_hit_json",
        "rejections_trace_json", "roll_forward_days",
        "blocking_reason", "rejection_detail", "rejection_reason",
        "slippage_bps", "executed_price", "intended_price",
        "target_qty_delta", "target_quantity", "min_lot_size",
        "target_position_pct", "action_subtype",
    ]
    _drop_indexes_referencing_columns("decision_evidence", ev_drop_cols)
    for idx_name in [
        "ix_decision_evidence_manual_price_flag",
        "ix_decision_evidence_match_mode",
        "ix_decision_evidence_idempotency_key",
        "ix_decision_evidence_pit_safe_flag",
        "ix_decision_evidence_score_id",
        "ix_decision_evidence_roll_forward_days",
        "ix_decision_evidence_rejection_reason",
        "ix_decision_evidence_action_subtype",
    ]:
        try:
            op.drop_index(idx_name, table_name="decision_evidence")
        except Exception:
            pass
    for ck_name in [
        "ck_decision_evidence_pit_safe_flag_values",
        "ck_decision_evidence_match_mode_values",
    ]:
        try:
            op.drop_constraint(ck_name, "decision_evidence", type_="check")
        except Exception:
            pass
    with op.batch_alter_table("decision_evidence") as batch_op:
        for col_name in ev_drop_cols:
            if _column_exists("decision_evidence", col_name):
                try:
                    batch_op.drop_column(col_name)
                except Exception:
                    pass
    # ── (1) decision_runs 反向 ──
    run_drop_cols = [
        "task_id", "correlation_id", "match_mode",
        "is_result_production_eligible", "duration_ms",
        "finished_at", "started_at", "versions_json",
        "blocking_reasons_json", "blocking_status",
        "score_max_age_days", "score_coverage_pct",
        "score_count_actual", "score_count_expected",
        "member_count", "universe_count",
        "result_hash", "checks_json", "summary",
    ]
    _drop_indexes_referencing_columns("decision_runs", run_drop_cols)
    for idx_name in [
        "ix_decision_runs_match_mode",
        "ix_decision_runs_correlation_id",
        "ix_decision_runs_blocking_status",
        "ix_decision_runs_result_hash",
    ]:
        try:
            op.drop_index(idx_name, table_name="decision_runs")
        except Exception:
            pass
    try:
        op.drop_constraint("ck_decision_runs_match_mode_values",
                           "decision_runs", type_="check")
    except Exception:
        pass
    with op.batch_alter_table("decision_runs") as batch_op:
        for col_name in run_drop_cols:
            if _column_exists("decision_runs", col_name):
                try:
                    batch_op.drop_column(col_name)
                except Exception:
                    pass
