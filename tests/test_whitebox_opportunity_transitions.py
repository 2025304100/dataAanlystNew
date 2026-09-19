"""白盒测试 - WP3.2 机会状态流转统一领域服务。

覆盖 app/services/opportunity_transitions.py 的所有接口：
1. transition_candidate_to_observation 基本流程 + 双击幂等 + 流转原子性
2. transition_observation_to_portfolio
3. transition_candidate_to_portfolio
4. exclude_candidate / restore_candidate / expire_candidate
5. exclude_observation / restore_observation
6. member_archive_to_observation
7. get_transition_history 查询审计链
8. 幂等键唯一性 / 自定义幂等键 / actor_type / reason_json

测试用 SQLite 内存库（db_session fixture），每个用例独立 session。

参照 spec line 178-196 的 3 个 Scenario：
- 双击幂等：同一候选连续点击两次"加入观察"只产生 1 个观察项和 1 个成功事件
- 流转原子性：任一步骤异常时所有写入回滚，不出现半状态
- 候选状态兼容：继续保留 is_promoted，由统一服务同步更新
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.discovery_candidate import DiscoveryCandidate
from app.models.opportunity_transition_event import (
    ACTOR_SYSTEM,
    ACTOR_USER,
    EVENT_CANDIDATE_TO_OBSERVATION,
    EVENT_CANDIDATE_TO_PORTFOLIO,
    EVENT_EXCLUDE,
    EVENT_EXPIRE,
    EVENT_MEMBER_ARCHIVE_TO_OBSERVATION,
    EVENT_OBSERVATION_TO_PORTFOLIO,
    EVENT_RESTORE,
    OpportunityTransitionEvent,
    TYPE_CANDIDATE,
    TYPE_OBSERVATION,
    TYPE_PORTFOLIO_MEMBER,
)
from app.models.portfolio import Portfolio
from app.models.scan import ScanRun
from app.models.symbol import Symbol
from app.models.universe import UniverseSymbol
from app.models.watchlist import Watchlist, WatchlistItem
from app.services import opportunity_transitions as trans_svc


pytestmark = pytest.mark.whitebox


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------


def _make_watchlist(db_session, name: str = "QA-Trans-WL") -> Watchlist:
    wl = Watchlist(name=name, list_type="custom", description="qa transitions")
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


def _make_portfolio(db_session, name: str = "QA-Trans-PF") -> Portfolio:
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


def _make_scan_run(db_session, name: str = "QA-Trans-Scan") -> ScanRun:
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
    name: str = "QA-Trans-Candidate",
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
    db_session, *, symbol: str = "600500"
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
    )
    wl = _make_watchlist(db_session, name=f"QA-WL-{symbol}")
    return candidate, sym, wl


# ----------------------------------------------------------------------------
# 1. transition_candidate_to_observation 基本流程
# ----------------------------------------------------------------------------


def test_transition_candidate_to_observation_creates_item_and_event(db_session):
    """【WP3.2】候选加入观察池创建观察项 + 审计事件，并更新 is_promoted。"""
    candidate, sym, wl = _setup_candidate_with_symbol(db_session, symbol="600501")

    item, event = trans_svc.transition_candidate_to_observation(
        db_session,
        candidate_id=candidate.id,
        watchlist_id=wl.id,
        note="测试加入",
        priority=3,
        tags=["科技", "龙头"],
    )

    # 断言观察项创建
    assert item.id is not None
    assert item.watchlist_id == wl.id
    assert item.symbol_id == sym.id
    assert item.origin_type == "candidate"
    assert item.origin_id == candidate.id
    assert item.status == "watching"
    assert item.priority == 3
    assert item.note == "测试加入"
    assert json.loads(item.tags_json) == ["科技", "龙头"]

    # 断言审计事件创建
    assert event is not None
    assert event.event_type == EVENT_CANDIDATE_TO_OBSERVATION
    assert event.source_type == TYPE_CANDIDATE
    assert event.source_id == candidate.id
    assert event.target_type == TYPE_OBSERVATION
    assert event.target_id == wl.id
    assert event.from_status == "candidate"
    assert event.to_status == "observation"
    assert event.actor_type == ACTOR_USER
    assert event.idempotency_key is not None
    assert event.reason_json is not None
    reason = json.loads(event.reason_json)
    assert reason["source"] == "candidate"
    assert reason["candidate_id"] == candidate.id

    # 断言候选 is_promoted 已更新
    db_session.refresh(candidate)
    assert candidate.is_promoted == 1
    assert candidate.promoted_at is not None


# ----------------------------------------------------------------------------
# 2. 双击幂等（spec Scenario）
# ----------------------------------------------------------------------------


def test_double_click_idempotent(db_session):
    """【WP3.2】【spec Scenario "双击幂等"】同一候选连续点击两次只产生 1 个观察项和 1 个成功事件。"""
    candidate, _sym, wl = _setup_candidate_with_symbol(db_session, symbol="600502")

    # 第一次点击
    item1, event1 = trans_svc.transition_candidate_to_observation(
        db_session,
        candidate_id=candidate.id,
        watchlist_id=wl.id,
    )
    assert event1 is not None

    # 第二次点击（相同参数）
    item2, event2 = trans_svc.transition_candidate_to_observation(
        db_session,
        candidate_id=candidate.id,
        watchlist_id=wl.id,
    )

    # 断言：返回同一观察项，无新事件
    assert item2.id == item1.id
    assert event2 is None

    # 断言：DB 中只有 1 个观察项
    items = db_session.execute(
        select(WatchlistItem).where(WatchlistItem.watchlist_id == wl.id)
    ).scalars().all()
    assert len(items) == 1

    # 断言：DB 中只有 1 个审计事件
    events = db_session.execute(
        select(OpportunityTransitionEvent).where(
            OpportunityTransitionEvent.event_type == EVENT_CANDIDATE_TO_OBSERVATION
        )
    ).scalars().all()
    assert len(events) == 1


# ----------------------------------------------------------------------------
# 3. 流转原子性（spec Scenario）
# ----------------------------------------------------------------------------


def test_atomicity_rollback_on_event_creation_failure(monkeypatch, db_session):
    """【WP3.2】【spec Scenario "流转原子性"】审计事件创建失败时所有写入回滚，无半状态。"""
    candidate, _sym, wl = _setup_candidate_with_symbol(db_session, symbol="600503")

    # monkeypatch _create_event 抛异常（模拟审计事件写入失败）
    def _raise(*args, **kwargs):
        raise RuntimeError("simulated audit event failure")

    monkeypatch.setattr(trans_svc, "_create_event", _raise)

    # 调用应抛出 RuntimeError
    with pytest.raises(RuntimeError, match="simulated audit event failure"):
        trans_svc.transition_candidate_to_observation(
            db_session,
            candidate_id=candidate.id,
            watchlist_id=wl.id,
        )

    # 断言：无观察项被持久化
    items = db_session.execute(
        select(WatchlistItem).where(WatchlistItem.watchlist_id == wl.id)
    ).scalars().all()
    assert len(items) == 0, "失败后不应有观察项残留（半状态）"

    # 断言：无审计事件被持久化
    events = db_session.execute(select(OpportunityTransitionEvent)).scalars().all()
    assert len(events) == 0, "失败后不应有审计事件残留"

    # 断言：候选 is_promoted 未被更新（仍为 0）
    # expire_all 强制刷新 identity map 中的实例
    db_session.expire_all()
    fresh_candidate = db_session.get(DiscoveryCandidate, candidate.id)
    assert fresh_candidate is not None
    assert fresh_candidate.is_promoted == 0, "失败后候选 is_promoted 不应被更新"


def test_atomicity_rollback_on_commit_failure(monkeypatch, db_session):
    """【WP3.2】commit 失败时所有写入回滚。"""
    candidate, _sym, wl = _setup_candidate_with_symbol(db_session, symbol="600504")

    # 保存原始 commit
    original_commit = db_session.commit

    # 让 commit 在第一次调用时抛异常
    call_count = {"n": 0}

    def _failing_commit():
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated commit failure")
        return original_commit()

    monkeypatch.setattr(db_session, "commit", _failing_commit)

    with pytest.raises(RuntimeError, match="simulated commit failure"):
        trans_svc.transition_candidate_to_observation(
            db_session,
            candidate_id=candidate.id,
            watchlist_id=wl.id,
        )

    # 恢复原始 commit 以便后续查询
    monkeypatch.setattr(db_session, "commit", original_commit)

    # 断言：无观察项被持久化
    items = db_session.execute(
        select(WatchlistItem).where(WatchlistItem.watchlist_id == wl.id)
    ).scalars().all()
    assert len(items) == 0

    # 断言：无审计事件
    events = db_session.execute(select(OpportunityTransitionEvent)).scalars().all()
    assert len(events) == 0


# ----------------------------------------------------------------------------
# 4. transition_observation_to_portfolio
# ----------------------------------------------------------------------------


def test_transition_observation_to_portfolio(db_session):
    """【WP3.2】观察项加入组合：更新 target_portfolio_id + 创建审计事件。"""
    candidate, _sym, wl = _setup_candidate_with_symbol(db_session, symbol="600505")
    pf = _make_portfolio(db_session, name="QA-PF-Obs-Port")

    # 先候选加入观察池
    item, _ = trans_svc.transition_candidate_to_observation(
        db_session, candidate_id=candidate.id, watchlist_id=wl.id
    )

    # 观察项加入组合
    updated_item, event = trans_svc.transition_observation_to_portfolio(
        db_session,
        watchlist_item_id=item.id,
        portfolio_id=pf.id,
        execution_mode="manual",
        note="手动加入组合",
    )

    # 断言 target_portfolio_id 更新
    assert updated_item.target_portfolio_id == pf.id

    # 断言审计事件
    assert event is not None
    assert event.event_type == EVENT_OBSERVATION_TO_PORTFOLIO
    assert event.source_type == TYPE_OBSERVATION
    assert event.source_id == item.id
    assert event.target_type == TYPE_PORTFOLIO_MEMBER
    assert event.target_id == pf.id
    assert event.to_status == "portfolio_member"

    # 断言 reason 包含 execution_mode
    reason = json.loads(event.reason_json)
    assert reason["execution_mode"] == "manual"
    assert reason["note"] == "手动加入组合"


def test_transition_observation_to_portfolio_idempotent(db_session):
    """【WP3.2】观察项加入组合双击幂等。"""
    candidate, _sym, wl = _setup_candidate_with_symbol(db_session, symbol="600506")
    pf = _make_portfolio(db_session, name="QA-PF-Obs-Idem")

    item, _ = trans_svc.transition_candidate_to_observation(
        db_session, candidate_id=candidate.id, watchlist_id=wl.id
    )

    # 第一次加入组合
    __, event1 = trans_svc.transition_observation_to_portfolio(
        db_session, watchlist_item_id=item.id, portfolio_id=pf.id
    )
    assert event1 is not None

    # 第二次（幂等）
    __, event2 = trans_svc.transition_observation_to_portfolio(
        db_session, watchlist_item_id=item.id, portfolio_id=pf.id
    )
    assert event2 is None

    # DB 中只有 1 个 observation_to_portfolio 事件
    events = db_session.execute(
        select(OpportunityTransitionEvent).where(
            OpportunityTransitionEvent.event_type == EVENT_OBSERVATION_TO_PORTFOLIO
        )
    ).scalars().all()
    assert len(events) == 1


# ----------------------------------------------------------------------------
# 5. transition_candidate_to_portfolio
# ----------------------------------------------------------------------------


def test_transition_candidate_to_portfolio(db_session):
    """【WP3.2】候选直接加入组合：创建审计事件 + 更新 is_promoted。"""
    candidate, _sym, _wl = _setup_candidate_with_symbol(db_session, symbol="600507")
    pf = _make_portfolio(db_session, name="QA-PF-Cand-Port")

    obs_item, event = trans_svc.transition_candidate_to_portfolio(
        db_session,
        candidate_id=candidate.id,
        portfolio_id=pf.id,
        execution_mode="auto",
        note="自动加入",
    )

    # 不传 watchlist_id 时观察项为 None
    assert obs_item is None

    # 断言审计事件
    assert event is not None
    assert event.event_type == EVENT_CANDIDATE_TO_PORTFOLIO
    assert event.source_type == TYPE_CANDIDATE
    assert event.source_id == candidate.id
    assert event.target_type == TYPE_PORTFOLIO_MEMBER
    assert event.target_id == pf.id
    assert event.to_status == "portfolio_member"

    # 断言 reason
    reason = json.loads(event.reason_json)
    assert reason["execution_mode"] == "auto"
    assert reason["note"] == "自动加入"

    # 断言候选 is_promoted 已更新
    db_session.refresh(candidate)
    assert candidate.is_promoted == 1


def test_transition_candidate_to_portfolio_with_observation(db_session):
    """【WP3.2】候选加入组合同时加入观察池：创建两个事件 + 观察项。"""
    candidate, _sym, wl = _setup_candidate_with_symbol(db_session, symbol="600508")
    pf = _make_portfolio(db_session, name="QA-PF-Cand-Obs-Port")

    obs_item, event = trans_svc.transition_candidate_to_portfolio(
        db_session,
        candidate_id=candidate.id,
        portfolio_id=pf.id,
        watchlist_id=wl.id,
    )

    # 断言观察项创建
    assert obs_item is not None
    assert obs_item.origin_type == "candidate"
    assert obs_item.origin_id == candidate.id
    assert obs_item.target_portfolio_id == pf.id

    # 断言候选→组合事件
    assert event is not None
    assert event.event_type == EVENT_CANDIDATE_TO_PORTFOLIO

    # 断言候选→观察事件也创建（同事务）
    obs_events = db_session.execute(
        select(OpportunityTransitionEvent).where(
            OpportunityTransitionEvent.event_type == EVENT_CANDIDATE_TO_OBSERVATION
        )
    ).scalars().all()
    assert len(obs_events) == 1


# ----------------------------------------------------------------------------
# 6. exclude_candidate
# ----------------------------------------------------------------------------


def test_exclude_candidate(db_session):
    """【WP3.2】排除候选：创建审计事件。"""
    candidate, _sym, _wl = _setup_candidate_with_symbol(db_session, symbol="600509")

    event = trans_svc.exclude_candidate(
        db_session,
        candidate_id=candidate.id,
        reason="基本面恶化",
    )

    # 断言审计事件
    assert event is not None
    assert event.event_type == EVENT_EXCLUDE
    assert event.source_type == TYPE_CANDIDATE
    assert event.source_id == candidate.id
    assert event.to_status == "excluded"
    assert event.actor_type == ACTOR_USER

    # reason
    reason = json.loads(event.reason_json)
    assert reason["reason"] == "基本面恶化"


def test_exclude_candidate_idempotent(db_session):
    """【WP3.2】排除候选幂等：第二次调用返回 None。"""
    candidate, _sym, _wl = _setup_candidate_with_symbol(db_session, symbol="600510")

    event1 = trans_svc.exclude_candidate(
        db_session, candidate_id=candidate.id, reason="第一次"
    )
    assert event1 is not None

    event2 = trans_svc.exclude_candidate(
        db_session, candidate_id=candidate.id, reason="第二次"
    )
    assert event2 is None

    # DB 中只有 1 个 exclude 事件
    events = db_session.execute(
        select(OpportunityTransitionEvent).where(
            OpportunityTransitionEvent.event_type == EVENT_EXCLUDE,
            OpportunityTransitionEvent.source_type == TYPE_CANDIDATE,
        )
    ).scalars().all()
    assert len(events) == 1


# ----------------------------------------------------------------------------
# 7. restore_candidate
# ----------------------------------------------------------------------------


def test_restore_candidate(db_session):
    """【WP3.2】恢复排除的候选：创建审计事件。"""
    candidate, _sym, _wl = _setup_candidate_with_symbol(db_session, symbol="600511")

    # 先排除
    trans_svc.exclude_candidate(db_session, candidate_id=candidate.id)

    # 恢复
    event = trans_svc.restore_candidate(db_session, candidate_id=candidate.id)

    assert event is not None
    assert event.event_type == EVENT_RESTORE
    assert event.source_type == TYPE_CANDIDATE
    assert event.source_id == candidate.id
    assert event.to_status == "active"


# ----------------------------------------------------------------------------
# 8. expire_candidate
# ----------------------------------------------------------------------------


def test_expire_candidate(db_session):
    """【WP3.2】过期候选：系统触发，actor_type='system'。"""
    candidate, _sym, _wl = _setup_candidate_with_symbol(db_session, symbol="600512")

    event = trans_svc.expire_candidate(db_session, candidate_id=candidate.id)

    assert event is not None
    assert event.event_type == EVENT_EXPIRE
    assert event.source_type == TYPE_CANDIDATE
    assert event.source_id == candidate.id
    assert event.to_status == "expired"
    # 默认 actor_type='system'
    assert event.actor_type == ACTOR_SYSTEM


# ----------------------------------------------------------------------------
# 9. exclude_observation
# ----------------------------------------------------------------------------


def test_exclude_observation(db_session):
    """【WP3.2】排除观察项：归档 + 创建审计事件。"""
    candidate, _sym, wl = _setup_candidate_with_symbol(db_session, symbol="600513")

    # 先候选加入观察池
    item, _ = trans_svc.transition_candidate_to_observation(
        db_session, candidate_id=candidate.id, watchlist_id=wl.id
    )
    assert item.status == "watching"

    # 排除观察项
    event = trans_svc.exclude_observation(
        db_session, watchlist_item_id=item.id, reason="误加入"
    )

    # 断言观察项已归档
    db_session.refresh(item)
    assert item.status == "archived"
    assert item.archived_at is not None

    # 断言审计事件
    assert event is not None
    assert event.event_type == EVENT_EXCLUDE
    assert event.source_type == TYPE_OBSERVATION
    assert event.source_id == item.id
    assert event.to_status == "archived"
    assert event.from_status == "watching"


# ----------------------------------------------------------------------------
# 10. restore_observation
# ----------------------------------------------------------------------------


def test_restore_observation(db_session):
    """【WP3.2】恢复归档的观察项：status 恢复为 watching + 创建审计事件。"""
    candidate, _sym, wl = _setup_candidate_with_symbol(db_session, symbol="600514")

    # 候选加入观察池
    item, _ = trans_svc.transition_candidate_to_observation(
        db_session, candidate_id=candidate.id, watchlist_id=wl.id
    )

    # 排除
    trans_svc.exclude_observation(db_session, watchlist_item_id=item.id)
    db_session.refresh(item)
    assert item.status == "archived"

    # 恢复
    event = trans_svc.restore_observation(db_session, watchlist_item_id=item.id)

    # 断言观察项已恢复
    db_session.refresh(item)
    assert item.status == "watching"
    assert item.archived_at is None

    # 断言审计事件
    assert event is not None
    assert event.event_type == EVENT_RESTORE
    assert event.source_type == TYPE_OBSERVATION
    assert event.source_id == item.id
    assert event.to_status == "watching"
    assert event.from_status == "archived"


# ----------------------------------------------------------------------------
# 11. 幂等键唯一性（IntegrityError）
# ----------------------------------------------------------------------------


def test_idempotency_key_unique_constraint(db_session):
    """【WP3.2】相同 idempotency_key 的第二条事件抛 IntegrityError。"""
    event1 = OpportunityTransitionEvent(
        symbol_id=1,
        event_type=EVENT_CANDIDATE_TO_OBSERVATION,
        source_type=TYPE_CANDIDATE,
        source_id=100,
        target_type=TYPE_OBSERVATION,
        target_id=200,
        to_status="observation",
        idempotency_key="unique-key-001",
        actor_type=ACTOR_USER,
    )
    db_session.add(event1)
    db_session.commit()

    event2 = OpportunityTransitionEvent(
        symbol_id=999,  # 不同 symbol 仍应触发唯一约束
        event_type=EVENT_CANDIDATE_TO_OBSERVATION,
        source_type=TYPE_CANDIDATE,
        source_id=100,
        target_type=TYPE_OBSERVATION,
        target_id=200,
        to_status="observation",
        idempotency_key="unique-key-001",  # 相同幂等键
        actor_type=ACTOR_USER,
    )
    db_session.add(event2)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


# ----------------------------------------------------------------------------
# 12. get_transition_history 按 symbol_id 查询
# ----------------------------------------------------------------------------


def test_get_transition_history_by_symbol(db_session):
    """【WP3.2】按 symbol_id 查询审计链，返回多条事件按 created_at 降序。"""
    candidate, sym, wl = _setup_candidate_with_symbol(db_session, symbol="600515")
    pf = _make_portfolio(db_session, name="QA-PF-History")

    # 创建 3 条事件：候选→观察、观察→组合、排除
    item, _ = trans_svc.transition_candidate_to_observation(
        db_session, candidate_id=candidate.id, watchlist_id=wl.id
    )
    trans_svc.transition_observation_to_portfolio(
        db_session, watchlist_item_id=item.id, portfolio_id=pf.id
    )
    trans_svc.exclude_observation(db_session, watchlist_item_id=item.id)

    # 按 symbol_id 查询
    history = trans_svc.get_transition_history(db_session, symbol_id=sym.id)

    # 断言返回 3 条，按 created_at 降序（最新在前）
    assert len(history) == 3
    # 最新事件应是 exclude（最后创建的）
    assert history[0].event_type == EVENT_EXCLUDE
    # 第二个是 observation_to_portfolio
    assert history[1].event_type == EVENT_OBSERVATION_TO_PORTFOLIO
    # 最早是 candidate_to_observation
    assert history[2].event_type == EVENT_CANDIDATE_TO_OBSERVATION


# ----------------------------------------------------------------------------
# 13. get_transition_history 按 source 筛选
# ----------------------------------------------------------------------------


def test_get_transition_history_by_source(db_session):
    """【WP3.2】按 source_type + source_id 筛选审计链。"""
    candidate, sym, wl = _setup_candidate_with_symbol(db_session, symbol="600516")
    pf = _make_portfolio(db_session, name="QA-PF-Source")

    # 候选→观察（source=candidate）
    item, _ = trans_svc.transition_candidate_to_observation(
        db_session, candidate_id=candidate.id, watchlist_id=wl.id
    )
    # 观察→组合（source=observation）
    trans_svc.transition_observation_to_portfolio(
        db_session, watchlist_item_id=item.id, portfolio_id=pf.id
    )

    # 按 source_type='candidate' 筛选
    candidate_events = trans_svc.get_transition_history(
        db_session, source_type=TYPE_CANDIDATE, source_id=candidate.id
    )
    # 只返回 candidate 来源的事件（候选→观察）
    assert len(candidate_events) == 1
    assert candidate_events[0].event_type == EVENT_CANDIDATE_TO_OBSERVATION

    # 按 source_type='observation' 筛选
    observation_events = trans_svc.get_transition_history(
        db_session, source_type=TYPE_OBSERVATION, source_id=item.id
    )
    assert len(observation_events) == 1
    assert observation_events[0].event_type == EVENT_OBSERVATION_TO_PORTFOLIO


# ----------------------------------------------------------------------------
# 14. 自定义 idempotency_key
# ----------------------------------------------------------------------------


def test_custom_idempotency_key(db_session):
    """【WP3.2】调用方传入自定义 idempotency_key 时事件使用该 key。"""
    candidate, _sym, wl = _setup_candidate_with_symbol(db_session, symbol="600517")

    custom_key = "my-custom-key-2024-001"
    _item, event = trans_svc.transition_candidate_to_observation(
        db_session,
        candidate_id=candidate.id,
        watchlist_id=wl.id,
        idempotency_key=custom_key,
    )

    assert event is not None
    assert event.idempotency_key == custom_key


def test_custom_idempotency_key_idempotent(db_session):
    """【WP3.2】相同自定义 idempotency_key 的第二次调用返回 None 事件。"""
    candidate, _sym, wl = _setup_candidate_with_symbol(db_session, symbol="600518")

    custom_key = "my-custom-key-2024-002"
    _item1, event1 = trans_svc.transition_candidate_to_observation(
        db_session,
        candidate_id=candidate.id,
        watchlist_id=wl.id,
        idempotency_key=custom_key,
    )
    assert event1 is not None

    _item2, event2 = trans_svc.transition_candidate_to_observation(
        db_session,
        candidate_id=candidate.id,
        watchlist_id=wl.id,
        idempotency_key=custom_key,
    )
    assert event2 is None


# ----------------------------------------------------------------------------
# 15. actor_type 取值
# ----------------------------------------------------------------------------


def test_actor_type_system(db_session):
    """【WP3.2】传 actor_type='system' 时事件 actor_type='system'。"""
    candidate, _sym, wl = _setup_candidate_with_symbol(db_session, symbol="600519")

    _item, event = trans_svc.transition_candidate_to_observation(
        db_session,
        candidate_id=candidate.id,
        watchlist_id=wl.id,
        actor_type=ACTOR_SYSTEM,
    )

    assert event is not None
    assert event.actor_type == ACTOR_SYSTEM


# ----------------------------------------------------------------------------
# 16. reason_json 解析
# ----------------------------------------------------------------------------


def test_reason_json_parseable(db_session):
    """【WP3.2】审计事件的 reason_json 可解析为 dict。"""
    candidate, _sym, wl = _setup_candidate_with_symbol(db_session, symbol="600520")

    _item, event = trans_svc.transition_candidate_to_observation(
        db_session,
        candidate_id=candidate.id,
        watchlist_id=wl.id,
        note="用户手动加入观察池",
    )

    assert event is not None
    assert event.reason_json is not None
    reason = json.loads(event.reason_json)
    assert isinstance(reason, dict)
    assert reason["source"] == "candidate"
    assert reason["candidate_id"] == candidate.id
    assert reason["note"] == "用户手动加入观察池"


# ----------------------------------------------------------------------------
# 17. 来源链查询（spec 验收："每个观察项和组合成员都能查看来源链"）
# ----------------------------------------------------------------------------


def test_source_chain_queryable_for_observation(db_session):
    """【WP3.2】【spec 验收】每个观察项都能查看来源链（按 source 查询）。"""
    candidate, _sym, wl = _setup_candidate_with_symbol(db_session, symbol="600521")

    # 候选加入观察池
    item, _ = trans_svc.transition_candidate_to_observation(
        db_session, candidate_id=candidate.id, watchlist_id=wl.id
    )

    # 按 source_type='candidate', source_id=candidate.id 查询来源链
    chain = trans_svc.get_transition_history(
        db_session,
        source_type=TYPE_CANDIDATE,
        source_id=candidate.id,
    )

    # 断言返回该事件
    assert len(chain) >= 1
    found = chain[0]
    assert found.event_type == EVENT_CANDIDATE_TO_OBSERVATION
    assert found.source_type == TYPE_CANDIDATE
    assert found.source_id == candidate.id
    assert found.target_type == TYPE_OBSERVATION
    assert found.target_id == wl.id


def test_source_chain_queryable_for_portfolio_member(db_session):
    """【WP3.2】【spec 验收】每个组合成员都能查看来源链（按 target 查询）。"""
    candidate, _sym, wl = _setup_candidate_with_symbol(db_session, symbol="600522")
    pf = _make_portfolio(db_session, name="QA-PF-Chain")

    # 候选→观察→组合
    item, _ = trans_svc.transition_candidate_to_observation(
        db_session, candidate_id=candidate.id, watchlist_id=wl.id
    )
    trans_svc.transition_observation_to_portfolio(
        db_session, watchlist_item_id=item.id, portfolio_id=pf.id
    )

    # 按 target_type='portfolio_member', target_id=portfolio_id 查询来源链
    chain = trans_svc.get_transition_history(
        db_session,
        target_type=TYPE_PORTFOLIO_MEMBER,
        target_id=pf.id,
    )

    # 断言返回 observation_to_portfolio 事件
    assert len(chain) >= 1
    found = chain[0]
    assert found.event_type == EVENT_OBSERVATION_TO_PORTFOLIO
    assert found.target_type == TYPE_PORTFOLIO_MEMBER
    assert found.target_id == pf.id


# ----------------------------------------------------------------------------
# 18. member_archive_to_observation
# ----------------------------------------------------------------------------


def test_member_archive_to_observation_creates_event(db_session):
    """【WP3.2】组合成员归档后回到观察状态：创建审计事件 + 观察项。"""
    sym = _make_symbol(db_session, symbol="600523")
    wl = _make_watchlist(db_session, name="QA-WL-Member-Archive")

    # 第一阶段：调用方提供 symbol_id
    item, event = trans_svc.member_archive_to_observation(
        db_session,
        portfolio_member_id=999,
        watchlist_id=wl.id,
        symbol_id=sym.id,
    )

    # 断言观察项创建（来源为 portfolio_member）
    assert item is not None
    assert item.watchlist_id == wl.id
    assert item.symbol_id == sym.id
    assert item.origin_type == "portfolio_member"
    assert item.origin_id == 999
    assert item.status == "watching"

    # 断言审计事件
    assert event is not None
    assert event.event_type == EVENT_MEMBER_ARCHIVE_TO_OBSERVATION
    assert event.source_type == TYPE_PORTFOLIO_MEMBER
    assert event.source_id == 999
    assert event.target_type == TYPE_OBSERVATION
    assert event.target_id == wl.id
    assert event.symbol_id == sym.id


def test_member_archive_to_observation_idempotent(db_session):
    """【WP3.2】组合成员归档回观察双击幂等。"""
    sym = _make_symbol(db_session, symbol="600524")
    wl = _make_watchlist(db_session, name="QA-WL-Member-Idem")

    item1, event1 = trans_svc.member_archive_to_observation(
        db_session,
        portfolio_member_id=888,
        watchlist_id=wl.id,
        symbol_id=sym.id,
    )
    assert event1 is not None

    item2, event2 = trans_svc.member_archive_to_observation(
        db_session,
        portfolio_member_id=888,
        watchlist_id=wl.id,
        symbol_id=sym.id,
    )

    # 幂等：无新事件，无新观察项
    assert event2 is None
    assert item2 is None

    # DB 中只有 1 个事件
    events = db_session.execute(
        select(OpportunityTransitionEvent).where(
            OpportunityTransitionEvent.event_type == EVENT_MEMBER_ARCHIVE_TO_OBSERVATION
        )
    ).scalars().all()
    assert len(events) == 1


# ----------------------------------------------------------------------------
# 19. candidate_not_found 错误处理
# ----------------------------------------------------------------------------


def test_transition_candidate_to_observation_candidate_not_found(db_session):
    """【WP3.2】候选不存在时抛 ValueError。"""
    wl = _make_watchlist(db_session, name="QA-WL-404")
    with pytest.raises(ValueError, match="not found"):
        trans_svc.transition_candidate_to_observation(
            db_session, candidate_id=99999, watchlist_id=wl.id
        )


def test_transition_observation_to_portfolio_item_not_found(db_session):
    """【WP3.2】观察项不存在时抛 ValueError。"""
    pf = _make_portfolio(db_session, name="QA-PF-404")
    with pytest.raises(ValueError, match="not found"):
        trans_svc.transition_observation_to_portfolio(
            db_session, watchlist_item_id=99999, portfolio_id=pf.id
        )


# ----------------------------------------------------------------------------
# 20. 来源链反查（spec 验收 line 151："每个观察项和组合成员都能查看来源链"）
# ----------------------------------------------------------------------------


def test_observation_source_chain_query(db_session):
    """【WP3.4】【spec line 151】观察项来源链查询：可从观察项反查候选来源。

    验收项："每个观察项和组合成员都能查看来源链"。

    步骤：
    1. 候选加入观察池
    2. 按 source_type='candidate', source_id=candidate_id 查询审计链
    3. 断言返回该事件
    4. 断言可从观察项反查到候选来源（origin_type / origin_id）
    """
    candidate, _sym, wl = _setup_candidate_with_symbol(db_session, symbol="600525")

    # 1. 候选加入观察池
    item, event = trans_svc.transition_candidate_to_observation(
        db_session,
        candidate_id=candidate.id,
        watchlist_id=wl.id,
    )

    # 2. 按 source 查询审计链
    events = trans_svc.get_transition_history(
        db_session,
        source_type=TYPE_CANDIDATE,
        source_id=candidate.id,
    )

    # 3. 断言返回该事件
    assert len(events) == 1
    assert events[0].event_type == EVENT_CANDIDATE_TO_OBSERVATION
    assert events[0].target_type == TYPE_OBSERVATION
    assert events[0].target_id == wl.id
    assert events[0].id == event.id

    # 4. 从观察项反查来源（业务对象上的来源指针）
    assert item.origin_type == "candidate"
    assert item.origin_id == candidate.id


def test_full_transition_chain_query(db_session):
    """【WP3.4】【spec line 151】完整流转链查询：候选→观察→组合按 symbol_id 查询。

    验收项："每个观察项和组合成员都能查看来源链"。

    步骤：
    1. 候选加入观察池
    2. 观察项加入组合
    3. 按 symbol_id 查询完整链
    4. 断言返回 2 个事件（candidate_to_observation + observation_to_portfolio）
    5. 断言按时间降序（最新在前）
    """
    candidate, sym, wl = _setup_candidate_with_symbol(db_session, symbol="600526")
    pf = _make_portfolio(db_session, name="QA-PF-Full-Chain")

    # 1. 候选加入观察池
    item, event1 = trans_svc.transition_candidate_to_observation(
        db_session,
        candidate_id=candidate.id,
        watchlist_id=wl.id,
    )
    assert event1 is not None

    # 2. 观察项加入组合
    _item2, event2 = trans_svc.transition_observation_to_portfolio(
        db_session,
        watchlist_item_id=item.id,
        portfolio_id=pf.id,
    )
    assert event2 is not None

    # 3. 按 symbol_id 查询完整链
    events = trans_svc.get_transition_history(
        db_session,
        symbol_id=sym.id,
    )

    # 4. 断言返回 2 个事件
    assert len(events) == 2

    # 5. 断言按时间降序（最新在前）
    assert events[0].event_type == EVENT_OBSERVATION_TO_PORTFOLIO
    assert events[1].event_type == EVENT_CANDIDATE_TO_OBSERVATION

    # 链路完整性：候选→观察→组合
    assert events[1].source_type == TYPE_CANDIDATE
    assert events[1].target_type == TYPE_OBSERVATION
    assert events[0].source_type == TYPE_OBSERVATION
    assert events[0].target_type == TYPE_PORTFOLIO_MEMBER
