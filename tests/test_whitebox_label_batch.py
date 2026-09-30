"""B-3 的解析层：`resolve_label_batch` 的补齐语义与不越权边界。

这个模块是"挖掘/评估只读、缺口就地补写同一批次"的唯一入口，因此重点验三件事：
补齐只会用到常驻批次（不产生私有批次）；已覆盖时是纯读；补不出来的缺口必须报错。
"""

from datetime import date, datetime, timedelta

import pytest

from app.services.factors.label_batch import (
    LabelCoverageError,
    horizon_trading_days_of,
    resolve_label_batch,
)
from app.services.factors.store import SHARED_TARGET_BATCH_ID, FactorWarehouse
from app.services.factors.target_engine import TARGET_CODE

pytestmark = pytest.mark.whitebox

SYMBOLS = ("600000", "600001")
START = date(2026, 1, 1)


def _days(count: int, offset: int = 0) -> list[date]:
    return [START + timedelta(days=offset + i) for i in range(count)]


def _warehouse(tmp_path, days: list[date]) -> FactorWarehouse:
    warehouse = FactorWarehouse(tmp_path / "label_batch.duckdb")
    warehouse.initialize()
    with warehouse._write_lock, warehouse.connection() as conn:
        for index, trade_date in enumerate(days):
            for symbol in SYMBOLS:
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
    return warehouse


def _batch_ids(warehouse: FactorWarehouse) -> set[str]:
    with warehouse.connection(read_only=True) as conn:
        return {
            str(row[0])
            for row in conn.execute(
                "SELECT DISTINCT calc_batch_id FROM factor_targets"
            ).fetchall()
        }


def _row_count(warehouse: FactorWarehouse) -> int:
    with warehouse.connection(read_only=True) as conn:
        return int(conn.execute(
            "SELECT COUNT(*) FROM factor_targets"
        ).fetchone()[0])


def test_resolve_backfills_into_single_shared_batch(tmp_path):
    warehouse = _warehouse(tmp_path, _days(20))

    resolved = resolve_label_batch(
        warehouse, start_date=START, end_date=START + timedelta(days=9)
    )

    assert resolved.batch_id == SHARED_TARGET_BATCH_ID
    assert resolved.backfilled is True
    assert resolved.rows_written == 10 * len(SYMBOLS)
    assert resolved.coverage.satisfied is True
    # 补齐不得产生私有批次
    assert _batch_ids(warehouse) == {SHARED_TARGET_BATCH_ID}


def test_second_resolve_is_pure_read(tmp_path):
    warehouse = _warehouse(tmp_path, _days(20))
    resolve_label_batch(
        warehouse, start_date=START, end_date=START + timedelta(days=9)
    )
    rows_after_first = _row_count(warehouse)

    again = resolve_label_batch(
        warehouse, start_date=START, end_date=START + timedelta(days=9)
    )

    assert again.backfilled is False
    assert again.rows_written == 0
    assert _row_count(warehouse) == rows_after_first


def test_narrower_window_inside_coverage_needs_no_write(tmp_path):
    warehouse = _warehouse(tmp_path, _days(20))
    resolve_label_batch(
        warehouse, start_date=START, end_date=START + timedelta(days=14)
    )
    rows = _row_count(warehouse)

    narrow = resolve_label_batch(
        warehouse, start_date=START + timedelta(days=3),
        end_date=START + timedelta(days=6),
    )

    assert narrow.backfilled is False
    assert narrow.coverage.covered_days == 4
    assert _row_count(warehouse) == rows


def test_extending_window_backfills_only_what_is_needed(tmp_path):
    """先短后长：扩窗口要补齐新增区间，但批次仍然只有一个。"""
    warehouse = _warehouse(tmp_path, _days(30))
    resolve_label_batch(
        warehouse, start_date=START, end_date=START + timedelta(days=9)
    )

    wider = resolve_label_batch(
        warehouse, start_date=START, end_date=START + timedelta(days=19)
    )

    assert wider.backfilled is True
    assert wider.coverage.satisfied is True
    assert _batch_ids(warehouse) == {SHARED_TARGET_BATCH_ID}
    with warehouse.connection(read_only=True) as conn:
        days = conn.execute(
            "SELECT COUNT(DISTINCT signal_date) FROM factor_targets "
            "WHERE calc_batch_id = ?",
            [SHARED_TARGET_BATCH_ID],
        ).fetchone()[0]
    assert days == 20


