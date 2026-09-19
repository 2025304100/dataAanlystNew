"""apply_daily_filters 引擎单元测试（纯函数，无 DB 依赖）。

覆盖 Task 9/10 要求的 17+ 用例：
- 次新股（119/120/121/listing_date_unknown）4 条
- ST（候选排除 + 持仓不强平）2 条
- 停牌（候选排除 + 持仓冻结）2 条
- 退市整理期排除 1 条
- 已摘牌强制清算候选 1 条
- UNKNOWN（生产阻断 / 非保真排除）2 条
- 单开关消融 1 条
- 事件稳定排序 1 条
- 互斥性校验（eligible ∩ frozen / delisting）1 条
- INCLUDE 事件完备性 1 条
- config_hash 全量填充 1 条
"""
from __future__ import annotations

from datetime import date

import pytest

from app.services.backtest_filters.config import BacktestFilterConfig
from app.services.backtest_filters.engine import apply_daily_filters
from app.services.backtest_filters.rules import (
    ACTION_EXCLUDE,
    ACTION_FREEZE,
    ACTION_FORCE_LIQUIDATE,
    ACTION_INCLUDE,
    RULE_DELISTING_LIQUIDATION_MANDATORY,
    RULE_DELISTING_PERIOD_EXCLUDE,
    RULE_INCLUDE_NORMAL,
    RULE_NEW_LISTING_EXCLUDE,
    RULE_NEW_LISTING_LISTING_DATE_UNKNOWN,
    RULE_STATUS_UNKNOWN_BLOCK,
    RULE_ST_EXCLUDE,
    RULE_ST_HOLD_NO_FORCE_LIQUIDATE,
    RULE_SUSPENDED_EXCLUDE,
    RULE_SUSPENDED_FREEZE,
    StatusUnknownBlockingError,
)
from app.services.security_status.pit_service import SecurityStatusDTO


# =====================================================================
# 测试常量
# =====================================================================
TRADE_DATE = date(2024, 6, 1)
DEFAULT_CFG = BacktestFilterConfig()  # 全量开启 + production_fidelity=True


# =====================================================================
# 辅助：快速构造 SecurityStatusDTO
# =====================================================================
def _mk_status(
    symbol_id: int,
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
) -> SecurityStatusDTO:
    """便捷工厂：按字面 status 自动推导 is_* 标志（调用方也可显式覆盖）。"""
    # 按 status 推导布尔标志（仅当调用方未显式传 True 时）
    _is_st = is_st or (status == "ST")
    _is_suspended = is_suspended or (status == "SUSPENDED")
    _is_dp = is_delisting_period or (status == "DELISTING_PERIOD")
    _is_listed = (status != "DELISTED") and is_listed
    # listing_age 推断：若传了 listing_date 但没传 listing_age，自动算
    if listing_age is None and listing_date is not None:
        listing_age = (TRADE_DATE - listing_date).days
    # delisting_days_ago 推断
    if delisting_days_ago is None and delisting_date is not None:
        delisting_days_ago = (TRADE_DATE - delisting_date).days
    return SecurityStatusDTO(
        symbol_id=symbol_id,
        trade_date=TRADE_DATE,
        status=status,  # type: ignore[arg-type]
        listing_age_calendar_days=listing_age,
        delisting_days_ago=delisting_days_ago,
        raw_source=raw_source,
        listing_date=listing_date,
        delisting_date=delisting_date,
        is_st=_is_st,
        is_suspended=_is_suspended,
        is_delisting_period=_is_dp,
        is_listed=_is_listed,
    )


# =====================================================================
# 次新股 4 条
# =====================================================================
def test_new_listing_119_excluded():
    """上市 119 天 → 次新股排除（严格 < 120）。"""
    sid = 1001
    pit = {sid: _mk_status(sid, "LISTED", listing_age=119,
                           listing_date=date(2024, 2, 2))}
    outcome = apply_daily_filters(
        TRADE_DATE, [sid], {}, pit, DEFAULT_CFG,
    )
    assert sid not in outcome.eligible_candidates
    events = [e for e in outcome.filter_events if e.symbol_id == sid]
    rule_codes = {e.rule_code for e in events}
    assert RULE_NEW_LISTING_EXCLUDE in rule_codes
    # 对应事件 action == exclude_candidate
    ev = next(e for e in events if e.rule_code == RULE_NEW_LISTING_EXCLUDE)
    assert ev.action == ACTION_EXCLUDE
    assert "119" in ev.reason


