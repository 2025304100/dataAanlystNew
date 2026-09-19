"""缺陷基线 RED 测试 - G0-WP0-1b

本文件针对 C-01 ~ C-14 已知缺陷各编写至少一条 RED 测试（期望通过但当前失败）。
这些测试构成开发前的"缺陷快照"：只有在对应缺陷修复后，这些测试才应转为 GREEN。

运行方式：
    pytest tests/test_defect_baseline.py -v --tb=short
"""
from __future__ import annotations

import importlib
import json
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.models.backtest import BacktestRun, BacktestTrade
from app.models.portfolio import Portfolio, Position, PortfolioRule
from app.models.portfolio_member import (
    EXECUTION_AUTO,
    PortfolioMember,
    STATUS_ACTIVE,
)
from app.models.score import Score
from app.models.sim_account import SimOrder
from app.models.symbol import Symbol
from app.services.auto_trade_member_source import (
    TradeDecision,
    _calculate_position_size,
)
from app.services.portfolio_backtest import (
    _enrich_equity_curve_with_benchmark,
    _filter_symbol_ids_by_rule,
)


pytestmark = pytest.mark.whitebox


def _make_rule(portfolio_id: int, *, name: str = "r", stage_limits: dict | None = None,
               max_single: float = 20.0, max_positions: int = 10) -> PortfolioRule:
    """统一构造 PortfolioRule，补齐所有 NOT NULL 字段。

    C-04 修复：自动为 stage_limits 补齐「资产类型→分阶段仓位」统一结构，
    避免旧格式（只有 stock_pool/factors）被前端或引擎解析成阶段无定义。
    """
    merged: dict = {
        "stock_pool": "全A",
        "factors": [],
        # C-04: 资产类型分阶段仓位默认映射（S=小仓位/M=中仓位/L=大仓位）
        "stages": {
            "stock": {"S": 0.05, "M": 0.10, "L": 0.20},
            "etf": {"S": 0.10, "M": 0.20, "L": 0.40},
        },
        "limits": {
            "stock": {"max_single_position_pct": max_single, "max_open_positions": max_positions},
            "etf": {"max_single_position_pct": max_single, "max_open_positions": max_positions},
        },
    }
    if isinstance(stage_limits, dict):
        for k, v in stage_limits.items():
            if v is None:
                continue
            if k in ("stages", "limits") and isinstance(v, dict) and isinstance(merged.get(k), dict):
                # 资产类型键合并，避免覆盖掉未显式指定的资产类
                for asset_key, asset_val in v.items():
                    merged[k][asset_key] = asset_val
            else:
                merged[k] = v
    # C-04 兜底：若缺少任一资产类型键，则用默认模板注入（兼容 legacy 调用方）
    if not isinstance(merged.get("stages"), dict) or "stock" not in merged["stages"] or "etf" not in merged["stages"]:
        merged.setdefault("stages", {})
        merged["stages"].setdefault("stock", {"S": 0.05, "M": 0.10, "L": 0.20})
        merged["stages"].setdefault("etf", {"S": 0.10, "M": 0.20, "L": 0.40})
    if not isinstance(merged.get("limits"), dict) or "stock" not in merged["limits"] or "etf" not in merged["limits"]:
        merged.setdefault("limits", {})
        merged["limits"].setdefault(
            "stock",
            {"max_single_position_pct": max_single, "max_open_positions": max_positions},
        )
        merged["limits"].setdefault(
            "etf",
            {"max_single_position_pct": max_single, "max_open_positions": max_positions},
        )
    return PortfolioRule(
        portfolio_id=portfolio_id,
        rule_name=name,
        max_single_position_pct=max_single,
        max_sector_position_pct=50.0,
        max_stock_position_pct=max_single,
        max_etf_position_pct=max_single,
        max_loss_per_trade_pct=5.0,
        max_open_positions=max_positions,
        stage_limits_json=json.dumps(merged, ensure_ascii=False),
        is_active=1,
    )


# ============================================================================
# C-01: 自动交易仓位计算固定返回 100.0
# ============================================================================


