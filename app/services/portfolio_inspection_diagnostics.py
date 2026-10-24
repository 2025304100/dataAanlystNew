"""Evidence-preserving portfolio data inspection; no writes or admission changes.

Business dates, publication timestamps and ingestion timestamps remain separate.
An unavailable calendar never falls back to weekdays or the latest stored bar.
"""
from __future__ import annotations

import hashlib
import json
import logging
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.app_setting import AppSetting
from app.models.daily_bar import DailyBar
from app.models.decision_engine import PortfolioFactorUsage, StrategyExecutionSnapshot
from app.models.market_data import MarketCalendar
from app.models.portfolio import Portfolio, PortfolioRule, Position
from app.models.portfolio_candidate import PortfolioCandidate
from app.models.portfolio_member import PortfolioMember
from app.models.score import Score
from app.models.scoring_config import ScoringConfig
from app.models.symbol import Symbol
from app.schemas.portfolio_inspection import PortfolioInspectionResponse
from app.services.factors.__facade__ import read_inspection_factor_dependencies, read_inspection_factor_value
from app.services.portfolio_asset_scope import allows_asset_type

logger = logging.getLogger(__name__)
POLICY_VERSION = "portfolio-inspection-v1"
CN_ZONE = ZoneInfo("Asia/Shanghai")
CN_SOURCE = "https://www.sse.com.cn/disclosure/dealinstruc/closed/"
CN_MARKETS = {"SH", "SZ", "BJ", "SSE", "SZSE", "BSE", "CN", "CN-A"}
BAD_STATUSES = {"MISSING", "STALE", "READ_ERROR", "VERSION_MISMATCH", "FUTURE_DATA", "CALENDAR_UNKNOWN", "UNKNOWN"}
ADMISSION_CHECK_CODES = {"DAILY_BAR_FRESHNESS", "RAW_BAR_INPUT", "SCORE_FRESHNESS", "SCORING_CONFIGURATION",
    "STRATEGY_BINDING", "SCORING_POLICY", "RULE_BINDING", "FACTOR_DEPENDENCIES", "MEMBER_RULE_VERSION",
    "SCOPE_READ", "SYMBOL_READ", "EXPECTED_TRADE_DATE"}
INDICATOR_LABELS = {"trend_score": "趋势指标", "momentum_score": "动量指标",
    "volatility_score": "波动指标", "liquidity_score": "流动性指标", "breadth_score": "题材指标",
    "amount_activity": "成交活跃指标", "event_score": "事件指标", "pe_score": "估值指标",
    "main_net_inflow_score": "主力资金指标", "breakout_score": "突破指标", "pullback_score": "回调指标"}
_CURRENT_READ_KEY = "portfolio_inspection_current_read"


@contextmanager
def _read_mode(db: Session, current_read: bool):
    previous = db.info.get(_CURRENT_READ_KEY)
    db.info[_CURRENT_READ_KEY] = bool(current_read or previous)
    try:
        yield
    finally:
        if previous is None:
            db.info.pop(_CURRENT_READ_KEY, None)
        else:
            db.info[_CURRENT_READ_KEY] = previous


def _execute_select(db: Session, statement):
    if db.info.get(_CURRENT_READ_KEY):
        statement = statement.with_for_update().execution_options(populate_existing=True)
    return db.execute(statement)


def _get_record(db: Session, model, key):
    current = bool(db.info.get(_CURRENT_READ_KEY))
    return db.get(model, key, with_for_update=current, populate_existing=current)


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _object(raw: str | None) -> dict:
    value = json.loads(raw or "{}")
    if not isinstance(value, dict):
        raise ValueError("expected_object")
    return value


def _safe_read(label: str, callback):
    try:
        return callback(), None
    except Exception:
        # Never echo SQL, connection details, or raw database exceptions to clients.
        logger.warning("Inspection read failed source=%s", label, exc_info=True)
        return None, "数据读取失败，请稍后重试或检查数据源任务"


