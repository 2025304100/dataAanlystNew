"""全市场资金流快照抓取与日更任务的分叉（capital_flow 增量必须只抓一次排行）。"""
from __future__ import annotations

import json
from datetime import date

import pandas as pd
import pytest

from app.db.manager import DatabaseManager
from app.models.async_task import AsyncTaskRecord
from app.models.capital_flow import CapitalFlow
from app.models.symbol import Symbol
from app.services import capital_flow_data
from app.services import external_data_sync_task as service
from app.services.capital_flow_data import sync_market_capital_flow_snapshot


pytestmark = pytest.mark.whitebox


def _cn_symbols(db_session, codes: list[str]) -> list[Symbol]:
    symbols = [
        Symbol(
            symbol=code,
            name=f"stock {code}",
            asset_type="stock",
            market="sh" if code.startswith("6") else "sz",
        )
        for code in codes
    ]
    db_session.add_all(symbols)
    db_session.flush()
    return symbols


def _rank_frame(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame([
        {
            "代码": row["code"],
            "名称": row.get("name", "x"),
            "今日主力净流入-净额": row.get("main"),
            "今日主力净流入-净占比": row.get("main_pct"),
            "今日超大单净流入-净额": row.get("super_large"),
            "今日大单净流入-净额": row.get("large"),
            "今日中单净流入-净额": row.get("medium"),
            "今日小单净流入-净额": row.get("small"),
        }
        for row in rows
    ])


def _dated_history_frame(*dates: date) -> pd.DataFrame:
    return pd.DataFrame([
        {
            "日期": item.isoformat(),
            "主力净流入-净额": 5.0,
            "主力净流入-净占比": 1.0,
            "超大单净流入-净额": 1.0,
            "大单净流入-净额": 1.0,
            "中单净流入-净额": 1.0,
            "小单净流入-净额": 1.0,
        }
        for item in sorted(dates)
    ])


def _fake_provider(rank_frame: pd.DataFrame, session_date: date | None):
    """Rank endpoint carries no date; the dated per-symbol endpoint is probed too."""
    history = _dated_history_frame(session_date) if session_date else pd.DataFrame()

    def call(_func, *args, **kwargs):
        if kwargs.get("api_key") == "stock_individual_fund_flow_rank":
            return rank_frame
        return history

    return call


def test_market_capital_flow_snapshot_fetches_full_market_only_once(db_session, monkeypatch):
    symbols = _cn_symbols(db_session, ["600000", "000001", "000002"])
    frame = _rank_frame([
        {"code": "600000", "main": 1_000.0, "main_pct": 3.5, "super_large": 600.0,
         "large": 400.0, "medium": -100.0, "small": -900.0},
        {"code": "000001", "main": -2_000.0, "main_pct": -1.5, "super_large": -800.0,
         "large": -1_200.0, "medium": 300.0, "small": 1_700.0},
    ])
    calls: list[dict] = []

    def fake_call(_func, *args, **kwargs):
        calls.append(kwargs)
        return _fake_provider(frame, date(2026, 10, 9))(_func, *args, **kwargs)

    monkeypatch.setattr(capital_flow_data, "call_akshare_with_retry", fake_call)

    synced = sync_market_capital_flow_snapshot(db_session, symbols, date(2026, 10, 9))
    db_session.commit()

    rank_calls = [item for item in calls if item.get("api_key") == "stock_individual_fund_flow_rank"]
    assert len(rank_calls) == 1
    assert rank_calls[0]["indicator"] == "今日"
    assert synced == {symbols[0].id, symbols[1].id}
    rows = db_session.query(CapitalFlow).order_by(CapitalFlow.symbol_id).all()
    assert len(rows) == 2
    assert rows[0].main_net_inflow == pytest.approx(1_000.0)
    assert rows[0].main_net_inflow_pct == pytest.approx(3.5)
    assert rows[0].super_large_net_inflow == pytest.approx(600.0)
    assert rows[0].small_net_inflow == pytest.approx(-900.0)
    assert all(row.trade_date == date(2026, 10, 9) for row in rows)
    # 000002 不在 provider 返回的排行里：不补零、不造行
    assert db_session.query(CapitalFlow).filter(
        CapitalFlow.symbol_id == symbols[2].id
    ).count() == 0


def test_market_capital_flow_snapshot_stamps_the_providers_session_not_the_calendar_day(
    db_session, monkeypatch
):
    """国庆/周末跑日更：排行接口给的是上一交易日，日期必须跟着 provider 走。"""
    symbols = _cn_symbols(db_session, ["600000"])
    frame = _rank_frame([{"code": "600000", "main": 700.0}])
    monkeypatch.setattr(
        capital_flow_data,
        "call_akshare_with_retry",
        _fake_provider(frame, date(2026, 9, 30)),
    )

    sync_market_capital_flow_snapshot(db_session, symbols, date(2026, 10, 1))
    db_session.commit()

    row = db_session.query(CapitalFlow).one()
    assert row.trade_date == date(2026, 9, 30)


def test_market_capital_flow_snapshot_writes_nothing_without_a_session_date(
    db_session, monkeypatch
):
    """日期无从印证时必须失败关闭，不能把当日流水挂到无交易日的行上。"""
    symbols = _cn_symbols(db_session, ["600000"])
    frame = _rank_frame([{"code": "600000", "main": 700.0}])
    monkeypatch.setattr(
        capital_flow_data,
        "call_akshare_with_retry",
        _fake_provider(frame, None),
    )

    with pytest.raises(RuntimeError, match="no corroborating session date"):
        sync_market_capital_flow_snapshot(db_session, symbols, date(2026, 10, 9))

    assert db_session.query(CapitalFlow).count() == 0


def test_market_capital_flow_snapshot_skips_rows_without_main_inflow(db_session, monkeypatch):
    symbols = _cn_symbols(db_session, ["600000", "600001"])
    frame = _rank_frame([
        {"code": "600000", "main": None, "super_large": 10.0},
        {"code": "600001", "main": 50.0},
    ])
    monkeypatch.setattr(
        capital_flow_data,
        "call_akshare_with_retry",
        _fake_provider(frame, date(2026, 10, 9)),
    )

    synced = sync_market_capital_flow_snapshot(db_session, symbols, date(2026, 10, 9))
    db_session.commit()

    assert synced == {symbols[1].id}
    assert db_session.query(CapitalFlow).count() == 1


def test_market_capital_flow_snapshot_raises_on_unusable_frame(db_session, monkeypatch):
    """空/畸形返回必须抛错，否则任务不会切到个股接口兜底而是静默零写入。"""
    symbols = _cn_symbols(db_session, ["600000"])
    monkeypatch.setattr(
        capital_flow_data,
        "call_akshare_with_retry",
        _fake_provider(pd.DataFrame(), date(2026, 10, 9)),
    )

    with pytest.raises(RuntimeError, match="no usable rows"):
        sync_market_capital_flow_snapshot(db_session, symbols, date(2026, 10, 9))


def test_market_capital_flow_snapshot_ignores_etf_and_non_cn_symbols(db_session, monkeypatch):
    symbols = _cn_symbols(db_session, ["600000"])
    symbols.append(Symbol(
        symbol="510300", name="ETF", asset_type="etf", market="sh"
    ))
    symbols.append(Symbol(
        symbol="AAPL", name="Apple", asset_type="stock", market="us"
    ))
    db_session.add_all(symbols[1:])
    db_session.flush()
    monkeypatch.setattr(
        capital_flow_data,
        "call_akshare_with_retry",
        _fake_provider(_rank_frame([{"code": "600000", "main": 1.0}]), date(2026, 10, 9)),
    )

    synced = sync_market_capital_flow_snapshot(db_session, symbols, date(2026, 10, 9))

    assert synced == {symbols[0].id}
    assert db_session.query(CapitalFlow).count() == 1


def test_fundamental_incremental_still_uses_one_market_snapshot_after_branch_merge(
    db_session, monkeypatch
):
    """合并分支后估值路径不能退化：仍是 1 次全市场抓取 + 个股接口兜底。"""
    symbols = _cn_symbols(db_session, ["600000", "000001"])
    task = AsyncTaskRecord(
        id="fund-incremental-snapshot",
        task_type="external_sync_fundamental",
        status="running",
        stage="prepare",
        payload_json=json.dumps({"dataset": "fundamental", "source": "all"}),
    )
    db_session.add(task)
    db_session.commit()
    monkeypatch.setattr(
        service, "resolve_external_symbols", lambda *_args, **_kwargs: symbols
    )
    calls: dict[str, int] = {"snapshot": 0, "per_symbol": 0}

    def fake_snapshot(_db, syms, trade_date=None):
        calls["snapshot"] += 1
        return {symbol.id for symbol in syms}

    def fake_per_symbol(_db, symbol, trade_date=None):
        calls["per_symbol"] += 1
        return object()

    import app.services.fundamental_data as fundamental_data

    monkeypatch.setattr(
        fundamental_data, "sync_market_valuation_snapshot", fake_snapshot
    )
    monkeypatch.setattr(fundamental_data, "sync_symbol_valuation", fake_per_symbol)

    payload = {
        "dataset": "fundamental", "source": "all", "mode": "incremental",
        "include_northbound": False,
    }
    plan = service.build_external_sync_plan("fundamental", payload, today=date(2026, 10, 9))
    result = service._run_symbol_sync(
        db_session, task.id, "fundamental", {**payload, "plan": plan}
    )

    assert calls == {"snapshot": 1, "per_symbol": 0}
    assert result["success"] == 2
    assert result["records"] == 2


def _running_task(db_session, task_id: str) -> AsyncTaskRecord:
    task = AsyncTaskRecord(
        id=task_id,
        task_type="external_sync_capital_flow",
        status="running",
        stage="prepare",
        payload_json=json.dumps({"dataset": "capital_flow", "source": "all"}),
    )
    db_session.add(task)
    db_session.commit()
    return task


def test_capital_flow_incremental_task_uses_one_market_snapshot(db_session, monkeypatch):
    symbols = _cn_symbols(db_session, ["600000", "000001"])
    task = _running_task(db_session, "cf-incremental-snapshot")
    monkeypatch.setattr(
        service, "resolve_external_symbols", lambda *_args, **_kwargs: symbols
    )
    calls: dict[str, int] = {"snapshot": 0, "per_symbol": 0, "northbound": 0}

    def fake_snapshot(_db, syms, trade_date=None):
        calls["snapshot"] += 1
        return {symbol.id for symbol in syms}

    def fake_per_symbol(_db, _symbol, trade_date=None):
        calls["per_symbol"] += 1
        return object()

    def fake_northbound(_db, days=30):
        calls["northbound"] += 1
        return 0

    monkeypatch.setattr(
        capital_flow_data, "sync_market_capital_flow_snapshot", fake_snapshot
    )
    monkeypatch.setattr(capital_flow_data, "sync_symbol_capital_flow", fake_per_symbol)
    monkeypatch.setattr(capital_flow_data, "sync_northbound_flow", fake_northbound)

    payload = {
        "dataset": "capital_flow", "source": "all", "mode": "incremental",
        "include_northbound": True,
    }
    plan = service.build_external_sync_plan("capital_flow", payload, today=date(2026, 10, 9))
    result = service._run_symbol_sync(
        db_session, task.id, "capital_flow", {**payload, "plan": plan}
    )

    # 5,000+ 标的的日更必须是 1 次全市场抓取，不是 N 次个股抓取
    assert calls["snapshot"] == 1
    assert calls["per_symbol"] == 0
    assert calls["northbound"] == 1
    assert result["success"] == 2
    assert result["records"] == 2
    assert result["total"] == 2


def test_capital_flow_incremental_falls_back_to_per_symbol_when_snapshot_fails(
    db_session, monkeypatch
):
    symbols = _cn_symbols(db_session, ["600000", "000001"])
    task = _running_task(db_session, "cf-incremental-fallback")
    monkeypatch.setattr(
        service, "resolve_external_symbols", lambda *_args, **_kwargs: symbols
    )
    monkeypatch.setattr(
        service, "SessionLocal", DatabaseManager.get().session_factory
    )
    seen: list[str] = []

    def broken_snapshot(*_args, **_kwargs):
        raise RuntimeError("provider disconnected")

    def fake_per_symbol(_db, symbol, trade_date=None):
        seen.append(symbol.symbol)
        return object() if symbol.symbol == "600000" else None

    monkeypatch.setattr(
        capital_flow_data, "sync_market_capital_flow_snapshot", broken_snapshot
    )
    monkeypatch.setattr(capital_flow_data, "sync_symbol_capital_flow", fake_per_symbol)
    monkeypatch.setattr(
        capital_flow_data, "sync_northbound_flow", lambda *_a, **_k: 0
    )

    payload = {
        "dataset": "capital_flow", "source": "watchlist", "mode": "incremental",
        "include_northbound": False, "max_workers": 1,
    }
    plan = service.build_external_sync_plan("capital_flow", payload, today=date(2026, 10, 9))
    result = service._run_symbol_sync(
        db_session, task.id, "capital_flow", {**payload, "plan": plan}
    )

    assert sorted(seen) == ["000001", "600000"]
    assert result["success"] == 1
    assert result["skipped"] == 1
    assert any("批量接口不可用" in message for message in result["errors"])


def test_capital_flow_all_scope_does_not_fan_out_per_symbol_when_snapshot_fails(
    db_session, monkeypatch
):
    """全市场 scope 的排行接口挂了必须显式失败，不能悄悄变成 5,000+ 次个股请求。"""
    symbols = _cn_symbols(db_session, ["600000", "000001"])
    task = _running_task(db_session, "cf-all-scope-refuses-fanout")
    monkeypatch.setattr(
        service, "resolve_external_symbols", lambda *_args, **_kwargs: symbols
    )
    calls: list[str] = []

    def broken_snapshot(*_args, **_kwargs):
        raise RuntimeError("provider disconnected")

    def fake_per_symbol(_db, symbol, trade_date=None):
        calls.append(symbol.symbol)
        return object()

    monkeypatch.setattr(
        capital_flow_data, "sync_market_capital_flow_snapshot", broken_snapshot
    )
    monkeypatch.setattr(capital_flow_data, "sync_symbol_capital_flow", fake_per_symbol)
    monkeypatch.setattr(
        capital_flow_data, "sync_northbound_flow", lambda *_a, **_k: 0
    )

    payload = {
        "dataset": "capital_flow", "source": "all", "mode": "incremental",
        "include_northbound": False, "max_workers": 1,
    }
    plan = service.build_external_sync_plan("capital_flow", payload, today=date(2026, 10, 9))

    with pytest.raises(RuntimeError, match="未执行逐标的兜底"):
        service._run_symbol_sync(
            db_session, task.id, "capital_flow", {**payload, "plan": plan}
        )

    assert calls == []


def test_capital_flow_backfill_still_uses_per_symbol_range_endpoint(db_session, monkeypatch):
    """回填窗口只能走个股历史接口：排行接口只有当日，无法回答区间。"""
    symbols = _cn_symbols(db_session, ["600000"])
    task = _running_task(db_session, "cf-backfill-range")
    monkeypatch.setattr(
        service, "resolve_external_symbols", lambda *_args, **_kwargs: symbols
    )
    monkeypatch.setattr(service, "_wait_for_market_priority", lambda *_args: True)
    calls: dict[str, int] = {"snapshot": 0, "range": 0}
    monkeypatch.setattr(
        capital_flow_data,
        "sync_market_capital_flow_snapshot",
        lambda *_a, **_k: calls.__setitem__("snapshot", calls["snapshot"] + 1) or set(),
    )
    monkeypatch.setattr(
        capital_flow_data,
        "sync_symbol_capital_flow_range",
        lambda *_a, **_k: calls.__setitem__("range", calls["range"] + 1) or 3,
    )

    payload = {
        "dataset": "capital_flow", "source": "all", "mode": "backfill",
        "start_date": "2026-09-01", "end_date": "2026-09-30",
        "lookback_days": 30, "include_northbound": False,
    }
    plan = service.build_external_sync_plan("capital_flow", payload, today=date(2026, 10, 9))
    result = service._run_symbol_sync(
        db_session, task.id, "capital_flow", {**payload, "plan": plan}
    )

    assert calls == {"snapshot": 0, "range": 1}
    assert result["records"] == 3


def test_history_fetch_separates_empty_response_from_request_failure(db_session, monkeypatch):
    """provider 答"没有这只票" 与 "请求失败" 是两种结果，不能都塌成空列表。"""
    symbols = _cn_symbols(db_session, ["600000"])
    monkeypatch.setattr(
        capital_flow_data,
        "call_akshare_with_retry",
        lambda *a, **k: pd.DataFrame(),
    )
    assert capital_flow_data._fetch_individual_fund_flow_history(
        db_session, symbols[0], start_date=date(2026, 8, 25), end_date=date(2026, 9, 30)
    ) == []

    def boom(*_a, **_k):
        raise ConnectionError("Remote end closed connection without response")

    monkeypatch.setattr(capital_flow_data, "call_akshare_with_retry", boom)
    with pytest.raises(capital_flow_data.FundFlowProviderError, match="ConnectionError"):
        capital_flow_data._fetch_individual_fund_flow_history(
            db_session, symbols[0], start_date=date(2026, 8, 25), end_date=date(2026, 9, 30)
        )


def test_range_sync_propagates_provider_failure(db_session, monkeypatch):
    symbols = _cn_symbols(db_session, ["600000"])

    def boom(*_a, **_k):
        raise ConnectionError("Remote end closed connection without response")

    monkeypatch.setattr(capital_flow_data, "call_akshare_with_retry", boom)

    with pytest.raises(capital_flow_data.FundFlowProviderError):
        capital_flow_data.sync_symbol_capital_flow_range(
            db_session, symbols[0], start_date=date(2026, 8, 25), end_date=date(2026, 9, 30)
        )


def test_backfill_task_counts_provider_failure_as_failed_not_skipped(db_session, monkeypatch):
    """回填被掐网时必须记成 failed：否则 5,554 只跑完显示 done/skipped 无法验收。"""
    symbols = _cn_symbols(db_session, ["600000", "000001"])
    task = _running_task(db_session, "cf-backfill-provider-failure")
    monkeypatch.setattr(
        service, "resolve_external_symbols", lambda *_args, **_kwargs: symbols
    )
    monkeypatch.setattr(service, "_wait_for_market_priority", lambda *_args: True)

    def boom(*_a, **_k):
        raise capital_flow_data.FundFlowProviderError("600000: ConnectionError: aborted")

    monkeypatch.setattr(capital_flow_data, "sync_symbol_capital_flow_range", boom)

    payload = {
        "dataset": "capital_flow", "source": "all", "mode": "backfill",
        "start_date": "2026-08-25", "end_date": "2026-09-30",
        "lookback_days": 37, "include_northbound": False,
    }
    plan = service.build_external_sync_plan("capital_flow", payload, today=date(2026, 10, 9))
    result = service._run_symbol_sync(
        db_session, task.id, "capital_flow", {**payload, "plan": plan}
    )

    assert result["failed"] == 2
    assert result["skipped"] == 0
    assert result["success"] == 0
    assert any("FundFlowProviderError" in message or "ConnectionError" in message
               for message in result["errors"])


def test_scoring_on_demand_path_still_returns_stale_row_on_failure(db_session, monkeypatch):
    """评分按需拉取不能被请求失败打断：宁可回旧行，也不抛到打分链里。"""
    symbols = _cn_symbols(db_session, ["600000"])
    stale = CapitalFlow(
        symbol_id=symbols[0].id, trade_date=date(2026, 8, 20),
        main_net_inflow=1.0, main_net_inflow_score=55.0, source="test",
    )
    db_session.add(stale)
    db_session.commit()

    def boom(*_a, **_k):
        raise capital_flow_data.FundFlowProviderError("600000: ConnectionError: aborted")

    monkeypatch.setattr(
        capital_flow_data, "_fetch_individual_fund_flow", boom
    )

    row = capital_flow_data.get_or_sync_capital_flow(db_session, symbols[0], date(2026, 10, 9))

    assert row is stale