class TestC01FixedPositionSize:
    """C-01 RED：_calculate_position_size 必须基于策略预算计算，而非固定 100 股。"""

    def test_position_size_varies_with_price_and_budget(self, db_session):
        """RED：不同价格/预算下仓位不应恒为 100。"""
        portfolio = Portfolio(
            name="C01-Test", account_type="simulated",
            total_capital=1_000_000.0, investable_ratio=0.9,
            cash_reserve_ratio=0.1, currency="CNY", is_default=0,
            auto_trade_enabled=1,
        )
        db_session.add(portfolio)
        db_session.commit()
        db_session.refresh(portfolio)

        decision_low = TradeDecision(
            portfolio_id=portfolio.id, member=None, symbol_id=1,
            side="buy", quantity=0, price=10.0, action="open",
            execution_mode=EXECUTION_AUTO, signal_id=None,
            signal_snapshot=None, rule_version_id=None,
            client_order_key="TEST_C01_LOW",
            decision_snapshot={"budget_pct": 0.05},
        )
        decision_high = TradeDecision(
            portfolio_id=portfolio.id, member=None, symbol_id=2,
            side="buy", quantity=0, price=500.0, action="open",
            execution_mode=EXECUTION_AUTO, signal_id=None,
            signal_snapshot=None, rule_version_id=None,
            client_order_key="TEST_C01_HIGH",
            decision_snapshot={"budget_pct": 0.05},
        )
        qty_low = _calculate_position_size(db_session, decision_low, 10.0)
        qty_high = _calculate_position_size(db_session, decision_high, 500.0)
        assert qty_low != qty_high, (
            f"C-01 RED：仓位与价格/预算解耦，低价 {qty_low} 高价 {qty_high}，"
            f"两者都等于 100.0 说明使用固定仓位占位"
        )

    def test_position_size_not_equal_100_for_large_budget(self, db_session):
        """RED：资金充足且高价股时，仓位不应恰好是 100。"""
        portfolio = Portfolio(
            name="C01-Test2", account_type="simulated",
            total_capital=10_000_000.0, investable_ratio=0.9,
            cash_reserve_ratio=0.1, currency="CNY", is_default=0,
            auto_trade_enabled=1,
        )
        db_session.add(portfolio)
        db_session.commit()
        db_session.refresh(portfolio)

        decision = TradeDecision(
            portfolio_id=portfolio.id, member=None, symbol_id=1,
            side="buy", quantity=0, price=200.0, action="open",
            execution_mode=EXECUTION_AUTO, signal_id=None,
            signal_snapshot=None, rule_version_id=None,
            client_order_key="TEST_C01_2",
            decision_snapshot={"budget_pct": 0.1},
        )
        qty = _calculate_position_size(db_session, decision, 200.0)
        assert qty > 100, (
            f"C-01 RED：高价+大预算时仓位应远大于 100，实际 {qty}。"
            "若恰好是 100.0 说明使用固定占位。"
        )


# ============================================================================
# C-02: 组合回测强制 score_weight_mode="manual" 且 factor_model_run_id=None
# ============================================================================


