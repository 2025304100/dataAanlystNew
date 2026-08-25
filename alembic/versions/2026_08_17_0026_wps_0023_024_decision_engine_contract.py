"""wps_0023_024_decision_engine_contract.

Revision ID: wps_0023_024_decision_engine_contract
Revises: wps_0023_023_score_traceability
Create Date: 2026-08-17 00:26:00.000000

"""
from alembic import op
from sqlalchemy import Boolean, CheckConstraint, Column, Date, DateTime, Float, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, inspect

# revision identifiers, used by Alembic.
revision = 'wps_0023_024_decision_engine_contract'
down_revision = 'wps_0023_023_score_traceability'
branch_labels = None
depends_on = None


def _column_exists(table_name: str, column_name: str) -> bool:
    conn = op.get_bind()
    inspector = inspect(conn)
    columns = [c["name"] for c in inspector.get_columns(table_name)]
    return column_name in columns


def _constraint_exists(table_name: str, constraint_name: str) -> bool:
    conn = op.get_bind()
    inspector = inspect(conn)
    unique_constraints = inspector.get_unique_constraints(table_name)
    for uc in unique_constraints:
        if uc["name"] == constraint_name:
            return True
    return False


def _drop_indexes_referencing_columns(table_name: str, column_names: list[str]) -> None:
    """Drop reflected ORM indexes before SQLite batch column removal."""
    conn = op.get_bind()
    inspector = inspect(conn)
    try:
        indexes = inspector.get_indexes(table_name)
    except Exception:
        return
    removed = set(column_names)
    for idx in indexes:
        name = idx.get("name")
        if name and set(idx.get("column_names") or ()).intersection(removed):
            try:
                op.drop_index(name, table_name=table_name)
            except Exception:
                pass


def _named_constraint_exists(table_name: str, constraint_name: str,
                             constraint_type: str) -> bool:
    inspector = inspect(op.get_bind())
    try:
        constraints = (
            inspector.get_check_constraints(table_name)
            if constraint_type == "check"
            else inspector.get_unique_constraints(table_name)
        )
    except Exception:
        return False
    return any(item.get("name") == constraint_name for item in constraints)


