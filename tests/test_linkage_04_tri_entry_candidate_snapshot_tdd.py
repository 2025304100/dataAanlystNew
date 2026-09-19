"""portfolio-factor-backtest-full-linkage 阻塞点 #4：三入口候选池快照（RED TDD 先失败）。

Seam（纯函数）：
    from app.services.tri_entry_candidate_snapshot import (
        EntryPoint,                     # Literal["dry_run", "backtest", "auto_simulation"]
        pick_candidate_snapshot_rows(
            candidate_rows: list[PortfolioCandidateLike],
            entry_point: EntryPoint,
            *,
            today: date,                         # dry_run 用的"今天"
            backtest_trade_date: date | None,    # backtest 当日交易日（必填，否则 ValueError）
            auto_sim_trade_date: date | None,    # auto_simulation 当日交易日（必填，否则 ValueError）
        ) -> list[PortfolioCandidateLike]:

Pf-linkage tasks.md N5（三入口候选池快照按对应日期）：
  ① dry_run（前台手动"试跑"入口）       → 用 today 的候选快照（今天仍有效）
  ② backtest（历史回测）                 → 用 backtest_trade_date 当日的候选快照（必须 SCD2 精确到当日；
                                           不能用 today 的，正式 PIT 会 BLOCKED）
  ③ auto_simulation（每日 20:30 自动跑） → 用 auto_sim_trade_date 的候选快照（不是今天的成员）

SCD2 选取规则：对每个 symbol_id，选 effective_from ≤ trade_date 且（effective_to IS NULL 或 effective_to ≥ trade_date）
              再对同日 audit_version 选最大的版本（与 linkage #1 的 SCD2 规则一致）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pytest


@dataclass
class CandidateScd2Fx:
    portfolio_id: int
    symbol_id: int
    effective_from: date
    effective_to: date | None
    removed_manually_flag: int = 0
    audit_version: int = 1
    row_tag: str = ""   # 方便断言"拿到的是哪一行"


def _row(pid, sid, ef, et, *, rm=0, v=1, tag=""):
    return CandidateScd2Fx(
        portfolio_id=pid, symbol_id=sid, effective_from=ef, effective_to=et,
        removed_manually_flag=rm, audit_version=v, row_tag=tag,
    )


class TestLinkage4EntryPointContract:
    def test_linkage_4_entry_point_enum_literal_3_values(self):
        from app.services.tri_entry_candidate_snapshot import EntryPoint
        try:
            vals = set(EntryPoint.__args__)
        except AttributeError:
            vals = {e.value for e in EntryPoint}  # type: ignore[union-attr]
        assert vals == {"dry_run", "backtest", "auto_simulation"}


class TestLinkage4DryRunUsesToday:
    def test_linkage_4_dry_run_today_active_symbol_picked(self):
        """today=2026-01-15；symbol 100 候选 [2025-06-01, ∞) → 被选中。"""
        from app.services.tri_entry_candidate_snapshot import pick_candidate_snapshot_rows
        today = date(2026, 1, 15)
        rows = [
            _row(1, 100, date(2025, 6, 1), None, tag="always-active-s100"),
            _row(1, 101, date(2025, 6, 1), date(2025, 12, 31), tag="expired-s101"),
            _row(1, 102, date(2026, 2, 1), None, tag="future-s102"),
        ]
        picked = pick_candidate_snapshot_rows(rows, "dry_run", today=today)
        ids = {r.symbol_id for r in picked}
        assert ids == {100}
        assert picked[0].row_tag == "always-active-s100"

    def test_linkage_4_dry_run_backtest_date_kwarg_is_ignored_even_if_provided(self):
        """dry_run 只认 today；即使多传 backtest_trade_date=2025-11-01 也应该 still = today-based。
        （否则 = 漏洞：用户可在 dry_run 用历史日期绕过 today 的候选门禁）
        """
        from app.services.tri_entry_candidate_snapshot import pick_candidate_snapshot_rows
        today = date(2026, 1, 15)
        rows = [
            _row(1, 100, date(2025, 6, 1), date(2025, 12, 31), tag="expired 2025-12-31"),
            _row(1, 200, date(2026, 1, 10), None, tag="2026-01-10~"),
        ]
        picked = pick_candidate_snapshot_rows(rows, "dry_run", today=today,
                                              backtest_trade_date=date(2025, 11, 1))
        ids = {r.symbol_id for r in picked}
        # 200 今天在；100 今天已过期 → 只能有 200
        assert ids == {200}


class TestLinkage4BacktestUsesPreciseTradeDate:
    def test_linkage_4_backtest_missing_trade_date_raises(self):
        from app.services.tri_entry_candidate_snapshot import pick_candidate_snapshot_rows
        with pytest.raises((ValueError, TypeError)):
            pick_candidate_snapshot_rows([], "backtest", today=date(2026, 1, 15))

    def test_linkage_4_backtest_only_picks_that_day(self):
        """backtest_trade_date=2025-04-15。只选在当日仍有效的 symbol。"""
        from app.services.tri_entry_candidate_snapshot import pick_candidate_snapshot_rows
        bt = date(2025, 4, 15)
        today = date(2026, 1, 15)
        rows = [
            # s100: 2025 全年有效
            _row(1, 100, date(2025, 1, 1), date(2025, 12, 31), tag="2025 all year"),
            # s200: 2024-01-01 ~ 2025-03-31（4 月前）
            _row(1, 200, date(2024, 1, 1), date(2025, 3, 31), tag="expired April"),
            # s300: 2025-05-01 起（4 月还未来）
            _row(1, 300, date(2025, 5, 1), None, tag="future May"),
            # s400: 2026-01-10 起（今天在，但 backtest 日太早）
            _row(1, 400, date(2026, 1, 10), None, tag="today only"),
        ]
        picked = pick_candidate_snapshot_rows(rows, "backtest", today=today, backtest_trade_date=bt)
        ids = {r.symbol_id for r in picked}
        assert ids == {100}

    def test_linkage_4_backtest_same_day_two_versions_latest_wins(self):
        """SCD2 同日 audit_version=1(移除) v.s. v2（恢复）→ backtest 日只拿 v2 行。"""
        from app.services.tri_entry_candidate_snapshot import pick_candidate_snapshot_rows
        bt = date(2025, 4, 15)
        rows = [
            CandidateScd2Fx(1, 999, bt, None, 1, 1, "v1 removed"),
            CandidateScd2Fx(1, 999, bt, None, 0, 2, "v2 restored"),
        ]
        picked = pick_candidate_snapshot_rows(rows, "backtest", today=bt, backtest_trade_date=bt)
        assert len(picked) == 1
        assert picked[0].audit_version == 2
        assert picked[0].row_tag == "v2 restored"


class TestLinkage4AutoSimUsesTradeDate:
    def test_linkage_4_auto_sim_missing_trade_date_raises(self):
        from app.services.tri_entry_candidate_snapshot import pick_candidate_snapshot_rows
        with pytest.raises((ValueError, TypeError)):
            pick_candidate_snapshot_rows([], "auto_simulation", today=date(2026, 1, 15))

    def test_linkage_4_auto_sim_date_not_equal_today_picks_sim_date(self):
        """典型场景：2026-01-16 凌晨重算 2026-01-15 的 auto_simulation。today ≠ auto_sim_trade_date。
        应使用 auto_sim_trade_date，不是 today。"""
        from app.services.tri_entry_candidate_snapshot import pick_candidate_snapshot_rows
        today = date(2026, 1, 16)
        sim = date(2026, 1, 15)
        rows = [
            # symbol 50：1 月 15 日之前是候选，16 日被移除（effective_to=15）
            _row(1, 50, date(2025, 1, 1), date(2026, 1, 15), tag="good on 15, removed on 16"),
            # symbol 51：1 月 16 日刚加入（future 15）
            _row(1, 51, date(2026, 1, 16), None, tag="added 16"),
        ]
        picked = pick_candidate_snapshot_rows(rows, "auto_simulation", today=today, auto_sim_trade_date=sim)
        ids = {r.symbol_id for r in picked}
        # 15 日时 50 在池中；51 还没入池
        assert ids == {50}