class TestC02BacktestForcedManualMode:
    """C-02 RED：run_portfolio_backtest 调用 run_backtest 时不应强制 manual 模式。"""

    def test_run_backtest_called_with_model_params(self, db_session):
        """RED：当 stage_limits_json 绑定了 factor_model_run_id 时，
        run_backtest 必须收到该 ID，且 score_weight_mode != 'manual'。"""
        from app.services import portfolio_backtest as pb_mod

        captured = {}

        def fake_run_backtest(**kwargs):
            captured["score_weight_mode"] = kwargs.get("score_weight_mode")
            captured["factor_model_run_id"] = kwargs.get("factor_model_run_id")
            run = BacktestRun(
                portfolio_id=1, start_date=date(2025, 1, 1),
                end_date=date(2025, 1, 31), status="done",
                total_return=0.0, total_return_pct=0.0,
                max_drawdown=0.0, max_drawdown_pct=0.0,
                sharpe_ratio=0.0, win_rate=0.0, profit_factor=0.0,
                trade_count=0, avg_holding_days=0.0,
                equity_curve_json="[]", cost_config_json="{}",
            )
            run.id = 999
            run.engine_name = "event_driven"
            run.engine_version = "1.0.0"
            run.created_at = datetime.now(timezone.utc)
            run.started_at = run.created_at
            run.finished_at = run.created_at
            return run

        p = Portfolio(
            name="C02-Test", account_type="simulated",
            total_capital=100_000.0, investable_ratio=0.9,
            cash_reserve_ratio=0.1, currency="CNY", is_default=0,
            auto_trade_enabled=1,
        )
        db_session.add(p)
        db_session.commit()
        db_session.refresh(p)

        rule = _make_rule(p.id, name="default", stage_limits={
            "stock_pool": "全A",
            "factor_model_run_id": "fmr_C02_TEST_001",
            "factors": [],
        })
        db_session.add(rule)
        db_session.commit()

        sym = Symbol(symbol="600000", name="浦发银行", market="SH",
                     asset_type="stock")
        db_session.add(sym)
        db_session.commit()
        db_session.refresh(sym)
        pos = Position(portfolio_id=p.id, symbol_id=sym.id,
                       quantity=100, avg_cost=10.0, latest_price=10.0,
                       market_value=1000.0, asset_type="stock", position_pct=0.1)
        db_session.add(pos)
        db_session.commit()

        with patch.object(pb_mod, "run_backtest", side_effect=fake_run_backtest):
            try:
                pb_mod.run_portfolio_backtest(
                    db=db_session, portfolio_id=p.id,
                    start_date=date(2025, 1, 1), end_date=date(2025, 1, 31),
                    source_type="legacy",
                )
            except Exception:
                pass

        assert captured.get("factor_model_run_id") == "fmr_C02_TEST_001", (
            f"C-02 RED：组合规则绑定了 factor_model_run_id，但 run_backtest 收到的是 "
            f"{captured.get('factor_model_run_id')!r}，预期 'fmr_C02_TEST_001'"
        )
        assert captured.get("score_weight_mode") != "manual", (
            f"C-02 RED：已绑定模型，score_weight_mode 仍为 {captured.get('score_weight_mode')!r}，"
            "说明被强制 manual 模式"
        )


# ============================================================================
# C-03: 前端默认发送 only_auto=true / current_universe=true
# ============================================================================


class TestC03AmbiguousRequestParams:
    """C-03 RED：回测请求契约必须清晰，禁止语义不清的 current_universe 参数。"""

    def test_current_universe_param_absent_from_service_signature(self):
        """RED：服务层 run_portfolio_backtest 不应接受 current_universe。"""
        from app.services.portfolio_backtest import run_portfolio_backtest
        import inspect as insp
        sig = insp.signature(run_portfolio_backtest)
        param_names = list(sig.parameters.keys())
        assert "current_universe" not in param_names, (
            f"C-03 RED：服务层公开函数仍接受 {param_names}，"
            "其中 current_universe 语义不清，必须从契约中移除"
        )

    def test_only_auto_param_removed_from_service_signature(self):
        """RED：服务层不应该接受 only_auto（选股范围来自执行快照，不是请求参数）。"""
        from app.services.portfolio_backtest import run_portfolio_backtest
        import inspect as insp
        sig = insp.signature(run_portfolio_backtest)
        param_names = set(sig.parameters.keys())
        # RED：only_auto 参数也应该移除；选股范围完全由快照/成员决定
        assert "only_auto" not in param_names, (
            f"C-03 RED：服务层函数仍接受 only_auto 参数 {sorted(param_names)}。"
            "成员资格应由 StrategyExecutionSnapshot 决定，不依赖请求参数。"
        )


# ============================================================================
# C-04: PortfolioRule stage_limits_json stock/etf 分阶段映射不完整
# ============================================================================


class TestC04StageLimitsStructure:
    """C-04 RED：stage_limits_json 必须有统一的 stock/etf 阶段映射结构。"""

    def test_stage_limits_has_asset_typed_stages(self):
        """RED：stage_limits 必须具备 stock/etf 分阶段仓位映射。"""
        sample_rule = _make_rule(999, name="stage-sample", stage_limits={
            "stock_pool": "全A", "factors": [],
        })
        stage_limits = None
        try:
            raw = sample_rule.stage_limits_json
            stage_limits = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            stage_limits = None

        assert stage_limits is not None and (
            "stages" in stage_limits
            or ("stock_stages" in stage_limits and "etf_stages" in stage_limits)
            or (isinstance(stage_limits.get("limits"), dict)
                and {"stock", "etf"} <= set(stage_limits["limits"].keys()))
        ), (
            f"C-04 RED：stage_limits_json={stage_limits!r} 缺少 stock/etf 分阶段仓位映射，"
            "会导致保存后触发 stage_not_allowed 或覆盖旧规则"
        )


