"""Market-sensitive subscriptions. All public writes retain config versions."""
import json
import re

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select, or_

from app.models.market_signal import MarketSignalRule, MarketSignalRuleVersion, MarketSignalSettings
from app.models.ai_profile import AIProfile
from app.models.symbol import Symbol
from app.models.universe import UniverseSymbol
from app.schemas.market_signal import SignalRuleConfig
from app.services.sector_concepts import resolve_concepts

DEFAULT_SETTINGS = {"enabled": False, "ai_profile_id": None, "global_daily_limit": 3,
                    "max_analysis_per_run": 10, "auto_analyze": True, "collect_on_monitor": True, "activation_at": None}


def get_settings(db):
    row = db.get(MarketSignalSettings, 1)
    return {**DEFAULT_SETTINGS, **(json.loads(row.config_json) if row else {})}


def update_settings(db, payload):
    from app.services.market_event_collection_lock import market_event_write_lock
    with market_event_write_lock(db, namespace="market-signals-settings"):
        db.rollback()
        return _update_settings(db, payload)


def _update_settings(db, payload):
    from app.services.market_signal_sources import utcnow
    old = get_settings(db)
    config = {**old, **payload.model_dump(exclude_unset=True)}
    for key in ("enabled", "global_daily_limit", "max_analysis_per_run", "auto_analyze", "collect_on_monitor"):
        if config[key] is None:
            raise HTTPException(422, f"{key} 不得为空")
    if config["ai_profile_id"] is not None:
        profile = db.get(AIProfile, config["ai_profile_id"])
        if not profile or not profile.is_enabled or profile.purpose not in ("all", "explanation"):
            raise HTTPException(422, "请选择已启用且用途为解释或全部的 AI 配置")
    if config["enabled"] and not config["ai_profile_id"]:
        raise HTTPException(422, "启用前请先选择 AI 配置")
    if config["enabled"] and not old["enabled"]:
        config["activation_at"] = utcnow().isoformat()
    row = db.get(MarketSignalSettings, 1)
    if row is None:
        row = MarketSignalSettings(id=1)
        db.add(row)
    row.config_json = _json(config)
    db.commit()
    if config["enabled"]:
        ensure_monitor_schedule(db)
    else:
        from app.models.market_signal import MarketSignalPrediction
        for prediction in db.scalars(select(MarketSignalPrediction).where(
                MarketSignalPrediction.delivery_status.in_(("queued", "waiting_digest")))):
            prediction.delivery_status, prediction.outcome_status = "suppressed", "not_applicable"
            prediction.outcome_json = _json({"reason": "用户关闭异动提醒，取消未送达预测"})
        db.commit()
    return config


def ensure_monitor_schedule(db):
    from app.models.scheduled_task import ScheduledTask
    from app.schemas.scheduled_task import ScheduledTaskCreate, ScheduledTaskUpdate
    from app.services.scheduled_tasks import create_schedule, update_schedule
    row = db.scalar(select(ScheduledTask).where(ScheduledTask.task_type == "market_signals_monitor").order_by(ScheduledTask.id))
    if row:
        if not row.enabled:
            update_schedule(db, row.id, ScheduledTaskUpdate(enabled=True))
        return
    create_schedule(db, ScheduledTaskCreate(name="行情异动提醒自动监控", task_type="market_signals_monitor",
        frequency="interval", interval_minutes=15, weekdays=[], timezone="Asia/Shanghai", payload={}, enabled=True))


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _rule_read(row):
    return {**json.loads(row.config_json), "id": row.id, "revision": row.revision,
            "created_at": row.created_at, "updated_at": row.updated_at}


def list_rules(db):
    return [_rule_read(row) for row in db.scalars(select(MarketSignalRule).order_by(MarketSignalRule.id))]


def _get_rule(db, rule_id):
    row = db.get(MarketSignalRule, rule_id)
    if row is None:
        raise HTTPException(404, "关注规则不存在")
    return row