def test_new_listing_120_included():
    """上市 120 天 → 包含（阈值为严格 <，120 不满足排除条件）。"""
    sid = 1002
    pit = {sid: _mk_status(sid, "LISTED", listing_age=120,
                           listing_date=date(2024, 2, 1))}
    outcome = apply_daily_filters(
        TRADE_DATE, [sid], {}, pit, DEFAULT_CFG,
    )
    assert sid in outcome.eligible_candidates
    events = [e for e in outcome.filter_events if e.symbol_id == sid]
    # 必须没有 NEW_LISTING_EXCLUDE
    assert RULE_NEW_LISTING_EXCLUDE not in {e.rule_code for e in events}
    # 必有 INCLUDE_NORMAL
    assert any(e.rule_code == RULE_INCLUDE_NORMAL and e.action == ACTION_INCLUDE
               for e in events)


def test_new_listing_121_included():
    """上市 121 天 → 包含。"""
    sid = 1003
    pit = {sid: _mk_status(sid, "LISTED", listing_age=121,
                           listing_date=date(2024, 1, 31))}
    outcome = apply_daily_filters(
        TRADE_DATE, [sid], {}, pit, DEFAULT_CFG,
    )
    assert sid in outcome.eligible_candidates
    rule_codes = {e.rule_code for e in outcome.filter_events
                  if e.symbol_id == sid}
    assert RULE_NEW_LISTING_EXCLUDE not in rule_codes
    assert RULE_INCLUDE_NORMAL in rule_codes


def test_new_listing_listing_date_unknown():
    """listing_date=None → 排除（rule=NEW_LISTING_LISTING_DATE_UNKNOWN）。"""
    sid = 1004
    pit = {sid: _mk_status(sid, "LISTED", listing_age=None, listing_date=None)}
    outcome = apply_daily_filters(
        TRADE_DATE, [sid], {}, pit, DEFAULT_CFG,
    )
    assert sid not in outcome.eligible_candidates
    events = [e for e in outcome.filter_events if e.symbol_id == sid]
    rule_codes = {e.rule_code for e in events}
    assert RULE_NEW_LISTING_LISTING_DATE_UNKNOWN in rule_codes
    ev = next(e for e in events if e.rule_code == RULE_NEW_LISTING_LISTING_DATE_UNKNOWN)
    assert ev.action == ACTION_EXCLUDE


# =====================================================================
# ST 2 条
# =====================================================================
def test_st_candidate_excluded():
    """候选为 ST → 排除（RULE_ST_EXCLUDE）。"""
    sid = 2001
    pit = {sid: _mk_status(sid, "ST", listing_age=500,
                           listing_date=date(2023, 1, 1))}
    outcome = apply_daily_filters(
        TRADE_DATE, [sid], {}, pit, DEFAULT_CFG,
    )
    assert sid not in outcome.eligible_candidates
    events = [e for e in outcome.filter_events if e.symbol_id == sid]
    rule_codes = {e.rule_code for e in events}
    assert RULE_ST_EXCLUDE in rule_codes
    ev = next(e for e in events if e.rule_code == RULE_ST_EXCLUDE)
    assert ev.action == ACTION_EXCLUDE