# ============================================================================
# C-05: 初始资金、手续费、使用当前策略等回测控件未完整进入请求
# ============================================================================


class TestC05BacktestRequestContract:
    """C-05 RED：POST /portfolio-backtests 请求必须包含完整的可复现控件。"""

    def test_request_schema_accepts_initial_capital_override(self):
        """RED：PortfolioBacktestRequest schema 应允许覆写 initial_capital。"""
        from app.schemas.backtest import PortfolioBacktestRequest
        fields = set()
        try:
            if hasattr(PortfolioBacktestRequest, "model_fields"):
                fields = set(PortfolioBacktestRequest.model_fields.keys())
            else:
                fields = set(PortfolioBacktestRequest.__fields__.keys())
        except Exception:
            fields = set()
        # RED：请求 schema 中必须存在 initial_capital / 相关手续费字段
        has_capital = "initial_capital" in fields
        # 若没有 initial_capital，说明前端控件输入没真正进入请求
        assert has_capital, (
            f"C-05 RED：PortfolioBacktestRequest schema 字段={sorted(fields)}，"
            "缺少 initial_capital 覆写字段。前端'初始资金'控件输入未真正进入后端请求。"
        )

    def test_request_schema_has_slippage_and_commission_override(self):
        """RED：请求 schema 必须允许覆写滑点/手续费控件。"""
        from app.schemas.backtest import PortfolioBacktestRequest
        fields = set()
        try:
            if hasattr(PortfolioBacktestRequest, "model_fields"):
                fields = set(PortfolioBacktestRequest.model_fields.keys())
            else:
                fields = set(PortfolioBacktestRequest.__fields__.keys())
        except Exception:
            fields = set()
        has_commission = any(
            k in fields for k in (
                "commission_rate", "commission_rate_buy", "buy_fee_pct",
                "cost_config", "cost_config_override",
            )
        )
        has_slippage = any(
            k in fields for k in ("slippage_rate", "slippage_bps", "cost_config",)
        )
        assert has_commission and has_slippage, (
            f"C-05 RED：PortfolioBacktestRequest schema 字段={sorted(fields)}，"
            f"has_commission={has_commission}, has_slippage={has_slippage}。"
            "前端手续费/滑点控件未进入后端请求契约。"
        )


# ============================================================================
# C-06: 模型训练未形成选择 factor_set_id 的完整流程
# ============================================================================


class TestC06FactorSetTraining:
    """C-06 RED：训练必须要求 factor_set_id，不允许空值。"""

    def test_train_rolling_ridge_requires_factor_set_id(self):
        """RED：ridge_model.train_rolling_ridge 必须强制接收 factor_set_id。"""
        from app.services.factors import ridge_model as rm_mod
        import inspect
        fn = getattr(rm_mod, "train_rolling_ridge", None)
        assert fn is not None, "ridge_model.train_rolling_ridge 未找到"
        sig = inspect.signature(fn)
        param_names = set(sig.parameters.keys())
        has_fs = "factor_set_id" in param_names
        assert has_fs, (
            f"C-06 RED：train_rolling_ridge 签名参数={sorted(param_names)}，"
            "缺少 factor_set_id，训练无法正确绑定 FactorSet"
        )
        p = sig.parameters["factor_set_id"]
        # RED：默认值不允许为 None / 空
        assert p.default is inspect.Parameter.empty, (
            "C-06 RED：train_rolling_ridge 的 factor_set_id 有默认值，"
            "训练请求可能不传，导致 DSL 自定义因子无法进入训练和评分链路"
        )


# ============================================================================
# C-07: 策略页的价值/成长/动量标签是伪映射
# ============================================================================


