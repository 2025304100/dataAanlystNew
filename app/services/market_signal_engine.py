"""Immutable predictions, delivery gating, and honest post-delivery settlement."""
import json
import math
from datetime import date, datetime, time, timedelta

from sqlalchemy import and_, func, or_, select
from fastapi import HTTPException

from app.models.market_signal import MarketSignalAssessment, MarketSignalEvidence, MarketSignalPrediction, MarketSignalRule, MarketSignalOutcomeVersion
from app.services import market_signal_sources as sources
from app.services.market_event_collection_lock import market_event_write_lock
from app.services.market_signal_quotes import CHINA, QuoteUnavailable, load_bars, quote_context, valid_bar
from app.services.market_signals import _json, get_settings


def prediction_read(row):
    return {"id": row.id, "assessment_id": row.assessment_id, "rule_id": row.rule_id,
        "rule_revision": row.rule_revision, "target_type": row.target_type, "target_key": row.target_key,
        "delivery_status": row.delivery_status, "outcome_status": row.outcome_status,
        "snapshot": json.loads(row.snapshot_json), "outcome": json.loads(row.outcome_json),
        "outbox_id": row.outbox_id, "sent_at": row.sent_at, "due_at": row.due_at, "created_at": row.created_at}


def list_predictions(db, *, rule_id=None, offset=0, limit=50):
    query = select(MarketSignalPrediction)
    if rule_id:
        query = query.where(MarketSignalPrediction.rule_id == rule_id)
    return {"total": db.scalar(select(func.count()).select_from(query.subquery())) or 0,
        "items": [prediction_read(row) for row in db.scalars(query.order_by(
            MarketSignalPrediction.id.desc()).offset(offset).limit(limit))]}


def _eligibility(db, assessment, rule, config, now):
    evidence = db.get(MarketSignalEvidence, assessment.evidence_id)
    if not evidence or evidence.time_quality not in ("supplier", "manual") or not evidence.published_at:
        return "来源发布时间不明确，需补充准确时间"
    age = (now-evidence.published_at).total_seconds()/3600
    if age < 0 or age > config["max_age_hours"]:
        return "消息尚未发布或已超出有效期"
    if not rule.enabled or assessment.rule_revision != rule.revision:
        return "规则已暂停或分析不属于当前规则版本"
    analysis = json.loads(assessment.analysis_json)
    if assessment.status != "scored" or assessment.score is None or assessment.score < config["min_score"]:
        return "未达到评分门槛或证据不足"
    if analysis.get("novelty_points", 0) < 20:
        return "没有足够新增事实，重复报道不提醒"
    return None


def inflight_prediction_ids(db):
    from app.models.notification import NotificationOutbox
    ids = set()
    for box in db.scalars(select(NotificationOutbox).where(
            NotificationOutbox.source_type == "market_signal", NotificationOutbox.status == "sending")):
        candidates = json.loads(box.payload_json or "{}").get("prediction_ids", [])
        for row in db.scalars(select(MarketSignalPrediction).where(MarketSignalPrediction.id.in_(candidates))):
            preparation = json.loads(row.snapshot_json).get("preparations", {}).get(str(box.id), {})
            if preparation.get("transport_attempt") == box.attempt_count+1:
                ids.add(row.id)
    return ids


def prediction_budget_day(row, now, inflight=()):
    if row.sent_at is not None:
        return (sources.utc(row.sent_at)+CHINA).date().isoformat()
    if row.id in inflight:
        # An unconfirmed transport can cross midnight. Keep its reservation
        # active on the current day until success/failure is known.
        return (now+CHINA).date().isoformat()
    return json.loads(row.snapshot_json).get("budget_day")


def prediction_occupies_budget(row, inflight=()):
    return row.delivery_status in ("shadow", "waiting_digest", "queued", "sent") or (
        row.delivery_status == "failed" and row.id in inflight)


