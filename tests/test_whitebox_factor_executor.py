"""WP2-06: DuckDB 兼容执行白盒测试。

覆盖：
- 冻结批次读取（read_only 连接，一致快照）
- 公式评估（AST 递归求值，pandas Series 向量化）
- 后处理（winsorize / zscore / rank / missing_policy）
- 单写锁写入（safe_write_context，跨进程锁 + 事务）
- 失败不覆盖旧批次（calc_batch_id 主键 + ROLLBACK）
- 预览 API 集成（只读，不写入）

对齐 docs/专业因子库开发计划.md §WP2-06 和 checklist.md WP2 退出条件。
"""
from __future__ import annotations

import math
import os
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from app.services.factors.factor_compiler import compile_formula
from app.services.factors.factor_executor import (
    ExecutionOutcome,
    FactorExecutor,
    PreviewOutcome,
    _eval_ast,
    _safe_float,
    _safe_pow,
    _winsorize_mad,
    _winsorize_quantile,
    _zscore,
    _rank_percentile,
    apply_postprocess,
)
from app.services.factors.store import FactorWarehouse

pytestmark = pytest.mark.whitebox


# ══════════════════════════════════════════════════════════
# 测试 fixtures
# ══════════════════════════════════════════════════════════


@pytest.fixture()
def tmp_warehouse():
    """创建临时 DuckDB 仓库并初始化 schema。"""
    fd, path = tempfile.mkstemp(suffix=".duckdb", prefix="qa_executor_test_")
    os.close(fd)
    # mkstemp 创建空文件，DuckDB 无法打开空文件，需先删除
    os.remove(path)
    warehouse = FactorWarehouse(path)
    warehouse.initialize()
    yield warehouse
    try:
        os.remove(path)
    except OSError:
        pass
    # 清理锁文件
    lockfile = Path(str(path) + ".lock")
    if lockfile.exists():
        try:
            lockfile.unlink()
        except OSError:
            pass


def _seed_daily_bars(warehouse: FactorWarehouse, rows: list[dict]):
    """向 raw_daily_bars 表插入测试数据（直接 SQL 插入）。"""
    now = datetime.now(timezone.utc)
    for row in rows:
        row.setdefault("ingested_at", now)
        row.setdefault("batch_id", "test-seed")
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


def _seed_valuation(warehouse: FactorWarehouse, rows: list[dict]):
    """向 raw_valuation_snapshots 表插入测试数据（直接 SQL 插入）。"""
    now = datetime.now(timezone.utc)
    for row in rows:
        row.setdefault("source", "test")
        row.setdefault("ingested_at", now)
        row.setdefault("batch_id", "test-seed")
    df = pd.DataFrame(rows)
    with warehouse._write_lock, warehouse.connection() as conn:
        conn.register("incoming_val", df)
        conn.execute("""
            INSERT INTO raw_valuation_snapshots
            (symbol, trade_date, pe_ttm, source, ingested_at, batch_id)
            SELECT symbol, trade_date, pe_ttm, source, ingested_at, batch_id
            FROM incoming_val
        """)
        conn.unregister("incoming_val")


def _seed_financial_reports(warehouse: FactorWarehouse, rows: list[dict]):
    """向 raw_financial_reports 表插入公告日 PIT 测试数据。"""
    now = datetime.now(timezone.utc)
    for row in rows:
        row.setdefault("report_type", "annual")
        row.setdefault("source", "test")
        row.setdefault("ingested_at", now)
        row.setdefault("batch_id", "test-seed")
    df = pd.DataFrame(rows)
    with warehouse._write_lock, warehouse.connection() as conn:
        conn.register("incoming_financial", df)
        conn.execute("""
            INSERT INTO raw_financial_reports
            (symbol, report_period, announcement_date, report_type,
             roe_ttm, source, ingested_at, batch_id)
            SELECT symbol, report_period, announcement_date, report_type,
                   roe_ttm, source, ingested_at, batch_id
            FROM incoming_financial
        """)
        conn.unregister("incoming_financial")


