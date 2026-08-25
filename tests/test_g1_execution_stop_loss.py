"""T-A8 Q7.2：止损验证 Fail-Closed。

覆盖：
- T-A8-C1：今缺价 + 有持仓 + 止损线无法验证 → REJECTED/UNABLE_TO_VERIFY_STOP_LOSS
- T-A8-C2：前收已触止损 + 今日 FIRST_OPEN 有效 → 允许 SELL + 记录 FIRST_OPEN
- T-A8-C3：今日没触发止损条件 + 今日数据缺 → 走 T-A7 DATA_BLOCKED HOLD
- T-A8-B0：控制组，不触发止损场景 → 行为与 T-A7 一致（向后兼容）
- T-A8-U1~U4：check_stop_loss_verifiability 函数本身单元测试
"""
from __future__ import annotations

import os
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

os.environ.setdefault("ALEMBIC_DATABASE_URL", "")


@pytest.fixture(scope="function")
def tmp_alembic_db():
    """每个函数独立 SQLite 文件，显式跑 alembic upgrade head（与 G0 契约一致）。"""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="qa_g1_stoploss_")
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


# ======================================================================
# Unit Test: check_stop_loss_verifiability 函数本身
# ======================================================================
class TestCheckStopLossVerifiability:
    def test_t_a8_c1_data_blocked_unable_verify(self):
        """T-A8-C1：今缺价 + 有持仓 + 止损线无法验证 → REJECTED/UNABLE_TO_VERIFY_STOP_LOSS

        current_position_qty=1000, stop_loss_price=9.5, prev_close=10.0（前收在止损线上方，
        昨日没触但今日数据缺，不知道今日最低有没有跌到9.5以下），
        first_open_price=None，data_status=DATA_BLOCKED。
        """
        from app.services.decision_engine import check_stop_loss_verifiability
        result = check_stop_loss_verifiability(
            current_position_qty=1000,
            stop_loss_price=9.5,
            prev_close_price=10.0,
            first_open_price=None,
            data_status={"available": False, "status": "DATA_BLOCKED",
                         "reason": "DATA_BLOCKED test"},
        )
        assert result["should_intervene"] is True
        assert result["action"] == "REJECTED"
        assert result["action_subtype"] == "UNABLE_TO_VERIFY_STOP_LOSS"
        assert result["executed_price"] is None
        assert result["stop_loss_triggered"] is False
        assert result["stop_loss_verified_price_source"] is None
        assert "UNABLE_TO_VERIFY_STOP_LOSS" in result["reason"]

    def test_t_a8_c2_prev_close_triggered_first_open_valid(self):
        """T-A8-C2：前收已触止损 + 今日 FIRST_OPEN 有效 → 允许 SELL + 记录 FIRST_OPEN

        current_position_qty=1000, stop_loss_price=9.5, prev_close=9.3（昨日收盘已经低于9.5，
        触了但昨已过，今 FIRST_OPEN=9.2 今开盘继续低，今日可执行），
        first_open_price=9.2, data_status OK。
        """
        from app.services.decision_engine import check_stop_loss_verifiability
        result = check_stop_loss_verifiability(
            current_position_qty=1000,
            stop_loss_price=9.5,
            prev_close_price=9.3,
            first_open_price=9.2,
            data_status={"available": True, "status": "OK", "reason": ""},
        )
        assert result["should_intervene"] is True
        assert result["action"] == "SELL"
        assert result["action_subtype"] == "STOP_LOSS_EXECUTED"
        assert result["executed_price"] == 9.2
        assert result["stop_loss_verified_price_source"] == "FIRST_OPEN"
        assert result["stop_loss_triggered"] is True
        assert "FIRST_OPEN" in result["reason"]

    def test_t_a8_c3_first_open_above_stop_loss_but_data_blocked(self):
        """T-A8-C3：今日没触发止损条件 + 今日数据缺 → should_intervene=False 走 T-A7

        current_position_qty=1000, stop_loss_price=9.5, prev_close=10.0,
        今日 first_open=9.8（>9.5 今日没触发止损）但今日 close/high/low/volume=None
        → 今日 data_status=DATA_BLOCKED，但 first_open=9.8 是唯一已知价 > stop_loss。
        """
        from app.services.decision_engine import check_stop_loss_verifiability
        result = check_stop_loss_verifiability(
            current_position_qty=1000,
            stop_loss_price=9.5,
            prev_close_price=10.0,
            first_open_price=9.8,
            data_status={"available": False, "status": "DATA_BLOCKED",
                         "reason": "DATA_BLOCKED test"},
        )
        assert result["should_intervene"] is False
        assert result["action"] == "DATA_BLOCKED_PASS"
        assert result["stop_loss_triggered"] is False
        assert "first_open=9.8" in result["reason"]

    def test_t_a8_u0_step0_no_position(self):
        """U0 Step 0：无持仓 → 不介入"""
        from app.services.decision_engine import check_stop_loss_verifiability
        result = check_stop_loss_verifiability(
            current_position_qty=0,
            stop_loss_price=9.5,
            prev_close_price=10.0,
            first_open_price=None,
            data_status={"available": False, "status": "DATA_BLOCKED", "reason": ""},
        )
        assert result["should_intervene"] is False

    def test_t_a8_u0_step0_no_stop_loss(self):
        """U0 Step 0：无 stop_loss_price → 不介入"""
        from app.services.decision_engine import check_stop_loss_verifiability
        result = check_stop_loss_verifiability(
            current_position_qty=1000,
            stop_loss_price=None,
            prev_close_price=10.0,
            first_open_price=None,
            data_status={"available": False, "status": "DATA_BLOCKED", "reason": ""},
        )
        assert result["should_intervene"] is False

    def test_t_a8_u2_first_open_only_triggered(self):
        """U2：前收未触(10.0 > 9.5)，只有 FIRST_OPEN=9.0 < 9.5 触发 → SELL"""
        from app.services.decision_engine import check_stop_loss_verifiability
        result = check_stop_loss_verifiability(
            current_position_qty=1000,
            stop_loss_price=9.5,
            prev_close_price=10.0,
            first_open_price=9.0,
            data_status={"available": True, "status": "OK", "reason": ""},
        )
        assert result["should_intervene"] is True
        assert result["action"] == "SELL"
        assert result["action_subtype"] == "STOP_LOSS_EXECUTED"
        assert result["executed_price"] == 9.0
        assert result["stop_loss_verified_price_source"] == "FIRST_OPEN"
        assert result["stop_loss_triggered"] is True

    def test_t_a8_u3_prev_close_triggered_no_first_open(self):
        """U3：前收触 9.3 <= 9.5，FIRST_OPEN=None → 用 PREV_CLOSE 9.3 作为 executed_price，
        price_source 为 None (Q7.2 主强调 FIRST_OPEN，但 PREV_CLOSE 仍兼容)。"""
        from app.services.decision_engine import check_stop_loss_verifiability
        result = check_stop_loss_verifiability(
            current_position_qty=1000,
            stop_loss_price=9.5,
            prev_close_price=9.3,
            first_open_price=None,
            data_status={"available": True, "status": "OK", "reason": ""},
        )
        assert result["should_intervene"] is True
        assert result["action"] == "SELL"
        assert result["action_subtype"] == "STOP_LOSS_EXECUTED"
        assert result["executed_price"] == 9.3
        assert result["stop_loss_verified_price_source"] is None
        assert result["stop_loss_triggered"] is True

    def test_t_a8_u4_step4_no_trigger_ok_data(self):
        """U4 Step 4：正常数据 + 未触发止损 → 不介入"""
        from app.services.decision_engine import check_stop_loss_verifiability
        result = check_stop_loss_verifiability(
            current_position_qty=1000,
            stop_loss_price=9.5,
            prev_close_price=10.0,
            first_open_price=10.2,
            data_status={"available": True, "status": "OK", "reason": ""},
        )
        assert result["should_intervene"] is False
        assert result["action"] == "DATA_BLOCKED_PASS"
        assert result["stop_loss_triggered"] is False


