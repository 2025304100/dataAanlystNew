"""T-A7 Q7.1：价格数据全缺时 HOLD/DATA_BLOCKED，绝不猜价。

覆盖：
- T-A7-C1：2 日回测，第 2 日 1 只价格全 None → HOLD/DATA_BLOCKED
- T-A7-C2：绝不猜价（executed_price=NULL，内部从未走 fallback）
- T-A7-C3：portfolio 持仓保持不变（target_qty_delta=0, target_quantity 不变）
- T-A7-B0：控制组，第 2 日价格正常 → 正常 BUY/SELL 行为不变
"""
from __future__ import annotations

import os
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

os.environ.setdefault("ALEMBIC_DATABASE_URL", "")


@pytest.fixture(scope="function")
def tmp_alembic_db():
    """每个函数独立 SQLite 文件，显式跑 alembic upgrade head（与 G0 契约一致）。"""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="qa_g1_datablock_")
    os.close(fd)
    url = f"sqlite:///{path}"
    os.environ["ALEMBIC_DATABASE_URL"] = url
    from alembic.config import Config
    from alembic import command as ac
    cfg = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parent.parent / "alembic"))
    ac.upgrade(cfg, "head")
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    engine = create_engine(url)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    sess = SessionLocal()
    try:
        yield sess
    finally:
        sess.close()
        engine.dispose()
        try:
            os.remove(path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _make_portfolio(db, **kw):
    from app.models.portfolio import Portfolio
    p = Portfolio(
        name=kw.pop("name", "T_A7_Test"),
        account_type=kw.pop("account_type", "sim"),
        asset_scope=kw.pop("asset_scope", "mixed"),
        total_capital=kw.pop("total_capital", 1_000_000.0),
        investable_ratio=kw.pop("investable_ratio", 1.0),
        cash_reserve_ratio=kw.pop("cash_reserve_ratio", 0.05),
        currency=kw.pop("currency", "CNY"),
        is_default=kw.pop("is_default", 0),
        buy_fee_pct=kw.pop("buy_fee_pct", 0.00025),
        sell_fee_pct=kw.pop("sell_fee_pct", 0.00025),
        benchmark_code=kw.pop("benchmark_code", "000300"),
        default_single_position_pct=kw.pop("default_single_position_pct", 0.3),
        auto_trade_enabled=kw.pop("auto_trade_enabled", 0),
    )
    for k, v in kw.items():
        setattr(p, k, v)
    db.add(p); db.flush()
    return p


def _make_symbol(db, code="TEST", name="Test", market="SSE"):
    from app.models.symbol import Symbol
    s = Symbol(symbol=code, name=name, market=market, asset_type="STOCK", is_active=1)
    for attr in ("board", "industry", "theme"):
        if not hasattr(s, attr):
            setattr(s, attr, None)
    db.add(s); db.flush(); return s


def _set_active_model(db, model_run_id: str | None):
    from app.models.factor_runtime import FactorRuntimeState
    s = db.get(FactorRuntimeState, 1)
    if s is None:
        s = FactorRuntimeState(id=1, weight_mode="manual", active_model_run_id=model_run_id,
                               updated_by="qa", version=1)
        db.add(s)
    else:
        s.weight_mode = "manual"; s.active_model_run_id = model_run_id; s.version += 1
    db.flush(); return s


def _save_and_apply(db, portfolio_id, fmr_id, run_mode="research"):
    from app.services.factor_usage_service import save_and_apply_usage
    from app.schemas.decision_engine import FactorUsageBindRequest
    return save_and_apply_usage(
        db, portfolio_id,
        FactorUsageBindRequest(factor_model_run_id=fmr_id, run_mode=run_mode, pit_mode="best_effort"),
        "qa_user",
    )


def _insert_scores(db, fmr_id, pairs):
    """pairs: list[(symbol_id:int, trade_date:date, value:float, published_at:datetime or None)]"""
    from app.models.score import Score
    for idx, (sym_id, td, value, pub) in enumerate(pairs):
        bid = f"qa_a7_{fmr_id[-6:]}_{idx:03d}"
        cutoff_utc = datetime(td.year, td.month, td.day, 7, 0, 0)
        db.add(Score(
            symbol_id=sym_id, trade_date=td, quality_score=0.0, quality_grade="A",
            timing_score=0.0, stage="growth", action="BUY", priority_score=0.0,
            weight_mode="manual", factor_model_run_id=fmr_id,
            factor_data_cutoff_at=cutoff_utc,
            model_alpha_score=value, calc_batch_id=bid,
            published_at=pub, pit_flag=("PIT_SAFE" if pub else "NOT_CHECKED"),
        ))
    db.flush()


# ======================================================================
# Unit Test: check_data_availability 函数本身
# ======================================================================
class TestCheckDataAvailability:
    def test_all_prices_none_returns_data_blocked(self):
        """全价格 None + volume None → DATA_BLOCKED"""
        from app.services.decision_engine import check_data_availability
        result = check_data_availability(
            symbol_id=42,
            trade_date=date(2025, 1, 7),
            open_price=None,
            close_price=None,
            high_price=None,
            low_price=None,
            volume=None,
            is_suspended_today=False,
        )
        assert result["available"] is False
        assert result["status"] == "DATA_BLOCKED"
        assert "DATA_BLOCKED" in result["reason"]
        assert "symbol_id=42" in result["reason"]

    def test_all_prices_zero_returns_data_blocked(self):
        """全价格 0 + volume 0 → DATA_BLOCKED（Q7.1 偏保守，任一足够阻断）"""
        from app.services.decision_engine import check_data_availability
        result = check_data_availability(
            symbol_id=42,
            trade_date=date(2025, 1, 7),
            open_price=0,
            close_price=0,
            high_price=0,
            low_price=0,
            volume=0,
            is_suspended_today=False,
        )
        assert result["available"] is False
        assert result["status"] == "DATA_BLOCKED"

    def test_volume_missing_only_also_blocks(self):
        """价格有值但 volume None → DATA_BLOCKED（Q7.1 偏保守）"""
        from app.services.decision_engine import check_data_availability
        result = check_data_availability(
            symbol_id=42,
            trade_date=date(2025, 1, 7),
            open_price=10.0,
            close_price=10.5,
            high_price=11.0,
            low_price=9.5,
            volume=None,
            is_suspended_today=False,
        )
        assert result["available"] is False
        assert result["status"] == "DATA_BLOCKED"

    def test_price_mixed_zero_and_none_blocks(self):
        """部分价 0，部分价 None → 视为全部缺失 → DATA_BLOCKED"""
        from app.services.decision_engine import check_data_availability
        result = check_data_availability(
            symbol_id=42,
            trade_date=date(2025, 1, 7),
            open_price=None,
            close_price=0,
            high_price=None,
            low_price=0,
            volume=500,
            is_suspended_today=False,
        )
        assert result["available"] is False
        assert result["status"] == "DATA_BLOCKED"

    def test_is_suspended_today_returns_suspended(self):
        """法定停牌标记 → SUSPENDED（即使价格有值）"""
        from app.services.decision_engine import check_data_availability
        result = check_data_availability(
            symbol_id=42,
            trade_date=date(2025, 1, 7),
            open_price=10.0,
            close_price=10.5,
            high_price=11.0,
            low_price=9.5,
            volume=1000,
            is_suspended_today=True,
        )
        assert result["available"] is False
        assert result["status"] == "SUSPENDED"
        assert "法定停牌" in result["reason"]

    def test_normal_prices_returns_ok(self):
        """价格正常 → OK"""
        from app.services.decision_engine import check_data_availability
        result = check_data_availability(
            symbol_id=42,
            trade_date=date(2025, 1, 7),
            open_price=10.2,
            close_price=10.3,
            high_price=10.5,
            low_price=10.0,
            volume=1000,
            is_suspended_today=False,
        )
        assert result["available"] is True
        assert result["status"] == "OK"
        assert result["reason"] == ""


# ======================================================================
# 直接测试 _default_build_evidence + check_data_availability 接入
# ======================================================================
class TestEvidenceBuilderDataBlocked:
    def _make_evidence_inputs(self, symbol_id: int, current_qty: float = 1000.0,
                              current_pct: float = 0.1,
                              alloc_direction: str = "BUY",
                              alloc_executed_price: float = 10.05,
                              alloc_target_qty: float = 1500.0):
        """构造 _default_build_evidence 的最小输入参数集合。"""
        from app.services.decision_engine import (
            LoadedSnapshot, ResolvedClock, UniverseAndEligibility,
            ScoredUniverse, SignalResult, RiskAllocationResult,
        )
        snap = LoadedSnapshot(
            snapshot=None,  # type: ignore
            factor_model_run_id="fmr_test",
            factor_set_id=None,
            rule_id=None,
            rule_version=None,
            members=[{
                "symbol_id": symbol_id,
                "current_quantity": current_qty,
                "current_position_pct": current_pct,
            }],
            candidate_pool=[],
            gate_policy_version="v1",
            cost_config={"min_lot_size": 100, "slippage_buy_bps": 5, "slippage_sell_bps": 5},
            versions={"run_mode": "research"},
        )
        clock = ResolvedClock(
            decision_at=datetime(2025, 1, 7, 7, 5, 0),
            data_cutoff_at=datetime(2025, 1, 7, 7, 0, 0),
            execution_at=datetime(2025, 1, 8, 7, 30, 0),
        )
        universe = UniverseAndEligibility(
            universe=[{
                "symbol_id": symbol_id,
                "current_quantity": current_qty,
                "current_position_pct": current_pct,
            }],
            universe_count=1,
            member_count=1,
        )
        score_pub = datetime(2025, 1, 7, 6, 59, 0)
        scored = ScoredUniverse(
            items=[{
                "symbol_id": symbol_id,
                "score_id": 1001,
                "score_value": 0.85,
                "score_rank": 1,
                "published_at": score_pub,
            }],
            expected=1,
            actual=1,
            coverage_pct=100.0,
            max_age_days=0,
        )
        signal = SignalResult(items=[{
            "symbol_id": symbol_id, "direction": alloc_direction,
            "reasons": ["SCORE_DRIVEN"], "score_item": {},
            "price": 10.0,
        }])
        alloc = RiskAllocationResult(items=[{
            "symbol_id": symbol_id,
            "direction": alloc_direction,
            "target_position_pct": 0.15,
            "target_quantity": alloc_target_qty,
            "min_lot_size": 100,
            "intended_price": 10.0,
            "executed_price": alloc_executed_price,
            "slippage_bps": 5.0,
            "clamp_steps": [],
            "asset_type": "stock",
            "industry": "tech",
        }])
        return snap, clock, universe, scored, signal, alloc

    # ------------------------------------------------------------------
    # T-A7-C1：价格全 None → HOLD/DATA_BLOCKED
    # ------------------------------------------------------------------
    def test_T_A7_C1_data_blocked_hold_action(self):
        """T-A7-C1：价格全 None 时 action=HOLD, action_subtype=DATA_BLOCKED, 无 BUY/SELL"""
        from app.services.decision_engine import _default_build_evidence
        symbol_id = 42
        snap, clock, universe, scored, signal, alloc = self._make_evidence_inputs(
            symbol_id=symbol_id,
            alloc_direction="BUY",
        )
        price_data = {
            symbol_id: {
                "open_price": None, "close_price": None,
                "high_price": None, "low_price": None,
                "volume": None, "is_suspended_today": False,
            }
        }
        ev_list = _default_build_evidence(
            snap=snap, clock=clock, universe=universe, scored=scored,
            signal=signal, alloc=alloc, blocking_status="READY",
            blocking_reasons=[],
            price_data_by_symbol=price_data,
            trade_date=date(2025, 1, 7),
        )
        assert len(ev_list) == 1
        ev = ev_list[0]
        assert ev.symbol_id == symbol_id
        assert ev.action == "HOLD", f"应为 HOLD，实际 {ev.action}"
        assert ev.action_subtype == "DATA_BLOCKED", f"应为 DATA_BLOCKED，实际 {ev.action_subtype}"
        assert ev.target_qty_delta == 0.0, f"零调仓 target_qty_delta 应为 0，实际 {ev.target_qty_delta}"
        # 无任何 BUY/SELL
        all_actions = {e.action for e in ev_list}
        assert not (all_actions & {"BUY", "SELL"}), f"DATA_BLOCKED 时禁止 BUY/SELL，实际 {all_actions}"
        # blocking_reason 有留痕
        assert ev.blocking_reason is not None
        assert "DATA_BLOCKED" in ev.blocking_reason
        assert "symbol_id=42" in ev.blocking_reason

    # ------------------------------------------------------------------
    # T-A7-C2：绝不猜价（executed_price=NULL）
    # ------------------------------------------------------------------
    def test_T_A7_C2_executed_price_null_no_fallback(self):
        """T-A7-C2：DATA_BLOCKED 时 executed_price 必须是 None，
        绝不等于前一日收盘价(10.0)或行业均价(9.5)或任何 fallback"""
        from app.services.decision_engine import _default_build_evidence
        symbol_id = 42
        # 构造：allocator 给出了 executed_price=10.05（基于前收 10.0 计算）
        # mock 场景：前一日收盘价=10.0，行业均价=9.5 —— 这些在 allocator exec_price 里
        snap, clock, universe, scored, signal, alloc = self._make_evidence_inputs(
            symbol_id=symbol_id,
            alloc_direction="BUY",
            alloc_executed_price=10.05,  # 前收 10.0 + 滑点
            alloc_target_qty=1500.0,
        )
        # 但第 2 日价格全缺 → DATA_BLOCKED
        price_data = {
            symbol_id: {
                "open_price": None, "close_price": None,
                "high_price": None, "low_price": None,
                "volume": None, "is_suspended_today": False,
            }
        }
        ev_list = _default_build_evidence(
            snap=snap, clock=clock, universe=universe, scored=scored,
            signal=signal, alloc=alloc, blocking_status="READY",
            blocking_reasons=[],
            price_data_by_symbol=price_data,
            trade_date=date(2025, 1, 7),
        )
        ev = ev_list[0]
        # ── 关键断言：executed_price 显式 NULL
        assert ev.executed_price is None, (
            f"DATA_BLOCKED 时 executed_price 必须是 NULL，实际 {ev.executed_price}"
        )
        # 不等于前一日收盘价（mock 前收=10.0）
        assert ev.executed_price != 10.0, "禁止使用前一日收盘价兜底"
        # 不等于行业均价（mock 行业均价=9.5）
        assert ev.executed_price != 9.5, "禁止使用行业均价兜底"
        # 不等于 allocator 的 fallback 价（10.05）
        assert ev.executed_price != 10.05, "禁止继承 allocator 执行价 fallback"
        # 不等于默认 0.0
        assert ev.executed_price != 0.0, "禁止默认价 0.0 兜底"
        # intended_price 也应为 None
        assert ev.intended_price is None, "DATA_BLOCKED 时 intended_price 也应为 NULL"

    # ------------------------------------------------------------------
    # T-A7-C3：portfolio 持仓保持不变
    # ------------------------------------------------------------------
    def test_T_A7_C3_portfolio_position_unchanged(self):
        """T-A7-C3：第 1 日结束持仓 1000 股；DATA_BLOCKED 后仍为 1000 股"""
        from app.services.decision_engine import _default_build_evidence
        symbol_id = 42
        initial_qty = 1000.0
        initial_pct = 0.1
        # 即使 allocator 想调到 1500 股，DATA_BLOCKED 也必须维持 1000
        snap, clock, universe, scored, signal, alloc = self._make_evidence_inputs(
            symbol_id=symbol_id,
            current_qty=initial_qty,
            current_pct=initial_pct,
            alloc_direction="BUY",
            alloc_target_qty=1500.0,  # 意图买入加仓
        )
        price_data = {
            symbol_id: {
                "open_price": None, "close_price": None,
                "high_price": None, "low_price": None,
                "volume": None, "is_suspended_today": False,
            }
        }
        ev_list = _default_build_evidence(
            snap=snap, clock=clock, universe=universe, scored=scored,
            signal=signal, alloc=alloc, blocking_status="READY",
            blocking_reasons=[],
            price_data_by_symbol=price_data,
            trade_date=date(2025, 1, 7),
        )
        ev = ev_list[0]
        # target_quantity 保持初始持仓（零调仓 = 持仓不变）
        assert ev.target_quantity == initial_qty, (
            f"DATA_BLOCKED 时 target_quantity 应保持 {initial_qty}，实际 {ev.target_quantity}"
        )
        assert ev.target_qty_delta == 0.0, (
            f"DATA_BLOCKED 时 target_qty_delta 应=0，实际 {ev.target_qty_delta}"
        )
        # target_position_pct 也保持不变
        assert ev.target_position_pct == initial_pct, (
            f"target_position_pct 应保持 {initial_pct}，实际 {ev.target_position_pct}"
        )

    # ------------------------------------------------------------------
    # B0 控制组：价格正常时 → 正常 BUY/SELL，executed_price 使用正常计算值
    # ------------------------------------------------------------------
    def test_T_A7_B0_control_group_normal_prices_buy(self):
        """B0 控制组：价格正常 → action=BUY, executed_price 含统一 5bps 滑点"""
        from app.services.decision_engine import _default_build_evidence
        symbol_id = 42
        expected_exec_price = 10.05
        snap, clock, universe, scored, signal, alloc = self._make_evidence_inputs(
            symbol_id=symbol_id,
            alloc_direction="BUY",
            alloc_executed_price=expected_exec_price,
            alloc_target_qty=1500.0,
            current_qty=1000.0,
        )
        price_data = {
            symbol_id: {
                "open_price": 10.2, "close_price": 10.3,
                "high_price": 10.5, "low_price": 10.0,
                "volume": 1000, "is_suspended_today": False,
            }
        }
        ev_list = _default_build_evidence(
            snap=snap, clock=clock, universe=universe, scored=scored,
            signal=signal, alloc=alloc, blocking_status="READY",
            blocking_reasons=[],
            price_data_by_symbol=price_data,
            trade_date=date(2025, 1, 7),
        )
        ev = ev_list[0]
        assert ev.action == "BUY", f"控制组应正常 BUY，实际 {ev.action}"
        assert ev.action_subtype is None or "DATA_BLOCKED" not in str(ev.action_subtype)
        # 执行价正常使用
        expected_filled_price = round(expected_exec_price * 1.0005, 6)
        assert ev.executed_price == expected_filled_price, (
            f"正常情况 executed_price 应为 {expected_filled_price}，实际 {ev.executed_price}"
        )
        assert ev.executed_price is not None
        # target_qty_delta 正常非零（1500 - 1000 = 500）
        assert ev.target_qty_delta == 500.0, (
            f"正常情况 target_qty_delta 应为 500，实际 {ev.target_qty_delta}"
        )
        # blocking_reason 应为 None
        assert ev.blocking_reason is None, "正常情况 blocking_reason 应为 None"

    def test_suspended_subtype_different_from_data_blocked(self):
        """SUSPENDED vs DATA_BLOCKED：action_subtype 不同，方便区分交易所停牌 vs 数据源问题"""
        from app.services.decision_engine import _default_build_evidence
        symbol_id = 42
        snap, clock, universe, scored, signal, alloc = self._make_evidence_inputs(
            symbol_id=symbol_id, alloc_direction="SELL",
        )
        # 场景：法定停牌（is_suspended_today=True）
        price_data_suspended = {
            symbol_id: {
                "open_price": None, "close_price": None,
                "high_price": None, "low_price": None,
                "volume": None, "is_suspended_today": True,
            }
        }
        ev_list = _default_build_evidence(
            snap=snap, clock=clock, universe=universe, scored=scored,
            signal=signal, alloc=alloc, blocking_status="READY",
            blocking_reasons=[],
            price_data_by_symbol=price_data_suspended,
            trade_date=date(2025, 1, 7),
        )
        ev_sus = ev_list[0]
        assert ev_sus.action == "HOLD"
        assert ev_sus.action_subtype == "SUSPENDED", "法定停牌 subtype 应是 SUSPENDED"
        assert ev_sus.executed_price is None
        assert ev_sus.target_qty_delta == 0.0
        assert ev_sus.blocking_reason is not None and "法定停牌" in ev_sus.blocking_reason

        # 对比 DATA_BLOCKED
        price_data_blocked = {
            symbol_id: {
                "open_price": None, "close_price": None,
                "high_price": None, "low_price": None,
                "volume": None, "is_suspended_today": False,
            }
        }
        ev_list2 = _default_build_evidence(
            snap=snap, clock=clock, universe=universe, scored=scored,
            signal=signal, alloc=alloc, blocking_status="READY",
            blocking_reasons=[],
            price_data_by_symbol=price_data_blocked,
            trade_date=date(2025, 1, 7),
        )
        ev_blk = ev_list2[0]
        assert ev_blk.action_subtype == "DATA_BLOCKED"
        # 两者 subtype 不同
        assert ev_sus.action_subtype != ev_blk.action_subtype


# ======================================================================
# 集成测试：走完整 DecisionEngine.evaluate 流水线
# ======================================================================
class TestFullPipelineDataBlocked:
    def test_full_pipeline_evaluate_with_price_data(self, tmp_alembic_db):
        """集成：完整 DecisionEngine.evaluate + price_data_by_symbol → DATA_BLOCKED"""
        from app.services.decision_engine import evaluate
        db = tmp_alembic_db
        p = _make_portfolio(db)
        _set_active_model(db, "fmr_A7_FULL_01")
        s1 = _make_symbol(db, "A7S1", "A7 Stock 1")
        s2 = _make_symbol(db, "A7S2", "A7 Stock 2")

        from app.models.portfolio_member import PortfolioMember
        for s in (s1, s2):
            db.add(PortfolioMember(portfolio_id=p.id, symbol_id=s.id, status="active"))

        _insert_scores(db, "fmr_A7_FULL_01", [
            (s1.id, date(2025, 1, 7), 0.85, datetime(2025, 1, 7, 6, 59, 0)),
            (s2.id, date(2025, 1, 7), 0.75, datetime(2025, 1, 7, 6, 59, 0)),
        ])

        resp = _save_and_apply(db, p.id, "fmr_A7_FULL_01", run_mode="research")
        db.commit()

        # 第 2 日：s1 价格全缺（DATA_BLOCKED），s2 价格正常（正常动作）
        price_data = {
            s1.id: {
                "open_price": None, "close_price": None,
                "high_price": None, "low_price": None,
                "volume": None, "is_suspended_today": False,
            },
            s2.id: {
                "open_price": 10.2, "close_price": 10.3,
                "high_price": 10.5, "low_price": 10.0,
                "volume": 1000, "is_suspended_today": False,
            },
        }

        result = evaluate(
            db,
            portfolio_id=p.id,
            strategy_snapshot_id=resp.strategy_snapshot_id,
            trade_date=date(2025, 1, 7),
            run_type="research_preflight",
            dry_run=True,
            price_data_by_symbol=price_data,
        )

        ev_by_sym = {e.symbol_id: e for e in result.evidence}
        assert s1.id in ev_by_sym, "s1 证据应存在"
        assert s2.id in ev_by_sym, "s2 证据应存在"

        # s1: DATA_BLOCKED
        ev1 = ev_by_sym[s1.id]
        assert ev1.action == "HOLD"
        assert ev1.action_subtype == "DATA_BLOCKED"
        assert ev1.executed_price is None
        assert ev1.blocking_reason is not None

        # s2: 正常（因 Score 存在，默认 stub 行为，但 DATA_BLOCKED 不应影响它）
        ev2 = ev_by_sym[s2.id]
        assert ev2.action_subtype != "DATA_BLOCKED", "s2 价格正常，不应被 DATA_BLOCKED"
        assert ev2.blocking_reason is None, "s2 blocking_reason 应为 None"

    def test_no_price_data_kwarg_backward_compatible(self, tmp_alembic_db):
        """向后兼容：不传 price_data_by_symbol → 行为不变（不崩溃）"""
        from app.services.decision_engine import evaluate
        db = tmp_alembic_db
        p = _make_portfolio(db)
        _set_active_model(db, "fmr_A7_BC_01")
        s1 = _make_symbol(db, "BCK1")
        from app.models.portfolio_member import PortfolioMember
        db.add(PortfolioMember(portfolio_id=p.id, symbol_id=s1.id, status="active"))
        resp = _save_and_apply(db, p.id, "fmr_A7_BC_01", run_mode="research")
        db.commit()

        result = evaluate(
            db,
            portfolio_id=p.id,
            strategy_snapshot_id=resp.strategy_snapshot_id,
            trade_date=date(2025, 1, 7),
            run_type="research_preflight",
            dry_run=True,
            # 不传 price_data_by_symbol → 兼容旧代码路径
        )
        assert result.evidence is not None
        assert len(result.evidence) >= 1
