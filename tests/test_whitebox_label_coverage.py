"""共享标签批次覆盖 gate 的白盒测试（VIZ-0930-27 改造 B-1）。

覆盖三件必须成立的事：
1. 期望交易日来自镜像 bars 日历，且扣掉 horizon 的尾部（否则 gate 会永远
   认为"最近 5 天缺失"，变成天天补数的死循环）；
2. 请求起点早于镜像起点时判 bars_gap —— 只看标签缺口会让这种窗口静默通过；
3. 覆盖表缺失时 gate 仍能从 factor_targets 现算（存量批次没有覆盖记录）。
"""

from datetime import date, datetime, timedelta

import pytest

from app.services.factors.label_coverage import (
    TARGET_HORIZON_TRADING_DAYS,
    evaluate_label_coverage,
)
from app.services.factors.store import FactorWarehouse

pytestmark = pytest.mark.whitebox

TARGET_CODE = "target_5d_return"
BATCH = "targets-labels"


def _make_warehouse(tmp_path) -> FactorWarehouse:
    warehouse = FactorWarehouse(tmp_path / "label_coverage.duckdb")
    warehouse.initialize()
    return warehouse


def _insert_bars(warehouse: FactorWarehouse, days: list[date]) -> None:
    with warehouse._write_lock, warehouse.connection() as conn:
        for index, trade_date in enumerate(days):
            conn.execute(
                "INSERT INTO raw_daily_bars "
                "(symbol, trade_date, adjust, business_symbol_id, "
                " universe_symbol_id, open, high, low, close, volume, amount, "
                " turnover_rate, source, source_origin, source_row_id, "
                " source_updated_at, ingested_at, batch_id) "
                "VALUES ('600519', ?, 'qfq', NULL, NULL, 10, 11, 9, 10, 100, "
                " 1000, 0.5, 'test', 'daily_bars', ?, NULL, ?, 'bars-test')"
                " ON CONFLICT (symbol, trade_date, adjust) DO UPDATE SET "
                "  batch_id = excluded.batch_id",
                [trade_date, index + 1, datetime(2026, 1, 1)],
            )


def _insert_targets(
    warehouse: FactorWarehouse,
    days: list[date],
    *,
    batch_id: str = BATCH,
    target_code: str = TARGET_CODE,
    horizon: int = TARGET_HORIZON_TRADING_DAYS,
) -> None:
    rows = []
    for index, signal_date in enumerate(days):
        rows.append(
            {
                "symbol": "600519",
                "signal_date": signal_date,
                "entry_date": signal_date + timedelta(days=1),
                "exit_date": signal_date + timedelta(days=horizon),
                "target_code": target_code,
                "target_value": 0.01 * index,
                "is_tradable": True,
                "invalid_reason": None,
                "calc_batch_id": batch_id,
                "created_at": datetime(2026, 1, 1, 10, 0, 0),
            }
        )
    warehouse.upsert_records("factor_targets", rows)


def _consecutive_days(start: date, count: int) -> list[date]:
    return [start + timedelta(days=index) for index in range(count)]


def test_gate_satisfied_and_carves_out_horizon_tail(tmp_path):
    warehouse = _make_warehouse(tmp_path)
    days = _consecutive_days(date(2026, 1, 1), 20)
    _insert_bars(warehouse, days)
    # 只写到 bars_latest - horizon，尾部 5 天天然还没有可定值的标签
    _insert_targets(warehouse, days[: 20 - TARGET_HORIZON_TRADING_DAYS])

    report = evaluate_label_coverage(
        warehouse,
        batch_id=BATCH,
        start_date=date(2026, 1, 1),
        end_date=date(2026, 1, 20),
    )

    assert report.satisfied is True
    assert report.blocker is None
    assert report.effective_end == date(2026, 1, 15)
    assert report.expected_days == 15
    assert report.covered_days == 15
    assert report.bars_latest_date == date(2026, 1, 20)


def test_gate_reports_missing_span_inside_covered_window(tmp_path):
    warehouse = _make_warehouse(tmp_path)
    days = _consecutive_days(date(2026, 1, 1), 20)
    _insert_bars(warehouse, days)
    hole = date(2026, 1, 7)
    _insert_targets(
        warehouse,
        [d for d in days[: 20 - TARGET_HORIZON_TRADING_DAYS] if d != hole],
    )

    report = evaluate_label_coverage(
        warehouse,
        batch_id=BATCH,
        start_date=date(2026, 1, 1),
        end_date=date(2026, 1, 20),
    )

    assert report.satisfied is False
    assert report.blocker == "coverage_gap"
    assert report.missing_days == 1
    assert report.missing_dates == (hole,)
    assert report.missing_backfillable is True
    assert "2026-01-07" in report.message


