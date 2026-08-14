"""Task3/Task4/P0-5: execute_panel + 横截面按天后处理 + 默认日期区间 + 无 limit=500 测试。

覆盖：
- test_execute_panel_sma20_warmup: sma(close,20) 预热窗口与 SMA 值正确性
- test_zscore_cross_sectional: 按 trade_date 分组的横截面 zscore（mean≈0 std≈1）
- test_validation_days_default_meets_gate: 默认日期区间保证验证期≥50
- test_no_limit500_in_eval_path: wp5_eval_task.py 文本无 "limit=500"
"""
from __future__ import annotations

import math
import os
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pandas as pd
import pytest

from app.services.factors.factor_compiler import compile_formula
from app.services.factors.factor_executor import (
    FactorExecutor,
    PanelExecutionOutcome,
    apply_postprocess_cross_sectional,
    _zscore,
)
from app.services.factors.store import FactorWarehouse
from app.services.factors.wp5_eval_task import select_evaluation_date_range

pytestmark = pytest.mark.whitebox


# ══════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════


@pytest.fixture()
def tmp_warehouse():
    """创建临时 DuckDB 仓库并初始化 schema。"""
    fd, path = tempfile.mkstemp(suffix=".duckdb", prefix="qa_panel_test_")
    os.close(fd)
    os.remove(path)
    warehouse = FactorWarehouse(path)
    warehouse.initialize()
    yield warehouse
    try:
        os.remove(path)
    except OSError:
        pass
    lockfile = Path(str(path) + ".lock")
    if lockfile.exists():
        try:
            lockfile.unlink()
        except OSError:
            pass


def _seed_daily_bars(warehouse: FactorWarehouse, rows: list[dict]):
    now = datetime.now(timezone.utc)
    for row in rows:
        row.setdefault("ingested_at", now)
        row.setdefault("batch_id", "test-seed")
        row.setdefault("adjust", "qfq")
        row.setdefault("source", "test")
        row.setdefault("source_origin", "test")
        row.setdefault("source_row_id", 0)
    df = pd.DataFrame(rows)
    with warehouse._write_lock, warehouse.connection() as conn:
        conn.register("incoming_bars", df)
        conn.execute("""
            INSERT INTO raw_daily_bars
            (symbol, trade_date, adjust, open, high, low, close,
             volume, amount, turnover_rate, source, source_origin,
             source_row_id, ingested_at, batch_id)
            SELECT symbol, trade_date, adjust, open, high, low, close,
                   volume, amount, turnover_rate, source, source_origin,
                   source_row_id, ingested_at, batch_id
            FROM incoming_bars
        """)
        conn.unregister("incoming_bars")


def _gen_trade_dates(n_days: int, *, start: date | None = None) -> list[date]:
    """生成连续 n_days 个'伪交易日'（连续自然日，测试用）。"""
    if start is None:
        start = date(2025, 1, 1)
    return [start + timedelta(days=i) for i in range(n_days)]


# ══════════════════════════════════════════════════════════
# 1. SMA20 预热窗口 + 值正确性
# ══════════════════════════════════════════════════════════


class TestExecutePanelSma20Warmup:
    def test_sma20_warmup_and_value(self, tmp_warehouse):
        """40 交易日 × 3 只股票，公式 sma(close,20)，start_date 取第 25 个交易日。

        断言：
        1. 前 19 个交易日（在 warmup 内）不出现在结果里
        2. 第 25 日的值 ≈ 第 6-25 日（前20个有效日）的 close 均值
        """
        symbols = ["AAA", "BBB", "CCC"]
        # 生成 40 个交易日，close 按 1,2,3,...,40 线性递增（每只股票相同，便于断言）
        trade_dates = _gen_trade_dates(40)
        rows: list[dict[str, Any]] = []
        for i, td in enumerate(trade_dates):
            close_val = float(i + 1)  # 第1日=1，第40日=40
            for sym in symbols:
                rows.append({
                    "symbol": sym,
                    "trade_date": td,
                    "open": close_val,
                    "high": close_val,
                    "low": close_val,
                    "close": close_val,
                    "volume": 10000.0,
                    "amount": close_val * 10000.0,
                    "turnover_rate": 0.01,
                })
        _seed_daily_bars(tmp_warehouse, rows)

        compile_result = compile_formula(
            formula="sma(close, 20)",
            params={},
            strict_fields=True,
        )
        assert compile_result.is_valid
        plan = compile_result.execution_plan
        assert plan.max_lookback == 20

        executor = FactorExecutor(tmp_warehouse)

        # start_date = 第 25 个交易日（索引 24，0-based）
        start_idx = 24
        start_date = trade_dates[start_idx]
        end_date = trade_dates[-1]

        outcome = executor.execute_panel(plan, start_date=start_date, end_date=end_date)

        assert isinstance(outcome, PanelExecutionOutcome)
        assert outcome.n_cells > 0

        # 断言 1：结果中的最早交易日 >= start_date，warmup 内(第0-23日)不应出现
        for d in outcome.trade_dates:
            assert d >= start_date, f"warmup date {d} should not appear in result"

        # 断言 2：第 25 日（start_date）SMA 值 = 第 6-25 日 close 均值
        # close 值从 1 开始，第 6-25 日 (索引 5-24) 的值是 6,7,...,25
        # 均值 = (6+7+...+25)/20 = (6+25)*20/2 /20 = 31/2 = 15.5
        expected_sma_25th = (6.0 + 25.0) * 20.0 / 2.0 / 20.0  # = 15.5

        flong = outcome.factors_long
        target_rows = flong[flong["trade_date"] == start_date]
        assert not target_rows.empty, f"no rows for start_date {start_date}"

        for _, r in target_rows.iterrows():
            actual = r["raw_value"]
            assert actual is not None and not math.isnan(actual), "sma should not be nan"
            rel_err = abs(actual - expected_sma_25th) / abs(expected_sma_25th)
            assert rel_err <= 0.001, (
                f"SMA@{start_date} for {r['symbol']}: {actual} ≈ {expected_sma_25th}, "
                f"rel_err={rel_err:.4%} > 0.1%"
            )