def _settle(db, now):
    rows = list(db.scalars(select(MarketSignalPrediction).where(
        MarketSignalPrediction.delivery_status.in_(("shadow", "sent")),
        MarketSignalPrediction.outcome_status == "pending", MarketSignalPrediction.due_at <= now)))
    for row in rows:
        snapshot = json.loads(row.snapshot_json)
        if snapshot.get("validation_block"):
            row.outcome_status, row.outcome_json = "unverifiable", _json({"reason": snapshot["validation_block"]})
            continue
        config = {**snapshot["rule"], "quote_store": snapshot.get("quote_store")}
        dates = [date.fromisoformat(value) for value in snapshot["observation_dates"]]
        reference_day = date.fromisoformat(snapshot["reference_date"])
        bars = load_bars(db, config, start=reference_day, end=dates[-1], limit=20)
        mapping = {bar.trade_date: bar for bar in bars}
        needed = [reference_day, *dates]
        if any(day not in mapping or not valid_bar(mapping[day]) for day in needed):
            row.outcome_status, row.outcome_json = "unverifiable", _json({"reason": "到期行情缺失或无效", "settled_at": now})
            continue
        if any(mapping[day].source != snapshot["price_source"] for day in needed):
            row.outcome_status, row.outcome_json = "unverifiable", _json({"reason": "行情来源或复权口径发生变化，需复核", "settled_at": now})
            continue
        # Re-read the same reference on the current qfq scale to remove mechanical
        # dividend/split scaling. The original prediction snapshot remains intact.
        reference = mapping[reference_day].close
        moves = [(day, max(abs(mapping[day].high/reference-1), abs(mapping[day].low/reference-1))*100) for day in dates]
        max_move = max(value for _, value in moves)
        first_hit = next((day.isoformat() for day, value in moves if value+1e-10 >= snapshot["threshold_pct"]), None)
        row.outcome_status = "hit" if first_hit else "miss"
        row.outcome_json = _json({"max_move_pct": max_move, "first_hit_date": first_hit,
            "outcome_reference_close": reference, "reference_rebased": reference != snapshot["reference_close"],
            "settled_at": now, "bars": [{"date": day.isoformat(), "high": mapping[day].high,
                "low": mapping[day].low, "close": mapping[day].close} for day in dates]})
    for row in rows:
        if row.outcome_status == "pending":
            continue
        version = (db.scalar(select(func.max(MarketSignalOutcomeVersion.version)).where(
            MarketSignalOutcomeVersion.prediction_id == row.id)) or 0)+1
        db.add(MarketSignalOutcomeVersion(prediction_id=row.id, version=version,
            status=row.outcome_status, result_json=row.outcome_json, created_at=now))
    db.commit()
    return len(rows)


def outcome_history(db, prediction_id):
    if db.get(MarketSignalPrediction, prediction_id) is None:
        raise HTTPException(404, "预测记录不存在")
    return [{"version": row.version, "status": row.status, "result": json.loads(row.result_json),
             "created_at": row.created_at} for row in db.scalars(select(MarketSignalOutcomeVersion).where(
                 MarketSignalOutcomeVersion.prediction_id == prediction_id).order_by(MarketSignalOutcomeVersion.version))]


def recheck(db, prediction_id):
    with market_event_write_lock(db, namespace="market-signals-engine"):
        db.rollback()
        row = db.get(MarketSignalPrediction, prediction_id)
        if row is None:
            raise HTTPException(404, "预测记录不存在")
        if row.delivery_status not in ("shadow", "sent") or row.due_at > sources.utcnow():
            raise HTTPException(409, "只有已送达或影子预测到期后才可复核")
        row.outcome_status = "pending"
        db.flush()
        _settle(db, sources.utcnow())
        db.refresh(row)
        return prediction_read(row)


def _reconcile_deliveries(db):
    from app.models.notification import NotificationOutbox
    from app.services.market_signal_delivery import record_delivery
    pending = list(db.scalars(select(MarketSignalPrediction).where(or_(
        MarketSignalPrediction.delivery_status.in_(("queued", "failed")),
        and_(MarketSignalPrediction.delivery_status == "sent",
            MarketSignalPrediction.sent_at >= sources.utcnow()-timedelta(days=8))))))
    for row in pending:
        ids = json.loads(row.snapshot_json).get("outbox_ids", [row.outbox_id] if row.outbox_id else [])
        outboxes = list(db.scalars(select(NotificationOutbox).where(NotificationOutbox.id.in_(ids))))
        sent = sorted((box for box in outboxes if box.status == "sent"), key=lambda box: box.sent_at)
        if sent:
            record_delivery(db, sent[0], locked=True)
        elif row.sent_at is None and outboxes and all(box.status == "dead_letter" for box in outboxes):
            row.delivery_status, row.outcome_status = "failed", "not_applicable"
            row.outcome_json = _json({"reason": "通知未成功送达，可从通知中心按原记录重试"})
    db.commit()


def _digest_slot(config, now):
    local = now+CHINA
    hour, minute = (int(value) for value in config["digest_time"].split(":"))
    target = datetime.combine(local.date(), time(hour, minute))
    if local > target:
        target += timedelta(days=1)
    return target-CHINA