def upgrade() -> None:
    op.create_table(
        'portfolio_factor_usages',
        Column('id', String(64), primary_key=True),
        Column('portfolio_id', Integer, ForeignKey('portfolios.id', ondelete='RESTRICT'), nullable=False),
        Column('factor_model_run_id', String(64), nullable=False),
        Column('factor_set_id', String(64), nullable=True),
        Column('rule_id', Integer, nullable=True),
        Column('rule_version', Integer, nullable=True),
        Column('run_mode', String(16), nullable=False, server_default='research'),
        Column('pit_mode', String(16), nullable=False, server_default='best_effort'),
        Column('score_sla_coverage_pct', Float, nullable=False, server_default='95.0'),
        Column('score_sla_max_age_days', Integer, nullable=False, server_default='1'),
        Column('status', String(16), nullable=False, server_default='draft'),
        Column('rollback_target_model_run_id', String(64), nullable=True),
        Column('versions_json', Text, nullable=True),
        Column('content_hash', String(64), nullable=False),
        Column('effective_from', DateTime, nullable=False),
        Column('effective_to', DateTime, nullable=True),
        Column('created_by', String(128), nullable=False, server_default='local_user'),
        Column('created_at', DateTime, nullable=False),
        Column('updated_at', DateTime, nullable=False),
        CheckConstraint(
            "run_mode IN ('research', 'production_pit', 'production_sim')",
            name='ck_portfolio_factor_usages_run_mode_3values',
        ),
        CheckConstraint(
            "status IN ('draft', 'active', 'deprecated', 'rollback_pending')",
            name='ck_portfolio_factor_usages_status_4values',
        ),
    )
    op.create_index('ix_portfolio_factor_usages_portfolio_id', 'portfolio_factor_usages', ['portfolio_id'])
    op.create_index('ix_portfolio_factor_usages_factor_model_run_id', 'portfolio_factor_usages', ['factor_model_run_id'])
    op.create_index('ix_portfolio_factor_usages_status', 'portfolio_factor_usages', ['status'])
    op.create_index('ix_portfolio_factor_usages_content_hash', 'portfolio_factor_usages', ['content_hash'])
    op.create_index('ix_portfolio_factor_usages_effective_from', 'portfolio_factor_usages', ['effective_from'])
    op.create_index('ix_portfolio_factor_usages_created_at', 'portfolio_factor_usages', ['created_at'])

    op.create_table(
        'strategy_execution_snapshots',
        Column('id', String(64), primary_key=True),
        Column('snapshot_no', Integer, nullable=False),
        Column('portfolio_id', Integer, ForeignKey('portfolios.id', ondelete='RESTRICT'), nullable=False),
        Column('portfolio_factor_usage_id', String(64), ForeignKey('portfolio_factor_usages.id', ondelete='SET NULL'), nullable=True),
        Column('factor_model_run_id', String(64), nullable=True),
        Column('factor_set_id', String(64), nullable=True),
        Column('rule_id', Integer, nullable=True),
        Column('rule_version', Integer, nullable=True),
        Column('decision_clock_json', Text, nullable=False),
        Column('cost_config_json', Text, nullable=True),
        Column('member_snapshot_json', Text, nullable=False),
        Column('candidate_pool_json', Text, nullable=True),
        Column('universe_type', String(32), nullable=False, server_default='portfolio_members'),
        Column('benchmark_code', String(32), nullable=True),
        Column('snapshot_type', String(16), nullable=False, server_default='save_and_apply'),
        Column('snapshot_hash', String(64), nullable=False),
        Column('idempotency_key', String(64), nullable=True),
        Column('key_members_json', Text, nullable=True),
        Column('gate_policy_version', String(32), nullable=True),
        Column('gate_result_json', Text, nullable=True),
        Column('versions_json', Text, nullable=True),
        Column('effective_from', DateTime, nullable=False),
        Column('task_locked_at', DateTime, nullable=True),
        Column('task_id', String(64), nullable=True),
        Column('locked_by_run_type', String(16), nullable=True),
        Column('created_by', String(128), nullable=False, server_default='local_user'),
        Column('created_at', DateTime, nullable=False),
        UniqueConstraint(
            'portfolio_id', 'snapshot_no',
            name='uq_strategy_exec_snapshot_portfolio_no',
        ),
        CheckConstraint(
            "universe_type IN ('portfolio_members', 'candidate_pool_union')",
            name='ck_strategy_execution_snapshots_universe_type_2values',
        ),
        CheckConstraint(
            "snapshot_type IN ('preflight', 'save_and_apply', 'task_locked')",
            name='ck_strategy_execution_snapshots_snapshot_type_3values',
        ),
    )
    op.create_index('ix_strategy_execution_snapshots_portfolio_id', 'strategy_execution_snapshots', ['portfolio_id'])
    op.create_index('ix_strategy_execution_snapshots_portfolio_factor_usage_id', 'strategy_execution_snapshots', ['portfolio_factor_usage_id'])
    op.create_index('ix_strategy_execution_snapshots_factor_model_run_id', 'strategy_execution_snapshots', ['factor_model_run_id'])
    op.create_index('ix_strategy_execution_snapshots_factor_set_id', 'strategy_execution_snapshots', ['factor_set_id'])
    op.create_index('ix_strategy_execution_snapshots_snapshot_hash', 'strategy_execution_snapshots', ['snapshot_hash'])
    op.create_index('ix_strategy_execution_snapshots_idempotency_key', 'strategy_execution_snapshots', ['idempotency_key'], unique=True)
    op.create_index('ix_strategy_execution_snapshots_task_id', 'strategy_execution_snapshots', ['task_id'])
    op.create_index('ix_strategy_execution_snapshots_effective_from', 'strategy_execution_snapshots', ['effective_from'])

    op.create_table(
        'decision_runs',
        Column('id', String(64), primary_key=True),
        Column('strategy_snapshot_id', String(64), ForeignKey('strategy_execution_snapshots.id', ondelete='RESTRICT'), nullable=False),
        Column('portfolio_id', Integer, nullable=False),
        Column('run_type', String(16), nullable=False),
        Column('status', String(32), nullable=False, server_default='PENDING'),
        Column('summary', Text, nullable=True),
        Column('checks_json', Text, nullable=True),
        Column('result_hash', String(64), nullable=True),
        Column('trade_date', Date, nullable=False),
        Column('decision_at', DateTime, nullable=False),
        Column('data_cutoff_at', DateTime, nullable=False),
        Column('execution_at', DateTime, nullable=False),
        Column('run_mode', String(16), nullable=False, server_default='research'),
        Column('pit_mode', String(16), nullable=False, server_default='best_effort'),
        Column('universe_count', Integer, nullable=False, server_default='0'),
        Column('member_count', Integer, nullable=False, server_default='0'),
        Column('score_count_expected', Integer, nullable=True),
        Column('score_count_actual', Integer, nullable=True),
        Column('score_coverage_pct', Float, nullable=True),
        Column('score_max_age_days', Integer, nullable=True),
        Column('blocking_status', String(32), nullable=False, server_default='READY'),
        Column('blocking_reasons_json', Text, nullable=True),
        Column('idempotency_key', String(64), nullable=True),
        Column('versions_json', Text, nullable=True),
        Column('started_at', DateTime, nullable=True),
        Column('finished_at', DateTime, nullable=True),
        Column('duration_ms', Integer, nullable=True),
        Column('is_result_production_eligible', Integer, nullable=False, server_default='1'),
        Column('created_at', DateTime, nullable=False),
        CheckConstraint(
            "run_type IN ('dry_run','backtest','auto_simulation')",
            name='ck_decision_runs_run_type_3values',
        ),
        CheckConstraint(
            "status IN ('PENDING','RUNNING','SUCCEEDED','FAILED','INTERRUPTED','CANCELLED')",
            name='ck_decision_runs_status_6values',
        ),
        CheckConstraint(
            "blocking_status IN ('READY','DATA_INCOMPLETE_PAUSED','RECONCILIATION_BLOCKED','MODEL_INACTIVE','SCORE_STALE','COMPLETED')",
            name='ck_decision_runs_blocking_status_6values',
        ),
        UniqueConstraint(
            'idempotency_key',
            name='uq_decision_runs_idempotency_key',
        ),
    )
    op.create_index('ix_decision_runs_strategy_snapshot_id', 'decision_runs', ['strategy_snapshot_id'])
    op.create_index('ix_decision_runs_portfolio_id', 'decision_runs', ['portfolio_id'])
    op.create_index('ix_decision_runs_run_type', 'decision_runs', ['run_type'])
    op.create_index('ix_decision_runs_status', 'decision_runs', ['status'])
    op.create_index('ix_decision_runs_result_hash', 'decision_runs', ['result_hash'])
    op.create_index('ix_decision_runs_trade_date', 'decision_runs', ['trade_date'])
    op.create_index('ix_decision_runs_idempotency_key', 'decision_runs', ['idempotency_key'])
    op.create_index('ix_decision_runs_created_at', 'decision_runs', ['created_at'])

    op.create_table(
        'decision_evidence',
        Column('id', String(64), primary_key=True),
        Column('decision_run_id', String(64), ForeignKey('decision_runs.id', ondelete='RESTRICT'), nullable=False),
        Column('strategy_snapshot_id', String(64), nullable=False),
        Column('portfolio_id', Integer, nullable=False),
        Column('symbol_id', Integer, ForeignKey('symbols.id', ondelete='RESTRICT'), nullable=False),
        Column('trade_date', Date, nullable=False),
        Column('decision_at', DateTime, nullable=False),
        Column('data_cutoff_at', DateTime, nullable=False),
        Column('execution_at', DateTime, nullable=False),
        Column('action', String(24), nullable=False),
        Column('action_subtype', String(32), nullable=True),
        Column('target_position_pct', Float, nullable=True),
        Column('min_lot_size', Integer, nullable=False, server_default='100'),
        Column('target_quantity', Float, nullable=True),
        Column('target_qty_delta', Float, nullable=True),
        Column('intended_price', Float, nullable=True),
        Column('executed_price', Float, nullable=True),
        Column('slippage_bps', Float, nullable=True),
        Column('rejection_reason', String(64), nullable=True),
        Column('rejection_detail', Text, nullable=True),
        Column('blocking_reason', Text, nullable=True),
        Column('score_id', Integer, ForeignKey('scores.id', ondelete='SET NULL'), nullable=True),
        Column('score_value', Float, nullable=True),
        Column('score_rank', Integer, nullable=True),
        Column('score_published_at', DateTime, nullable=True),
        Column('pit_safe_flag', String(16), nullable=False, server_default='UNKNOWN'),
        Column('constraints_json', Text, nullable=True),
        Column('versions_json', Text, nullable=True),
        Column('reason_codes_json', Text, nullable=True),
        Column('factor_contributions_json', Text, nullable=True),
        Column('legacy_fallback_flag', Integer, nullable=False, server_default='0'),
        Column('stop_loss_verified_price_source', String(32), nullable=True),
        Column('stop_loss_triggered', Integer, nullable=False, server_default='0'),
        Column('match_mode', String(16), nullable=False, server_default='NEXT_OPEN'),
        Column('content_hash', String(64), nullable=False),
        Column('idempotency_key', String(64), nullable=True),
        Column('snapshot_no', Integer, nullable=True),
        Column('created_at', DateTime, nullable=False),
        CheckConstraint(
            "action IN ('BUY', 'SELL', 'HOLD', 'NO_ACTION', 'REJECTED', 'DATA_BLOCKED')",
            name='ck_decision_evidence_action_6values',
        ),
        UniqueConstraint(
            'decision_run_id', 'symbol_id',
            name='uq_decision_evidence_run_symbol',
        ),
    )
    op.create_index('ix_decision_evidence_decision_run_id', 'decision_evidence', ['decision_run_id'])
    op.create_index('ix_decision_evidence_strategy_snapshot_id', 'decision_evidence', ['strategy_snapshot_id'])
    op.create_index('ix_decision_evidence_portfolio_id', 'decision_evidence', ['portfolio_id'])
    op.create_index('ix_decision_evidence_symbol_id', 'decision_evidence', ['symbol_id'])
    op.create_index('ix_decision_evidence_trade_date', 'decision_evidence', ['trade_date'])
    op.create_index('ix_decision_evidence_action', 'decision_evidence', ['action'])
    op.create_index('ix_decision_evidence_action_subtype', 'decision_evidence', ['action_subtype'])
    op.create_index('ix_decision_evidence_rejection_reason', 'decision_evidence', ['rejection_reason'])
    op.create_index('ix_decision_evidence_idempotency_key', 'decision_evidence', ['idempotency_key'])
    op.create_index('ix_decision_evidence_created_at', 'decision_evidence', ['created_at'])

    op.create_table(
        'idempotency_records',
        Column('idempotency_key', String(128), primary_key=True),
        Column('resource_type', String(64), nullable=False),
        Column('resource_id', String(128), nullable=True),
        Column('request_hash', String(128), nullable=True),
        Column('status', String(32), nullable=False),
        Column('response_json', Text, nullable=True),
        Column('created_at', DateTime, nullable=False),
        Column('expires_at', DateTime, nullable=True),
    )
    op.create_index('ix_idempotency_records_resource_type', 'idempotency_records', ['resource_type'])
    op.create_index('ix_idempotency_records_status', 'idempotency_records', ['status'])
    op.create_index('ix_idempotency_records_created_at', 'idempotency_records', ['created_at'])

    op.create_table(
        'portfolio_cron_schedules',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('portfolio_id', Integer, ForeignKey('portfolios.id', ondelete='RESTRICT'), nullable=False),
        Column('schedule_type', String(32), nullable=False),
        Column('hour', Integer, nullable=False),
        Column('minute', Integer, nullable=False),
        Column('weekdays_json', Text, nullable=True),
        Column('timezone', String(64), nullable=False, server_default='Asia/Shanghai'),
        Column('enabled', Integer, nullable=False, server_default='1'),
        Column('last_run_at', DateTime, nullable=True),
        Column('next_run_at', DateTime, nullable=True),
        Column('created_at', DateTime, nullable=False),
        Column('updated_at', DateTime, nullable=False),
        UniqueConstraint(
            'portfolio_id', 'schedule_type',
            name='uq_portfolio_cron_schedules_portfolio_type',
        ),
        CheckConstraint(
            "hour >= 0 AND hour <= 23",
            name='ck_portfolio_cron_schedules_hour_range',
        ),
        CheckConstraint(
            "minute >= 0 AND minute <= 59",
            name='ck_portfolio_cron_schedules_minute_range',
        ),
    )
    op.create_index('ix_portfolio_cron_schedules_portfolio_id', 'portfolio_cron_schedules', ['portfolio_id'])
    op.create_index('ix_portfolio_cron_schedules_schedule_type', 'portfolio_cron_schedules', ['schedule_type'])
    op.create_index('ix_portfolio_cron_schedules_enabled', 'portfolio_cron_schedules', ['enabled'])

    with op.batch_alter_table("portfolios") as batch_op:
        if not _column_exists("portfolios", "last_decision_trade_date"):
            batch_op.add_column(Column('last_decision_trade_date', String(32), nullable=True))
        if not _column_exists("portfolios", "last_reconciled_trade_date"):
            batch_op.add_column(Column('last_reconciled_trade_date', String(32), nullable=True))
        if not _column_exists("portfolios", "lease_key"):
            batch_op.add_column(Column('lease_key', String(128), nullable=True))
        if not _column_exists("portfolios", "lease_expire_at"):
            batch_op.add_column(Column('lease_expire_at', DateTime, nullable=True))
        if not _column_exists("portfolios", "effective_start_date"):
            batch_op.add_column(Column('effective_start_date', String(32), nullable=True))
        if not _column_exists("portfolios", "auto_schedule_hour"):
            batch_op.add_column(Column('auto_schedule_hour', Integer, nullable=False, server_default='20'))
        if not _column_exists("portfolios", "auto_schedule_minute"):
            batch_op.add_column(Column('auto_schedule_minute', Integer, nullable=False, server_default='30'))

    with op.batch_alter_table("alert_events") as batch_op:
        if not _column_exists("alert_events", "portfolio_id"):
            batch_op.add_column(Column('portfolio_id', Integer, nullable=True))
        if not _column_exists("alert_events", "decision_run_id"):
            batch_op.add_column(Column('decision_run_id', String(64), nullable=True))
        if not _column_exists("alert_events", "task_id"):
            batch_op.add_column(Column('task_id', String(64), nullable=True))
        if not _column_exists("alert_events", "correlation_id"):
            batch_op.add_column(Column('correlation_id', String(128), nullable=True))
        if not _column_exists("alert_events", "dedupe_key"):
            batch_op.add_column(Column('dedupe_key', String(256), nullable=True))
        if not _column_exists("alert_events", "incident_no"):
            batch_op.add_column(Column('incident_no', String(64), nullable=True))
        if not _column_exists("alert_events", "status"):
            batch_op.add_column(Column('status', String(16), nullable=False, server_default='open'))
        if not _column_exists("alert_events", "resolved_at"):
            batch_op.add_column(Column('resolved_at', DateTime, nullable=True))
        if not _column_exists("alert_events", "window_start_at"):
            batch_op.add_column(Column('window_start_at', DateTime, nullable=True))
        if not _column_exists("alert_events", "severity_level"):
            batch_op.add_column(Column('severity_level', String(16), nullable=True))

    if not _constraint_exists("alert_events", "ck_alert_events_status"):
        try:
            op.create_check_constraint(
                "ck_alert_events_status",
                "alert_events",
                "status IN ('open', 'acknowledged', 'resolved', 'closed')",
            )
        except Exception:
            pass

    if not _constraint_exists("alert_events", "ck_alert_events_severity_level"):
        try:
            op.create_check_constraint(
                "ck_alert_events_severity_level",
                "alert_events",
                "severity_level IN ('info', 'warning', 'error', 'critical')",
            )
        except Exception:
            pass

    if not _constraint_exists("alert_events", "uq_dedupe_incident"):
        try:
            op.create_unique_constraint(
                "uq_dedupe_incident",
                "alert_events",
                ["dedupe_key", "incident_no"],
            )
        except Exception:
            pass

    with op.batch_alter_table("portfolio_candidates") as batch_op:
        if _constraint_exists("portfolio_candidates", "uq_portfolio_candidate_symbol"):
            batch_op.drop_constraint("uq_portfolio_candidate_symbol", type_="unique")
        if not _column_exists("portfolio_candidates", "effective_from"):
            batch_op.add_column(Column('effective_from', DateTime, nullable=False, server_default='1970-01-01 00:00:00'))
        if not _column_exists("portfolio_candidates", "effective_to"):
            batch_op.add_column(Column('effective_to', DateTime, nullable=True))
        if not _column_exists("portfolio_candidates", "auto_authorized_flag"):
            batch_op.add_column(Column('auto_authorized_flag', Integer, nullable=False, server_default='0'))
        if not _column_exists("portfolio_candidates", "removed_manually_flag"):
            batch_op.add_column(Column('removed_manually_flag', Integer, nullable=False, server_default='0'))
        if not _column_exists("portfolio_candidates", "removal_reason"):
            batch_op.add_column(Column('removal_reason', String(256), nullable=True))
        if not _column_exists("portfolio_candidates", "audit_version"):
            batch_op.add_column(Column('audit_version', Integer, nullable=False, server_default='1'))

    if not _constraint_exists("portfolio_candidates", "uq_portfolio_candidate_symbol_effective"):
        try:
            op.create_unique_constraint(
                "uq_portfolio_candidate_symbol_effective",
                "portfolio_candidates",
                ["portfolio_id", "symbol_id", "effective_from"],
            )
        except Exception:
            pass