def test_gate_flags_bars_gap_when_request_predates_mirror(tmp_path):
    warehouse = _make_warehouse(tmp_path)
    days = _consecutive_days(date(2026, 1, 5), 16)
    _insert_bars(warehouse, days)
    _insert_targets(warehouse, days[: 16 - TARGET_HORIZON_TRADING_DAYS])

    report = evaluate_label_coverage(
        warehouse,
        batch_id=BATCH,
        start_date=date(2026, 1, 1),
        end_date=date(2026, 1, 20),
    )

    # 镜像起点晚于请求起点：即便已有 K 线区间的标签齐全，也必须判失败
    assert report.satisfied is False
    assert report.blocker == "bars_gap"
    assert report.missing_backfillable is False
    assert report.bars_cover_start == date(2026, 1, 5)
    assert "2026-01-05" in report.message


def test_gate_does_not_carve_out_tail_when_request_ends_before_library(tmp_path):
    """请求末端早于全库末端时，末端标签已可定值，不能再扣 horizon。

    真实数据里这个偏差表现为 392/397：批次末端 2026-09-01，而 K 线已到
    2026-09-29，那 5 天的标签其实早就定值了。
    """
    warehouse = _make_warehouse(tmp_path)
    days = _consecutive_days(date(2026, 1, 1), 30)
    _insert_bars(warehouse, days)
    request_days = days[:20]
    _insert_targets(warehouse, request_days)

    report = evaluate_label_coverage(
        warehouse,
        batch_id=BATCH,
        start_date=date(2026, 1, 1),
        end_date=date(2026, 1, 20),
    )

    assert report.satisfied is True
    assert report.effective_end == date(2026, 1, 20)
    assert report.expected_days == 20
    assert report.covered_days == 20
    assert report.bars_latest_date == date(2026, 1, 30)


def test_gate_reports_bars_gap_when_mirror_has_no_bars_at_all(tmp_path):
    warehouse = _make_warehouse(tmp_path)
    _insert_bars(warehouse, _consecutive_days(date(2026, 3, 1), 5))

    report = evaluate_label_coverage(
        warehouse,
        batch_id=BATCH,
        start_date=date(2026, 1, 1),
        end_date=date(2026, 1, 20),
    )

    assert report.satisfied is False
    assert report.blocker == "bars_gap"
    assert report.expected_days == 0
    assert "K 线" in report.message


def test_gate_rejects_window_entirely_inside_horizon(tmp_path):
    warehouse = _make_warehouse(tmp_path)
    days = _consecutive_days(date(2026, 1, 1), 20)
    _insert_bars(warehouse, days)
    _insert_targets(warehouse, days)

    report = evaluate_label_coverage(
        warehouse,
        batch_id=BATCH,
        start_date=date(2026, 1, 17),
        end_date=date(2026, 1, 20),
    )

    assert report.satisfied is False
    assert report.blocker == "horizon_only"


def test_start_on_non_trading_day_is_not_a_bars_gap(tmp_path):
    """请求起点落在节假日/周末时不能误判成 K 线缺口。

    HTTP 复验实测踩过：起点 2025-06-01（周六）被报成"镜像只覆盖到 06-03"。
    头缺口必须比全库最早交易日，而不是比请求区间内的第一个交易日。
    """
    warehouse = _make_warehouse(tmp_path)
    days = _consecutive_days(date(2026, 1, 1), 5)          # 01-01..01-05
    days += _consecutive_days(date(2026, 1, 7), 14)        # 01-06 是停牌/节假日
    _insert_bars(warehouse, days)
    _insert_targets(warehouse, [d for d in days if d <= date(2026, 1, 14)])

    report = evaluate_label_coverage(
        warehouse,
        batch_id=BATCH,
        start_date=date(2026, 1, 6),      # 库里这天没有 bar，但镜像早已开始
        end_date=date(2026, 1, 14),
    )

    assert report.satisfied is True, report.message
    assert report.blocker is None
    assert report.bars_cover_start == date(2026, 1, 1)
    assert report.expected_days == 8
    assert report.covered_days == 8


