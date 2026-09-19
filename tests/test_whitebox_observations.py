"""白盒测试 - WP2.2 正式观察池服务。

覆盖 app/services/observations.py 的核心接口：
1. idempotent_add_observation 幂等性（同 watchlist+symbol 返回同一记录，非 409）
2. 归档后再次加入自动恢复（status='watching', archived_at=None）
3. add_candidate_to_observation 单事务写来源 + 评分快照
4. get_observation_rich 富读字段填充
5. get_observation_rich 子查询失败降级（degraded=True，不抛异常）
6. list_observations_rich 筛选（默认排除 archived，支持 status / origin_type / tag）
7. update_observation 更新字段
8. archive_observation 归档
9. restore_observation 恢复
10. ObservationRich.to_dict 序列化
11. legacy_manual_unknown 标记（不伪造来源）
12. 从候选池和标的研究加入观察得到同一条后端记录

测试用 SQLite 内存库（db_session fixture），每个用例独立 session。
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from sqlalchemy import select

from app.models.daily_bar import DailyBar
from app.models.discovery_candidate import DiscoveryCandidate
from app.models.opportunity_transition_event import (
    EVENT_CANDIDATE_TO_OBSERVATION,
    EVENT_EXCLUDE,
    EVENT_RESTORE,
    OpportunityTransitionEvent,
    TYPE_OBSERVATION,
)
from app.models.portfolio import Portfolio, Position
from app.models.score import Score
from app.models.scan import ScanRun
from app.models.symbol import Symbol
from app.models.universe import UniverseSymbol
from app.models.watchlist import Watchlist, WatchlistItem
from app.schemas.watchlist import ObservationRead
from app.services import observations as obs_svc
from app.services import opportunity_transitions as trans_svc


pytestmark = pytest.mark.whitebox


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------


def _make_watchlist(db_session, name: str = "QA-Obs-WL") -> Watchlist:
    wl = Watchlist(name=name, list_type="custom", description="qa")
    db_session.add(wl)
    db_session.commit()
    db_session.refresh(wl)
    return wl


def _make_symbol(db_session, symbol: str = "600000") -> Symbol:
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


def _make_portfolio(db_session, name: str = "QA-Obs-PF") -> Portfolio:
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


def _make_position(
    db_session,
    *,
    portfolio_id: int,
    symbol_id: int,
    quantity: float = 100.0,
    asset_type: str = "stock",
) -> Position:
    pos = Position(
        portfolio_id=portfolio_id,
        symbol_id=symbol_id,
        quantity=quantity,
        avg_cost=10.0,
        latest_price=12.0,
        market_value=1200.0,
        position_pct=0.1,
        asset_type=asset_type,
    )
    db_session.add(pos)
    db_session.commit()
    db_session.refresh(pos)
    return pos


def _make_daily_bar(
    db_session,
    *,
    symbol_id: int,
    trade_date: date | None = None,
    close: float = 12.34,
) -> DailyBar:
    bar = DailyBar(
        symbol_id=symbol_id,
        trade_date=trade_date or date(2024, 1, 15),
        open=10.0,
        high=13.0,
        low=9.5,
        close=close,
        volume=1000000.0,
        amount=12000000.0,
    )
    db_session.add(bar)
    db_session.commit()
    db_session.refresh(bar)
    return bar


def _make_score(
    db_session,
    *,
    symbol_id: int,
    trade_date: date | None = None,
    quality_score: float = 75.0,
    timing_score: float = 70.0,
    priority_score: float = 80.0,
    quality_grade: str = "B",
    stage: str = "hold",
    action: str = "watch",
) -> Score:
    score = Score(
        symbol_id=symbol_id,
        trade_date=trade_date or date(2024, 1, 15),
        quality_score=quality_score,
        quality_grade=quality_grade,
        timing_score=timing_score,
        stage=stage,
        action=action,
        priority_score=priority_score,
    )
    db_session.add(score)
    db_session.commit()
    db_session.refresh(score)
    return score


def _make_scan_run(db_session, name: str = "QA-ScanRun") -> ScanRun:
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
    db_session, symbol: str = "600000"
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
    symbol: str = "600000",
    name: str = "QA-Candidate",
    quality_score: float = 78.0,
    timing_score: float = 72.0,
    priority_score: float = 82.5,
    stage: str = "hold",
    action: str = "buy",
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
        is_promoted=0,
    )
    db_session.add(c)
    db_session.commit()
    db_session.refresh(c)
    return c


# ----------------------------------------------------------------------------
# 1. idempotent_add_observation 幂等性
# ----------------------------------------------------------------------------


def test_idempotent_add_observation_creates_new_record(db_session):
    """【WP2.2】第一次加入创建新记录。"""
    wl = _make_watchlist(db_session, name="QA-Idem-New")
    sym = _make_symbol(db_session, symbol="600100")

    item = obs_svc.idempotent_add_observation(
        db_session,
        watchlist_id=wl.id,
        symbol_id=sym.id,
        origin_type=obs_svc.ORIGIN_MANUAL,
        priority=5,
        tags=["科技", "龙头"],
        note="测试笔记",
    )

    assert item.id is not None
    assert item.watchlist_id == wl.id
    assert item.symbol_id == sym.id
    assert item.origin_type == "manual"
    assert item.status == "watching"
    assert item.priority == 5
    assert json.loads(item.tags_json) == ["科技", "龙头"]
    assert item.note == "测试笔记"
    assert item.added_at is not None


def test_idempotent_add_observation_duplicate_returns_same_record(db_session):
    """【WP2.2】同 watchlist_id + symbol_id 重复请求返回已有记录（非 409）。

    参照 spec line 165："同名单同标的重复请求返回已有记录（非 409）"。
    """
    wl = _make_watchlist(db_session, name="QA-Idem-Dup")
    sym = _make_symbol(db_session, symbol="600101")

    item1 = obs_svc.idempotent_add_observation(
        db_session,
        watchlist_id=wl.id,
        symbol_id=sym.id,
        note="第一次",
    )
    item2 = obs_svc.idempotent_add_observation(
        db_session,
        watchlist_id=wl.id,
        symbol_id=sym.id,
        note="第二次（应被忽略）",
    )

    # 两次调用返回相同 watchlist_item_id
    assert item1.id == item2.id
    # note 不被覆盖（幂等：返回已有记录）
    assert item2.note == "第一次"


def test_idempotent_add_observation_different_watchlist_separate(db_session):
    """【WP2.2】不同 watchlist_id 同 symbol_id 创建独立记录。"""
    wl1 = _make_watchlist(db_session, name="QA-Idem-WL1")
    wl2 = _make_watchlist(db_session, name="QA-Idem-WL2")
    sym = _make_symbol(db_session, symbol="600102")

    item1 = obs_svc.idempotent_add_observation(
        db_session, watchlist_id=wl1.id, symbol_id=sym.id
    )
    item2 = obs_svc.idempotent_add_observation(
        db_session, watchlist_id=wl2.id, symbol_id=sym.id
    )

    assert item1.id != item2.id


# ----------------------------------------------------------------------------
# 2. 归档后再次加入恢复
# ----------------------------------------------------------------------------


def test_archive_then_re_add_restores(db_session):
    """【WP2.2】归档后再次加入相同 (watchlist, symbol) 恢复为 watching。

    参照 idempotent_add_observation 文档："如果 status == 'archived'：恢复为 'watching'"。
    """
    wl = _make_watchlist(db_session, name="QA-Restore")
    sym = _make_symbol(db_session, symbol="600103")

    # 第一次加入
    item1 = obs_svc.idempotent_add_observation(
        db_session, watchlist_id=wl.id, symbol_id=sym.id
    )
    assert item1.status == "watching"

    # 归档
    archived = obs_svc.archive_observation(
        db_session, watchlist_item_id=item1.id
    )
    assert archived.status == "archived"
    assert archived.archived_at is not None

    # 再次加入相同 (watchlist, symbol) → 应恢复
    item2 = obs_svc.idempotent_add_observation(
        db_session, watchlist_id=wl.id, symbol_id=sym.id
    )

    # 返回同一条记录，但状态已恢复
    assert item2.id == item1.id
    assert item2.status == "watching"
    assert item2.archived_at is None


# ----------------------------------------------------------------------------
# 3. add_candidate_to_observation 单事务写来源 + 评分快照
# ----------------------------------------------------------------------------


def test_add_candidate_to_observation_writes_origin_and_snapshot(db_session):
    """【WP2.2】候选加入观察时同一事务写来源（origin_id 指向 candidate）与评分快照。

    参照 spec line 164："同一事务写入观察项和来源/评分快照"。
    """
    wl = _make_watchlist(db_session, name="QA-Cand-Obs")
    sym = _make_symbol(db_session, symbol="600200")
    run = _make_scan_run(db_session, name="QA-ScanRun-Cand")
    u = _make_universe_symbol(db_session, symbol="600200")
    candidate = _make_candidate(
        db_session,
        scan_run_id=run.id,
        universe_symbol_id=u.id,
        symbol="600200",
        quality_score=78.0,
        timing_score=72.0,
        priority_score=82.5,
        stage="hold",
        action="buy",
    )

    item = obs_svc.add_candidate_to_observation(
        db_session,
        candidate_id=candidate.id,
        watchlist_id=wl.id,
        note="候选加入",
        priority=3,
        tags=["候选", "科技"],
    )

    # 来源：origin_type='candidate', origin_id=candidate.id
    assert item.origin_type == "candidate"
    assert item.origin_id == candidate.id
    assert item.symbol_id == sym.id
    assert item.status == "watching"
    assert item.priority == 3
    assert item.note == "候选加入"

    # 评分快照：含 candidate 的评分字段
    assert item.score_snapshot_json is not None
    snapshot = json.loads(item.score_snapshot_json)
    assert snapshot["source"] == "discovery_candidate"
    assert snapshot["candidate_id"] == candidate.id
    assert snapshot["quality_score"] == 78.0
    assert snapshot["timing_score"] == 72.0
    assert snapshot["priority_score"] == 82.5
    assert snapshot["captured_at"] is not None

    # 原因：含 candidate 来源信息
    assert item.reason_json is not None
    reason = json.loads(item.reason_json)
    assert reason["source"] == "candidate"
    assert reason["candidate_id"] == candidate.id
    assert reason["action"] == "buy"


def test_add_candidate_to_observation_idempotent(db_session):
    """【WP2.2】同一候选重复加入返回同一观察项（非 409）。"""
    wl = _make_watchlist(db_session, name="QA-Cand-Idem")
    sym = _make_symbol(db_session, symbol="600201")
    run = _make_scan_run(db_session, name="QA-ScanRun-Idem")
    u = _make_universe_symbol(db_session, symbol="600201")
    candidate = _make_candidate(
        db_session,
        scan_run_id=run.id,
        universe_symbol_id=u.id,
        symbol="600201",
    )

    item1 = obs_svc.add_candidate_to_observation(
        db_session, candidate_id=candidate.id, watchlist_id=wl.id
    )
    item2 = obs_svc.add_candidate_to_observation(
        db_session, candidate_id=candidate.id, watchlist_id=wl.id
    )

    assert item1.id == item2.id
    assert item2.origin_type == "candidate"
    assert item2.origin_id == candidate.id


def test_add_candidate_to_observation_candidate_not_found_raises(db_session):
    """【WP2.2】候选不存在时抛 ValueError。"""
    wl = _make_watchlist(db_session, name="QA-Cand-404")
    with pytest.raises(ValueError, match="not found"):
        obs_svc.add_candidate_to_observation(
            db_session, candidate_id=99999, watchlist_id=wl.id
        )


# ----------------------------------------------------------------------------
# 4. get_observation_rich 富读字段填充
# ----------------------------------------------------------------------------


def test_get_observation_rich_populates_all_fields(db_session):
    """【WP2.2】富读模型填充标的、最新行情、最新评分、数据健康、来源、组合关系。"""
    wl = _make_watchlist(db_session, name="QA-Rich")
    sym = _make_symbol(db_session, symbol="600300")
    pf = _make_portfolio(db_session, name="QA-Rich-PF")
    _make_position(db_session, portfolio_id=pf.id, symbol_id=sym.id)

    # 创建 5 条 DailyBar（满足 medium credibility >= 60）
    base_date = date(2024, 1, 1)
    for i in range(65):
        _make_daily_bar(
            db_session,
            symbol_id=sym.id,
            trade_date=base_date + timedelta(days=i),
            close=10.0 + i * 0.1,
        )
    # 最新一条价格最高（trade_date 倒序）
    latest_bar_date = base_date + timedelta(days=64)
    latest_close = 10.0 + 64 * 0.1

    _make_score(
        db_session,
        symbol_id=sym.id,
        trade_date=latest_bar_date,
        quality_score=75.0,
        timing_score=70.0,
        priority_score=80.0,
    )

    item = obs_svc.idempotent_add_observation(
        db_session,
        watchlist_id=wl.id,
        symbol_id=sym.id,
        origin_type=obs_svc.ORIGIN_CANDIDATE,
        origin_id=42,
        reason={"source": "candidate", "candidate_id": 42},
        score_snapshot={"quality_score": 78.0, "candidate_id": 42},
        priority=5,
        tags=["科技", "龙头"],
        target_portfolio_id=pf.id,
        note="富读测试",
    )

    rich = obs_svc.get_observation_rich(
        db_session, watchlist_item_id=item.id
    )

    # 基础
    assert rich.watchlist_item_id == item.id
    assert rich.watchlist_id == wl.id
    assert rich.watchlist_name == "QA-Rich"
    assert rich.symbol_id == sym.id
    assert rich.symbol == "600300"
    assert rich.added_at is not None

    # 来源
    assert rich.origin_type == "candidate"
    assert rich.origin_id == 42
    assert rich.reason == {"source": "candidate", "candidate_id": 42}
    assert rich.score_snapshot == {"quality_score": 78.0, "candidate_id": 42}

    # 状态
    assert rich.status == "watching"
    assert rich.priority == 5
    assert rich.tags == ["科技", "龙头"]
    assert rich.note == "富读测试"

    # 目标组合
    assert rich.target_portfolio_id == pf.id
    assert rich.target_portfolio_name == "QA-Rich-PF"

    # 最新行情
    assert rich.latest_price == pytest.approx(latest_close)
    assert rich.latest_price_date == latest_bar_date.isoformat()

    # 最新评分（priority_score 作为 latest_total_score 等价）
    assert rich.latest_total_score == pytest.approx(80.0)
    assert rich.latest_quality_score == pytest.approx(75.0)
    assert rich.latest_timing_score == pytest.approx(70.0)
    assert rich.latest_score_date == latest_bar_date.isoformat()

    # 数据健康
    assert rich.bar_count == 65
    assert rich.data_credibility == "medium"

    # 组合关系
    assert rich.has_position is True
    assert rich.position_portfolio_name == "QA-Rich-PF"

    # 未降级
    assert rich.degraded is False
    assert rich.degraded_reason is None


def test_get_observation_rich_data_credibility_high(db_session):
    """【WP2.2】bar_count >= 250 时 data_credibility='high'。"""
    wl = _make_watchlist(db_session, name="QA-Rich-High")
    sym = _make_symbol(db_session, symbol="600301")

    # 创建 250 条 DailyBar
    base_date = date(2024, 1, 1)
    for i in range(250):
        _make_daily_bar(
            db_session,
            symbol_id=sym.id,
            trade_date=base_date + timedelta(days=i),
            close=10.0,
        )

    item = obs_svc.idempotent_add_observation(
        db_session, watchlist_id=wl.id, symbol_id=sym.id
    )

    rich = obs_svc.get_observation_rich(db_session, watchlist_item_id=item.id)

    assert rich.bar_count == 250
    assert rich.data_credibility == "high"


def test_get_observation_rich_data_credibility_low(db_session):
    """【WP2.2】bar_count < 60 时 data_credibility='low'。"""
    wl = _make_watchlist(db_session, name="QA-Rich-Low")
    sym = _make_symbol(db_session, symbol="600302")

    # 创建 10 条 DailyBar
    base_date = date(2024, 1, 1)
    for i in range(10):
        _make_daily_bar(
            db_session,
            symbol_id=sym.id,
            trade_date=base_date + timedelta(days=i),
            close=10.0,
        )

    item = obs_svc.idempotent_add_observation(
        db_session, watchlist_id=wl.id, symbol_id=sym.id
    )

    rich = obs_svc.get_observation_rich(db_session, watchlist_item_id=item.id)

    assert rich.bar_count == 10
    assert rich.data_credibility == "low"


def test_get_observation_rich_no_position(db_session):
    """【WP2.2】无持仓时 has_position=False。"""
    wl = _make_watchlist(db_session, name="QA-Rich-NoPos")
    sym = _make_symbol(db_session, symbol="600303")

    item = obs_svc.idempotent_add_observation(
        db_session, watchlist_id=wl.id, symbol_id=sym.id
    )

    rich = obs_svc.get_observation_rich(db_session, watchlist_item_id=item.id)

    assert rich.has_position is False
    assert rich.position_portfolio_name is None


def test_get_observation_rich_not_found_raises(db_session):
    """【WP2.2】不存在的 watchlist_item_id 抛 ValueError。"""
    with pytest.raises(ValueError, match="not found"):
        obs_svc.get_observation_rich(db_session, watchlist_item_id=99999)


# ----------------------------------------------------------------------------
# 5. get_observation_rich 子查询失败降级
# ----------------------------------------------------------------------------


def test_get_observation_rich_degraded_on_subquery_failure(db_session, monkeypatch):
    """【WP2.2】子查询失败时降级（degraded=True）但不抛异常，其他字段仍正常返回。"""
    wl = _make_watchlist(db_session, name="QA-Degrade")
    sym = _make_symbol(db_session, symbol="600400")

    item = obs_svc.idempotent_add_observation(
        db_session, watchlist_id=wl.id, symbol_id=sym.id
    )

    # monkeypatch _query_position 抛异常
    def _boom(*args, **kwargs):
        raise RuntimeError("boom position query")

    monkeypatch.setattr(obs_svc, "_query_position", _boom)

    rich = obs_svc.get_observation_rich(db_session, watchlist_item_id=item.id)

    # 降级标记
    assert rich.degraded is True
    assert rich.degraded_reason is not None
    assert "position" in rich.degraded_reason
    assert "boom" in rich.degraded_reason

    # 其他字段仍正常返回
    assert rich.watchlist_name == "QA-Degrade"
    assert rich.symbol == "600400"
    assert rich.has_position is False  # 降级为默认值
    assert rich.position_portfolio_name is None


def test_get_observation_rich_multiple_degradations(db_session, monkeypatch):
    """【WP2.2】多个子查询失败时 degraded_reason 含多条原因。"""
    wl = _make_watchlist(db_session, name="QA-Degrade-Multi")
    sym = _make_symbol(db_session, symbol="600401")

    item = obs_svc.idempotent_add_observation(
        db_session, watchlist_id=wl.id, symbol_id=sym.id
    )

    def _boom_price(*args, **kwargs):
        raise RuntimeError("boom price")

    def _boom_score(*args, **kwargs):
        raise RuntimeError("boom score")

    monkeypatch.setattr(obs_svc, "_query_latest_price", _boom_price)
    monkeypatch.setattr(obs_svc, "_query_latest_score", _boom_score)

    rich = obs_svc.get_observation_rich(db_session, watchlist_item_id=item.id)

    assert rich.degraded is True
    assert "price" in rich.degraded_reason
    assert "score" in rich.degraded_reason
    assert rich.latest_price is None
    assert rich.latest_total_score is None


# ----------------------------------------------------------------------------
# 6. list_observations_rich 筛选
# ----------------------------------------------------------------------------


def test_list_observations_rich_default_excludes_archived(db_session):
    """【WP2.2】默认不返回 archived 状态。"""
    wl = _make_watchlist(db_session, name="QA-List-Default")
    # 创建 3 个 watching + 2 个 archived
    sym_ids = []
    for i in range(5):
        sym = _make_symbol(db_session, symbol=f"600500{i}")
        sym_ids.append(sym.id)
        item = obs_svc.idempotent_add_observation(
            db_session, watchlist_id=wl.id, symbol_id=sym.id
        )
        if i >= 3:
            obs_svc.archive_observation(db_session, watchlist_item_id=item.id)

    result = obs_svc.list_observations_rich(db_session, watchlist_id=wl.id)

    # 默认只返回 3 个 watching
    assert len(result) == 3
    for r in result:
        assert r.status == "watching"


def test_list_observations_rich_status_filter_archived(db_session):
    """【WP2.2】status='archived' 显式筛选返回归档项。"""
    wl = _make_watchlist(db_session, name="QA-List-Archived")
    for i in range(5):
        sym = _make_symbol(db_session, symbol=f"600501{i}")
        item = obs_svc.idempotent_add_observation(
            db_session, watchlist_id=wl.id, symbol_id=sym.id
        )
        if i >= 3:
            obs_svc.archive_observation(db_session, watchlist_item_id=item.id)

    result = obs_svc.list_observations_rich(
        db_session, watchlist_id=wl.id, status="archived"
    )

    assert len(result) == 2
    for r in result:
        assert r.status == "archived"


def test_list_observations_rich_origin_type_filter(db_session):
    """【WP2.2】origin_type 筛选只返回对应来源的项。"""
    wl = _make_watchlist(db_session, name="QA-List-Origin")
    # 2 个 manual + 1 个 candidate
    sym1 = _make_symbol(db_session, symbol="600510")
    sym2 = _make_symbol(db_session, symbol="600511")
    sym3 = _make_symbol(db_session, symbol="600512")

    obs_svc.idempotent_add_observation(
        db_session, watchlist_id=wl.id, symbol_id=sym1.id, origin_type="manual"
    )
    obs_svc.idempotent_add_observation(
        db_session, watchlist_id=wl.id, symbol_id=sym2.id, origin_type="manual"
    )
    obs_svc.idempotent_add_observation(
        db_session, watchlist_id=wl.id, symbol_id=sym3.id, origin_type="candidate",
        origin_id=99,
    )

    result = obs_svc.list_observations_rich(
        db_session, watchlist_id=wl.id, origin_type="candidate"
    )

    assert len(result) == 1
    assert result[0].origin_type == "candidate"
    assert result[0].origin_id == 99


def test_list_observations_rich_tag_filter(db_session):
    """【WP2.2】tag 筛选只返回含该标签的项。"""
    wl = _make_watchlist(db_session, name="QA-List-Tag")
    sym1 = _make_symbol(db_session, symbol="600520")
    sym2 = _make_symbol(db_session, symbol="600521")

    obs_svc.idempotent_add_observation(
        db_session, watchlist_id=wl.id, symbol_id=sym1.id, tags=["科技", "龙头"]
    )
    obs_svc.idempotent_add_observation(
        db_session, watchlist_id=wl.id, symbol_id=sym2.id, tags=["消费"]
    )

    result = obs_svc.list_observations_rich(
        db_session, watchlist_id=wl.id, tag="龙头"
    )

    assert len(result) == 1
    assert "龙头" in result[0].tags


def test_list_observations_rich_limit_offset(db_session):
    """【WP2.2】limit / offset 分页生效。"""
    wl = _make_watchlist(db_session, name="QA-List-Page")
    for i in range(5):
        sym = _make_symbol(db_session, symbol=f"600530{i}")
        obs_svc.idempotent_add_observation(
            db_session, watchlist_id=wl.id, symbol_id=sym.id
        )

    page1 = obs_svc.list_observations_rich(
        db_session, watchlist_id=wl.id, limit=2, offset=0
    )
    page2 = obs_svc.list_observations_rich(
        db_session, watchlist_id=wl.id, limit=2, offset=2
    )

    assert len(page1) == 2
    assert len(page2) == 2
    # 两页不应有重复
    page1_ids = {r.watchlist_item_id for r in page1}
    page2_ids = {r.watchlist_item_id for r in page2}
    assert page1_ids.isdisjoint(page2_ids)


# ----------------------------------------------------------------------------
# 7. update_observation
# ----------------------------------------------------------------------------


def test_update_observation_updates_fields(db_session):
    """【WP2.2】update_observation 更新 priority / tags / reason / 目标组合 / note / status。"""
    wl = _make_watchlist(db_session, name="QA-Update")
    sym = _make_symbol(db_session, symbol="600600")
    pf = _make_portfolio(db_session, name="QA-Update-PF")
    item = obs_svc.idempotent_add_observation(
        db_session, watchlist_id=wl.id, symbol_id=sym.id
    )

    updated = obs_svc.update_observation(
        db_session,
        watchlist_item_id=item.id,
        priority=10,
        tags=["高优", "科技"],
        reason={"source": "manual", "note": "调整"},
        target_portfolio_id=pf.id,
        note="更新后笔记",
        status="ready",
    )

    assert updated is not None
    assert updated.priority == 10
    assert json.loads(updated.tags_json) == ["高优", "科技"]
    assert json.loads(updated.reason_json) == {"source": "manual", "note": "调整"}
    assert updated.target_portfolio_id == pf.id
    assert updated.note == "更新后笔记"
    assert updated.status == "ready"
    assert updated.updated_at is not None


def test_update_observation_partial_update(db_session):
    """【WP2.2】仅更新显式传入的字段，其他字段保持不变。"""
    wl = _make_watchlist(db_session, name="QA-Update-Partial")
    sym = _make_symbol(db_session, symbol="600601")
    item = obs_svc.idempotent_add_observation(
        db_session,
        watchlist_id=wl.id,
        symbol_id=sym.id,
        priority=1,
        tags=["初始"],
        note="初始笔记",
    )

    updated = obs_svc.update_observation(
        db_session, watchlist_item_id=item.id, priority=99
    )

    assert updated.priority == 99
    # 其他字段不变
    assert json.loads(updated.tags_json) == ["初始"]
    assert updated.note == "初始笔记"
    assert updated.status == "watching"


def test_update_observation_not_found_returns_none(db_session):
    """【WP2.2】不存在的 watchlist_item_id 返回 None。"""
    result = obs_svc.update_observation(
        db_session, watchlist_item_id=99999, priority=5
    )
    assert result is None


# ----------------------------------------------------------------------------
# 8. archive_observation
# ----------------------------------------------------------------------------


def test_archive_observation_sets_status_and_archived_at(db_session):
    """【WP2.2】归档设置 status='archived' 且 archived_at 非空。"""
    wl = _make_watchlist(db_session, name="QA-Archive")
    sym = _make_symbol(db_session, symbol="600700")
    item = obs_svc.idempotent_add_observation(
        db_session, watchlist_id=wl.id, symbol_id=sym.id
    )
    assert item.status == "watching"
    assert item.archived_at is None

    archived = obs_svc.archive_observation(
        db_session, watchlist_item_id=item.id
    )

    assert archived is not None
    assert archived.status == "archived"
    assert archived.archived_at is not None
    assert archived.updated_at is not None


def test_archive_observation_not_found_returns_none(db_session):
    """【WP2.2】归档不存在的项返回 None。"""
    result = obs_svc.archive_observation(db_session, watchlist_item_id=99999)
    assert result is None


def test_archive_observation_excludes_from_default_list(db_session):
    """【WP2.2】归档后默认列表不返回该项。"""
    wl = _make_watchlist(db_session, name="QA-Archive-Exclude")
    sym = _make_symbol(db_session, symbol="600701")
    item = obs_svc.idempotent_add_observation(
        db_session, watchlist_id=wl.id, symbol_id=sym.id
    )

    obs_svc.archive_observation(db_session, watchlist_item_id=item.id)

    result = obs_svc.list_observations_rich(db_session, watchlist_id=wl.id)
    assert len(result) == 0


# ----------------------------------------------------------------------------
# 9. restore_observation
# ----------------------------------------------------------------------------


def test_restore_observation_resets_status_and_archived_at(db_session):
    """【WP2.2】恢复设置 status='watching' 且 archived_at=None。"""
    wl = _make_watchlist(db_session, name="QA-Restore-Obs")
    sym = _make_symbol(db_session, symbol="600800")
    item = obs_svc.idempotent_add_observation(
        db_session, watchlist_id=wl.id, symbol_id=sym.id
    )
    obs_svc.archive_observation(db_session, watchlist_item_id=item.id)

    restored = obs_svc.restore_observation(
        db_session, watchlist_item_id=item.id
    )

    assert restored is not None
    assert restored.status == "watching"
    assert restored.archived_at is None
    assert restored.updated_at is not None


def test_restore_observation_not_found_returns_none(db_session):
    """【WP2.2】恢复不存在的项返回 None。"""
    result = obs_svc.restore_observation(db_session, watchlist_item_id=99999)
    assert result is None


def test_restore_makes_item_visible_in_default_list(db_session):
    """【WP2.2】恢复后项重新出现在默认列表中。"""
    wl = _make_watchlist(db_session, name="QA-Restore-Visible")
    sym = _make_symbol(db_session, symbol="600801")
    item = obs_svc.idempotent_add_observation(
        db_session, watchlist_id=wl.id, symbol_id=sym.id
    )
    obs_svc.archive_observation(db_session, watchlist_item_id=item.id)
    assert len(obs_svc.list_observations_rich(db_session, watchlist_id=wl.id)) == 0

    obs_svc.restore_observation(db_session, watchlist_item_id=item.id)

    result = obs_svc.list_observations_rich(db_session, watchlist_id=wl.id)
    assert len(result) == 1
    assert result[0].status == "watching"


# ----------------------------------------------------------------------------
# 10. ObservationRich.to_dict 序列化
# ----------------------------------------------------------------------------


def test_observation_rich_to_dict_serializes_all_fields(db_session):
    """【WP2.2】ObservationRich.to_dict 正确序列化所有字段（含 ISO 时间）。"""
    wl = _make_watchlist(db_session, name="QA-ToDict")
    sym = _make_symbol(db_session, symbol="600900")
    pf = _make_portfolio(db_session, name="QA-ToDict-PF")
    _make_position(db_session, portfolio_id=pf.id, symbol_id=sym.id)
    _make_daily_bar(db_session, symbol_id=sym.id, close=15.6)
    _make_score(
        db_session,
        symbol_id=sym.id,
        quality_score=75.0,
        timing_score=70.0,
        priority_score=80.0,
    )

    item = obs_svc.idempotent_add_observation(
        db_session,
        watchlist_id=wl.id,
        symbol_id=sym.id,
        origin_type="candidate",
        origin_id=42,
        priority=7,
        tags=["科技"],
        target_portfolio_id=pf.id,
        note="to_dict 测试",
    )

    rich = obs_svc.get_observation_rich(db_session, watchlist_item_id=item.id)
    d = rich.to_dict()

    # 基础字段
    assert d["watchlist_item_id"] == item.id
    assert d["watchlist_id"] == wl.id
    assert d["watchlist_name"] == "QA-ToDict"
    assert d["symbol_id"] == sym.id
    assert d["symbol"] == "600900"
    assert d["added_at"] is not None
    assert isinstance(d["added_at"], str)

    # 来源与状态
    assert d["origin_type"] == "candidate"
    assert d["origin_id"] == 42
    assert d["reason"] is None or isinstance(d["reason"], dict)
    assert d["status"] == "watching"
    assert d["priority"] == 7
    assert d["tags"] == ["科技"]
    assert d["note"] == "to_dict 测试"

    # 目标组合
    assert d["target_portfolio_id"] == pf.id
    assert d["target_portfolio_name"] == "QA-ToDict-PF"

    # 富读字段
    assert d["latest_price"] == pytest.approx(15.6)
    assert d["latest_total_score"] == pytest.approx(80.0)
    assert d["latest_quality_score"] == pytest.approx(75.0)
    assert d["latest_timing_score"] == pytest.approx(70.0)
    assert d["bar_count"] == 1
    assert d["data_credibility"] == "low"  # 1 条 < 60
    assert d["has_position"] is True
    assert d["position_portfolio_name"] == "QA-ToDict-PF"

    # 降级标记
    assert d["degraded"] is False
    assert d["degraded_reason"] is None


def test_observation_rich_to_dict_none_fields(db_session):
    """【WP2.2】ObservationRich.to_dict 正确处理 None 字段。"""
    wl = _make_watchlist(db_session, name="QA-ToDict-None")
    sym = _make_symbol(db_session, symbol="600901")
    item = obs_svc.idempotent_add_observation(
        db_session, watchlist_id=wl.id, symbol_id=sym.id
    )

    rich = obs_svc.get_observation_rich(db_session, watchlist_item_id=item.id)
    d = rich.to_dict()

    # 无行情/评分/持仓时字段为 None
    assert d["latest_price"] is None
    assert d["latest_price_date"] is None
    assert d["latest_total_score"] is None
    assert d["latest_quality_score"] is None
    assert d["latest_timing_score"] is None
    assert d["latest_score_date"] is None
    assert d["bar_count"] == 0
    assert d["data_credibility"] is None  # 无 DailyBar 时为 None（不可评估）
    assert d["has_position"] is False
    assert d["position_portfolio_name"] is None
    assert d["target_portfolio_id"] is None
    assert d["target_portfolio_name"] is None
    assert d["archived_at"] is None
    assert d["degraded"] is False


# ----------------------------------------------------------------------------
# 11. legacy_manual_unknown 标记
# ----------------------------------------------------------------------------


def test_legacy_manual_unknown_origin_not_faked(db_session):
    """【WP2.2】历史无来源项标记 legacy_manual_unknown，富读返回正确 origin_type 不伪造来源。

    参照 spec line 176："历史无来源项标记 legacy/manual_unknown，禁止伪造来源"。
    """
    wl = _make_watchlist(db_session, name="QA-Legacy-Obs")
    sym = _make_symbol(db_session, symbol="601000")

    # 直接以 legacy_manual_unknown 创建（模拟迁移后的旧记录）
    item = WatchlistItem(
        watchlist_id=wl.id,
        symbol_id=sym.id,
        origin_type="legacy_manual_unknown",
        # origin_id / reason_json / score_snapshot_json 均为 None（不伪造）
    )
    db_session.add(item)
    db_session.commit()
    db_session.refresh(item)

    rich = obs_svc.get_observation_rich(db_session, watchlist_item_id=item.id)

    assert rich.origin_type == "legacy_manual_unknown"
    # 不伪造来源
    assert rich.origin_id is None
    assert rich.reason is None
    assert rich.score_snapshot is None


def test_legacy_manual_unknown_in_list_filter(db_session):
    """【WP2.2】legacy_manual_unknown 来源可通过 origin_type 筛选。"""
    wl = _make_watchlist(db_session, name="QA-Legacy-Filter")
    sym1 = _make_symbol(db_session, symbol="601001")
    sym2 = _make_symbol(db_session, symbol="601002")

    # 1 个 legacy + 1 个 manual
    db_session.add(WatchlistItem(
        watchlist_id=wl.id, symbol_id=sym1.id, origin_type="legacy_manual_unknown",
    ))
    db_session.add(WatchlistItem(
        watchlist_id=wl.id, symbol_id=sym2.id, origin_type="manual",
    ))
    db_session.commit()

    result = obs_svc.list_observations_rich(
        db_session, watchlist_id=wl.id, origin_type="legacy_manual_unknown"
    )

    assert len(result) == 1
    assert result[0].origin_type == "legacy_manual_unknown"
    assert result[0].symbol == "601001"


# ----------------------------------------------------------------------------
# 12. 候选池与标的研究加入得到同一条后端记录
# ----------------------------------------------------------------------------


def test_candidate_and_manual_add_same_symbol_returns_same_record(db_session):
    """【WP2.2】从候选池和标的研究页加入观察（同一 watchlist + symbol）得到同一条记录。

    参照 checklist line 107："从候选池和标的研究加入观察池得到同一条后端记录"。
    """
    wl = _make_watchlist(db_session, name="QA-SameRecord")
    sym = _make_symbol(db_session, symbol="601100")
    run = _make_scan_run(db_session, name="QA-ScanRun-Same")
    u = _make_universe_symbol(db_session, symbol="601100")
    candidate = _make_candidate(
        db_session,
        scan_run_id=run.id,
        universe_symbol_id=u.id,
        symbol="601100",
        quality_score=80.0,
        timing_score=75.0,
        priority_score=85.0,
    )

    # 1. 从候选池加入观察（origin_type='candidate'）
    item_from_candidate = obs_svc.add_candidate_to_observation(
        db_session, candidate_id=candidate.id, watchlist_id=wl.id
    )
    assert item_from_candidate.origin_type == "candidate"
    assert item_from_candidate.origin_id == candidate.id

    # 2. 从标的研究页加入观察（同一 watchlist + symbol，origin_type='manual'）
    #    幂等：返回已有记录（不创建新记录，不抛 409）
    item_from_manual = obs_svc.idempotent_add_observation(
        db_session,
        watchlist_id=wl.id,
        symbol_id=sym.id,
        origin_type="manual",
        note="从标的研究页加入",
    )

    # 两次操作得到同一条 watchlist_item_id
    assert item_from_candidate.id == item_from_manual.id

    # 已有记录的来源信息不被覆盖（候选来源仍保留）
    assert item_from_manual.origin_type == "candidate"
    assert item_from_manual.origin_id == candidate.id

    # 数据库中只有一条记录
    items = (
        db_session.query(WatchlistItem)
        .filter_by(watchlist_id=wl.id, symbol_id=sym.id)
        .all()
    )
    assert len(items) == 1


def test_candidate_then_manual_add_after_archive_restores(db_session):
    """【WP2.2】候选加入 → 归档 → 标的研究页再次加入 → 恢复同一条记录。

    验证归档后通过另一入口（manual）重新加入能恢复同一记录。
    """
    wl = _make_watchlist(db_session, name="QA-SameRecord-Restore")
    sym = _make_symbol(db_session, symbol="601101")
    run = _make_scan_run(db_session, name="QA-ScanRun-Restore")
    u = _make_universe_symbol(db_session, symbol="601101")
    candidate = _make_candidate(
        db_session,
        scan_run_id=run.id,
        universe_symbol_id=u.id,
        symbol="601101",
    )

    # 1. 候选加入
    item1 = obs_svc.add_candidate_to_observation(
        db_session, candidate_id=candidate.id, watchlist_id=wl.id
    )

    # 2. 归档
    obs_svc.archive_observation(db_session, watchlist_item_id=item1.id)

    # 3. 标的研究页再次加入 → 恢复
    item2 = obs_svc.idempotent_add_observation(
        db_session,
        watchlist_id=wl.id,
        symbol_id=sym.id,
        origin_type="manual",
    )

    # 同一条记录，状态已恢复
    assert item2.id == item1.id
    assert item2.status == "watching"
    assert item2.archived_at is None


# ============================================================================
# UAT-PAGES.1 P1-02：真实契约 + 审计事件 + 幂等（候选加入 / 归档 / 恢复）
#
# 覆盖：
# 1. ObservationRead 真实契约：ObservationRich.to_dict() 能构造出含全部字段的 ObservationRead
# 2. transition_candidate_to_observation（路由 from-candidate 现调用此服务）写入审计事件
# 3. 候选双击加入只产生 1 个观察项 + 1 个审计事件（幂等）
# 4. exclude_observation（路由 archive 现追加调用）写入审计事件
# 5. restore_observation（路由 restore 现追加调用）写入审计事件
# ============================================================================


def test_observation_read_contract_complete_fields(db_session):
    """【UAT-PAGES.1 P1-02 真实契约】ObservationRich.to_dict() 构造的 ObservationRead 含全部字段。

    前后端字段一致：前端 ObservationItem 接口与后端 ObservationRead 对齐，
    缺字段会导致前端渲染 undefined。本测试确保所有字段在响应中存在。
    """
    wl = _make_watchlist(db_session, name="QA-Contract")
    sym = _make_symbol(db_session, symbol="600900")
    pf = _make_portfolio(db_session, name="QA-Contract-PF")
    _make_position(db_session, portfolio_id=pf.id, symbol_id=sym.id)

    # 写入足够 DailyBar 以满足 credibility
    base_date = date(2024, 1, 1)
    for i in range(65):
        _make_daily_bar(
            db_session,
            symbol_id=sym.id,
            trade_date=base_date + timedelta(days=i),
            close=10.0 + i * 0.1,
        )
    _make_score(
        db_session,
        symbol_id=sym.id,
        trade_date=base_date + timedelta(days=64),
        quality_score=80.0,
        timing_score=75.0,
        priority_score=85.0,
    )

    item = obs_svc.idempotent_add_observation(
        db_session,
        watchlist_id=wl.id,
        symbol_id=sym.id,
        origin_type="manual",
        priority=50,
        tags=["契约", "测试"],
        target_portfolio_id=pf.id,
        note="契约测试",
    )

    rich = obs_svc.get_observation_rich(db_session, watchlist_item_id=item.id)
    data = rich.to_dict()

    # 构造 ObservationRead 不抛异常即证明字段齐全
    read = ObservationRead(**data)

    # 断言所有 ObservationRead 字段均存在（不缺字段，前端不会拿到 undefined）
    expected_fields = {
        "watchlist_item_id", "watchlist_id", "watchlist_name", "symbol_id",
        "symbol", "added_at", "updated_at", "archived_at", "origin_type",
        "origin_id", "reason", "score_snapshot", "status", "priority",
        "tags", "note", "target_portfolio_id", "target_portfolio_name",
        "latest_price", "latest_price_date", "price_change_pct",
        "latest_total_score", "latest_quality_score", "latest_timing_score",
        "latest_score_date", "data_credibility", "bar_count", "has_position",
        "position_portfolio_name", "degraded", "degraded_reason",
    }
    for field in expected_fields:
        assert hasattr(read, field), f"ObservationRead 缺少字段: {field}"

    # 关键字段值正确
    assert read.watchlist_item_id == item.id
    assert read.symbol == "600900"
    assert read.origin_type == "manual"
    assert read.priority == 50
    assert read.tags == ["契约", "测试"]
    assert read.target_portfolio_id == pf.id
    assert read.has_position is True
    assert read.position_portfolio_name == "QA-Contract-PF"
    assert read.latest_price is not None
    assert read.data_credibility is not None
    assert read.latest_total_score is not None


def test_transition_candidate_to_observation_writes_audit_event(db_session):
    """【UAT-PAGES.1 P1-02 审计】候选加入观察池写入 OpportunityTransitionEvent 审计事件。

    路由 POST /from-candidate 现调用 transition_candidate_to_observation，
    必须单事务写来源 + 评分快照 + 审计事件。
    """
    wl = _make_watchlist(db_session, name="QA-Audit-Cand")
    sym = _make_symbol(db_session, symbol="600901")
    run = _make_scan_run(db_session, name="QA-ScanRun-Audit")
    u = _make_universe_symbol(db_session, symbol="600901")
    candidate = _make_candidate(
        db_session,
        scan_run_id=run.id,
        universe_symbol_id=u.id,
        symbol="600901",
        quality_score=80.0,
        timing_score=74.0,
        priority_score=86.0,
    )

    item, event = trans_svc.transition_candidate_to_observation(
        db_session,
        candidate_id=candidate.id,
        watchlist_id=wl.id,
        note="审计测试",
        priority=5,
    )

    # 观察项已创建
    assert item.origin_type == "candidate"
    assert item.origin_id == candidate.id
    assert item.status == "watching"

    # 审计事件已创建
    assert event is not None
    assert event.event_type == EVENT_CANDIDATE_TO_OBSERVATION
    assert event.source_type == "candidate"
    assert event.source_id == candidate.id
    assert event.target_type == TYPE_OBSERVATION
    assert event.target_id == wl.id
    assert event.to_status == "observation"
    assert event.idempotency_key is not None

    # DB 中存在该审计事件
    events = db_session.execute(
        select(OpportunityTransitionEvent).where(
            OpportunityTransitionEvent.event_type == EVENT_CANDIDATE_TO_OBSERVATION,
            OpportunityTransitionEvent.source_id == candidate.id,
        )
    ).scalars().all()
    assert len(events) == 1


def test_transition_candidate_to_observation_double_click_idempotent(db_session):
    """【UAT-PAGES.1 P1-02 幂等】候选双击加入只产生 1 个观察项 + 1 个审计事件。

    前端双击"加入观察"按钮不应产生重复数据。
    """
    wl = _make_watchlist(db_session, name="QA-Double-Click")
    sym = _make_symbol(db_session, symbol="600902")
    run = _make_scan_run(db_session, name="QA-ScanRun-DBL")
    u = _make_universe_symbol(db_session, symbol="600902")
    candidate = _make_candidate(
        db_session,
        scan_run_id=run.id,
        universe_symbol_id=u.id,
        symbol="600902",
    )

    # 第一次点击
    item1, event1 = trans_svc.transition_candidate_to_observation(
        db_session, candidate_id=candidate.id, watchlist_id=wl.id
    )
    assert event1 is not None

    # 第二次点击（双击）
    item2, event2 = trans_svc.transition_candidate_to_observation(
        db_session, candidate_id=candidate.id, watchlist_id=wl.id
    )

    # 返回同一观察项，无新事件
    assert item2.id == item1.id
    assert event2 is None

    # DB 中只有 1 个观察项
    items = db_session.execute(
        select(WatchlistItem).where(WatchlistItem.watchlist_id == wl.id)
    ).scalars().all()
    assert len(items) == 1

    # DB 中只有 1 个候选→观察审计事件
    events = db_session.execute(
        select(OpportunityTransitionEvent).where(
            OpportunityTransitionEvent.event_type == EVENT_CANDIDATE_TO_OBSERVATION
        )
    ).scalars().all()
    assert len(events) == 1


def test_exclude_observation_writes_audit_event(db_session):
    """【UAT-PAGES.1 P1-02 审计】归档观察项写入 exclude 审计事件。

    路由 POST /archive 现追加调用 _audit_exclude_observation 写审计事件。
    """
    wl = _make_watchlist(db_session, name="QA-Audit-Archive")
    sym = _make_symbol(db_session, symbol="600903")
    run = _make_scan_run(db_session, name="QA-ScanRun-Arch")
    u = _make_universe_symbol(db_session, symbol="600903")
    candidate = _make_candidate(
        db_session,
        scan_run_id=run.id,
        universe_symbol_id=u.id,
        symbol="600903",
    )

    # 候选加入观察池
    item, _ = trans_svc.transition_candidate_to_observation(
        db_session, candidate_id=candidate.id, watchlist_id=wl.id
    )
    assert item.status == "watching"

    # 归档（路由层调用的审计服务）
    event = trans_svc.exclude_observation(
        db_session, watchlist_item_id=item.id, reason="手动归档"
    )

    # 审计事件已创建
    assert event is not None
    assert event.event_type == EVENT_EXCLUDE
    assert event.source_type == TYPE_OBSERVATION
    assert event.source_id == item.id

    # 观察项已归档
    db_session.refresh(item)
    assert item.status == "archived"
    assert item.archived_at is not None


def test_restore_observation_writes_audit_event(db_session):
    """【UAT-PAGES.1 P1-02 审计】恢复归档观察项写入 restore 审计事件。

    路由 POST /restore 现追加调用 _audit_restore_observation 写审计事件。
    """
    wl = _make_watchlist(db_session, name="QA-Audit-Restore")
    sym = _make_symbol(db_session, symbol="600904")
    run = _make_scan_run(db_session, name="QA-ScanRun-Rst")
    u = _make_universe_symbol(db_session, symbol="600904")
    candidate = _make_candidate(
        db_session,
        scan_run_id=run.id,
        universe_symbol_id=u.id,
        symbol="600904",
    )

    # 候选加入观察池
    item, _ = trans_svc.transition_candidate_to_observation(
        db_session, candidate_id=candidate.id, watchlist_id=wl.id
    )

    # 先归档
    trans_svc.exclude_observation(
        db_session, watchlist_item_id=item.id, reason="临时归档"
    )
    db_session.refresh(item)
    assert item.status == "archived"

    # 恢复（路由层调用的审计服务）
    event = trans_svc.restore_observation(
        db_session, watchlist_item_id=item.id
    )

    # 审计事件已创建
    assert event is not None
    assert event.event_type == EVENT_RESTORE
    assert event.source_type == TYPE_OBSERVATION
    assert event.source_id == item.id

    # 观察项已恢复
    db_session.refresh(item)
    assert item.status == "watching"
    assert item.archived_at is None


def test_observation_service_errors_mapped_to_404(db_session):
    """【UAT-PAGES.1 P1-02 错误展示】服务层抛 ValueError 时路由映射为 404 + NOT_FOUND 错误码。

    服务层对"对象不存在"抛 ValueError，路由层捕获并转为 HTTPException(404)，
    全局异常处理器包装为 WP-S.6 统一错误协议（error_code=NOT_FOUND）。
    本测试验证服务层错误行为，API 层映射见 test_whitebox_observations_api.py。
    """
    wl = _make_watchlist(db_session, name="QA-Err-404")

    # 候选不存在 → ValueError
    with pytest.raises(ValueError, match="not found"):
        trans_svc.transition_candidate_to_observation(
            db_session, candidate_id=99999, watchlist_id=wl.id
        )

    # 归档不存在的观察项 → 服务返回 None（路由层转 404）
    result = obs_svc.archive_observation(db_session, watchlist_item_id=99999)
    assert result is None

    # 恢复不存在的观察项 → 服务返回 None（路由层转 404）
    result = obs_svc.restore_observation(db_session, watchlist_item_id=99999)
    assert result is None


# ============================================================================
# UAT-PAGES.1 P1-02：统一错误协议 ERROR_CODE_LIBRARY 完整性 + next_actions
#
# 覆盖：
# 1. NOT_FOUND 错误码含 next_actions（不再只显示"重试"按钮，提供"返回观察池"动作）
# 2. DATA_NOT_READY 错误码存在于字典，含 redirect + sync + retry 三个 next_actions
# 3. build_user_error 对 DATA_NOT_READY 不再降级到 UNKNOWN_ERROR 文案
# ============================================================================


def test_error_code_library_not_found_has_next_actions():
    """【UAT-PAGES.1 P1-02 错误展示】NOT_FOUND 错误码含 next_actions。

    修复前：NOT_FOUND 仅含 user_message/impact/retryable，无 next_actions，
    前端只能显示"重试"按钮（retryable=False 时连重试都没有），用户无下一步动作。
    修复后：NOT_FOUND 含 dismiss next_action（"返回观察池"），用户可关闭错误返回列表。
    """
    from app.schemas.errors import ERROR_CODE_LIBRARY, build_user_error

    template = ERROR_CODE_LIBRARY["NOT_FOUND"]
    assert "next_actions" in template, "NOT_FOUND 必须含 next_actions"
    assert len(template["next_actions"]) >= 1
    # 至少包含一个 dismiss 动作（用户可关闭错误返回列表）
    action_types = [na["action_type"] for na in template["next_actions"]]
    assert "dismiss" in action_types, "NOT_FOUND 应提供 dismiss 动作关闭错误"

    # build_user_error 构造的 UserError 含 next_actions
    user_error = build_user_error("NOT_FOUND")
    assert user_error.error_code == "NOT_FOUND"
    assert user_error.retryable is False
    assert len(user_error.next_actions) >= 1
    assert any(a.action_type == "dismiss" for a in user_error.next_actions)


def test_error_code_library_data_not_ready_has_full_next_actions():
    """【UAT-PAGES.1 P1-02 错误展示】DATA_NOT_READY 错误码含完整 next_actions。

    修复前：DATA_NOT_READY 不在 ERROR_CODE_LIBRARY，build_user_error 降级到
    UNKNOWN_ERROR 文案（"服务暂时不可用，请稍后重试"），用户看到通用错误无下一步。
    修复后：DATA_NOT_READY 在字典中，含 redirect(前往基础数据) + sync(运行增量同步)
    + retry(数据就绪后重试) 三个具体动作，前端无需硬编码按钮即可展示完整下一步。
    """
    from app.schemas.errors import ERROR_CODE_LIBRARY, build_user_error

    template = ERROR_CODE_LIBRARY["DATA_NOT_READY"]
    assert template["retryable"] is True
    assert "next_actions" in template
    action_types = {na["action_type"] for na in template["next_actions"]}
    # 必须包含 redirect（前往基础数据）、sync（运行增量同步）、retry（重试）
    assert "redirect" in action_types, "DATA_NOT_READY 应提供 redirect 动作"
    assert "sync" in action_types, "DATA_NOT_READY 应提供 sync 动作"
    assert "retry" in action_types, "DATA_NOT_READY 应提供 retry 动作"

    # build_user_error 构造的 UserError 不降级到 UNKNOWN_ERROR 文案
    user_error = build_user_error("DATA_NOT_READY")
    assert user_error.error_code == "DATA_NOT_READY"
    # user_message 应为 DATA_NOT_READY 专属文案，而非 UNKNOWN_ERROR 的通用文案
    assert "数据未准备好" in user_error.user_message
    assert len(user_error.next_actions) >= 3


def test_error_code_library_unknown_error_still_fallback_for_missing_code():
    """【UAT-PAGES.1 P1-02 错误展示】未知 error_code 仍降级到 UNKNOWN_ERROR 文案。

    防御性：若后端误传未定义的 error_code，build_user_error 不抛 KeyError，
    降级到 UNKNOWN_ERROR 文案但保留原始 error_code 字段供排查。
    """
    from app.schemas.errors import build_user_error

    user_error = build_user_error("THIS_CODE_DOES_NOT_EXIST")
    # 保留原始 error_code（便于排查），但文案使用 UNKNOWN_ERROR
    assert user_error.error_code == "THIS_CODE_DOES_NOT_EXIST"
    assert "服务暂时不可用" in user_error.user_message
    assert user_error.retryable is True


def test_observation_read_contract_optional_fields_safe_for_frontend(db_session):
    """【UAT-PAGES.1 P1-02 真实契约】ObservationRead 可选字段为 None 时前端不会拿到 undefined。

    前端 ObservationItem 接口中所有富读字段（latest_price / latest_total_score /
    target_portfolio_name / has_position / degraded 等）均允许 null，
    后端 ObservationRead 必须显式返回这些字段（即使值为 None），
    不能省略字段，否则前端拿到 undefined 导致 .toFixed() 等调用崩溃。
    """
    wl = _make_watchlist(db_session, name="QA-Contract-None")
    sym = _make_symbol(db_session, symbol="600910")
    # 不创建 DailyBar / Score / Position → 所有富读字段应为 None / 默认值
    item = obs_svc.idempotent_add_observation(
        db_session, watchlist_id=wl.id, symbol_id=sym.id
    )

    rich = obs_svc.get_observation_rich(db_session, watchlist_item_id=item.id)
    data = rich.to_dict()

    # 构造 ObservationRead 不抛异常
    read = ObservationRead(**data)

    # 所有可能为 None 的字段在响应中显式存在（前端不会拿到 undefined）
    noneable_fields = [
        "watchlist_name", "symbol", "added_at", "updated_at", "archived_at",
        "origin_id", "reason", "score_snapshot", "note",
        "target_portfolio_id", "target_portfolio_name",
        "latest_price", "latest_price_date", "price_change_pct",
        "latest_total_score", "latest_quality_score", "latest_timing_score",
        "latest_score_date", "data_credibility", "bar_count",
        "position_portfolio_name", "degraded_reason",
    ]
    for field in noneable_fields:
        assert hasattr(read, field), f"ObservationRead 缺少字段: {field}"

    # 关键富读字段在无数据时为 None（前端用 != null 判断后渲染 "-"）
    assert read.latest_price is None
    assert read.latest_total_score is None
    assert read.target_portfolio_name is None
    assert read.data_credibility is None  # 无 DailyBar 时为 None
    assert read.has_position is False  # 默认 False
    assert read.degraded is False
