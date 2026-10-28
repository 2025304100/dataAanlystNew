"""One logical event alert, durable dedup, and send-time subscription checks."""
import ipaddress
import json
from datetime import timedelta
from urllib.parse import urlsplit

from fastapi import HTTPException
from sqlalchemy import select

from app.models.market_signal import MarketSignalAssessment, MarketSignalEvidence, MarketSignalPrediction, MarketSignalRule
from app.models.notification import NotificationChannel, NotificationDelivery, NotificationOutbox
from app.services import market_signal_sources as sources
from app.services.market_event_collection_lock import market_event_write_lock
from app.services.market_signal_quotes import CHINA, QuoteUnavailable, quote_context
from app.services.market_signals import _json, get_settings
from app.services.notifications.channel_state import is_selectable_for_policy
from app.utils.secret_mask import decrypt_config


def channel_allowed(channel):
    if channel is None or not is_selectable_for_policy(channel):
        return False
    if channel.channel_type == "in_app":
        return True
    if channel.channel_type != "webhook":
        return False
    try:
        config = json.loads(decrypt_config(channel.config_encrypted_json))
        parsed = urlsplit(config.get("url", ""))
        host = parsed.hostname
        loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
        return parsed.scheme in ("http", "https") and loopback and not parsed.username
    except Exception:
        return False


def validate_channels(db, ids):
    for channel_id in ids:
        channel = db.get(NotificationChannel, channel_id)
        if channel is None or not channel_allowed(channel):
            raise HTTPException(422, "本轮仅支持已启用的站内渠道或本机 Webhook；第三方送达另行安排")


def selectable_channels(db):
    return [{"id": row.id, "name": row.name, "channel_type": row.channel_type}
        for row in db.scalars(select(NotificationChannel).order_by(NotificationChannel.id)) if channel_allowed(row)]


def _channels(db, config):
    if config["channel_ids"]:
        selected = [db.get(NotificationChannel, value) for value in config["channel_ids"]]
        selected = [channel for channel in selected if channel_allowed(channel)]
        if not selected:
            raise HTTPException(422, "已选通知渠道均已停用或不再是本机渠道")
        return selected
    existing = list(db.scalars(select(NotificationChannel).where(NotificationChannel.channel_type == "in_app").order_by(NotificationChannel.id)))
    if existing:
        selected = next((row for row in existing if channel_allowed(row)), None)
        if selected is None:
            raise HTTPException(422, "站内通知渠道已关闭，请先在通知设置启用")
        return [selected]
    channel = NotificationChannel(name="行情异动站内提醒", channel_type="in_app", enabled=True, status="enabled")
    db.add(channel)
    db.flush()
    return [channel]


def _payload(rows):
    first = json.loads(rows[0].snapshot_json)
    evidence = first["evidence"]
    targets = []
    for row in rows:
        snapshot = json.loads(row.snapshot_json)
        targets.extend([f"{snapshot['rule'].get('target_name', row.target_key)}：{snapshot['evidence']['title']}",
            f"评分{snapshot['score']}，观察{snapshot['rule']['window_days']}个交易日，显著波动门槛{snapshot['threshold_pct']:.2f}%（方向不限）",
            f"新增事实：{'；'.join(snapshot['analysis']['new_facts'])}",
            f"依据：{'；'.join(snapshot['analysis']['evidence_quotes'])}",
            f"来源：{snapshot['evidence'].get('source_url') or snapshot['evidence']['source']}"])
    body = "\n".join([*targets,
        "试用提醒：75%是验证目标，当前命中率以结果看板为准。"])
    # Existing notification payloads use MySQL TEXT. Full immutable evidence is
    # retained in signal tables; bound duplicated body/body_text below 64KiB.
    if len(body.encode("utf-8")) > 20000:
        body = body.encode("utf-8")[:19500].decode("utf-8", errors="ignore")+"\n完整目标、依据与结果请进入行情异动提醒查看。"
    event_count = len({(row.event_key, row.fact_key) for row in rows})
    title = f"行情异动汇总：{event_count}个事件" if event_count > 1 else f"行情异动关注：{evidence['title']}"
    return {"title": title, "body": body, "body_text": body,
        "prediction_ids": [row.id for row in rows], "jump_url": "/?tab=news&marketSignals=1",
        "direction_independent": True}