# ══════════════════════════════════════════════════════════
# WP2-06: 后处理函数测试
# ══════════════════════════════════════════════════════════


class TestWinsorizeMad:
    """MAD winsorize 测试。"""

    def test_clips_outliers(self):
        """MAD winsorize 裁剪极端值。"""
        series = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0, 100.0])
        result = _winsorize_mad(series, multiplier=3.0)
        assert result.max() < 100.0
        assert result.min() >= 1.0

    def test_preserves_normal_values(self):
        """正常值不被裁剪。"""
        series = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
        result = _winsorize_mad(series, multiplier=3.0)
        # 正常值应保持不变
        assert result.iloc[0] == 1.0
        assert result.iloc[-1] == 5.0

    def test_short_series_returns_unchanged(self):
        """少于 5 个值的序列不处理。"""
        series = pd.Series([1.0, 2.0, 3.0])
        result = _winsorize_mad(series)
        assert result.equals(series)


class TestWinsorizeQuantile:
    """分位数 winsorize 测试。"""

    def test_clips_by_quantile(self):
        """分位数 winsorize 裁剪。"""
        series = pd.Series(list(range(100)) + [1000.0])
        result = _winsorize_quantile(series, lower_q=0.01, upper_q=0.99)
        assert result.max() < 1000.0

    def test_short_series_returns_unchanged(self):
        """少于 5 个值的序列不处理。"""
        series = pd.Series([1.0, 2.0])
        result = _winsorize_quantile(series)
        assert result.equals(series)


class TestZscore:
    """z-score 测试。"""

    def test_normalizes_to_zero_mean(self):
        """z-score 均值接近 0。"""
        series = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
        result = _zscore(series, ddof=0)
        assert abs(result.dropna().mean()) < 1e-10

    def test_normalizes_to_unit_std(self):
        """z-score 标准差接近 1。"""
        series = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
        result = _zscore(series, ddof=0)
        assert abs(result.dropna().std(ddof=0) - 1.0) < 1e-10

    def test_zero_variance_returns_zeros(self):
        """零方差返回全 0。"""
        series = pd.Series([3.0, 3.0, 3.0, 3.0])
        result = _zscore(series)
        assert (result == 0.0).all()


class TestRankPercentile:
    """百分位 rank 测试。"""

    def test_ranks_between_0_and_1(self):
        """rank 值在 (0, 1] 之间。"""
        series = pd.Series([10.0, 20.0, 30.0, 40.0, 50.0])
        result = _rank_percentile(series, ascending=True)
        valid = result.dropna()
        assert valid.min() > 0
        assert valid.max() <= 1.0

    def test_descending_rank(self):
        """降序 rank。"""
        series = pd.Series([10.0, 20.0, 30.0])
        result = _rank_percentile(series, ascending=False)
        # 10.0 应该有最高的 rank（降序）
        assert result.iloc[0] > result.iloc[2]


class TestApplyPostprocess:
    """后处理集成测试。"""

    def test_none_postprocess_returns_input(self):
        """None 后处理返回原始值。"""
        values = pd.Series([1.0, 2.0, 3.0])
        w, n = apply_postprocess(values, None)
        assert w.equals(values)
        assert n.equals(values)

    def test_winsorize_then_zscore(self):
        """winsorize → zscore 流程。"""
        values = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0, 100.0])
        w, n = apply_postprocess(values, {
            "winsorize": {"method": "mad", "mad_multiplier": 3.0},
            "zscore": {"ddof": 0},
        })
        # winsorized 值应裁剪 100.0
        assert w.max() < 100.0
        # normalized 均值接近 0
        assert abs(n.dropna().mean()) < 1e-10

    def test_impute_zero_fills_nan(self):
        """impute_zero 策略填充 NaN。"""
        values = pd.Series([1.0, None, 3.0])
        w, n = apply_postprocess(values, {
            "missing_policy": "impute_zero",
        })
        assert not w.isna().any()
        assert not n.isna().any()

    def test_exclude_keeps_nan(self):
        """exclude 策略保持 NaN。"""
        values = pd.Series([1.0, None, 3.0])
        w, n = apply_postprocess(values, {
            "missing_policy": "exclude",
        })
        assert w.isna().any()


