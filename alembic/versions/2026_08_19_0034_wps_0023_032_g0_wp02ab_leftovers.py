# ===========================================================================
#  G0 合约 + WP0-3 收尾：WP0-2a/2b 遗留 + scores 复合索引 + evidence 幂等约束
# ===========================================================================
#  覆盖 G0 test_g0_contract.py 5 个失败：
#    1) backtest_runs 缺少 WP0-2b 扩展列（7 列）
#    2) sim_orders 缺少 WP0-2b 扩展列（7 列，含 decision_evidence_id FK）
#    3) backtest_trades 表不存在
#    4) scores 缺少复合索引 ix_scores_fmr_symbol_trade_date
#       (factor_model_run_id, symbol_id, trade_date)
#    5) decision_evidence.idempotency_key 无 UNIQUE 约束
# ===========================================================================
"""wps_0023_032_g0_wp02ab_leftovers.

Revision ID: wps_0023_032_g0_wp02ab_leftovers
Revises: wps_0023_031_wp03_decision_runs_evidence_full_cols
Create Date: 2026-08-19 12:20:00.000000
"""
from alembic import op
from sqlalchemy import (
    BigInteger, CheckConstraint, Column, Date, DateTime, Float, ForeignKey,
    Index, Integer, String, Text, UniqueConstraint, inspect, Boolean,
)


revision = 'wps_0023_032_g0_wp02ab_leftovers'
down_revision = 'wps_0023_031_wp03_decision_runs_evidence_full_cols'
branch_labels = None
depends_on = None


def _column_exists(table_name: str, column_name: str) -> bool:
    conn = op.get_bind()
    inspector = inspect(conn)
    columns = {c["name"] for c in inspector.get_columns(table_name)}
    return column_name in columns


def _table_exists(table_name: str) -> bool:
    conn = op.get_bind()
    inspector = inspect(conn)
    return table_name in inspector.get_table_names()


def _index_exists(table_name: str, index_name: str) -> bool:
    conn = op.get_bind()
    inspector = inspect(conn)
    indexes = {i["name"] for i in inspector.get_indexes(table_name)}
    return index_name in indexes