def test_bars_gap_raises_and_writes_nothing(tmp_path):
    warehouse = _warehouse(tmp_path, _days(20))

    with pytest.raises(LabelCoverageError) as exc:
        resolve_label_batch(
            warehouse,
            start_date=START - timedelta(days=120),
            end_date=START - timedelta(days=60),
        )

    assert exc.value.report.blocker == "bars_gap"
    assert "K 线" in exc.value.report.message
    assert _batch_ids(warehouse) == set()


def test_horizon_only_window_raises_without_writing(tmp_path):
    warehouse = _warehouse(tmp_path, _days(8))

    with pytest.raises(LabelCoverageError) as exc:
        resolve_label_batch(
            warehouse,
            start_date=START + timedelta(days=5),
            end_date=START + timedelta(days=7),
        )

    assert exc.value.report.blocker == "horizon_only"
    assert _row_count(warehouse) == 0


def test_allow_backfill_false_never_writes(tmp_path):
    warehouse = _warehouse(tmp_path, _days(20))

    with pytest.raises(LabelCoverageError):
        resolve_label_batch(
            warehouse,
            start_date=START,
            end_date=START + timedelta(days=9),
            allow_backfill=False,
        )

    assert _row_count(warehouse) == 0


def test_other_target_code_does_not_claim_shared_batch(tmp_path):
    """非 5d 口径不许被静默解析成常驻 5d 批次（那等于换成另一套标签）。"""
    warehouse = _warehouse(tmp_path, _days(20))
    resolve_label_batch(
        warehouse, start_date=START, end_date=START + timedelta(days=9)
    )
    assert _batch_ids(warehouse) == {SHARED_TARGET_BATCH_ID}

    with pytest.raises(LabelCoverageError) as exc:
        resolve_label_batch(
            warehouse,
            start_date=START,
            end_date=START + timedelta(days=9),
            target_code="target_3d_return",
        )

    assert exc.value.report.batch_id != ""
    # 仍然只有 5d 的常驻批次，没有为 3d 新造一份
    assert _batch_ids(warehouse) == {SHARED_TARGET_BATCH_ID}


def test_as_of_caps_window_at_pit_boundary(tmp_path):
    """给了 as_of 就按 PIT 截窗：cutoff 之后才定值的标签不许进入窗口。"""
    warehouse = _warehouse(tmp_path, _days(30))

    resolved = resolve_label_batch(
        warehouse,
        start_date=START,
        end_date=START + timedelta(days=29),
        as_of_exit_date=START + timedelta(days=9),
    )

    # 上界 = as_of 往前 5 个交易日
    assert resolved.usable_end == START + timedelta(days=4)
    assert resolved.coverage.covered_days == 5
    assert resolved.rows_written == 5 * len(SYMBOLS)
    with warehouse.connection(read_only=True) as conn:
        latest_signal = conn.execute(
            "SELECT MAX(signal_date) FROM factor_targets"
        ).fetchone()[0]
    assert latest_signal == START + timedelta(days=4)


def test_as_of_earlier_than_start_raises_instead_of_shifting_window(tmp_path):
    """as_of 早于窗口起点时必须报错，不能悄悄把窗口往前挪。"""
    warehouse = _warehouse(tmp_path, _days(30))

    with pytest.raises(LabelCoverageError) as exc:
        resolve_label_batch(
            warehouse,
            start_date=START + timedelta(days=10),
            end_date=START + timedelta(days=20),
            as_of_exit_date=START + timedelta(days=3),
        )

    assert exc.value.report.blocker in ("invalid_window", "bars_gap")
    assert _row_count(warehouse) == 0