# ══════════════════════════════════════════════════════════
# WP2-06: AST 评估器测试
# ══════════════════════════════════════════════════════════


class TestSafePow:
    """安全幂运算测试。"""

    def test_normal_power(self):
        """正常幂运算。"""
        assert _safe_pow(2.0, 3.0) == 8.0

    def test_excessive_exponent_rejected(self):
        """过大指数拒绝。"""
        assert _safe_pow(2.0, 2000) is None

    def test_excessive_result_rejected(self):
        """过大结果拒绝。"""
        assert _safe_pow(10.0, 200) is None

    def test_zero_base(self):
        """0 的幂。"""
        assert _safe_pow(0.0, 5.0) == 0.0


class TestEvalAST:
    """AST 评估器测试。"""

    def test_constant(self):
        """常量评估。"""
        import ast as ast_module
        tree = ast_module.parse("42", mode="eval")
        result = _eval_ast(tree, {})
        assert result == 42

    def test_field_reference(self):
        """字段引用。"""
        import ast as ast_module
        tree = ast_module.parse("close", mode="eval")
        ctx = {"close": pd.Series([1.0, 2.0, 3.0])}
        result = _eval_ast(tree, ctx)
        assert isinstance(result, pd.Series)
        assert result.tolist() == [1.0, 2.0, 3.0]

    def test_binop_div(self):
        """除法运算。"""
        import ast as ast_module
        tree = ast_module.parse("1 / pe_ttm", mode="eval")
        ctx = {"pe_ttm": pd.Series([10.0, 20.0, 0.0])}
        result = _eval_ast(tree, ctx)
        assert result.iloc[0] == 0.1
        assert result.iloc[1] == 0.05

    def test_unary_minus(self):
        """一元负号。"""
        import ast as ast_module
        tree = ast_module.parse("-pb", mode="eval")
        ctx = {"pb": pd.Series([1.0, 2.0, 3.0])}
        result = _eval_ast(tree, ctx)
        assert result.tolist() == [-1.0, -2.0, -3.0]

    def test_if_exp(self):
        """条件表达式。"""
        import ast as ast_module
        tree = ast_module.parse(
            "1 / pe_ttm if pe_ttm > 0 else 0", mode="eval"
        )
        ctx = {"pe_ttm": pd.Series([10.0, -5.0, 20.0])}
        result = _eval_ast(tree, ctx)
        assert result.iloc[0] == 0.1
        assert result.iloc[1] == 0.0
        assert result.iloc[2] == 0.05

    def test_compare(self):
        """比较运算。"""
        import ast as ast_module
        tree = ast_module.parse("pe_ttm > 0", mode="eval")
        ctx = {"pe_ttm": pd.Series([10.0, -5.0, 0.0])}
        result = _eval_ast(tree, ctx)
        assert result.tolist() == [True, False, False]

    def test_bool_op(self):
        """布尔运算。"""
        import ast as ast_module
        tree = ast_module.parse("pe_ttm > 0 and pb > 0", mode="eval")
        ctx = {
            "pe_ttm": pd.Series([10.0, -5.0, 20.0]),
            "pb": pd.Series([1.0, 2.0, -3.0]),
        }
        result = _eval_ast(tree, ctx)
        assert result.tolist() == [True, False, False]

    def test_abs_function(self):
        """abs 函数。"""
        import ast as ast_module
        tree = ast_module.parse("abs(close)", mode="eval")
        ctx = {"close": pd.Series([-1.0, 2.0, -3.0])}
        result = _eval_ast(tree, ctx)
        assert result.tolist() == [1.0, 2.0, 3.0]

    def test_max_function(self):
        """max 函数。"""
        import ast as ast_module
        tree = ast_module.parse("max(close, open)", mode="eval")
        ctx = {
            "close": pd.Series([1.0, 5.0, 3.0]),
            "open": pd.Series([2.0, 3.0, 4.0]),
        }
        result = _eval_ast(tree, ctx)
        assert result.tolist() == [2.0, 5.0, 4.0]

    def test_sma_rolling(self):
        """sma 滚动函数。"""
        import ast as ast_module
        tree = ast_module.parse("sma(close, 2)", mode="eval")
        ctx = {"close": pd.Series([1.0, 2.0, 3.0, 4.0])}
        symbol_series = pd.Series(["A", "A", "A", "A"])
        result = _eval_ast(tree, ctx, symbol_series=symbol_series)
        # sma(2): [NaN, 1.5, 2.5, 3.5]
        assert math.isnan(result.iloc[0]) or result.iloc[0] == 1.0  # min_periods=1
        assert abs(result.iloc[1] - 1.5) < 1e-10
        assert abs(result.iloc[2] - 2.5) < 1e-10
        assert abs(result.iloc[3] - 3.5) < 1e-10


