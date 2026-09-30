"""B-2：常驻共享标签批次（日期下推 + 尾部重算带 + 覆盖表 + 审计行数）。

钉四件事：
1. 窗口下推进 SQL 之后，结果与"全量自连接再截窗口"逐行一致（尾部信号不能因为
   窗口截短而丢未来的 entry/exit K 线）；
2. 同一批次重复写不累积重复行（冲突键 upsert）；
3. 尾部带必须能翻正：未来 K 线补齐后，上次 insufficient_future_calendar 的行
   要变成 tradable；
4. prune 永远不删常驻批次；calculate_targets 写完要刷新覆盖表。
"""

from datetime import date, datetime, timedelta

import pytest

from app.services.factors.label_coverage import (
    TARGET_HORIZON_TRADING_DAYS,
    evaluate_label_coverage,
)
from app.services.factors.store import (
    SHARED_TARGET_BATCH_ID,
    FactorWarehouse,
)
from app.services.factors.target_engine import TARGET_CODE, calculate_targets

pytestmark = pytest.mark.whitebox

SYMBOLS = ("600000", "600001", "000001")
START = date(2026, 1, 1)


def _days(count: int, offset: int = 0) -> list[date]:
    return [START + timedelta(days=offset + i) for i in range(count)]


def _insert_bars(warehouse: FactorWarehouse, days: list[date],
                 symbols: tuple[str, ...] = SYMBOLS) -> None:
    """写入可交易 K 线：high/low 拉开、成交量额非零、无涨跌停缺口。"""
    with warehouse._write_lock, warehouse.connection() as conn:
        for index, trade_date in enumerate(days):
            for symbol in symbols:
                conn.execute(
                    "INSERT INTO raw_daily_bars "
                    "(symbol, trade_date, adjust, business_symbol_id, "
                    " universe_symbol_id, open, high, low, close, volume, "
                    " amount, turnover_rate, source, source_origin, "
                    " source_row_id, source_updated_at, ingested_at, batch_id) "
                    "VALUES (?, ?, 'qfq', NULL, NULL, 10, 11, 9, 10, 100, "
                    " 1000, 0.5, 'test', 'daily_bars', ?, NULL, ?, "
                    " 'bars-test') "
                    "ON CONFLICT (symbol, trade_date, adjust) DO UPDATE SET "
                    "  open = excluded.open, high = excluded.high, "
                    "  low = excluded.low, close = excluded.close, "
                    "  volume = excluded.volume, amount = excluded.amount, "
                    "  batch_id = excluded.batch_id",
                    [symbol, trade_date, index + 1, datetime(2026, 1, 1)],
                )


def _make_warehouse(tmp_path, days: list[date]) -> FactorWarehouse:
    warehouse = FactorWarehouse(tmp_path / "shared_labels.duckdb")
    warehouse.initialize()
    _insert_bars(warehouse, days)
    return warehouse


def _panel(warehouse: FactorWarehouse, batch_id: str):
    with warehouse.connection(read_only=True) as conn:
        return conn.execute(
            "SELECT symbol, signal_date, entry_date, exit_date, target_value,"
            "       is_tradable, invalid_reason "
            "FROM factor_targets WHERE calc_batch_id = ? "
            "ORDER BY symbol, signal_date",
            [batch_id],
        ).fetchall()


def test_window_pushdown_equals_full_panel_sliced(tmp_path):
    """下推窗口后的结果必须与全量算完再截窗口逐行一致。"""
    warehouse = _make_warehouse(tmp_path, _days(30))

    calculate_targets(warehouse, calc_batch_id="batch-full")
    calculate_targets(
        warehouse,
        start_date=START + timedelta(days=5),
        end_date=START + timedelta(days=10),
        calc_batch_id="batch-window",
    )

    full = {(r[0], r[1]): r for r in _panel(warehouse, "batch-full")}
    window = {(r[0], r[1]): r for r in _panel(warehouse, "batch-window")}
    sliced = {
        key: row for key, row in full.items()
        if START + timedelta(days=5) <= key[1] <= START + timedelta(days=10)
    }

    assert window == sliced
    assert len(window) == 6 * len(SYMBOLS)


def test_shared_batch_upsert_does_not_accumulate(tmp_path):
    """同批次多次写入是覆盖，不是新增一份（VIZ-0930-27 的机械前提）。"""
    warehouse = _make_warehouse(tmp_path, _days(30))

    first = calculate_targets(
        warehouse,
        start_date=_days(20)[0],
        end_date=_days(20)[-1],
        calc_batch_id=SHARED_TARGET_BATCH_ID,
    )
    second = calculate_targets(
        warehouse,
        start_date=_days(25)[0],
        end_date=_days(25)[-1],
        calc_batch_id=SHARED_TARGET_BATCH_ID,
    )

    with warehouse.connection(read_only=True) as conn:
        rows = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT symbol || '|' || signal_date ||"
            "       '|' || target_code || '|' || calc_batch_id) "
            "FROM factor_targets WHERE calc_batch_id = ?",
            [SHARED_TARGET_BATCH_ID],
        ).fetchone()
    assert rows[0] == rows[1]                      # 没有重复键行
    assert rows[0] == 25 * len(SYMBOLS)            # 只增了新增信号日
    assert second.rows_written > first.rows_written