def test_gate_works_without_coverage_row(tmp_path):
    """存量批次没有覆盖记录时，gate 必须仍能现算，否则第 1 步无法验证。"""
    warehouse = _make_warehouse(tmp_path)
    days = _consecutive_days(date(2026, 1, 1), 20)
    _insert_bars(warehouse, days)
    _insert_targets(warehouse, days[: 20 - TARGET_HORIZON_TRADING_DAYS])

    assert warehouse.get_label_coverage(BATCH, TARGET_CODE) is None
    report = evaluate_label_coverage(
        warehouse,
        batch_id=BATCH,
        start_date=date(2026, 1, 1),
        end_date=date(2026, 1, 20),
    )
    assert report.satisfied is True
    assert report.symbol_count is None

    summary = warehouse.refresh_label_coverage(BATCH, TARGET_CODE)
    assert summary["signal_days"] == 15
    assert summary["symbol_count"] == 1
    assert summary["rows_tradable"] == 15
    assert summary["bars_latest_date"] == date(2026, 1, 20)

    coverage = warehouse.get_label_coverage(BATCH, TARGET_CODE)
    assert coverage is not None
    assert coverage["signal_days"] == 15
    # 再刷一次不应新增行
    warehouse.refresh_label_coverage(BATCH, TARGET_CODE)
    with warehouse.connection(read_only=True) as conn:
        rows = conn.execute(
            "SELECT COUNT(*) FROM warehouse_label_coverage"
        ).fetchone()[0]
    assert rows == 1

    after = evaluate_label_coverage(
        warehouse,
        batch_id=BATCH,
        start_date=date(2026, 1, 1),
        end_date=date(2026, 1, 20),
    )
    assert after.satisfied is True
    assert after.symbol_count == 1


def test_get_target_panel_filters_window_and_as_of(tmp_path):
    warehouse = _make_warehouse(tmp_path)
    days = _consecutive_days(date(2026, 1, 1), 20)
    _insert_bars(warehouse, days)
    _insert_targets(warehouse, days[:10])

    panel, batch, code = warehouse.get_target_panel(
        BATCH,
        TARGET_CODE,
        start_date=date(2026, 1, 3),
        end_date=date(2026, 1, 6),
    )
    assert (batch, code) == (BATCH, TARGET_CODE)
    assert sorted({d.date() for d in panel["signal_date"]}) == [
        date(2026, 1, 3),
        date(2026, 1, 4),
        date(2026, 1, 5),
        date(2026, 1, 6),
    ]

    # as_of：只允许看到 exit_date <= cutoff 的标签，挡住未来信息
    first_exit = date(2026, 1, 1) + timedelta(days=TARGET_HORIZON_TRADING_DAYS)
    pit_panel, _, _ = warehouse.get_target_panel(
        BATCH, TARGET_CODE, as_of_exit_date=first_exit
    )
    assert len(pit_panel) == 1
    assert pit_panel["signal_date"].iloc[0].date() == date(2026, 1, 1)

    later_cutoff = date(2026, 1, 3) + timedelta(days=TARGET_HORIZON_TRADING_DAYS)
    wider, _, _ = warehouse.get_target_panel(
        BATCH, TARGET_CODE, as_of_exit_date=later_cutoff
    )
    assert sorted(d.date() for d in wider["signal_date"]) == [
        date(2026, 1, 1),
        date(2026, 1, 2),
        date(2026, 1, 3),
    ]

    unfiltered, _, _ = warehouse.get_target_panel(BATCH, TARGET_CODE)
    assert len(unfiltered) == 10


def test_shift_back_trading_days_skips_calendar_gaps(tmp_path):
    warehouse = _make_warehouse(tmp_path)
    # 中间断 3 天（长假），推 5 个交易日必须落到真实交易日上
    days = _consecutive_days(date(2026, 1, 1), 6) + _consecutive_days(
        date(2026, 1, 10), 6
    )
    _insert_bars(warehouse, days)

    assert warehouse.shift_back_trading_days(date(2026, 1, 15), 5) == date(
        2026, 1, 10
    )
    assert warehouse.shift_back_trading_days(date(2026, 1, 15), 0) == date(
        2026, 1, 15
    )
    with pytest.raises(ValueError):
        warehouse.shift_back_trading_days(date(2026, 1, 15), -1)


def test_list_trading_days_ignores_other_adjust(tmp_path):
    warehouse = _make_warehouse(tmp_path)
    _insert_bars(warehouse, _consecutive_days(date(2026, 1, 1), 5))
    with warehouse._write_lock, warehouse.connection() as conn:
        conn.execute(
            "INSERT INTO raw_daily_bars "
            "(symbol, trade_date, adjust, business_symbol_id, "
            " universe_symbol_id, open, high, low, close, volume, amount, "
            " turnover_rate, source, source_origin, source_row_id, "
            " source_updated_at, ingested_at, batch_id) "
            "VALUES ('600519', '2026-02-01', 'hfq', NULL, NULL, 10, 11, 9, 10,"
            " 100, 1000, 0.5, 'test', 'daily_bars', 99, NULL, ?, 'bars-test')",
            [datetime(2026, 1, 1)],
        )

    qfq = warehouse.list_trading_days(date(2026, 1, 1), date(2026, 3, 1))
    assert len(qfq) == 5
    hfq = warehouse.list_trading_days(
        date(2026, 1, 1), date(2026, 3, 1), adjust="hfq"
    )
    assert len(hfq) == 1