def test_st_held_not_liquidated():
    """持仓变 ST → 仅产生 ST_HOLD_NO_FORCE_LIQUIDATE 事件，不 frozen 不排除。

    注意：持仓中的 symbol 本身不能在 candidate_symbol_ids 中（否则会被候选 ST 排除
    逻辑命中，导致排除事件冲突）。这里把它仅放在 position_map。
    """
    held_sid = 2002
    candidate_sid = 2003  # 一个普通候选用于推动流程
    pit = {
        held_sid: _mk_status(held_sid, "ST", listing_age=500,
                             listing_date=date(2023, 1, 1)),
        candidate_sid: _mk_status(candidate_sid, "LISTED", listing_age=300,
                                  listing_date=date(2023, 8, 1)),
    }
    positions = {held_sid: 100.0}
    outcome = apply_daily_filters(
        TRADE_DATE, [candidate_sid], positions, pit, DEFAULT_CFG,
    )
    # held_sid 不在候选池，也不应被加入 frozen
    assert held_sid not in outcome.frozen_positions
    # 必须有 ST_HOLD_NO_FORCE_LIQUIDATE 事件
    held_events = [e for e in outcome.filter_events if e.symbol_id == held_sid]
    rule_codes = {e.rule_code for e in held_events}
    assert RULE_ST_HOLD_NO_FORCE_LIQUIDATE in rule_codes
    # 该事件 action == include（不强平不动作）
    ev = next(e for e in held_events
              if e.rule_code == RULE_ST_HOLD_NO_FORCE_LIQUIDATE)
    assert ev.action == ACTION_INCLUDE
    # 候选正常通过
    assert candidate_sid in outcome.eligible_candidates


# =====================================================================
# 停牌 2 条
# =====================================================================
def test_suspended_candidate_excluded():
    """候选为停牌 → 排除（RULE_SUSPENDED_EXCLUDE）。"""
    sid = 3001
    pit = {sid: _mk_status(sid, "SUSPENDED", listing_age=500,
                           listing_date=date(2023, 1, 1))}
    outcome = apply_daily_filters(
        TRADE_DATE, [sid], {}, pit, DEFAULT_CFG,
    )
    assert sid not in outcome.eligible_candidates
    events = [e for e in outcome.filter_events if e.symbol_id == sid]
    rule_codes = {e.rule_code for e in events}
    assert RULE_SUSPENDED_EXCLUDE in rule_codes
    ev = next(e for e in events if e.rule_code == RULE_SUSPENDED_EXCLUDE)
    assert ev.action == ACTION_EXCLUDE


def test_suspended_holding_frozen():
    """持仓停牌 → frozen_positions 有条目且 opening_quantity 正确。"""
    held_sid = 3002
    candidate_sid = 3003
    qty = 250.0
    pit = {
        held_sid: _mk_status(held_sid, "SUSPENDED", listing_age=500,
                             listing_date=date(2023, 1, 1)),
        candidate_sid: _mk_status(candidate_sid, "LISTED", listing_age=400,
                                  listing_date=date(2023, 4, 1)),
    }
    positions = {held_sid: qty}
    outcome = apply_daily_filters(
        TRADE_DATE, [candidate_sid], positions, pit, DEFAULT_CFG,
    )
    # frozen 有条目
    assert held_sid in outcome.frozen_positions
    info = outcome.frozen_positions[held_sid]
    assert info.reason_rule_code == RULE_SUSPENDED_FREEZE
    assert info.opening_quantity == qty
    # 对应 freeze 事件存在
    held_events = [e for e in outcome.filter_events if e.symbol_id == held_sid]
    assert any(e.rule_code == RULE_SUSPENDED_FREEZE and e.action == ACTION_FREEZE
               for e in held_events)
    # 候选正常
    assert candidate_sid in outcome.eligible_candidates


# =====================================================================
# 退市整理期 1 条
# =====================================================================
def test_delisting_period_candidate_excluded():
    """退市整理期候选 → 排除（RULE_DELISTING_PERIOD_EXCLUDE）。"""
    sid = 4001
    pit = {sid: _mk_status(sid, "DELISTING_PERIOD", listing_age=1000,
                           listing_date=date(2021, 1, 1))}
    outcome = apply_daily_filters(
        TRADE_DATE, [sid], {}, pit, DEFAULT_CFG,
    )
    assert sid not in outcome.eligible_candidates
    events = [e for e in outcome.filter_events if e.symbol_id == sid]
    rule_codes = {e.rule_code for e in events}
    assert RULE_DELISTING_PERIOD_EXCLUDE in rule_codes
    ev = next(e for e in events if e.rule_code == RULE_DELISTING_PERIOD_EXCLUDE)
    assert ev.action == ACTION_EXCLUDE


