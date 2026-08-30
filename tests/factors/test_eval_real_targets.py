"""真实目标标签集成单元测试。

覆盖：
- 无批次时 fallback 路径产生 eval.data.target_fallback_used blocker
- horizon!=5 且无批次时 error blocker target_horizon_unavailable
- 有批次时真实 target_panel pivot 形状与值正确
"""
from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from unittest.mock import MagicMock

from app.services.factors.wp5_eval_task import resolve_forward_returns


pytestmark = pytest.mark.whitebox


def _make_factor_values(
    n_days=10, n_symbols=4, start_date=date(2026, 5, 1), seed=42
) -> pd.DataFrame:
    rng = np.random.RandomState(seed)
    dates = [start_date + timedelta(days=i) for i in range(n_days)]
    symbols = [f"S{i:04d}" for i in range(n_symbols)]
    values = rng.rand(n_days, n_symbols) * 10 + 10
    return pd.DataFrame(values, index=dates, columns=symbols)


class TestFallbackUsedWhenNoBatch:
    """无批次时 fallback 路径产生 eval.data.target_fallback_used blocker。"""

    def test_fallback_used_when_no_batch(self, tmp_path):
        from app.services.factors.store import FactorWarehouse

        warehouse = FactorWarehouse(tmp_path / "fw.duckdb")
        warehouse.initialize()
        factor_values = _make_factor_values()

        class FakeEngine:
            pass

        forward_returns, blockers, ctx = resolve_forward_returns(
            warehouse,
            factor_values,
            target_horizon=5,
            _target_engine_module=FakeEngine(),
        )

        codes = [b["code"] for b in blockers]
        assert "eval.data.target_fallback_used" in codes
        assert ctx["fallback_used"] is True
        assert ctx["latest_batch_id"] is None
        assert ctx["target_code"] == "target_5d_return"
        assert ctx["target_horizon"] == 5

        fb = next(
            b for b in blockers if b["code"] == "eval.data.target_fallback_used"
        )
        assert fb["severity"] == "warn"
        assert fb["category"] == "data"
        assert "target_5d_return" in fb.get("detail_zh", "")
        assert fb.get("retryable") is True
        assert fb.get("evidence", {}).get("target_code") == "target_5d_return"

        expected_fallback = factor_values.shift(-5).pct_change(
            periods=5, fill_method=None
        )
        common = factor_values.index.intersection(expected_fallback.index)
        expected_fallback = expected_fallback.loc[common]
        pd.testing.assert_frame_equal(
            forward_returns.sort_index(axis=1),
            expected_fallback.sort_index(axis=1),
            check_names=False,
        )

    def test_monkey_patch_get_latest_returns_none_triggers_fallback(self):
        """monkey-patch warehouse.get_latest_target_batch_id 返回 None。"""
        warehouse = MagicMock()
        warehouse.get_latest_target_batch_id.return_value = None
        warehouse.get_target_panel.return_value = (
            pd.DataFrame(columns=["symbol", "signal_date", "target_value"]),
            "unused",
            "unused",
        )
        factor_values = _make_factor_values()

        class _DummyEngine:
            pass

        forward_returns, blockers, ctx = resolve_forward_returns(
            warehouse,
            factor_values,
            target_horizon=5,
            _target_engine_module=_DummyEngine(),
        )

        warehouse.get_latest_target_batch_id.assert_called_once_with(
            "target_5d_return"
        )
        assert any(
            b["code"] == "eval.data.target_fallback_used" for b in blockers
        )
        assert ctx["fallback_used"] is True

        expected = factor_values.shift(-5).pct_change(periods=5, fill_method=None)
        common = factor_values.index.intersection(expected.index)
        expected = expected.loc[common]
        pd.testing.assert_frame_equal(
            forward_returns.sort_index(axis=1),
            expected.sort_index(axis=1),
            check_names=False,
        )


