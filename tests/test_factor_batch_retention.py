from datetime import date, datetime, timedelta

from app.services.factors.store import FactorWarehouse


import pytest

pytestmark = pytest.mark.whitebox
def _factor_row(batch_id: str, created_at: datetime):
    return {
        "symbol": "600000",
        "trade_date": date(2026, 8, 1),
        "factor_code": "value_pe",
        "factor_version": 1,
        "raw_value": 1.0,
        "winsorized_value": 1.0,
        "normalized_value": 0.0,
        "is_imputed": False,
        "imputation_method": None,
        "eligible": True,
        "data_cutoff_at": created_at,
        "calc_batch_id": batch_id,
        "created_at": created_at,
    }


def _target_row(batch_id: str, created_at: datetime):
    return {
        "symbol": "600000",
        "signal_date": date(2026, 8, 1),
        "entry_date": date(2026, 8, 2),
        "exit_date": date(2026, 8, 7),
        "target_code": "target_5d_return",
        "target_value": 0.1,
        "is_tradable": True,
        "invalid_reason": None,
        "calc_batch_id": batch_id,
        "created_at": created_at,
    }


def test_prune_keeps_recent_batches_and_explicit_protected_batch(tmp_path):
    warehouse = FactorWarehouse(tmp_path / "retention.duckdb")
    base = datetime(2026, 8, 1, 10, 0, 0)
    for index in range(4):
        created = base + timedelta(days=index)
        batch = f"batch-{index}"
        warehouse.upsert_records("factor_values", [_factor_row(batch, created)])
        warehouse.upsert_records("factor_targets", [_target_row(batch, created)])

    result = warehouse.prune_calculation_batches(
        keep_batches=2,
        protected_batch_ids=["batch-0"],
    )

    assert result["deleted_batch_ids"] == ["batch-1"]
    assert result["factor_value_rows_deleted"] == 1
    assert result["factor_target_rows_deleted"] == 1
    with warehouse.connection(read_only=True) as conn:
        remaining = {
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT calc_batch_id FROM factor_values"
            ).fetchall()
        }
    assert remaining == {"batch-0", "batch-2", "batch-3"}


def test_prune_does_not_delete_when_within_retention(tmp_path):
    warehouse = FactorWarehouse(tmp_path / "retention-small.duckdb")
    warehouse.upsert_records(
        "factor_values",
        [_factor_row("only", datetime(2026, 8, 1, 10, 0, 0))],
    )
    result = warehouse.prune_calculation_batches(keep_batches=2)
    assert result["deleted_batch_ids"] == []
    assert result["factor_value_rows_deleted"] == 0


# ---------- VIZ-0930-32：模型引用的批次必须进保护清单 ----------

import json


def _model_run(run_id: str, factor_batch: str, target_batch: str):
    from app.models.factor_model import FactorModelRun

    return FactorModelRun(
        id=run_id,
        model_type="ridge",
        asset_type="stock",
        target_code="target_5d_return",
        hyperparameters_json=json.dumps({
            "factor_calc_batch_id": factor_batch,
            "target_calc_batch_id": target_batch,
            "window_days": 250,
        }),
    )


def test_model_referenced_batch_ids_reads_both_keys(db_session):
    from app.services.factors.pipeline_task import _model_referenced_batch_ids

    db_session.add(_model_run("mdl-b4-a", "factors-b4-a", "targets-b4-a"))
    db_session.commit()

    ids = _model_referenced_batch_ids(db_session)
    assert {"factors-b4-a", "targets-b4-a"} <= ids


def test_model_referenced_batch_ids_survives_bad_json(db_session):
    """脏 hyperparameters_json 不能把保留清理整条链路带崩。"""
    from app.models.factor_model import FactorModelRun
    from app.services.factors.pipeline_task import _model_referenced_batch_ids

    db_session.add(FactorModelRun(
        id="mdl-b4-bad", model_type="ridge", asset_type="stock",
        target_code="target_5d_return", hyperparameters_json="{不是 json",
    ))
    db_session.commit()

    assert isinstance(_model_referenced_batch_ids(db_session), set)


def test_retention_protected_ids_union_score_and_model_batches(db_session):
    """保护集必须同时含 Score 引用批次与模型 identity 引用批次（含 target 侧）。

    原 bug 就长在组装上：只用了 Score 的 factor_calc_batch_id，target 批次一个没进。
    """
    from app.services.factors.pipeline_task import retention_protected_batch_ids

    db_session.add(_model_run("mdl-b4-d", "factors-model-d", "targets-model-d"))
    db_session.commit()

    protected = retention_protected_batch_ids(
        db_session, {"protected_factor_batch_ids": ["factors-score-d"]}
    )
    assert protected == sorted(
        ["factors-score-d", "factors-model-d", "targets-model-d"])


def test_prune_keeps_target_batch_referenced_by_model(tmp_path, db_session):
    """按数量淘汰不许删掉"某个已训练模型当年用的那份标签批次"。"""
    from app.services.factors.pipeline_task import retention_protected_batch_ids

    warehouse = FactorWarehouse(tmp_path / "retention-model.duckdb")
    referenced = "targets-model-referenced"
    base = datetime(2026, 1, 1)
    warehouse.upsert_records("factor_targets", [
        _target_row(referenced, base),                      # 最旧
        _target_row("targets-fresh", base + timedelta(days=5)),
    ])
    db_session.add(_model_run("mdl-b4-c", "factors-x", referenced))
    db_session.commit()

    protected = retention_protected_batch_ids(db_session, {"protected_factor_batch_ids": []})
    result = warehouse.prune_calculation_batches(keep_batches=1,
                                                 protected_batch_ids=protected)

    assert referenced in result["retained_batch_ids"]
    assert referenced not in result["deleted_batch_ids"]
    with warehouse.connection(read_only=True) as conn:
        still = conn.execute(
            "SELECT COUNT(*) FROM factor_targets WHERE calc_batch_id = ?",
            [referenced],
        ).fetchone()[0]
    assert still == 1