def queue_predictions(db, rows, now):
    grouped = {}
    for row in rows:
        config = json.loads(row.snapshot_json)["rule"]
        try:
            channels = _channels(db, config)
        except HTTPException as exc:
            row.delivery_status, row.outcome_status = "suppressed", "not_applicable"
            row.outcome_json = _json({"reason": exc.detail})
            continue
        for channel in channels:
            digest_key = f"{json.loads(row.snapshot_json)['budget_day']}:{config['digest_time']}"
            group = ("digest", digest_key, channel.id) if config["delivery_mode"] == "digest" else (row.event_key, row.fact_key, channel.id)
            grouped.setdefault(group, []).append(row)
    for (event_key, fact_key, channel_id), predictions in grouped.items():
        key = "market-signal:" + sources.digest(f"{event_key}:{fact_key}:{channel_id}")
        outbox = db.scalar(select(NotificationOutbox).where(NotificationOutbox.event_key == key,
            NotificationOutbox.channel_id == channel_id))
        if outbox is not None and outbox.status != "pending":
            for row in predictions:
                if row.outbox_id is None:
                    row.delivery_status, row.outcome_status = "suppressed", "not_applicable"
                    row.outcome_json = _json({"reason": "同一事件已送达或正在投递，不追加重复提醒"})
            continue
        if outbox is None:
            outbox = NotificationOutbox(event_key=key, source_type="market_signal", source_id=predictions[0].id,
                event_type="market_signal_alert", severity="info", channel_id=channel_id,
                status="pending", attempt_count=0, max_attempts=5, next_retry_at=now, created_at=now)
            db.add(outbox)
            db.flush()
        else:
            old_ids = json.loads(outbox.payload_json).get("prediction_ids", [])
            predictions = list({row.id: row for row in [*db.scalars(select(MarketSignalPrediction).where(
                MarketSignalPrediction.id.in_(old_ids))), *predictions]}.values())
        outbox.payload_json = _json(_payload(predictions))
        for row in predictions:
            row.delivery_status = "queued"
            row.outbox_id = row.outbox_id or outbox.id
            snapshot = json.loads(row.snapshot_json)
            snapshot["outbox_ids"] = list(dict.fromkeys([*snapshot.get("outbox_ids", []), outbox.id]))
            row.snapshot_json = _json(snapshot)


def _prediction_rows(db, outbox):
    ids = json.loads(outbox.payload_json or "{}").get("prediction_ids", [])
    return list(db.scalars(select(MarketSignalPrediction).where(MarketSignalPrediction.id.in_(ids)).execution_options(populate_existing=True)))


