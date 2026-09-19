"""Deterministic contract for the isolated G3 UI acceptance fixture."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SEED_SCRIPT = REPOSITORY_ROOT / "scripts" / "seed_ui_acceptance.py"


def test_seed_ui_acceptance_provides_the_g3_matrix_data_shape(tmp_path: Path) -> None:
    """The fixture exposes enough real persisted data for every matrix path."""
    database_path = tmp_path / "ui_acceptance.db"
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        filter(None, [str(REPOSITORY_ROOT), environment.get("PYTHONPATH")])
    )
    result = subprocess.run(
        [
            sys.executable,
            str(SEED_SCRIPT),
            "--database-url",
            f"sqlite:///{database_path.as_posix()}",
        ],
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout.strip().splitlines()[-1])

    with sqlite3.connect(database_path) as connection:
        historical_run_count = connection.execute(
            "SELECT COUNT(*) FROM backtest_runs"
        ).fetchone()[0]
        trade_count = connection.execute(
            "SELECT COUNT(*) FROM backtest_trades"
        ).fetchone()[0]
        execution_fill_count = connection.execute(
            "SELECT COUNT(*) FROM backtest_execution_fills"
        ).fetchone()[0]
        valuation_snapshot_count = connection.execute(
            "SELECT COUNT(*) FROM backtest_valuation_snapshots"
        ).fetchone()[0]
        reproducible_run_count = connection.execute(
            "SELECT COUNT(*) FROM backtest_runs WHERE reproducibility_status = 'reproducible'"
        ).fetchone()[0]
        rejected_evidence_count = connection.execute(
            """
            SELECT COUNT(*) FROM decision_evidence
            WHERE action IN ('REJECTED', 'DATA_BLOCKED')
            """
        ).fetchone()[0]
        evidence_versions = [
            json.loads(row[0])
            for row in connection.execute(
                "SELECT versions_json FROM decision_evidence WHERE versions_json IS NOT NULL"
            )
        ]

    partial_fills = [
        versions
        for versions in evidence_versions
        if versions.get("order_plan_status") in {"PARTIAL_FILL", "PARTIAL_FILL_PENDING"}
    ]
    assert historical_run_count >= 3
    assert trade_count >= 126
    assert execution_fill_count >= 126
    assert valuation_snapshot_count > 0
    assert reproducible_run_count == historical_run_count
    assert rejected_evidence_count >= 18
    assert partial_fills
    assert any(
        float(versions["order_plan_filled_quantity"])
        < float(versions["order_plan_requested_quantity"])
        and float(versions["order_plan_remaining_quantity"]) > 0
        for versions in partial_fills
    )

    assert summary["backtest_run_count"] == historical_run_count
    assert summary["trade_count"] == trade_count
    assert summary["execution_fill_count"] == execution_fill_count
    assert summary["rejected_evidence_count"] == rejected_evidence_count
    assert summary["partial_fill_count"] == len(partial_fills)