def _calendar(db: Session, market: str, at: datetime) -> dict:
    local = at.astimezone(CN_ZONE)
    result = {"market": market, "status": "CALENDAR_UNKNOWN", "source": None,
        "version": None, "synced_at": None, "expected_trade_date": None,
        "not_due": False, "trading_dates": [], "reason": "当前没有该市场的可核验交易日历"}
    if market.upper() not in CN_MARKETS:
        return result
    state, error = _safe_read("calendar_provenance", lambda: _get_record(db, AppSetting, "market.cn_trade_calendar"))
    if error:
        return {**result, "status": "READ_ERROR", "reason": error}
    try:
        provenance = _object(state.value_json) if state else {}
        if (provenance.get("source") != CN_SOURCE or provenance.get("year") != local.year
                or state.updated_by != "exchange-calendar-sync"):
            return {**result, "reason": "日历缺少当前年度交易所同步来源证明"}
        stamp = _utc(datetime.fromisoformat(provenance["refreshed_at"]))
        if stamp > at or not 235 <= int(provenance.get("trading_days", 0)) <= 250:
            return {**result, "reason": "日历来源版本或同步时间不可信"}
    except (TypeError, ValueError, KeyError, AttributeError):
        return {**result, "reason": "日历来源记录无法解析"}
    rows, error = _safe_read("trade_calendar", lambda: _execute_select(db, select(MarketCalendar).where(
        MarketCalendar.trade_date >= date(local.year, 1, 1),
        MarketCalendar.trade_date <= date(local.year, 12, 31),
    ).order_by(MarketCalendar.trade_date)).scalars().all())
    if error:
        return {**result, "status": "READ_ERROR", "reason": error}
    by_day = {row.trade_date: row.is_trading_day for row in rows}
    year_start, year_end = date(local.year, 1, 1), date(local.year, 12, 31)
    required = {year_start + timedelta(days=i) for i in range((year_end - year_start).days + 1)}
    open_dates = sorted(day for day, flag in by_day.items() if flag == 1)
    if (set(by_day) != required or any(flag not in (0, 1) for flag in by_day.values())
            or len(open_dates) != provenance["trading_days"]):
        return {**result, "reason": "日历日期不完整或与已同步来源的交易日数量冲突"}
    today_due = local.time().replace(tzinfo=None) >= time(15, 0)
    due = [day for day in open_dates if day < local.date() or (day == local.date() and today_due)]
    expected = due[-1] if due else None
    calendar_identity = {"source": provenance["source"], "year": provenance["year"],
        "publication_versions": {key: provenance[key] for key in (
            "version", "publication_version", "source_version", "release_version") if key in provenance},
        "days": sorted(by_day.items())}
    return {**result, "status": "PASSED" if expected else "CALENDAR_UNKNOWN",
        "source": CN_SOURCE, "version": _hash(calendar_identity),
        "synced_at": stamp, "expected_trade_date": expected, "trading_dates": open_dates,
        "not_due": bool(by_day.get(local.date()) == 1 and not today_due),
        "reason": "当日日K尚未到收盘可用时点，检查上一应到交易日" if by_day.get(local.date()) == 1 and not today_due else "按交易所年度日历与15:00收盘时点计算应到数据日"}


def _lag(calendar: dict, actual: date, expected: date) -> int | None:
    days = calendar["trading_dates"]
    if actual not in days or expected not in days:
        return None
    return days.index(expected) - days.index(actual)


def resolve_inspection_calendar(db: Session, *, market: str = "CN", at: datetime | None = None, current_read: bool = False) -> dict:
    """Public, read-only expected-session evidence for other portfolio routes."""
    with db.no_autoflush, _read_mode(db, current_read):
        return _calendar(db, market, _utc(at or datetime.now(timezone.utc)))


def resolve_inspection_expected_trade_date(db: Session, *, market: str = "CN", at: datetime | None = None, current_read: bool = False) -> date | None:
    """Return an independently expected date; unknown calendars return None."""
    return resolve_inspection_calendar(db, market=market, at=at, current_read=current_read)["expected_trade_date"]


def _freshness(actual: date | None, calendar: dict, allowed_lag: int, available_at: datetime | None,
        cutoff: datetime) -> tuple[str, str, int | None]:
    if actual is None:
        return "MISSING", "没有找到对应业务日期的数据记录", None
    expected = calendar["expected_trade_date"]
    if actual > cutoff.astimezone(CN_ZONE).date() or (available_at and _utc(available_at) > cutoff):
        return "FUTURE_DATA", "数据日期或发布时间晚于本次检查截止时点", None
    if expected is None:
        return "CALENDAR_UNKNOWN", calendar["reason"], None
    if actual > expected:
        return "FUTURE_DATA", "业务日期晚于应到日期，不能用尚未到期数据替代", None
    lag = _lag(calendar, actual, expected)
    if lag is None:
        return "CALENDAR_UNKNOWN", "数据业务日期不在已核验日历范围，不能计算交易日滞后", None
    if lag > allowed_lag:
        return "STALE", f"落后{lag}个交易日，允许最多{allowed_lag}个交易日", lag
    return "PASSED", "业务日期和可用时点满足本次诊断要求", lag


def _evidence(at: datetime, *, evidence_id: str, check_code: str, label: str,
        status: str, reason: str, source_code: str, source_label: str, symbol: Symbol | None = None,
        **kwargs) -> dict:
    return {"evidence_id": evidence_id, "check_code": check_code, "label": label,
        "status": status, "reason": reason, "source_code": source_code, "source_label": source_label,
        "symbol_id": symbol.id if symbol else None, "symbol": symbol.symbol if symbol else None,
        "observed_at": at, "admission_relevant": check_code in ADMISSION_CHECK_CODES, **kwargs}


def inspection_admission_failures(evidence: list[dict]) -> list[dict]:
    """The same executed-scope predicate used by safety and runtime views.

    Legacy factor-value provenance and indicator explanation are diagnostic
    evidence; the real model-score lineage is checked separately. Missing
    production input must not be inferred from a different storage system.
    """
    return [ev for ev in evidence if ev.get("admission_relevant", False) and ev.get("status") in BAD_STATUSES]


def _member_rule_evidence(at: datetime, symbol: Symbol, version_id: int, binding: dict, caps: list[str]) -> dict:
    actual_rule_verified = bool(binding.get("rule_verified"))
    reason = (f"成员旧字段引用的版本 {version_id} 没有独立注册记录；实际执行采用已应用快照中的策略 {binding.get('rule_id')} / 版本 {binding.get('rule_version')}"
        if actual_rule_verified else f"成员规则版本 {version_id} 缺少可核验版本注册记录，且当前实际执行策略尚未核验")
    return _evidence(at, evidence_id=f"{symbol.id}:member_rule:{version_id}", check_code="MEMBER_RULE_VERSION",
        label="成员入场规则版本", status="UNKNOWN", reason=reason, symbol=symbol,
        source_code="portfolio_members", source_label="组合自动成员", expected=version_id,
        affected_capabilities=[] if actual_rule_verified else caps, admission_relevant=not actual_rule_verified,
        metadata={"actual_rule_id": binding.get("rule_id"), "actual_rule_version": binding.get("rule_version"),
            "legacy_reference_not_used_for_applied_strategy": actual_rule_verified})