# =====================================================================
# 已摘牌清算候选 1 条
# =====================================================================
def test_delisted_holding_liquidation_candidate():
    """已 DELISTED 且持仓 → delisting_candidates + force_liquidate 事件。"""
    delisted_sid = 5001
    qty = 500.0
    candidate_sid = 5002
    pit = {
        delisted_sid: _mk_status(delisted_sid, "DELISTED", listing_age=2000,
                                 listing_date=date(2019, 1, 1),
                                 delisting_date=date(2024, 5, 15),
                                 delisting_days_ago=17,
                                 is_listed=False),
        candidate_sid: _mk_status(candidate_sid, "LISTED", listing_age=300,
                                  listing_date=date(2023, 8, 1)),
    }
    positions = {delisted_sid: qty}
    outcome = apply_daily_filters(
        TRADE_DATE, [candidate_sid], positions, pit, DEFAULT_CFG,
    )
    # delisting_candidates 含条目
    matched = [c for c in outcome.delisting_candidates
               if c.symbol_id == delisted_sid]
    assert len(matched) == 1
    assert matched[0].holding_quantity == qty
    assert matched[0].trade_date == TRADE_DATE
    # last_valid_close 留空（清算器回填）
    assert matched[0].last_valid_close is None
    # force_liquidate 事件
    evs = [e for e in outcome.filter_events if e.symbol_id == delisted_sid]
    rule_codes = {e.rule_code for e in evs}
    assert RULE_DELISTING_LIQUIDATION_MANDATORY in rule_codes
    ev = next(e for e in evs
              if e.rule_code == RULE_DELISTING_LIQUIDATION_MANDATORY)
    assert ev.action == ACTION_FORCE_LIQUIDATE
    # delisted symbol 不在 frozen（走清算而非冻结）
    assert delisted_sid not in outcome.frozen_positions
    # 候选正常
    assert candidate_sid in outcome.eligible_candidates


# =====================================================================
# UNKNOWN 2 条
# =====================================================================
def test_status_unknown_production_blocked():
    """production_fidelity=True + UNKNOWN symbol → 抛 StatusUnknownBlockingError。"""
    unknown_sid = 6001
    # pit 中完全没有该 symbol（等价于 UNKNOWN），或者 status=UNKNOWN
    pit: dict = {}  # 查不到 => UNKNOWN
    with pytest.raises(StatusUnknownBlockingError) as excinfo:
        apply_daily_filters(
            TRADE_DATE, [unknown_sid], {}, pit,
            BacktestFilterConfig(production_fidelity=True),
        )
    assert unknown_sid in excinfo.value.symbol_ids
    assert excinfo.value.trade_date == TRADE_DATE
    assert excinfo.value.rule_code == RULE_STATUS_UNKNOWN_BLOCK


def test_status_unknown_nonfidelity_excluded():
    """production_fidelity=False，UNKNOWN 仅排除，不抛异常。"""
    unknown_sid = 6002
    good_sid = 6003
    pit = {
        good_sid: _mk_status(good_sid, "LISTED", listing_age=200,
                             listing_date=date(2023, 11, 1)),
        # 不提供 unknown_sid => UNKNOWN
    }
    outcome = apply_daily_filters(
        TRADE_DATE, [unknown_sid, good_sid], {}, pit,
        BacktestFilterConfig(production_fidelity=False),
    )
    # UNKNOWN 被排除
    assert unknown_sid not in outcome.eligible_candidates
    # 正常 symbol 保留
    assert good_sid in outcome.eligible_candidates
    # 存在 STATUS_UNKNOWN_BLOCK 事件（排除但不阻断）
    evs = [e for e in outcome.filter_events if e.symbol_id == unknown_sid]
    assert any(e.rule_code == RULE_STATUS_UNKNOWN_BLOCK
               and e.action == ACTION_EXCLUDE for e in evs)