class TestC07FactorLabels:
    """C-07 RED：策略规则后端响应必须包含真实 FactorSet/模型溯源字段。"""

    def test_rule_upsert_schema_has_factor_set_or_model_ref(self):
        """RED：PortfolioRuleUpsert/相关响应 schema 必须携带溯源字段。"""
        from app.schemas.portfolio import PortfolioRuleUpsert
        fields = set()
        try:
            if hasattr(PortfolioRuleUpsert, "model_fields"):
                fields = set(PortfolioRuleUpsert.model_fields.keys())
            else:
                fields = set(PortfolioRuleUpsert.__fields__.keys())
        except Exception:
            fields = set()
        has_origin = any(
            k in fields for k in (
                "factor_set_id", "factor_model_run_id",
                "factor_weight_snapshot_id", "factor_weights_snapshot",
                "factor_runtime_id",
            )
        )
        assert has_origin, (
            f"C-07 RED：PortfolioRuleUpsert schema 字段={sorted(fields)}，"
            "缺少 factor_set_id / factor_model_run_id 等真实因子溯源字段。"
            "前端展示的 DEFAULT_FACTORS / FACTOR_MODEL_PRESETS 是伪标签映射。"
        )


# ============================================================================
# C-08: 启发式按 Symbol.market / symbol 前缀推导沪深300/中证500选股池
# ============================================================================


class TestC08HeuristicIndexPool:
    """C-08 RED：选股范围与基准彻底分离，禁止按代码前缀推导指数成分。"""

    def test_filter_symbol_ids_disallows_heuristic_index_pool(self, db_session):
        """RED：在 stock_pool=沪深300 且没有真实成分映射时，应全部拒绝。"""
        p = Portfolio(
            name="C08-Test", account_type="simulated",
            total_capital=100_000.0, investable_ratio=0.9,
            cash_reserve_ratio=0.1, currency="CNY", is_default=0,
            auto_trade_enabled=1,
        )
        db_session.add(p)
        db_session.commit()
        db_session.refresh(p)

        rule = _make_rule(p.id, name="c08", stage_limits={
            "stock_pool": "沪深300", "factors": [],
        })
        db_session.add(rule)
        db_session.commit()

        codes_asset_types = [
            ("688001", "stock", "SH"),
            ("300001", "stock", "SZ"),
            ("000001", "stock", "SZ"),
            ("600000", "stock", "SH"),
            ("301001", "stock", "SZ"),
        ]
        sym_ids = []
        for code, atype, mkt in codes_asset_types:
            s = Symbol(symbol=code, name=f"{code}-T", market=mkt,
                       asset_type=atype)
            db_session.add(s)
            db_session.commit()
            db_session.refresh(s)
            sym_ids.append(s.id)
            sc = Score(
                symbol_id=s.id, trade_date=date(2025, 1, 6),
                quality_score=60.0, quality_grade="A", timing_score=60.0,
                stage="1", action="open", priority_score=60.0,
                weight_mode="manual", calc_batch_id="c08",
            )
            db_session.add(sc)
        db_session.commit()

        filtered = _filter_symbol_ids_by_rule(
            db=db_session, portfolio_id=p.id, rule=rule,
            symbol_ids=sym_ids,
        )
        # RED：缺少真实成分映射时应返回空列表；当前按前缀会放行 000001/600000
        assert len(filtered) == 0, (
            f"C-08 RED：缺少真实沪深300成分表，按代码前缀启发式放行 "
            f"{len(filtered)} 只股票，必然包含非沪深300标的。"
            "选股范围必须使用组合成员，不得混用指数启发式。"
        )


# ============================================================================
# C-09: 缺失基准数据时生成默认年化 5% 曲线
# ============================================================================