def _scope(db: Session, portfolio: Portfolio, at: datetime) -> tuple[list[dict], list[dict]]:
    refs: dict[int, dict] = {}
    evidence = []
    local_day, naive = at.astimezone(CN_ZONE).date(), at.replace(tzinfo=None)
    def add(sid, scope, entry=False):
        ref = refs.setdefault(sid, {"symbol_id": sid, "scopes": [], "entry_authorized": False, "member_rule_versions": []})
        if scope not in ref["scopes"]:
            ref["scopes"].append(scope)
        ref["entry_authorized"] |= entry
    readers = [
        ("positions", "持仓范围", lambda: _execute_select(db, select(Position).where(
            Position.portfolio_id == portfolio.id, Position.quantity > 0).order_by(Position.symbol_id, Position.id)).scalars().all()),
        ("portfolio_candidates", "候选范围", lambda: _execute_select(db, select(PortfolioCandidate).where(
            PortfolioCandidate.portfolio_id == portfolio.id, PortfolioCandidate.effective_from <= local_day,
            (PortfolioCandidate.effective_to.is_(None)) | (PortfolioCandidate.effective_to >= local_day),
            PortfolioCandidate.removed_manually_flag == 0).order_by(PortfolioCandidate.symbol_id, PortfolioCandidate.id)).scalars().all()),
        ("portfolio_members", "自动成员范围", lambda: _execute_select(db, select(PortfolioMember).where(
            PortfolioMember.portfolio_id == portfolio.id,
            PortfolioMember.status.in_(["active", "paused"]), PortfolioMember.effective_from <= naive,
            (PortfolioMember.effective_to.is_(None)) | (PortfolioMember.effective_to > naive)).order_by(PortfolioMember.symbol_id, PortfolioMember.id)).scalars().all()),
    ]
    for code, label, read in readers:
        rows, error = _safe_read(code, read)
        if error:
            evidence.append(_evidence(at, evidence_id=f"scope:{code}", check_code="SCOPE_READ", label=label,
                status="READ_ERROR", reason=error, source_code=code, source_label=label,
                affected_capabilities=["new_buy", "automatic_recovery"]))
            continue
        for row in rows:
            if code == "portfolio_members" and row.execution_mode != "auto":
                # A candidate must not silently override a manual/confirm member.
                if row.symbol_id in refs:
                    refs[row.symbol_id]["entry_authorized"] = False
                continue
            entry = (code == "portfolio_candidates" and portfolio.auto_trade_source_mode == "portfolio"
                and bool(row.auto_authorized_flag)) or (code == "portfolio_members" and row.status == "active")
            add(row.symbol_id, {"positions": "position", "portfolio_candidates": "candidate",
                "portfolio_members": "auto_member"}[code], entry)
            if code == "portfolio_members" and row.status != "active":
                refs[row.symbol_id]["entry_authorized"] = False
            if code == "portfolio_members" and row.entry_rule_version_id is not None:
                refs[row.symbol_id]["member_rule_versions"].append(row.entry_rule_version_id)
    return list(refs.values()), evidence


