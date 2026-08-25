"""G5 双跑加速回放服务 pytest 验证（AC-15 20交易日逐证券对比框架）。

覆盖范围：
  * T_G5_01: 20 交易日生成（工作日，无周末）→ 20 天 + 首尾 weekday<5
  * T_G5_02: explicit_dates 直接使用，显式跳过周末判断
  * T_G5_03: always_pass 合成 runner → 20 日对比 100% 通过 + g5_eligible_for_g6 = True
  * T_G5_04: 候选集 Jaccard = 1.0 (A=B) + 动作不一致 → 不触发 MEMBER_SOURCE_DIFF
  * T_G5_05: 候选集 Jaccard < 1（B 独有）→ 差异归类为 MEMBER_SOURCE_DIFF，不进 P0
  * T_G5_06: chain A DATA_BLOCKED vs chain B 正常 → 归类 SCORE_DATA_GAP
  * T_G5_07: 一边 hard_gate blocked → 归类 HARD_GATE_INTERVENTION
  * T_G5_08: 20 日合成 + 故意 3 天 P0/P1 差异 → failing_days 汇总正确
  * T_G5_09: ACTION_TYPES 6x6 混淆矩阵对角线 = 动作一致数
  * T_G5_10: API 序列化 as_dict() 可 json.dumps（无 date/datetime 溢出）
"""
from __future__ import annotations

import json
from datetime import date

import pytest

from app.services.g5_dual_run_replay import (
    ACTION_TYPES,
    DailyChainResult,
    SecurityDecision,
    build_confusion_matrix_6x6,
    build_synthetic_chain_runner,
    classify_diff_reason,
    compare_one_day,
    generate_20_trade_days_backward,
    run_g5_dual_run_replay,
)


# ============================================================================
# Helpers
# ============================================================================
def _weekday_range(d_list: list[date]) -> list[int]:
    return [d.weekday() for d in d_list]


# ============================================================================
# T_G5_01: 20 交易日生成
# ============================================================================
def test_g5_01_generate_20_trade_days_no_weekends():
    """从固定 anchor 生成 20 日 → 20 天，每一天 weekday() < 5（无周六/周日）。"""
    # anchor = 2026-08-14 周五 (weekday=4)
    anchor = date(2026, 8, 14)
    days = generate_20_trade_days_backward(anchor, n_days=20)
    assert len(days) == 20
    wd_list = _weekday_range(days)
    assert all(0 <= w < 5 for w in wd_list)
    # 正向顺序：最早在前，最晚在后
    assert days[0] < days[-1]
    assert days[-1] == anchor  # anchor 是周五，应在结果最后一个


def test_g5_01b_generate_10_plus_minimum_for_ac15():
    """AC-15 至少 10 日 → n_days=10 生成 10 日。"""
    days = generate_20_trade_days_backward(date(2026, 8, 14), n_days=10)
    assert len(days) == 10
    assert all(d.weekday() < 5 for d in days)


# ============================================================================
# T_G5_02: explicit_dates 优先
# ============================================================================
def test_g5_02_explicit_dates_override_generator():
    """传入 explicit_dates → 忽略 anchor/n_days，直接使用给定日期。"""
    custom = [date(2026, 6, 1), date(2026, 6, 3), date(2026, 6, 15)]
    runner = build_synthetic_chain_runner(
        seed_portfolio_id=1, universe_a=[1, 2], universe_b=[1, 2], always_pass=True,
    )
    s = run_g5_dual_run_replay(
        portfolio_id=1, chain_runner=runner,
        anchor_date=date(2026, 8, 14), n_days=999, explicit_dates=custom,
    )
    assert s.total_days == 3
    assert s.start_date == date(2026, 6, 1)
    assert s.end_date == date(2026, 6, 15)
    assert s.days_replayed == 3