def downgrade() -> None:
    candidate_drop_cols = [
        "audit_version", "removal_reason", "removed_manually_flag",
        "auto_authorized_flag", "effective_to", "effective_from",
    ]
    _drop_indexes_referencing_columns("portfolio_candidates", candidate_drop_cols)
    with op.batch_alter_table("portfolio_candidates") as batch_op:
        if _named_constraint_exists("portfolio_candidates", "uq_portfolio_candidate_symbol_effective", "unique"):
            batch_op.drop_constraint("uq_portfolio_candidate_symbol_effective", type_="unique")
        if _column_exists("portfolio_candidates", "audit_version"):
            batch_op.drop_column("audit_version")
        if _column_exists("portfolio_candidates", "removal_reason"):
            batch_op.drop_column("removal_reason")
        if _column_exists("portfolio_candidates", "removed_manually_flag"):
            batch_op.drop_column("removed_manually_flag")
        if _column_exists("portfolio_candidates", "auto_authorized_flag"):
            batch_op.drop_column("auto_authorized_flag")
        if _column_exists("portfolio_candidates", "effective_to"):
            batch_op.drop_column("effective_to")
        if _column_exists("portfolio_candidates", "effective_from"):
            batch_op.drop_column("effective_from")
        try:
            batch_op.create_unique_constraint("uq_portfolio_candidate_symbol", ["portfolio_id", "symbol_id"])
        except Exception:
            pass

    alert_drop_cols = [
        "severity_level", "window_start_at", "resolved_at", "status",
        "incident_no", "dedupe_key", "correlation_id", "task_id",
        "decision_run_id", "portfolio_id",
    ]
    _drop_indexes_referencing_columns("alert_events", alert_drop_cols)
    with op.batch_alter_table("alert_events") as batch_op:
        for constraint_name, constraint_type in [
            ("uq_dedupe_incident", "unique"),
            ("uq_alert_events_dedupe_incident", "unique"),
            ("ck_alert_events_severity_level", "check"),
            ("ck_alert_events_status", "check"),
        ]:
            if _named_constraint_exists("alert_events", constraint_name, constraint_type):
                batch_op.drop_constraint(constraint_name, type_=constraint_type)
        if _column_exists("alert_events", "severity_level"):
            batch_op.drop_column("severity_level")
        if _column_exists("alert_events", "window_start_at"):
            batch_op.drop_column("window_start_at")
        if _column_exists("alert_events", "resolved_at"):
            batch_op.drop_column("resolved_at")
        if _column_exists("alert_events", "status"):
            batch_op.drop_column("status")
        if _column_exists("alert_events", "incident_no"):
            batch_op.drop_column("incident_no")
        if _column_exists("alert_events", "dedupe_key"):
            batch_op.drop_column("dedupe_key")
        if _column_exists("alert_events", "correlation_id"):
            batch_op.drop_column("correlation_id")
        if _column_exists("alert_events", "task_id"):
            batch_op.drop_column("task_id")
        if _column_exists("alert_events", "decision_run_id"):
            batch_op.drop_column("decision_run_id")
        if _column_exists("alert_events", "portfolio_id"):
            batch_op.drop_column("portfolio_id")

    portfolio_drop_cols = [
        "auto_schedule_minute", "auto_schedule_hour", "effective_start_date",
        "lease_expire_at", "lease_key", "last_reconciled_trade_date",
        "last_decision_trade_date",
    ]
    _drop_indexes_referencing_columns("portfolios", portfolio_drop_cols)
    with op.batch_alter_table("portfolios") as batch_op:
        if _column_exists("portfolios", "auto_schedule_minute"):
            batch_op.drop_column("auto_schedule_minute")
        if _column_exists("portfolios", "auto_schedule_hour"):
            batch_op.drop_column("auto_schedule_hour")
        if _column_exists("portfolios", "effective_start_date"):
            batch_op.drop_column("effective_start_date")
        if _column_exists("portfolios", "lease_expire_at"):
            batch_op.drop_column("lease_expire_at")
        if _column_exists("portfolios", "lease_key"):
            batch_op.drop_column("lease_key")
        if _column_exists("portfolios", "last_reconciled_trade_date"):
            batch_op.drop_column("last_reconciled_trade_date")
        if _column_exists("portfolios", "last_decision_trade_date"):
            batch_op.drop_column("last_decision_trade_date")

    op.drop_index('ix_portfolio_cron_schedules_enabled', table_name='portfolio_cron_schedules')
    op.drop_index('ix_portfolio_cron_schedules_schedule_type', table_name='portfolio_cron_schedules')
    op.drop_index('ix_portfolio_cron_schedules_portfolio_id', table_name='portfolio_cron_schedules')
    op.drop_table('portfolio_cron_schedules')

    op.drop_index('ix_idempotency_records_created_at', table_name='idempotency_records')
    op.drop_index('ix_idempotency_records_status', table_name='idempotency_records')
    op.drop_index('ix_idempotency_records_resource_type', table_name='idempotency_records')
    op.drop_table('idempotency_records')

    op.drop_index('ix_decision_evidence_created_at', table_name='decision_evidence')
    op.drop_index('ix_decision_evidence_idempotency_key', table_name='decision_evidence')
    op.drop_index('ix_decision_evidence_rejection_reason', table_name='decision_evidence')
    op.drop_index('ix_decision_evidence_action_subtype', table_name='decision_evidence')
    op.drop_index('ix_decision_evidence_action', table_name='decision_evidence')
    op.drop_index('ix_decision_evidence_trade_date', table_name='decision_evidence')
    op.drop_index('ix_decision_evidence_symbol_id', table_name='decision_evidence')
    op.drop_index('ix_decision_evidence_portfolio_id', table_name='decision_evidence')
    op.drop_index('ix_decision_evidence_strategy_snapshot_id', table_name='decision_evidence')
    op.drop_index('ix_decision_evidence_decision_run_id', table_name='decision_evidence')
    op.drop_table('decision_evidence')

    op.drop_index('ix_decision_runs_created_at', table_name='decision_runs')
    op.drop_index('ix_decision_runs_idempotency_key', table_name='decision_runs')
    op.drop_index('ix_decision_runs_result_hash', table_name='decision_runs')
    op.drop_index('ix_decision_runs_status', table_name='decision_runs')
    op.drop_index('ix_decision_runs_trade_date', table_name='decision_runs')
    op.drop_index('ix_decision_runs_run_type', table_name='decision_runs')
    op.drop_index('ix_decision_runs_portfolio_id', table_name='decision_runs')
    op.drop_index('ix_decision_runs_strategy_snapshot_id', table_name='decision_runs')
    op.drop_table('decision_runs')

    op.drop_index('ix_strategy_execution_snapshots_effective_from', table_name='strategy_execution_snapshots')
    op.drop_index('ix_strategy_execution_snapshots_task_id', table_name='strategy_execution_snapshots')
    op.drop_index('ix_strategy_execution_snapshots_idempotency_key', table_name='strategy_execution_snapshots')
    op.drop_index('ix_strategy_execution_snapshots_snapshot_hash', table_name='strategy_execution_snapshots')
    op.drop_index('ix_strategy_execution_snapshots_factor_set_id', table_name='strategy_execution_snapshots')
    op.drop_index('ix_strategy_execution_snapshots_factor_model_run_id', table_name='strategy_execution_snapshots')
    op.drop_index('ix_strategy_execution_snapshots_portfolio_factor_usage_id', table_name='strategy_execution_snapshots')
    op.drop_index('ix_strategy_execution_snapshots_portfolio_id', table_name='strategy_execution_snapshots')
    op.drop_table('strategy_execution_snapshots')

    op.drop_index('ix_portfolio_factor_usages_effective_from', table_name='portfolio_factor_usages')
    op.drop_index('ix_portfolio_factor_usages_created_at', table_name='portfolio_factor_usages')
    op.drop_index('ix_portfolio_factor_usages_content_hash', table_name='portfolio_factor_usages')
    op.drop_index('ix_portfolio_factor_usages_status', table_name='portfolio_factor_usages')
    op.drop_index('ix_portfolio_factor_usages_factor_model_run_id', table_name='portfolio_factor_usages')
    op.drop_index('ix_portfolio_factor_usages_portfolio_id', table_name='portfolio_factor_usages')
    op.drop_table('portfolio_factor_usages')