def _binding(db: Session, portfolio_id: int, at: datetime) -> tuple[dict, list[dict], dict]:
    ev = []
    snapshot, error = _safe_read("strategy_execution_snapshots", lambda: _execute_select(db, 
        select(StrategyExecutionSnapshot).where(StrategyExecutionSnapshot.portfolio_id == portfolio_id,
            StrategyExecutionSnapshot.snapshot_type == "save_and_apply").order_by(
                StrategyExecutionSnapshot.snapshot_no.desc(), StrategyExecutionSnapshot.created_at.desc()).limit(1)
    ).scalars().first())
    binding = {"snapshot_id": snapshot.id if snapshot else None,
        "model_run_id": snapshot.factor_model_run_id if snapshot else None,
        "factor_set_id": snapshot.factor_set_id if snapshot else None,
        "source": "applied_portfolio_snapshot" if snapshot else "unbound",
        "score_allowed_lag": 0, "score_lag_basis": "conservative_diagnostic_default",
        "rule_id": snapshot.rule_id if snapshot else None,
        "rule_version": snapshot.rule_version if snapshot else None}
    if error:
        ev.append(_evidence(at, evidence_id="binding:read", check_code="STRATEGY_BINDING", label="组合实际绑定",
            status="READ_ERROR", reason=error, source_code="strategy_execution_snapshots", source_label="策略执行快照"))
        binding["read_error"] = True
    if snapshot:
        usage, error = _safe_read("portfolio_factor_usages", lambda: _get_record(db, PortfolioFactorUsage,
            snapshot.portfolio_factor_usage_id) if snapshot.portfolio_factor_usage_id else None)
        if usage and usage.score_sla_max_age_days is not None:
            binding["score_allowed_lag"] = max(0, usage.score_sla_max_age_days)
            binding["score_lag_basis"] = "portfolio_factor_usage"
        if snapshot.effective_from and _utc(snapshot.effective_from) > at:
            binding["not_effective"] = True
        if error:
            ev.append(_evidence(at, evidence_id="binding:usage", check_code="SCORING_POLICY", label="评分时效策略",
                status="READ_ERROR", reason=error, source_code="portfolio_factor_usages", source_label="组合模型绑定"))
        if snapshot.rule_id is not None:
            rule, error = _safe_read("portfolio_rules", lambda: _get_record(db, PortfolioRule, snapshot.rule_id))
            valid = bool(rule and rule.portfolio_id == portfolio_id and rule.is_active)
            binding["rule_verified"] = bool(valid and snapshot.rule_version == 1 and not error)
            ev.append(_evidence(at, evidence_id="binding:rule", check_code="RULE_BINDING", label="实际使用策略",
                status="READ_ERROR" if error else ("PASSED" if valid and snapshot.rule_version == 1 else "VERSION_MISMATCH"),
                reason=error or ("策略存在、启用且属于本组合；现有规则契约版本为1" if valid and snapshot.rule_version == 1 else "策略已失效或规则版本无法核验"),
                value={"id": rule.id, "name": rule.rule_name, "active": bool(rule.is_active)} if rule else None,
                expected={"id": snapshot.rule_id, "version": snapshot.rule_version},
                source_code="portfolio_rules", source_label="组合策略", source_record_ref=f"portfolio_rule:{snapshot.rule_id}"))
    deps = {"status": "NOT_APPLICABLE", "members": []}
    if binding["factor_set_id"]:
        deps, error = _safe_read("factor_set_dependencies", lambda: read_inspection_factor_dependencies(
            db, binding["factor_set_id"], current_read=bool(db.info.get(_CURRENT_READ_KEY))))
        if error:
            deps = {"status": "READ_ERROR", "reason": error, "members": []}
        ev.append(_evidence(at, evidence_id="binding:factors", check_code="FACTOR_DEPENDENCIES", label="冻结指标依赖",
            status=deps["status"], reason=deps["reason"], source_code="factor_sets", source_label="冻结因子集",
            expected=binding["factor_set_id"], value=len(deps["members"])))
    return binding, ev, deps


