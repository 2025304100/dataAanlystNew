"""Compensate already-applied 0079-0082 without deleting historical facts."""
from alembic import op

revision = "wps_0024_083_combination_contracts"
down_revision = "wps_0024_082_status_dt6"
branch_labels = depends_on = None


def upgrade():
    from app.db.migration_contracts_v83 import ensure_member_contract, ensure_idempotency_contract, ensure_backtest_legacy_write_columns, ensure_order_review_contract, ensure_retired_combination_columns
    from app.db.migration_schema_v80 import apply_frozen_schema

    bind = op.get_bind()
    ensure_member_contract(bind)
    ensure_idempotency_contract(bind)
    apply_frozen_schema(bind)
    ensure_backtest_legacy_write_columns(bind)
    ensure_order_review_contract(bind)
    ensure_retired_combination_columns(bind)
    import sqlalchemy as sa
    wanted = ["factor_model_run_id", "symbol_id", "trade_date", "factor_data_cutoff_at", "id"]
    indexes = {i["name"]: i for i in sa.inspect(bind).get_indexes("scores")}
    name = "idx_score_fmr_sym_dt"
    if name in indexes:
        if indexes[name].get("column_names") != wanted or indexes[name].get("unique"):
            raise RuntimeError("idx_score_fmr_sym_dt does not match the approved PIT access path")
    else:
        op.create_index(name, "scores", wanted, unique=False)


def downgrade():
    raise RuntimeError("Retain member uniqueness and idempotency evidence; use a verified recovery plan")