# ============================================================================
# T_G5_03: always_pass 模式 → G5 通过 + g5_eligible_for_g6 = True
# ============================================================================
def test_g5_03_always_pass_20_days_eligible_for_g6():
    """新旧链完全一致 → P0/P1 清零，准入 G6。"""
    universe = list(range(101, 111))  # 10 个标的
    runner = build_synthetic_chain_runner(
        seed_portfolio_id=42, universe_a=universe, universe_b=universe, always_pass=True,
    )
    s = run_g5_dual_run_replay(
        portfolio_id=42, chain_runner=runner,
        anchor_date=date(2026, 8, 14), n_days=20,
    )
    assert s.days_replayed == 20
    assert s.skipped_days == []
    assert s.total_p0_unexplained == 0
    assert s.total_p1_hold_noaction_flip == 0
    assert s.failing_days_p0 == [] and s.failing_days_p1 == []
    # always_pass 下 universe Jaccard 都为 1.0，动作匹配率 1.0
    assert abs(s.avg_universe_jaccard - 1.0) < 1e-9
    assert abs(s.avg_action_match_rate - 1.0) < 1e-9
    assert s.g5_eligible_for_g6 is True
    assert any("G5 通过" in n for n in s.summary_notes)


# ============================================================================
# T_G5_04: A=B 候选集 + 动作不一致（非 DATA_BLOCKED）→ 不触发 MEMBER_SOURCE_DIFF
# ============================================================================
def test_g5_04_universe_equal_action_diff_not_member_source():
    """同候选集，动作不同 → 原因不得是 MEMBER_SOURCE_DIFF。"""
    td = date(2026, 6, 1)
    syms = [1, 2, 3]
    a = DailyChainResult(
        chain="A", trade_date=td, universe_symbol_ids=list(syms),
        decisions={
            1: SecurityDecision(symbol_id=1, action="BUY"),
            2: SecurityDecision(symbol_id=2, action="HOLD"),
            3: SecurityDecision(symbol_id=3, action="SELL"),
        },
    )
    b = DailyChainResult(
        chain="B", trade_date=td, universe_symbol_ids=list(syms),
        decisions={
            1: SecurityDecision(symbol_id=1, action="SELL"),   # 不一致
            2: SecurityDecision(symbol_id=2, action="HOLD"),
            3: SecurityDecision(symbol_id=3, action="HOLD"),   # 不一致
        },
    )
    rpt = compare_one_day(a, b, portfolio_id=77)
    assert rpt.portfolio_id == 77
    assert rpt.chain_a_id != rpt.chain_b_id
    assert rpt.universe_jaccard == 1.0
    # BUY↔SELL 和 SELL↔HOLD 都有不一致
    assert rpt.action_confusion["BUY"]["SELL"] == 1
    assert rpt.action_confusion["SELL"]["HOLD"] == 1
    # 同候选集 + 无特殊 reject_code → 默认归类 UNKNOWN_ENGINE_DIFF（P0 要清零）
    assert rpt.inconsistent_by_category.get("UNKNOWN_ENGINE_DIFF", 0) >= 2
    assert "MEMBER_SOURCE_DIFF" not in rpt.inconsistent_by_category


def test_g5_04b_chain_ids_are_distinct_across_portfolios():
    """同一日期的双跑报告必须能按组合隔离追溯。"""
    td = date(2026, 6, 1)
    a = DailyChainResult(chain="A", trade_date=td, universe_symbol_ids=[1])
    b = DailyChainResult(chain="B", trade_date=td, universe_symbol_ids=[1])
    first = compare_one_day(a, b, portfolio_id=1)
    second = compare_one_day(a, b, portfolio_id=2)
    assert first.chain_a_id != second.chain_a_id
    assert first.chain_b_id != second.chain_b_id