# =====================================================================
# 单开关消融 1 条
# =====================================================================
def test_single_switch_off_only_skips_rule():
    """关闭 filter_st=True→False，次新股规则仍生效。

    - candidate A: ST + 老股 → 在 filter_st=False 时应通过（ST 不排除）
    - candidate B: LISTED + 次新股（age=50）→ 仍被次新股排除
    """
    sid_st = 7001
    sid_new = 7002
    pit = {
        sid_st: _mk_status(sid_st, "ST", listing_age=1000,
                           listing_date=date(2021, 1, 1)),
        sid_new: _mk_status(sid_new, "LISTED", listing_age=50,
                            listing_date=date(2024, 4, 10)),
    }
    cfg = BacktestFilterConfig(filter_st=False)  # 只关 ST
    outcome = apply_daily_filters(
        TRADE_DATE, [sid_st, sid_new], {}, pit, cfg,
    )
    # ST 开关关闭：ST 候选不被 ST 规则排除
    assert sid_st in outcome.eligible_candidates
    st_rules = [e.rule_code for e in outcome.filter_events
                if e.symbol_id == sid_st and e.rule_code == RULE_ST_EXCLUDE]
    assert len(st_rules) == 0
    # 次新股开关仍开启（默认 True）：次新股仍被排除
    assert sid_new not in outcome.eligible_candidates
    new_rules = {e.rule_code for e in outcome.filter_events
                 if e.symbol_id == sid_new}
    assert RULE_NEW_LISTING_EXCLUDE in new_rules


# =====================================================================
# 事件稳定排序 1 条
# =====================================================================
def test_events_sorted_stable():
    """相同输入 5 次执行，events 的 (symbol_id, rule_code) 序列完全一致。"""
    sids = [8001, 8002, 8003, 8004]
    pit = {
        8001: _mk_status(8001, "LISTED", listing_age=300,
                         listing_date=date(2023, 8, 1)),
        8002: _mk_status(8002, "ST", listing_age=500,
                         listing_date=date(2023, 1, 15)),
        8003: _mk_status(8003, "SUSPENDED", listing_age=600,
                         listing_date=date(2022, 10, 1)),
        8004: _mk_status(8004, "LISTED", listing_age=80,
                         listing_date=date(2024, 3, 10)),
    }
    positions = {8003: 150.0}  # 停牌持仓会有 freeze 事件
    sequences = []
    for _ in range(5):
        outcome = apply_daily_filters(
            TRADE_DATE, list(sids), positions, pit, DEFAULT_CFG,
        )
        seq = [(e.symbol_id, e.rule_code) for e in outcome.filter_events]
        sequences.append(seq)
    # 5 条序列两两相等
    for i in range(1, 5):
        assert sequences[i] == sequences[0], (
            f"第 {i} 次执行与第 0 次事件序列不一致"
        )
    # 事件按 symbol_id 升序（弱校验：序列中 symbol_id 不下降）
    sym_seq = [s for s, _ in sequences[0]]
    assert sym_seq == sorted(sym_seq)


# =====================================================================
# 互斥性 1 条
# =====================================================================
def test_eligible_disjoint_frozen_liquidation():
    """eligible_candidates ∩ frozen_keys = {}，
    eligible_candidates ∩ delisting_candidate_ids = {}。"""
    sid_eligible1 = 9001
    sid_eligible2 = 9002
    sid_frozen = 9003
    sid_delisted = 9004
    pit = {
        sid_eligible1: _mk_status(sid_eligible1, "LISTED", listing_age=300,
                                  listing_date=date(2023, 8, 1)),
        sid_eligible2: _mk_status(sid_eligible2, "LISTED", listing_age=400,
                                  listing_date=date(2023, 4, 22)),
        sid_frozen: _mk_status(sid_frozen, "SUSPENDED", listing_age=500,
                               listing_date=date(2023, 1, 1)),
        sid_delisted: _mk_status(sid_delisted, "DELISTED", listing_age=2000,
                                 listing_date=date(2019, 1, 1),
                                 delisting_date=date(2024, 5, 1),
                                 delisting_days_ago=31,
                                 is_listed=False),
    }
    positions = {sid_frozen: 200.0, sid_delisted: 300.0}
    candidates = [sid_eligible1, sid_eligible2, sid_frozen]
    outcome = apply_daily_filters(
        TRADE_DATE, candidates, positions, pit, DEFAULT_CFG,
    )
    eligible_set = set(outcome.eligible_candidates)
    frozen_keys = set(outcome.frozen_positions.keys())
    delisted_ids = {c.symbol_id for c in outcome.delisting_candidates}
    # eligible 与 frozen 互斥
    assert eligible_set.isdisjoint(frozen_keys), (
        f"eligible {eligible_set} 与 frozen {frozen_keys} 交集非空"
    )
    # eligible 与 delisting 互斥
    assert eligible_set.isdisjoint(delisted_ids), (
        f"eligible {eligible_set} 与 delisting {delisted_ids} 交集非空"
    )
    # 语义上 sid_frozen 是候选+停牌，所以被排除；sid_delisted 不是候选
    assert sid_frozen not in eligible_set
    assert sid_delisted not in eligible_set
    assert sid_frozen in frozen_keys
    assert sid_delisted in delisted_ids