def _flush_digest(db, now, settings):
    ready = []
    for row in db.scalars(select(MarketSignalPrediction).where(MarketSignalPrediction.delivery_status == "waiting_digest")):
        snapshot = json.loads(row.snapshot_json)
        rule = db.get(MarketSignalRule, row.rule_id)
        assessment = db.get(MarketSignalAssessment, row.assessment_id)
        reason = _eligibility(db, assessment, rule, json.loads(rule.config_json), now)
        if not settings["enabled"]:
            reason = "异动提醒已关闭"
        if reason:
            row.delivery_status, row.outcome_status = "suppressed", "not_applicable"
            row.outcome_json = _json({"reason": reason})
        elif sources.utc(snapshot["digest_due_at"]) <= now:
            row.delivery_status = "queued"
            ready.append(row)
    if ready:
        from app.services.market_signal_delivery import queue_predictions
        queue_predictions(db, ready, now)
    db.commit()


def _candidate_assessments(db, now):
    # Apply each live rule's own horizon before pagination. A fixed historical
    # top-N would permanently starve fresh events behind expired/high scores.
    rules = list(db.scalars(select(MarketSignalRule).where(MarketSignalRule.enabled.is_(True))))
    scopes = []
    for rule in rules:
        config = json.loads(rule.config_json)
        scopes.append(and_(MarketSignalAssessment.rule_id == rule.id,
            MarketSignalAssessment.rule_revision == rule.revision,
            MarketSignalAssessment.score >= config["min_score"],
            MarketSignalEvidence.published_at >= now-timedelta(hours=config["max_age_hours"])))
    if not scopes:
        return
    query = select(MarketSignalAssessment).join(MarketSignalEvidence,
        MarketSignalEvidence.id == MarketSignalAssessment.evidence_id).where(
            MarketSignalAssessment.status == "scored",
            MarketSignalEvidence.time_quality.in_(("supplier", "manual")),
            MarketSignalEvidence.published_at <= now, or_(*scopes),
            ~select(MarketSignalPrediction.id).where(
                MarketSignalPrediction.assessment_id == MarketSignalAssessment.id).exists())
    cursor = None
    while True:
        page = query
        if cursor is not None:
            score, row_id = cursor
            page = page.where(or_(MarketSignalAssessment.score < score,
                and_(MarketSignalAssessment.score == score, MarketSignalAssessment.id < row_id)))
        rows = list(db.scalars(page.order_by(
            MarketSignalAssessment.score.desc(), MarketSignalAssessment.id.desc()).limit(500)))
        if not rows:
            return
        # Finish reading a page before issuing writes/other queries: MySQL does
        # not permit interleaving queries with an unbuffered streaming cursor.
        cursor = (rows[-1].score, rows[-1].id)
        yield from rows
        if len(rows) < 500:
            return