# ══════════════════════════════════════════════════════════
# 2. 横截面 zscore 按天独立执行
# ══════════════════════════════════════════════════════════


class TestZscoreCrossSectional:
    def test_zscore_per_date_mean0_std1(self):
        """每个 trade_date 内 5 只股票，取 zscore 后每个分组断言 mean≈0, std≈1。"""
        # 构造 5 个交易日 × 5 只股票的测试数据，每天值不同
        n_days = 5
        n_syms = 5
        trade_dates = _gen_trade_dates(n_days)
        symbols = [f"S{i}" for i in range(n_syms)]

        records: list[dict[str, Any]] = []
        raw_values: list[float] = []
        td_series: list[date] = []
        import random
        random.seed(42)
        for td in trade_dates:
            # 每天一组不同 scale/shift 的随机值
            shift = random.uniform(-100, 100)
            scale = random.uniform(0.5, 5.0)
            day_vals = [shift + scale * random.uniform(-1, 1) for _ in range(n_syms)]
            for sym, v in zip(symbols, day_vals):
                records.append({"symbol": sym, "trade_date": td})
                raw_values.append(v)
                td_series.append(td)

        raw_s = pd.Series(raw_values)
        td_s = pd.Series(td_series)

        postprocess = {
            "zscore": {"ddof": 0},
            "missing_policy": "exclude",
        }
        _, normalized = apply_postprocess_cross_sectional(raw_s, td_s, postprocess)

        # 按天分组断言
        df = pd.DataFrame({"td": td_s, "norm": normalized.values})
        for td, grp in df.groupby("td"):
            vals = grp["norm"].dropna()
            assert len(vals) == n_syms, "all 5 stocks should have zscore"
            m = vals.mean()
            s = vals.std(ddof=0)
            assert abs(m) < 1e-6, f"date {td} zscore mean={m}, expect ~0"
            assert abs(s - 1.0) < 1e-3, f"date {td} zscore std={s}, expect ~1"

    def test_zscore_raw_function_mean0_std1(self):
        """直接对单组 Series 调用 _zscore，mean≈0 std≈1。"""
        s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
        z = _zscore(s, ddof=0)
        assert abs(z.mean()) < 1e-9
        assert abs(z.std(ddof=0) - 1.0) < 1e-9


# ══════════════════════════════════════════════════════════
# 3. 默认日期区间：验证期≥50 天
# ══════════════════════════════════════════════════════════


