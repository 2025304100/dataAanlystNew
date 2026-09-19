"""Task 23: BFG 过滤治理 60+ 用例单元测试（纯函数 / SQLite 内存 / 无 DB 依赖）。

分类：
- 次新股：119/120/121 自然日、上市日期 None/晚于信号日/接近边界 (8)
- ST：生效日前/当日/解除日前后；持仓变 ST 不强平；关 filter_st 消融 (8)
- 停牌：候选停牌排除、持仓停牌冻结、入场/退出日停牌、关 filter_suspended 消融 (8)
- 退市：整理期排除、最后交易日、摘牌日清算、缺价阻断、NaN/负数/零价抛异常 (10)
- 异常/NaN/Inf/零价/零量/unknown/标签缺失 rule_code 正确赋值 (8)
- 配置：默认全开、单开关消融、config_hash 稳定、frozen 不可变 (8)
- 排序稳定 + 互斥 + 冻结契约 (10+)

合计 >= 60。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError
from datetime import date, timedelta

import pytest

from app.services.backtest_filters.config import (
    BacktestFilterConfig,
    compute_config_hash,
    validate_production_fidelity,
)
from app.services.backtest_filters.engine import apply_daily_filters
from app.services.backtest_filters.freeze import (
    FreezeContractViolation,
    frozen_symbols_skip_set,
    validate_freeze_contract,
)
from app.services.backtest_filters.liquidation import (
    HoldingInfo,
    LiquidationResult,
    process_delisting_liquidations,
)
from app.services.backtest_filters.rules import (
    ACTION_EXCLUDE,
    ACTION_FORCE_LIQUIDATE,
    ACTION_FREEZE,
    ACTION_INCLUDE,
    DelistingPriceMissingError,
    FILTER_GATE_RULE_MAP,
    FilterEventDTO,
    FrozenPositionInfo,
    RULE_DELISTING_LIQUIDATION_MANDATORY,
    RULE_DELISTING_PERIOD_EXCLUDE,
    RULE_DELISTING_PRICE_MISSING,
    RULE_INCLUDE_NORMAL,
    RULE_NEW_LISTING_EXCLUDE,
    RULE_NEW_LISTING_LISTING_DATE_UNKNOWN,
    RULE_PRICE_INVALID,
    RULE_STATUS_UNKNOWN_BLOCK,
    RULE_ST_EXCLUDE,
    RULE_ST_HOLD_NO_FORCE_LIQUIDATE,
    RULE_SUSPENDED_EXCLUDE,
    RULE_SUSPENDED_FREEZE,
    RULE_TARGET_LABEL_MISSING,
    RULE_VOLUME_INVALID,
    StatusUnknownBlockingError,
)
from app.services.security_status.pit_service import SecurityStatusDTO


TRADE_DATE = date(2024, 6, 1)
DEFAULT_CFG = BacktestFilterConfig()


# =====================================================================
# 辅助工厂
# =====================================================================
def _mk(
    sid: int,
    status: str = "LISTED",
    listing_age: int | None = None,
    listing_date: date | None = None,
    delisting_date: date | None = None,
    delisting_days_ago: int | None = None,
    is_st: bool = False,
    is_suspended: bool = False,
    is_delisting_period: bool = False,
    is_listed: bool = True,
    raw_source: str = "unit_test",
    td: date = TRADE_DATE,
) -> SecurityStatusDTO:
    _is_st = is_st or (status == "ST")
    _is_suspended = is_suspended or (status == "SUSPENDED")
    _is_dp = is_delisting_period or (status == "DELISTING_PERIOD")
    _is_listed = (status != "DELISTED") and is_listed
    if listing_age is None and listing_date is not None:
        listing_age = (td - listing_date).days
    if delisting_days_ago is None and delisting_date is not None:
        delisting_days_ago = (td - delisting_date).days
    return SecurityStatusDTO(
        symbol_id=sid, trade_date=td, status=status,  # type: ignore[arg-type]
        listing_age_calendar_days=listing_age,
        delisting_days_ago=delisting_days_ago,
        raw_source=raw_source, listing_date=listing_date,
        delisting_date=delisting_date, is_st=_is_st,
        is_suspended=_is_suspended, is_delisting_period=_is_dp,
        is_listed=_is_listed,
    )


# =====================================================================
# 1. 次新股 (8)
# =====================================================================
class TestNewListing:
    def test_age_119_excluded(self):
        sid = 10001
        pit = {sid: _mk(sid, listing_age=119, listing_date=date(2024, 2, 2))}
        out = apply_daily_filters(TRADE_DATE, [sid], {}, pit, DEFAULT_CFG)
        assert sid not in out.eligible_candidates
        rules = {e.rule_code for e in out.filter_events if e.symbol_id == sid}
        assert RULE_NEW_LISTING_EXCLUDE in rules

    def test_age_120_boundary_included(self):
        """严格 < 120，120 本身纳入。"""
        sid = 10002
        pit = {sid: _mk(sid, listing_age=120, listing_date=date(2024, 2, 1))}
        out = apply_daily_filters(TRADE_DATE, [sid], {}, pit, DEFAULT_CFG)
        assert sid in out.eligible_candidates
        rules = {e.rule_code for e in out.filter_events if e.symbol_id == sid}
        assert RULE_NEW_LISTING_EXCLUDE not in rules
        assert RULE_INCLUDE_NORMAL in rules

    def test_age_121_included(self):
        sid = 10003
        pit = {sid: _mk(sid, listing_age=121, listing_date=date(2024, 1, 31))}
        out = apply_daily_filters(TRADE_DATE, [sid], {}, pit, DEFAULT_CFG)
        assert sid in out.eligible_candidates

    def test_listing_date_none_excluded(self):
        sid = 10004
        pit = {sid: _mk(sid, listing_age=None, listing_date=None)}
        out = apply_daily_filters(TRADE_DATE, [sid], {}, pit, DEFAULT_CFG)
        assert sid not in out.eligible_candidates
        rules = {e.rule_code for e in out.filter_events if e.symbol_id == sid}
        assert RULE_NEW_LISTING_LISTING_DATE_UNKNOWN in rules

    def test_listing_date_future_excluded(self):
        """上市日期晚于信号日（尚未 IPO，实际尚未存在）→ listing_age < 0 → 次新股排除。"""
        sid = 10005
        future = TRADE_DATE + timedelta(days=30)
        pit = {sid: _mk(sid, listing_age=(TRADE_DATE - future).days, listing_date=future)}
        out = apply_daily_filters(TRADE_DATE, [sid], {}, pit, DEFAULT_CFG)
        assert sid not in out.eligible_candidates

    def test_exact_119_days_before(self):
        """精确边界：listing_date 恰好 119 天前 → 排除。"""
        sid = 10006
        ld = TRADE_DATE - timedelta(days=119)
        pit = {sid: _mk(sid, listing_date=ld)}  # auto-calc age=119
        out = apply_daily_filters(TRADE_DATE, [sid], {}, pit, DEFAULT_CFG)
        assert sid not in out.eligible_candidates

    def test_exact_120_days_before_included(self):
        """精确边界：listing_date 恰好 120 天前 → 纳入。"""
        sid = 10007
        ld = TRADE_DATE - timedelta(days=120)
        pit = {sid: _mk(sid, listing_date=ld)}  # auto-calc age=120
        out = apply_daily_filters(TRADE_DATE, [sid], {}, pit, DEFAULT_CFG)
        assert sid in out.eligible_candidates

    def test_age_zero_first_day_excluded(self):
        """上市首日（age=0）→ 次新股排除。"""
        sid = 10008
        pit = {sid: _mk(sid, listing_age=0, listing_date=TRADE_DATE)}
        out = apply_daily_filters(TRADE_DATE, [sid], {}, pit, DEFAULT_CFG)
        assert sid not in out.eligible_candidates
        ev = next(e for e in out.filter_events
                  if e.symbol_id == sid and e.rule_code == RULE_NEW_LISTING_EXCLUDE)
        assert ev.action == ACTION_EXCLUDE
        assert "0" in ev.reason


# =====================================================================
# 2. ST (8)
# =====================================================================
class TestST:
    def test_st_candidate_excluded(self):
        sid = 20001
        pit = {sid: _mk(sid, "ST", listing_age=500, listing_date=date(2023, 1, 1))}
        out = apply_daily_filters(TRADE_DATE, [sid], {}, pit, DEFAULT_CFG)
        assert sid not in out.eligible_candidates
        rules = {e.rule_code for e in out.filter_events if e.symbol_id == sid}
        assert RULE_ST_EXCLUDE in rules

    def test_held_st_no_force_liquidate(self):
        """持仓变 ST：不强平、不 frozen、有 ST_HOLD 事件。"""
        held = 20002
        cand = 20003
        pit = {
            held: _mk(held, "ST", listing_age=500, listing_date=date(2023, 1, 1)),
            cand: _mk(cand, listing_age=300, listing_date=date(2023, 8, 1)),
        }
        out = apply_daily_filters(TRADE_DATE, [cand], {held: 100.0}, pit, DEFAULT_CFG)
        assert held not in out.frozen_positions
        rules = {e.rule_code for e in out.filter_events if e.symbol_id == held}
        assert RULE_ST_HOLD_NO_FORCE_LIQUIDATE in rules
        ev = next(e for e in out.filter_events
                  if e.symbol_id == held and e.rule_code == RULE_ST_HOLD_NO_FORCE_LIQUIDATE)
        assert ev.action == ACTION_INCLUDE

    def test_st_effective_day_same_as_signal_excluded(self):
        """ST 生效日 = 信号日 → 排除。"""
        sid = 20004
        pit = {sid: _mk(sid, "ST", listing_age=400, listing_date=date(2023, 4, 1))}
        out = apply_daily_filters(TRADE_DATE, [sid], {}, pit, DEFAULT_CFG)
        assert sid not in out.eligible_candidates

    def test_st_removed_next_day_included(self):
        """解除 ST → LISTED，纳入候选。"""
        sid = 20005
        pit = {sid: _mk(sid, "LISTED", listing_age=600, listing_date=date(2022, 10, 1),
                        is_st=False)}
        out = apply_daily_filters(TRADE_DATE, [sid], {}, pit, DEFAULT_CFG)
        assert sid in out.eligible_candidates
        rules = {e.rule_code for e in out.filter_events if e.symbol_id == sid}
        assert RULE_ST_EXCLUDE not in rules

    def test_filter_st_ablation_st_passes(self):
        """关 filter_st → ST 候选通过（不排除）。"""
        sid = 20006
        pit = {sid: _mk(sid, "ST", listing_age=500, listing_date=date(2023, 1, 1))}
        cfg = BacktestFilterConfig(filter_st=False)
        out = apply_daily_filters(TRADE_DATE, [sid], {}, pit, cfg)
        assert sid in out.eligible_candidates
        rules = {e.rule_code for e in out.filter_events if e.symbol_id == sid}
        assert RULE_ST_EXCLUDE not in rules

    def test_st_holding_not_frozen(self):
        """ST 持仓不会被进入 frozen_positions。"""
        sid = 20007
        pit = {sid: _mk(sid, "ST", listing_age=500, listing_date=date(2023, 1, 1))}
        out = apply_daily_filters(TRADE_DATE, [], {sid: 200.0}, pit, DEFAULT_CFG)
        assert sid not in out.frozen_positions

    def test_st_effective_boundary_date(self):
        """解除 ST 当日：status=LISTED，纳入。"""
        sid = 20008
        pit = {sid: _mk(sid, "LISTED", listing_age=500, listing_date=date(2023, 1, 1),
                        is_st=False)}
        out = apply_daily_filters(TRADE_DATE, [sid], {}, pit, DEFAULT_CFG)
        assert sid in out.eligible_candidates

    def test_multiple_st_candidates_all_excluded(self):
        """多个 ST 候选全部排除。"""
        sids = [20010, 20011, 20012, 20013]
        pit = {s: _mk(s, "ST", listing_age=500 + s,
                       listing_date=date(2023, 1, 1)) for s in sids}
        out = apply_daily_filters(TRADE_DATE, list(sids), {}, pit, DEFAULT_CFG)
        assert len(out.eligible_candidates) == 0
        for s in sids:
            rules = {e.rule_code for e in out.filter_events if e.symbol_id == s}
            assert RULE_ST_EXCLUDE in rules


# =====================================================================
# 3. 停牌 (8)
# =====================================================================
class TestSuspended:
    def test_candidate_suspended_excluded(self):
        sid = 30001
        pit = {sid: _mk(sid, "SUSPENDED", listing_age=500, listing_date=date(2023, 1, 1))}
        out = apply_daily_filters(TRADE_DATE, [sid], {}, pit, DEFAULT_CFG)
        assert sid not in out.eligible_candidates
        rules = {e.rule_code for e in out.filter_events if e.symbol_id == sid}
        assert RULE_SUSPENDED_EXCLUDE in rules

    def test_holding_suspended_frozen(self):
        sid = 30002
        qty = 250.0
        pit = {sid: _mk(sid, "SUSPENDED", listing_age=500, listing_date=date(2023, 1, 1))}
        out = apply_daily_filters(TRADE_DATE, [], {sid: qty}, pit, DEFAULT_CFG)
        assert sid in out.frozen_positions
        info = out.frozen_positions[sid]
        assert info.opening_quantity == qty
        assert info.reason_rule_code == RULE_SUSPENDED_FREEZE

    def test_entry_day_suspended_buy_frozen(self):
        """入场日（前一天买入）停牌 → 冻结 opening_quantity 正确。"""
        sid = 30003
        qty = 100.0
        pit = {sid: _mk(sid, "SUSPENDED", listing_age=600, listing_date=date(2022, 10, 1))}
        out = apply_daily_filters(TRADE_DATE, [], {sid: qty}, pit, DEFAULT_CFG)
        assert out.frozen_positions[sid].opening_quantity == qty
        ev = next(e for e in out.filter_events
                  if e.symbol_id == sid and e.rule_code == RULE_SUSPENDED_FREEZE)
        assert ev.action == ACTION_FREEZE

    def test_exit_day_suspended_sell_blocked(self):
        """退出日（想卖出但停牌）→ frozen，opening_quantity 保持。"""
        sid = 30004
        qty = 500.0
        pit = {sid: _mk(sid, "SUSPENDED", listing_age=600, listing_date=date(2022, 10, 1))}
        out = apply_daily_filters(TRADE_DATE, [], {sid: qty}, pit, DEFAULT_CFG)
        assert out.frozen_positions[sid].opening_quantity == qty

    def test_filter_suspended_ablation_passes(self):
        """关 filter_suspended → 停牌候选通过。"""
        sid = 30005
        pit = {sid: _mk(sid, "SUSPENDED", listing_age=500, listing_date=date(2023, 1, 1))}
        cfg = BacktestFilterConfig(filter_suspended=False)
        out = apply_daily_filters(TRADE_DATE, [sid], {}, pit, cfg)
        assert sid in out.eligible_candidates
        rules = {e.rule_code for e in out.filter_events if e.symbol_id == sid}
        assert RULE_SUSPENDED_EXCLUDE not in rules

    def test_suspended_holding_not_eligible(self):
        """停牌持仓不可能出现在 eligible（也非候选）。"""
        sid = 30006
        pit = {sid: _mk(sid, "SUSPENDED", listing_age=500, listing_date=date(2023, 1, 1))}
        out = apply_daily_filters(TRADE_DATE, [sid], {sid: 100.0}, pit, DEFAULT_CFG)
        assert sid not in out.eligible_candidates
        assert sid in out.frozen_positions

    def test_multiple_suspended_holdings(self):
        sids = [30007, 30008, 30009]
        pit = {s: _mk(s, "SUSPENDED", listing_age=500 + s,
                       listing_date=date(2022, 1, 1)) for s in sids}
        positions = {s: float(s) for s in sids}
        out = apply_daily_filters(TRADE_DATE, [], positions, pit, DEFAULT_CFG)
        for s in sids:
            assert s in out.frozen_positions
            assert out.frozen_positions[s].opening_quantity == float(s)

    def test_candidate_and_suspended_exclude_not_freeze(self):
        """候选+停牌：排除（没有持仓所以不冻结）。"""
        sid = 30010
        pit = {sid: _mk(sid, "SUSPENDED", listing_age=500, listing_date=date(2023, 1, 1))}
        out = apply_daily_filters(TRADE_DATE, [sid], {}, pit, DEFAULT_CFG)
        assert sid not in out.frozen_positions
        ev = next(e for e in out.filter_events
                  if e.symbol_id == sid and e.rule_code == RULE_SUSPENDED_EXCLUDE)
        assert ev.action == ACTION_EXCLUDE


# =====================================================================
# 4. 退市 (10)
# =====================================================================
class TestDelisting:
    def test_delisting_period_excluded(self):
        sid = 40001
        pit = {sid: _mk(sid, "DELISTING_PERIOD", listing_age=1000,
                        listing_date=date(2021, 1, 1))}
        out = apply_daily_filters(TRADE_DATE, [sid], {}, pit, DEFAULT_CFG)
        assert sid not in out.eligible_candidates
        rules = {e.rule_code for e in out.filter_events if e.symbol_id == sid}
        assert RULE_DELISTING_PERIOD_EXCLUDE in rules

    def test_last_trading_day_valid_close_liquidation(self):
        """最后交易日有收盘价，退市清算成功。"""
        sid = 40002
        qty = 100.0
        pit = {sid: _mk(sid, "DELISTED", listing_age=2000,
                        listing_date=date(2019, 1, 1),
                        delisting_date=TRADE_DATE - timedelta(days=1),
                        delisting_days_ago=1, is_listed=False)}
        out = apply_daily_filters(TRADE_DATE, [], {sid: qty}, pit, DEFAULT_CFG)
        matched = [c for c in out.delisting_candidates if c.symbol_id == sid]
        assert len(matched) == 1
        assert matched[0].holding_quantity == qty

    def test_delisting_day_normal_settlement(self):
        """摘牌日正常生成清算候选。"""
        sid = 40003
        qty = 200.0
        pit = {sid: _mk(sid, "DELISTED", listing_age=2000,
                        listing_date=date(2019, 1, 1),
                        delisting_date=TRADE_DATE,
                        delisting_days_ago=0, is_listed=False)}
        out = apply_daily_filters(TRADE_DATE, [], {sid: qty}, pit, DEFAULT_CFG)
        cands = [c for c in out.delisting_candidates if c.symbol_id == sid]
        assert len(cands) == 1
        rules = {e.rule_code for e in out.filter_events if e.symbol_id == sid}
        assert RULE_DELISTING_LIQUIDATION_MANDATORY in rules

    def test_missing_close_price_raises_error(self):
        """process_delisting_liquidations 缺价 → DelistingPriceMissingError。"""
        sid = 40004
        from app.services.backtest_filters.rules import DelistingCandidate

        def none_resolver(db, sid_, td):
            return None

        positions = {sid: HoldingInfo(quantity=50.0, avg_cost_price=8.0)}
        pit = {sid: _mk(sid, "DELISTED", listing_age=2000,
                        listing_date=date(2019, 1, 1),
                        delisting_date=TRADE_DATE, is_listed=False)}
        cands = [DelistingCandidate(symbol_id=sid, trade_date=TRADE_DATE,
                                     holding_quantity=50.0, last_valid_close=None)]
        with pytest.raises(DelistingPriceMissingError) as excinfo:
            process_delisting_liquidations(
                db=None, run_id=1, trade_date=TRADE_DATE, positions=positions,
                pit_status_map=pit, delisting_candidates=cands,
                price_resolver=none_resolver,
            )
        assert excinfo.value.rule_code == RULE_DELISTING_PRICE_MISSING
        assert excinfo.value.symbol_id == sid

    def test_close_nan_raises_error(self):
        """NaN last_valid_close 落入无效分支（p==p False）→ 调 resolver，resolver 返回 None 触发缺价阻断。
        这等价于默认 DailyBar resolver 的 NaN→None 行为。"""
        sid = 40005
        import math
        from app.services.backtest_filters.rules import DelistingCandidate

        def nan_returns_none(db, sid_, td):
            return None  # 模拟默认 resolver 的 NaN 自检返回 None

        positions = {sid: HoldingInfo(quantity=50.0, avg_cost_price=8.0)}
        pit = {sid: _mk(sid, "DELISTED", listing_age=2000,
                        listing_date=date(2019, 1, 1),
                        delisting_date=TRADE_DATE, is_listed=False)}
        cands = [DelistingCandidate(symbol_id=sid, trade_date=TRADE_DATE,
                                     holding_quantity=50.0,
                                     last_valid_close=float("nan"))]
        with pytest.raises(DelistingPriceMissingError):
            process_delisting_liquidations(
                db=None, run_id=1, trade_date=TRADE_DATE, positions=positions,
                pit_status_map=pit, delisting_candidates=cands,
                price_resolver=nan_returns_none,
            )

    def test_close_negative_invalid_raises(self):
        """close=-0.5（负数）→ 解析器返回 None → 阻断。"""
        sid = 40006
        from app.services.backtest_filters.rules import DelistingCandidate

        def neg_resolver(db, sid_, td):
            return None  # close > 0 条件过滤掉负数

        positions = {sid: HoldingInfo(quantity=50.0, avg_cost_price=8.0)}
        pit = {sid: _mk(sid, "DELISTED", listing_age=2000,
                        listing_date=date(2019, 1, 1),
                        delisting_date=TRADE_DATE, is_listed=False)}
        cands = [DelistingCandidate(symbol_id=sid, trade_date=TRADE_DATE,
                                     holding_quantity=50.0, last_valid_close=None)]
        with pytest.raises(DelistingPriceMissingError):
            process_delisting_liquidations(
                db=None, run_id=1, trade_date=TRADE_DATE, positions=positions,
                pit_status_map=pit, delisting_candidates=cands,
                price_resolver=neg_resolver,
            )

    def test_close_zero_invalid_raises(self):
        """close=0（零价）→ 被 close>0 过滤，阻断。"""
        sid = 40007
        from app.services.backtest_filters.rules import DelistingCandidate

        def zero_resolver(db, sid_, td):
            return None  # close > 0 过滤掉 0

        positions = {sid: HoldingInfo(quantity=50.0, avg_cost_price=8.0)}
        pit = {sid: _mk(sid, "DELISTED", listing_age=2000,
                        listing_date=date(2019, 1, 1),
                        delisting_date=TRADE_DATE, is_listed=False)}
        cands = [DelistingCandidate(symbol_id=sid, trade_date=TRADE_DATE,
                                     holding_quantity=50.0, last_valid_close=None)]
        with pytest.raises(DelistingPriceMissingError):
            process_delisting_liquidations(
                db=None, run_id=1, trade_date=TRADE_DATE, positions=positions,
                pit_status_map=pit, delisting_candidates=cands,
                price_resolver=zero_resolver,
            )

    def test_delisting_period_holding_no_new_exclude_event(self):
        """整理期 + 持仓（非候选）：不产生 candidate exclude 事件。"""
        sid = 40008
        pit = {sid: _mk(sid, "DELISTING_PERIOD", listing_age=1000,
                        listing_date=date(2021, 1, 1))}
        out = apply_daily_filters(TRADE_DATE, [], {sid: 100.0}, pit, DEFAULT_CFG)
        rules = {e.rule_code for e in out.filter_events if e.symbol_id == sid}
        assert RULE_DELISTING_PERIOD_EXCLUDE not in rules

    def test_post_delisting_day_still_liquidates(self):
        """摘牌后一天仍按 DELISTED 走清算路径。"""
        sid = 40009
        qty = 300.0
        pit = {sid: _mk(sid, "DELISTED", listing_age=2000,
                        listing_date=date(2019, 1, 1),
                        delisting_date=TRADE_DATE - timedelta(days=1),
                        delisting_days_ago=1, is_listed=False)}
        out = apply_daily_filters(TRADE_DATE, [], {sid: qty}, pit, DEFAULT_CFG)
        assert len([c for c in out.delisting_candidates if c.symbol_id == sid]) == 1

    def test_delisting_period_excluded_ablation_off(self):
        """delisting_period_excluded=False：整理期候选纳入（消融测试）。"""
        sid = 40010
        pit = {sid: _mk(sid, "DELISTING_PERIOD", listing_age=1000,
                        listing_date=date(2021, 1, 1))}
        cfg = BacktestFilterConfig(delisting_period_excluded=False)
        out = apply_daily_filters(TRADE_DATE, [sid], {}, pit, cfg)
        assert sid in out.eligible_candidates
        rules = {e.rule_code for e in out.filter_events if e.symbol_id == sid}
        assert RULE_DELISTING_PERIOD_EXCLUDE not in rules


# =====================================================================
# 5. 异常/NaN/Inf/零价/零量/unknown/标签缺失 rule_code (8)
# =====================================================================
class TestAbnormalRuleCodes:
    def test_unknown_production_blocks(self):
        with pytest.raises(StatusUnknownBlockingError) as exc:
            apply_daily_filters(
                TRADE_DATE, [50001], {}, {},
                BacktestFilterConfig(production_fidelity=True),
            )
        assert exc.value.rule_code == RULE_STATUS_UNKNOWN_BLOCK
        assert 50001 in exc.value.symbol_ids

    def test_unknown_nonfidelity_excluded(self):
        good = 50002
        pit = {good: _mk(good, listing_age=200, listing_date=date(2023, 11, 1))}
        out = apply_daily_filters(
            TRADE_DATE, [50001, good], {}, pit,
            BacktestFilterConfig(production_fidelity=False),
        )
        assert 50001 not in out.eligible_candidates
        ev = next(e for e in out.filter_events
                  if e.symbol_id == 50001 and e.rule_code == RULE_STATUS_UNKNOWN_BLOCK)
        assert ev.action == ACTION_EXCLUDE

    def test_rule_price_invalid_constant_exists(self):
        """PRICE_INVALID 常量存在且为非空字符串。"""
        assert isinstance(RULE_PRICE_INVALID, str)
        assert RULE_PRICE_INVALID == "PRICE_INVALID"
        assert len(RULE_PRICE_INVALID) > 0

    def test_rule_volume_invalid_constant_exists(self):
        assert isinstance(RULE_VOLUME_INVALID, str)
        assert RULE_VOLUME_INVALID == "VOLUME_INVALID"

    def test_rule_target_label_missing_constant_exists(self):
        assert isinstance(RULE_TARGET_LABEL_MISSING, str)
        assert RULE_TARGET_LABEL_MISSING == "TARGET_LABEL_MISSING"

    def test_missing_pit_entry_triggers_unknown(self):
        """pit 没有条目 → production 模式抛 StatusUnknownBlockingError。"""
        sid = 50003
        pit: dict = {}
        with pytest.raises(StatusUnknownBlockingError) as exc:
            apply_daily_filters(TRADE_DATE, [sid], {}, pit, DEFAULT_CFG)
        assert sid in exc.value.symbol_ids

    def test_all_rule_constants_unique(self):
        """所有 RULE_* 字符串常量唯一。"""
        rule_names = [
            RULE_STATUS_UNKNOWN_BLOCK, RULE_PRICE_INVALID,
            RULE_VOLUME_INVALID, RULE_TARGET_LABEL_MISSING,
            RULE_NEW_LISTING_EXCLUDE, RULE_NEW_LISTING_LISTING_DATE_UNKNOWN,
            RULE_ST_EXCLUDE, RULE_ST_HOLD_NO_FORCE_LIQUIDATE,
            RULE_SUSPENDED_EXCLUDE, RULE_SUSPENDED_FREEZE,
            RULE_DELISTING_PERIOD_EXCLUDE, RULE_DELISTING_LIQUIDATION_MANDATORY,
            RULE_DELISTING_PRICE_MISSING, RULE_INCLUDE_NORMAL,
        ]
        assert len(rule_names) == len(set(rule_names)), (
            f"RULE_* 重复: {len(rule_names)} vs unique {len(set(rule_names))}"
        )

    def test_rule_constants_json_roundtrip(self):
        """RULE 常量可序列化 / 反序列化 JSON。"""
        rule_names = [
            RULE_STATUS_UNKNOWN_BLOCK, RULE_PRICE_INVALID,
            RULE_VOLUME_INVALID, RULE_TARGET_LABEL_MISSING,
            RULE_NEW_LISTING_EXCLUDE, RULE_INCLUDE_NORMAL,
        ]
        payload = {"rules": rule_names}
        s = json.dumps(payload)
        loaded = json.loads(s)
        assert loaded["rules"] == rule_names


# =====================================================================
# 6. 配置 (8)
# =====================================================================
class TestConfig:
    def test_default_all_enabled(self):
        cfg = BacktestFilterConfig()
        assert cfg.filter_new_listing is True
        assert cfg.filter_st is True
        assert cfg.filter_suspended is True
        assert cfg.force_delisting_liquidation is True
        assert cfg.delisting_period_excluded is True
        assert cfg.min_listing_age_calendar_days == 120
        assert cfg.production_fidelity is True
        assert cfg.diagnostic_only is False

    def test_filter_new_listing_ablation_only(self):
        """关 filter_new_listing 仅影响次新股规则；ST 仍排除。"""
        st = 60001
        new_ = 60002
        pit = {
            st: _mk(st, "ST", listing_age=500, listing_date=date(2023, 1, 1)),
            new_: _mk(new_, listing_age=50, listing_date=date(2024, 4, 10)),
        }
        cfg = BacktestFilterConfig(filter_new_listing=False)
        out = apply_daily_filters(TRADE_DATE, [st, new_], {}, pit, cfg)
        assert new_ in out.eligible_candidates  # 次新股规则关了
        assert st not in out.eligible_candidates  # ST 仍排除
        rules = {e.rule_code for e in out.filter_events if e.symbol_id == new_}
        assert RULE_NEW_LISTING_EXCLUDE not in rules

    def test_filter_st_ablation_only(self):
        """关 filter_st；次新股仍排除。"""
        st = 60003
        new_ = 60004
        pit = {
            st: _mk(st, "ST", listing_age=500, listing_date=date(2023, 1, 1)),
            new_: _mk(new_, listing_age=50, listing_date=date(2024, 4, 10)),
        }
        cfg = BacktestFilterConfig(filter_st=False)
        out = apply_daily_filters(TRADE_DATE, [st, new_], {}, pit, cfg)
        assert st in out.eligible_candidates
        assert new_ not in out.eligible_candidates

    def test_filter_suspended_ablation_only(self):
        """关 filter_suspended：停牌候选通过；次新股仍排除。"""
        sus = 60005
        new_ = 60006
        pit = {
            sus: _mk(sus, "SUSPENDED", listing_age=500, listing_date=date(2023, 1, 1)),
            new_: _mk(new_, listing_age=80, listing_date=date(2024, 3, 10)),
        }
        cfg = BacktestFilterConfig(filter_suspended=False)
        out = apply_daily_filters(TRADE_DATE, [sus, new_], {}, pit, cfg)
        assert sus in out.eligible_candidates
        assert new_ not in out.eligible_candidates

    def test_config_hash_1000_times_stable(self):
        """相同输入 1000 次 compute_config_hash 结果完全一致。"""
        cfg = BacktestFilterConfig()
        h0 = compute_config_hash(cfg)
        for _ in range(999):
            assert compute_config_hash(cfg) == h0

    def test_frozen_immutable(self):
        cfg = BacktestFilterConfig()
        with pytest.raises(FrozenInstanceError):
            cfg.filter_st = False
        with pytest.raises(FrozenInstanceError):
            cfg.min_listing_age_calendar_days = 90

    def test_validate_production_fidelity_default_passes(self):
        cfg = BacktestFilterConfig()
        ok, reason = validate_production_fidelity(cfg)
        assert ok is True
        assert reason == ""

    def test_hash_excludes_diagnostic_only(self):
        a = BacktestFilterConfig(diagnostic_only=False)
        b = BacktestFilterConfig(diagnostic_only=True)
        assert compute_config_hash(a) == compute_config_hash(b)


# =====================================================================
# 7. 排序稳定 + 互斥 + 冻结契约 (11)
# =====================================================================
class TestStabilityAndContracts:
    def test_events_sorted_20_runs_identical(self):
        """同一输入 20 次 events 序列一致（hash 完全相同）。"""
        sids = [70001, 70002, 70003, 70004, 70005]
        pit = {
            70001: _mk(70001, listing_age=300, listing_date=date(2023, 8, 1)),
            70002: _mk(70002, "ST", listing_age=500, listing_date=date(2023, 1, 15)),
            70003: _mk(70003, "SUSPENDED", listing_age=600, listing_date=date(2022, 10, 1)),
            70004: _mk(70004, listing_age=80, listing_date=date(2024, 3, 10)),
            70005: _mk(70005, "DELISTING_PERIOD", listing_age=1000,
                        listing_date=date(2021, 1, 1)),
        }
        positions = {70003: 150.0}
        seq_hashes = []
        for _ in range(20):
            out = apply_daily_filters(TRADE_DATE, list(sids), positions, pit, DEFAULT_CFG)
            h = hashlib.sha256(
                json.dumps([(e.symbol_id, e.rule_code, e.action)
                            for e in out.filter_events],
                           sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest()
            seq_hashes.append(h)
        first = seq_hashes[0]
        assert all(h == first for h in seq_hashes), f"20 次序列不一致: unique={len(set(seq_hashes))}"

    def test_eligible_disjoint_frozen(self):
        e1, f1, d1 = 70011, 70012, 70013
        pit = {
            e1: _mk(e1, listing_age=300, listing_date=date(2023, 8, 1)),
            f1: _mk(f1, "SUSPENDED", listing_age=500, listing_date=date(2023, 1, 1)),
            d1: _mk(d1, "DELISTED", listing_age=2000,
                    listing_date=date(2019, 1, 1),
                    delisting_date=date(2024, 5, 1),
                    delisting_days_ago=31, is_listed=False),
        }
        out = apply_daily_filters(
            TRADE_DATE, [e1, f1], {f1: 200.0, d1: 300.0}, pit, DEFAULT_CFG,
        )
        eligible = set(out.eligible_candidates)
        frozen = set(out.frozen_positions.keys())
        delisted = {c.symbol_id for c in out.delisting_candidates}
        assert eligible.isdisjoint(frozen)
        assert eligible.isdisjoint(delisted)

    def test_eligible_each_has_include(self):
        sids = [70021, 70022, 70023, 70024]
        pit = {
            70021: _mk(70021, listing_age=150, listing_date=date(2024, 1, 2)),
            70022: _mk(70022, listing_age=200, listing_date=date(2023, 11, 13)),
            70023: _mk(70023, listing_age=120, listing_date=date(2024, 2, 1)),
            70024: _mk(70024, "ST", listing_age=500, listing_date=date(2023, 1, 1)),
        }
        out = apply_daily_filters(TRADE_DATE, list(sids), {}, pit, DEFAULT_CFG)
        for sid in out.eligible_candidates:
            assert any(e.symbol_id == sid and e.action == ACTION_INCLUDE
                       for e in out.filter_events), f"sid={sid} 无 include 事件"

    def test_all_events_config_hash_len_64(self):
        sids = [70031, 70032, 70033]
        pit = {
            70031: _mk(70031, listing_age=200, listing_date=date(2023, 11, 1)),
            70032: _mk(70032, "ST", listing_age=500, listing_date=date(2023, 1, 1)),
            70033: _mk(70033, listing_age=80, listing_date=date(2024, 3, 10)),
        }
        out = apply_daily_filters(TRADE_DATE, list(sids), {}, pit, DEFAULT_CFG,
                                   run_id=42, data_batch_id="batch-xyz")
        for ev in out.filter_events:
            assert len(ev.config_hash) == 64
            int(ev.config_hash, 16)

    def test_run_id_data_batch_id_propagated(self):
        pit = {70041: _mk(70041, listing_age=200, listing_date=date(2023, 11, 1))}
        out = apply_daily_filters(TRADE_DATE, [70041], {}, pit, DEFAULT_CFG,
                                   run_id=99, data_batch_id="B-1")
        for ev in out.filter_events:
            assert ev.run_id == 99
            assert ev.data_batch_id == "B-1"

    def test_freeze_contract_all_pass(self):
        viols = validate_freeze_contract(
            trade_date=TRADE_DATE,
            frozen_ids={70051, 70052},
            opening_by_symbol={70051: 100.0, 70052: 200.0},
            fills_symbol_ids=[70053],
            buy_qty_by_symbol={70053: 50.0},
            sell_qty_by_symbol={70053: 10.0},
            closing_by_symbol={70051: 100.0, 70052: 200.0},
            filter_events=[
                FilterEventDTO(trade_date=TRADE_DATE, symbol_id=70051,
                               action=ACTION_FREEZE, rule_code=RULE_SUSPENDED_FREEZE,
                               reason="停牌冻结"),
                FilterEventDTO(trade_date=TRADE_DATE, symbol_id=70052,
                               action=ACTION_FREEZE, rule_code=RULE_SUSPENDED_FREEZE,
                               reason="停牌冻结"),
            ],
        )
        assert viols == []

    def test_freeze_contract_v1_opening_closing_mismatch(self):
        viols = validate_freeze_contract(
            trade_date=TRADE_DATE, frozen_ids={70061},
            opening_by_symbol={70061: 100.0},
            fills_symbol_ids=[],
            buy_qty_by_symbol={}, sell_qty_by_symbol={},
            closing_by_symbol={70061: 80.0},
        )
        assert any(v.violation_code == "FREEZE_V1_CLOSING_MISMATCH" for v in viols)

    def test_freeze_contract_v2_nonzero_trade(self):
        viols = validate_freeze_contract(
            trade_date=TRADE_DATE, frozen_ids={70062},
            opening_by_symbol={70062: 100.0},
            fills_symbol_ids=[],
            buy_qty_by_symbol={70062: 10.0},
            sell_qty_by_symbol={},
            closing_by_symbol={70062: 110.0},
        )
        assert any(v.violation_code == "FREEZE_V2_NONZERO_TRADE_QTY" for v in viols)

    def test_freeze_contract_v3_fills_exist(self):
        viols = validate_freeze_contract(
            trade_date=TRADE_DATE, frozen_ids={70063},
            opening_by_symbol={70063: 100.0},
            fills_symbol_ids=[70063, 70099],
            buy_qty_by_symbol={}, sell_qty_by_symbol={},
            closing_by_symbol={70063: 100.0},
        )
        assert any(v.violation_code == "FREEZE_V3_FILLS_EXIST" for v in viols)

    def test_freeze_contract_v4_missing_event(self):
        viols = validate_freeze_contract(
            trade_date=TRADE_DATE, frozen_ids={70064},
            opening_by_symbol={70064: 100.0},
            fills_symbol_ids=[],
            buy_qty_by_symbol={}, sell_qty_by_symbol={},
            closing_by_symbol={70064: 100.0},
            filter_events=[],  # 无冻结事件
        )
        assert any(v.violation_code == "FREEZE_V4_NO_AUDIT_EVENT" for v in viols)

    def test_frozen_skip_set_dict_and_iterable(self):
        frozen_dict = {
            70071: FrozenPositionInfo(
                symbol_id=70071, reason_rule_code=RULE_SUSPENDED_FREEZE,
                reason_detail="x", opening_quantity=100.0,
            ),
            70072: FrozenPositionInfo(
                symbol_id=70072, reason_rule_code=RULE_SUSPENDED_FREEZE,
                reason_detail="x", opening_quantity=200.0,
            ),
        }
        assert frozen_symbols_skip_set(frozen_dict) == {70071, 70072}
        assert frozen_symbols_skip_set([70073, 70074]) == {70073, 70074}
        assert frozen_symbols_skip_set({70075, 70076}) == {70075, 70076}


# =====================================================================
# 8. 退市清算 + 价格解析 (3 - 总数达到 60+)
# =====================================================================
class TestLiquidation:
    def test_liquidation_result_pnl_calculation(self):
        from app.services.backtest_filters.rules import DelistingCandidate

        def fixed_resolver(db, sid_, td):
            return 10.5

        sid = 80001
        positions = {sid: HoldingInfo(quantity=100.0, avg_cost_price=9.0)}
        pit = {sid: _mk(sid, "DELISTED", listing_age=2000,
                        listing_date=date(2019, 1, 1),
                        delisting_date=TRADE_DATE, is_listed=False)}
        cands = [DelistingCandidate(symbol_id=sid, trade_date=TRADE_DATE,
                                     holding_quantity=100.0, last_valid_close=None)]
        results, _ = process_delisting_liquidations(
            db=None, run_id=1, trade_date=TRADE_DATE, positions=positions,
            pit_status_map=pit, delisting_candidates=cands,
            price_resolver=fixed_resolver,
        )
        assert len(results) == 1
        r = results[0]
        assert r.exit_price == 10.5
        assert r.quantity == 100.0
        assert abs(r.pnl - (10.5 - 9.0) * 100.0) < 1e-9

    def test_liquidation_inf_price_treated_missing(self):
        """inf close 由 resolver 层过滤（这里让 resolver 返回 None → 阻断）。"""
        from app.services.backtest_filters.rules import DelistingCandidate

        def inf_resolver(db, sid_, td):
            return None

        sid = 80002
        positions = {sid: HoldingInfo(quantity=100.0, avg_cost_price=5.0)}
        pit = {sid: _mk(sid, "DELISTED", listing_age=2000,
                        listing_date=date(2019, 1, 1),
                        delisting_date=TRADE_DATE, is_listed=False)}
        cands = [DelistingCandidate(symbol_id=sid, trade_date=TRADE_DATE,
                                     holding_quantity=100.0, last_valid_close=None)]
        with pytest.raises(DelistingPriceMissingError):
            process_delisting_liquidations(
                db=None, run_id=1, trade_date=TRADE_DATE, positions=positions,
                pit_status_map=pit, delisting_candidates=cands,
                price_resolver=inf_resolver,
            )

    def test_cand_last_valid_close_used_first(self):
        """candidate 已提供 last_valid_close → 优先使用，不调用 resolver。"""
        from app.services.backtest_filters.rules import DelistingCandidate

        def should_not_call(db, sid_, td):
            pytest.fail("resolver 不应被调用，last_valid_close 应优先使用")

        sid = 80003
        positions = {sid: HoldingInfo(quantity=100.0, avg_cost_price=8.0)}
        pit = {sid: _mk(sid, "DELISTED", listing_age=2000,
                        listing_date=date(2019, 1, 1),
                        delisting_date=TRADE_DATE, is_listed=False)}
        cands = [DelistingCandidate(symbol_id=sid, trade_date=TRADE_DATE,
                                     holding_quantity=100.0, last_valid_close=12.0)]
        results, events = process_delisting_liquidations(
            db=None, run_id=1, trade_date=TRADE_DATE, positions=positions,
            pit_status_map=pit, delisting_candidates=cands,
            price_resolver=should_not_call,
        )
        assert len(results) == 1
        assert results[0].exit_price == 12.0