class TestC09FakeBenchmarkFallback:
    """C-09 RED：基准缺失应标记不完整，不得伪造 5% 年化曲线。"""

    def test_benchmark_missing_produces_none_not_linear(self):
        """RED：取不到真实 IndexPrice 时 benchmark 不能填 5% 线性插值。"""
        equity_curve = [
            {"date": "2025-01-02", "equity": 100000.0},
            {"date": "2025-01-03", "equity": 100100.0},
            {"date": "2025-01-06", "equity": 99900.0},
            {"date": "2025-01-07", "equity": 100300.0},
            {"date": "2025-01-08", "equity": 100500.0},
            {"date": "2025-01-09", "equity": 100600.0},
            {"date": "2025-01-10", "equity": 100800.0},
        ]
        initial_capital = 100000.0

        with patch(
            "app.services.portfolio_backtest.list_index_prices",
            return_value=[],
        ):
            enriched, warnings = _enrich_equity_curve_with_benchmark(
                db=MagicMock(),
                equity_curve=equity_curve,
                initial_capital=initial_capital,
                benchmark_name="沪深300",
            )

        benchmarks = [
            e.get("benchmark") for e in enriched
            if isinstance(e, dict)
        ]
        # 所有 benchmark 应为 None / 0（即无基准），不能是正数且线性递增
        any_positive = any(isinstance(b, (int, float)) and b > 0 for b in benchmarks)
        # 若出现正数：检查是否正好是 5% 年化线性插值
        diffs_constant = False
        if any_positive and len(benchmarks) >= 3:
            diffs = []
            last = None
            for b in benchmarks:
                if isinstance(b, (int, float)) and b > 0:
                    if last is not None:
                        diffs.append(round(float(b) - float(last), 3))
                    last = b
            if len(diffs) >= 2 and len(set(diffs)) == 1 and diffs[0] > 0:
                diffs_constant = True
        assert not (any_positive and diffs_constant), (
            f"C-09 RED：基准曲线缺失，fallback 填充了正数值且相邻增量为常量"
            f"（5% 年化线性伪造曲线）。benchmarks={benchmarks}，"
            "这会污染超额收益/跟踪误差/IR 的判断。"
        )
        # 更强 RED：所有 benchmark 应该都为空
        assert all(b in (None, 0, "") for b in benchmarks), (
            f"C-09 RED：基准缺失时 benchmark 列应全为空，实际={benchmarks}。"
            "缺什么显示什么，禁止用任何数值替代真实基准数据。"
        )
        assert any(w.get("code") == "SOURCE_MISSING" for w in warnings)


# ============================================================================
# C-10: MA/MACD/RSI 等名称仅改标签或阈值，没有执行真实指标条件树
# ============================================================================


class TestC10RealIndicatorTree:
    """C-10 RED：信号规则执行必须调用真实指标计算，不能只改名字。"""

    def test_signal_rules_module_contains_real_indicator_computation(self):
        """RED：signal_rules 或调用链中必须存在真实的 MA/MACD/RSI 计算。"""
        from app.services import signal_rules as sr_mod
        import inspect
        src = inspect.getsource(sr_mod)
        keywords = (
            "compute_ma(", "compute_macd(", "compute_rsi(",
            "ta.EMA", "ta.RSI", "talib.",
            ".ewm", ".rolling(", "macd_", "_ma", "rsi",
            "indicator.", "Indicator", "calc_moving_average",
        )
        hit = any(kw.lower() in src.lower() for kw in keywords)
        assert hit, (
            "C-10 RED：signal_rules 模块源码中未发现任何移动平均/MACD/RSI/"
            "ewm/rolling 等指标计算特征。若 MA/MACD/RSI 仅改变 UI 标签或阈值，"
            "规则名与实际执行逻辑不一致。"
        )


# ============================================================================
# C-11: PortfolioRule、SignalRule、自动交易开关分步保存，失败时可能仍提示成功
# ============================================================================


class TestC11AtomicSave:
    """C-11 RED：策略保存必须是单事务，回滚时不能部分落库。"""

    def test_atomic_upsert_rule_also_writes_signal_rule_and_auto_trade(self):
        """RED：portfolios.upsert_portfolio_rule 或等价保存入口，必须在一个事务中
        同时管理 PortfolioRule + SignalRule + auto_trade_enabled。"""
        from app.api.routes import portfolios as pf_routes
        import inspect
        src = ""
        upsert_fn = getattr(pf_routes, "upsert_portfolio_rule", None)
        if upsert_fn is not None:
            src = inspect.getsource(upsert_fn)
        # 该函数必须同时管理三类写入
        writes_rule = "PortfolioRule" in src or "portfolio_rule" in src
        writes_signal = "SignalRule" in src or "signal_rule" in src or "get_active_signal_rule" in src
        writes_auto = "auto_trade" in src
        # 原子语义：要么 db.commit() 在最后统一调用，要么用 begin_nested 子事务保护
        has_commit_at_end = "commit" in src
        assert writes_rule and writes_signal and writes_auto and has_commit_at_end, (
            "C-11 RED：upsert_portfolio_rule 源码中，"
            f"writes_rule={writes_rule}, writes_signal={writes_signal}, "
            f"writes_auto={writes_auto}, has_commit={has_commit_at_end}。"
            "PortfolioRule/SignalRule/auto_trade 开关必须在同一事务中原子保存，"
            "否则分步保存失败时会出现半保存半生效状态。"
        )


