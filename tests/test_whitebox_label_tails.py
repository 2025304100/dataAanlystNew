"""B-3 两条尾巴的测：接口/PIT 口径一致性 + 预检④按窗口判定。

尾巴 1：`/factors/label-coverage` 与挖掘/评估必须用同一个截窗口径（`usable_end`），
否则接口按全库 bars 判、跑批按各自 cutoff 判，UI 上就是"永远缺最后 horizon 天"。
尾巴 2：预检④"有批次"不等于"所选区间有标签"，要按窗口判定并给真实缺口。
"""

from datetime import date, datetime, timedelta

import pytest

from app.services.factors.label_batch import usable_end
from app.services.factors.store import (
    SHARED_TARGET_BATCH_ID,
    FactorWarehouse,
)
from app.services.factors.target_engine import TARGET_CODE, calculate_targets
from app.services.factors.wp5_eval_task import (
    _preflight_target_availability_item,
)

pytestmark = pytest.mark.whitebox

SYMBOLS = ("600000", "000001")
START = date(2026, 1, 1)


def _days(count: int, offset: int = 0) -> list[date]:
    return [START + timedelta(days=offset + i) for i in range(count)]


def _warehouse(tmp_path, days: list[date]) -> FactorWarehouse:
    warehouse = FactorWarehouse(tmp_path / "tails.duckdb")
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
                    " 1000, 0.5, 'test', 'daily_bars', ?, NULL, ?, 'bars')",
                    [symbol, trade_date, index + 1, datetime(2026, 1, 1)],
                )
    return warehouse


def test_usable_end_is_optional_on_as_of(tmp_path):
    """`usable_end` 只管"按声明的 PIT 边界截"，horizon 扣除在覆盖判定那一层。

    两层各管一件事：这里若也扣 horizon，接口与跑批就会出现两种上界算法。
    """
    warehouse = _warehouse(tmp_path, _days(30))

    # 没有 as_of：上界就是请求末端，不可定值的尾部由覆盖判定去扣
    assert usable_end(warehouse, end_date=START + timedelta(days=29),
                      target_code=TARGET_CODE) == START + timedelta(days=29)
    # 显式 None 与省略参数必须等价（接口与跑批走同一函数，不能两种签名两种结果）
    assert usable_end(warehouse, end_date=START + timedelta(days=29),
                      as_of_exit_date=None, target_code=TARGET_CODE) == \
        usable_end(warehouse, end_date=START + timedelta(days=29),
                   target_code=TARGET_CODE)
    # 给了更早的 as_of：上界由它决定（as_of 往前 5 个交易日）
    assert usable_end(warehouse, end_date=START + timedelta(days=29),
                      as_of_exit_date=START + timedelta(days=9),
                      target_code=TARGET_CODE) == START + timedelta(days=4)


def test_preflight_item_reports_window_gap(tmp_path):
    """有批次但所选区间大部分没标签 → warn + 真实缺口，不再报"可用"。"""
    warehouse = _warehouse(tmp_path, _days(40))
    calculate_targets(
        warehouse,
        start_date=START + timedelta(days=30),
        end_date=START + timedelta(days=39),
        calc_batch_id=SHARED_TARGET_BATCH_ID,
    )

    item = _preflight_target_availability_item(
        warehouse,
        target_code=TARGET_CODE,
        target_horizon=5,
        latest_batch_id=SHARED_TARGET_BATCH_ID,
        start_date=START + timedelta(days=5),
        end_date=START + timedelta(days=34),
    )

    assert item["code"] == "preflight.target_availability.incomplete"
    assert item["severity"] == "warn"
    assert item["evidence"]["missing_days"] > 0
    assert item["evidence"]["covered_days"] < item["evidence"]["expected_days"]
    assert "缺口" in item["detail_zh"]


def test_preflight_item_passes_when_window_covered(tmp_path):
    warehouse = _warehouse(tmp_path, _days(40))
    calculate_targets(
        warehouse,
        start_date=START,
        end_date=START + timedelta(days=24),
        calc_batch_id=SHARED_TARGET_BATCH_ID,
    )

    item = _preflight_target_availability_item(
        warehouse,
        target_code=TARGET_CODE,
        target_horizon=5,
        latest_batch_id=SHARED_TARGET_BATCH_ID,
        start_date=START,
        end_date=START + timedelta(days=19),
    )

    assert item["code"] == "preflight.target_availability.ok"
    assert item["severity"] == "pass"
    assert item["evidence"]["expected_days"] == 20
    assert item["evidence"]["covered_days"] == 20


def test_preflight_item_without_window_says_it_cannot_judge(tmp_path):
    """没给评测区间时不许宣称"区间可用"，要明说无法判定窗口覆盖。"""
    warehouse = _warehouse(tmp_path, _days(40))
    calculate_targets(warehouse, calc_batch_id=SHARED_TARGET_BATCH_ID)

    item = _preflight_target_availability_item(
        warehouse,
        target_code=TARGET_CODE,
        target_horizon=5,
        latest_batch_id=SHARED_TARGET_BATCH_ID,
        start_date=None,
        end_date=None,
    )

    assert item["code"] == "preflight.target_availability.ok"
    assert item["evidence"]["expected_days"] is None
    assert "无法判定窗口覆盖" in item["detail_zh"]