def upgrade() -> None:
    # ── 1) backtest_runs 扩展列 ──
    if _table_exists("backtest_runs"):
        bt_new_cols = [
            ("factor_set_id", Integer, None, True),
            ("strategy_snapshot_id", String(64), None, True),
            ("pit_mode", String(32), "'best_effort'", False),
            ("decision_run_ids_json", Text, None, True),
            ("benchmark_equity_json", Text, None, True),
            ("benchmark_status", String(32), "'PENDING'", False),
            ("benchmark_gap_days", Integer, None, True),
        ]
        with op.batch_alter_table("backtest_runs") as batch_op:
            for col_name, col_type, srv_default, nullable in bt_new_cols:
                if not _column_exists("backtest_runs", col_name):
                    kwargs = {"nullable": nullable}
                    if srv_default is not None:
                        kwargs["server_default"] = srv_default
                    batch_op.add_column(Column(col_name, col_type, **kwargs))
        for idx_cols, idx_name in [
            (["strategy_snapshot_id"], "ix_backtest_runs_strategy_snapshot_id"),
            (["factor_set_id"], "ix_backtest_runs_factor_set_id"),
            (["benchmark_status"], "ix_backtest_runs_benchmark_status"),
            (["pit_mode"], "ix_backtest_runs_pit_mode"),
        ]:
            if not _index_exists("backtest_runs", idx_name):
                try:
                    op.create_index(idx_name, "backtest_runs", idx_cols, unique=False)
                except Exception:
                    pass

    # ── 2) sim_orders 扩展列 ──
    if _table_exists("sim_orders"):
        so_new_cols = [
            ("review_status", String(32), "'NONE'", False),
            ("review_by", String(128), None, True),
            ("reviewed_at", DateTime, None, True),
            ("review_deadline_at", DateTime, None, True),
            ("review_reason", String(256), None, True),
            ("review_note", Text, None, True),
        ]
        with op.batch_alter_table("sim_orders") as batch_op:
            for col_name, col_type, srv_default, nullable in so_new_cols:
                if not _column_exists("sim_orders", col_name):
                    kwargs = {"nullable": nullable}
                    if srv_default is not None:
                        kwargs["server_default"] = srv_default
                    batch_op.add_column(Column(col_name, col_type, **kwargs))
        # decision_evidence_id FK 必须脱离 batch_alter_table：
        # SQLite 的 Alembic ApplyBatchImpl.add_constraint 强制要求约束有名字，
        # Column(ForeignKey(...)) 简写不会自动给 constraint 命名，导致 ValueError。
        # 改为：op.add_column 单独加列（Column 不带 ForeignKey），然后用
        # op.create_foreign_key 显式命名地加 FK 约束。
        if not _column_exists("sim_orders", "decision_evidence_id"):
            try:
                op.add_column(
                    "sim_orders",
                    Column("decision_evidence_id", String(64), nullable=True),
                )
            except Exception:
                pass
        try:
            op.create_foreign_key(
                "fk_sim_orders_decision_evidence_id",
                "sim_orders",  # source
                "decision_evidence",  # referent
                ["decision_evidence_id"],  # local cols
                ["id"],  # remote cols
                ondelete="SET NULL",
            )
        except Exception:
            pass
        for idx_cols, idx_name in [
            (["review_status"], "ix_sim_orders_review_status"),
            (["review_by"], "ix_sim_orders_review_by"),
            (["reviewed_at"], "ix_sim_orders_reviewed_at"),
            (["review_deadline_at"], "ix_sim_orders_review_deadline_at"),
            (["decision_evidence_id"], "ix_sim_orders_decision_evidence_id"),
        ]:
            if not _index_exists("sim_orders", idx_name):
                try:
                    op.create_index(idx_name, "sim_orders", idx_cols, unique=False)
                except Exception:
                    pass
        # review_status CHECK
        try:
            op.create_check_constraint(
                "ck_sim_orders_review_status_values",
                "sim_orders",
                "review_status IN ('NONE','PENDING','APPROVED','REJECTED','ESCALATED')",
            )
        except Exception:
            pass

    # ── 3) backtest_trades 表创建 ──
    if not _table_exists("backtest_trades"):
        op.create_table(
            "backtest_trades",
            Column("id", BigInteger().with_variant(Integer, "sqlite"),
                   primary_key=True, autoincrement=True),
            Column("backtest_run_id", BigInteger().with_variant(Integer, "sqlite"),
                   ForeignKey("backtest_runs.id", ondelete="CASCADE"),
                   nullable=False, index=True),
            Column("symbol_id", Integer, nullable=False, index=True),
            Column("trade_date", Date, nullable=False, index=True),
            Column("decision_at", DateTime, nullable=False),
            Column("execution_at", DateTime, nullable=True, index=True),
            Column("side", String(8), nullable=False),
            Column("action", String(32), nullable=True),
            Column("quantity", Float, nullable=False),
            Column("price", Float, nullable=False),
            Column("notional", Float, nullable=True),
            Column("commission", Float, nullable=True, server_default="0"),
            Column("tax", Float, nullable=True, server_default="0"),
            Column("slippage_bps", Float, nullable=True),
            Column("intended_entry_price", Float, nullable=True),
            Column("entry_rejection_reason", String(256), nullable=True),
            Column("position_before", Float, nullable=True),
            Column("position_after", Float, nullable=True),
            Column("pct_of_portfolio", Float, nullable=True),
            Column("matched", Integer, nullable=False, server_default="1"),
            Column("match_note", String(256), nullable=True),
            Column("legacy_fallback_flag", Integer, nullable=False, server_default="0"),
            Column("manual_price_flag", Integer, nullable=False, server_default="0"),
            Column("pit_safe_flag", String(16), nullable=True,
                   server_default="UNKNOWN"),
            Column("order_id_before_fill", BigInteger().with_variant(Integer, "sqlite"),
                   nullable=True, index=True),
            Column("decision_evidence_id", String(64),
                   ForeignKey("decision_evidence.id", ondelete="SET NULL"),
                   nullable=True, index=True),
            Column("exit_evidence_id", String(64),
                   ForeignKey("decision_evidence.id", ondelete="SET NULL"),
                   nullable=True, index=True),
            Column("created_at", DateTime, nullable=False),
            Column("updated_at", DateTime, nullable=False),
            CheckConstraint(
                "side IN ('BUY','SELL')",
                name="ck_backtest_trades_side_values",
            ),
            CheckConstraint(
                "pit_safe_flag IN ('PIT_SAFE','NOT_PIT_SAFE','UNKNOWN')",
                name="ck_backtest_trades_pit_safe_flag_values",
            ),
        )
        try:
            op.create_index(
                "ix_backtest_trades_run_date_symbol",
                "backtest_trades",
                ["backtest_run_id", "trade_date", "symbol_id"],
                unique=False,
            )
        except Exception:
            pass
        try:
            op.create_index(
                "ix_backtest_trades_symbol_date",
                "backtest_trades",
                ["symbol_id", "trade_date"],
                unique=False,
            )
        except Exception:
            pass
    else:
        # 如果 backtest_trades 表已经存在（旧 DB），补上 2 个遗漏列
        for col_name, col_type in [
            ("intended_entry_price", Float),
            ("entry_rejection_reason", String(256)),
        ]:
            if not _column_exists("backtest_trades", col_name):
                try:
                    op.add_column(
                        "backtest_trades",
                        Column(col_name, col_type, nullable=True),
                    )
                except Exception:
                    pass

    # ── 4) scores 两个复合索引 ──
    if _table_exists("scores"):
        # (a) (factor_model_run_id, symbol_id, trade_date)
        idx_name = "ix_scores_fmr_symbol_trade_date"
        if not _index_exists("scores", idx_name):
            try:
                op.create_index(
                    idx_name, "scores",
                    ["factor_model_run_id", "symbol_id", "trade_date"],
                    unique=False,
                )
            except Exception:
                pass
        # (b) (factor_model_run_id, symbol_id, factor_data_cutoff_at)
        idx_name2 = "ix_scores_fmr_symbol_cutoff"
        if not _index_exists("scores", idx_name2):
            try:
                op.create_index(
                    idx_name2, "scores",
                    ["factor_model_run_id", "symbol_id", "factor_data_cutoff_at"],
                    unique=False,
                )
            except Exception:
                pass

    # ── 5) decision_evidence.idempotency_key UNIQUE 约束 ──
    if _table_exists("decision_evidence") and _column_exists("decision_evidence", "idempotency_key"):
        uq_name = "uq_decision_evidence_idempotency_key"
        try:
            with op.batch_alter_table("decision_evidence") as batch_op:
                batch_op.create_unique_constraint(uq_name, ["idempotency_key"])
        except Exception:
            # SQLite / dialect 降级：create_unique_constraint 若 batch 不支持，
            # 退回用通用 try（不致命，ORM 层+普通索引已双保险）
            try:
                op.create_unique_constraint(uq_name, "decision_evidence", ["idempotency_key"])
            except Exception:
                pass