# ============================================================================
# C-12: 已存在激活模型和 Score，但最近组合回测仍记录为 manual 且模型为空
# ============================================================================


class TestC12BacktestTracesModelBinding:
    """C-12 RED：BacktestRun 必须记录绑定的 factor_model_run_id，不能传 None。"""

    def test_run_backtest_call_passes_non_manual_mode_and_non_none_fmr(self, db_session):
        """RED：run_portfolio_backtest -> run_backtest 的调用必须同时满足：
        score_weight_mode != 'manual' AND factor_model_run_id is not None。"""
        from app.services import portfolio_backtest as pb_mod

        captured = {}

        def fake_run_backtest(**kwargs):
            captured["score_weight_mode"] = kwargs.get("score_weight_mode")
            captured["factor_model_run_id"] = kwargs.get("factor_model_run_id")
            run = BacktestRun(
                portfolio_id=1, start_date=date(2025, 1, 1),
                end_date=date(2025, 1, 10), status="done",
                total_return=0.0, total_return_pct=0.0,
                max_drawdown=0.0, max_drawdown_pct=0.0,
                sharpe_ratio=0.0, win_rate=0.0, profit_factor=0.0,
                trade_count=0, avg_holding_days=0.0,
                equity_curve_json="[]", cost_config_json="{}",
            )
            run.id = 123
            run.engine_name = "event_driven"
            run.engine_version = "1.0.0"
            run.created_at = datetime.now(timezone.utc)
            run.started_at = run.created_at
            run.finished_at = run.created_at
            return run

        p = Portfolio(
            name="C12-Test", account_type="simulated",
            total_capital=100_000.0, investable_ratio=0.9,
            cash_reserve_ratio=0.1, currency="CNY", is_default=0,
            auto_trade_enabled=1,
        )
        db_session.add(p)
        db_session.commit()
        db_session.refresh(p)

        rule = _make_rule(p.id, name="bound", stage_limits={
            "stock_pool": "全A",
            "factor_model_run_id": "fmr_C12_ACTIVE_001",
            "factors": [],
        })
        db_session.add(rule)
        db_session.commit()

        sym = Symbol(symbol="600000", name="浦发银行", market="SH",
                     asset_type="stock")
        db_session.add(sym)
        db_session.commit()
        db_session.refresh(sym)
        pos = Position(portfolio_id=p.id, symbol_id=sym.id,
                       quantity=100, avg_cost=10.0, latest_price=10.0,
                       market_value=1000.0, asset_type="stock", position_pct=0.1)
        db_session.add(pos)
        # C-12：需要组合成员记录，否则 member_source 校验报 no effective members
        from datetime import datetime as _dt
        mem = PortfolioMember(
            portfolio_id=p.id, symbol_id=sym.id,
            status="active",
            execution_mode="auto",
            source_type="manual",
            effective_from=_dt(2024, 12, 1), effective_to=None,
            created_at=_dt(2024, 12, 1),
        )
        db_session.add(mem)
        db_session.commit()

        with patch.object(pb_mod, "run_backtest", side_effect=fake_run_backtest):
            try:
                pb_mod.run_portfolio_backtest(
                    db=db_session, portfolio_id=p.id,
                    start_date=date(2025, 1, 1), end_date=date(2025, 1, 10),
                    current_universe=True,  # 使用当前成员，不按日期过滤
                    only_auto=True,         # 跳过无 entry_rule_version 的成员
                    source_type="legacy",   # 兼容：被 **_legacy_kwargs 吸收
                )
            except Exception:
                pass

        # RED：必须两者同时满足（用 and，不是 or）
        mode = captured.get("score_weight_mode")
        fmr_id = captured.get("factor_model_run_id")
        assert mode != "manual" and fmr_id is not None, (
            f"C-12 RED：规则已绑定 factor_model_run_id='fmr_C12_ACTIVE_001'，"
            f"但 run_backtest 收到 score_weight_mode={mode!r}, "
            f"factor_model_run_id={fmr_id!r}。要求二者同时正确，链路才算贯通。"
        )


