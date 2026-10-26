"""白盒测试 - 行情消息 P1-7：采集可观测（2026-10-25）。

契约：
- 每次采集落一行 market_event_collect_runs（manual/scheduled 来源、成功/失败状态）；
- 失败采集也记账后原样抛出；
- recent_collects_all_empty：最近 lookback 轮全部零新增才告警，证据不足不告警；
- 账目按 90 天窗口自清理（写入时顺带）。
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.models.market_event import MarketEventCollectRun
from app.schemas.market_event import MarketEventCollectRequest
from app.services import market_events as svc

pytestmark = pytest.mark.whitebox


def _fake_fetch(monkeypatch, rows):
    monkeypatch.setattr("app.services.market_events._fetch_global_news", lambda: list(rows))


def _row(title):
    return {"title": title, "summary": None, "source": "eastmoney-global",
            "source_url": None, "published_at": svc._now()}


def test_collect_records_run_row(db_session, monkeypatch):
    _fake_fetch(monkeypatch, [_row("央行发布三季度货币政策执行报告甲")])
    result = svc.collect_market_events(
        db_session, MarketEventCollectRequest(days=7, sources=["eastmoney-global"]),
        trigger_source="scheduled",
    )
    assert result.collected == 1
    runs = svc.list_collect_runs(db_session)
    assert len(runs) == 1
    run = runs[0]
    assert run.trigger_source == "scheduled"
    assert run.status == "done"
    assert run.collected == 1
    assert run.finished_at is not None


def test_collect_failure_still_records_run(db_session, monkeypatch):
    def _boom():
        raise RuntimeError("db broken")

    monkeypatch.setattr("app.services.market_events._fetch_global_news", _boom)
    # _fetch_from_ak 内部 catch 掉源异常 → 采到 0 条；真正的 DB 异常路径：
    # 直接让 inner 在去重阶段抛错
    monkeypatch.setattr("app.services.market_events._backfill_title_hashes",
                        lambda db: (_ for _ in ()).throw(RuntimeError("db down")))
    with pytest.raises(RuntimeError):
        svc.collect_market_events(
            db_session, MarketEventCollectRequest(days=7, sources=["eastmoney-global"])
        )
    runs = svc.list_collect_runs(db_session)
    assert len(runs) == 1
    assert runs[0].status == "failed"
    assert "RuntimeError" in runs[0].errors_json


def test_recent_collects_all_empty_judgement(db_session, monkeypatch):
    _fake_fetch(monkeypatch, [_row("首条消息用于打破空采乙")])
    svc.collect_market_events(db_session, MarketEventCollectRequest(days=7, sources=["eastmoney-global"]))
    # 只有 1 条历史记录：证据不足，不判定空采
    assert svc.recent_collects_all_empty(db_session, lookback=2) is False

    _fake_fetch(monkeypatch, [])  # 空采（源返回空）
    svc.collect_market_events(db_session, MarketEventCollectRequest(days=7, sources=["eastmoney-global"]))
    # 1 条有新增 + 1 条空采 → 不算连续空采
    assert svc.recent_collects_all_empty(db_session, lookback=2) is False

    svc.collect_market_events(db_session, MarketEventCollectRequest(days=7, sources=["eastmoney-global"]))
    svc.collect_market_events(db_session, MarketEventCollectRequest(days=7, sources=["eastmoney-global"]))
    # 最近两轮全部零新增 → 告警
    assert svc.recent_collects_all_empty(db_session, lookback=2) is True


def test_collect_runs_pruned_beyond_window(db_session, monkeypatch):
    _fake_fetch(monkeypatch, [_row("触发一次采集以写入账目丙")])
    svc.collect_market_events(db_session, MarketEventCollectRequest(days=7, sources=["eastmoney-global"]))
    # 手工把该账目时间拨回 100 天前
    run = svc.list_collect_runs(db_session)[0]
    run.created_at = svc._now() - timedelta(days=100)
    db_session.commit()

    _fake_fetch(monkeypatch, [_row("再采一次触发账目清理丁")])
    svc.collect_market_events(db_session, MarketEventCollectRequest(days=7, sources=["eastmoney-global"]))
    runs = svc.list_collect_runs(db_session)
    assert len(runs) == 1  # 100 天前的旧账目已被顺带清理
    assert db_session.query(MarketEventCollectRun).count() == 1
