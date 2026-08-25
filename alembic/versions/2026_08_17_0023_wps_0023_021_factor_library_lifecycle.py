"""wps_0023_021_factor_library_lifecycle.

Revision ID: wps_0023_021_factor_library_lifecycle
Revises: wps_0801_001_universe_incremental_index
Create Date: 2026-08-17 00:23:00.000000

"""
from datetime import datetime, timezone
from hashlib import sha256

from alembic import op
from sqlalchemy import Boolean, CheckConstraint, Column, Date, DateTime, Float, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, inspect, text

# revision identifiers, used by Alembic.
revision = 'wps_0023_021_factor_library_lifecycle'
down_revision = 'wps_0801_001_universe_incremental_index'
branch_labels = None
depends_on = None


# The original lifecycle revision was squashed with the external-data table
# creation below.  Keep the system factor initialization here so an existing
# metadata-first database and a migration-first database receive identical
# lifecycle state and audit history.
_SYSTEM_FACTOR_KIND = {
    "ep_ttm": "continuous",
    "negative_pb": "continuous",
    "roe_yoy_growth": "continuous",
    "main_inflow_5d_ratio": "continuous",
    "turnover_z20": "continuous",
    "lhb_institution_net_ratio": "event",
    "hot_rank_attention": "event",
    "tail_accumulation_proxy": "continuous",
}
_SYSTEM_FACTOR_CODES = tuple(sorted(_SYSTEM_FACTOR_KIND))
_MIGRATION_NOTE = "wps_0023_021_factor_library_lifecycle"
_LEGACY_FACTOR_SET_ID = "legacy-system-v1"


def _table_exists(table_name: str) -> bool:
    return table_name in inspect(op.get_bind()).get_table_names()