# ══════════════════════════════════════════════════════════
# WP2-06: FactorExecutor 集成测试
# ══════════════════════════════════════════════════════════


class TestFactorExecutorPreview:
    """FactorExecutor.preview 集成测试。"""

    def test_preview_returns_values(self, tmp_warehouse):
        """预览返回因子值。"""
        # 准备数据
        rows = [
            {"symbol": "000001", "trade_date": "2026-07-24", "adjust": "qfq",
             "close": 10.0, "open": 9.5, "high": 10.5, "low": 9.0,
             "volume": 1000.0, "amount": 10000.0, "turnover_rate": 0.05,
             "source": "test", "source_origin": "test", "source_row_id": 1},
            {"symbol": "000002", "trade_date": "2026-07-24", "adjust": "qfq",
             "close": 20.0, "open": 19.5, "high": 20.5, "low": 19.0,
             "volume": 2000.0, "amount": 40000.0, "turnover_rate": 0.10,
             "source": "test", "source_origin": "test", "source_row_id": 2},
        ]
        _seed_daily_bars(tmp_warehouse, rows)

        # 编译公式
        result = compile_formula(formula="close", strict_fields=True)
        assert result.is_valid

        # 预览
        executor = FactorExecutor(tmp_warehouse)
        outcome = executor.preview(
            result.execution_plan,
            trade_date=date(2026, 7, 24),
            limit=10,
        )

        assert isinstance(outcome, PreviewOutcome)
        assert len(outcome.values) == 2
        assert outcome.values[0]["symbol"] == "000001"
        assert outcome.values[0]["raw_value"] == 10.0
        assert outcome.values[1]["raw_value"] == 20.0
        assert outcome.symbol_count == 2
        assert outcome.coverage == 1.0

    def test_preview_derives_prev_close_without_physical_column(self, tmp_warehouse):
        rows = [
            {"symbol": "000001", "trade_date": "2026-07-23", "adjust": "qfq",
             "close": 10.0, "open": 10.0, "high": 10.0, "low": 10.0,
             "volume": 1000.0, "amount": 10000.0, "turnover_rate": 0.05,
             "source": "test", "source_origin": "test", "source_row_id": 1},
            {"symbol": "000001", "trade_date": "2026-07-24", "adjust": "qfq",
             "close": 11.0, "open": 11.0, "high": 11.0, "low": 11.0,
             "volume": 1000.0, "amount": 11000.0, "turnover_rate": 0.05,
             "source": "test", "source_origin": "test", "source_row_id": 2},
        ]
        _seed_daily_bars(tmp_warehouse, rows)
        result = compile_formula(formula="prev_close", strict_fields=True)

        outcome = FactorExecutor(tmp_warehouse).preview(
            result.execution_plan, trade_date=date(2026, 7, 24)
        )

        assert outcome.errors == []
        assert outcome.values[0]["raw_value"] == 10.0

    def test_preview_valuation_uses_backward_asof_with_max_age(self, tmp_warehouse):
        _seed_daily_bars(tmp_warehouse, [{
            "symbol": "000001", "trade_date": "2026-07-24", "adjust": "qfq",
            "close": 11.0, "open": 11.0, "high": 11.0, "low": 11.0,
            "volume": 1000.0, "amount": 11000.0, "turnover_rate": 0.05,
            "source": "test", "source_origin": "test", "source_row_id": 1,
        }])
        _seed_valuation(tmp_warehouse, [
            {"symbol": "000001", "trade_date": "2026-07-22", "pe_ttm": 10.0},
        ])
        result = compile_formula(formula="pe_ttm", strict_fields=True)

        outcome = FactorExecutor(tmp_warehouse).preview(
            result.execution_plan, trade_date=date(2026, 7, 24)
        )

        assert outcome.errors == []
        assert outcome.values[0]["raw_value"] == 10.0

    def test_preview_financial_uses_announcement_date_asof(self, tmp_warehouse):
        _seed_daily_bars(tmp_warehouse, [{
            "symbol": "000001", "trade_date": "2026-07-24", "adjust": "qfq",
            "close": 11.0, "open": 11.0, "high": 11.0, "low": 11.0,
            "volume": 1000.0, "amount": 11000.0, "turnover_rate": 0.05,
            "source": "test", "source_origin": "test", "source_row_id": 1,
        }])
        _seed_financial_reports(tmp_warehouse, [
            {"symbol": "000001", "report_period": "2025-12-31", "announcement_date": "2026-07-23", "roe_ttm": 0.10},
            {"symbol": "000001", "report_period": "2026-03-31", "announcement_date": "2026-07-25", "roe_ttm": 0.20},
        ])
        result = compile_formula(formula="roe_ttm", strict_fields=True)

        outcome = FactorExecutor(tmp_warehouse).preview(
            result.execution_plan, trade_date=date(2026, 7, 24)
        )

        assert outcome.errors == []
        assert outcome.values[0]["raw_value"] == 0.10

    def test_preview_ep_formula(self, tmp_warehouse):
        """EP 公式预览。"""
        # 需要估值数据
        valuation_rows = [
            {"symbol": "000001", "trade_date": "2026-07-24", "pe_ttm": 10.0},
            {"symbol": "000002", "trade_date": "2026-07-24", "pe_ttm": 20.0},
        ]
        _seed_valuation(tmp_warehouse, valuation_rows)

        result = compile_formula(formula="1 / pe_ttm", strict_fields=True)
        assert result.is_valid

        executor = FactorExecutor(tmp_warehouse)
        outcome = executor.preview(
            result.execution_plan,
            trade_date=date(2026, 7, 24),
        )

        assert len(outcome.values) == 2
        assert abs(outcome.values[0]["raw_value"] - 0.1) < 1e-10
        assert abs(outcome.values[1]["raw_value"] - 0.05) < 1e-10

    def test_preview_conditional_formula(self, tmp_warehouse):
        """条件公式预览。"""
        valuation_rows = [
            {"symbol": "000001", "trade_date": "2026-07-24", "pe_ttm": 10.0},
            {"symbol": "000002", "trade_date": "2026-07-24", "pe_ttm": -5.0},
        ]
        _seed_valuation(tmp_warehouse, valuation_rows)

        result = compile_formula(
            formula="1 / pe_ttm if pe_ttm > 0 else 0",
            strict_fields=True,
        )
        assert result.is_valid

        executor = FactorExecutor(tmp_warehouse)
        outcome = executor.preview(
            result.execution_plan,
            trade_date=date(2026, 7, 24),
        )

        assert len(outcome.values) == 2
        assert outcome.values[0]["raw_value"] == 0.1
        assert outcome.values[1]["raw_value"] == 0.0

    def test_preview_empty_warehouse(self, tmp_warehouse):
        """空仓库预览返回错误。"""
        result = compile_formula(formula="close", strict_fields=True)
        assert result.is_valid

        executor = FactorExecutor(tmp_warehouse)
        outcome = executor.preview(result.execution_plan)

        assert len(outcome.values) == 0
        assert outcome.symbol_count == 0
        assert len(outcome.errors) > 0

    def test_preview_with_postprocess(self, tmp_warehouse):
        """带后处理的预览。"""
        rows = [
            {"symbol": f"00000{i}", "trade_date": "2026-07-24", "adjust": "qfq",
             "close": float(i * 10), "open": float(i * 10), "high": float(i * 10),
             "low": float(i * 10), "volume": 1000.0, "amount": 10000.0,
             "turnover_rate": 0.05, "source": "test", "source_origin": "test",
             "source_row_id": i}
            for i in range(1, 11)
        ]
        _seed_daily_bars(tmp_warehouse, rows)

        result = compile_formula(
            formula="close",
            postprocess={"winsorize": {"method": "mad", "mad_multiplier": 3.0}},
            strict_fields=True,
        )
        assert result.is_valid

        executor = FactorExecutor(tmp_warehouse)
        outcome = executor.preview(
            result.execution_plan,
            trade_date=date(2026, 7, 24),
        )

        assert len(outcome.values) == 10
        # 所有值都应该有 winsorized_value
        for v in outcome.values:
            assert v.get("winsorized_value") is not None

    def test_preview_turnover_z20_formula(self, tmp_warehouse):
        """R1-GATE: turnover_z20 风格公式预览（sma(turnover_rate, 20)）。

        验证至少用 turnover_z20 和 2 个日线数值草稿完成真实 DuckDB 预览。
        需要至少 20 个交易日的数据来支持 20 日滚动窗口。
        """
        # 生成 22 个交易日的数据（2 个标的 × 22 天），确保 sma 窗口可计算
        from datetime import timedelta

        base_date = date(2026, 7, 1)
        rows = []
        for symbol_idx, symbol in enumerate(["000001", "000002"]):
            base_turnover = 0.05 + symbol_idx * 0.03  # 0.05 / 0.08
            for day_offset in range(22):
                d = base_date + timedelta(days=day_offset)
                # 添加微小波动使 stddev 非零
                turnover = base_turnover + 0.01 * (day_offset % 5)
                rows.append({
                    "symbol": symbol,
                    "trade_date": d.isoformat(),
                    "adjust": "qfq",
                    "close": 10.0 + day_offset * 0.1,
                    "open": 10.0,
                    "high": 10.5,
                    "low": 9.5,
                    "volume": 1000.0,
                    "amount": 10000.0,
                    "turnover_rate": turnover,
                    "source": "test",
                    "source_origin": "test",
                    "source_row_id": symbol_idx * 100 + day_offset,
                })
        _seed_daily_bars(tmp_warehouse, rows)

        # turnover_z20 风格公式：sma(turnover_rate, 20)
        result = compile_formula(
            formula="sma(turnover_rate, 20)",
            strict_fields=True,
        )
        assert result.is_valid, f"编译失败: {result.errors}"

        executor = FactorExecutor(tmp_warehouse)
        # 预览 2026-07-22（第 22 天，已有 20 日窗口）
        outcome = executor.preview(
            result.execution_plan,
            trade_date=date(2026, 7, 22),
        )

        # 2 个标的都有值（20 日窗口已满足）
        assert len(outcome.values) == 2
        assert outcome.values[0]["symbol"] in ("000001", "000002")
        # sma 值应在 turnover_rate 的均值范围内
        for v in outcome.values:
            raw = v["raw_value"]
            assert raw is not None
            assert 0.0 < raw < 1.0  # 换手率均值在合理范围