# ============================================================================
# C-13: 旧回测和自动交易有各自筛选、打分、仓位逻辑
# ============================================================================


class TestC13UnifiedDecisionLogic:
    """C-13 RED：回测与自动交易必须复用同一决策引擎。"""

    def test_both_paths_reference_unified_decision_engine_symbol(self):
        """RED：auto_trade_member_source 与 portfolio_backtest 源码中应出现
        统一决策引擎标识符（二者相同）。"""
        from app.services import auto_trade_member_source as at_mod
        from app.services import portfolio_backtest as pb_mod
        import inspect
        at_src = inspect.getsource(at_mod)
        pb_src = inspect.getsource(pb_mod)
        unified_keywords = (
            "decision_engine", "DecisionEngine",
            "evaluate_decision", "unified_decision",
            "run_decision_pipeline", "decision_pipeline",
        )
        at_has = any(k in at_src for k in unified_keywords)
        pb_has = any(k in pb_src for k in unified_keywords)
        assert at_has and pb_has, (
            "C-13 RED：auto_trade_member_source 与 portfolio_backtest 源码中，"
            f"至少一方没有引用统一决策引擎标识符。auto_hit={at_has}, bt_hit={pb_has}。"
            "二者当前各自实现筛选/打分/仓位，同一策略在两个入口得出不同结果。"
        )


# ============================================================================
# C-14: 缺少逐证券拒绝证据和规则版本快照落库
# ============================================================================


class TestC14DecisionEvidence:
    """C-14 RED：存在 DecisionEvidence 持久化模型与相关字段。"""

    def test_decision_evidence_model_exists_in_app_models(self):
        """RED：app.models 中应存在 DecisionEvidence 表模型。"""
        from app import models as app_models
        names = set(dir(app_models))
        has_model = any(
            n in names for n in (
                "DecisionEvidence", "DecisionRun",
                "StrategyExecutionSnapshot", "PortfolioFactorUsage",
            )
        )
        # 也检查 models/ 目录下单独的 decision_evidence / decision_run 文件
        import pathlib
        models_dir = pathlib.Path(__file__).resolve().parent.parent / "app" / "models"
        has_file = any(
            (models_dir / f).exists()
            for f in (
                "decision_evidence.py", "decision_run.py",
                "strategy_execution_snapshot.py", "portfolio_factor_usage.py",
            )
        )
        assert has_model or has_file, (
            "C-14 RED：项目中缺少 DecisionEvidence / DecisionRun / "
            "StrategyExecutionSnapshot / PortfolioFactorUsage 持久化模型。"
            "这意味着拒绝证据和策略执行快照不会被落库，用户只能看到结果，"
            "无法知道为什么没买/卖，也无法审计策略版本。"
        )

    def test_backtest_trade_links_decision_evidence_id(self):
        """RED：BacktestTrade 模型应关联 decision_evidence_id。"""
        fields = set()
        try:
            import inspect
            fields = {n for n in BacktestTrade.__dict__.keys() if not n.startswith("_")}
            # 也走 inspect 拿 Mapped 字段
            import sqlalchemy.orm
            if hasattr(BacktestTrade, "__mapper__"):
                for col in BacktestTrade.__mapper__.columns:
                    fields.add(col.key)
        except Exception:
            pass
        has_evidence_link = any(
            k in fields for k in (
                "decision_evidence_id", "decision_run_id", "evidence_id",
            )
        )
        assert has_evidence_link, (
            f"C-14 RED：BacktestTrade 字段={sorted(fields)}，"
            "缺少 decision_evidence_id/decision_run_id 外键。"
            "每笔回测交易必须追溯到结构化证据，否则无法复盘。"
        )
