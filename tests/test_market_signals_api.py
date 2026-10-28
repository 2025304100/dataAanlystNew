"""Market-sensitive subscriptions and outcome verification through HTTP seams.

Inputs are seeded into dedicated fixture databases; assertions use public APIs.
Supplier HTTP and time are external seams. No production DB or recipient is used.
"""
from datetime import date, datetime
from datetime import timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db.session import get_db
from app.models.symbol import Symbol
from app.models.ai_profile import AIProfile
from app.api.routes.market_signals import router
from app.api.routes.market_events import router as events_router
from app.api.routes.notifications import router as notifications_router

pytestmark = pytest.mark.whitebox


@pytest.fixture
def signals_client(db_session):
    db_session.add(Symbol(symbol="000001", name="验收银行", asset_type="stock", market="SZ"))
    db_session.commit()
    app = FastAPI()
    app.include_router(router)
    app.include_router(events_router)
    app.include_router(notifications_router)
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as client:
        yield client


def test_independent_rules_are_versioned_and_pause_without_losing_history(signals_client):
    stock = signals_client.post("/market-signals/rules", json={
        "name": "银行单股", "target_type": "symbol", "target_key": "000001",
        "min_score": 90, "window_days": 1, "min_move_pct": 3,
    })
    assert stock.status_code == 201, stock.text
    sector = signals_client.post("/market-signals/rules", json={
        "name": "半导体板块", "target_type": "sector", "target_key": "semiconductor",
        "quote_key": "000300", "min_score": 80, "window_days": 5, "min_move_pct": 2,
    })
    assert sector.status_code == 201, sector.text
    rows = signals_client.get("/market-signals/rules").json()
    assert [(r["min_score"], r["window_days"]) for r in rows] == [(90, 1), (80, 5)]
    rule_id = stock.json()["id"]
    updated = signals_client.put(f"/market-signals/rules/{rule_id}", json={"window_days": 3, "enabled": False})
    assert updated.status_code == 200, updated.text
    assert (updated.json()["revision"], updated.json()["enabled"]) == (2, False)
    history = signals_client.get(f"/market-signals/rules/{rule_id}/versions").json()
    assert [r["config"]["window_days"] for r in history] == [1, 3]
    assert sector.json()["window_days"] == 5


def test_collected_evidence_keeps_unknown_time_and_same_title_updates(signals_client, monkeypatch):
    import pandas as pd
    from app.services import market_events
    summary = ["验收银行获得新增订单100亿元，业务执行条件已明确。"]
    monkeypatch.setattr(market_events.ak, "stock_info_global_em", lambda: pd.DataFrame([
        {"title": "验收银行新增订单正式落地", "summary": summary[0], "url": "https://example.com/order"}
    ]))
    first = signals_client.post("/market-events/collect", json={"sources": ["eastmoney-global"]})
    assert first.status_code == 200
    summary[0] = "验收银行获得新增订单200亿元，业务执行条件已明确。"
    signals_client.post("/market-events/collect", json={"sources": ["eastmoney-global"]})
    signals_client.post("/market-events/collect", json={"sources": ["eastmoney-global"]})
    rows = signals_client.get("/market-signals/evidence").json()
    assert rows["total"] == 2
    assert {r["text"] for r in rows["items"]} == {
        "验收银行获得新增订单100亿元，业务执行条件已明确。",
        "验收银行获得新增订单200亿元，业务执行条件已明确。",
    }
    assert all(r["time_quality"] == "unknown" and r["published_at"] is None for r in rows["items"])