# ======================================================================
# Integration Test: build_evidence 全链路接入止损
# ======================================================================
class TestBuildEvidenceStopLoss:
    def _make_evidence_build_args(self, tmp_db, symbol_id: int = 1):
        """构造最小化 build_evidence 输入（复用 T-A7 模式）。"""
        from app.services.decision_engine import (
            LoadedSnapshot, UniverseAndEligibility, ScoredUniverse,
            SignalResult, RiskAllocationResult, ResolvedClock,
        )
        snap = LoadedSnapshot(
            snapshot=None,  # type: ignore
            factor_model_run_id="qa_sl_001",
            factor_set_id=None,
            rule_id=None,
            rule_version=None,
            members=[{"symbol_id": symbol_id, "current_quantity": 1000,
                      "current_position_pct": 0.3}],
            candidate_pool=[],
            cost_config={"min_lot_size": 100, "slippage_buy_bps": 5.0,
                         "slippage_sell_bps": 5.0},
        )
        td = date(2026, 8, 15)
        clock = ResolvedClock(
            decision_at=datetime(2026, 8, 15, 7, 5, 0),
            data_cutoff_at=datetime(2026, 8, 15, 7, 0, 0),
            execution_at=datetime(2026, 8, 16, 1, 30, 0),
        )
        universe = UniverseAndEligibility(
            universe=[{"symbol_id": symbol_id, "current_quantity": 1000,
                       "current_position_pct": 0.3}],
            universe_count=1, member_count=1,
        )
        scored = ScoredUniverse(items=[{"symbol_id": symbol_id, "score_value": 80.0,
                                        "score_rank": 1, "published_at": clock.data_cutoff_at}],
                                expected=1, actual=1, coverage_pct=100.0)
        signal = SignalResult(items=[{"symbol_id": symbol_id, "direction": "HOLD"}])
        alloc = RiskAllocationResult(items=[{
            "symbol_id": symbol_id, "direction": "HOLD",
            "target_position_pct": 0.3, "target_quantity": 1000,
            "min_lot_size": 100, "clamp_steps": [],
        }])
        return dict(
            snap=snap, clock=clock, universe=universe, scored=scored,
            signal=signal, alloc=alloc, blocking_status="READY",
            blocking_reasons=[], trade_date=td,
        )

    def test_t_a8_c1_build_evidence_rejected(self):
        """T-A8-C1（build_evidence 链路）：今缺价 + 有止损线 → REJECTED，持仓不变。"""
        from app.services.decision_engine import _default_build_evidence
        args = self._make_evidence_build_args(None, symbol_id=1)
        args["price_data_by_symbol"] = {
            1: {
                "open_price": None, "close_price": None,
                "high_price": None, "low_price": None,
                "volume": None, "is_suspended_today": False,
                "stop_loss_price": 9.5,
                "prev_close_price": 10.0,
                "first_open_price": None,
            }
        }
        ev = _default_build_evidence(**args)
        assert len(ev) == 1
        e = ev[0]
        assert e.action == "REJECTED"
        assert e.action_subtype == "UNABLE_TO_VERIFY_STOP_LOSS"
        assert e.target_qty_delta == 0
        assert e.target_quantity == 1000
        assert e.executed_price is None
        assert e.stop_loss_triggered is False
        assert e.stop_loss_verified_price_source is None

    def test_t_a8_c2_build_evidence_sell_first_open(self):
        """T-A8-C2（build_evidence 链路）：前收已触 + FIRST_OPEN 有效 → SELL 全清。"""
        from app.services.decision_engine import _default_build_evidence
        args = self._make_evidence_build_args(None, symbol_id=1)
        args["price_data_by_symbol"] = {
            1: {
                "open_price": None, "close_price": None,
                "high_price": None, "low_price": None,
                "volume": None, "is_suspended_today": False,
                "stop_loss_price": 9.5,
                "prev_close_price": 9.3,
                "first_open_price": 9.2,
            }
        }
        ev = _default_build_evidence(**args)
        assert len(ev) == 1
        e = ev[0]
        assert e.action == "SELL"
        assert e.action_subtype == "STOP_LOSS_EXECUTED"
        assert e.executed_price == 9.2
        assert e.stop_loss_verified_price_source == "FIRST_OPEN"
        assert e.stop_loss_triggered is True
        assert e.target_quantity == 0.0
        assert e.target_qty_delta == -1000.0

    def test_t_a8_c3_build_evidence_data_blocked_hold(self):
        """T-A8-C3（build_evidence 链路）：first_open > stop_loss 但其余缺 → HOLD/DATA_BLOCKED。"""
        from app.services.decision_engine import _default_build_evidence
        args = self._make_evidence_build_args(None, symbol_id=1)
        args["price_data_by_symbol"] = {
            1: {
                "open_price": None, "close_price": None,
                "high_price": None, "low_price": None,
                "volume": None, "is_suspended_today": False,
                "stop_loss_price": 9.5,
                "prev_close_price": 10.0,
                "first_open_price": 9.8,  # > 9.5，明确今日未触止损
            }
        }
        ev = _default_build_evidence(**args)
        assert len(ev) == 1
        e = ev[0]
        # C3 分支：止损验证 should_intervene=False → 走 T-A7 DATA_BLOCKED HOLD
        assert e.action == "HOLD"
        assert e.action_subtype == "DATA_BLOCKED"
        assert e.target_qty_delta == 0
        assert e.target_quantity == 1000
        assert e.stop_loss_triggered is False
        assert e.stop_loss_verified_price_source is None

    def test_t_a8_b0_backward_compat_no_stop_loss_price(self):
        """T-A8-B0：不传 stop_loss_price → 行为与 T-A7 改造前一致。

        价格正常 + 无 stop_loss_price → 走正常 HOLD 分支（DATA_BLOCKED 走 HOLD）。
        """
        from app.services.decision_engine import _default_build_evidence
        args = self._make_evidence_build_args(None, symbol_id=1)
        # 价格全缺，但 stop_loss_price 未传 → 止损验证 Step0 不介入 → 走 T-A7 HOLD
        args["price_data_by_symbol"] = {
            1: {
                "open_price": None, "close_price": None,
                "high_price": None, "low_price": None,
                "volume": None, "is_suspended_today": False,
                # 注意：故意不传入 stop_loss_price → 触发 Step0 不介入
                "prev_close_price": 10.0,
            }
        }
        ev = _default_build_evidence(**args)
        assert len(ev) == 1
        e = ev[0]
        assert e.action == "HOLD"
        assert e.action_subtype == "DATA_BLOCKED"
        assert e.target_qty_delta == 0
        assert e.executed_price is None
        assert e.stop_loss_triggered is False

    def test_t_a8_b0_backward_compat_price_ok_no_stoploss(self):
        """T-A8-B0-2：价格正常 + 无 stop_loss_price → 走原 signal HOLD 分支。"""
        from app.services.decision_engine import _default_build_evidence
        args = self._make_evidence_build_args(None, symbol_id=1)
        args["price_data_by_symbol"] = {
            1: {
                "open_price": 10.1, "close_price": 10.3,
                "high_price": 10.5, "low_price": 10.0,
                "volume": 50000, "is_suspended_today": False,
            }
        }
        ev = _default_build_evidence(**args)
        assert len(ev) == 1
        e = ev[0]
        assert e.action == "HOLD"
        # signal 方向 HOLD → subtype 不应是 DATA_BLOCKED
        assert e.action_subtype != "DATA_BLOCKED"
        assert e.stop_loss_triggered is False
        assert e.stop_loss_verified_price_source is None