class TestTargetHorizonUnavailable:
    """horizon!=5 且无批次时 error blocker target_horizon_unavailable。"""

    def test_target_horizon_unavailable_returns_error_blocker(self):
        warehouse = MagicMock()
        warehouse.get_latest_target_batch_id.return_value = None
        factor_values = _make_factor_values()

        class _DummyEngine:
            pass

        forward_returns, blockers, ctx = resolve_forward_returns(
            warehouse,
            factor_values,
            target_horizon=10,
            _target_engine_module=_DummyEngine(),
        )

        assert forward_returns.empty
        assert len(blockers) == 1
        b = blockers[0]
        assert b["code"] == "eval.data.target_horizon_unavailable"
        assert b["severity"] == "error"
        assert b["category"] == "data"
        assert b.get("retryable") is True
        assert b.get("evidence", {}).get("target_horizon") == 10
        assert b.get("evidence", {}).get("target_code") == "target_10d_return"
        assert "target_10d_return" in b.get("detail_zh", "")

        assert ctx["fallback_used"] is False
        assert ctx["latest_batch_id"] is None
        assert ctx["target_horizon"] == 10
        assert ctx["target_code"] == "target_10d_return"

    def test_horizon_3_unavailable_same(self):
        warehouse = MagicMock()
        warehouse.get_latest_target_batch_id.return_value = None
        factor_values = _make_factor_values()

        forward_returns, blockers, ctx = resolve_forward_returns(
            warehouse,
            factor_values,
            target_horizon=3,
            _target_engine_module=MagicMock(spec=[]),
        )
        assert forward_returns.empty
        assert any(
            b["code"] == "eval.data.target_horizon_unavailable"
            and b["severity"] == "error"
            for b in blockers
        )
        assert ctx["fallback_used"] is False