def _symbol_diagnostics(db: Session, ref: dict, symbol: Symbol, calendar: dict,
        binding: dict, deps: dict, at: datetime) -> dict:
    sid = symbol.id
    ev = []
    caps = ["new_buy", "automatic_recovery"] if ref["entry_authorized"] else []
    expected = calendar["expected_trade_date"]
    bar, error = _safe_read("daily_bars", lambda: _execute_select(db, select(DailyBar).where(
        DailyBar.symbol_id == sid).order_by(DailyBar.trade_date.desc(), DailyBar.id.desc()).limit(1)).scalars().first())
    status, reason, lag = _freshness(bar.trade_date if bar else None, calendar, 0,
        bar.created_at if bar else None, at)
    if error:
        status, reason = "READ_ERROR", error
    ev.append(_evidence(at, evidence_id=f"{sid}:daily_bar", check_code="DAILY_BAR_FRESHNESS", label="日K行情",
        status=status, reason=reason, symbol=symbol, source_code="daily_bars", source_label="日K行情",
        value=bar.trade_date if bar else None, expected=expected, actual_trade_date=bar.trade_date if bar else None,
        expected_trade_date=expected, trading_day_lag=lag, allowed_lag=0, available_at=bar.created_at if bar else None,
        availability_basis="ingested_at_only", source_record_ref=f"daily_bar:{bar.id}" if bar else None,
        affected_capabilities=caps, metadata={"adjustment_basis": "unknown", "source": bar.source if bar else None,
            "historical_publication_verified": False}))
    config, config_error = _safe_read("scoring_configs", lambda: _execute_select(db, select(ScoringConfig).where(
        ScoringConfig.asset_type == symbol.asset_type, ScoringConfig.is_active == 1).order_by(
            ScoringConfig.id.desc()).limit(1)).scalars().first())
    config_json, config_parse_error = None, False
    if config:
        try:
            config_json = _object(config.config_json)
            dimensions = config_json.get("dimensions")
            if not isinstance(dimensions, list) or not dimensions:
                raise ValueError("configuration_dimensions_missing")
            for dim in dimensions:
                if (not isinstance(dim, dict) or not isinstance(dim.get("factors", []), list)
                        or any(not isinstance(factor, dict) for factor in dim.get("factors", []))):
                    raise ValueError("configuration_dimensions_invalid")
        except (TypeError, ValueError):
            config_parse_error = True
            config_json = None
    scoring = {"expected_model_run_id": binding["model_run_id"], "expected_factor_set_id": binding["factor_set_id"],
        "current_config_id": config.id if config else None, "current_config_name": config.name if config else None,
        "current_config_version": config.version if config else None,
        "current_config": config_json, "configuration_source": "current_asset_scoring_runtime",
        "current_config_valid": bool(config_json and not config_parse_error and not config_error),
        "portfolio_config_frozen": False}
    ev.append(_evidence(at, evidence_id=f"{sid}:config", check_code="SCORING_CONFIGURATION", label="当前规则评分配置",
        status="READ_ERROR" if config_error else ("UNKNOWN" if config_parse_error else ("PASSED" if config else "MISSING")),
        reason=config_error or ("评分配置无法解析" if config_parse_error else ("当前资产类型已激活的规则评分配置；未冒充组合冻结配置" if config else "没有找到已激活的规则评分配置")),
        symbol=symbol, source_code="scoring_configs", source_label="规则评分配置",
        value={"id": config.id, "name": config.name, "version": config.version} if config else None,
        config_id=config.id if config else None, config_version=config.version if config else None,
        source_record_ref=f"scoring_config:{config.id}" if config else None, affected_capabilities=caps))
    def score_query(matching: bool):
        stmt = select(Score).where(Score.symbol_id == sid)
        if matching:
            if binding["model_run_id"]:
                stmt = stmt.where(Score.factor_model_run_id == binding["model_run_id"],
                    Score.factor_set_id == binding["factor_set_id"], Score.weight_mode == "ridge")
            else:
                stmt = stmt.where(Score.weight_mode == "manual", Score.factor_model_run_id.is_(None))
                if config:
                    stmt = stmt.where(Score.scoring_config_id == config.id, Score.scoring_config_version == config.version)
        return _execute_select(db, stmt.order_by(Score.trade_date.desc(), Score.id.desc()).limit(1)).scalars().first()
    score, score_error = _safe_read("matching_scores", lambda: score_query(True))
    latest = score
    if score is None and not score_error:
        latest, score_error = _safe_read("latest_scores", lambda: score_query(False))
    status, reason, lag = _freshness(score.trade_date if score else None, calendar,
        binding["score_allowed_lag"], score.published_at if score else None, at)
    if score_error:
        status, reason = "READ_ERROR", score_error
    elif score is None and latest:
        status, reason = "VERSION_MISMATCH", "存在评分，但实际模型、因子集或规则评分配置与本次检查范围不一致"
    elif binding.get("read_error") or binding.get("not_effective"):
        status, reason = "UNKNOWN", "组合绑定无法读取或尚未生效，不能确认评分适用性"
    elif not binding["model_run_id"] and not config:
        status, reason = "UNKNOWN", "缺少可核验评分配置，不能用任意最新评分判定通过"
    if latest:
        try:
            actual_config = _object(latest.scoring_config_snapshot_json)
        except (TypeError, ValueError):
            actual_config = None
        if score and not binding["model_run_id"] and config_json and actual_config != config_json:
            status, reason = "VERSION_MISMATCH", "评分保存的实际配置快照与当前配置内容不一致"
        if score and score.factor_data_cutoff_at and _utc(score.factor_data_cutoff_at) > at:
            status, reason = "FUTURE_DATA", "评分采用的因子数据截止时间晚于检查时点"
        try:
            source_dates = _object(score.factor_scores_json).get("_data_freshness", {}) if score else {}
            source_date = date.fromisoformat(source_dates["latest_bar_trade_date"]) if source_dates.get("latest_bar_trade_date") else None
            if source_date and score and source_date < score.trade_date:
                status, reason = "STALE", "评分业务日期较新，但其实际采用的日K输入仍停在更早日期"
            elif source_date and score and source_date > score.trade_date:
                status, reason = "FUTURE_DATA", "评分采用了晚于评分业务日期的日K输入"
        except (TypeError, ValueError, AttributeError):
            status, reason = "UNKNOWN", "评分的实际行情输入日期记录无法解析"
        scoring.update({"actual_model_run_id": latest.factor_model_run_id, "actual_factor_set_id": latest.factor_set_id,
            "actual_weight_mode": latest.weight_mode, "actual_config_id": latest.scoring_config_id,
            "actual_config_version": latest.scoring_config_version, "actual_config_name": latest.scoring_preset_name,
            "actual_config_snapshot": actual_config, "score_record_id": latest.id})
    ev.append(_evidence(at, evidence_id=f"{sid}:score", check_code="SCORE_FRESHNESS", label="适用评分",
        status=status, reason=reason, symbol=symbol, source_code="scores", source_label="评分记录",
        value=latest.trade_date if latest else None, expected=expected, actual_trade_date=latest.trade_date if latest else None,
        expected_trade_date=expected, trading_day_lag=lag, allowed_lag=binding["score_allowed_lag"],
        available_at=latest.published_at if latest else None, availability_basis="published_at",
        model_run_id=latest.factor_model_run_id if latest else None, config_id=latest.scoring_config_id if latest else None,
        config_version=latest.scoring_config_version if latest else None,
        source_record_ref=f"score:{latest.id}" if latest else None, affected_capabilities=caps,
        depends_on=[f"{sid}:daily_bar", f"{sid}:config"],
        metadata={"expected_model_run_id": binding["model_run_id"], "pit_safety": latest.pit_safety if latest else None,
            "factor_data_cutoff_at": latest.factor_data_cutoff_at.isoformat() if latest and latest.factor_data_cutoff_at else None}))
    if binding["model_run_id"]:
        for dependency in deps["members"]:
            fv_data, error = _safe_read("factor_values", lambda dep=dependency: read_inspection_factor_value(
                db, sid, dep["factor_id"], current_read=bool(db.info.get(_CURRENT_READ_KEY))))
            from types import SimpleNamespace
            fv = SimpleNamespace(**fv_data) if fv_data else None
            freq = (dependency["frequency"] or "").lower()
            status, reason, lag = _freshness(fv.trade_date if fv else None, calendar, 0,
                fv.created_at if fv else None, at)
            if error:
                status, reason = "READ_ERROR", error
            elif not dependency["version_valid"]:
                status, reason = "VERSION_MISMATCH", "冻结因子版本不存在或未通过校验"
            elif fv and fv.raw_value is None and fv.normalized_value is None:
                status, reason = "MISSING", "指标记录存在，但原始值和标准化值均缺失"
            elif fv and freq not in {"d", "1d", "daily", "day"}:
                status, reason, lag = "UNKNOWN", "该指标未配置可核验发布频率与到期策略，未套用日K时限", None
            elif fv and status == "PASSED":
                status, reason = "UNKNOWN", "已找到指标值，但存储未记录计算采用的因子版本，无法核验冻结版本"
            ev.append(_evidence(at, evidence_id=f"{sid}:factor:{dependency['code']}", check_code="FACTOR_INPUT",
                label=dependency["name"], status=status, reason=reason, symbol=symbol, source_code="factor_values",
                source_label="指标计算记录", value=fv.raw_value if fv else None,
                expected={"version": dependency["version"], "frequency": dependency["frequency"]},
                actual_trade_date=fv.trade_date if fv else None, expected_trade_date=expected,
                trading_day_lag=lag, allowed_lag=0 if freq in {"d", "1d", "daily", "day"} else None,
                available_at=fv.created_at if fv else None, availability_basis="ingested_at_only",
                source_record_ref=f"factor_value:{fv.id}" if fv else None, affected_capabilities=caps,
                depends_on=[f"{sid}:daily_bar"], metadata=dependency))
    elif config_json:
        factor_details = {}
        if score:
            try:
                factor_details = _object(score.factor_scores_json)
            except (TypeError, ValueError):
                pass
        seen = set()
        raw_field_evidence = set()
        for dimension in config_json.get("dimensions", []):
            if not dimension.get("enabled", True):
                continue
            for factor in dimension.get("factors", []):
                key = factor.get("key")
                if not key or key in seen or not factor.get("enabled", True):
                    continue
                seen.add(key)
                supported = factor.get("source", "builtin") in {"builtin", "daily_bar"}
                value = getattr(score, key, None) if score else None
                detail = factor_details.get(key)
                if value is None and isinstance(detail, dict):
                    value = detail.get("normalized_value")
                ind_status = "MISSING" if value is None else ev[2]["status"]
                ind_reason = "适用评分未保存该指标产物" if value is None else "已从实际评分产物读取；时效随该评分业务日核验"
                if not supported:
                    ind_status, ind_reason = "UNKNOWN", "该外部指标的发布频率与原始数据血缘尚未接入本检查器，请在数据中心核验"
                required_field = {"amount_activity": "amount", "liquidity_score": "amount",
                    "turnover_activity": "turnover_rate"}.get(key)
                dependencies = [f"{sid}:daily_bar", f"{sid}:score"]
                if required_field:
                    raw_id = f"{sid}:bar_input:{required_field}"
                    dependencies.append(raw_id)
                    missing_input = bar is None or getattr(bar, required_field, None) is None
                    if raw_id not in raw_field_evidence:
                        raw_field_evidence.add(raw_id)
                        raw_status = "READ_ERROR" if ev[0]["status"] == "READ_ERROR" else ("MISSING" if missing_input else ev[0]["status"])
                        ev.append(_evidence(at, evidence_id=raw_id, check_code="RAW_BAR_INPUT",
                            label={"amount": "行情成交金额", "turnover_rate": "行情换手率"}[required_field],
                            status=raw_status, reason="指标依赖的原始行情字段缺失，不能用默认中性值表示已完成" if missing_input else "已读取原始行情字段；时效随日K业务日期核验",
                            symbol=symbol, source_code="daily_bars", source_label="日K行情",
                            value=getattr(bar, required_field, None) if bar else None,
                            expected="已发布的原始行情数值", actual_trade_date=bar.trade_date if bar else None,
                            expected_trade_date=expected, source_record_ref=f"daily_bar:{bar.id}" if bar else None,
                            affected_capabilities=caps, metadata={"field": required_field}))
                    if missing_input:
                        ind_status, ind_reason = "MISSING", "指标依赖的原始行情字段缺失，评分中的替代数值不能证明输入完整"
                ev.append(_evidence(at, evidence_id=f"{sid}:indicator:{key}", check_code="RULE_INDICATOR",
                    label=INDICATOR_LABELS.get(key) or f"{dimension.get('name', '规则')}指标",
                    status=ind_status, reason=ind_reason, symbol=symbol, source_code="scores", source_label="评分指标产物",
                    value=value, expected="当前配置启用的指标产物", actual_trade_date=score.trade_date if score else None,
                    expected_trade_date=expected, config_id=config.id, config_version=config.version,
                    source_record_ref=f"score:{score.id}" if score else None, affected_capabilities=caps,
                    depends_on=dependencies,
                    metadata={"indicator_key": key, "source": factor.get("source", "builtin"),
                        "missing_policy": factor.get("missing_policy", "neutral"), "raw_input_verified": False}))
    if calendar["not_due"]:
        ev.append(_evidence(at, evidence_id=f"{sid}:await_close", check_code="CURRENT_BAR_AVAILABILITY", label="当日收盘数据",
            status="NOT_DUE", reason="尚未到15:00收盘时点；当日数据不作为已到期缺口", symbol=symbol,
            expected=at.astimezone(CN_ZONE).date(), source_code="trade_calendar", source_label="交易所日历"))
    for version_id in ref.get("member_rule_versions", []):
        ev.append(_member_rule_evidence(at, symbol, version_id, binding, caps))
    return {**ref, "symbol": symbol.symbol, "name": symbol.name, "market": symbol.market,
        "asset_type": symbol.asset_type, "expected_trade_date": expected,
        "actual_bar_trade_date": bar.trade_date if bar else None,
        "actual_score_trade_date": latest.trade_date if latest else None, "scoring": scoring, "evidence": ev}