# ============================================================================
# T_G5_05: 候选集不同（B 新增）→ MEMBER_SOURCE_DIFF 归因，不进 P0
# ============================================================================
def test_g5_05_universe_diff_triggers_member_source_category():
    """B 比 A 多一个标的 → 归类 MEMBER_SOURCE_DIFF，P0 计数为 0。

    注意：公共交集（1,2）两边动作必须一致，避免引入无关 UNKNOWN_ENGINE_DIFF 影响 P0 断言。
    """
    td = date(2026, 6, 1)
    a_syms, b_syms = [1, 2], [1, 2, 99]
    a = DailyChainResult(
        chain="A", trade_date=td, universe_symbol_ids=a_syms,
        decisions={s: SecurityDecision(symbol_id=s, action="HOLD") for s in a_syms},
    )
    # 公共集合 [1,2] 两边都 HOLD（一致）；仅 99 号是 B 独有的 BUY（触发 MEMBER_SOURCE_DIFF）
    b_decs = {s: SecurityDecision(symbol_id=s, action="HOLD") for s in a_syms}
    b_decs[99] = SecurityDecision(symbol_id=99, action="BUY")
    b = DailyChainResult(
        chain="B", trade_date=td, universe_symbol_ids=b_syms,
        decisions=b_decs,
    )
    rpt = compare_one_day(a, b)
    assert rpt.universe_added == [99]
    assert rpt.universe_removed == []
    # 仅 99 号一个差异，归类为 MEMBER_SOURCE_DIFF
    assert rpt.inconsistent_by_category.get("MEMBER_SOURCE_DIFF", 0) == 1
    # 交集动作一致，不产生 UNKNOWN → P0 = 0
    assert rpt.p0_unexplained_count == 0
    # 动作匹配率：2 个一致 + 1 个 99 算不一致 = 2/3 ≈ 0.667
    assert abs(rpt.action_match_rate - 2 / 3) < 0.001


# ============================================================================
# T_G5_06: A=DATA_BLOCKED, B=正常 → SCORE_DATA_GAP 归因
# ============================================================================
def test_g5_06_data_blocked_vs_normal_score_gap_category():
    td = date(2026, 6, 1)
    syms = [7]
    a = DailyChainResult(chain="A", trade_date=td, universe_symbol_ids=syms, decisions={
        7: SecurityDecision(symbol_id=7, action="DATA_BLOCKED",
                            reject_reason_code="DATA_BLOCKED__SCORE_MISSING"),
    })
    b = DailyChainResult(chain="B", trade_date=td, universe_symbol_ids=syms, decisions={
        7: SecurityDecision(symbol_id=7, action="BUY", score_value=0.81),
    })
    rpt = compare_one_day(a, b)
    cat = rpt.inconsistent_by_category
    assert cat.get("SCORE_DATA_GAP", 0) >= 1
    # SCORE_DATA_GAP 不进 P0
    assert rpt.p0_unexplained_count == 0


# ============================================================================
# T_G5_07: 一边 hard_gate blocked → HARD_GATE_INTERVENTION 归因
# ============================================================================
def test_g5_07_hard_gate_blocked_category():
    td = date(2026, 6, 1)
    syms = [10, 11]
    a = DailyChainResult(
        chain="A", trade_date=td, universe_symbol_ids=syms,
        decisions={s: SecurityDecision(symbol_id=s, action="NO_ACTION") for s in syms},
        blocking_status="READY",
    )
    b = DailyChainResult(
        chain="B", trade_date=td, universe_symbol_ids=syms,
        decisions={s: SecurityDecision(symbol_id=s, action="DATA_BLOCKED") for s in syms},
        blocking_status="BLOCKED", blocking_reasons=["SCHEDULE_TOO_EARLY"],
    )
    rpt = compare_one_day(a, b)
    cat = rpt.inconsistent_by_category
    assert cat.get("HARD_GATE_INTERVENTION", 0) >= 1
    assert rpt.p0_unexplained_count == 0


