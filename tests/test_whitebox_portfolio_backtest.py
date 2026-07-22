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
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.db.session import get_db
from app.models.backtest import BacktestRun, BacktestTrade
from app.models.daily_bar import DailyBar
from app.models.portfolio import Portfolio, PortfolioRule, Position
from app.models.portfolio_member import PortfolioMember
from app.models.scan import ScanResult, ScanRun
from app.models.score import Score
from app.models.symbol import Symbol
from app.services.portfolio_backtest import (
    _build_rule_config,
    _derive_symbol_ids,
    compare_new_old_engine,
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

    def test_success_with_scan_candidate_produces_trade(self, db_session, member_source_disabled):
        """完整场景：scan 候选 + Score.action=open + DailyBar → 产生 BacktestRun + BacktestTrade。

        信号链路：
        - Score.action='open' ∈ {open, buy_dip} → 触发买入
        - 后续 Score.action='exit' ∈ {exit, reduce} → 触发卖出

        注：WP9.5 后默认来源为 member，本测试守护 legacy 来源行为，使用
        member_source_disabled fixture 临时关闭成员来源开关。
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

    def test_success_with_positions_only(self, db_session, member_source_disabled):
        """有持仓、无 scan 候选 → 也能跑回测（symbol_ids 来自持仓）。

        注：WP9.5 后默认来源为 member，本测试守护 legacy 来源行为，使用
        member_source_disabled fixture 临时关闭成员来源开关。
        """
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

    def test_run_name_custom(self, db_session, member_source_disabled):
        """自定义 run_name 被透传到 BacktestRun。

        注：WP9.5 后默认来源为 member，本测试守护 legacy 来源行为，使用
        member_source_disabled fixture 临时关闭成员来源开关。
        """
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

    def test_rule_config_uses_active_rule_values(self, db_session, member_source_disabled):
        """active rule 的 max_single_position_pct/max_open_positions 被映射到 rule_config。

        通过比对 BacktestRun.rule_config_json 验证。

        注：WP9.5 后默认来源为 member，本测试守护 legacy 来源行为，使用
        member_source_disabled fixture 临时关闭成员来源开关。
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

    def test_api_success(self, db_session, member_source_disabled):
        """API 正常调用返回 200。

        注：WP9.5 后默认来源为 member，本测试守护 legacy 来源行为，使用
        member_source_disabled fixture 临时关闭成员来源开关。
        """
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

    def test_api_custom_run_name(self, db_session, member_source_disabled):
        """自定义 run_name 透传到响应。

        注：WP9.5 后默认来源为 member，本测试守护 legacy 来源行为，使用
        member_source_disabled fixture 临时关闭成员来源开关。
        """
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


# ============================================================================
# 6. WP7 组合回测成员化与历史可复现（WP7.5 测试补全）
#
# 覆盖 spec WP7 验收清单 L254-L270 后端测试项（除前端项 L263/L269）：
# - L258 历史回测即使成员/规则/模型改变仍按原快照可读
# - L259 同一快照重复运行得到一致标的集/规则/成本
# - L262 PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED 开关路由新旧来源
# - L264 新旧引擎使用相同日期/资金/成本后做对比；差异可解释
# - L265 VectorBT 可用于快速研究对比，正式回测以事件驱动引擎为权威
# - L266 完整组合回测前提：全部有效成员均为 auto 且规则有效
# - L267 存在 manual/confirm 成员时默认禁止完整回测，提供"仅回测自动成员"选项及排除清单
# - L268 切回旧开关后原历史回测仍可查看
# - L270 test_whitebox_portfolio_backtest.py 扩展通过（本类全部用例通过即满足）
# ============================================================================


# --- WP7 测试辅助函数与 fixtures ---


def _utc_naive(dt: datetime) -> datetime:
    """将 datetime 规范化为 naive UTC（与项目模型保持一致）。"""
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _make_portfolio_member(
    db_session,
    *,
    portfolio_id: int,
    symbol_id: int,
    status: str = "active",
    execution_mode: str = "auto",
    entry_rule_version_id: int | None = 1,
    exit_rule_version_id: int | None = 2,
    effective_from: datetime | None = None,
    effective_to: datetime | None = None,
) -> PortfolioMember:
    """直接构造 PortfolioMember 记录，用于精确控制 effective_from/effective_to。

    注意：模型有部分唯一索引（portfolio_id, symbol_id where effective_to IS NULL），
    同一组合同一标的同时只能有一条 effective_to IS NULL 的记录。
    """
    now = _utc_naive(datetime.now(timezone.utc))
    member = PortfolioMember(
        portfolio_id=portfolio_id,
        symbol_id=symbol_id,
        status=status,
        execution_mode=execution_mode,
        source_type="manual",
        entry_rule_version_id=entry_rule_version_id,
        exit_rule_version_id=exit_rule_version_id,
        effective_from=_utc_naive(effective_from) if effective_from else now,
        effective_to=_utc_naive(effective_to) if effective_to else None,
        manual_lock=False,
        priority=0,
        created_at=now,
    )
    db_session.add(member)
    db_session.commit()
    db_session.refresh(member)
    return member


@pytest.fixture
def member_source_enabled():
    """临时开启 PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED，测试结束后恢复原值。

    settings 是单例，必须确保恢复，避免污染同进程后续测试。
    """
    original = settings.PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED
    settings.PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED = True
    try:
        yield
    finally:
        settings.PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED = original


@pytest.fixture
def member_source_disabled():
    """临时关闭 PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED，测试结束后恢复原值。"""
    original = settings.PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED
    settings.PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED = False
    try:
        yield
    finally:
        settings.PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED = original


class TestWP7BacktestMembership:
    """守护 WP7 新增功能：成员化标的来源、快照可复现、新旧引擎对比、manual 阻止。

    每个测试用例标注覆盖的 WP7 验收清单项编号（L254-L270）。
    """

    # ----- L258 历史快照可读 -----

    def test_historical_backtest_readable_after_member_change(
        self, db_session, member_source_enabled
    ):
        """【L258】创建 BacktestRun（含快照）后修改 PortfolioMember，历史快照仍保留原始内容。

        场景：
        - 回测时 2 个 auto 成员（sym_a, sym_b），快照记录两条成员
        - 回测后归档 sym_a 成员（设置 effective_to 为过去 + status='archived'）
          并改 sym_b execution_mode='manual'
        - 重新读取 BacktestRun，member_snapshot_json 仍保留原始 2 条成员记录
          且 execution_mode 仍为 'auto'
        """
        # Arrange
        p = _make_portfolio(db_session, name="QA-WP7-HistSnapshot")
        sym_a = _make_symbol(db_session, symbol="800010", name="A")
        sym_b = _make_symbol(db_session, symbol="800011", name="B")
        _make_active_rule(db_session, p.id)

        trade_dt = datetime(2026, 1, 10, 12, 0, 0)
        m_a = _make_portfolio_member(
            db_session, portfolio_id=p.id, symbol_id=sym_a.id,
            execution_mode="auto", entry_rule_version_id=1001,
            effective_from=trade_dt - timedelta(days=30),
        )
        m_b = _make_portfolio_member(
            db_session, portfolio_id=p.id, symbol_id=sym_b.id,
            execution_mode="auto", entry_rule_version_id=1002,
            effective_from=trade_dt - timedelta(days=30),
        )

        for i, d in enumerate([date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)]):
            _make_daily_bar(db_session, sym_a.id, d, close=10.0 + i * 0.2)
            _make_daily_bar(db_session, sym_b.id, d, close=20.0 + i * 0.2)
        _make_score(db_session, sym_a.id, action="open", trade_date=date(2026, 1, 5))
        _make_score(db_session, sym_b.id, action="open", trade_date=date(2026, 1, 5))

        # Act - 第一次回测，生成快照
        result = run_portfolio_backtest(
            db_session, portfolio_id=p.id,
            start_date=date(2026, 1, 5), end_date=date(2026, 1, 7),
        )
        run_id = result["run_id"]
        run_before = db_session.get(BacktestRun, run_id)
        original_snapshot = run_before.member_snapshot_json
        original_symbol_ids = run_before.symbol_ids_json

        # 修改成员：归档 m_a + 改 m_b execution_mode='manual'
        m_a.effective_to = trade_dt - timedelta(days=10)
        m_a.status = "archived"
        m_b.execution_mode = "manual"
        db_session.commit()

        # Assert - 重新读取 BacktestRun，快照仍保留原始内容
        run_after = db_session.get(BacktestRun, run_id)
        assert run_after.member_snapshot_json == original_snapshot
        assert run_after.symbol_ids_json == original_symbol_ids

        snapshot = json.loads(run_after.member_snapshot_json)
        assert len(snapshot) == 2  # 仍为 2 条
        member_ids = {item["member_id"] for item in snapshot}
        assert member_ids == {m_a.id, m_b.id}
        # 快照中两条成员 execution_mode 仍为 'auto'（修改前的状态）
        for item in snapshot:
            assert item["execution_mode"] == "auto"

    # ----- L259 同一快照重复运行一致 -----

    def test_same_parameters_produce_consistent_symbol_set(
        self, db_session, member_source_enabled
    ):
        """【L259】相同 portfolio_id/start_date/end_date/only_auto 参数两次运行得到相同 symbol_ids。

        基于快照 JSON 对比，确保同一组参数的可复现性。
        """
        # Arrange
        p = _make_portfolio(db_session, name="QA-WP7-Consistent")
        sym_a = _make_symbol(db_session, symbol="800020", name="C")
        sym_b = _make_symbol(db_session, symbol="800021", name="D")
        _make_active_rule(db_session, p.id)

        trade_dt = datetime(2026, 1, 10, 12, 0, 0)
        _make_portfolio_member(
            db_session, portfolio_id=p.id, symbol_id=sym_a.id,
            execution_mode="auto", entry_rule_version_id=1001,
            effective_from=trade_dt - timedelta(days=30),
        )
        _make_portfolio_member(
            db_session, portfolio_id=p.id, symbol_id=sym_b.id,
            execution_mode="auto", entry_rule_version_id=1002,
            effective_from=trade_dt - timedelta(days=30),
        )

        for i, d in enumerate([date(2026, 1, 5), date(2026, 1, 6)]):
            _make_daily_bar(db_session, sym_a.id, d, close=10.0 + i * 0.2)
            _make_daily_bar(db_session, sym_b.id, d, close=20.0 + i * 0.2)
        _make_score(db_session, sym_a.id, action="open", trade_date=date(2026, 1, 5))
        _make_score(db_session, sym_b.id, action="open", trade_date=date(2026, 1, 5))

        # Act - 两次运行相同参数
        r1 = run_portfolio_backtest(
            db_session, portfolio_id=p.id,
            start_date=date(2026, 1, 5), end_date=date(2026, 1, 6),
        )
        r2 = run_portfolio_backtest(
            db_session, portfolio_id=p.id,
            start_date=date(2026, 1, 5), end_date=date(2026, 1, 6),
        )

        # Assert - symbol_ids 一致（基于快照 JSON 对比）
        run1 = db_session.get(BacktestRun, r1["run_id"])
        run2 = db_session.get(BacktestRun, r2["run_id"])
        assert run1.symbol_ids_json == run2.symbol_ids_json
        assert json.loads(run1.symbol_ids_json) == sorted([sym_a.id, sym_b.id])
        assert json.loads(run2.symbol_ids_json) == sorted([sym_a.id, sym_b.id])
        # member_snapshot_json 内容一致（成员 ID 集合相同）
        snap1 = json.loads(run1.member_snapshot_json)
        snap2 = json.loads(run2.member_snapshot_json)
        assert {item["member_id"] for item in snap1} == {item["member_id"] for item in snap2}
        # rule_config / cost_config 一致
        assert run1.rule_config_json == run2.rule_config_json

    # ----- L262 旧来源开关行为 -----

    def test_legacy_source_uses_old_logic_when_disabled(
        self, db_session, member_source_disabled
    ):
        """【L262】开关关闭时使用 _derive_symbol_ids（持仓+扫描），不读取成员。

        场景：
        - 持仓 sym_a + scan 候选 sym_b（legacy 应返回 [sym_a, sym_b]）
        - 成员 sym_c（不应被使用，因开关关闭）
        - 期望 symbol_ids == [sym_a, sym_b]，不含 sym_c
        """
        # Arrange
        p = _make_portfolio(db_session, name="QA-WP7-LegacyOff")
        sym_a = _make_symbol(db_session, symbol="800030", name="E")
        sym_b = _make_symbol(db_session, symbol="800031", name="F")
        sym_c = _make_symbol(db_session, symbol="800032", name="G")
        _make_active_rule(db_session, p.id)

        _make_position(db_session, p.id, sym_a.id, quantity=100)
        _make_scan_run_with_candidate(db_session, p.id, sym_b.id)

        # 造成员 sym_c（应被忽略）
        trade_dt = datetime(2026, 1, 10, 12, 0, 0)
        _make_portfolio_member(
            db_session, portfolio_id=p.id, symbol_id=sym_c.id,
            execution_mode="auto", entry_rule_version_id=1003,
            effective_from=trade_dt - timedelta(days=30),
        )

        for d in [date(2026, 1, 5), date(2026, 1, 6)]:
            _make_daily_bar(db_session, sym_a.id, d, close=10.0)
            _make_daily_bar(db_session, sym_b.id, d, close=10.0)

        # Act
        result = run_portfolio_backtest(
            db_session, portfolio_id=p.id,
            start_date=date(2026, 1, 5), end_date=date(2026, 1, 6),
        )

        # Assert - 来源为 legacy，symbol_ids 来自持仓+扫描
        assert result["symbol_source"] == "legacy"
        assert result["source_type"] == "legacy_scan"
        assert sorted(result["symbol_ids"]) == sorted([sym_a.id, sym_b.id])
        assert sym_c.id not in result["symbol_ids"]

    def test_member_source_uses_new_logic_when_enabled(
        self, db_session, member_source_enabled
    ):
        """【L262】开关开启时使用 _derive_symbol_ids_from_members，不读取持仓/扫描。

        场景：
        - 成员 sym_a（member 应返回 [sym_a]）
        - 持仓 sym_b + scan 候选 sym_c（不应被使用，因开关开启）
        - 期望 symbol_ids == [sym_a]，不含 sym_b/sym_c
        """
        # Arrange
        p = _make_portfolio(db_session, name="QA-WP7-MemberOn")
        sym_a = _make_symbol(db_session, symbol="800040", name="H")
        sym_b = _make_symbol(db_session, symbol="800041", name="I")
        sym_c = _make_symbol(db_session, symbol="800042", name="J")
        _make_active_rule(db_session, p.id)

        trade_dt = datetime(2026, 1, 10, 12, 0, 0)
        _make_portfolio_member(
            db_session, portfolio_id=p.id, symbol_id=sym_a.id,
            execution_mode="auto", entry_rule_version_id=1001,
            effective_from=trade_dt - timedelta(days=30),
        )
        # 持仓 sym_b + scan 候选 sym_c（应被忽略）
        _make_position(db_session, p.id, sym_b.id, quantity=100)
        _make_scan_run_with_candidate(db_session, p.id, sym_c.id)

        for d in [date(2026, 1, 5), date(2026, 1, 6)]:
            _make_daily_bar(db_session, sym_a.id, d, close=10.0)
        _make_score(db_session, sym_a.id, action="open", trade_date=date(2026, 1, 5))

        # Act
        result = run_portfolio_backtest(
            db_session, portfolio_id=p.id,
            start_date=date(2026, 1, 5), end_date=date(2026, 1, 6),
        )

        # Assert - 来源为 members，symbol_ids 仅来自成员
        assert result["symbol_source"] == "members"
        assert result["source_type"] == "member"
        assert result["symbol_ids"] == [sym_a.id]
        assert sym_b.id not in result["symbol_ids"]
        assert sym_c.id not in result["symbol_ids"]

    # ----- L264 新旧引擎对比 -----

    def test_compare_new_old_engine_returns_complete_diff(
        self, db_session, member_source_disabled
    ):
        """【L264】compare_new_old_engine 返回完整 diff 结构。

        验证返回值包含：
        - legacy_run_id / member_run_id（两次回测的 run_id）
        - symbols_only_in_legacy（旧有新无，diff.symbol_ids_removed）
        - symbols_only_in_member（新有旧无，diff.symbol_ids_added）
        - metrics_diff（6 个指标的 old/new/delta）
        - explanation（文本解释，提及标的集差异）
        """
        # Arrange - 旧来源：持仓 sym_a；新来源：成员 sym_b（sym_a 不在成员中）
        p = _make_portfolio(db_session, name="QA-WP7-Compare")
        sym_a = _make_symbol(db_session, symbol="800050", name="K")
        sym_b = _make_symbol(db_session, symbol="800051", name="L")
        _make_active_rule(db_session, p.id)

        _make_position(db_session, p.id, sym_a.id, quantity=100)

        trade_dt = datetime(2026, 1, 10, 12, 0, 0)
        _make_portfolio_member(
            db_session, portfolio_id=p.id, symbol_id=sym_b.id,
            execution_mode="auto", entry_rule_version_id=1002,
            effective_from=trade_dt - timedelta(days=30),
        )

        for i, d in enumerate([date(2026, 1, 5), date(2026, 1, 6)]):
            _make_daily_bar(db_session, sym_a.id, d, close=10.0 + i * 0.2)
            _make_daily_bar(db_session, sym_b.id, d, close=20.0 + i * 0.2)
        _make_score(db_session, sym_a.id, action="open", trade_date=date(2026, 1, 5))
        _make_score(db_session, sym_b.id, action="open", trade_date=date(2026, 1, 5))

        # Act
        result = compare_new_old_engine(
            db_session, portfolio_id=p.id,
            start_date=date(2026, 1, 5), end_date=date(2026, 1, 6),
        )

        # Assert - 结构完整
        assert "old" in result
        assert "new" in result
        assert "diff" in result

        # legacy_run_id / member_run_id
        legacy_run_id = result["old"]["run_id"]
        member_run_id = result["new"]["run_id"]
        assert legacy_run_id is not None
        assert member_run_id is not None
        assert legacy_run_id != member_run_id

        # 来源标签
        assert result["old"]["source_type"] == "legacy_scan"
        assert result["new"]["source_type"] == "member"

        # symbols_only_in_legacy = sym_a（旧有新无）
        symbols_only_in_legacy = result["diff"]["symbol_ids_removed"]
        assert sym_a.id in symbols_only_in_legacy
        assert sym_b.id not in symbols_only_in_legacy

        # symbols_only_in_member = sym_b（新有旧无）
        symbols_only_in_member = result["diff"]["symbol_ids_added"]
        assert sym_b.id in symbols_only_in_member
        assert sym_a.id not in symbols_only_in_member

        # metrics_diff 包含 6 个指标，每个含 old/new/delta
        metrics_diff = result["diff"]["metrics_diff"]
        for key in ("total_return", "total_return_pct", "max_drawdown",
                    "max_drawdown_pct", "sharpe_ratio", "trade_count"):
            assert key in metrics_diff
            assert "old" in metrics_diff[key]
            assert "new" in metrics_diff[key]
            assert "delta" in metrics_diff[key]

        # explanation 非空且提及标的集差异
        explanation = result["diff"]["explanation"]
        assert isinstance(explanation, str)
        assert len(explanation) > 0
        assert "标的" in explanation

    # ----- L265 VectorBT 仅研究 -----

    def test_vectorbt_module_has_research_only_docstring(self):
        """【L265】vectorbt_backtest.py 模块 docstring 包含"仅作研究/仅供参考/不作为正式回测权威"。

        确保模块明确标注 VectorBT 仅用于研究对比，正式回测以事件驱动引擎为权威。
        """
        # Act
        from app.services import vectorbt_backtest
        docstring = vectorbt_backtest.__doc__ or ""

        # Assert - 至少包含一个研究用途标识
        assert ("仅作研究" in docstring
                or "仅供参考" in docstring
                or "不作为正式回测权威" in docstring), (
            "vectorbt_backtest 模块 docstring 必须明确标注仅作研究用途"
        )

    # ----- L266 完整回测前提 -----

    def test_full_backtest_requires_all_auto_members(
        self, db_session, member_source_enabled
    ):
        """【L266】全 auto + 规则有效时完整回测（only_auto=False）成功完成。

        场景：
        - 2 个 auto 成员，entry_rule_version_id 齐全
        - active rule 存在
        - 期望：only_auto=False 不抛错，回测完成，无排除成员
        """
        # Arrange
        p = _make_portfolio(db_session, name="QA-WP7-AllAuto")
        sym_a = _make_symbol(db_session, symbol="800060", name="M")
        sym_b = _make_symbol(db_session, symbol="800061", name="N")
        _make_active_rule(db_session, p.id)

        trade_dt = datetime(2026, 1, 10, 12, 0, 0)
        _make_portfolio_member(
            db_session, portfolio_id=p.id, symbol_id=sym_a.id,
            execution_mode="auto", entry_rule_version_id=1001,
            effective_from=trade_dt - timedelta(days=30),
        )
        _make_portfolio_member(
            db_session, portfolio_id=p.id, symbol_id=sym_b.id,
            execution_mode="auto", entry_rule_version_id=1002,
            effective_from=trade_dt - timedelta(days=30),
        )

        for i, d in enumerate([date(2026, 1, 5), date(2026, 1, 6)]):
            _make_daily_bar(db_session, sym_a.id, d, close=10.0 + i * 0.2)
            _make_daily_bar(db_session, sym_b.id, d, close=20.0 + i * 0.2)
        _make_score(db_session, sym_a.id, action="open", trade_date=date(2026, 1, 5))
        _make_score(db_session, sym_b.id, action="open", trade_date=date(2026, 1, 5))

        # Act - only_auto=False 不应抛错
        result = run_portfolio_backtest(
            db_session, portfolio_id=p.id,
            start_date=date(2026, 1, 5), end_date=date(2026, 1, 6),
            only_auto=False,
        )

        # Assert
        assert result["status"] == "completed"
        assert result["symbol_source"] == "members"
        assert sorted(result["symbol_ids"]) == sorted([sym_a.id, sym_b.id])
        assert result["excluded_member_count"] == 0

    def test_full_backtest_blocks_when_manual_present(
        self, db_session, member_source_enabled
    ):
        """【L267】含 manual 成员且 only_auto=False 时抛 ValueError，不创建 BacktestRun。

        场景：
        - 1 auto + 1 manual 成员
        - only_auto=False
        - 期望：抛 ValueError("manual/confirm 成员")，不创建任何 BacktestRun
        """
        # Arrange
        p = _make_portfolio(db_session, name="QA-WP7-BlockManual")
        sym_auto = _make_symbol(db_session, symbol="800070", name="O")
        sym_manual = _make_symbol(db_session, symbol="800071", name="P")
        _make_active_rule(db_session, p.id)

        trade_dt = datetime(2026, 1, 10, 12, 0, 0)
        _make_portfolio_member(
            db_session, portfolio_id=p.id, symbol_id=sym_auto.id,
            execution_mode="auto", entry_rule_version_id=1001,
            effective_from=trade_dt - timedelta(days=30),
        )
        _make_portfolio_member(
            db_session, portfolio_id=p.id, symbol_id=sym_manual.id,
            execution_mode="manual", entry_rule_version_id=1002,
            effective_from=trade_dt - timedelta(days=30),
        )

        for d in [date(2026, 1, 5), date(2026, 1, 6)]:
            _make_daily_bar(db_session, sym_auto.id, d, close=10.0)
            _make_daily_bar(db_session, sym_manual.id, d, close=10.0)

        # Act + Assert - 抛 ValueError
        with pytest.raises(ValueError, match="manual/confirm 成员"):
            run_portfolio_backtest(
                db_session, portfolio_id=p.id,
                start_date=date(2026, 1, 5), end_date=date(2026, 1, 6),
                only_auto=False,
            )

        # 不应创建任何 BacktestRun
        runs = db_session.query(BacktestRun).filter_by(portfolio_id=p.id).all()
        assert len(runs) == 0

    # ----- L267 manual/confirm 选项 -----

    def test_only_auto_option_excludes_manual_members(
        self, db_session, member_source_enabled
    ):
        """【L267】only_auto=True 跳过 manual 成员并记录到 excluded_members_json。

        场景：
        - 1 auto + 1 manual 成员
        - only_auto=True
        - 期望：回测完成，symbol_ids 仅含 auto 成员标的，
          excluded_members_json 含 manual 成员及 reason
        """
        # Arrange
        p = _make_portfolio(db_session, name="QA-WP7-OnlyAutoSkip")
        sym_auto = _make_symbol(db_session, symbol="800080", name="Q")
        sym_manual = _make_symbol(db_session, symbol="800081", name="R")
        _make_active_rule(db_session, p.id)

        trade_dt = datetime(2026, 1, 10, 12, 0, 0)
        m_auto = _make_portfolio_member(
            db_session, portfolio_id=p.id, symbol_id=sym_auto.id,
            execution_mode="auto", entry_rule_version_id=1001,
            effective_from=trade_dt - timedelta(days=30),
        )
        m_manual = _make_portfolio_member(
            db_session, portfolio_id=p.id, symbol_id=sym_manual.id,
            execution_mode="manual", entry_rule_version_id=1002,
            effective_from=trade_dt - timedelta(days=30),
        )

        for i, d in enumerate([date(2026, 1, 5), date(2026, 1, 6)]):
            _make_daily_bar(db_session, sym_auto.id, d, close=10.0 + i * 0.2)
            _make_daily_bar(db_session, sym_manual.id, d, close=20.0 + i * 0.2)
        _make_score(db_session, sym_auto.id, action="open", trade_date=date(2026, 1, 5))

        # Act
        result = run_portfolio_backtest(
            db_session, portfolio_id=p.id,
            start_date=date(2026, 1, 5), end_date=date(2026, 1, 6),
            only_auto=True,
        )

        # Assert
        assert result["status"] == "completed"
        assert result["symbol_ids"] == [sym_auto.id]
        assert result["excluded_member_count"] == 1

        run = db_session.get(BacktestRun, result["run_id"])
        excluded = json.loads(run.excluded_members_json)
        assert len(excluded) == 1
        assert excluded[0]["member_id"] == m_manual.id
        assert excluded[0]["symbol_id"] == sym_manual.id
        assert "execution_mode" in excluded[0]["reason"]
        assert "manual" in excluded[0]["reason"]

        # member_snapshot_json 应仅含 auto 成员
        snapshot = json.loads(run.member_snapshot_json)
        assert len(snapshot) == 1
        assert snapshot[0]["member_id"] == m_auto.id
        assert snapshot[0]["execution_mode"] == "auto"

    # ----- L268 切换后历史可读 -----

    def test_historical_legacy_backtest_readable_after_switch_to_member(
        self, db_session, member_source_disabled
    ):
        """【L268】旧来源创建的回测，切换到新来源后仍可读取。

        场景：
        - 创建一个 source_type='legacy_scan' 的 BacktestRun（模拟旧来源回测）
        - 切换开关到 member 来源
        - 验证 db_session.get 仍能读出原始记录，source_type 保持 'legacy_scan'
        """
        # Arrange - 创建一个 legacy_scan 来源的历史回测
        p = _make_portfolio(db_session, name="QA-WP7-LegacyHist")
        run = BacktestRun(
            portfolio_id=p.id,
            run_name="legacy-history-run",
            symbols_json=json.dumps([1, 2, 3]),
            rule_config_json=json.dumps({"buy_conditions": {}}),
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 31),
            initial_capital=100000.0,
            status="completed",
            engine_name="event_driven",
            engine_version="1.0.0",
            source_type="legacy_scan",
        )
        db_session.add(run)
        db_session.commit()
        db_session.refresh(run)
        original_id = run.id

        # Act - 切换到 member 来源
        settings.PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED = True
        try:
            reloaded = db_session.get(BacktestRun, original_id)
        finally:
            settings.PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED = False

        # Assert - 历史回测仍可读，source_type 保持原值
        assert reloaded is not None
        assert reloaded.run_name == "legacy-history-run"
        assert reloaded.status == "completed"
        assert reloaded.source_type == "legacy_scan"
        assert reloaded.engine_name == "event_driven"
        # legacy 来源的历史回测没有 member 快照
        assert reloaded.member_snapshot_json is None
        assert reloaded.symbol_ids_json is None

    def test_historical_member_backtest_readable_after_switch_to_legacy(
        self, db_session, member_source_disabled
    ):
        """【L268】新来源创建的回测，切换到旧来源后仍可读取。

        场景：
        - 创建一个 source_type='member' 的 BacktestRun（含完整快照，模拟 member 来源回测）
        - 切换开关到 legacy 来源（关闭）
        - 验证 db_session.get 仍能读出原始记录，快照字段保持原值
        """
        # Arrange - 创建一个 member 来源的历史回测（含完整快照）
        p = _make_portfolio(db_session, name="QA-WP7-MemberHist")
        sym = _make_symbol(db_session, symbol="800090", name="S")

        member_snapshot_data = [
            {
                "member_id": 8888,
                "symbol_id": sym.id,
                "effective_from": "2026-01-01T00:00:00",
                "effective_to": None,
                "execution_mode": "auto",
                "entry_rule_version_id": 1001,
                "exit_rule_version_id": 2001,
            }
        ]
        run = BacktestRun(
            portfolio_id=p.id,
            run_name="member-history-run",
            symbols_json=json.dumps([sym.id]),
            rule_config_json=json.dumps({"buy_conditions": {}}),
            cost_config_json=json.dumps({"commission_rate": None}),
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 31),
            initial_capital=100000.0,
            status="completed",
            # WP7.2/WP7.3 快照字段
            member_snapshot_json=json.dumps(member_snapshot_data, ensure_ascii=False),
            symbol_ids_json=json.dumps([sym.id]),
            excluded_members_json=json.dumps([]),
            portfolio_rule_version_id=42,
            score_mode="auto_trade_signal",
            data_cutoff_at=datetime(2026, 1, 31),
            engine_name="event_driven",
            engine_version="1.0.0",
            source_type="member",
        )
        db_session.add(run)
        db_session.commit()
        db_session.refresh(run)
        original_id = run.id

        # Act - 切换到 legacy 来源（关闭 member source）
        # member_source_disabled fixture 已经将开关关闭，此处显式再确认
        assert settings.PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED is False

        reloaded = db_session.get(BacktestRun, original_id)

        # Assert - 历史 member 来源回测仍可读，快照字段保持原值
        assert reloaded is not None
        assert reloaded.run_name == "member-history-run"
        assert reloaded.source_type == "member"
        assert reloaded.engine_name == "event_driven"
        assert reloaded.engine_version == "1.0.0"
        assert reloaded.portfolio_rule_version_id == 42
        assert reloaded.score_mode == "auto_trade_signal"

        # JSON 字段按原值回读
        assert json.loads(reloaded.symbol_ids_json) == [sym.id]
        assert json.loads(reloaded.member_snapshot_json) == member_snapshot_data
        assert json.loads(reloaded.excluded_members_json) == []