def test_trailing_band_flips_from_insufficient_to_tradable(tmp_path):
    """尾部带是正确性职责：未来 K 线到齐后必须翻正，不能永久停在 invalid。"""
    warehouse = _make_warehouse(tmp_path, _days(21))

    calculate_targets(
        warehouse,
        start_date=_days(21)[-6],
        end_date=_days(21)[-1],
        calc_batch_id=SHARED_TARGET_BATCH_ID,
    )
    tail_before = {
        r[1]: (r[5], r[6]) for r in _panel(warehouse, SHARED_TARGET_BATCH_ID)
    }
    assert any(not tradable for tradable, _ in tail_before.values())
    reasons = {reason for _t, reason in tail_before.values() if reason}
    assert "insufficient_future_calendar" in reasons

    # 未来 K 线补齐后，只刷尾部窗口
    _insert_bars(warehouse, _days(10, offset=21))
    calculate_targets(
        warehouse,
        start_date=_days(21)[-6],
        end_date=_days(21)[-1],
        calc_batch_id=SHARED_TARGET_BATCH_ID,
    )
    tail_after = {
        r[1]: (r[5], r[6]) for r in _panel(warehouse, SHARED_TARGET_BATCH_ID)
    }
    assert all(tradable for tradable, _ in tail_after.values())
    assert tail_after[_days(21)[-1]][0] is True


def test_calculate_targets_refreshes_coverage_table(tmp_path):
    warehouse = _make_warehouse(tmp_path, _days(30))
    calculate_targets(
        warehouse,
        start_date=START,
        end_date=START + timedelta(days=9),
        calc_batch_id=SHARED_TARGET_BATCH_ID,
    )

    coverage = warehouse.get_label_coverage(
        SHARED_TARGET_BATCH_ID, TARGET_CODE
    )
    assert coverage is not None
    assert coverage["signal_days"] == 10
    assert coverage["symbol_count"] == len(SYMBOLS)
    assert coverage["rows_total"] == 10 * len(SYMBOLS)
    assert coverage["bars_latest_date"] == START + timedelta(days=29)

    report = evaluate_label_coverage(
        warehouse,
        batch_id=SHARED_TARGET_BATCH_ID,
        start_date=START,
        end_date=START + timedelta(days=9),
    )
    assert report.satisfied is True
    assert report.symbol_count == len(SYMBOLS)


def test_prune_never_deletes_shared_batch(tmp_path):
    """常驻批次不能靠"它恰好是最新的一批"活下来。"""
    warehouse = _make_warehouse(tmp_path, _days(12))
    calculate_targets(warehouse, calc_batch_id=SHARED_TARGET_BATCH_ID)
    shared_rows = len(_panel(warehouse, SHARED_TARGET_BATCH_ID))

    # 造 3 个 created_at 更晚的批次，把共享批次挤出保留名额；
    # 少了 prune 里的常驻保护，这一步就会把全市场标签删掉。
    created = datetime(2099, 1, 1)
    for index in range(3):
        row = dict(
            symbol="600000",
            signal_date=START + timedelta(days=index),
            entry_date=START + timedelta(days=index + 1),
            exit_date=START + timedelta(days=index + 6),
            target_code=TARGET_CODE,
            target_value=0.01,
            is_tradable=True,
            invalid_reason=None,
            calc_batch_id=f"mining-newer-{index}",
            created_at=created + timedelta(days=index),
        )
        warehouse.upsert_records("factor_targets", [row])

    result = warehouse.prune_calculation_batches(keep_batches=1)

    assert SHARED_TARGET_BATCH_ID in result["retained_batch_ids"]
    assert SHARED_TARGET_BATCH_ID not in result["deleted_batch_ids"]
    assert len(_panel(warehouse, SHARED_TARGET_BATCH_ID)) == shared_rows


def test_batch_context_records_rows_written(tmp_path):
    """committed 批次必须带真实行数，否则原子性核对恒判不符（VIZ-0930-28）。"""
    from app.services.factors.batch_audit import batch_context

    warehouse = _make_warehouse(tmp_path, _days(12))
    with batch_context(
        warehouse,
        batch_id="bars-audit-test",
        source_key="target.generation",
        scope={"start_date": str(START)},
    ) as audit:
        result = calculate_targets(warehouse, calc_batch_id="targets-audit-test")
        audit.rows_written = result.rows_written
        audit.rows_received = result.rows_written

    with warehouse.connection(read_only=True) as conn:
        row = conn.execute(
            "SELECT status, rows_received, rows_written FROM ingestion_batches"
            " WHERE batch_id = ?",
            ["bars-audit-test"],
        ).fetchone()
    assert row is not None
    assert row[0] == "committed"
    assert row[1] == row[2] == result.rows_written
    assert row[2] > 0


def test_horizon_constant_matches_target_engine_joins():
    """标签需要 5 个未来交易日；常量与 target_engine 的自连接偏移必须一致。"""
    import inspect

    import app.services.factors.target_engine as te

    sql = inspect.getsource(te._load_target_panel)
    assert "s.trade_index + 5" in sql
    assert TARGET_HORIZON_TRADING_DAYS == 5