# =====================================================================
# INCLUDE 事件完备性 1 条
# =====================================================================
def test_included_each_has_one_include_event():
    """每个 eligible_candidate 至少有一条 action=include 事件。"""
    sids = [9101, 9102, 9103, 9104]
    pit = {
        9101: _mk_status(9101, "LISTED", listing_age=150,
                         listing_date=date(2024, 1, 2)),
        9102: _mk_status(9102, "LISTED", listing_age=200,
                         listing_date=date(2023, 11, 13)),
        9103: _mk_status(9103, "LISTED", listing_age=120,  # 边界
                         listing_date=date(2024, 2, 1)),
        9104: _mk_status(9104, "ST", listing_age=500,  # 被排除
                         listing_date=date(2023, 1, 1)),
    }
    outcome = apply_daily_filters(
        TRADE_DATE, list(sids), {}, pit, DEFAULT_CFG,
    )
    # eligible 候选
    eligible = outcome.eligible_candidates
    assert len(eligible) == 3  # 9101/9102/9103，9104 被 ST 排除
    # 每个 eligible 至少 1 条 include 事件
    for sid in eligible:
        include_evs = [e for e in outcome.filter_events
                       if e.symbol_id == sid and e.action == ACTION_INCLUDE]
        assert len(include_evs) >= 1, (
            f"eligible sid={sid} 缺少 action=include 事件"
        )


# =====================================================================
# config_hash 全量填充 1 条
# =====================================================================
def test_config_hash_present_in_all_events():
    """所有 filter_events 的 config_hash 长度为 64（SHA256 hex）。"""
    sids = [9201, 9202, 9203]
    pit = {
        9201: _mk_status(9201, "LISTED", listing_age=200,
                         listing_date=date(2023, 11, 1)),
        9202: _mk_status(9202, "ST", listing_age=500,
                         listing_date=date(2023, 1, 1)),
        9203: _mk_status(9203, "LISTED", listing_age=80,
                         listing_date=date(2024, 3, 10)),
    }
    positions = {9204: 100.0}
    # 加一个 DELISTED 持仓产生清算事件
    pit[9204] = _mk_status(9204, "DELISTED", listing_age=1000,
                           listing_date=date(2021, 6, 1),
                           delisting_date=date(2024, 5, 20),
                           delisting_days_ago=12,
                           is_listed=False)
    outcome = apply_daily_filters(
        TRADE_DATE, list(sids), positions, pit, DEFAULT_CFG,
        run_id=42,
        data_batch_id="batch-xyz",
    )
    assert len(outcome.filter_events) > 0
    for ev in outcome.filter_events:
        assert len(ev.config_hash) == 64, (
            f"sid={ev.symbol_id} rule={ev.rule_code} config_hash 长度="
            f"{len(ev.config_hash)}，期望 64"
        )
        # 全部是合法 hex
        int(ev.config_hash, 16)
    # 检查 run_id / data_batch_id 回填
    for ev in outcome.filter_events:
        assert ev.run_id == 42
        assert ev.data_batch_id == "batch-xyz"
