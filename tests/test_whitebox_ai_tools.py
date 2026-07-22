"""白盒测试 - WP-AI.4 第一阶段只读工具集。

覆盖：
1. get_capabilities 能力门禁
2. get_data_health 数据健康
3. get_task_status 任务状态
4. get_symbol_research 标的研究
5. get_candidate_explanation 候选解释
6. get_portfolio_summary 组合摘要
7. get_backtest_explanation 回测解释
8. 缺数据返回 no_data
9. 只读不写 DB
10. 7 个工具注册完整
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import inspect as sa_inspect

from app.services.ai.context_pack import ContextPack, build_context_pack
from app.services.ai.tools import TOOL_REGISTRY, call_tool
from app.services.ai.tools.backtest_explanation import get_backtest_explanation
from app.services.ai.tools.candidate_explanation import get_candidate_explanation
from app.services.ai.tools.capabilities import get_capabilities
from app.services.ai.tools.data_health import get_data_health
from app.services.ai.tools.portfolio_summary import get_portfolio_summary
from app.services.ai.tools.symbol_research import get_symbol_research
from app.services.ai.tools.task_status import get_task_status


pytestmark = pytest.mark.whitebox


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _make_symbol(db_session, **kwargs):
    from app.models.symbol import Symbol
    defaults = {
        "symbol": "600010", "name": "测试股票", "asset_type": "stock",
        "market": "cn", "board": "main", "is_st": 0, "is_active": 1,
    }
    defaults.update(kwargs)
    sym = Symbol(**defaults)
    db_session.add(sym)
    db_session.commit()
    db_session.refresh(sym)
    return sym


def _make_daily_bar(db_session, symbol_id, *, trade_date=None, close=10.5):
    from app.models.daily_bar import DailyBar
    if trade_date is None:
        trade_date = _utcnow_naive().date()
    bar = DailyBar(
        symbol_id=symbol_id, trade_date=trade_date,
        open=10.0, high=11.0, low=9.8, close=close,
        volume=1000000.0, amount=10500000.0, turnover_rate=0.01,
    )
    db_session.add(bar)
    db_session.commit()
    return bar


def _make_portfolio(db_session, **kwargs):
    from app.models.portfolio import Portfolio
    defaults = {
        "name": "测试组合", "account_type": "simulated",
        "total_capital": 100000.0, "investable_ratio": 0.8,
        "cash_reserve_ratio": 0.2, "currency": "CNY", "is_default": 0,
    }
    defaults.update(kwargs)
    p = Portfolio(**defaults)
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


def _make_score(db_session, symbol_id, *, trade_date=None):
    from app.models.score import Score
    if trade_date is None:
        trade_date = _utcnow_naive().date()
    s = Score(
        symbol_id=symbol_id, trade_date=trade_date,
        quality_score=75.5, quality_grade="B",
        timing_score=68.2, stage="hold", action="watch",
        priority_score=72.0, data_credibility=0.92,
        weight_mode="manual", calc_batch_id="test-batch",
    )
    db_session.add(s)
    db_session.commit()
    return s


def _make_backtest_run(db_session, portfolio_id, **kwargs):
    from app.models.backtest import BacktestRun
    defaults = {
        "portfolio_id": portfolio_id,
        "run_name": "测试回测",
        "symbols_json": "[]",
        "rule_config_json": "{}",
        "start_date": date(2024, 1, 1),
        "end_date": date(2024, 6, 30),
        "initial_capital": 100000.0,
        "total_return_pct": 12.5,
        "max_drawdown_pct": 0.08,
        "sharpe_ratio": 1.5,
        "win_rate": 0.6,
        "trade_count": 20,
        "status": "completed",
    }
    defaults.update(kwargs)
    run = BacktestRun(**defaults)
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)
    return run


def _make_context(db_session, **refs) -> ContextPack:
    """构造一个简单的 ContextPack 用于工具测试。"""
    return build_context_pack(
        db_session,
        user_question="测试问题",
        source_page=refs.pop("source_page", "research"),
        references=refs,
    )


# ----------------------------------------------------------------------------
# 1. get_capabilities 能力门禁
# ----------------------------------------------------------------------------


def test_get_capabilities(db_session):
    """【WP-AI.4】get_capabilities 返回当前系统能力门禁。"""
    ctx = _make_context(db_session, source_page="research")
    result = get_capabilities(db_session, ctx)

    assert result["status"] == "ok"
    data = result["data"]
    assert "current_gate" in data
    assert data["current_gate"] in ("ready", "degraded", "blocked", "unknown")
    assert isinstance(data["allowed_next_step"], list)
    assert len(data["allowed_next_step"]) > 0
    assert "capabilities" in data
    assert isinstance(data["capabilities"], list)


# ----------------------------------------------------------------------------
# 2. get_data_health 数据健康
# ----------------------------------------------------------------------------


def test_get_data_health(db_session):
    """【WP-AI.4】get_data_health 返回数据健康状态。"""
    sym = _make_symbol(db_session)
    _make_daily_bar(db_session, sym.id, close=12.34)
    ctx = _make_context(db_session, symbol_id=sym.id)

    result = get_data_health(db_session, ctx)

    assert result["status"] == "ok"
    data = result["data"]
    assert "latest_trade_date" in data
    assert data["latest_trade_date"] is not None
    assert data["total_symbols"] >= 1
    assert data["covered_symbols"] >= 1
    assert "coverage_pct" in data
    assert isinstance(data["missing_items"], list)


# ----------------------------------------------------------------------------
# 3. get_task_status 任务状态
# ----------------------------------------------------------------------------


def test_get_task_status(db_session):
    """【WP-AI.4】get_task_status 返回任务状态。"""
    from app.models.scheduled_task import ScheduledTask
    task = ScheduledTask(
        name="测试调度任务", task_type="market_data_sync",
        frequency="daily", time_of_day="09:00",
        timezone="Asia/Shanghai", payload_json="{}", enabled=1,
        next_run_at=_utcnow_naive() + timedelta(hours=1),
    )
    db_session.add(task)
    db_session.commit()

    ctx = _make_context(db_session, source_page="task")
    result = get_task_status(db_session, ctx)

    assert result["status"] == "ok"
    data = result["data"]
    assert "scheduled_tasks" in data
    assert len(data["scheduled_tasks"]) >= 1
    assert data["scheduled_tasks"][0]["name"] == "测试调度任务"
    assert "next_run_at" in data


# ----------------------------------------------------------------------------
# 4. get_symbol_research 标的研究
# ----------------------------------------------------------------------------


def test_get_symbol_research(db_session):
    """【WP-AI.4】get_symbol_research 返回标的研究数据。"""
    sym = _make_symbol(db_session, symbol="600020")
    _make_daily_bar(db_session, sym.id, close=15.6)
    _make_score(db_session, sym.id)

    ctx = _make_context(db_session, symbol_id=sym.id)
    result = get_symbol_research(db_session, ctx)

    assert result["status"] == "ok"
    data = result["data"]
    assert data["symbol"]["symbol"] == "600020"
    assert data["symbol"]["name"] == "测试股票"
    assert data["latest_bar"]["close"] == 15.6
    assert data["latest_score"]["quality_score"] == 75.5
    assert data["latest_score"]["quality_grade"] == "B"


# ----------------------------------------------------------------------------
# 5. get_candidate_explanation 候选解释
# ----------------------------------------------------------------------------


def test_get_candidate_explanation(db_session):
    """【WP-AI.4】get_candidate_explanation 返回候选解释（基于 ScanResult）。"""
    from app.models.scan import ScanResult, ScanRun
    from app.models.portfolio import Portfolio

    portfolio = _make_portfolio(db_session)
    sym = _make_symbol(db_session, symbol="600030")
    run = ScanRun(
        run_name="测试扫描", scope_snapshot="{}",
        status="completed", portfolio_id=portfolio.id,
    )
    db_session.add(run)
    db_session.commit()

    result_row = ScanResult(
        scan_run_id=run.id, symbol_id=sym.id, result_type="candidate",
        rank_no=1, quality_score=80.0, timing_score=70.0, priority_score=75.0,
        stage="hold", action="watch", is_frozen=0, is_active=1,
        reason_tags=json.dumps(["量价齐升", "趋势向上"]),
    )
    db_session.add(result_row)
    db_session.commit()
    db_session.refresh(result_row)

    ctx = _make_context(db_session, scan_result_id=result_row.id)
    result = get_candidate_explanation(db_session, ctx)

    assert result["status"] == "ok"
    data = result["data"]
    assert data["candidate"]["symbol_id"] == sym.id
    assert data["scores"]["quality_score"] == 80.0
    assert data["rank"] == 1
    assert "量价齐升" in data["reason_tags"]


# ----------------------------------------------------------------------------
# 6. get_portfolio_summary 组合摘要
# ----------------------------------------------------------------------------


def test_get_portfolio_summary(db_session):
    """【WP-AI.4】get_portfolio_summary 返回组合摘要。"""
    portfolio = _make_portfolio(db_session, name="AI测试组合")
    sym = _make_symbol(db_session, symbol="600040")
    _make_daily_bar(db_session, sym.id, close=20.0)

    from app.models.portfolio import Position
    pos = Position(
        portfolio_id=portfolio.id, symbol_id=sym.id,
        quantity=500, avg_cost=15.0, latest_price=20.0,
        market_value=10000.0, position_pct=0.1,
        asset_type="stock",
    )
    db_session.add(pos)
    db_session.commit()

    ctx = _make_context(db_session, portfolio_id=portfolio.id)
    result = get_portfolio_summary(db_session, ctx)

    assert result["status"] == "ok"
    data = result["data"]
    assert data["portfolio"]["name"] == "AI测试组合"
    assert data["portfolio"]["account_type"] == "simulated"
    assert len(data["positions"]) >= 1
    assert data["positions"][0]["symbol_id"] == sym.id
    # 模拟账户应有账户摘要
    assert data["account_summary"] is not None
    assert "total_equity" in data["account_summary"]


# ----------------------------------------------------------------------------
# 7. get_backtest_explanation 回测解释
# ----------------------------------------------------------------------------


def test_get_backtest_explanation(db_session):
    """【WP-AI.4】get_backtest_explanation 返回回测解释。"""
    portfolio = _make_portfolio(db_session, name="回测组合")
    run = _make_backtest_run(db_session, portfolio.id)

    ctx = _make_context(db_session, portfolio_id=portfolio.id, backtest_id=run.id)
    result = get_backtest_explanation(db_session, ctx)

    assert result["status"] == "ok"
    data = result["data"]
    assert data["run"]["id"] == run.id
    assert data["run"]["run_name"] == "测试回测"
    assert data["run"]["status"] == "completed"
    assert data["metrics"]["total_return_pct"] == 12.5
    assert data["metrics"]["sharpe_ratio"] == 1.5
    assert data["metrics"]["win_rate"] == 0.6
    assert "config" in data
    assert "rule_config" in data["config"]


# ----------------------------------------------------------------------------
# 8. 缺数据返回 no_data
# ----------------------------------------------------------------------------


def test_tool_returns_no_data_when_empty(db_session):
    """【WP-AI.4】缺数据时返回 {"status": "no_data", "message": "..."}。"""
    # 不提供 symbol_id
    ctx = _make_context(db_session, source_page="research")

    result = get_symbol_research(db_session, ctx)
    assert result["status"] == "no_data"
    assert "message" in result

    # 不提供 portfolio_id
    result = get_portfolio_summary(db_session, ctx)
    assert result["status"] == "no_data"
    assert "message" in result

    # 不提供 backtest_id/portfolio_id
    result = get_backtest_explanation(db_session, ctx)
    assert result["status"] == "no_data"
    assert "message" in result


# ----------------------------------------------------------------------------
# 9. 只读不写 DB
# ----------------------------------------------------------------------------


def test_tool_does_not_write_db(db_session):
    """【WP-AI.4】只读工具不产生任何数据库变化。"""
    sym = _make_symbol(db_session, symbol="600050")
    _make_daily_bar(db_session, sym.id)
    _make_score(db_session, sym.id)

    # 记录调用前的所有表行数
    engine = db_session.bind
    inspector = sa_inspect(engine)
    table_names = [t for t in inspector.get_table_names()
                   if not t.startswith("ai_") and not t.startswith("sqlite_")]

    before_counts: dict[str, int] = {}
    from sqlalchemy import text
    for tname in table_names:
        try:
            cnt = db_session.execute(text(f"SELECT COUNT(*) FROM {tname}")).scalar()
            before_counts[tname] = int(cnt or 0)
        except Exception:
            pass

    ctx = _make_context(db_session, symbol_id=sym.id, source_page="research")

    # 调用所有 7 个只读工具
    for tool_name, fn in TOOL_REGISTRY.items():
        fn(db_session, ctx)

    # 验证所有表行数未变化
    for tname, before_cnt in before_counts.items():
        after_cnt = db_session.execute(text(f"SELECT COUNT(*) FROM {tname}")).scalar()
        assert int(after_cnt or 0) == before_cnt, (
            f"工具调用后表 {tname} 行数变化: {before_cnt} -> {after_cnt}"
        )


# ----------------------------------------------------------------------------
# 10. 7 个工具注册完整
# ----------------------------------------------------------------------------


def test_tool_registry_complete():
    """【WP-AI.4】TOOL_REGISTRY 包含 7 个工具。"""
    expected_tools = {
        "get_capabilities",
        "get_data_health",
        "get_task_status",
        "get_symbol_research",
        "get_candidate_explanation",
        "get_portfolio_summary",
        "get_backtest_explanation",
    }
    assert set(TOOL_REGISTRY.keys()) == expected_tools
    # 每个工具都是可调用对象
    for name, fn in TOOL_REGISTRY.items():
        assert callable(fn), f"工具 {name} 不可调用"


# ----------------------------------------------------------------------------
# 11. call_tool 统一入口
# ----------------------------------------------------------------------------


def test_call_tool_dispatch(db_session):
    """【WP-AI.4】call_tool 按名称分派到对应工具。"""
    sym = _make_symbol(db_session, symbol="600060")
    _make_daily_bar(db_session, sym.id)

    ctx = _make_context(db_session, symbol_id=sym.id)
    result = call_tool("get_symbol_research", db_session, ctx)
    assert result["status"] == "ok"

    # 未知工具应抛 KeyError
    with pytest.raises(KeyError):
        call_tool("nonexistent_tool", db_session, ctx)


# ----------------------------------------------------------------------------
# 12. 不可信内容标记（新闻）
# ----------------------------------------------------------------------------


def test_symbol_research_marks_news_untrusted(db_session):
    """【WP-AI.4】get_symbol_research 返回的新闻标记 is_untrusted: true。"""
    from app.models.news_event import NewsEvent
    sym = _make_symbol(db_session, symbol="600070")
    _make_daily_bar(db_session, sym.id)
    news = NewsEvent(
        symbol_id=sym.id, symbol="600070",
        title="利好消息", source="external",
        event_type="news", sentiment="positive",
        risk_level="low", published_at=_utcnow_naive(),
    )
    db_session.add(news)
    db_session.commit()

    ctx = _make_context(db_session, symbol_id=sym.id)
    result = get_symbol_research(db_session, ctx)

    assert result["status"] == "ok"
    for item in result["data"]["news"]:
        assert item["is_untrusted"] is True
