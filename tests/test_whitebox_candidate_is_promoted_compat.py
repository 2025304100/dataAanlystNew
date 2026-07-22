"""白盒测试 - WP3.3 候选状态兼容（is_promoted 双轨兼容）。

覆盖 spec line 192-196 Scenario "候选状态兼容"：
- 第一阶段继续保留 `discovery_candidates.is_promoted`，由统一服务同步更新
- 历史 `is_promoted` 候选仍可正常显示

验证项（参照 checklist.md line 147-148）：
1. transition_candidate_to_observation 同步 is_promoted=True
2. transition_candidate_to_portfolio 同步 is_promoted=True
3. 双击幂等不重复更新 is_promoted（仍为 True，不报错）
4. 历史 is_promoted=True 候选仍可正常显示
5. 未晋升候选正常显示（is_promoted=False）
6. exclude_candidate 不影响 is_promoted（排除不取消已晋升标记）
7. 候选响应 dict 含 is_promoted 字段（API 契约）

测试用 SQLite 内存库（db_session fixture），每个用例独立 session。
"""
from __future__ import annotations

import pytest

from app.models.discovery_candidate import DiscoveryCandidate
from app.models.portfolio import Portfolio
from app.models.scan import ScanRun
from app.models.symbol import Symbol
from app.models.universe import UniverseSymbol
from app.models.watchlist import Watchlist
from app.services import opportunity_transitions as trans_svc
from app.services.candidate_promote import list_candidates


pytestmark = pytest.mark.whitebox


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------


def _make_watchlist(db_session, name: str = "QA-Compat-WL") -> Watchlist:
    wl = Watchlist(name=name, list_type="custom", description="qa is_promoted compat")
    db_session.add(wl)
    db_session.commit()
    db_session.refresh(wl)
    return wl


def _make_symbol(db_session, symbol: str = "600500") -> Symbol:
    sym = Symbol(
        symbol=symbol,
        name=f"测试-{symbol}",
        asset_type="stock",
        market="cn",
        theme="测试",
    )
    db_session.add(sym)
    db_session.commit()
    db_session.refresh(sym)
    return sym


def _make_portfolio(db_session, name: str = "QA-Compat-PF") -> Portfolio:
    pf = Portfolio(
        name=name,
        account_type="cash",
        total_capital=100000.0,
        investable_ratio=0.8,
        cash_reserve_ratio=0.2,
    )
    db_session.add(pf)
    db_session.commit()
    db_session.refresh(pf)
    return pf


def _make_scan_run(db_session, name: str = "QA-Compat-Scan") -> ScanRun:
    run = ScanRun(
        run_name=name,
        scope_snapshot="cn_stock",
        status="done",
    )
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)
    return run


def _make_universe_symbol(
    db_session, symbol: str = "600500"
) -> UniverseSymbol:
    u = UniverseSymbol(
        symbol=symbol,
        name=f"基础-{symbol}",
        asset_type="stock",
        market="sh",
        region="cn",
    )
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


def _make_candidate(
    db_session,
    *,
    scan_run_id: int,
    universe_symbol_id: int,
    symbol: str = "600500",
    name: str = "QA-Compat-Candidate",
    quality_score: float = 78.0,
    timing_score: float = 72.0,
    priority_score: float = 82.5,
    stage: str = "hold",
    action: str = "buy",
    is_promoted: int = 0,
) -> DiscoveryCandidate:
    c = DiscoveryCandidate(
        scan_run_id=scan_run_id,
        universe_symbol_id=universe_symbol_id,
        symbol=symbol,
        name=name,
        asset_type="stock",
        quality_score=quality_score,
        timing_score=timing_score,
        priority_score=priority_score,
        stage=stage,
        action=action,
        is_promoted=is_promoted,
    )
    db_session.add(c)
    db_session.commit()
    db_session.refresh(c)
    return c


def _setup_candidate_with_symbol(
    db_session, *, symbol: str = "600500", is_promoted: int = 0
) -> tuple[DiscoveryCandidate, Symbol, Watchlist]:
    """快速构造一套候选 + 标的 + 观察池测试数据。"""
    sym = _make_symbol(db_session, symbol=symbol)
    run = _make_scan_run(db_session, name=f"QA-Scan-{symbol}")
    u = _make_universe_symbol(db_session, symbol=symbol)
    candidate = _make_candidate(
        db_session,
        scan_run_id=run.id,
        universe_symbol_id=u.id,
        symbol=symbol,
        is_promoted=is_promoted,
    )
    wl = _make_watchlist(db_session, name=f"QA-WL-{symbol}")
    return candidate, sym, wl


# ----------------------------------------------------------------------------
# 1. transition_candidate_to_observation 同步 is_promoted
# ----------------------------------------------------------------------------


def test_transition_candidate_to_observation_syncs_is_promoted(db_session):
    """【WP3.3】候选加入观察池后 is_promoted 应同步为 1。"""
    candidate, _sym, wl = _setup_candidate_with_symbol(db_session, symbol="600601")
    assert candidate.is_promoted == 0

    trans_svc.transition_candidate_to_observation(
        db_session,
        candidate_id=candidate.id,
        watchlist_id=wl.id,
    )

    db_session.refresh(candidate)
    assert candidate.is_promoted == 1
    assert candidate.promoted_at is not None