class TestValidationDaysDefaultMeetsGate:
    def test_400_trade_dates_splits_satisfy_gate(self, tmp_warehouse):
        """mock 400 个交易日 → 默认切分：总交易日≥275，validation_days≥55。"""
        n = 400
        trade_dates = _gen_trade_dates(n)
        rows: list[dict[str, Any]] = []
        for td in trade_dates:
            rows.append({
                "symbol": "DUMMY",
                "trade_date": td,
                "open": 10.0,
                "high": 11.0,
                "low": 9.0,
                "close": 10.0,
                "volume": 1000.0,
                "amount": 10000.0,
                "turnover_rate": 0.01,
            })
        _seed_daily_bars(tmp_warehouse, rows)

        # 截止日 = 最后一日之后（保证所有 400 日均为历史）
        cutoff_date = trade_dates[-1] + timedelta(days=1)

        start_date, end_date, all_tds, blockers = select_evaluation_date_range(
            tmp_warehouse,
            cutoff_date,
            min_validation_days=50,
            purge_days=0,
            embargo_days=5,
            train_ratio=0.6,
            validation_ratio=0.2,
        )

        # 不应该出现 error blocker
        has_err = any(b.get("severity") == "error" for b in blockers)
        assert not has_err, f"unexpected error blockers: {blockers}"

        # 找到 start_date, end_date 在全量列表中的索引
        start_idx = all_tds.index(start_date)
        end_idx = all_tds.index(end_date)
        total_selected = end_idx - start_idx + 1

        # 总交易日 ≥ 275
        assert total_selected >= 275, f"total selected {total_selected} < 275"

        # 验证期天数 = int(total * 0.2)（按 factor_evaluator 划分）
        validation_days = int(total_selected * 0.2)
        assert validation_days >= 55, (
            f"validation_days {validation_days} < 55 "
            f"(min_validation_days+purge+embargo = 55)"
        )

    def test_100_trade_days_triggers_insufficient_blocker(self, tmp_warehouse):
        """仅 100 交易日 → 触发 eval.data.historical_data_insufficient error blocker。"""
        n = 100
        trade_dates = _gen_trade_dates(n)
        rows: list[dict[str, Any]] = []
        for td in trade_dates:
            rows.append({
                "symbol": "DUMMY",
                "trade_date": td,
                "open": 10.0,
                "high": 11.0,
                "low": 9.0,
                "close": 10.0,
                "volume": 1000.0,
                "amount": 10000.0,
                "turnover_rate": 0.01,
            })
        _seed_daily_bars(tmp_warehouse, rows)
        cutoff_date = trade_dates[-1] + timedelta(days=1)

        _, _, _, blockers = select_evaluation_date_range(
            tmp_warehouse,
            cutoff_date,
            min_validation_days=50,
            purge_days=0,
            embargo_days=5,
            validation_ratio=0.2,
        )

        codes = {b.get("code") for b in blockers if b.get("severity") == "error"}
        assert "eval.data.historical_data_insufficient" in codes, (
            f"expected blocker eval.data.historical_data_insufficient, got {codes}"
        )


# ══════════════════════════════════════════════════════════
# 4. 文本 grep：评价路径不再出现 limit=500
# ══════════════════════════════════════════════════════════


class TestNoLimit500InEvalPath:
    def test_wp5_eval_task_no_limit500(self):
        """对 wp5_eval_task.py 文本 grep，断言不出现 'limit=500'。"""
        from app.services.factors import wp5_eval_task as wp5_mod
        path = Path(wp5_mod.__file__)
        text = path.read_text(encoding="utf-8")
        assert "limit=500" not in text, (
            "wp5_eval_task.py 中仍存在 'limit=500'，请改为 execute_panel 批量计算。"
        )

    def test_wp5_eval_task_no_preview_loop_call(self):
        """评价 worker 路径内不得出现 executor.preview( 的循环调用（查 for.*preview）。"""
        from app.services.factors import wp5_eval_task as wp5_mod
        import re
        path = Path(wp5_mod.__file__)
        lines = path.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            # 只检查评价 worker 实际路径，注释不算
            if stripped.startswith("#"):
                continue
            if re.search(r"executor\.preview\s*\(", stripped):
                raise AssertionError(
                    f"wp5_eval_task.py:{i} 仍出现 executor.preview() 调用，"
                    "评价任务必须使用 execute_panel() 批量计算。"
                )


# ══════════════════════════════════════════════════════════
# 5. ExecutionPlan.max_lookback 字段校验
# ══════════════════════════════════════════════════════════


class TestExecutionPlanMaxLookback:
    def test_compile_max_lookback_sma20(self):
        """sma(close,20) → plan.max_lookback == 20。"""
        r = compile_formula(formula="sma(close, 20)", strict_fields=True)
        assert r.is_valid
        assert r.execution_plan.max_lookback == 20

    def test_compile_max_lookback_nested_windows(self):
        """最大窗口来自 rsi 风格假设的多窗口嵌套，取最大 = 50。"""
        # 这里使用 stddev(close, 50) + sma(close, 20) → max 应为 50
        r = compile_formula(formula="stddev(close, 50) - sma(close, 20)", strict_fields=True)
        assert r.is_valid
        assert r.execution_plan.max_lookback == 50

    def test_compile_max_lookback_nofunc(self):
        """无滚动函数：max_lookback == 1（默认）。"""
        r = compile_formula(formula="close / open - 1", strict_fields=True)
        assert r.is_valid
        assert r.execution_plan.max_lookback == 1
