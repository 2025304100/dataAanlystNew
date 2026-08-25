"""portfolio-factor-backtest-full-linkage 阻塞点 #1：候选池三条后端约束（RED TDD 先失败）。

Seam（纯函数）：
    app.services.candidate_pool_rules.evaluate_candidate_buy_eligibility(
        *,
        portfolio_id: int,
        symbol_id: int,
        action: Literal["BUY", "SELL"],
        candidate_rows: list[PortfolioCandidateLike],     # SCD2 候选历史（PortfolioCandidate 原型或 dataclass 都行）
        current_position_qty: int | float,                # 当前实际持仓股数（0=已清仓）
        trade_date: datetime.date,                        # 本次动作的"视角交易日"
        *,
        require_restore_operation_for_removed: bool = True,
    ) -> CandidateBuyEligibilityResult:

CandidateBuyEligibilityResult 契约：
    allowed: bool                                      # true = 放行到下一环节；false = 必须 REJECTED
    rejection_reason: Literal[None, "OUTSIDE_CANDIDATE_POOL", "MANUALLY_REMOVED_BLOCKED"]
    decision_evidence_meta: dict[str, Any]             # 可塞进 DecisionEvidence.metadata_json 的审计信息

3 条需求在 portfolio-factor-backtest-full-linkage 的 tasks.md 前 4 条 PENDING 子任务：
    (N1) 候选池外证券 × BUY 动作 → REJECTED + OUTSIDE_CANDIDATE_POOL
    (N2) 清仓卖出 0 持仓，但 portfolio_candidates 该行仍在 → 下次信号达条件可重新 BUY
    (N3) 手动移除 removed_manually_flag=1 + removal_reason 写入 → 任何入口禁止再次 BUY，
         必须先通过"恢复候选资格"显式操作（require_restore_operation_for_removed=True 时强制执行）
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

import pytest


# ──────────────────────────────────────────────────────────────────────
# 与 PortfolioCandidate 字段同形（不依赖 ORM，纯数据类，seam 接受即可）
@dataclass
class _CandidateRowFixture:
    portfolio_id: int
    symbol_id: int
    effective_from: date
    effective_to: date | None
    removed_manually_flag: int = 0
    removal_reason: str | None = None
    auto_authorized_flag: int = 1
    audit_version: int = 1


def _r(pid: int, sid: int, ef: date, et: date | None,
       rm: int = 0, rr: str | None = None, aa: int = 1) -> _CandidateRowFixture:
    return _CandidateRowFixture(
        portfolio_id=pid, symbol_id=sid,
        effective_from=ef, effective_to=et,
        removed_manually_flag=rm, removal_reason=rr, auto_authorized_flag=aa,
    )


class TestLinkage1OutsideCandidateBuyReject:
    """N1: 候选池外证券 × BUY → OUTSIDE_CANDIDATE_POOL 强制拒绝。"""

    def test_linkage_1_n1_buy_symbol_not_any_candidate_row_rejected_outside_pool(self):
        from app.services.candidate_pool_rules import (
            evaluate_candidate_buy_eligibility,
        )
        res = evaluate_candidate_buy_eligibility(
            portfolio_id=42, symbol_id=999999, action="BUY",
            candidate_rows=[],  # 完全没有候选
            current_position_qty=0, trade_date=date(2026, 1, 15),
        )
        assert res.allowed is False
        assert res.rejection_reason == "OUTSIDE_CANDIDATE_POOL"

    def test_linkage_1_n1_buy_candidate_effective_from_future_means_outside_today(self):
        """候选从明天才生效 → 今天视角仍是候选池外。"""
        from app.services.candidate_pool_rules import (
            evaluate_candidate_buy_eligibility,
        )
        today = date(2026, 1, 15)
        rows = [_r(42, 1001, ef=today.__class__(2026, 1, 16), et=None)]  # 明日生效
        res = evaluate_candidate_buy_eligibility(
            portfolio_id=42, symbol_id=1001, action="BUY",
            candidate_rows=rows,
            current_position_qty=0, trade_date=today,
        )
        assert res.allowed is False
        assert res.rejection_reason == "OUTSIDE_CANDIDATE_POOL"

    def test_linkage_1_n1_buy_candidate_effective_to_passed_means_outside(self):
        """历史上是候选，但 effective_to 已 < trade_date → 已过期不算在池。"""
        from app.services.candidate_pool_rules import (
            evaluate_candidate_buy_eligibility,
        )
        today = date(2026, 1, 15)
        rows = [_r(42, 1002, ef=date(2025, 1, 1), et=date(2025, 12, 31))]
        res = evaluate_candidate_buy_eligibility(
            portfolio_id=42, symbol_id=1002, action="BUY",
            candidate_rows=rows, current_position_qty=0, trade_date=today,
        )
        assert res.allowed is False
        assert res.rejection_reason == "OUTSIDE_CANDIDATE_POOL"

    def test_linkage_1_n1_sell_action_does_not_need_candidate(self):
        """SELL 不经过 BUY 门禁（无订单禁止卖出不现实），SELL 路径直接 allowed。"""
        from app.services.candidate_pool_rules import (
            evaluate_candidate_buy_eligibility,
        )
        res = evaluate_candidate_buy_eligibility(
            portfolio_id=42, symbol_id=999999, action="SELL",
            candidate_rows=[], current_position_qty=1000, trade_date=date(2026, 1, 15),
        )
        assert res.allowed is True
        assert res.rejection_reason is None


class TestLinkage1ZeroHoldingRebuyAllowed:
    """N2: 清仓 0 持仓 → 候选仍有效 → 可以重新 BUY。"""

    def test_linkage_1_n2_zero_qty_active_candidate_buy_allowed(self):
        from app.services.candidate_pool_rules import (
            evaluate_candidate_buy_eligibility,
        )
        today = date(2026, 1, 15)
        rows = [_r(42, 2001, ef=date(2025, 6, 1), et=None)]
        res = evaluate_candidate_buy_eligibility(
            portfolio_id=42, symbol_id=2001, action="BUY",
            candidate_rows=rows, current_position_qty=0, trade_date=today,
        )
        assert res.allowed is True
        assert res.rejection_reason is None
        # decision_evidence_meta 要包含"候选 SCD2 生效日"作为审计信息（便于 T-C8 回溯）
        meta: dict[str, Any] = res.decision_evidence_meta or {}
        assert meta.get("candidate_effective_from") == date(2025, 6, 1).isoformat()

    def test_linkage_1_n2_already_holding_buy_naturally_allowed(self):
        """已持仓 + 继续加仓 BUY 当然允许（回归：不能把 N2 误写成"仅零持仓允许买"）。"""
        from app.services.candidate_pool_rules import (
            evaluate_candidate_buy_eligibility,
        )
        rows = [_r(42, 2002, ef=date(2025, 6, 1), et=None)]
        res = evaluate_candidate_buy_eligibility(
            portfolio_id=42, symbol_id=2002, action="BUY",
            candidate_rows=rows, current_position_qty=500, trade_date=date(2026, 1, 15),
        )
        assert res.allowed is True
        assert res.rejection_reason is None


class TestLinkage1ManuallyRemovedBlocks:
    """N3: removed_manually_flag=1 → 任何入口都必须 REJECTED，除非显式走恢复操作。"""

    def test_linkage_1_n3_manually_removed_active_flag_blocks_buy_by_default(self):
        from app.services.candidate_pool_rules import (
            evaluate_candidate_buy_eligibility,
        )
        today = date(2026, 1, 15)
        rows = [_r(42, 3001, ef=date(2025, 6, 1), et=None,
                   rm=1, rr="MANUAL_REMOVE", aa=1)]
        res = evaluate_candidate_buy_eligibility(
            portfolio_id=42, symbol_id=3001, action="BUY",
            candidate_rows=rows, current_position_qty=0, trade_date=today,
        )
        assert res.allowed is False
        assert res.rejection_reason == "MANUALLY_REMOVED_BLOCKED"
        meta: dict[str, Any] = res.decision_evidence_meta or {}
        assert meta.get("removal_reason") == "MANUAL_REMOVE"

    def test_linkage_1_n3_removed_but_effective_row_expired_then_outside_not_manual(
        self,
    ):
        """移除 + 过期（同时满足"已出池"+"手动移除"）→ 应优先用 OUTSIDE（出池更根本原因）。"""
        from app.services.candidate_pool_rules import (
            evaluate_candidate_buy_eligibility,
        )
        today = date(2026, 1, 15)
        rows = [_r(42, 3002, ef=date(2025, 1, 1), et=date(2025, 10, 1),
                   rm=1, rr="MANUAL_REMOVE")]
        res = evaluate_candidate_buy_eligibility(
            portfolio_id=42, symbol_id=3002, action="BUY",
            candidate_rows=rows, current_position_qty=0, trade_date=today,
        )
        # 两条都满足：优先级 ① OUTSIDE_CANDIDATE_POOL（出池了，没理由再提"手动移除"）
        assert res.allowed is False
        assert res.rejection_reason == "OUTSIDE_CANDIDATE_POOL"

    def test_linkage_1_n3_restore_flag_can_unblock_if_user_explicitly_requires_false(
        self,
    ):
        """require_restore_operation_for_removed=False（仅"恢复候选资格"入口用）→ 即使 rm=1 也放行。"""
        from app.services.candidate_pool_rules import (
            evaluate_candidate_buy_eligibility,
        )
        today = date(2026, 1, 15)
        rows = [_r(42, 3003, ef=date(2025, 6, 1), et=None, rm=1, rr="MANUAL_REMOVE")]
        res = evaluate_candidate_buy_eligibility(
            portfolio_id=42, symbol_id=3003, action="BUY",
            candidate_rows=rows,
            current_position_qty=0, trade_date=today,
            require_restore_operation_for_removed=False,
        )
        assert res.allowed is True
        assert res.rejection_reason is None


class TestLinkage1Scd2Merge:
    """N4 附带：同日多次变更（audit_version=1,2,3）→ 只拿 当日最高 audit_version 判定。"""

    def test_linkage_1_n4_same_day_two_versions_latest_wins_removed_final(self):
        from app.services.candidate_pool_rules import (
            evaluate_candidate_buy_eligibility,
        )
        today = date(2026, 1, 15)
        rows = [
            # v1: 普通候选
            _CandidateRowFixture(42, 4001, today, None, 0, None, 1, 1),
            # v2: 同日下午用户手动移除
            _CandidateRowFixture(42, 4001, today, None, 1, "MANUAL_REMOVE", 1, 2),
        ]
        res = evaluate_candidate_buy_eligibility(
            portfolio_id=42, symbol_id=4001, action="BUY",
            candidate_rows=rows, current_position_qty=0, trade_date=today,
        )
        assert res.allowed is False
        assert res.rejection_reason == "MANUALLY_REMOVED_BLOCKED"

    def test_linkage_1_n4_same_day_two_versions_latest_wins_added_back(self):
        """v1 removed + v2 restored → 应允许 BUY。"""
        from app.services.candidate_pool_rules import (
            evaluate_candidate_buy_eligibility,
        )
        today = date(2026, 1, 15)
        rows = [
            _CandidateRowFixture(42, 4002, today, None, 1, "MANUAL_REMOVE", 1, 1),
            _CandidateRowFixture(42, 4002, today, None, 0, None, 1, 2),
        ]
        res = evaluate_candidate_buy_eligibility(
            portfolio_id=42, symbol_id=4002, action="BUY",
            candidate_rows=rows, current_position_qty=0, trade_date=today,
            require_restore_operation_for_removed=False,
        )
        assert res.allowed is True
        assert res.rejection_reason is None