# ============================================================================
# T_G5_08: 20 日合成 + 3 天 P0/P1 差异 → failing_days 正确
# ============================================================================
def test_g5_08_failing_days_summary_reports_p0_p1_correctly():
    custom_dates = [date(2026, 7, d) for d in range(1, 31) if date(2026, 7, d).weekday() < 5][:20]
    assert len(custom_dates) == 20

    p0_days_idx = {2, 5, 17}   # 第 3/6/18 个交易日触发 P0
    p1_days_idx = {9, 15}      # 第 10/16 个交易日触发 P1

    base_universe = list(range(1, 21))  # 20 个标的

    def custom_runner(td: date, chain: str):
        # 查当前是第几天
        idx = custom_dates.index(td) if td in custom_dates else -1
        decs: dict[int, SecurityDecision] = {}
        # 默认动作 = HOLD
        for sym_id in base_universe:
            decs[sym_id] = SecurityDecision(symbol_id=sym_id, action="HOLD")
        # 构造 P0 差异：BUY vs SELL + 无明确原因码（触发 UNKNOWN）
        if idx in p0_days_idx:
            if chain == "A":
                decs[5] = SecurityDecision(symbol_id=5, action="BUY")
            else:
                decs[5] = SecurityDecision(symbol_id=5, action="SELL")
        # 构造 P1 差异：HOLD ↔ NO_ACTION
        if idx in p1_days_idx:
            if chain == "A":
                decs[7] = SecurityDecision(symbol_id=7, action="HOLD")
            else:
                decs[7] = SecurityDecision(symbol_id=7, action="NO_ACTION")
        return DailyChainResult(
            chain=chain, trade_date=td,
            universe_symbol_ids=list(base_universe),
            decisions=decs, blocking_status="READY",
        )

    s = run_g5_dual_run_replay(
        portfolio_id=99, chain_runner=custom_runner, explicit_dates=custom_dates,
    )
    assert s.days_replayed == 20
    assert len(s.failing_days_p0) == 3
    assert len(s.failing_days_p1) == 2
    # P0/P1 叠加的交易日应当同时出现在两边（不要求，但单独计数正确即可）
    assert s.total_p0_unexplained == 3
    assert s.total_p1_hold_noaction_flip == 2
    assert s.g5_eligible_for_g6 is False
    assert any("G5 未通过" in n for n in s.summary_notes)


# ============================================================================
# T_G5_09: 6x6 混淆矩阵对角线 = 一致数
# ============================================================================
def test_g5_09_confusion_matrix_diagonal_sums_to_matches():
    cm = build_confusion_matrix_6x6()
    assert set(cm.keys()) == set(ACTION_TYPES)
    for a in ACTION_TYPES:
        assert set(cm[a].keys()) == set(ACTION_TYPES)
    # 构造：3 BUY→BUY，2 SELL→SELL，1 NO_ACTION→NO_ACTION，1 SELL→NO_ACTION
    #       → diagonal = 6，非对角 = 1
    cm["BUY"]["BUY"] = 3
    cm["SELL"]["SELL"] = 2
    cm["NO_ACTION"]["NO_ACTION"] = 1
    cm["SELL"]["NO_ACTION"] = 1
    total = sum(sum(row.values()) for row in cm.values())
    diagonal = sum(cm[x][x] for x in ACTION_TYPES)
    assert total == 7
    assert diagonal == 6


# ============================================================================
# T_G5_10: G5ReplaySummary.as_dict() JSON 可序列化（含 ISO 日期）
# ============================================================================
def test_g5_10_summary_as_dict_json_roundtrip():
    universe = [1, 2]
    runner = build_synthetic_chain_runner(
        seed_portfolio_id=1, universe_a=universe, universe_b=universe, always_pass=True,
    )
    s = run_g5_dual_run_replay(
        portfolio_id=1, chain_runner=runner,
        anchor_date=date(2026, 8, 14), n_days=12,
    )
    d = s.as_dict()
    serialized = json.dumps(d)
    back = json.loads(serialized)
    assert back["portfolio_id"] == 1
    assert back["total_days"] == 12
    assert isinstance(back["daily_reports"], list) and len(back["daily_reports"]) == 12
    # 日期都转成 ISO string 了，反序列化回去应该成功
    date.fromisoformat(back["start_date"])
    date.fromisoformat(back["end_date"])
    for dr in back["daily_reports"]:
        date.fromisoformat(dr["trade_date"])


def test_g5_11_failed_day_cannot_unlock_g6():
    """即便剩余日期达到 10 天，窗口内漏跑也不能准入 G6。"""
    dates = [date(2026, 7, d) for d in range(1, 16) if date(2026, 7, d).weekday() < 5]
    runner = build_synthetic_chain_runner(
        seed_portfolio_id=1, universe_a=[1, 2], universe_b=[1, 2], always_pass=True,
    )
    failed_date = dates[2]

    def flaky_runner(td: date, chain: str):
        if td == failed_date and chain == "A":
            raise RuntimeError("fixture engine failure")
        return runner(td, chain)

    summary = run_g5_dual_run_replay(
        portfolio_id=1, chain_runner=flaky_runner, explicit_dates=dates,
    )
    assert summary.days_replayed >= 10
    assert summary.skipped_days == [failed_date]
    assert summary.g5_eligible_for_g6 is False