def _validated_config(db, value):
    try:
        config = SignalRuleConfig.model_validate(value).model_dump()
    except ValidationError as exc:
        raise HTTPException(422, exc.errors(include_context=False)) from exc
    if config["target_type"] == "symbol":
        symbol = db.scalar(select(Symbol).where(Symbol.symbol == config["target_key"]))
        if symbol is None:
            symbol = db.scalar(select(UniverseSymbol).where(UniverseSymbol.symbol == config["target_key"]))
        if symbol is None:
            raise HTTPException(422, "标的不在本地标的库，请先同步或添加标的")
        if not re.fullmatch(r"\d{6}", symbol.symbol) or symbol.asset_type not in ("stock", "etf"):
            raise HTTPException(422, "首版日线验证支持A股股票和ETF，请选择六位代码标的")
        config["quote_key"] = symbol.symbol
        config["quote_type"] = "symbol"
        config["target_name"] = symbol.name
    else:
        if not re.fullmatch(r"\d{6}", config["quote_key"] or ""):
            raise HTTPException(422, "验证指数或ETF需使用六位代码")
        if config["quote_type"] == "symbol":
            quote = db.scalar(select(Symbol).where(Symbol.symbol == config["quote_key"])) or db.scalar(
                select(UniverseSymbol).where(UniverseSymbol.symbol == config["quote_key"]))
            if quote is None or quote.asset_type not in ("stock", "etf"):
                raise HTTPException(422, "验证标的不在本地股票/ETF库，请先同步标的元数据")
        if config["target_key"] not in {item.id for item in resolve_concepts(db)}:
            raise HTTPException(422, "板块不存在")
        config["target_name"] = config["target_key"]
    from app.services.market_signal_delivery import validate_channels
    validate_channels(db, config["channel_ids"])
    return config


def metadata(db, keyword=""):
    from app.models.index_price import IndexPrice
    from app.models.scheduled_task import ScheduledTask
    from app.services.market_signal_delivery import selectable_channels
    from app.services.market_signal_quotes import quote_context, QuoteUnavailable
    from app.services.market_signal_sources import utcnow
    choices = {}
    for model in (Symbol, UniverseSymbol):
        query = select(model)
        if keyword:
            query = query.where(or_(model.symbol.contains(keyword, autoescape=True), model.name.contains(keyword, autoescape=True)))
        for row in db.scalars(query.order_by(model.symbol).limit(80)):
            choices.setdefault(row.symbol, {"key": row.symbol, "name": row.name or row.symbol,
                                            "asset_type": row.asset_type})
    readiness = []
    for rule in db.scalars(select(MarketSignalRule).order_by(MarketSignalRule.id)):
        config = json.loads(rule.config_json)
        try:
            context = quote_context(db, config, utcnow())
            readiness.append({"rule_id": rule.id, "ready": True, "reason": None, "context": context})
        except QuoteUnavailable as exc:
            readiness.append({"rule_id": rule.id, "ready": False, "reason": str(exc)})
    return {"symbols": list(choices.values()), "sectors": [{"key": item.id, "name": item.id} for item in resolve_concepts(db)],
        "indices": list(db.scalars(select(IndexPrice.symbol).distinct().order_by(IndexPrice.symbol))),
        "ai_profiles": [{"id": row.id, "name": row.name, "model": row.model,
                          "enabled": row.is_enabled, "daily_request_limit": row.daily_request_limit,
                          "daily_request_count": row.daily_request_count} for row in db.scalars(select(AIProfile).where(
                              AIProfile.purpose.in_(("all", "explanation"))).order_by(AIProfile.id))],
        "channels": selectable_channels(db), "readiness": readiness,
        "schedules": [{"id": row.id, "enabled": bool(row.enabled), "last_status": row.last_status,
                       "next_run_at": row.next_run_at} for row in db.scalars(select(ScheduledTask).where(
                           ScheduledTask.task_type == "market_signals_monitor"))]}


def create_rule(db, payload):
    config = _validated_config(db, payload.model_dump())
    row = MarketSignalRule(name=config["name"], target_type=config["target_type"],
        target_key=config["target_key"], enabled=config["enabled"], revision=1, config_json=_json(config))
    db.add(row)
    db.flush()
    db.add(MarketSignalRuleVersion(rule_id=row.id, revision=1, config_json=row.config_json))
    db.commit()
    db.refresh(row)
    return _rule_read(row)


def update_rule(db, rule_id, payload):
    from app.services.market_event_collection_lock import market_event_write_lock
    with market_event_write_lock(db, namespace="market-signals-engine"):
        db.rollback()
        return _update_rule(db, rule_id, payload)


def _update_rule(db, rule_id, payload):
    row = _get_rule(db, rule_id)
    config = json.loads(row.config_json)
    config.pop("target_name", None)
    config.update(payload.model_dump(exclude_unset=True))
    config = _validated_config(db, config)
    if _json(config) != row.config_json:
        row.revision += 1
        row.name, row.target_type, row.target_key = config["name"], config["target_type"], config["target_key"]
        row.enabled, row.config_json = config["enabled"], _json(config)
        db.add(MarketSignalRuleVersion(rule_id=row.id, revision=row.revision, config_json=row.config_json))
        db.commit()
        db.refresh(row)
    return _rule_read(row)


def rule_versions(db, rule_id):
    _get_rule(db, rule_id)
    return [{"revision": row.revision, "config": json.loads(row.config_json), "created_at": row.created_at}
            for row in db.scalars(select(MarketSignalRuleVersion).where(
                MarketSignalRuleVersion.rule_id == rule_id).order_by(MarketSignalRuleVersion.revision))]
