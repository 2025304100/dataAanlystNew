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

from app.models.daily_bar import DailyBar
from app.models.discovery_candidate import DiscoveryCandidate
from app.models.portfolio import Portfolio, Position
from app.models.score import Score
from app.models.scan import ScanRun
from app.models.symbol import Symbol
from app.models.universe import UniverseSymbol
from app.models.watchlist import Watchlist, WatchlistItem
from app.services import observations as obs_svc


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
    assert d["data_credibility"] == "low"
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