class TestRealTargetPanelPivot:
    """有批次时真实 target_panel pivot 形状与值正确。"""

    def test_real_target_panel_pivot_shape_and_values(self):
        warehouse = MagicMock()
        warehouse.get_latest_target_batch_id.return_value = "batch-real-001"

        panel_rows = []
        dates_str = ["2026-05-01", "2026-05-02", "2026-05-03"]
        symbols = ["S0000", "S0001", "S0002"]
        values_matrix = [
            [0.01, 0.02, 0.03],
            [0.04, 0.05, 0.06],
            [0.07, 0.08, 0.09],
        ]
        for i, d in enumerate(dates_str):
            for j, s in enumerate(symbols):
                panel_rows.append({
                    "symbol": s,
                    "signal_date": d,
                    "target_value": values_matrix[i][j],
                })
        target_panel = pd.DataFrame.from_records(panel_rows)
        warehouse.get_target_panel.return_value = (
            target_panel,
            "batch-real-001",
            "target_5d_return",
        )

        fv_dates = [date(2026, 5, 1), date(2026, 5, 2), date(2026, 5, 3)]
        fv_symbols = ["S0000", "S0001", "S0002", "S0003"]
        factor_values = pd.DataFrame(
            np.ones((3, 4)),
            index=fv_dates,
            columns=fv_symbols,
        )

        forward_returns, blockers, ctx = resolve_forward_returns(
            warehouse,
            factor_values,
            target_horizon=5,
            _target_engine_module=MagicMock(spec=[]),
        )

        warehouse.get_target_panel.assert_called_once_with(
            "batch-real-001", "target_5d_return"
        )
        assert ctx["latest_batch_id"] == "batch-real-001"
        assert ctx["fallback_used"] is False
        assert ctx["target_code"] == "target_5d_return"
        assert len(blockers) == 0

        assert forward_returns.shape == (3, 3)
        assert list(forward_returns.columns) == symbols
        assert list(forward_returns.index) == [
            date(2026, 5, 1), date(2026, 5, 2), date(2026, 5, 3)
        ]
        assert forward_returns.loc[date(2026, 5, 1), "S0000"] == pytest.approx(0.01)
        assert forward_returns.loc[date(2026, 5, 2), "S0001"] == pytest.approx(0.05)
        assert forward_returns.loc[date(2026, 5, 3), "S0002"] == pytest.approx(0.09)

    def test_real_target_panel_datetime_signal_date_normalized(self):
        """signal_date 是 datetime 类型时也能正确 pivot。"""
        warehouse = MagicMock()
        warehouse.get_latest_target_batch_id.return_value = "batch-dt"

        panel_rows = []
        dates_dt = pd.to_datetime(["2026-05-01", "2026-05-02"])
        for d in dates_dt:
            for s, v in [("A", 0.1), ("B", 0.2)]:
                panel_rows.append({
                    "symbol": s,
                    "signal_date": d,
                    "target_value": v,
                })
        target_panel = pd.DataFrame.from_records(panel_rows)
        warehouse.get_target_panel.return_value = (
            target_panel, "batch-dt", "target_5d_return"
        )

        fv_dates = [date(2026, 5, 1), date(2026, 5, 2)]
        factor_values = pd.DataFrame(
            np.ones((2, 2)), index=fv_dates, columns=["A", "B"],
        )

        forward_returns, blockers, ctx = resolve_forward_returns(
            warehouse, factor_values, target_horizon=5,
            _target_engine_module=MagicMock(spec=[]),
        )
        assert len(blockers) == 0
        assert forward_returns.shape == (2, 2)
        assert forward_returns.loc[date(2026, 5, 1), "A"] == pytest.approx(0.1)
        assert forward_returns.loc[date(2026, 5, 2), "B"] == pytest.approx(0.2)

    def test_real_target_panel_intersection_with_factor_values_index(self):
        """标签日期/股票仅保留与因子值的交集。"""
        warehouse = MagicMock()
        warehouse.get_latest_target_batch_id.return_value = "batch-intersect"

        panel_rows = [
            {"symbol": "A", "signal_date": "2026-05-01", "target_value": 0.01},
            {"symbol": "A", "signal_date": "2026-05-02", "target_value": 0.02},
            {"symbol": "A", "signal_date": "2026-05-10", "target_value": 0.99},
            {"symbol": "C", "signal_date": "2026-05-01", "target_value": 0.03},
        ]
        target_panel = pd.DataFrame.from_records(panel_rows)
        warehouse.get_target_panel.return_value = (
            target_panel, "batch-intersect", "target_5d_return"
        )

        fv_dates = [date(2026, 5, 1), date(2026, 5, 2)]
        factor_values = pd.DataFrame(
            np.ones((2, 2)), index=fv_dates, columns=["A", "B"],
        )

        forward_returns, _blockers, _ctx = resolve_forward_returns(
            warehouse, factor_values, target_horizon=5,
            _target_engine_module=MagicMock(spec=[]),
        )
        assert list(forward_returns.index) == fv_dates
        assert list(forward_returns.columns) == ["A"]
        assert forward_returns.loc[date(2026, 5, 1), "A"] == pytest.approx(0.01)
        assert forward_returns.loc[date(2026, 5, 2), "A"] == pytest.approx(0.02)

    def test_partial_target_coverage_keeps_real_labels_without_factor_fallback(self):
        """区间标签不完整时不得把因子自身变化冒充未来收益。"""
        warehouse = MagicMock()
        warehouse.get_latest_target_batch_id.return_value = "batch-partial"
        target_panel = pd.DataFrame.from_records([
            {"symbol": symbol, "signal_date": d, "target_value": value}
            for d, value in zip(
                ["2026-05-01", "2026-05-02", "2026-05-03"],
                [0.01, 0.02, 0.03],
            )
            for symbol in ["A", "B"]
        ])
        warehouse.get_target_panel.side_effect = [
            (target_panel, "batch-partial", "target_5d_return"),
            (target_panel, "batch-regen", "target_5d_return"),
        ]

        class FakeEngine:
            @staticmethod
            def calculate_targets(**_kwargs):
                return SimpleNamespace(calc_batch_id="batch-regen")

        factor_values = pd.DataFrame(
            np.arange(10, dtype=float).reshape(5, 2),
            index=[date(2026, 5, day) for day in range(1, 6)],
            columns=["A", "B"],
        )
        forward_returns, blockers, ctx = resolve_forward_returns(
            warehouse,
            factor_values,
            target_horizon=5,
            evaluation_start_date=date(2026, 5, 1),
            evaluation_end_date=date(2026, 5, 5),
            _target_engine_module=FakeEngine,
        )

        assert ctx["fallback_used"] is False
        assert list(forward_returns.index) == [
            date(2026, 5, 1), date(2026, 5, 2), date(2026, 5, 3),
        ]
        assert any(b["code"] == "eval.data.target_coverage_partial" for b in blockers)


class TestStoreTargetMethods:
    """Store 中新方法的基础保护：表不存在时返回 None / 空 DataFrame。"""

    def test_get_latest_target_batch_id_no_table_returns_none(self, tmp_path):
        from app.services.factors.store import FactorWarehouse

        warehouse = FactorWarehouse(tmp_path / "empty.duckdb")
        assert warehouse._table_exists("factor_targets") is False
        assert warehouse.get_latest_target_batch_id() is None

    def test_get_target_panel_no_table_returns_empty(self, tmp_path):
        from app.services.factors.store import FactorWarehouse

        warehouse = FactorWarehouse(tmp_path / "empty2.duckdb")
        df, bid, tcode = warehouse.get_target_panel("any", "target_5d_return")
        assert df.empty
        assert set(df.columns) == {"symbol", "signal_date", "target_value"}
        assert bid == "any"
        assert tcode == "target_5d_return"