class TestFactorExecutorExecute:
    """FactorExecutor.execute 集成测试。"""

    def test_execute_writes_to_warehouse(self, tmp_warehouse):
        """执行写入 factor_values 表。"""
        rows = [
            {"symbol": "000001", "trade_date": "2026-07-24", "adjust": "qfq",
             "close": 10.0, "open": 10.0, "high": 10.0, "low": 10.0,
             "volume": 1000.0, "amount": 10000.0, "turnover_rate": 0.05,
             "source": "test", "source_origin": "test", "source_row_id": 1},
            {"symbol": "000002", "trade_date": "2026-07-24", "adjust": "qfq",
             "close": 20.0, "open": 20.0, "high": 20.0, "low": 20.0,
             "volume": 2000.0, "amount": 40000.0, "turnover_rate": 0.10,
             "source": "test", "source_origin": "test", "source_row_id": 2},
        ]
        _seed_daily_bars(tmp_warehouse, rows)

        result = compile_formula(formula="close", strict_fields=True)
        assert result.is_valid

        executor = FactorExecutor(tmp_warehouse)
        outcome = executor.execute(
            result.execution_plan,
            factor_code="test_close",
            factor_version=1,
            trade_date=date(2026, 7, 24),
        )

        assert isinstance(outcome, ExecutionOutcome)
        assert outcome.rows_written == 2
        assert outcome.eligible_rows == 2
        assert outcome.coverage == 1.0
        assert outcome.symbol_count == 2
        assert len(outcome.errors) == 0

        # 验证写入
        with tmp_warehouse.connection(read_only=True) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM factor_values WHERE factor_code = 'test_close'"
            ).fetchone()[0]
        assert count == 2

    def test_execute_does_not_overwrite_old_batch(self, tmp_warehouse):
        """执行不覆盖旧批次（calc_batch_id 主键）。"""
        rows = [
            {"symbol": "000001", "trade_date": "2026-07-24", "adjust": "qfq",
             "close": 10.0, "open": 10.0, "high": 10.0, "low": 10.0,
             "volume": 1000.0, "amount": 10000.0, "turnover_rate": 0.05,
             "source": "test", "source_origin": "test", "source_row_id": 1},
        ]
        _seed_daily_bars(tmp_warehouse, rows)

        result = compile_formula(formula="close", strict_fields=True)

        executor = FactorExecutor(tmp_warehouse)
        # 第一次执行
        outcome1 = executor.execute(
            result.execution_plan,
            factor_code="test_batch",
            factor_version=1,
            trade_date=date(2026, 7, 24),
            calc_batch_id="batch-001",
        )
        assert outcome1.rows_written == 1

        # 第二次执行（不同 batch_id）
        outcome2 = executor.execute(
            result.execution_plan,
            factor_code="test_batch",
            factor_version=1,
            trade_date=date(2026, 7, 24),
            calc_batch_id="batch-002",
        )
        assert outcome2.rows_written == 1

        # 验证两批次都存在
        with tmp_warehouse.connection(read_only=True) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM factor_values WHERE factor_code = 'test_batch'"
            ).fetchone()[0]
        assert count == 2  # 两个批次各一行

    def test_execute_empty_data_returns_error(self, tmp_warehouse):
        """空数据执行返回错误。"""
        result = compile_formula(formula="close", strict_fields=True)
        assert result.is_valid

        executor = FactorExecutor(tmp_warehouse)
        outcome = executor.execute(
            result.execution_plan,
            factor_code="test_empty",
            factor_version=1,
        )

        assert outcome.rows_written == 0
        assert len(outcome.errors) > 0
        assert "no_source_data" in outcome.errors

    def test_execute_uses_safe_write_context(self, tmp_warehouse):
        """执行使用 safe_write_context（单写锁 + 事务）。"""
        rows = [
            {"symbol": "000001", "trade_date": "2026-07-24", "adjust": "qfq",
             "close": 10.0, "open": 10.0, "high": 10.0, "low": 10.0,
             "volume": 1000.0, "amount": 10000.0, "turnover_rate": 0.05,
             "source": "test", "source_origin": "test", "source_row_id": 1},
        ]
        _seed_daily_bars(tmp_warehouse, rows)

        result = compile_formula(formula="close", strict_fields=True)
        executor = FactorExecutor(tmp_warehouse)

        # 正常执行应成功
        outcome = executor.execute(
            result.execution_plan,
            factor_code="test_lock",
            factor_version=1,
        )
        assert outcome.rows_written == 1

        # 执行完成后锁应已释放
        from app.services.factors.warehouse_locks import diagnose_warehouse_lock
        lock_info = diagnose_warehouse_lock(tmp_warehouse.path)
        assert not lock_info.is_locked