def _column_exists(table_name: str, column_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    return column_name in {
        column["name"] for column in inspect(op.get_bind()).get_columns(table_name)
    }


def _system_factor_rows():
    bind = op.get_bind()
    placeholders = ", ".join(f":code_{index}" for index in range(len(_SYSTEM_FACTOR_CODES)))
    params = {f"code_{index}": code for index, code in enumerate(_SYSTEM_FACTOR_CODES)}
    return bind.execute(
        text(
            "SELECT f.id, f.code, f.active_version_id, "
            "fv.id AS version_id, fv.version AS version_no "
            "FROM factors f "
            "LEFT JOIN factor_versions fv "
            "ON fv.factor_id = f.id AND fv.is_latest = 1 "
            f"WHERE f.code IN ({placeholders}) ORDER BY f.code"
        ),
        params,
    ).mappings().all()


def _backfill_system_factor_lifecycle() -> None:
    """Initialize legacy system factors without overwriting user governance."""
    required_tables = ("factors", "factor_versions", "factor_transition_audits", "factor_sets", "factor_set_members")
    if not all(_table_exists(table_name) for table_name in required_tables):
        return
    if not all(_column_exists("factors", name) for name in ("lifecycle_status", "origin", "factor_kind", "active_version_id")):
        return

    bind = op.get_bind()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    rows = _system_factor_rows()
    for row in rows:
        factor_id = row["id"]
        code = row["code"]
        version_id = row["version_id"]
        bind.execute(
            text(
                "UPDATE factors SET origin = :origin, lifecycle_status = :status, "
                "factor_kind = :kind, risk_level = :risk, asset_scope_json = :scope "
                "WHERE id = :factor_id AND lifecycle_status IS NULL"
            ),
            {
                "origin": "system", "status": "active", "kind": _SYSTEM_FACTOR_KIND[code],
                "risk": "medium", "scope": '[\"cn-stock\"]', "factor_id": factor_id,
            },
        )
        if version_id is not None and _column_exists("factor_versions", "validation_status"):
            bind.execute(
                text(
                    "UPDATE factor_versions SET validation_status = :status, "
                    "created_by = :created_by, created_via = :created_via, "
                    "compiler_version = :compiler_version "
                    "WHERE id = :version_id AND validation_status IS NULL"
                ),
                {
                    "status": "valid", "created_by": "system", "created_via": "import",
                    "compiler_version": "legacy-v1", "version_id": version_id,
                },
            )
        if version_id is not None:
            bind.execute(
                text(
                    "UPDATE factors SET active_version_id = :version_id "
                    "WHERE id = :factor_id AND active_version_id IS NULL"
                ),
                {"version_id": version_id, "factor_id": factor_id},
            )
        exists = bind.execute(
            text(
                "SELECT 1 FROM factor_transition_audits "
                "WHERE factor_id = :factor_id AND migration_note = :note"
            ),
            {"factor_id": factor_id, "note": _MIGRATION_NOTE},
        ).first()
        if not exists:
            bind.execute(
                text(
                    "INSERT INTO factor_transition_audits "
                    "(factor_id, factor_version_id, from_status, to_status, actor, reason, migration_note, created_at) "
                    "VALUES (:factor_id, :version_id, NULL, 'active', 'system_migration', :reason, :note, :created_at)"
                ),
                {
                    "factor_id": factor_id, "version_id": version_id,
                    "reason": "Alembic lifecycle backfill for system factor",
                    "note": _MIGRATION_NOTE, "created_at": now,
                },
            )

    existing_set = bind.execute(
        text("SELECT 1 FROM factor_sets WHERE id = :factor_set_id"),
        {"factor_set_id": _LEGACY_FACTOR_SET_ID},
    ).first()
    if not existing_set:
        bind.execute(
            text(
                "INSERT INTO factor_sets "
                "(id, name, description, status, frozen_at, created_by, created_at, updated_at) "
                "VALUES (:id, :name, :description, 'frozen', :now, 'system_migration', :now, :now)"
            ),
            {
                "id": _LEGACY_FACTOR_SET_ID,
                "name": "Legacy System Factors v1",
                "description": "System factors frozen by lifecycle migration",
                "now": now,
            },
        )

    members_for_hash: list[str] = []
    for display_order, row in enumerate(rows):
        version_id = row["version_id"]
        if version_id is None:
            continue
        factor_id = row["id"]
        code = row["code"]
        version_no = row["version_no"]
        members_for_hash.append(f"{code}:{version_no}")
        exists = bind.execute(
            text(
                "SELECT 1 FROM factor_set_members "
                "WHERE factor_set_id = :factor_set_id AND factor_id = :factor_id"
            ),
            {"factor_set_id": _LEGACY_FACTOR_SET_ID, "factor_id": factor_id},
        ).first()
        if not exists:
            bind.execute(
                text(
                    "INSERT INTO factor_set_members "
                    "(factor_set_id, factor_id, factor_version_id, factor_code, factor_version, role, display_order, missing_policy, created_at) "
                    "VALUES (:factor_set_id, :factor_id, :version_id, :code, :version_no, 'feature', :display_order, 'exclude', :created_at)"
                ),
                {
                    "factor_set_id": _LEGACY_FACTOR_SET_ID, "factor_id": factor_id,
                    "version_id": version_id, "code": code, "version_no": version_no,
                    "display_order": display_order, "created_at": now,
                },
            )
    content_hash = sha256("|".join(sorted(members_for_hash)).encode("utf-8")).hexdigest()
    bind.execute(
        text("UPDATE factor_sets SET content_hash = :content_hash, updated_at = :now WHERE id = :id"),
        {"content_hash": content_hash, "now": now, "id": _LEGACY_FACTOR_SET_ID},
    )


def upgrade() -> None:
    op.create_table(
        'hot_rank_snapshots',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('snapshot_date', String(32), nullable=False),
        Column('symbol', String(32), nullable=False),
        Column('name', String(128), nullable=True),
        Column('rank_no', Integer, nullable=False),
        Column('rank_source', String(32), nullable=False),
        Column('heat_score', Float, nullable=True),
        Column('price_change_pct', Float, nullable=True),
        Column('volume_ratio', Float, nullable=True),
        Column('turnover_rate', Float, nullable=True),
        Column('metadata_json', Text, nullable=True),
        Column('created_at', DateTime, nullable=False),
    )
    op.create_index('ix_hot_rank_snapshots_snapshot_date', 'hot_rank_snapshots', ['snapshot_date'])
    op.create_index('ix_hot_rank_snapshots_symbol', 'hot_rank_snapshots', ['symbol'])
    op.create_index('ix_hot_rank_snapshots_rank_source', 'hot_rank_snapshots', ['rank_source'])

    op.create_table(
        'lhb_institution_trades',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('trade_date', String(32), nullable=False),
        Column('symbol', String(32), nullable=False),
        Column('name', String(128), nullable=True),
        Column('institution_name', String(256), nullable=True),
        Column('institution_type', String(32), nullable=True),
        Column('side', String(16), nullable=False),
        Column('net_amount', Float, nullable=True),
        Column('buy_amount', Float, nullable=True),
        Column('sell_amount', Float, nullable=True),
        Column('close_price', Float, nullable=True),
        Column('price_change_pct', Float, nullable=True),
        Column('created_at', DateTime, nullable=False),
    )
    op.create_index('ix_lhb_institution_trades_trade_date', 'lhb_institution_trades', ['trade_date'])
    op.create_index('ix_lhb_institution_trades_symbol', 'lhb_institution_trades', ['symbol'])
    op.create_index('ix_lhb_institution_trades_institution_type', 'lhb_institution_trades', ['institution_type'])

    op.create_table(
        'stock_valuations',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('symbol_id', Integer, nullable=False),
        Column('trade_date', Date, nullable=False),
        Column('pe_ratio', Float, nullable=True),
        Column('pe_ratio_ttm', Float, nullable=True),
        Column('pb_ratio', Float, nullable=True),
        Column('ps_ratio', Float, nullable=True),
        Column('pcf_ratio', Float, nullable=True),
        Column('market_cap', Float, nullable=True),
        Column('circ_market_cap', Float, nullable=True),
        Column('created_at', DateTime, nullable=False),
    )
    op.create_index('ix_stock_valuations_symbol_id', 'stock_valuations', ['symbol_id'])
    op.create_index('ix_stock_valuations_trade_date', 'stock_valuations', ['trade_date'])

    op.create_table(
        'capital_flows',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('symbol_id', Integer, nullable=False),
        Column('trade_date', Date, nullable=False),
        Column('main_net_inflow', Float, nullable=True),
        Column('super_large_net_inflow', Float, nullable=True),
        Column('large_net_inflow', Float, nullable=True),
        Column('medium_net_inflow', Float, nullable=True),
        Column('small_net_inflow', Float, nullable=True),
        Column('northbound_net_inflow', Float, nullable=True),
        Column('created_at', DateTime, nullable=False),
    )
    op.create_index('ix_capital_flows_symbol_id', 'capital_flows', ['symbol_id'])
    op.create_index('ix_capital_flows_trade_date', 'capital_flows', ['trade_date'])

    op.create_table(
        'etf_indicators',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('symbol_id', Integer, nullable=False),
        Column('trade_date', Date, nullable=False),
        Column('nav', Float, nullable=True),
        Column('premium_discount_rate', Float, nullable=True),
        Column('tracking_error', Float, nullable=True),
        Column('liquidity_score', Float, nullable=True),
        Column('discount_7d_avg', Float, nullable=True),
        Column('created_at', DateTime, nullable=False),
    )
    op.create_index('ix_etf_indicators_symbol_id', 'etf_indicators', ['symbol_id'])
    op.create_index('ix_etf_indicators_trade_date', 'etf_indicators', ['trade_date'])

    op.create_table(
        'financial_reports',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('symbol_id', Integer, nullable=False),
        Column('report_date', String(32), nullable=False),
        Column('report_type', String(16), nullable=False),
        Column('revenue', Float, nullable=True),
        Column('net_profit', Float, nullable=True),
        Column('operating_cashflow', Float, nullable=True),
        Column('total_assets', Float, nullable=True),
        Column('total_liabilities', Float, nullable=True),
        Column('roe', Float, nullable=True),
        Column('roa', Float, nullable=True),
        Column('gross_margin', Float, nullable=True),
        Column('created_at', DateTime, nullable=False),
    )
    op.create_index('ix_financial_reports_symbol_id', 'financial_reports', ['symbol_id'])
    op.create_index('ix_financial_reports_report_date', 'financial_reports', ['report_date'])
    op.create_index('ix_financial_reports_report_type', 'financial_reports', ['report_type'])

    op.create_table(
        'tail_accumulation_snapshots',
        Column('id', Integer, primary_key=True, autoincrement=True),
        Column('snapshot_date', String(32), nullable=False),
        Column('symbol', String(32), nullable=False),
        Column('accumulation_days', Integer, nullable=False),
        Column('accumulation_score', Float, nullable=False),
        Column('price_change_pct', Float, nullable=True),
        Column('volume_change_pct', Float, nullable=True),
        Column('institution_presence', Integer, nullable=True),
        Column('metadata_json', Text, nullable=True),
        Column('created_at', DateTime, nullable=False),
    )
    op.create_index('ix_tail_accumulation_snapshots_snapshot_date', 'tail_accumulation_snapshots', ['snapshot_date'])
    op.create_index('ix_tail_accumulation_snapshots_symbol', 'tail_accumulation_snapshots', ['symbol'])

    _backfill_system_factor_lifecycle()


def downgrade() -> None:
    op.drop_index('ix_tail_accumulation_snapshots_symbol', table_name='tail_accumulation_snapshots')
    op.drop_index('ix_tail_accumulation_snapshots_snapshot_date', table_name='tail_accumulation_snapshots')
    op.drop_table('tail_accumulation_snapshots')

    op.drop_index('ix_financial_reports_report_type', table_name='financial_reports')
    op.drop_index('ix_financial_reports_report_date', table_name='financial_reports')
    op.drop_index('ix_financial_reports_symbol_id', table_name='financial_reports')
    op.drop_table('financial_reports')

    op.drop_index('ix_etf_indicators_trade_date', table_name='etf_indicators')
    op.drop_index('ix_etf_indicators_symbol_id', table_name='etf_indicators')
    op.drop_table('etf_indicators')

    op.drop_index('ix_capital_flows_trade_date', table_name='capital_flows')
    op.drop_index('ix_capital_flows_symbol_id', table_name='capital_flows')
    op.drop_table('capital_flows')

    op.drop_index('ix_stock_valuations_trade_date', table_name='stock_valuations')
    op.drop_index('ix_stock_valuations_symbol_id', table_name='stock_valuations')
    op.drop_table('stock_valuations')

    op.drop_index('ix_lhb_institution_trades_institution_type', table_name='lhb_institution_trades')
    op.drop_index('ix_lhb_institution_trades_symbol', table_name='lhb_institution_trades')
    op.drop_index('ix_lhb_institution_trades_trade_date', table_name='lhb_institution_trades')
    op.drop_table('lhb_institution_trades')

    op.drop_index('ix_hot_rank_snapshots_rank_source', table_name='hot_rank_snapshots')
    op.drop_index('ix_hot_rank_snapshots_symbol', table_name='hot_rank_snapshots')
    op.drop_index('ix_hot_rank_snapshots_snapshot_date', table_name='hot_rank_snapshots')
    op.drop_table('hot_rank_snapshots')
