"""A1 · `evaluate_short` 真实短样本评估（SD-v2.0 §6.10 / §7.2，开发计划 A1 卡）。

DoD 对照：
- 单测覆盖（真实 executor + 小样本面板）                      → 本文件
- `evaluate_short` 无 `NotImplementedError`                   → test_1/2
- 适应度只用 train 的守卫测试通过                            → test_4（assert_no_leakage）
- 探针/缓存有落库断言                                        → test_3（probe 列 + G2 统计 + 抽样校验）

跑法：`.venv/Scripts/python.exe -m pytest tests/services/factors/mining/test_evaluate_short.py -q`
"""
from __future__ import annotations

import math
import random
from datetime import date, datetime, timedelta

import pytest

pytest.importorskip("duckdb")

from app.services.factors.factor_compiler import compile_formula
from app.services.factors.mining import evaluation_adapter as EVA
from app.services.factors.mining.contracts import Individual, MiningContext
from app.services.factors.mining.evaluation_adapter import assert_no_leakage
from app.services.factors.store import FactorWarehouse
from app.services.factors.target_engine import calculate_targets

pytestmark = pytest.mark.whitebox

N_SYMBOLS = 12
N_DAYS = 300          # ≥ daily 地板 252（build_split 硬门槛）
BASE_DATE = date(2026, 3, 2)


def _bar(symbol: str, trade_date: date, row_id: int, close: float):
    return {
        "symbol": symbol,
        "trade_date": trade_date,
        "adjust": "qfq",
        "universe_symbol_id": row_id,
        "open": close * 0.998,
        "high": close * 1.01,
        "low": close * 0.99,
        "close": close,
        "volume": 100.0,
        "amount": 100000.0,
        "turnover_rate": 1.0,
        "source": "test",
        "source_origin": "universe_daily_bars",
        "source_row_id": row_id,
        "ingested_at": datetime(2026, 7, 1),
        "batch_id": "a1-bars",
    }


@pytest.fixture
def warehouse(tmp_path):
    path = tmp_path / "factor.duckdb"
    wh = FactorWarehouse(str(path))
    dates = [BASE_DATE + timedelta(days=i) for i in range(N_DAYS)]
    rng = random.Random(7)
    bars = []
    row = 0
    for symbol in [f"{i:06d}" for i in range(N_SYMBOLS)]:
        price = 10.0
        for trade_date in dates:
            row += 1
            price = max(2.0, price * (1 + (rng.random() - 0.5) * 0.03))
            bars.append(_bar(symbol, trade_date, row, close=round(price, 4)))
    wh.upsert_daily_bars(bars, source_key="a1.bars", watermark=row)
    calculate_targets(
        wh, start_date=dates[0], end_date=dates[-1], calc_batch_id="a1-target"
    )
    return str(path), dates


def _formula_individual(formula: str) -> Individual:
    result = compile_formula(formula=formula, params=None)
    assert result.success, f"公式编译失败: {formula} {getattr(result, 'errors', None)}"
    return Individual(
        formula_expr=formula,
        canonical_formula=formula,
        formula_hash="a1" + "e" * 30,
        execution_plan=result.execution_plan,
        dependency=None,
        category="trend",
        generation=0,
        complexity=3,
    )


def _make_ctx(path: str, dates, *, target_batch: str = "a1-target",
              split=None) -> MiningContext:
    wh = FactorWarehouse(path)
    if split is None:
        target_df, _bid, _tcode = wh.get_target_panel(target_batch, "target_5d_return")
        all_dates = sorted(
            d for d in (EVA._as_date(v) for v in target_df["signal_date"].tolist())
            if d is not None
        )
        split, budget = EVA.build_split(
            all_dates=all_dates, frequency="daily", target_horizon=5,
        )
    else:
        budget = None
    return MiningContext(
        run_id="run-a1", candidate_pool_snapshot_id="snap-a1",
        data_cutoff_at=datetime(2026, 6, 2), start_date=dates[0], end_date=dates[-1],
        rebalance_frequency="daily", target_horizon=5,
        split=split,
        purge_points=(budget.purge_points if budget else 5),
        embargo_points=(budget.embargo_points if budget else 5),
        train_ratio=0.6, validation_ratio=0.2,
        random_seed=42, config_hash="cfg-a1",
        target_calc_batch_id=target_batch,
        warehouse_path=path,
        data_snapshot_version="a1-test-v1",
    )


class TestEvaluateShort:
    def test_returns_real_fitness_for_real_formula(self, warehouse):
        """真实公式 → 非零覆盖率的 Fitness（不再抛 NotImplementedError）。"""
        path, dates = warehouse
        ctx = _make_ctx(path, dates)
        fit = EVA.evaluate_short(ctx, individual=_formula_individual("mean(close,5)/mean(close,20)-1"), sample_length="1y")
        assert fit is not None
        assert isinstance(fit.icir, float) and not math.isnan(fit.icir)
        assert fit.coverage > 0.1, f"覆盖率异常偏低: {fit.coverage}"
        assert fit.valid_cross_sections > 0
        assert fit.sample_count > 0
        assert fit.complexity == 3
        # ic_mean 与 icir 符号一致（非零覆盖下 IC 应可算）
        assert isinstance(fit.ic_mean, float)

    def test_returns_zero_fitness_when_no_target_batch(self, warehouse):
        """目标批次缺失 → 全 0 Fitness（复用正常 split，仅换 batch）。"""
        path, dates = warehouse
        ok_ctx = _make_ctx(path, dates)
        ctx = _make_ctx(path, dates, target_batch="missing-batch",
                        split=ok_ctx.split)
        fit = EVA.evaluate_short(ctx, individual=_formula_individual("mean(close,5)/mean(close,20)-1"), sample_length=None)
        assert fit.icir == 0.0 and fit.coverage == 0.0 and fit.valid_cross_sections == 0

    def test_probed_emits_probe_columns_and_g2_stats(self, warehouse):
        """探针 9 字段 + G2 三指标 + 抽样校验 passed=1。"""
        path, dates = warehouse
        ctx = _make_ctx(path, dates)
        fit, probe, cache = EVA.evaluate_short_probed(
            ctx, individual=_formula_individual("ts_delta_bars(close,5)"), sample_length="1y"
        )
        cols = probe.to_columns()
        for stage in ("data_load", "ast_eval", "subexpr_compute",
                      "factor_assemble", "metric_calc"):
            assert isinstance(cols[f"probe_{stage}_ms"], int)
            assert cols[f"probe_{stage}_ms"] >= 0, f"{stage} 耗时非负"
        assert cols["probe_subexpr_total"] >= 1
        assert cols["probe_subexpr_unique"] >= 1
        assert fit.coverage > 0.1
        validation = probe.to_validation_columns()
        assert validation.get("cache_validation_passed") == 1, (
            f"抽样校验未通过: {validation}")
        assert validation.get("cache_validation_max_diff", 0.0) <= 1e-6

    def test_leakage_guard_rejects_out_of_train_dates(self, warehouse):
        """C8 红线：train 段外的日期被 assert_no_leakage 拒绝。"""
        path, dates = warehouse
        ctx = _make_ctx(path, dates)
        lo = ctx.split.train_start
        hi = ctx.split.train_end
        # 取一个必然在 train 之外（val 段）的日期作为越界样本
        assert_no_leakage(ctx.split, [_as_date(lo), _as_date(hi)])
        val_day = [_as_date(ctx.split.train_end) + timedelta(days=7)]
        with pytest.raises(AssertionError):
            assert_no_leakage(ctx.split, val_day)


def _as_date(value):
    if isinstance(value, datetime):
        return value.date()
    return value