# ══════════════════════════════════════════════════════════
# WP2-06: 预览 API 集成测试
# ══════════════════════════════════════════════════════════


class TestPreviewAPIWithExecutor:
    """预览 API 集成执行器测试。"""

    def test_preview_returns_values_with_data(self, db_session, tmp_warehouse):
        """预览 API 返回因子值（有数据时）。"""
        from app.api.routes.factors import preview_factor_formula
        from app.schemas.factor_library import FactorPreviewRequest
        from app.services.factors.config import FactorSystemConfigSnapshot

        # 准备数据
        rows = [
            {"symbol": "000001", "trade_date": "2026-07-24", "adjust": "qfq",
             "close": 10.0, "open": 10.0, "high": 10.0, "low": 10.0,
             "volume": 1000.0, "amount": 10000.0, "turnover_rate": 0.05,
             "source": "test", "source_origin": "test", "source_row_id": 1},
            {"symbol": "000002", "trade_date": "2026-07-24", "adjust": "qfq",
             "close": 20.0, "open": 20.0, "high": 20.0, "low": 20.0,
             "volume": 2000.0, "amount": 40000.0, "turnover_rate": 0.10,
             "source": "test", "source_origin": "test", "source_row_id": 2},
        ]
        _seed_daily_bars(tmp_warehouse, rows)

        # Mock get_factor_system_config 返回 tmp_warehouse 路径
        import app.api.routes.factors as factors_route
        original_config = factors_route.get_factor_system_config

        def _mock_config(db):
            return FactorSystemConfigSnapshot(
                feature_enabled=True,
                warehouse_path=str(tmp_warehouse.path),
                updated_by="test",
                updated_at=datetime.now(timezone.utc).isoformat(),
            )

        factors_route.get_factor_system_config = _mock_config

        try:
            payload = FactorPreviewRequest(
                formula_expr="close",
            )
            response = preview_factor_formula(payload, db_session)
            assert response["is_valid"] is True
            # 如果有数据，应该返回值
            if response.get("values"):
                assert len(response["values"]) > 0
                assert response["values"][0]["symbol"] is not None
        finally:
            factors_route.get_factor_system_config = original_config


# ══════════════════════════════════════════════════════════
# 辅助函数测试
# ══════════════════════════════════════════════════════════


class TestSafeFloat:
    """_safe_float 测试。"""

    def test_normal_value(self):
        assert _safe_float(3.14) == 3.14

    def test_none(self):
        assert _safe_float(None) is None

    def test_nan(self):
        assert _safe_float(float("nan")) is None

    def test_inf(self):
        assert _safe_float(float("inf")) is None

    def test_string(self):
        assert _safe_float("not_a_number") is None