# ----------------------------------------------------------------------------
# 2. transition_candidate_to_portfolio 同步 is_promoted
# ----------------------------------------------------------------------------


def test_transition_candidate_to_portfolio_syncs_is_promoted(db_session):
    """【WP3.3】候选直接加入组合后 is_promoted 应同步为 1。"""
    candidate, _sym, _wl = _setup_candidate_with_symbol(db_session, symbol="600602")
    pf = _make_portfolio(db_session, name="QA-PF-Sync-Portfolio")
    assert candidate.is_promoted == 0

    trans_svc.transition_candidate_to_portfolio(
        db_session,
        candidate_id=candidate.id,
        portfolio_id=pf.id,
    )

    db_session.refresh(candidate)
    assert candidate.is_promoted == 1
    assert candidate.promoted_at is not None


# ----------------------------------------------------------------------------
# 3. 双击幂等不重复更新 is_promoted
# ----------------------------------------------------------------------------


def test_double_click_idempotent_is_promoted_stays_true(db_session):
    """【WP3.3】双击幂等：第二次调用不报错，is_promoted 仍为 1。"""
    candidate, _sym, wl = _setup_candidate_with_symbol(db_session, symbol="600603")

    # 第一次调用
    trans_svc.transition_candidate_to_observation(
        db_session,
        candidate_id=candidate.id,
        watchlist_id=wl.id,
    )
    db_session.refresh(candidate)
    assert candidate.is_promoted == 1
    first_promoted_at = candidate.promoted_at

    # 第二次调用（幂等，应不报错）
    item2, event2 = trans_svc.transition_candidate_to_observation(
        db_session,
        candidate_id=candidate.id,
        watchlist_id=wl.id,
    )

    # 幂等：返回已有观察项，无新事件
    assert item2 is not None
    assert event2 is None

    # is_promoted 仍为 1，不重复更新（promoted_at 不变）
    db_session.refresh(candidate)
    assert candidate.is_promoted == 1
    assert candidate.promoted_at == first_promoted_at


# ----------------------------------------------------------------------------
# 4. 历史 is_promoted 候选仍可正常显示
# ----------------------------------------------------------------------------


def test_historical_promoted_candidate_visible_in_list(db_session):
    """【WP3.3】【spec Scenario "候选状态兼容"】历史 is_promoted=1 候选仍可正常显示。"""
    candidate, _sym, _wl = _setup_candidate_with_symbol(
        db_session, symbol="600604", is_promoted=1
    )

    rows = list_candidates(db_session, scan_run_id=candidate.scan_run_id)

    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == candidate.id
    assert row["is_promoted"] == 1
    assert row["promoted_at"] is None or isinstance(row["promoted_at"], str)


# ----------------------------------------------------------------------------
# 5. 未晋升候选正常显示
# ----------------------------------------------------------------------------


def test_unpromoted_candidate_visible_in_list(db_session):
    """【WP3.3】未晋升候选 is_promoted=0 也能正常显示。"""
    candidate, _sym, _wl = _setup_candidate_with_symbol(
        db_session, symbol="600605", is_promoted=0
    )

    rows = list_candidates(db_session, scan_run_id=candidate.scan_run_id)

    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == candidate.id
    assert row["is_promoted"] == 0


# ----------------------------------------------------------------------------
# 6. exclude_candidate 不影响 is_promoted
# ----------------------------------------------------------------------------


def test_exclude_candidate_does_not_reset_is_promoted(db_session):
    """【WP3.3】exclude_candidate 不取消已晋升标记（排除只记审计事件，不改 is_promoted）。"""
    candidate, _sym, _wl = _setup_candidate_with_symbol(
        db_session, symbol="600606", is_promoted=1
    )

    trans_svc.exclude_candidate(
        db_session,
        candidate_id=candidate.id,
        reason="基本面恶化",
    )

    # 排除后 is_promoted 仍为 1（排除只是状态变化，不取消已晋升标记）
    db_session.refresh(candidate)
    assert candidate.is_promoted == 1


# ----------------------------------------------------------------------------
# 7. 候选响应 dict 含 is_promoted 字段（API 契约）
# ----------------------------------------------------------------------------


def test_candidate_response_dict_contains_is_promoted_field(db_session):
    """【WP3.3】list_candidates 返回的 dict 必须含 is_promoted 字段。

    spec line 194 硬约束："第一阶段继续保留 `discovery_candidates.is_promoted`"。
    后端无 Pydantic schema，dict 字段即 API 契约，需明确断言字段存在。
    """
    candidate, _sym, _wl = _setup_candidate_with_symbol(
        db_session, symbol="600607", is_promoted=0
    )

    rows = list_candidates(db_session, scan_run_id=candidate.scan_run_id)

    assert len(rows) == 1
    row = rows[0]
    # API 契约：必须含 is_promoted 字段
    assert "is_promoted" in row, "list_candidates 返回的 dict 必须含 is_promoted 字段"
    # 字段值类型正确（int 0/1，历史数据可能为 None）
    assert row["is_promoted"] in (0, 1, None)
    # 同时应含 promoted_at 字段（兼容前端展示）
    assert "promoted_at" in row