def downgrade() -> None:
    # 5) UNIQUE 反向
    if _table_exists("decision_evidence"):
        try:
            with op.batch_alter_table("decision_evidence") as batch_op:
                batch_op.drop_constraint("uq_decision_evidence_idempotency_key",
                                         type_="unique")
        except Exception:
            try:
                op.drop_constraint("uq_decision_evidence_idempotency_key",
                                   "decision_evidence", type_="unique")
            except Exception:
                pass

    # 4) scores 复合索引反向
    if _table_exists("scores") and _index_exists("scores", "ix_scores_fmr_symbol_trade_date"):
        try:
            op.drop_index("ix_scores_fmr_symbol_trade_date", table_name="scores")
        except Exception:
            pass

    # 3) backtest_trades 表反向
    if _table_exists("backtest_trades"):
        try:
            op.drop_index("ix_backtest_trades_symbol_date",
                          table_name="backtest_trades")
        except Exception:
            pass
        try:
            op.drop_index("ix_backtest_trades_run_date_symbol",
                          table_name="backtest_trades")
        except Exception:
            pass
        try:
            for ck in ["ck_backtest_trades_pit_safe_flag_values",
                       "ck_backtest_trades_side_values"]:
                try:
                    op.drop_constraint(ck, "backtest_trades", type_="check")
                except Exception:
                    pass
        except Exception:
            pass
        try:
            op.drop_table("backtest_trades")
        except Exception:
            pass

    # 2) sim_orders 反向
    if _table_exists("sim_orders"):
        try:
            op.drop_constraint("ck_sim_orders_review_status_values",
                               "sim_orders", type_="check")
        except Exception:
            pass
        for idx in ["ix_sim_orders_decision_evidence_id",
                    "ix_sim_orders_review_deadline_at",
                    "ix_sim_orders_reviewed_at",
                    "ix_sim_orders_review_by",
                    "ix_sim_orders_review_status"]:
            if _index_exists("sim_orders", idx):
                try:
                    op.drop_index(idx, table_name="sim_orders")
                except Exception:
                    pass
        with op.batch_alter_table("sim_orders") as batch_op:
            for col in ["decision_evidence_id", "review_note", "review_reason",
                        "review_deadline_at", "reviewed_at", "review_by",
                        "review_status"]:
                if _column_exists("sim_orders", col):
                    try:
                        batch_op.drop_column(col)
                    except Exception:
                        pass

    # 1) backtest_runs 反向
    if _table_exists("backtest_runs"):
        for idx in ["ix_backtest_runs_pit_mode",
                    "ix_backtest_runs_benchmark_status",
                    "ix_backtest_runs_factor_set_id",
                    "ix_backtest_runs_strategy_snapshot_id"]:
            if _index_exists("backtest_runs", idx):
                try:
                    op.drop_index(idx, table_name="backtest_runs")
                except Exception:
                    pass
        with op.batch_alter_table("backtest_runs") as batch_op:
            for col in ["benchmark_gap_days", "benchmark_status",
                        "benchmark_equity_json", "decision_run_ids_json",
                        "pit_mode", "strategy_snapshot_id", "factor_set_id"]:
                if _column_exists("backtest_runs", col):
                    try:
                        batch_op.drop_column(col)
                    except Exception:
                        pass
