"""`evaluate_all_impl` 的真实覆盖：它以前只被路由测试 mock 掉，导致函数体里的
NameError 一类问题无处可藏（本次改造中就真的踩到一次）。

这里用临时仓库跑真路径：`load_run` / `finalize_run` 打桩，
`resolve_label_batch` + `build_mining_context` 全部真跑。
"""

from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services.factors.label_batch import LabelCoverageError
from app.services.factors.mining import service as SVC
from app.services.factors.store import (
    SHARED_TARGET_BATCH_ID,
    FactorWarehouse,
)
from app.services.factors.target_engine import TARGET_CODE

pytestmark = pytest.mark.whitebox

START = date(2026, 1, 1)


def _days(count: int, offset: int = 0) -> list[date]:
    return [START + timedelta(days=offset + i) for i in range(count)]


def _seed_bars(warehouse: FactorWarehouse, days: list[date]) -> None:
    with warehouse._write_lock, warehouse.connection() as conn:
        for index, trade_date in enumerate(days):
            for symbol in ("600000", "000001"):
                conn.execute(
                    "INSERT INTO raw_daily_bars "
                    "(symbol, trade_date, adjust, business_symbol_id, "
                    " universe_symbol_id, open, high, low, close, volume, "
                    " amount, turnover_rate, source, source_origin, "
                    " source_row_id, source_updated_at, ingested_at, batch_id) "
                    "VALUES (?, ?, 'qfq', NULL, NULL, 10, 11, 9, 10, 100, "
                    " 1000, 0.5, 'test', 'daily_bars', ?, NULL, ?, "
                    " 'bars-test')",
                    [symbol, trade_date, index + 1, datetime(2026, 1, 1)],
                )


def _run(start: date, end: date, cutoff: datetime) -> SimpleNamespace:
    return SimpleNamespace(
        id="run-eval-all-1",
        start_date=datetime.combine(start, datetime.min.time()),
        end_date=datetime.combine(end, datetime.min.time()),
        data_cutoff_at=cutoff,
        rebalance_frequency="daily",
        target_horizon=5,
        candidate_pool_snapshot_id="pool-1",
        split_algorithm_version="split-1.0.0",
        random_seed=42,
    )


def _batch_ids(warehouse: FactorWarehouse) -> set[str]:
    with warehouse.connection(read_only=True) as conn:
        return {
            str(row[0])
            for row in conn.execute(
                "SELECT DISTINCT calc_batch_id FROM factor_targets"
            ).fetchall()
        }


def test_evaluate_all_impl_resolves_shared_batch(tmp_path, monkeypatch):
    warehouse = FactorWarehouse(tmp_path / "eval_all.duckdb")
    warehouse.initialize()
    # daily 频率的切分地板是 252 个调仓点，窗口给足才不会卡在切分阶段
    _seed_bars(warehouse, _days(400))
    run = _run(START, START + timedelta(days=350),
               datetime(2026, 1, 1) + timedelta(days=360))

    monkeypatch.setattr(SVC, "load_run", lambda db, run_id: run)
    seen: dict[str, object] = {}

    def _fake_finalize(db, *, ctx, run_id, top_k):
        seen["batch"] = ctx.target_calc_batch_id
        seen["window"] = (ctx.start_date, ctx.end_date)
        return {"status": "ok"}

    monkeypatch.setattr(SVC, "finalize_run", _fake_finalize)

    result = SVC.evaluate_all_impl(
        None, run_id=run.id, top_k=5, warehouse_path=str(warehouse.path))

    assert result == {"status": "ok"}
    assert seen["batch"] == SHARED_TARGET_BATCH_ID
    assert seen["window"] == (START, START + timedelta(days=350))
    # 关键：不再产生 mining-<run_id> 私有批次
    assert _batch_ids(warehouse) == {SHARED_TARGET_BATCH_ID}
    with warehouse.connection(read_only=True) as conn:
        codes = {str(row[0]) for row in conn.execute(
            "SELECT DISTINCT target_code FROM factor_targets").fetchall()}
    assert codes == {TARGET_CODE}


def test_evaluate_all_impl_fails_loudly_on_bars_gap(tmp_path, monkeypatch):
    """K 线没覆盖到的窗口必须报错，不能退回"抄一份能看到多少算多少"。"""
    warehouse = FactorWarehouse(tmp_path / "eval_all_gap.duckdb")
    warehouse.initialize()
    _seed_bars(warehouse, _days(20, offset=20))       # 库里只有 1 月下旬起
    run = _run(START, START + timedelta(days=5), datetime(2026, 1, 6))

    monkeypatch.setattr(SVC, "load_run", lambda db, run_id: run)
    monkeypatch.setattr(
        SVC, "finalize_run",
        lambda db, *, ctx, run_id, top_k: pytest.fail("缺口不该继续跑"),
    )

    with pytest.raises(LabelCoverageError):
        SVC.evaluate_all_impl(
            None, run_id=run.id, top_k=5, warehouse_path=str(warehouse.path))
