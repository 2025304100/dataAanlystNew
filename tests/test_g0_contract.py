"""G0-CONTRACT: immutable contract tests — DB schema, clock, hash, idempotency.

100% must PASS before any G1 work lands. Coverage:
  - [MIG] Alembic upgrade: wps_0031 + wps_0032 against a fresh SQLite DB
  - [MIG] Table presence + required columns for 4 new tables
  - [MIG] Extension columns on backtest_runs/scores/sim_orders/backtest_trades
  - [CLK] Q1: resolve(trade_date) default => decision=15:05 / cutoff=15:00 / execution=T+1 09:30 SH
  - [CLK] Q1: DB stored values are UTC naive; round-trip conversion via clock module only
  - [HSH] Canonical JSON: dict key order + None dropping + datetime ISO is deterministic
  - [HSH] content_hash(a,b) stable across identical calls
  - [IDM] idempotency_key(portfolio_id, snapshot, decision, trade_date, run_type) deterministic
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# -- Import UUTs ---------------------------------------------------------------
from app.core.hash_utils import (
    canonical_json,
    content_hash,
    idempotency_key,
)
from app.services import decision_clock


# ============================================================================
# HASH / IDEMPOTENCY (Q28)
# ============================================================================

class TestHashDeterminism:
    def test_canonical_json_sorts_keys_and_drops_none(self):
        a = {"z": 1, "a": None, "b": {"y": 2, "x": None}}
        b = {"a": None, "b": {"x": None, "y": 2}, "z": 1}
        assert canonical_json(a) == canonical_json(b)
        # None entries must have been removed.
        s = canonical_json(a)
        assert '"a":null' not in s
        assert '"x":null' not in s

    def test_content_hash_identical_inputs_stable(self):
        payloads = [
            {"k": "v", "n": 42, "arr": [1, 2, 3], "dt": date(2025, 1, 6)},
            "extra-prefix-salt",
        ]
        h1 = content_hash(*payloads)
        h2 = content_hash(*payloads)
        assert h1 == h2
        assert len(h1) == 64  # SHA-256 hex

    def test_content_hash_different_inputs_diverge(self):
        a = content_hash({"portfolio": 1}, {"rule_version": 3})
        b = content_hash({"portfolio": 1}, {"rule_version": 4})
        assert a != b

    def test_idempotency_key_deterministic(self):
        kwargs = dict(
            portfolio_id=42,
            strategy_snapshot_id="snap_abc123",
            decision_at=datetime(2025, 1, 6, 7, 5, tzinfo=None),  # UTC naive
            trade_date=date(2025, 1, 6),
            run_type="backtest",
        )
        k1 = idempotency_key(**kwargs)
        k2 = idempotency_key(**kwargs)
        assert k1 == k2
        assert len(k1) == 64

    def test_idempotency_key_semantically_different(self):
        base = dict(
            portfolio_id=1, strategy_snapshot_id="s",
            decision_at=datetime(2025, 1, 6, 7, 5),
            trade_date=date(2025, 1, 6), run_type="backtest",
        )
        k_base = idempotency_key(**base)
        # Different day -> different key
        assert idempotency_key(**{**base, "trade_date": date(2025, 1, 7)}) != k_base
        # Different run type -> different key
        assert idempotency_key(**{**base, "run_type": "auto_sim"}) != k_base


# ============================================================================
# CLOCK / TIMEZONE (Q1)
# ============================================================================

class TestUnifiedClock:
    T = date(2025, 7, 2)  # A Wednesday — T+1 Thu, no weekend crossing edge

    def test_default_schedule_values_shanghai(self):
        c = decision_clock.resolve(self.T, mode="t_day_close")
        # All three stored as UTC naive per module contract
        assert c.decision_at.tzinfo is None
        assert c.data_cutoff_at.tzinfo is None
        assert c.execution_at.tzinfo is None

        sh = c.as_shanghai_dict()
        # Q1.3 defaults
        assert "T15:05:00" in sh["decision_at"]
        assert "T15:00:00" in sh["data_cutoff_at"]
        # Execution: T+1 = July 3rd, 09:30
        assert "2025-07-03T09:30:00" in sh["execution_at"]

    def test_utc_shanghai_round_trip(self):
        # A datetime we know represents Shanghai 2025-07-02 15:05 (UTC+8)
        # = 2025-07-02 07:05 UTC
        utc_naive = datetime(2025, 7, 2, 7, 5, 0, tzinfo=None)
        sh_aware = decision_clock.utc_naive_to_shanghai(utc_naive)
        assert sh_aware.year == 2025 and sh_aware.month == 7 and sh_aware.day == 2
        assert sh_aware.hour == 15 and sh_aware.minute == 5
        # Reverse
        back = decision_clock.shanghai_to_utc_naive(sh_aware)
        assert back == utc_naive
        assert back.tzinfo is None  # DB-safe

    def test_resolve_returns_cutoff_before_decision_utc(self):
        c = decision_clock.resolve(self.T)
        # data_cutoff_at (15:00 SH = 07:00 UTC) < decision_at (15:05 SH = 07:05 UTC)
        assert c.data_cutoff_at < c.decision_at
        # execution_at must be strictly later than decision_at (T+1)
        assert c.execution_at > c.decision_at

    def test_rejects_naive_input_on_sh_to_utc(self):
        with pytest.raises(ValueError, match="tz-aware"):
            decision_clock.shanghai_to_utc_naive(
                datetime(2025, 1, 6, 15, 5, tzinfo=None),
            )

    def test_rejects_aware_input_on_utc_to_sh(self):
        with pytest.raises(ValueError, match="must be UTC naive"):
            decision_clock.utc_naive_to_shanghai(
                datetime(2025, 1, 6, 7, 5, tzinfo=timezone.utc),
            )


# ============================================================================
# MIGRATIONS (WP0-2a / WP0-2b) — run against a temp empty SQLite DB
# ============================================================================

class TestMigrationsApplyAndContract:
    """Run Alembic upgrade head; then probe the created schema."""

    @pytest.fixture(scope="class")
    def migrated_db(self, tmp_path_factory):
        tmp = tmp_path_factory.mktemp("g0_contract")
        db_path = tmp / "contract_test.db"
        db_url = f"sqlite:///{db_path.as_posix()}"

        from alembic.config import Config as AlembicConfig
        from alembic import command as alembic_command
        from sqlalchemy import create_engine

        # env.py reads ALEMBIC_DATABASE_URL first, above settings.database_url.
        mp = pytest.MonkeyPatch()
        mp.setenv("ALEMBIC_DATABASE_URL", db_url)

        ini = PROJECT_ROOT / "alembic.ini"
        cfg = AlembicConfig(str(ini))
        cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
        cfg.set_main_option("sqlalchemy.url", db_url)

        try:
            alembic_command.upgrade(cfg, "head")
        finally:
            mp.undo()

        # Now build an engine on EXACTLY that file for inspection.
        engine = create_engine(db_url, future=True)
        yield db_url, engine
        engine.dispose()

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _inspect(migrated_fxt):
        from sqlalchemy import inspect
        _, engine = migrated_fxt
        return inspect(engine)

    # ----------------------------- 4 new tables exist ----------------------
    def test_four_core_tables_present(self, migrated_db):
        insp = self._inspect(migrated_db)
        tables = set(insp.get_table_names())
        for expected in (
            "portfolio_factor_usages",
            "strategy_execution_snapshots",
            "decision_runs",
            "decision_evidence",
        ):
            assert expected in tables, f"Missing table: {expected}"

    # ----------------------------- not-nullable columns --------------------
    REQUIRED_COLS = {
        "portfolio_factor_usages": [
            "id", "portfolio_id", "factor_model_run_id", "run_mode", "pit_mode",
            "status", "content_hash", "effective_from", "created_at", "updated_at",
        ],
        "strategy_execution_snapshots": [
            "id", "portfolio_id", "decision_clock_json", "member_snapshot_json",
            "universe_type", "snapshot_type", "snapshot_hash",
            "effective_from", "created_at", "created_by",
        ],
        "decision_runs": [
            "id", "strategy_snapshot_id", "portfolio_id", "run_type",
            "trade_date", "decision_at", "data_cutoff_at", "execution_at",
            "run_mode", "pit_mode", "blocking_status", "created_at",
        ],
        "decision_evidence": [
            "id", "decision_run_id", "strategy_snapshot_id", "portfolio_id",
            "symbol_id", "trade_date",
            "decision_at", "data_cutoff_at", "execution_at",
            "action", "pit_safe_flag", "content_hash", "created_at",
        ],
    }

    @pytest.mark.parametrize("table,cols", REQUIRED_COLS.items())
    def test_required_columns_present(self, migrated_db, table, cols):
        insp = self._inspect(migrated_db)
        actual = {c["name"] for c in insp.get_columns(table)}
        missing = [c for c in cols if c not in actual]
        assert not missing, (
            f"Table {table} missing NOT-NULL/required columns: {missing}"
        )

    # ----------------------------- extended existing tables ----------------
    EXTENDED_COLS = {
        "backtest_runs": [
            "factor_set_id", "strategy_snapshot_id", "pit_mode",
            "decision_run_ids_json", "benchmark_equity_json",
            "benchmark_status", "benchmark_gap_days",
        ],
        "scores": [
            "published_at", "first_published_at", "revision_at", "pit_flag",
        ],
        "sim_orders": [
            "review_status", "review_by", "reviewed_at",
            "review_deadline_at", "review_reason", "review_note",
            "decision_evidence_id",
        ],
        "backtest_trades": [
            "decision_evidence_id", "exit_evidence_id",
            "intended_entry_price", "slippage_bps", "entry_rejection_reason",
        ],
    }

    @pytest.mark.parametrize("table,cols", EXTENDED_COLS.items())
    def test_extended_columns_present(self, migrated_db, table, cols):
        insp = self._inspect(migrated_db)
        actual = {c["name"] for c in insp.get_columns(table)}
        missing = [c for c in cols if c not in actual]
        assert not missing, (
            f"Table {table} missing WP0-2b extension columns: {missing}"
        )

    # ----------------------------- required indexes ------------------------
    def test_scores_composite_indexes(self, migrated_db):
        insp = self._inspect(migrated_db)
        idx_names = {i["name"] for i in insp.get_indexes("scores")}
        # Q22.2 mandatory composite indexes
        for need in ("ix_scores_fmr_symbol_trade_date",
                     "ix_scores_fmr_symbol_cutoff"):
            assert need in idx_names, f"Missing Score composite index {need}"

    def test_idempotency_unique_indexes(self, migrated_db):
        insp = self._inspect(migrated_db)
        for tbl in ("strategy_execution_snapshots",
                    "decision_runs", "decision_evidence"):
            uniq_from_constraints = {u["name"]
                                     for u in insp.get_unique_constraints(tbl)}
            # SQLite sometimes exposes UNIQUE on nullable columns only via
            # get_indexes, with unique=True flag set.
            uniq_from_indexes = {i["name"] for i in insp.get_indexes(tbl)
                                 if i.get("unique")}
            all_uniq = uniq_from_constraints | uniq_from_indexes
            assert any("idempotency" in u.lower() for u in all_uniq), (
                f"{tbl} missing unique idempotency key constraint "
                f"(candidates: {sorted(all_uniq)})"
            )