def test_horizon_parsed_from_target_code():
    """PIT 上界按 target_code 自带的 horizon 推，不能一律按 5d。"""
    assert horizon_trading_days_of("target_5d_return") == 5
    assert horizon_trading_days_of("target_20d_return") == 20
    assert horizon_trading_days_of("target_1d_return") == 1
    assert horizon_trading_days_of("something_else") == 5


def test_shared_batch_serves_two_windows_without_bleeding(tmp_path):
    """共享批次同时服务两个不同窗口的 run：各自只拿到自己的信号日。"""
    warehouse = _warehouse(tmp_path, _days(30))

    a = resolve_label_batch(
        warehouse, start_date=START, end_date=START + timedelta(days=9))
    panel_a, _, _ = warehouse.get_target_panel(
        a.batch_id, TARGET_CODE,
        start_date=START, end_date=a.usable_end)
    b = resolve_label_batch(
        warehouse, start_date=START + timedelta(days=15),
        end_date=START + timedelta(days=21))
    panel_b, _, _ = warehouse.get_target_panel(
        b.batch_id, TARGET_CODE,
        start_date=START + timedelta(days=15), end_date=b.usable_end)

    assert a.batch_id == b.batch_id == SHARED_TARGET_BATCH_ID
    dates_a = {d.date() for d in panel_a["signal_date"]}
    dates_b = {d.date() for d in panel_b["signal_date"]}
    assert dates_a == {_days(10)[i] for i in range(10)}
    assert dates_b == {START + timedelta(days=15 + i) for i in range(7)}
    assert not (dates_a & dates_b)
    assert _batch_ids(warehouse) == {SHARED_TARGET_BATCH_ID}


def test_cache_key_carries_window_and_cutoff():
    """`_TARGET_PIVOT_CACHE` 的键必须含窗口与 PIT：共享批次后 batch_id 不再唯一。"""
    from types import SimpleNamespace

    from app.services.factors.mining.evaluation_adapter import (
        target_pivot_cache_key,
    )

    base = dict(target_calc_batch_id=SHARED_TARGET_BATCH_ID)
    run_a = SimpleNamespace(**base, start_date=date(2026, 1, 1),
                            end_date=date(2026, 2, 1),
                            data_cutoff_at=datetime(2026, 2, 1))
    run_b = SimpleNamespace(**base, start_date=date(2026, 3, 1),
                            end_date=date(2026, 4, 1),
                            data_cutoff_at=datetime(2026, 4, 1))
    run_b_same_window_later_cutoff = SimpleNamespace(
        **base, start_date=date(2026, 1, 1), end_date=date(2026, 2, 1),
        data_cutoff_at=datetime(2026, 3, 1))

    key_a = target_pivot_cache_key(run_a, TARGET_CODE)
    assert key_a != target_pivot_cache_key(run_b, TARGET_CODE)
    # PIT 上界变化也要换键：同一个窗口在不同 cutoff 下可用标签集合不同
    assert key_a != target_pivot_cache_key(run_b_same_window_later_cutoff, TARGET_CODE)
    assert key_a == target_pivot_cache_key(
        SimpleNamespace(**base, start_date=date(2026, 1, 1),
                        end_date=date(2026, 2, 1),
                        data_cutoff_at=datetime(2026, 2, 1)), TARGET_CODE)


def test_resolved_batch_matches_target_engine_default_code(tmp_path):
    """常驻批次里的 target_code 必须与 target_engine 产出的常量一致。"""
    warehouse = _warehouse(tmp_path, _days(20))
    resolve_label_batch(
        warehouse, start_date=START, end_date=START + timedelta(days=9)
    )
    with warehouse.connection(read_only=True) as conn:
        codes = {
            str(row[0])
            for row in conn.execute(
                "SELECT DISTINCT target_code FROM factor_targets"
            ).fetchall()
        }
    assert codes == {TARGET_CODE}