def _repair_operations(symbols: list[dict], evidence: list[dict], binding: dict) -> list[dict]:
    operations = []
    for row in symbols:
        sid, expected = row["symbol_id"], row["expected_trade_date"]
        bad = [ev for ev in row["evidence"] if ev["status"] in BAD_STATUSES]
        bars_bad = [ev for ev in bad if ev["check_code"] in {"DAILY_BAR_FRESHNESS", "RAW_BAR_INPUT"}]
        base = {"symbol_id": sid, "symbol": row["symbol"], "trade_date": expected,
            "end_date": expected, "depends_on": []}
        if bars_bad:
            actual = row["actual_bar_trade_date"]
            operations.append({**base, "operation_id": f"{sid}:sync_daily_bars", "kind": "sync_daily_bars",
                "label": f"补齐 {row['name']} 日K行情", "start_date": (actual + timedelta(days=1)) if actual and expected and actual < expected else expected,
                "enabled": bool(expected and bars_bad[0]["status"] in {"MISSING", "STALE"}),
                "disabled_code": None if expected and bars_bad[0]["status"] in {"MISSING", "STALE"} else "DATE_OR_SOURCE_UNVERIFIED",
                "disabled_reason": None if expected and bars_bad[0]["status"] in {"MISSING", "STALE"} else "先核验交易日历、未来数据或读取异常，再生成准确补数范围",
                "target": "settings:data-center", "resolves_evidence_ids": [e["evidence_id"] for e in bars_bad],
                "expected_effect": "仅同步明确日期范围内的日K行情，成功后重检实际覆盖日"})
        score_bad = [ev for ev in bad if ev["check_code"] in {"SCORE_FRESHNESS", "RULE_INDICATOR", "FACTOR_INPUT"}]
        if score_bad:
            dynamic = bool(binding["model_run_id"])
            config = row["scoring"]
            external_missing = any(e["status"] == "UNKNOWN" and e["check_code"] == "RULE_INDICATOR" for e in score_bad)
            enabled = bool(expected and config.get("current_config_valid") and not dynamic and not external_missing
                and not binding.get("read_error") and not binding.get("not_effective"))
            code = None if enabled else ("MODEL_SCORING_REQUIRES_FROZEN_PIPELINE" if dynamic else "SCORING_INPUT_UNVERIFIED")
            operations.append({**base, "operation_id": f"{sid}:recalculate_score",
                "kind": "recalculate_model_score" if dynamic else "recalculate_rule_score",
                "label": f"重算 {row['name']} 指标与评分", "config_id": config.get("current_config_id"),
                "config_version": config.get("current_config_version"),
                "config_fingerprint": _hash(config["current_config"]) if config.get("current_config_valid") else None,
                "enabled": enabled,
                "disabled_code": code, "disabled_reason": None if enabled else (
                    "现有模型评分任务不能安全固定本组合模型、因子版本及标的范围；请到因子模型核验并运行正式评分" if dynamic else "评分配置、外部指标或应到日期尚未核验，不能安全重算"),
                "target": "scoring:models" if dynamic else "settings:data-center",
                "depends_on": [f"{sid}:sync_daily_bars"] if bars_bad else [],
                "resolves_evidence_ids": [e["evidence_id"] for e in score_bad],
                "expected_effect": "使用预览中固定的规则评分配置重算指标与评分；完成不等于自动恢复交易"})
    if any(ev["status"] in {"READ_ERROR", "CALENDAR_UNKNOWN", "UNKNOWN", "VERSION_MISMATCH"} for ev in evidence):
        operations.append({"operation_id": "review:evidence", "kind": "review_evidence", "label": "核验日历、绑定与未知来源",
            "enabled": False, "disabled_code": "MANUAL_EVIDENCE_REVIEW_REQUIRED",
            "disabled_reason": "来源与版本未知需要先核验，当前没有可安全自动修复的业务入口",
            "target": "settings:data-center", "expected_effect": "核验来源后重新检查，不跳过或清零未知项",
            "resolves_evidence_ids": [e["evidence_id"] for e in evidence if e["status"] in BAD_STATUSES]})
    return operations