@pytest.fixture
def ai_vendor(db_session, monkeypatch):
    state = {"requests": [], "analysis": {
        "event_identity": "验收银行-订单-20261027", "new_facts": ["新增订单100亿元"],
        "event_type": "demand", "relation": "direct", "mechanism": "新增订单直接增加目标公司需求",
        "material_points": 30, "novelty_points": 30, "breadth_points": 5, "duration_points": 10,
        "evidence_quotes": ["验收银行获得新增订单100亿元"], "missing": [],
    }}
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            state["requests"].append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            body = json.dumps({"choices": [{"message": {"content": json.dumps(state["analysis"], ensure_ascii=False)}}],
                               "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    profile = AIProfile(name="异动验收模型", provider="openai_compatible", auth_type="none", model="fixture-model",
                        base_url=f"http://127.0.0.1:{server.server_port}/v1", purpose="explanation")
    db_session.add(profile)
    db_session.commit()
    state["profile_id"] = profile.id
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def make_rule_and_evidence(client, **rule_overrides):
    rule = client.post("/market-signals/rules", json={
        "name": "银行敏感消息", "target_type": "symbol", "target_key": "000001", "adaptive_threshold": False,
        **rule_overrides,
    }).json()
    from app.services import market_signal_sources
    evidence = client.post("/market-signals/evidence", json={
        "title": "验收银行新增订单正式实施", "text": "验收银行获得新增订单100亿元，项目即日起执行。",
        "source_url": "https://example.com/orders", "published_at": market_signal_sources.utcnow().isoformat(),
        "symbol_codes": ["000001"],
    }).json()
    return rule, evidence


def test_ai_analysis_is_grounded_cached_and_does_not_claim_probability(signals_client, ai_vendor):
    result = signals_client.put("/market-signals/settings", json={"ai_profile_id": ai_vendor["profile_id"]})
    assert result.status_code == 200
    rule, evidence = make_rule_and_evidence(signals_client)
    response = signals_client.post("/market-signals/analyze", json={"evidence_ids": [evidence["id"]], "rule_ids": [rule["id"]]})
    assert response.status_code == 200, response.text
    item = response.json()["items"][0]
    assert (item["status"], item["score"]) == ("scored", 85)
    assert item["calibrated_probability"] is None
    assert item["analysis"]["evidence_quotes"] == ["验收银行获得新增订单100亿元"]
    again = signals_client.post("/market-signals/analyze", json={"evidence_ids": [evidence["id"]], "rule_ids": [rule["id"]]})
    assert again.json()["items"][0]["id"] == item["id"]
    assert len(ai_vendor["requests"]) == 1


def seed_signal_prices(db_session):
    from app.models.market_data import MarketCalendar
    from app.models.daily_bar import DailyBar
    start = date(2026, 10, 20)
    for offset in range(25):
        day = start + timedelta(days=offset)
        db_session.add(MarketCalendar(trade_date=day, is_trading_day=int(day.weekday() < 5 and day != date(2026, 10, 28))))
    symbol = db_session.query(Symbol).filter_by(symbol="000001").one()
    for day, high in [(date(2026,10,27), 120), (date(2026,10,29), 101)]:
        db_session.add(DailyBar(symbol_id=symbol.id, trade_date=day, open=100, high=high, low=99,
                               close=100, source="fixture:qfq"))
    db_session.commit()


def test_shadow_outcome_uses_real_calendar_and_never_counts_pre_delivery_high(signals_client, ai_vendor, db_session, monkeypatch):
    from app.services import market_signal_sources
    now = [datetime(2026,10,27,9,10)]  # 17:10 China; 28th is a fixture holiday
    monkeypatch.setattr(market_signal_sources, "utcnow", lambda: now[0])
    seed_signal_prices(db_session)
    signals_client.put("/market-signals/settings", json={"ai_profile_id": ai_vendor["profile_id"], "enabled": True})
    rule, evidence = make_rule_and_evidence(signals_client, window_days=1, min_move_pct=3)
    signals_client.post("/market-signals/analyze", json={"evidence_ids": [evidence["id"]], "rule_ids": [rule["id"]]})
    response = signals_client.post("/market-signals/run", json={"analyze": False})
    assert response.status_code == 200, response.text
    prediction = signals_client.get("/market-signals/predictions").json()["items"][0]
    assert prediction["delivery_status"] == "shadow"
    assert prediction["snapshot"]["observation_dates"] == ["2026-10-29"]
    assert prediction["snapshot"]["reference_close"] == 100
    assert prediction["outcome_status"] == "pending"
    signals_client.put(f"/market-signals/rules/{rule['id']}", json={"min_move_pct": 0.1, "window_days": 5})
    now[0] = datetime(2026,10,29,9,10)
    signals_client.post("/market-signals/run", json={"analyze": False})
    settled = signals_client.get("/market-signals/predictions").json()["items"][0]
    assert settled["outcome_status"] == "miss"
    assert settled["snapshot"]["threshold_pct"] == 3
    assert settled["outcome"]["max_move_pct"] == pytest.approx(1)
    dashboard = signals_client.get("/market-signals/dashboard").json()
    assert dashboard["live"]["matured"] == 0
    assert dashboard["shadow"]["misses"] == 1


@pytest.mark.parametrize("pause_before_send", [False, True])
def test_trial_delivery_is_deduplicated_and_rechecks_pause_before_send(signals_client, ai_vendor, db_session, monkeypatch, pause_before_send):
    from app.services import market_signal_sources
    from app.services.notifications import dispatcher, outbox
    now = datetime(2026,10,27,9,10)
    monkeypatch.setattr(market_signal_sources, "utcnow", lambda: now)
    monkeypatch.setattr(outbox, "_now_utc", lambda: now)
    monkeypatch.setattr(dispatcher, "_now_utc_naive", lambda: now)
    seed_signal_prices(db_session)
    signals_client.put("/market-signals/settings", json={"ai_profile_id": ai_vendor["profile_id"], "enabled": True})
    rule, evidence = make_rule_and_evidence(signals_client, mode="trial", window_days=1)
    signals_client.post("/market-signals/analyze", json={"evidence_ids": [evidence["id"]], "rule_ids": [rule["id"]]})
    response = signals_client.post("/market-signals/run", json={"analyze": False})
    assert response.status_code == 200, response.text
    assert signals_client.get("/market-signals/predictions").json()["items"][0]["delivery_status"] == "queued"
    if pause_before_send:
        signals_client.put(f"/market-signals/rules/{rule['id']}", json={"enabled": False})
    assert dispatcher.Dispatcher().run_once() == 1
    row = signals_client.get("/market-signals/predictions").json()["items"][0]
    assert row["delivery_status"] == ("suppressed" if pause_before_send else "sent")
    signals_client.post("/market-signals/run", json={"analyze": False})
    assert signals_client.get("/market-signals/predictions").json()["total"] == 1
    inbox = signals_client.get("/notifications/inbox").json()
    assert len([item for item in inbox if item["status"] == "sent"]) == (0 if pause_before_send else 1)
    assert dispatcher.Dispatcher().run_once() == 0


def test_live_outcomes_are_auditable_and_new_rule_version_does_not_inherit_old_accuracy(signals_client, ai_vendor, db_session, monkeypatch):
    from app.services import market_signal_sources
    from app.services.notifications import dispatcher, outbox
    from app.models.daily_bar import DailyBar
    now = [datetime(2026,10,27,9,10)]
    monkeypatch.setattr(market_signal_sources, "utcnow", lambda: now[0])
    monkeypatch.setattr(outbox, "_now_utc", lambda: now[0])
    monkeypatch.setattr(dispatcher, "_now_utc_naive", lambda: now[0])
    seed_signal_prices(db_session)
    signals_client.put("/market-signals/settings", json={"ai_profile_id": ai_vendor["profile_id"], "enabled": True})
    rule, evidence = make_rule_and_evidence(signals_client, mode="trial", window_days=1)
    signals_client.post("/market-signals/analyze", json={"evidence_ids": [evidence["id"]], "rule_ids": [rule["id"]]})
    signals_client.post("/market-signals/run", json={"analyze": False})
    dispatcher.Dispatcher().run_once()
    now[0] = datetime(2026,10,29,9,10)
    signals_client.post("/market-signals/run", json={"analyze": False})
    prediction = signals_client.get("/market-signals/predictions").json()["items"][0]
    assert prediction["outcome_status"] == "miss"
    # Corrected supplier data is an input repair, not a changed price threshold.
    db_session.expire_all()
    bar = db_session.query(DailyBar).filter_by(trade_date=date(2026,10,29)).one()
    bar.high = 104
    db_session.commit()
    checked = signals_client.post(f"/market-signals/predictions/{prediction['id']}/recheck")
    assert checked.status_code == 200, checked.text
    assert checked.json()["outcome_status"] == "hit"
    history = signals_client.get(f"/market-signals/predictions/{prediction['id']}/outcomes").json()
    assert [item["status"] for item in history] == ["miss", "hit"]
    signals_client.put(f"/market-signals/rules/{rule['id']}", json={"min_score": 95})
    dashboard = signals_client.get("/market-signals/dashboard").json()
    assert dashboard["live"]["hits"] == 1
    current_rule = next(row for row in dashboard["rules"] if row["rule_id"] == rule["id"])
    assert current_rule["live"]["matured"] == 0
    assert current_rule["live"]["validation_status"] == "not_verified"


@pytest.mark.parametrize("independent_events", [False, True])
def test_dashboard_requires_event_diversity_and_keeps_unverifiable_in_denominator(signals_client, db_session, independent_events):
    from app.models.market_signal import MarketSignalAssessment, MarketSignalEvidence, MarketSignalPrediction
    from app.services.market_signal_sources import digest
    rule = signals_client.post("/market-signals/rules", json={
        "name": "统计验收", "target_type": "symbol", "target_key": "000001", "adaptive_threshold": False,
    }).json()
    evidence = MarketSignalEvidence(fingerprint=digest("stats-input"), title="独立统计输入", text="合成统计样本",
        source="fixture", time_quality="manual", kind="news", metadata_json="{}")
    db_session.add(evidence)
    db_session.flush()
    assessment = MarketSignalAssessment(evidence_id=evidence.id, rule_id=rule["id"], rule_revision=1,
        request_key=digest("stats-assessment"), status="scored", score=90, analysis_json="{}", input_json="{}")
    db_session.add(assessment)
    db_session.flush()
    for index in range(200):
        at = datetime(2025,1,1,9) + timedelta(days=index)
        db_session.add(MarketSignalPrediction(dedup_key=digest(f"prediction-{index}"),
            event_key=digest(str(index) if independent_events else "one-macro-event"), fact_key=digest(str(index)),
            assessment_id=assessment.id, rule_id=rule["id"], rule_revision=1, target_type="symbol", target_key="000001",
            delivery_status="sent", snapshot_json=json.dumps({"historical_baseline_rate": 0.2}),
            outcome_status="hit", outcome_json="{}", due_at=at, sent_at=at, created_at=at))
    db_session.commit()
    stats = signals_client.get("/market-signals/dashboard").json()["live"]
    assert stats["precision"] == 1
    assert stats["event_clusters"] == (200 if independent_events else 1)
    assert stats["validation_status"] == ("historical_target_met" if independent_events else "not_verified")
    # A missing outcome is part of the strict metric, not silently dropped.
    row = db_session.query(MarketSignalPrediction).order_by(MarketSignalPrediction.id).first()
    row.outcome_status = "unverifiable"
    db_session.commit()
    stats = signals_client.get("/market-signals/dashboard").json()["live"]
    assert stats["precision"] == pytest.approx(0.995)
    assert stats["validation_status"] != "historical_target_met"


def test_digest_waits_for_its_slot_and_delivers_once(signals_client, ai_vendor, db_session, monkeypatch):
    from app.services import market_signal_sources
    from app.services.notifications import dispatcher, outbox
    now = [datetime(2026,10,27,8,0)]  # 16:00 China, digest at 16:10
    monkeypatch.setattr(market_signal_sources, "utcnow", lambda: now[0])
    monkeypatch.setattr(outbox, "_now_utc", lambda: now[0])
    monkeypatch.setattr(dispatcher, "_now_utc_naive", lambda: now[0])
    seed_signal_prices(db_session)
    signals_client.put("/market-signals/settings", json={"ai_profile_id": ai_vendor["profile_id"], "enabled": True})
    rule, evidence = make_rule_and_evidence(signals_client, mode="trial", delivery_mode="digest", digest_time="16:10")
    signals_client.post("/market-signals/analyze", json={"evidence_ids": [evidence["id"]], "rule_ids": [rule["id"]]})
    signals_client.post("/market-signals/run", json={"analyze": False})
    assert dispatcher.Dispatcher().run_once() == 0

    assert signals_client.get("/market-signals/predictions").json()["items"][0]["delivery_status"] == "waiting_digest"
    now[0] += timedelta(minutes=15)
    signals_client.post("/market-signals/run", json={"analyze": False})
    assert dispatcher.Dispatcher().run_once() == 1
    assert signals_client.get("/market-signals/predictions").json()["items"][0]["delivery_status"] == "sent"
    signals_client.post("/market-signals/run", json={"analyze": False})
    assert dispatcher.Dispatcher().run_once() == 0


@pytest.mark.reap_workers
def test_target_selection_and_monitor_job_work_without_blocking_the_page(signals_client, db_session):
    import time
    from app.models.universe import UniverseSymbol
    db_session.add(UniverseSymbol(symbol="600000", name="全市场验收股票", asset_type="stock", market="sh", region="cn"))
    db_session.commit()
    metadata = signals_client.get("/market-signals/metadata", params={"keyword": "验收"})
    assert metadata.status_code == 200, metadata.text
    assert {row["key"] for row in metadata.json()["symbols"]} == {"000001", "600000"}
    assert any(row["key"] == "semiconductor" for row in metadata.json()["sectors"])
    rule = signals_client.post("/market-signals/rules", json={
        "name": "全市场标的订阅", "target_type": "symbol", "target_key": "600000", "adaptive_threshold": False,
    })
    assert rule.status_code == 201, rule.text
    job = signals_client.post("/market-signals/jobs", json={"kind": "monitor", "analyze": False})
    assert job.status_code == 202, job.text
    job_id = job.json()["id"]
    deadline = time.monotonic()+12
    while time.monotonic() < deadline:
        status = signals_client.get(f"/market-signals/jobs/{job_id}").json()
        if status["status"] in ("done", "partial", "failed"):
            break
        time.sleep(0.05)
    assert status["status"] == "done", status


def test_production_api_registers_feature_and_enabling_installs_one_monitor(db_session, ai_vendor):
    from app.api.router import api_router
    app = FastAPI()
    app.include_router(api_router)
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as client:
        settings = client.get("/api/v1/market-signals/settings")
        assert settings.status_code == 200, settings.text
        for _ in range(2):
            saved = client.put("/api/v1/market-signals/settings", json={"enabled": True, "ai_profile_id": ai_vendor["profile_id"]})
            assert saved.status_code == 200, saved.text
        schedules = client.get("/api/v1/market-signals/metadata").json()["schedules"]
        assert len(schedules) == 1 and schedules[0]["enabled"] is True


def test_original_article_enrichment_preserves_time_and_blocks_private_urls(signals_client, monkeypatch):
    import requests
    seen = []
    def supplier(url, **kwargs):
        seen.append(url)
        response = requests.Response()
        response.status_code = 200
        response.headers["Content-Type"] = "text/html; charset=utf-8"
        response.encoding = "utf-8"
        response._content = '<html><script>无关脚本</script><article>验收银行获得新增订单100亿元，执行期限三年。</article></html>'.encode()
        response._content_consumed = True
        return response
    monkeypatch.setattr(requests, "get", supplier)
    item = signals_client.post("/market-signals/evidence", json={
        "title": "验收银行订单新政策", "text": "原始摘要信息较少。", "source_url": "https://www.gov.cn/policy-test",
        "published_at": "2026-10-27T08:00:00Z",
    }).json()
    enriched = signals_client.post(f"/market-signals/evidence/{item['id']}/enrich")
    assert enriched.status_code == 200, enriched.text
    assert enriched.json()["text"] == "验收银行获得新增订单100亿元，执行期限三年。"
    assert enriched.json()["published_at"] == "2026-10-27T08:00:00"
    private = signals_client.post("/market-signals/evidence", json={
        "title": "验收私网链接拒绝", "text": "不能读取本机或者私网地址。", "source_url": "http://127.0.0.1/admin",
    }).json()
    rejected = signals_client.post(f"/market-signals/evidence/{private['id']}/enrich")
    assert rejected.status_code == 422
    assert seen == ["https://www.gov.cn/policy-test"]


@pytest.mark.reap_workers
def test_required_data_job_reuses_ready_local_quotes_without_network(signals_client, db_session, monkeypatch):
    import time
    import requests
    from app.services import market_signal_sources
    monkeypatch.setattr(market_signal_sources, "utcnow", lambda: datetime(2026,10,27,9,10))
    monkeypatch.setattr(requests, "get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("unexpected network")))
    seed_signal_prices(db_session)
    rule = signals_client.post("/market-signals/rules", json={
        "name": "准备数据验收", "target_type": "symbol", "target_key": "000001", "adaptive_threshold": False,
    }).json()
    response = signals_client.post("/market-signals/jobs", json={"kind": "sync_data", "rule_ids": [rule["id"]]})
    assert response.status_code == 202
    deadline = time.monotonic()+12
    while time.monotonic() < deadline:
        job = signals_client.get(f"/market-signals/jobs/{response.json()['id']}").json()
        if job["status"] in ("done", "partial", "failed"):
            break
        time.sleep(0.05)
    assert job["status"] == "done", job
    assert job["result"]["items"][0]["status"] == "ready"


def test_same_stock_multiple_windows_produce_one_notification_and_independent_outcomes(signals_client, ai_vendor, db_session, monkeypatch):
    from app.services import market_signal_sources
    from app.services.notifications import dispatcher, outbox
    now = datetime(2026,10,27,9,10)
    monkeypatch.setattr(market_signal_sources, "utcnow", lambda: now)
    monkeypatch.setattr(outbox, "_now_utc", lambda: now)
    monkeypatch.setattr(dispatcher, "_now_utc_naive", lambda: now)
    seed_signal_prices(db_session)
    signals_client.put("/market-signals/settings", json={"ai_profile_id": ai_vendor["profile_id"], "enabled": True})
    first, evidence = make_rule_and_evidence(signals_client, mode="trial", window_days=1)
    second = signals_client.post("/market-signals/rules", json={
        "name": "银行三日", "target_type": "symbol", "target_key": "000001",
        "adaptive_threshold": False, "window_days": 3, "mode": "trial",
    }).json()
    signals_client.post("/market-signals/analyze", json={"evidence_ids": [evidence["id"]]})
    run = signals_client.post("/market-signals/run", json={"analyze": False})
    assert run.json()["created"] == 2
    assert dispatcher.Dispatcher().run_once() == 1
    predictions = signals_client.get("/market-signals/predictions").json()["items"]
    assert {row["snapshot"]["rule"]["window_days"] for row in predictions} == {1, 3}
    assert all(row["delivery_status"] == "sent" for row in predictions)
    assert len(signals_client.get("/notifications/inbox").json()) == 1
    assert signals_client.post("/market-signals/run", json={"analyze": False}).json()["created"] == 0


@pytest.mark.reap_workers
def test_monitor_job_collects_followed_news_scores_and_delivers_without_manual_copy(signals_client, ai_vendor, db_session, monkeypatch):
    import time
    import pandas as pd
    from app.services import market_signal_sources, market_events
    from app.services.notifications import dispatcher, outbox
    now = datetime(2026,10,27,9,10)
    monkeypatch.setattr(market_signal_sources, "utcnow", lambda: now)
    monkeypatch.setattr(outbox, "_now_utc", lambda: now)
    monkeypatch.setattr(dispatcher, "_now_utc_naive", lambda: now)
    seed_signal_prices(db_session)
    frame = pd.DataFrame([{"title": "验收银行新增订单正式落地", "内容": "验收银行获得新增订单100亿元，项目即日起执行。" + "正式条款明确执行，期限三年。"*12,
        "发布时间": "2026-10-27 17:00:00", "新闻链接": "https://www.eastmoney.com/order"}])
    monkeypatch.setattr(market_events.ak, "stock_info_global_em", lambda: frame)
    monkeypatch.setattr(market_events.ak, "stock_news_main_cx", lambda: pd.DataFrame())
    requested = []
    def stock_news(symbol):
        requested.append(symbol)
        return frame
    monkeypatch.setattr(market_events.ak, "stock_news_em", stock_news)
    monkeypatch.setattr(market_events.ak, "stock_zh_a_disclosure_report_cninfo", lambda **kwargs: pd.DataFrame())
    signals_client.post("/market-signals/rules", json={"name": "自动采集银行", "target_type": "symbol", "target_key": "000001",
        "adaptive_threshold": False, "window_days": 1, "mode": "trial"})
    signals_client.put("/market-signals/settings", json={"enabled": True, "ai_profile_id": ai_vendor["profile_id"]})
    response = signals_client.post("/market-signals/jobs", json={"kind": "monitor"})
    deadline = time.monotonic()+18
    while time.monotonic() < deadline:
        job = signals_client.get(f"/market-signals/jobs/{response.json()['id']}").json()
        if job["status"] in ("done", "partial", "failed"):
            break
        time.sleep(.05)
    assert job["status"] == "done", job
    assert requested == ["000001"]
    assert signals_client.get("/market-signals/evidence").json()["total"] >= 1
    assert signals_client.get("/market-signals/predictions").json()["items"][0]["delivery_status"] == "sent"
    assert len(signals_client.get("/notifications/inbox").json()) == 1


@pytest.mark.parametrize("bad_case", ["invented_quote", "calendar", "missing", "stale", "future", "unknown_time", "no_new_fact"])
def test_invalid_or_ineligible_evidence_never_creates_alert(signals_client, ai_vendor, db_session, monkeypatch, bad_case):
    from app.services import market_signal_sources
    now = datetime(2026,10,27,9,10)
    monkeypatch.setattr(market_signal_sources, "utcnow", lambda: now)
    seed_signal_prices(db_session)
    signals_client.put("/market-signals/settings", json={"ai_profile_id": ai_vendor["profile_id"], "enabled": True})
    rule, evidence = make_rule_and_evidence(signals_client, mode="trial", min_score=0)
    at = now-timedelta(hours=25) if bad_case == "stale" else now+timedelta(hours=1) if bad_case == "future" else None
    if bad_case in ("calendar", "stale", "future", "unknown_time"):
        evidence = signals_client.post("/market-signals/evidence", json={
            "title": "验收银行新增订单特定证据", "text": "验收银行获得新增订单100亿元，项目即日起执行。",
            "source_url": "https://example.com/test", "published_at": at.isoformat() if at else None,
            "kind": "calendar" if bad_case == "calendar" else "news",
        }).json()
    if bad_case == "invented_quote":
        ai_vendor["analysis"]["evidence_quotes"] = ["凭空编造的重大利好"]
    if bad_case == "missing":
        ai_vendor["analysis"]["missing"] = ["尚无可核验执行条件"]
    if bad_case == "no_new_fact":
        ai_vendor["analysis"]["novelty_points"] = 10
    response = signals_client.post("/market-signals/analyze", json={"evidence_ids": [evidence["id"]]})
    assert response.status_code == 200, response.text
    if bad_case == "calendar":
        assert len(ai_vendor["requests"]) == 0
        assert response.json()["items"][0]["status"] == "calendar"
    signals_client.post("/market-signals/run", json={"analyze": False})
    assert signals_client.get("/market-signals/predictions").json()["total"] == 0
    assert signals_client.get("/notifications/inbox").json() == []


def test_global_daily_budget_uses_beijing_event_groups_not_utc_date(signals_client, ai_vendor, db_session, monkeypatch):
    from app.services import market_signal_sources
    now = datetime(2026,10,27,16,30)  # Oct 28 00:30 China; holiday, reference is Oct 27.
    monkeypatch.setattr(market_signal_sources, "utcnow", lambda: now)
    seed_signal_prices(db_session)
    signals_client.put("/market-signals/settings", json={"ai_profile_id": ai_vendor["profile_id"], "enabled": True, "global_daily_limit": 2})
    rule, _ = make_rule_and_evidence(signals_client, mode="shadow", window_days=1, cooldown_hours=0, max_daily=10)
    for number in range(3):
        ai_vendor["analysis"]["event_identity"] = f"验收银行-独立事件{number}-20261028"
        evidence = signals_client.post("/market-signals/evidence", json={"title": f"验收银行独立订单第{number}项",
            "text": "验收银行获得新增订单100亿元，项目即日起执行。", "source_url": f"https://example.com/event/{number}",
            "published_at": now.isoformat()}).json()
        signals_client.post("/market-signals/analyze", json={"evidence_ids": [evidence["id"]], "rule_ids": [rule["id"]]})
    signals_client.post("/market-signals/run", json={"analyze": False})
    predictions = signals_client.get("/market-signals/predictions").json()
    assert predictions["total"] == 2
    assert all(row["snapshot"]["budget_day"] == "2026-10-28" for row in predictions["items"])


def test_supplier_date_precision_and_full_content_are_preserved(signals_client, monkeypatch):
    import pandas as pd
    from app.services import market_events
    body = "验收正文数据不能被五百字摘要截断。"*100
    monkeypatch.setattr(market_events.ak, "stock_info_global_em", lambda: pd.DataFrame([
        {"title": "验收只有日期消息", "内容": body, "日期": "2026-10-27", "url": "https://example.com/date"},
        {"title": "验收精确时间消息", "内容": body, "日期": "2026-10-27 08:00:00", "url": "https://example.com/time"},
    ]))
    response = signals_client.post("/market-events/collect", json={"sources": ["eastmoney-global"], "days": 30})
    assert response.status_code == 200, response.text
    rows = {row["title"]: row for row in signals_client.get("/market-signals/evidence").json()["items"]}
    assert rows["验收只有日期消息"]["time_quality"] == "date_only"
    assert rows["验收精确时间消息"]["time_quality"] == "supplier"
    assert rows["验收精确时间消息"]["published_at"] == "2026-10-27T00:00:00"
    assert rows["验收只有日期消息"]["text"] == body


def test_automatic_analysis_spends_calls_on_fresh_relevant_candidates(signals_client, ai_vendor, monkeypatch):
    from app.services import market_signal_sources
    now = datetime(2026,10,27,9,10)
    monkeypatch.setattr(market_signal_sources, "utcnow", lambda: now)
    signals_client.put("/market-signals/settings", json={"ai_profile_id": ai_vendor["profile_id"]})
    _, relevant = make_rule_and_evidence(signals_client)
    for title, at in [("今日体育赛事常规结果", now), ("验收银行过期新增订单", now-timedelta(hours=48))]:
        signals_client.post("/market-signals/evidence", json={"title": title, "text": "这是一段没有新增适用政策的普通体育内容。",
            "source_url": "https://example.com/irrelevant", "published_at": at.isoformat()})
    response = signals_client.post("/market-signals/analyze", json={})
    assert response.status_code == 200, response.text
    assert response.json()["calls"] == 1
    assert [row["evidence_id"] for row in response.json()["items"]] == [relevant["id"]]
    assert len(ai_vendor["requests"]) == 1


def test_too_small_ai_context_is_explained_without_an_over_budget_request(signals_client, ai_vendor, db_session):
    profile = db_session.get(AIProfile, ai_vendor["profile_id"])
    profile.max_context_tokens = 2048
    profile.max_tokens = 1800
    db_session.commit()
    signals_client.put("/market-signals/settings", json={"ai_profile_id": profile.id})
    _, evidence = make_rule_and_evidence(signals_client)
    response = signals_client.post("/market-signals/analyze", json={"evidence_ids": [evidence["id"]]})
    assert response.status_code == 200, response.text
    item = response.json()["items"][0]
    assert item["status"] == "unavailable" and item["score"] is None
    assert "上下文" in item["analysis"]["reason"]
    assert not ai_vendor["requests"]


def test_local_webhook_success_recovers_callback_after_primary_channel_is_disabled(signals_client, ai_vendor, db_session, monkeypatch):
    from threading import Event
    from sqlalchemy.orm import Session
    from app.models.notification import NotificationChannel
    from app.services import market_signal_sources
    from app.services.notifications import dispatcher, outbox
    from app.services.market_event_collection_lock import market_event_write_lock
    from app.utils.secret_mask import encrypt_config
    now = datetime(2026,10,27,9,10)
    monkeypatch.setattr(market_signal_sources, "utcnow", lambda: now)
    monkeypatch.setattr(outbox, "_now_utc", lambda: now)
    monkeypatch.setattr(dispatcher, "_now_utc_naive", lambda: now)
    released, receipts = Event(), []
    engine = db_session.get_bind()
    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            receipts.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            with Session(engine) as other, market_event_write_lock(other, namespace="market-signals-engine"):
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")
                self.wfile.flush()
                released.wait(5)
        def log_message(self, *args):
            pass
    receiver = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    thread = Thread(target=receiver.serve_forever, daemon=True)
    thread.start()
    try:
        primary = NotificationChannel(name="主站内渠道", channel_type="in_app", enabled=True, status="enabled")
        secondary = NotificationChannel(name="本机接收渠道", channel_type="webhook", enabled=True, status="enabled",
            config_encrypted_json=encrypt_config(json.dumps({"url": f"http://127.0.0.1:{receiver.server_port}/events", "timeout": 3})))
        db_session.add_all([primary, secondary])
        db_session.commit()
        seed_signal_prices(db_session)
        signals_client.put("/market-signals/settings", json={"ai_profile_id": ai_vendor["profile_id"], "enabled": True})
        rule, evidence = make_rule_and_evidence(signals_client, mode="trial", channel_ids=[primary.id, secondary.id])
        signals_client.post("/market-signals/analyze", json={"evidence_ids": [evidence["id"]]})
        signals_client.post("/market-signals/run", json={"analyze": False})
        primary.enabled, primary.status = False, "disabled"
        db_session.commit()
        assert dispatcher.Dispatcher().run_once() == 2
        released.set()
        assert len(receipts) == 1
        signals_client.post("/market-signals/run", json={"analyze": False})
        predictions = signals_client.get("/market-signals/predictions").json()
        assert predictions["total"] == 1
        assert predictions["items"][0]["delivery_status"] == "sent"
        assert predictions["items"][0]["sent_at"] == now.isoformat()
        assert dispatcher.Dispatcher().run_once() == 0
        assert len(receipts) == 1
    finally:
        released.set()
        receiver.shutdown()
        receiver.server_close()
        thread.join(timeout=3)


def test_adaptive_history_uses_ready_universe_data_and_excludes_future_prices(signals_client, db_session, monkeypatch):
    from app.models.universe import UniverseSymbol, UniverseDailyBar
    from app.models.market_data import MarketCalendar
    from app.models.daily_bar import DailyBar
    from app.services import market_signal_sources
    monkeypatch.setattr(market_signal_sources, "utcnow", lambda: datetime(2026,10,27,9,10))
    symbol = db_session.query(Symbol).filter_by(symbol="000001").one()
    universe = UniverseSymbol(symbol="000001", name="历史验收银行", asset_type="stock", market="sz", region="cn")
    db_session.add(universe)
    db_session.flush()
    for offset in range(175):
        day = date(2026,6,1)+timedelta(days=offset)
        db_session.add(MarketCalendar(trade_date=day, is_trading_day=int(day.weekday()<5)))
        if day.weekday()<5:
            db_session.add(UniverseDailyBar(universe_symbol_id=universe.id, trade_date=day, open=100,
                high=10000 if day>date(2026,10,27) else 101, low=99, close=100, source="fixture:qfq"))
    db_session.add(DailyBar(symbol_id=symbol.id, trade_date=date(2026,10,27), open=100, high=101, low=99, close=100, source="fixture:qfq"))
    db_session.commit()
    rule = signals_client.post("/market-signals/rules", json={"name":"历史完整度验收", "target_type":"symbol", "target_key":"000001"}).json()
    metadata = signals_client.get("/market-signals/metadata").json()
    ready = next(item for item in metadata["readiness"] if item["rule_id"]==rule["id"])
    assert ready["ready"], ready
    assert ready["context"]["quote_store"]=="universe"
    assert ready["context"]["history_windows"]>=60
    assert ready["context"]["threshold_pct"]==2