def run(db, payload):
    settings = get_settings(db)
    if payload.analyze and settings["enabled"]:
        from app.services.market_signal_analysis import analyze
        from app.schemas.market_signal import SignalAnalyzeRequest
        analyze(db, SignalAnalyzeRequest())
    with market_event_write_lock(db, namespace="market-signals-engine"):
        db.rollback()
        now = sources.utcnow()
        settings = get_settings(db)
        _reconcile_deliveries(db)
        settled = _settle(db, now)
        _flush_digest(db, now, settings)
        if not settings["enabled"]:
            return {"created": 0, "settled": settled, "reason": "异动提醒尚未启用"}
        created, trial_rows = 0, []
        inflight = inflight_prediction_ids(db)
        for assessment in _candidate_assessments(db, now):
            rule = db.get(MarketSignalRule, assessment.rule_id)
            config = json.loads(rule.config_json)
            reason = _eligibility(db, assessment, rule, config, now)
            key = sources.digest(f"{assessment.event_key}:{assessment.fact_key}:{rule.id}:{config['window_days']}")
            if db.scalar(select(MarketSignalPrediction.id).where(MarketSignalPrediction.dedup_key == key)):
                reason = "同一事件事实已记录，规则修改或转载不会重复提醒"
            same_mode = "shadow" if config["mode"] == "shadow" else "trial"
            digest_due = _digest_slot(config, now) if config["mode"] == "trial" and config["delivery_mode"] == "digest" else None
            budget_day = ((digest_due or now)+CHINA).date().isoformat()
            today = [row for row in db.scalars(select(MarketSignalPrediction).where(
                MarketSignalPrediction.created_at >= now-timedelta(days=8)))
                if prediction_budget_day(row, now, inflight) == budget_day and json.loads(row.snapshot_json)["rule"]["mode"] == same_mode
                and prediction_occupies_budget(row, inflight)]
            existing_events = {(row.event_key, row.fact_key) for row in today}
            if (assessment.event_key, assessment.fact_key) not in existing_events and len(existing_events) >= settings["global_daily_limit"]:
                reason = reason or "已达到今日全局提醒额度"
            target_events = {(row.event_key, row.fact_key) for row in today
                             if row.target_type == rule.target_type and row.target_key == rule.target_key}
            if (assessment.event_key, assessment.fact_key) not in target_events and len(target_events) >= config["max_daily"]:
                reason = reason or "已达到该关注对象的今日额度"
            recent = False
            if config["cooldown_hours"]:
                cutoff = now-timedelta(hours=config["cooldown_hours"])
                for other in db.scalars(select(MarketSignalPrediction).where(
                    MarketSignalPrediction.target_type == rule.target_type,
                    MarketSignalPrediction.target_key == rule.target_key,
                    (MarketSignalPrediction.event_key != assessment.event_key) | (MarketSignalPrediction.fact_key != assessment.fact_key),
                    or_(MarketSignalPrediction.created_at >= now-timedelta(days=8), MarketSignalPrediction.sent_at > cutoff),
                    MarketSignalPrediction.delivery_status.in_(("shadow", "waiting_digest", "queued", "sent")))):
                    previous = json.loads(other.snapshot_json)
                    mode = previous.get("rule", {}).get("mode", "shadow" if other.delivery_status == "shadow" else "trial")
                    effective_at = other.sent_at or sources.utc(previous.get("prepared_at")) or other.created_at
                    if mode == same_mode and effective_at > cutoff:
                        recent = True
                        break
            if recent:
                reason = reason or "该关注对象仍在冷却窗口内"
            if reason:
                assessment.decision_reason = reason
                continue
            try:
                context = quote_context(db, config, now)
            except QuoteUnavailable as exc:
                assessment.decision_reason = str(exc)
                continue
            due_at = context.pop("due_at")
            snapshot = {**context, "rule": config, "rule_revision": rule.revision,
                "assessment_id": assessment.id, "score": assessment.score,
                "evidence": sources.evidence_read(db.get(MarketSignalEvidence, assessment.evidence_id)),
                "analysis": json.loads(assessment.analysis_json), "simulated_at": now,
                "validation_target": 0.75, "budget_day": budget_day, "digest_due_at": digest_due}
            row = MarketSignalPrediction(dedup_key=key, event_key=assessment.event_key, fact_key=assessment.fact_key,
                assessment_id=assessment.id, rule_id=rule.id, rule_revision=rule.revision,
                target_type=rule.target_type, target_key=rule.target_key,
                delivery_status="shadow" if config["mode"] == "shadow" else "waiting_digest" if digest_due else "queued",
                snapshot_json=_json(snapshot), due_at=due_at, outcome_status="pending", outcome_json="{}", created_at=now)
            db.add(row)
            db.flush()
            if config["mode"] == "trial" and not digest_due:
                trial_rows.append(row)
            assessment.decision_reason = "已生成影子预测，未发送通知" if config["mode"] == "shadow" else "已进入试用提醒队列，发送前会再次检查规则"
            created += 1
        if trial_rows:
            from app.services.market_signal_delivery import queue_predictions
            queue_predictions(db, trial_rows, now)
        db.commit()
        _flush_digest(db, now, settings)
        return {"created": created, "settled": settled}


def _summary(rows):
    from app.services.market_signal_stats import summarize_predictions
    return summarize_predictions(rows)


def dashboard(db, rule_id=None):
    query = select(MarketSignalPrediction)
    if rule_id:
        query = query.where(MarketSignalPrediction.rule_id == rule_id)
    rows = list(db.scalars(query))
    return {"target_precision": 0.75, "live": _summary([row for row in rows if row.delivery_status == "sent"]),
        "shadow": _summary([row for row in rows if row.delivery_status == "shadow"]),
        "queued": sum(row.delivery_status in ("queued", "waiting_digest") for row in rows),
        "suppressed": sum(row.delivery_status in ("suppressed", "failed") for row in rows),
        "rules": [{"rule_id": rule.id, "name": rule.name, "revision": rule.revision,
            "live": _summary([row for row in rows if row.rule_id == rule.id and row.rule_revision == rule.revision and row.delivery_status == "sent"]),
            "shadow": _summary([row for row in rows if row.rule_id == rule.id and row.rule_revision == rule.revision and row.delivery_status == "shadow"]),
            "history_matured": sum(row.rule_id == rule.id and row.outcome_status in ("hit", "miss", "unverifiable") for row in rows)}
            for rule in db.scalars(select(MarketSignalRule).order_by(MarketSignalRule.id))]}