def build_portfolio_inspection(db: Session, portfolio_id: int, *, now: datetime | None = None, current_read: bool = False) -> dict:
    """Read evidence; final confirmations opt into locking MySQL current reads.

    The caller owns the transaction and must commit/rollback promptly after a
    current read. Ordinary GETs keep their existing non-locking, no-flush path.
    """
    with db.no_autoflush, _read_mode(db, current_read):
        return _build_portfolio_inspection(db, portfolio_id, now=now)


def _build_portfolio_inspection(db: Session, portfolio_id: int, *, now: datetime | None = None) -> dict:
    at = _utc(now or datetime.now(timezone.utc)).replace(microsecond=0)
    with db.no_autoflush:
        portfolio = _get_record(db, Portfolio, portfolio_id)
        if portfolio is None:
            raise LookupError("Portfolio not found")
        refs, global_evidence = _scope(db, portfolio, at)
        binding, binding_evidence, deps = _binding(db, portfolio_id, at)
        global_evidence.extend(binding_evidence)
        symbol_ids = [ref["symbol_id"] for ref in refs]
        rows, error = _safe_read("symbols", lambda: _execute_select(db, select(Symbol).where(Symbol.id.in_(symbol_ids)).order_by(Symbol.id)).scalars().all())
        if error:
            global_evidence.append(_evidence(at, evidence_id="scope:symbols", check_code="SYMBOL_READ", label="标的基本资料",
                status="READ_ERROR", reason=error, source_code="symbols", source_label="标的基本资料"))
        by_id = {symbol.id: symbol for symbol in (rows or [])}
        calendars = {market: _calendar(db, market, at) for market in sorted({symbol.market for symbol in by_id.values()})}
        symbols = []
        for ref in sorted(refs, key=lambda item: item["symbol_id"]):
            symbol = by_id.get(ref["symbol_id"])
            if symbol is None:
                global_evidence.append(_evidence(at, evidence_id=f"scope:missing_symbol:{ref['symbol_id']}",
                    check_code="SYMBOL_READ", label="标的基本资料", status="MISSING", reason="范围引用的标的基本资料不存在",
                    source_code="symbols", source_label="标的基本资料", value=ref["symbol_id"]))
                continue
            ref["entry_authorized"] = ref["entry_authorized"] and allows_asset_type(portfolio.asset_scope, symbol.asset_type)
            symbols.append(_symbol_diagnostics(db, ref, symbol, calendars[symbol.market], binding, deps, at))
        for market, cal in calendars.items():
            if cal["status"] != "PASSED":
                global_evidence.append(_evidence(at, evidence_id=f"calendar:{market}", check_code="EXPECTED_TRADE_DATE", label=f"{market}应到数据日",
                    status=cal["status"], reason=cal["reason"], source_code="trade_calendar", source_label="交易所日历",
                    affected_capabilities=["new_buy", "reconciliation_progress", "rollout_admission"]))
        all_evidence = global_evidence + [item for row in symbols for item in row["evidence"]]
        # Fingerprint only evidence facts; reading the same facts later does not
        # manufacture a new issue or invalidate a short-lived repair preview.
        fingerprint = {"policy": POLICY_VERSION, "portfolio_id": portfolio_id, "binding": binding,
            "symbols": [{**row, "evidence": [{k: v for k, v in ev.items() if k != "observed_at"} for ev in row["evidence"]]} for row in symbols],
            "global": [{k: v for k, v in ev.items() if k != "observed_at"} for ev in global_evidence],
            "calendars": [{key: value for key, value in cal.items() if key != "synced_at"} for cal in calendars.values()]}
        snapshot_id = _hash(fingerprint)
        operations = _repair_operations(symbols, global_evidence, binding)
        limits = ["日K与指标记录缺少独立发布时间或复权口径，当前结果不能替代历史PIT准入检查",
            "诊断不修改交易状态、对账游标、订单、成交或现金账本",
            "未接通的外部指标与冻结模型批量评分明确保留未知，需要在原业务入口核验"]
        plan = {"plan_id": _hash({"snapshot_id": snapshot_id, "operations": operations}),
            "snapshot_id": snapshot_id, "portfolio_id": portfolio_id, "generated_at": at,
            "expires_at": at + timedelta(minutes=15), "executable_count": sum(op["enabled"] for op in operations),
            "disabled_count": sum(not op["enabled"] for op in operations), "operations": operations,
            "completion_criteria": ["作业逐对象产物已保存", "重检应到业务日与实际配置一致", "未知或失败项仍保留，不能因任务完成自动放行"],
            "limitations": limits}
        counts = {status.lower(): sum(ev["status"] == status for ev in all_evidence) for status in (
            "PASSED", "MISSING", "STALE", "READ_ERROR", "VERSION_MISMATCH", "FUTURE_DATA", "NOT_DUE", "CALENDAR_UNKNOWN", "UNKNOWN")}
        summary = {**counts, "symbol_count": len(refs), "inspected_symbol_count": len(symbols),
            "admission_failure_count": len(inspection_admission_failures(all_evidence)),
            "affected_symbol_count": sum(any(e["status"] in BAD_STATUSES for e in row["evidence"]) for row in symbols),
            "passed_symbol_count": sum(all(e["status"] not in BAD_STATUSES for e in row["evidence"]) for row in symbols),
            "aggregation_rule": "逐项归集全部异常；持仓数据缺口不阻止风险退出；组合实际执行准入仍由执行门禁判断",
            "affected_capabilities": sorted({cap for ev in all_evidence if ev["status"] in BAD_STATUSES for cap in ev.get("affected_capabilities", [])})}
        return PortfolioInspectionResponse.model_validate({"portfolio_id": portfolio_id, "snapshot_id": snapshot_id,
            "generated_at": at, "policy_version": POLICY_VERSION, "summary": summary, "binding": binding,
            "calendars": [{k: v for k, v in cal.items() if k != "trading_dates"} for cal in calendars.values()],
            "symbols": symbols, "evidence": global_evidence, "repair_plan": plan, "limitations": limits}).model_dump(mode="json")