def prepare_notification(db, outbox):
    """Called after atomic claim and immediately before transport, under a DB lock."""
    from app.services.market_signal_engine import _eligibility
    outbox_id = outbox.id
    with market_event_write_lock(db, namespace="market-signals-engine"):
        # Atomic claim is committed by the dispatcher. Start a fresh snapshot
        # after obtaining the lock so another worker's reservation is visible.
        db.rollback()
        outbox = db.get(NotificationOutbox, outbox_id, populate_existing=True)
        if outbox is None:
            return False
        if not channel_allowed(db.get(NotificationChannel, outbox.channel_id)):
            # This transport is unavailable; another authorized local channel
            # can still deliver the same prediction. Reconciliation checks all.
            return False
        settings, now = get_settings(db), sources.utcnow()
        rows, valid = _prediction_rows(db, outbox), []
        all_predictions = list(db.scalars(select(MarketSignalPrediction).where(
            MarketSignalPrediction.delivery_status.in_(("queued", "sent", "failed")))))
        for row in rows:
            already_delivered = row.delivery_status == "sent"
            rule = db.get(MarketSignalRule, row.rule_id)
            assessment = db.get(MarketSignalAssessment, row.assessment_id)
            snapshot = json.loads(row.snapshot_json)
            config = snapshot["rule"]
            reason = None if settings["enabled"] else "异动提醒已关闭"
            reason = reason or _eligibility(db, assessment, rule, json.loads(rule.config_json), now)
            if row.delivery_status == "suppressed" or config["mode"] != "trial":
                reason = reason or "该预测已取消或未启用试用提醒"
            active = [other for other in all_predictions if other.delivery_status in ("queued", "sent")]
            today = [other for other in active if (
                json.loads(other.snapshot_json).get("budget_day") == (now+CHINA).date().isoformat())]
            other_events = {(other.event_key, other.fact_key) for other in today if other.event_key != row.event_key or other.fact_key != row.fact_key}
            if len(other_events) >= settings["global_daily_limit"]:
                reason = reason or "发送前检查：今日全局额度已用完"
            target_others = [other for other in active if other.target_type == row.target_type
                and other.target_key == row.target_key
                and (other.event_key, other.fact_key) != (row.event_key, row.fact_key)]
            target_events = {(other.event_key, other.fact_key) for other in today if other in target_others}
            if not already_delivered and len(target_events) >= config["max_daily"]:
                reason = reason or "发送前检查：该关注对象的今日额度已用完"
            if not already_delivered and config["cooldown_hours"]:
                cutoff = now-timedelta(hours=config["cooldown_hours"])
                for other in target_others:
                    reserved_at = other.sent_at or sources.utc(json.loads(other.snapshot_json).get("prepared_at")) or other.created_at
                    if reserved_at > cutoff:
                        reason = reason or "发送前检查：该关注对象仍在冷却窗口内"
                        break
            try:
                if reason:
                    raise QuoteUnavailable(reason)
                if already_delivered:
                    valid.append(row)
                    continue
                context = quote_context(db, config, now)
                snapshot.setdefault("preparations", {})[str(outbox.id)] = dict(context)
                row.due_at = context.pop("due_at")
                snapshot.update(context)
                snapshot.update(prepared_at=now, budget_day=(now+CHINA).date().isoformat())
                row.snapshot_json = _json(snapshot)
                if row.delivery_status == "failed":
                    row.delivery_status, row.outcome_status, row.outcome_json = "queued", "pending", "{}"
                valid.append(row)
            except QuoteUnavailable as exc:
                if not already_delivered:
                    row.delivery_status, row.outcome_status = "suppressed", "not_applicable"
                    row.outcome_json = _json({"reason": str(exc), "checked_at": now})
        if valid:
            outbox.payload_json = _json(_payload(valid))
        db.commit()
        return bool(valid)


def suppress_outbox(db, outbox):
    outbox.status, outbox.last_error_code = "dead_letter", "market_signal_suppressed"
    outbox.last_error_message = "规则已关闭、过期或行情验证条件不满足；已取消本次发送"
    outbox.next_retry_at = None
    db.add(NotificationDelivery(outbox_id=outbox.id, channel_id=outbox.channel_id,
        attempt_number=outbox.attempt_count+1, status="failed", error_code="market_signal_suppressed",
        error_message=outbox.last_error_message, duration_ms=0, sent_at=sources.utcnow(), created_at=sources.utcnow()))
    db.commit()


def record_delivery(db, outbox, *, locked=False):
    if not locked:
        outbox_id = outbox.id
        with market_event_write_lock(db, namespace="market-signals-engine"):
            db.rollback()
            current = db.get(NotificationOutbox, outbox_id, populate_existing=True)
            return record_delivery(db, current, locked=True)
    if outbox.status != "sent":
        return
    for row in _prediction_rows(db, outbox):
        if row.sent_at is not None and row.sent_at <= outbox.sent_at:
            continue
        snapshot = json.loads(row.snapshot_json)
        preparation = snapshot.get("preparations", {}).get(str(outbox.id))
        if preparation:
            row.due_at = sources.utc(preparation["due_at"])
            snapshot.update({key: value for key, value in preparation.items() if key != "due_at"})
        snapshot.pop("validation_block", None)
        row.delivery_status, row.sent_at, row.outbox_id = "sent", outbox.sent_at, outbox.id
        row.outcome_status, row.outcome_json = "pending", "{}"
        local = outbox.sent_at+CHINA
        # A transport that crosses the market open cannot reuse a pre-open
        # daily-bar anchor as though delivery happened before the open.
        from app.models.market_data import MarketCalendar
        calendar = db.get(MarketCalendar, local.date())
        if calendar and calendar.is_trading_day and (9, 30) <= (local.hour, local.minute) < (15, 0):
            snapshot["validation_block"] = "实际送达进入盘中，日线无法区分送达前后的波动"
            row.outcome_status = "unverifiable"
            row.outcome_json = _json({"reason": "实际送达进入盘中，日线无法区分送达前后的波动"})
        row.snapshot_json = _json(snapshot)
    db.commit()
