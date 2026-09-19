"""Persist immutable DecisionOrderPlan intents independently of evidence JSON.

Revision ID: wps_0023_042_decision_order_plans
Revises: wps_0023_041_g6_rollout_audit_actions
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "wps_0023_042_decision_order_plans"
down_revision = "wps_0023_041_g6_rollout_audit_actions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "decision_order_plans" in inspector.get_table_names():
        return
    op.create_table(
        "decision_order_plans",
        sa.Column("order_plan_id", sa.String(64), primary_key=True),
        sa.Column("decision_run_id", sa.String(64), sa.ForeignKey("decision_runs.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("evidence_id", sa.String(64), sa.ForeignKey("decision_evidence.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("portfolio_id", sa.Integer, sa.ForeignKey("portfolios.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("symbol_id", sa.Integer, sa.ForeignKey("symbols.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("action", sa.String(24), nullable=False),
        sa.Column("signal_date", sa.Date, nullable=False),
        sa.Column("execution_date", sa.Date, nullable=False),
        sa.Column("target_quantity", sa.Float, nullable=False),
        sa.Column("direction", sa.String(8), nullable=True),
        sa.Column("intended_price", sa.Float, nullable=True),
        sa.Column("reason_code", sa.String(64), nullable=True),
        sa.Column("rejection_trace_json", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.UniqueConstraint("evidence_id", name="uq_decision_order_plans_evidence"),
    )
    for name, columns in (
        ("ix_decision_order_plans_decision_run_id", ["decision_run_id"]),
        ("ix_decision_order_plans_evidence_id", ["evidence_id"]),
        ("ix_decision_order_plans_portfolio_id", ["portfolio_id"]),
        ("ix_decision_order_plans_symbol_id", ["symbol_id"]),
        ("ix_decision_order_plans_action", ["action"]),
        ("ix_decision_order_plans_signal_date", ["signal_date"]),
        ("ix_decision_order_plans_execution_date", ["execution_date"]),
        ("ix_decision_order_plans_reason_code", ["reason_code"]),
        ("ix_decision_order_plans_created_at", ["created_at"]),
        ("ix_decision_order_plans_run_execution", ["decision_run_id", "execution_date"]),
    ):
        op.create_index(name, "decision_order_plans", columns, unique=False)


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "decision_order_plans" not in inspector.get_table_names():
        return
    for item in inspector.get_indexes("decision_order_plans"):
        if item.get("name"):
            op.drop_index(item["name"], table_name="decision_order_plans")
    op.drop_table("decision_order_plans")
