"""白盒测试 - 组合整体回测（P2-2）。

守护 run_portfolio_backtest 与 API 端点的关键行为，防止重构/优化时退化：
1. _derive_symbol_ids: 持仓 only / scan 候选 only / 两者去重合并 / 都为空抛 ValueError
2. _build_rule_config: 有 active rule / 无 active rule（保守默认）
3. run_portfolio_backtest 错误场景：组合不存在 / 非模拟 / 未开启 auto_trade / total_capital<=0 / 无标的
4. run_portfolio_backtest 成功场景：完整执行产生 BacktestRun + BacktestTrade
5. API 端点：200 成功 / 404 不存在 / 409 非模拟 / 409 未开启自动交易 / 400 无标的
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.db.session import get_db
from app.models.backtest import BacktestRun, BacktestTrade
from app.models.daily_bar import DailyBar
from app.models.portfolio import Portfolio, PortfolioRule, Position
from app.models.scan import ScanResult, ScanRun
from app.models.score import Score
from app.models.symbol import Symbol
from app.services.portfolio_backtest import (
    _build_rule_config,
    _derive_symbol_ids,
    run_portfolio_backtest,
)
from app.services.sim_accounts import ensure_sim_account_seed

pytestmark = pytest.mark.whitebox


# ============================================================================
# 测试辅助
# ============================================================================

def _make_portfolio(
    db_session,
    name="QA-PortBt",
    account_type="simulated",
    total_capital=100000.0,
    auto_trade_enabled=True,
) -> Portfolio:
    """造一个组合并初始化 CashLedger。"""
    p = Portfolio(
        name=name,
        account_type=account_type,
        total_capital=total_capital,
        investable_ratio=0.9,
        cash_reserve_ratio=0.1,
        currency="CNY",
        is_default=0,
        auto_trade_enabled=int(auto_trade_enabled),
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    if account_type == "simulated":
        ensure_sim_account_seed(db_session, p)
        db_session.commit()
    return p


def _make_symbol(db_session, symbol="600010", name="测试标的", market="SH", asset_type="stock") -> Symbol:
    sym = Symbol(symbol=symbol, name=name, asset_type=asset_type, market=market)
    db_session.add(sym)
    db_session.commit()
    db_session.refresh(sym)
    return sym


def _make_daily_bar(db_session, symbol_id: int, trade_date: date, close: float = 10.0):
    bar = DailyBar(
        symbol_id=symbol_id,
        trade_date=trade_date,
        open=close,
        high=close * 1.02,
        low=close * 0.98,
        close=close,
        volume=100000.0,
    )
    db_session.add(bar)
    db_session.commit()
    return bar


def _make_score(
    db_session,
    symbol_id: int,
    *,
    action: str = "open",
    stage: str = "start",
    trade_date: date = date(2026, 1, 5),
) -> Score:
    score = Score(
        symbol_id=symbol_id,
        trade_date=trade_date,
        quality_score=70.0,
        quality_grade="B",
        timing_score=65.0,
        stage=stage,
        action=action,
        priority_score=75.0,
        weight_mode="manual",
    )
    db_session.add(score)
    db_session.commit()
    db_session.refresh(score)
    return score


def _make_position(
    db_session,
    portfolio_id: int,
    symbol_id: int,
    *,
    quantity: float = 100,
    avg_cost: float = 10.0,
    latest_price: float = 10.0,
    asset_type: str = "stock",
) -> Position:
    pos = Position(
        portfolio_id=portfolio_id,
        symbol_id=symbol_id,
        quantity=quantity,
        avg_cost=avg_cost,
        latest_price=latest_price,
        market_value=quantity * latest_price,
        position_pct=round(quantity * latest_price / 100000.0, 4),
        asset_type=asset_type,
    )
    db_session.add(pos)
    db_session.commit()
    db_session.refresh(pos)
    return pos


def _make_scan_run_with_candidate(
    db_session,
    portfolio_id: int,
    symbol_id: int,
    *,
    rank_no: int = 1,
    priority_score: float = 80.0,
    stage: str = "start",
    action: str = "open",
    result_type: str = "executable",
) -> tuple[ScanRun, ScanResult]:
    run = ScanRun(
        portfolio_id=portfolio_id,
        run_name=f"QA-Scan-{datetime.now().strftime('%H%M%S%f')}",
        scope_snapshot="cn-stock",
        status="done",
        started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
    )
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)
    result = ScanResult(
        scan_run_id=run.id,
        symbol_id=symbol_id,
        result_type=result_type,
        rank_no=rank_no,
        quality_score=70.0,
        timing_score=65.0,
        priority_score=priority_score,
        stage=stage,
        action=action,
        recommended_position_pct=0.2,
    )
    db_session.add(result)
    db_session.commit()
    db_session.refresh(result)
    return run, result


def _make_active_rule(db_session, portfolio_id: int) -> PortfolioRule:
    rule = PortfolioRule(
        portfolio_id=portfolio_id,
        rule_name="QA-Rule",
        max_single_position_pct=0.2,
        max_sector_position_pct=0.4,
        max_stock_position_pct=0.6,
        max_etf_position_pct=0.3,
        max_loss_per_trade_pct=0.02,
        max_open_positions=10,
        stage_limits_json=json.dumps({
            "stock": {"start": 0.2, "pullback": 0.15, "breakout": 0.1},
            "etf": {"start": 0.2, "pullback": 0.15, "breakout": 0.1},
        }),
        is_active=1,
    )
    db_session.add(rule)
    db_session.commit()
    db_session.refresh(rule)
    return rule


# ============================================================================
# 1. _derive_symbol_ids
# ============================================================================

class TestDeriveSymbolIds:
    """守护标的推导逻辑：持仓 + scan 候选合并去重。"""

    def test_positions_only(self, db_session):
        """有持仓、无 scan 候选 → 返回持仓 symbol_ids。"""
        p = _make_portfolio(db_session, name="QA-Derive-Pos")
        sym_a = _make_symbol(db_session, symbol="600010", name="A")
        sym_b = _make_symbol(db_session, symbol="600011", name="B")
        _make_position(db_session, p.id, sym_a.id, quantity=100)
        _make_position(db_session, p.id, sym_b.id, quantity=200)

        result = _derive_symbol_ids(db_session, p.id)

        assert sorted(result) == sorted([sym_a.id, sym_b.id])

    def test_scan_candidates_only(self, db_session):
        """无持仓、有 scan executable 候选 → 返回候选 symbol_ids。"""
        p = _make_portfolio(db_session, name="QA-Derive-Scan")
        sym = _make_symbol(db_session, symbol="600012", name="C")
        _make_scan_run_with_candidate(db_session, p.id, sym.id)

        result = _derive_symbol_ids(db_session, p.id)

        assert result == [sym.id]

    def test_both_dedup(self, db_session):
        """持仓 + scan 候选有重叠 → 去重后返回。"""
        p = _make_portfolio(db_session, name="QA-Derive-Both")
        sym_a = _make_symbol(db_session, symbol="600013", name="D")
        sym_b = _make_symbol(db_session, symbol="600014", name="E")
        sym_c = _make_symbol(db_session, symbol="600015", name="F")
        # sym_a 同时在持仓和 scan 候选中
        _make_position(db_session, p.id, sym_a.id, quantity=100)
        _make_position(db_session, p.id, sym_b.id, quantity=200)
        _make_scan_run_with_candidate(db_session, p.id, sym_a.id)
        _make_scan_run_with_candidate(db_session, p.id, sym_c.id)

        result = _derive_symbol_ids(db_session, p.id)

        assert sorted(result) == sorted([sym_a.id, sym_b.id, sym_c.id])
        assert len(result) == 3  # 无重复

    def test_empty_raises(self, db_session):
        """无持仓 + 无 scan 候选 → ValueError。"""
        p = _make_portfolio(db_session, name="QA-Derive-Empty")
        with pytest.raises(ValueError, match="no positions and no scan candidates"):
            _derive_symbol_ids(db_session, p.id)

    def test_non_executable_candidates_ignored(self, db_session):
        """result_type != 'executable' 的 scan 候选被忽略。"""
        p = _make_portfolio(db_session, name="QA-Derive-NonExec")
        sym = _make_symbol(db_session, symbol="600016", name="G")
        _make_scan_run_with_candidate(db_session, p.id, sym.id, result_type="watchlist")

        with pytest.raises(ValueError, match="no positions and no scan candidates"):
            _derive_symbol_ids(db_session, p.id)

    def test_failed_scan_run_ignored(self, db_session):
        """status != 'done' 的 scan run 被忽略。"""
        p = _make_portfolio(db_session, name="QA-Derive-FailedScan")
        sym = _make_symbol(db_session, symbol="600017", name="H")
        run = ScanRun(
            portfolio_id=p.id,
            run_name="QA-FailedScan",
            scope_snapshot="cn-stock",
            status="failed",
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
        )
        db_session.add(run)
        db_session.commit()
        db_session.refresh(run)
        result = ScanResult(
            scan_run_id=run.id,
            symbol_id=sym.id,
            result_type="executable",
            rank_no=1,
            quality_score=70.0,
            timing_score=65.0,
            priority_score=80.0,
            stage="start",
            action="open",
            recommended_position_pct=0.2,
        )
        db_session.add(result)
        db_session.commit()

        with pytest.raises(ValueError, match="no positions and no scan candidates"):
            _derive_symbol_ids(db_session, p.id)


# ============================================================================
# 2. _build_rule_config
# ============================================================================

class TestBuildRuleConfig:
    """守护 rule_config 构造逻辑：信号集合 + 仓位配置映射。"""

    def test_with_active_rule(self, db_session):
        """有 active rule → 使用 rule 的 max_single_position_pct / max_open_positions。"""
        p = _make_portfolio(db_session, name="QA-Rule-Active")
        rule = _make_active_rule(db_session, p.id)

        config = _build_rule_config(rule)

        assert config["buy_conditions"]["actions"] == ["open", "buy_dip"]
        assert config["sell_conditions"]["score_actions"] == ["exit", "reduce"]
        assert config["position_config"]["type"] == "fixed_pct"
        assert config["position_config"]["value"] == 0.2  # max_single_position_pct
        assert config["position_config"]["max_positions"] == 10  # max_open_positions
        assert config["execution_config"]["entry_timing"] == "signal_close"
        assert config["execution_config"]["exit_timing"] == "signal_close"

    def test_without_rule_uses_defaults(self, db_session):
        """无 active rule → 使用保守默认值（10% 单标的, 5 持仓）。"""
        config = _build_rule_config(None)

        assert config["position_config"]["value"] == 0.1
        assert config["position_config"]["max_positions"] == 5
        assert config["buy_conditions"]["actions"] == ["open", "buy_dip"]
        assert config["sell_conditions"]["score_actions"] == ["exit", "reduce"]

    def test_rule_with_none_fields_uses_defaults(self, db_session):
        """rule 存在但 max_single_position_pct/max_open_positions 为 None → 使用默认值。

        使用 SimpleNamespace 模拟，因为 PortfolioRule 模型有 NOT NULL 约束，
        但 _build_rule_config 通过 `or` 短路保护 None 场景（防御性编程）。
        """
        from types import SimpleNamespace
        rule = SimpleNamespace(
            max_single_position_pct=None,
            max_open_positions=None,
        )

        config = _build_rule_config(rule)

        assert config["position_config"]["value"] == 0.1  # 默认
        assert config["position_config"]["max_positions"] == 5  # 默认


# ============================================================================
# 3. run_portfolio_backtest 错误场景
# ============================================================================

class TestRunPortfolioBacktestErrors:
    """守护前置校验的边界处理。"""

    def test_portfolio_not_found_raises(self, db_session):
        """组合不存在 → ValueError('not found')。"""
        with pytest.raises(ValueError, match="not found"):
            run_portfolio_backtest(
                db_session, portfolio_id=99999,
                start_date=date(2026, 1, 1), end_date=date(2026, 3, 31),
            )

    def test_portfolio_not_simulated_raises(self, db_session):
        """组合不是 simulated → ValueError('not simulated')。"""
        p = _make_portfolio(db_session, name="QA-Real", account_type="real")
        with pytest.raises(ValueError, match="not simulated"):
            run_portfolio_backtest(
                db_session, portfolio_id=p.id,
                start_date=date(2026, 1, 1), end_date=date(2026, 3, 31),
            )

    def test_auto_trade_disabled_raises(self, db_session):
        """auto_trade_enabled=0 → ValueError('auto_trade_enabled')。"""
        p = _make_portfolio(db_session, name="QA-Disabled", auto_trade_enabled=False)
        with pytest.raises(ValueError, match="auto_trade_enabled"):
            run_portfolio_backtest(
                db_session, portfolio_id=p.id,
                start_date=date(2026, 1, 1), end_date=date(2026, 3, 31),
            )

    def test_total_capital_zero_raises(self, db_session):
        """total_capital=0 → ValueError('total_capital')。"""
        p = _make_portfolio(db_session, name="QA-NoCap", total_capital=0.0)
        with pytest.raises(ValueError, match="total_capital"):
            run_portfolio_backtest(
                db_session, portfolio_id=p.id,
                start_date=date(2026, 1, 1), end_date=date(2026, 3, 31),
            )

    def test_no_symbols_raises(self, db_session):
        """无持仓 + 无 scan 候选 → ValueError('Cannot run whole-portfolio')。"""
        p = _make_portfolio(db_session, name="QA-NoSymbols")
        with pytest.raises(ValueError, match="Cannot run whole-portfolio"):
            run_portfolio_backtest(
                db_session, portfolio_id=p.id,
                start_date=date(2026, 1, 1), end_date=date(2026, 3, 31),
            )


# ============================================================================
# 4. run_portfolio_backtest 成功场景
# ============================================================================

class TestRunPortfolioBacktestSuccess:
    """守护完整回测执行的正确性。"""

    def test_success_with_scan_candidate_produces_trade(self, db_session):
        """完整场景：scan 候选 + Score.action=open + DailyBar → 产生 BacktestRun + BacktestTrade。

        信号链路：
        - Score.action='open' ∈ {open, buy_dip} → 触发买入
        - 后续 Score.action='exit' ∈ {exit, reduce} → 触发卖出
        """
        p = _make_portfolio(db_session, name="QA-Success", total_capital=100000.0)
        sym = _make_symbol(db_session, symbol="600010", name="测试")
        _make_active_rule(db_session, p.id)

        # 造 5 天行情
        for i, d in enumerate([date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7),
                                date(2026, 1, 8), date(2026, 1, 9)]):
            _make_daily_bar(db_session, sym.id, d, close=10.0 + i * 0.5)

        # 第 1 天 Score.action=open 触发买入
        _make_score(db_session, sym.id, action="open", stage="start", trade_date=date(2026, 1, 5))
        # 第 3 天 Score.action=exit 触发卖出
        _make_score(db_session, sym.id, action="exit", stage="overheat", trade_date=date(2026, 1, 7))

        # scan 候选
        _make_scan_run_with_candidate(db_session, p.id, sym.id)

        result = run_portfolio_backtest(
            db_session, portfolio_id=p.id,
            start_date=date(2026, 1, 5), end_date=date(2026, 1, 9),
        )

        # 验证返回结构
        assert result["portfolio_id"] == p.id
        assert result["symbol_ids"] == [sym.id]
        assert result["symbol_count"] == 1
        assert result["status"] == "completed"
        assert result["initial_capital"] == 100000.0
        assert "组合整体回测" in result["run_name"]

        # 验证 BacktestRun 写入 DB
        run = db_session.get(BacktestRun, result["run_id"])
        assert run is not None
        assert run.status == "completed"
        assert run.portfolio_id == p.id

        # 验证产生了 BacktestTrade（买入 + 卖出 = 1 笔完整 trade）
        trades = db_session.query(BacktestTrade).filter_by(run_id=run.id).all()
        assert len(trades) >= 1
        trade = trades[0]
        assert trade.symbol_id == sym.id
        assert trade.entry_date == date(2026, 1, 5)
        assert trade.exit_date is not None  # 应该已平仓

    def test_success_with_positions_only(self, db_session):
        """有持仓、无 scan 候选 → 也能跑回测（symbol_ids 来自持仓）。"""
        p = _make_portfolio(db_session, name="QA-PosOnly", total_capital=100000.0)
        sym = _make_symbol(db_session, symbol="600011", name="持仓标的")
        _make_active_rule(db_session, p.id)
        _make_position(db_session, p.id, sym.id, quantity=100, avg_cost=10.0, latest_price=10.0)

        # 造 3 天行情
        for i, d in enumerate([date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)]):
            _make_daily_bar(db_session, sym.id, d, close=10.0 + i * 0.2)

        # 无 Score → 无买入信号，但回测应正常完成（0 trades）
        result = run_portfolio_backtest(
            db_session, portfolio_id=p.id,
            start_date=date(2026, 1, 5), end_date=date(2026, 1, 7),
        )

        assert result["status"] == "completed"
        assert result["symbol_ids"] == [sym.id]
        run = db_session.get(BacktestRun, result["run_id"])
        assert run is not None

    def test_run_name_custom(self, db_session):
        """自定义 run_name 被透传到 BacktestRun。"""
        p = _make_portfolio(db_session, name="QA-CustomName", total_capital=100000.0)
        sym = _make_symbol(db_session, symbol="600012", name="N")
        _make_position(db_session, p.id, sym.id, quantity=100)
        _make_daily_bar(db_session, sym.id, date(2026, 1, 5), close=10.0)
        _make_daily_bar(db_session, sym.id, date(2026, 1, 6), close=10.5)

        result = run_portfolio_backtest(
            db_session, portfolio_id=p.id,
            start_date=date(2026, 1, 5), end_date=date(2026, 1, 6),
            run_name="QA-Custom-Run-Name",
        )

        assert result["run_name"] == "QA-Custom-Run-Name"
        run = db_session.get(BacktestRun, result["run_id"])
        assert run.run_name == "QA-Custom-Run-Name"

    def test_rule_config_uses_active_rule_values(self, db_session):
        """active rule 的 max_single_position_pct/max_open_positions 被映射到 rule_config。

        通过比对 BacktestRun.rule_config_json 验证。
        """
        p = _make_portfolio(db_session, name="QA-RuleMap", total_capital=100000.0)
        sym = _make_symbol(db_session, symbol="600013", name="R")
        rule = PortfolioRule(
            portfolio_id=p.id,
            rule_name="QA-CustomRule",
            max_single_position_pct=0.15,  # 15%
            max_sector_position_pct=0.4,
            max_stock_position_pct=0.6,
            max_etf_position_pct=0.3,
            max_loss_per_trade_pct=0.02,
            max_open_positions=7,  # 7 个持仓
            stage_limits_json="{}",
            is_active=1,
        )
        db_session.add(rule)
        db_session.commit()
        _make_position(db_session, p.id, sym.id, quantity=100)
        _make_daily_bar(db_session, sym.id, date(2026, 1, 5), close=10.0)
        _make_daily_bar(db_session, sym.id, date(2026, 1, 6), close=10.5)

        result = run_portfolio_backtest(
            db_session, portfolio_id=p.id,
            start_date=date(2026, 1, 5), end_date=date(2026, 1, 6),
        )

        run = db_session.get(BacktestRun, result["run_id"])
        rule_config = json.loads(run.rule_config_json)
        assert rule_config["position_config"]["value"] == 0.15
        assert rule_config["position_config"]["max_positions"] == 7
        assert rule_config["buy_conditions"]["actions"] == ["open", "buy_dip"]
        assert rule_config["sell_conditions"]["score_actions"] == ["exit", "reduce"]


# ============================================================================
# 5. API 端点
# ============================================================================

class TestPortfolioBacktestEndpoint:
    """守护 POST /api/v1/backtest/portfolio/run 端点。"""

    def _client_with_db(self, db_session):
        from app.main import app

        def _override_get_db():
            try:
                yield db_session
            finally:
                pass

        app.dependency_overrides[get_db] = _override_get_db
        return TestClient(app)

    def test_api_success(self, db_session):
        """API 正常调用返回 200。"""
        p = _make_portfolio(db_session, name="QA-API-OK", total_capital=100000.0)
        sym = _make_symbol(db_session, symbol="600019", name="API")
        _make_position(db_session, p.id, sym.id, quantity=100)
        _make_daily_bar(db_session, sym.id, date(2026, 1, 5), close=10.0)
        _make_daily_bar(db_session, sym.id, date(2026, 1, 6), close=10.5)

        client = self._client_with_db(db_session)
        resp = client.post(
            "/api/v1/backtest/portfolio/run",
            json={
                "portfolio_id": p.id,
                "start_date": "2026-01-05",
                "end_date": "2026-01-06",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["portfolio_id"] == p.id
        assert data["status"] == "completed"
        assert data["symbol_count"] == 1
        assert data["initial_capital"] == 100000.0

    def test_api_portfolio_not_found_returns_404(self, db_session):
        """组合不存在 → 404。"""
        client = self._client_with_db(db_session)
        resp = client.post(
            "/api/v1/backtest/portfolio/run",
            json={
                "portfolio_id": 99999,
                "start_date": "2026-01-05",
                "end_date": "2026-01-06",
            },
        )
        assert resp.status_code == 404

    def test_api_not_simulated_returns_409(self, db_session):
        """组合不是 simulated → 409。"""
        p = _make_portfolio(db_session, name="QA-API-Real", account_type="real")
        client = self._client_with_db(db_session)
        resp = client.post(
            "/api/v1/backtest/portfolio/run",
            json={
                "portfolio_id": p.id,
                "start_date": "2026-01-05",
                "end_date": "2026-01-06",
            },
        )
        assert resp.status_code == 409

    def test_api_auto_trade_disabled_returns_409(self, db_session):
        """auto_trade_enabled=0 → 409。"""
        p = _make_portfolio(db_session, name="QA-API-Disabled", auto_trade_enabled=False)
        client = self._client_with_db(db_session)
        resp = client.post(
            "/api/v1/backtest/portfolio/run",
            json={
                "portfolio_id": p.id,
                "start_date": "2026-01-05",
                "end_date": "2026-01-06",
            },
        )
        assert resp.status_code == 409

    def test_api_no_symbols_returns_400(self, db_session):
        """无标的可回测 → 400。"""
        p = _make_portfolio(db_session, name="QA-API-NoSymbols")
        client = self._client_with_db(db_session)
        resp = client.post(
            "/api/v1/backtest/portfolio/run",
            json={
                "portfolio_id": p.id,
                "start_date": "2026-01-05",
                "end_date": "2026-01-06",
            },
        )
        assert resp.status_code == 400

    def test_api_end_date_before_start_returns_422(self, db_session):
        """end_date < start_date → 422（Pydantic 校验）。"""
        p = _make_portfolio(db_session, name="QA-API-BadDate")
        client = self._client_with_db(db_session)
        resp = client.post(
            "/api/v1/backtest/portfolio/run",
            json={
                "portfolio_id": p.id,
                "start_date": "2026-01-10",
                "end_date": "2026-01-05",
            },
        )
        assert resp.status_code == 422

    def test_api_custom_run_name(self, db_session):
        """自定义 run_name 透传到响应。"""
        p = _make_portfolio(db_session, name="QA-API-Name", total_capital=100000.0)
        sym = _make_symbol(db_session, symbol="600020", name="N")
        _make_position(db_session, p.id, sym.id, quantity=100)
        _make_daily_bar(db_session, sym.id, date(2026, 1, 5), close=10.0)
        _make_daily_bar(db_session, sym.id, date(2026, 1, 6), close=10.5)

        client = self._client_with_db(db_session)
        resp = client.post(
            "/api/v1/backtest/portfolio/run",
            json={
                "portfolio_id": p.id,
                "start_date": "2026-01-05",
                "end_date": "2026-01-06",
                "run_name": "API-Custom",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["run_name"] == "API-Custom"
