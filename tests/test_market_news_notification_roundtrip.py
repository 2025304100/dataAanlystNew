"""Scheduled collection -> policy -> outbox -> real local HTTP/in-app delivery.

Supplier outcomes are controlled fault inputs; HTTP transport/dispatcher/storage
are real. Nothing is delivered to third-party recipients.
"""
import json
import os
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace

import pandas as pd
import pytest

from app.models.notification import (NotificationChannel, NotificationDelivery,
    NotificationOutbox, NotificationPolicy, NotificationPolicyChannel)
from app.models.market_event import MarketEvent, MarketEventCollectRun
from app.services import scheduled_tasks
from app.services.notifications.dispatcher import Dispatcher
from app.services.notifications.template_renderer import escape_value
from app.services.notifications.webhook import WebhookAdapter
from app.utils.secret_mask import encrypt_config

pytestmark = pytest.mark.whitebox


def save_receipt(name, value):
    folder = os.environ.get("MARKET_NEWS_NOTIFICATION_EVIDENCE_DIR")
    if folder:
        path = Path(folder)
        path.mkdir(parents=True, exist_ok=True)
        (path / f"{name}.json").write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.fixture
def receiver():
    received = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"accepted":true}')
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/acceptance", received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@pytest.mark.parametrize("outcome", ["normal", "partial", "all_failed", "all_duplicate"])
def test_scheduled_collection_notification_roundtrip(db_session, monkeypatch, receiver, outcome):
    url, received = receiver
    # Loopback integration only; do not send a local request via user proxies.
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    channels = [NotificationChannel(name="验收站内消息", channel_type="in_app", enabled=True, status="enabled"),
        NotificationChannel(name="验收本机Webhook", channel_type="webhook", enabled=True, status="enabled",
            config_encrypted_json=encrypt_config(json.dumps({"url":url, "timeout":3})))]
    policy = NotificationPolicy(name="行情消息闭环验收", enabled=True, source_types_json='["system"]',
                                min_severity="warn", scope_type="all", delivery_mode="instant")
    db_session.add_all([*channels, policy])
    db_session.commit()
    db_session.add_all(NotificationPolicyChannel(policy_id=policy.id,channel_id=channel.id) for channel in channels)
    db_session.commit()
    def supplier(fetcher, *, api_key, **kwargs):
        if outcome == "all_failed" or (outcome == "partial" and api_key == "caixin"):
            raise RuntimeError("受控供应商故障")
        return pd.DataFrame([{"title":f"行情闭环验收消息来自{api_key}"}])
    monkeypatch.setattr("app.services.akshare_utils.call_akshare_with_retry", supplier)
    task = SimpleNamespace(task_type="market_events_collect",payload_json="{}")
    first = scheduled_tasks._dispatch_task(task)[1]
    assert first["status"] == ("failed" if outcome == "all_failed" else "done")
    if outcome in ("all_failed", "all_duplicate"):
        scheduled_tasks._dispatch_task(task)  # repeated failure dedup / healthy all-duplicate run
    db_session.expire_all()
    outboxes = db_session.query(NotificationOutbox).all()
    runs = db_session.query(MarketEventCollectRun).order_by(MarketEventCollectRun.id).all()
    expected_count = {"normal":6, "partial":5, "all_failed":0, "all_duplicate":6}[outcome]
    assert db_session.query(MarketEvent).count() == expected_count
    assert runs[0].collected == expected_count
    assert len(json.loads(runs[0].errors_json)) == {"partial":1, "all_failed":6}.get(outcome, 0)
    if outcome == "all_duplicate":
        assert runs[-1].collected == 0 and runs[-1].skipped_duplicate == 6
    if outcome != "all_failed":
        assert outboxes == [] and received == []
        save_receipt(outcome, dict(outcome=outcome, stored_events=expected_count, outbox_count=0,
            received_http_requests=0, runs=[dict(status=row.status,collected=row.collected,
                skipped_duplicate=row.skipped_duplicate) for row in runs]))
        return
    assert len(outboxes) == 2 and {row.channel_id for row in outboxes} == {row.id for row in channels}
    assert all(row.status == "pending" and row.event_type == "market_news_collect_failed" for row in outboxes)
    dispatcher = Dispatcher()
    for row in outboxes:
        dispatcher._process_one(db_session, row)
    db_session.expire_all()
    assert all(row.status == "sent" for row in db_session.query(NotificationOutbox))
    deliveries = db_session.query(NotificationDelivery).all()
    assert len(deliveries) == 2 and all(row.status == "success" for row in deliveries)
    assert len(received) == 1 and "行情消息采集失败" in received[0]["title"]
    assert "6 个来源失败" in received[0]["body"]
    # A later repeated failure cannot resend an already delivered daily alert.
    scheduled_tasks._dispatch_task(task)
    assert db_session.query(NotificationOutbox).count() == 2 and len(received) == 1
    # A new date key must still alert normally; dedup is per channel and key.
    from app.services.notifications.event_emitter import emit_market_news_collect_failed
    tomorrow = emit_market_news_collect_failed(db_session, errors=["受控供应商故障"],
        event_key="market_news_collect_failed:2099-01-01")
    assert len(tomorrow) == 2
    save_receipt(outcome, dict(outcome=outcome, stored_events=0, delivered_outboxes=2,
        successful_deliveries=2, received_http_requests=len(received), received_body=received[0],
        pending_and_sent_daily_dedup=True, next_key_outboxes=len(tomorrow)))


@pytest.mark.parametrize("nested", [False, True])
def test_webhook_keeps_json_structure_with_quotes_newlines_and_injection_text(monkeypatch, receiver, nested):
    url, received = receiver
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    title = '中文"标题"'
    body = '首行\n次行\\路径 **强调** ","injected":true,"tail":"'
    config = {"url":url, "timeout":3}
    if nested:
        config["request_template"] = json.dumps({"items":[{"text":"{{body}}"}],"enabled":True,"count":2})
    result = WebhookAdapter().send_message(config, title=title, body=body)
    assert result.success and len(received) == 1 and isinstance(received[0], dict)
    if nested:
        assert received[0] == {"items":[{"text":escape_value(body)}],"enabled":True,"count":2}
    else:
        assert received[0] == {"title":escape_value(title), "body":escape_value(body)}
    save_receipt(f"json-structure-{nested}", dict(nested=nested, status_code=result.status_code,
        received_http_requests=len(received), received_body=received[0]))
