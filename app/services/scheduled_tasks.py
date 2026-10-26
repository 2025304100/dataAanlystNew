"""Persistent cross-platform scheduler and task dispatcher."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import delete, desc, func, select, update
from sqlalchemy.orm import Session
from app.core.market_event_policy import MARKET_EVENT_KEEP_DAYS_DEFAULT, effective_keep_days

from app.db.session import get_session_local
from app.models.async_task import AsyncTaskRecord
from app.models.discovery import DiscoveryTaskRecord
from app.models.scheduled_task import (
    ScheduledTask,
    ScheduledTaskRun,
    ScheduledTaskSeedState,
)
from app.schemas.scheduled_task import ScheduledTaskCreate, ScheduledTaskUpdate
from app.services.feature_policy import (
    TAIL_PROXY_DISABLED_REASON, is_tail_proxy_schedule, reject_tail_proxy,
)


logger = logging.getLogger(__name__)
SCHEDULER_CHECK_INTERVAL_SECONDS = 30
# 派发失败后的短延迟重试：撞上前台行情同步这类瞬时冲突时，不该把一整天算成已跑。
DISPATCH_RETRY_DELAY_MINUTES = 15
DISPATCH_MAX_RETRIES_PER_DAY = 6
# v10：新增"每日行情消息采集"与"每日行情消息过期清理"两条默认排程（行情消息 P0-1/P0-2）。
# 升 key 触发重播种：按 name 去重，既有部署的历史排程与用户自建计划原样保留。
DEFAULT_SCHEDULE_SEED_KEY = "default_schedules_v10"
RECONCILIATION_SEED_KEY = "portfolio_reconciliation_schedule_v1"

TASK_DEFINITIONS: dict[str, dict] = {
    "portfolio_reconciliation": {
        "name": "组合自动对账",
        "description": "只核对到期业务日的计划、执行与账户证据，按连续覆盖范围推进；不生成交易或补造历史记录",
        "default_payload": {},
    },
    "universe_incremental_sync": {
        "name": "行情增量同步",
        "description": "增量同步基础股票池中已过期的日线行情",
        "default_payload": {
            "max_workers": 5,
            "scopes": ["cn-stock", "cn-etf", "us-stock", "us-etf"],
        },
    },
    "macro_update": {
        "name": "宏观数据更新",
        "description": "更新中美宏观指标、利率和流动性数据",
        "default_payload": {"region": "all"},
    },
    "factor_grade_review": {
        "name": "因子等级季度重评",
        "description": (
            "按需求 §6.8 季度重评因子质量等级：B 级连续 2 季稳定升 A；"
            "S/A 级连续 2 季下滑（ICIR 降幅>30% 或 OOS 反转）降级并移出 FactorSet；"
            "人工调整过的等级（grade_manual_adjusted=1）不覆盖"
        ),
        "default_payload": {"actor": "scheduler"},
    },
    "factor_pipeline": {
        "name": "因子流水线",
        "description": "运行本地镜像、因子计算、标签、模型和评分快照",
        "default_payload": {
            "full_refresh": False,
            "train_model": False,
            "materialize_scores": True,
            "window_days": 250,
            "validation_days": 50,
        },
    },
    "hot_rank_snapshot": {
        "name": "东方财富人气榜快照",
        "description": "保存当前前100名人气榜，免费接口不支持历史回填",
        "default_payload": {},
    },
    "lhb_institution_sync": {
        "name": "龙虎榜机构席位同步",
        "description": "同步最近3日市场级机构席位买入、卖出和净额",
        "default_payload": {"lookback_days": 3},
    },
    "financial_report_sync": {
        "name": "财报历史同步",
        "description": "按公告日同步自选股财务分析历史，默认限制20只",
        "default_payload": {"source": "watchlist", "limit": 20},
    },
    "discovery_mining": {
        "name": "机会挖掘",
        "description": "使用本地股票池运行机会扫描和候选生成",
        "default_payload": {
            "scope": "cn-stock",
            "min_score": 55,
            "include_news": True,
            "batch_size": 20,
            "max_workers": 1,
            "use_cached_bars_first": True,
            "use_cached_symbols_only": True,
        },
    },
    # P0-9：组合每日净值快照（绩效统计基础数据）
    "portfolio_equity_snapshot": {
        "name": "组合净值快照",
        "description": "每日收盘后写入所有模拟组合的净值快照（绩效统计基础数据，历史不可回溯）",
        "default_payload": {},
    },
    # P3+：市场指数日线同步（Benchmark 对比曲线数据源）
    "index_daily_sync": {
        "name": "指数日线同步",
        "description": "同步沪深300等指数日线到 index_prices 表，供组合绩效 benchmark 对比曲线使用",
        "default_payload": {"symbol": "000300", "lookback_days": 5},
    },
    "external_data_sync": {
        "name": "因子输入增量同步",
        "description": "按 payload.dataset 每天抓取全市场 A 股最新 PE/PB/市值或当日主力资金流快照，供因子公式使用",
        "default_payload": {
            "dataset": "fundamental",
            "source": "all",
            "include_northbound": False,
            "mode": "incremental",
            "lookback_days": 1,
        },
    },
    # P2-3：自动交易执行（基于评分+候选池+风控自动触发模拟买卖）
    "portfolio_auto_trade": {
        "name": "组合自动交易",
        "description": "默认每日 20:35 在策略决策时点后评估已开启接管的模拟组合，按不可变计划在下一交易日撮合",
        "default_payload": {"dry_run": False, "buy_candidate_limit": 10},
    },
    "alert_evaluation": {
        "name": "告警规则评估",
        "description": (
            "评估所有启用的告警规则（评分下降 / 数据过期 / 任务失败），"
            "生成正式告警事件并按通知策略投递；与告警中心的「立即评估」同一条代码路径"
        ),
        "default_payload": {},
    },
    # 行情消息 P0-2：市场事件定时采集（原先只能前端手动触发，且默认只采 2/6 源）
    "market_events_collect": {
        "name": "行情消息采集",
        "description": "从东财全球/财新/央视/百度经济/百度研报/上海金属网采集市场级事件并打标入库",
        "default_payload": {
            "days": 7,
            "sources": [
                "eastmoney-global", "caixin", "cctv", "baidu", "baidu-report", "futures-shmet",
            ],
        },
    },
    # 行情消息：过期消息清理（保护至少 365 天热点历史；
    # 被主题催化剂引用的行与手工行不删）
    "market_events_cleanup": {
        "name": "行情消息过期清理",
        "description": "删除已过期且发布时间超出保留窗口（至少365天）的自动采集消息；保护主题催化剂引用与手工录入",
        "default_payload": {"keep_days": MARKET_EVENT_KEEP_DAYS_DEFAULT},
    },
}

DEFAULT_SCHEDULES = (
    {
        "name": "每日行情增量同步",
        "task_type": "universe_incremental_sync",
        "frequency": "daily",
        "time_of_day": "18:00",
        "weekdays": [],
        "payload": TASK_DEFINITIONS["universe_incremental_sync"]["default_payload"],
        "enabled": True,
    },
    {
        "name": "每日因子估值增量同步",
        "task_type": "external_data_sync",
        "frequency": "daily",
        "time_of_day": "20:30",
        "weekdays": [],
        "payload": TASK_DEFINITIONS["external_data_sync"]["default_payload"],
        "enabled": True,
    },
    # 资金流接口是逐标的的，日更必须走全市场排行快照（见 sync_market_capital_flow_snapshot），
    # 排在估值任务之后，避免两个全市场抓取抢同一时段。
    # 两个时段都在 18:00 行情增量同步（实测 2.45 小时）之后：抢在前面只会被
    # `_wait_for_market_priority` 挡下，而挡下的那一天不会自动补。
    {
        "name": "每日因子资金流增量同步",
        "task_type": "external_data_sync",
        "frequency": "daily",
        "time_of_day": "20:40",
        "weekdays": [],
        "payload": {
            "dataset": "capital_flow",
            "source": "all",
            "include_northbound": False,
            "mode": "incremental",
            "lookback_days": 1,
        },
        "enabled": True,
    },
    {
        "name": "每日 ETF 指标快照",
        "task_type": "external_data_sync",
        "frequency": "daily",
        "time_of_day": "16:40",
        "weekdays": [],
        "payload": {
            "dataset": "etf",
            "source": "all",
            "include_northbound": False,
            "mode": "incremental",
            "lookback_days": 1,
        },
        "enabled": False,
    },
    {
        "name": "每日人气榜快照",
        "task_type": "hot_rank_snapshot",
        "frequency": "daily",
        "time_of_day": "16:20",
        "weekdays": [],
        "payload": TASK_DEFINITIONS["hot_rank_snapshot"]["default_payload"],
        "enabled": False,
    },
    {
        "name": "每日龙虎榜机构同步",
        "task_type": "lhb_institution_sync",
        "frequency": "daily",
        "time_of_day": "16:30",
        "weekdays": [],
        "payload": TASK_DEFINITIONS["lhb_institution_sync"]["default_payload"],
        "enabled": False,
    },
    {
        "name": "每周财报历史同步",
        "task_type": "financial_report_sync",
        "frequency": "weekly",
        "time_of_day": "10:00",
        "weekdays": [5],
        "payload": TASK_DEFINITIONS["financial_report_sync"]["default_payload"],
        "enabled": False,
    },
    {
        "name": "每日宏观数据更新",
        "task_type": "macro_update",
        "frequency": "daily",
        "time_of_day": "17:30",
        "weekdays": [],
        "payload": TASK_DEFINITIONS["macro_update"]["default_payload"],
        "enabled": False,
    },
    {
        "name": "每日因子评分",
        "task_type": "factor_pipeline",
        "frequency": "daily",
        "time_of_day": "18:10",
        "weekdays": [],
        "payload": TASK_DEFINITIONS["factor_pipeline"]["default_payload"],
        "enabled": False,
    },
    {
        "name": "每周因子训练",
        "task_type": "factor_pipeline",
        "frequency": "weekly",
        "time_of_day": "18:30",
        "weekdays": [4],
        "payload": {
            **TASK_DEFINITIONS["factor_pipeline"]["default_payload"],
            "train_model": True,
        },
        "enabled": False,
    },
    {
        "name": "每日机会挖掘",
        "task_type": "discovery_mining",
        "frequency": "daily",
        "time_of_day": "18:40",
        "weekdays": [],
        "payload": TASK_DEFINITIONS["discovery_mining"]["default_payload"],
        "enabled": False,
    },
    # P0-9：每日组合净值快照（A 股 15:00 收盘，16:00 写入避开行情同步高峰）
    {
        "name": "每日组合净值快照",
        "task_type": "portfolio_equity_snapshot",
        "frequency": "daily",
        "time_of_day": "16:00",
        "weekdays": [],
        "payload": TASK_DEFINITIONS["portfolio_equity_snapshot"]["default_payload"],
        "enabled": True,
    },
    # P3+：每日指数日线同步（16:05 在组合快照之后，确保 benchmark 数据就绪）
    {
        "name": "每日指数日线同步",
        "task_type": "index_daily_sync",
        "frequency": "daily",
        "time_of_day": "16:05",
        "weekdays": [],
        "payload": TASK_DEFINITIONS["index_daily_sync"]["default_payload"],
        "enabled": True,
    },
    # Default DecisionEngine clock: data cutoff 20:00, decision 20:30.
    # Existing schedules are retained for explicit review, never rewritten by seeding.
    {
        "name": "每日组合自动交易",
        "task_type": "portfolio_auto_trade",
        "frequency": "daily",
        "time_of_day": "20:35",
        "weekdays": [],
        "payload": TASK_DEFINITIONS["portfolio_auto_trade"]["default_payload"],
        "enabled": False,
    },
    # 排在当日同步/评分任务之后：任务失败的告警依赖当天的异步任务账目。
    {
        "name": "每日告警规则评估",
        "task_type": "alert_evaluation",
        "frequency": "daily",
        "time_of_day": "21:30",
        "weekdays": [],
        "payload": TASK_DEFINITIONS["alert_evaluation"]["default_payload"],
        "enabled": True,
    },
    # 行情消息 P0-2：盘前采集当日资讯（08:10 早于 09:30 开盘，供今日决策引用）
    {
        "name": "每日行情消息采集",
        "task_type": "market_events_collect",
        "frequency": "daily",
        "time_of_day": "08:10",
        "weekdays": [],
        "payload": TASK_DEFINITIONS["market_events_collect"]["default_payload"],
        "enabled": True,
    },
    # 凌晨低峰清理，保护 365 天热点历史与催化剂引用行。
    {
        "name": "每日行情消息过期清理",
        "task_type": "market_events_cleanup",
        "frequency": "daily",
        "time_of_day": "02:30",
        "weekdays": [],
        "payload": TASK_DEFINITIONS["market_events_cleanup"]["default_payload"],
        "enabled": True,
    },
)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _json(value: str | None, fallback):
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return fallback


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown timezone: {name}") from exc


def calculate_next_run(
    *,
    frequency: str,
    time_of_day: str | None,
    weekdays: list[int],
    interval_minutes: int | None,
    timezone_name: str,
    after_utc: datetime | None = None,
) -> datetime:
    """Return the next run as a naive UTC datetime."""
    after = after_utc or _now()
    if after.tzinfo is None:
        after = after.replace(tzinfo=timezone.utc)
    else:
        after = after.astimezone(timezone.utc)
    zone = _zone(timezone_name)
    local_after = after.astimezone(zone)

    if frequency == "interval":
        if not interval_minutes:
            raise ValueError("interval_minutes is required")
        candidate = after + timedelta(minutes=interval_minutes)
        return candidate.astimezone(timezone.utc).replace(tzinfo=None)

    if not time_of_day:
        raise ValueError("time_of_day is required")
    hour, minute = (int(item) for item in time_of_day.split(":"))
    run_time = time(hour=hour, minute=minute)

    if frequency == "daily":
        candidate = datetime.combine(local_after.date(), run_time, tzinfo=zone)
        if candidate <= local_after:
            candidate += timedelta(days=1)
    elif frequency == "weekly":
        if not weekdays:
            raise ValueError("weekdays is required")
        candidate = None
        for offset in range(0, 8):
            day = local_after.date() + timedelta(days=offset)
            if day.weekday() not in weekdays:
                continue
            possible = datetime.combine(day, run_time, tzinfo=zone)
            if possible > local_after:
                candidate = possible
                break
        if candidate is None:
            raise ValueError("Unable to calculate weekly schedule")
    else:
        raise ValueError(f"Unsupported frequency: {frequency}")
    return candidate.astimezone(timezone.utc).replace(tzinfo=None)


def _next_for_model(item: ScheduledTask, *, after: datetime | None = None) -> datetime:
    return calculate_next_run(
        frequency=item.frequency,
        time_of_day=item.time_of_day,
        weekdays=_json(item.weekdays_json, []),
        interval_minutes=item.interval_minutes,
        timezone_name=item.timezone,
        after_utc=after,
    )


def _seed_reconciliation_schedule(db: Session) -> int:
    # Independent seed: existing user schedules and deletion choices are retained.
    if db.get(ScheduledTaskSeedState, RECONCILIATION_SEED_KEY) is not None:
        return 0
    existing = db.scalar(select(ScheduledTask.id).where(ScheduledTask.task_type == "portfolio_reconciliation").limit(1))
    created = 0
    if existing is None:
        item = ScheduledTask(name="组合自动对账", task_type="portfolio_reconciliation", frequency="interval",
            time_of_day=None, weekdays_json="[]", interval_minutes=5, timezone="Asia/Shanghai",
            payload_json="{}", enabled=1)
        item.next_run_at = _next_for_model(item)
        db.add(item)
        created = 1
    db.add(ScheduledTaskSeedState(key=RECONCILIATION_SEED_KEY))
    db.flush()
    return created


def seed_default_schedules(db: Session) -> int:
    reconciliation_created = _seed_reconciliation_schedule(db)
    disable_retired_schedules(db)
    if db.get(ScheduledTaskSeedState, DEFAULT_SCHEDULE_SEED_KEY) is not None:
        return reconciliation_created
    created = 0
    existing_names = set(db.execute(select(ScheduledTask.name)).scalars().all())
    for data in DEFAULT_SCHEDULES:
        if data["name"] in existing_names:
            continue
        payload = ScheduledTaskCreate(
            **data,
            timezone="Asia/Shanghai",
        )
        item = ScheduledTask(
            name=payload.name,
            task_type=payload.task_type,
            frequency=payload.frequency,
            time_of_day=payload.time_of_day,
            weekdays_json=json.dumps(payload.weekdays),
            interval_minutes=payload.interval_minutes,
            timezone=payload.timezone,
            payload_json=json.dumps(payload.payload, ensure_ascii=False),
            enabled=int(payload.enabled),
        )
        if payload.enabled:
            item.next_run_at = _next_for_model(item)
        db.add(item)
        created += 1
    db.add(ScheduledTaskSeedState(key=DEFAULT_SCHEDULE_SEED_KEY))
    db.flush()
    return created + reconciliation_created


def validate_definition(task_type: str) -> None:
    if task_type not in TASK_DEFINITIONS:
        raise ValueError(f"Unsupported scheduled task type: {task_type}")


def validate_task_payload(task_type: str, payload: dict) -> dict:
    """Validate task-specific settings when a schedule is saved."""
    if is_tail_proxy_schedule(task_type, payload):
        reject_tail_proxy()
    validate_definition(task_type)
    if task_type == "portfolio_reconciliation":
        if set(payload) - {"portfolio_id", "trade_date"}:
            raise ValueError("自动对账仅接受组合和所属交易日，不能附带执行交易参数")
        from datetime import date
        result = {}
        if payload.get("portfolio_id") is not None:
            pid = payload["portfolio_id"]
            if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
                raise ValueError("portfolio_id 必须为正整数")
            result["portfolio_id"] = pid
        if payload.get("trade_date") is not None:
            result["trade_date"] = date.fromisoformat(str(payload["trade_date"])).isoformat()
        return result
    if task_type == "factor_grade_review":
        from app.schemas.scheduled_task import FactorGradeReviewPayload

        return FactorGradeReviewPayload.model_validate(payload).model_dump()
    if task_type == "universe_incremental_sync":
        allowed_scopes = {"cn-stock", "cn-etf", "us-stock", "us-etf"}
        scopes = payload.get("scopes") or sorted(allowed_scopes)
        if not isinstance(scopes, list) or not scopes:
            raise ValueError("scopes must be a non-empty list")
        if any(item not in allowed_scopes for item in scopes):
            raise ValueError("scopes contains an unsupported universe")
        max_workers = int(payload.get("max_workers", 5))
        if not 1 <= max_workers <= 20:
            raise ValueError("max_workers must be between 1 and 20")
        return {"max_workers": max_workers, "scopes": scopes}
    if task_type == "macro_update":
        from app.schemas.macro import MacroUpdateRequest

        return MacroUpdateRequest.model_validate(payload).model_dump(mode="json")
    if task_type == "hot_rank_snapshot":
        if payload:
            raise ValueError("hot_rank_snapshot payload must be empty")
        return {}
    if task_type == "lhb_institution_sync":
        lookback_days = int(payload.get("lookback_days", 3))
        if not 1 <= lookback_days <= 31:
            raise ValueError(
                "lookback_days must be between 1 and 31"
            )
        return {"lookback_days": lookback_days}
    if task_type == "financial_report_sync":
        source = str(payload.get("source") or "watchlist")
        if source not in {"watchlist", "positions", "all"}:
            raise ValueError("unsupported financial report source")
        limit = int(payload.get("limit", 20))
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        return {"source": source, "limit": limit}
    if task_type == "factor_pipeline":
        from app.schemas.async_task import FactorPipelineCreate

        validated = FactorPipelineCreate.model_validate(payload)
        if validated.validation_days >= validated.window_days:
            raise ValueError("validation_days must be less than window_days")
        return validated.model_dump(mode="json")
    if task_type == "discovery_mining":
        from app.schemas.discovery import DiscoveryTaskCreate

        return DiscoveryTaskCreate.model_validate(payload).model_dump(mode="json")
    if task_type == "portfolio_equity_snapshot":
        # P0-9：组合净值快照无需 payload（遍历所有模拟组合），但允许空 dict
        if payload:
            raise ValueError("portfolio_equity_snapshot payload must be empty")
        return {}
    if task_type == "index_daily_sync":
        # P3+：指数日线同步 payload 必须含 symbol(str) 和可选 lookback_days(int, 1-365)
        symbol = str(payload.get("symbol", "000300")).strip()
        if not symbol:
            raise ValueError("symbol must be a non-empty string")
        lookback_days = int(payload.get("lookback_days", 5))
        if not 1 <= lookback_days <= 365:
            raise ValueError("lookback_days must be between 1 and 365")
        return {"symbol": symbol, "lookback_days": lookback_days}
    if task_type == "external_data_sync":
        dataset = str(payload.get("dataset") or "fundamental")
        if dataset not in {"fundamental", "capital_flow", "etf"}:
            raise ValueError(
                "scheduled external sync supports fundamental, capital_flow and etf only"
            )
        source = str(payload.get("source") or "all")
        if source not in {"watchlist", "positions", "all"}:
            raise ValueError("unsupported external sync source")
        mode = str(payload.get("mode") or "incremental")
        if mode != "incremental":
            raise ValueError("scheduled external sync must use incremental mode")
        lookback_days = int(payload.get("lookback_days", 1))
        if not 1 <= lookback_days <= 30:
            raise ValueError("lookback_days must be between 1 and 30")
        return {
            "dataset": dataset,
            "source": source,
            "include_northbound": bool(payload.get("include_northbound", False)),
            "mode": mode,
            "lookback_days": lookback_days,
        }
    if task_type == "portfolio_auto_trade":
        # P2-3：自动交易 payload 必须含 dry_run(bool) 和 buy_candidate_limit(int, 1-50)
        dry_run = bool(payload.get("dry_run", False))
        buy_candidate_limit = int(payload.get("buy_candidate_limit", 10))
        if not 1 <= buy_candidate_limit <= 50:
            raise ValueError("buy_candidate_limit must be between 1 and 50")
        return {"dry_run": dry_run, "buy_candidate_limit": buy_candidate_limit}
    if task_type == "alert_evaluation":
        # 评估对象就是"所有启用的规则"，没有可配范围：配了反而会和告警中心的
        # 「立即评估」走出两种结果，所以拒绝任何 payload。
        if payload:
            raise ValueError("alert_evaluation payload must be empty")
        return {}
    if task_type == "market_events_collect":
        from app.services.market_events import ALL_COLLECT_SOURCES

        days = int(payload.get("days", 7))
        if not 1 <= days <= 30:
            raise ValueError("days must be between 1 and 30")
        sources = payload.get("sources") or list(ALL_COLLECT_SOURCES)
        if not isinstance(sources, list) or not sources:
            raise ValueError("sources must be a non-empty list")
        unknown = [item for item in sources if item not in ALL_COLLECT_SOURCES]
        if unknown:
            raise ValueError(f"unknown market event sources: {unknown}")
        return {"days": days, "sources": sources}
    if task_type == "market_events_cleanup":
        keep_days = payload.get("keep_days", MARKET_EVENT_KEEP_DAYS_DEFAULT)
        return {"keep_days": effective_keep_days(keep_days)}
    raise ValueError(f"Unsupported scheduled task type: {task_type}")


def _assert_no_duplicate_schedule(
    db: Session,
    payload: ScheduledTaskCreate,
    *,
    exclude_schedule_id: int | None = None,
) -> None:
    # VIZ-0929-04：同一 task_type + 完全相同调度时间 + 同时区的启用计划只允许一份，
    # 否则同名任务每日重复执行（如两条"行情增量同步 每天18:00"）。
    if not payload.enabled:
        return
    schedule_key = (
        payload.frequency,
        payload.time_of_day,
        json.dumps(sorted(payload.weekdays)),
        payload.interval_minutes,
    )
    query = select(ScheduledTask).where(
        ScheduledTask.task_type == payload.task_type,
        ScheduledTask.timezone == payload.timezone,
        ScheduledTask.enabled == 1,
    )
    for row in db.execute(query).scalars().all():
        if exclude_schedule_id is not None and row.id == exclude_schedule_id:
            continue
        row_key = (row.frequency, row.time_of_day, row.weekdays_json, row.interval_minutes)
        if row_key == schedule_key:
            raise ValueError(
                f"已存在相同任务类型与调度时间的启用计划（{payload.task_type}），请勿重复创建"
            )


def create_schedule(db: Session, payload: ScheduledTaskCreate) -> ScheduledTask:
    validate_definition(payload.task_type)
    _zone(payload.timezone)
    task_payload = validate_task_payload(payload.task_type, payload.payload)
    _assert_no_duplicate_schedule(db, payload)
    item = ScheduledTask(
        name=payload.name,
        task_type=payload.task_type,
        frequency=payload.frequency,
        time_of_day=payload.time_of_day,
        weekdays_json=json.dumps(payload.weekdays),
        interval_minutes=payload.interval_minutes,
        timezone=payload.timezone,
        payload_json=json.dumps(task_payload, ensure_ascii=False, default=str),
        enabled=int(payload.enabled),
    )
    if payload.enabled:
        item.next_run_at = _next_for_model(item)
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def update_schedule(
    db: Session,
    schedule_id: int,
    patch: ScheduledTaskUpdate,
) -> ScheduledTask:
    item = db.get(ScheduledTask, schedule_id)
    if item is None:
        raise ValueError("Scheduled task not found")
    current = {
        "name": item.name,
        "task_type": item.task_type,
        "frequency": item.frequency,
        "time_of_day": item.time_of_day,
        "weekdays": _json(item.weekdays_json, []),
        "interval_minutes": item.interval_minutes,
        "timezone": item.timezone,
        "payload": _json(item.payload_json, {}),
        "enabled": bool(item.enabled),
    }
    current.update(patch.model_dump(exclude_unset=True))
    validated = ScheduledTaskCreate.model_validate(current)
    validate_definition(validated.task_type)
    _zone(validated.timezone)
    _assert_no_duplicate_schedule(db, validated, exclude_schedule_id=schedule_id)
    task_payload = validate_task_payload(
        validated.task_type,
        validated.payload,
    )
    item.name = validated.name
    item.task_type = validated.task_type
    item.frequency = validated.frequency
    item.time_of_day = validated.time_of_day
    item.weekdays_json = json.dumps(validated.weekdays)
    item.interval_minutes = validated.interval_minutes
    item.timezone = validated.timezone
    item.payload_json = json.dumps(task_payload, ensure_ascii=False, default=str)
    item.enabled = int(validated.enabled)
    item.next_run_at = _next_for_model(item) if validated.enabled else None
    item.updated_at = _now()
    db.commit()
    db.refresh(item)
    return item


def delete_schedule(db: Session, schedule_id: int) -> None:
    item = db.get(ScheduledTask, schedule_id)
    if item is None:
        raise ValueError("Scheduled task not found")
    db.execute(delete(ScheduledTaskRun).where(ScheduledTaskRun.schedule_id == schedule_id))
    db.delete(item)
    db.commit()


def _fresh_service_session() -> Session:
    """为同步执行的任务开独立会话（任务自己提交/回滚，不与调度轮次共用事务）。"""
    from app.db.manager import DatabaseManager

    return DatabaseManager.get().session_factory()


def _dispatch_task(item: ScheduledTask):
    payload = _json(item.payload_json, {})
    if is_tail_proxy_schedule(item.task_type, payload):
        reject_tail_proxy()
    if item.task_type == "portfolio_reconciliation":
        from app.services.portfolio_reconciliation_job import create_portfolio_reconciliation_task
        validated = validate_task_payload(item.task_type, payload)
        return "async", create_portfolio_reconciliation_task(schedule_id=item.id, **validated)
    if item.task_type == "universe_incremental_sync":
        from app.services.universe_sync_task import start_universe_incremental_sync

        task = start_universe_incremental_sync(
            max_workers=int(payload.get("max_workers", 5)),
            scopes=payload.get("scopes"),
        )
        return "async", task
    if item.task_type == "macro_update":
        from app.schemas.macro import MacroUpdateRequest
        from app.services.macro_update_task import create_macro_update_task

        task = create_macro_update_task(MacroUpdateRequest.model_validate(payload))
        return "async", task
    if item.task_type == "hot_rank_snapshot":
        from app.services.hot_rank_task import create_hot_rank_task

        return "async", create_hot_rank_task()
    if item.task_type == "lhb_institution_sync":
        from app.services.lhb_institution_task import (
            create_lhb_institution_task,
        )

        return "async", create_lhb_institution_task(
            lookback_days=int(payload.get("lookback_days") or 3)
        )
    if item.task_type == "factor_grade_review":
        # 季度重评：**复用既有调度器**（not_do：不新建调度器），同步执行后
        # 把统计写进 ScheduledTaskRun.message；source 用 "grading" 以区别于
        # 异步任务（async_tasks），避免 _task_state 按异步任务表查错。
        from app.db.manager import DatabaseManager
        from app.services.factors.mining.factor_grading import run_quarterly_review

        session = DatabaseManager.get().session_factory()
        try:
            stats = run_quarterly_review(
                session, actor=str(payload.get("actor") or "scheduler"))
        finally:
            session.close()
        return "grading", {
            "id": f"grade-review-{int(_now().timestamp())}",
            "status": "done",
            "message": (
                f"季度重评完成：重评 {stats.get('reviewed', 0)} 个"
                f"（升 {stats.get('promoted', 0)} / 降 {stats.get('demoted', 0)}"
                f" / 保持 {stats.get('kept', 0)}；跳过人工调整"
                f" {stats.get('skipped_manual', 0)}；移出 FactorSet"
                f" {stats.get('removed_from_factor_set', 0)}）"
            ),
        }
    if item.task_type == "financial_report_sync":
        from app.services.financial_report_task import (
            create_financial_report_task,
        )

        return "async", create_financial_report_task(
            source=str(payload.get("source") or "watchlist"),
            limit=int(payload.get("limit") or 20),
        )
    if item.task_type == "factor_pipeline":
        from app.schemas.async_task import FactorPipelineCreate
        from app.services.factors.pipeline_task import create_factor_pipeline_task

        task = create_factor_pipeline_task(FactorPipelineCreate.model_validate(payload))
        return "async", task
    if item.task_type == "discovery_mining":
        from app.schemas.discovery import DiscoveryTaskCreate
        from app.services.discovery_tasks import create_discovery_task

        task = create_discovery_task(DiscoveryTaskCreate.model_validate(payload))
        return "discovery", task
    if item.task_type == "portfolio_equity_snapshot":
        from app.services.portfolio_equity_snapshot import (
            create_portfolio_equity_snapshot_task,
        )

        return "async", create_portfolio_equity_snapshot_task()
    if item.task_type == "index_daily_sync":
        from app.services.index_data_task import (
            create_index_daily_sync_task,
        )

        symbol = str(payload.get("symbol", "000300"))
        lookback_days = int(payload.get("lookback_days", 5))
        return "async", create_index_daily_sync_task(symbol=symbol, lookback_days=lookback_days)
    if item.task_type == "external_data_sync":
        from app.services.external_data_sync_task import start_external_data_sync

        dataset = str(payload.get("dataset") or "fundamental")
        return "async", start_external_data_sync(dataset, payload)
    if item.task_type == "portfolio_auto_trade":
        from app.services.auto_trade_task import (
            create_portfolio_auto_trade_task,
        )

        return "async", create_portfolio_auto_trade_task(
            dry_run=bool(payload.get("dry_run", False)),
            buy_candidate_limit=int(payload.get("buy_candidate_limit", 10)),
        )
    if item.task_type == "alert_evaluation":
        # 与告警中心「立即评估」同一条代码路径（自己开会话、自己提交），所以这里
        # 同步执行即可；source 用 "alerts" 而非 "async"，避免 _task_state 去异步
        # 任务表里查一个不存在的 id。
        from app.services.alerts import evaluate_all_rules

        events = evaluate_all_rules()
        return "alerts", {
            "id": f"alert-eval-{int(_now().timestamp())}",
            "status": "done",
            "message": f"告警评估完成：本轮命中 {len(events)} 条告警",
        }
    if item.task_type == "market_events_collect":
        # 行情消息 P0-2：同步执行（scheduler_loop 在 to_thread 里跑，akshare 的
        # 秒级耗时不会阻塞事件循环）；自己开会话自己提交，与 alert_evaluation 同一模式。
        from app.schemas.market_event import MarketEventCollectRequest
        from app.services.market_events import collect_market_events

        validated = validate_task_payload(item.task_type, payload)
        session = _fresh_service_session()
        try:
            result = collect_market_events(
                session, MarketEventCollectRequest(**validated), trigger_source="scheduled"
            )
            message = (
                f"行情消息采集完成：新增 {result.collected} 条，"
                f"跳过重复 {result.skipped_duplicate} 条"
            )
            from app.services.notifications.event_emitter import emit_market_news_collect_failed

            daily_key = f"market_news_collect_failed:{_now().date().isoformat()}"
            # 告警一（全源失败）：部分失败源常见且已有重试兜底，不值得打扰。
            if result.status == "failed":
                emit_market_news_collect_failed(
                    session, errors=result.errors, event_key=daily_key,
                )
                message += f"；全部 {len(result.errors)} 个来源失败，已触发告警策略"
            elif result.errors:
                message += f"；{len(result.errors)} 个来源失败"
            # 告警二（P1-7 连续空采）：采集"成功"但连续两轮零新增——源在返回
            # 空数据而不是报错，全源失败告警覆盖不到这种静默断供。
            from app.services.market_events import recent_collects_all_empty

            if result.status == "done" and result.collected == 0 and recent_collects_all_empty(session, lookback=2):
                emit_market_news_collect_failed(
                    session, errors=[],
                    event_key=f"{daily_key}:stale",
                    reason_text="行情消息连续两轮采集零新增，疑似数据源静默断供，请人工核查。",
                )
                message += "；连续空采，已触发告警策略"
        finally:
            session.close()
        return "market_events", {
            "id": f"market-events-collect-{int(_now().timestamp())}",
            "status": "failed" if result.status == "failed" else "done",
            "message": message,
        }
    if item.task_type == "market_events_cleanup":
        # 行情消息 P0-1：凌晨清理过期消息（服务内已提交）。
        from app.services.market_events import cleanup_expired_market_events

        validated = validate_task_payload(item.task_type, payload)
        session = _fresh_service_session()
        try:
            result = cleanup_expired_market_events(session, keep_days=validated["keep_days"])
        finally:
            session.close()
        return "market_events", {
            "id": f"market-events-cleanup-{int(_now().timestamp())}",
            "status": "done",
            "message": f"行情消息清理完成：删除 {result.deleted} 条（保留窗口 {result.keep_days} 天）",
        }
    raise ValueError(f"Unsupported scheduled task type: {item.task_type}")


class ScheduleDispatchError(ValueError):
    """派发失败，带上是哪条调度、哪条 run，好让调用方在干净会话里补记原因。"""

    def __init__(self, reason: str, *, schedule_id: int, run_id: int | None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.schedule_id = schedule_id
        self.run_id = run_id


def execute_schedule(
    db: Session,
    schedule_id: int,
    *,
    trigger_source: str,
) -> ScheduledTaskRun:
    item = db.get(ScheduledTask, schedule_id)
    if item is None:
        raise ValueError("Scheduled task not found")
    run = ScheduledTaskRun(
        schedule_id=item.id,
        trigger_source=trigger_source,
        status="dispatching",
        message="Dispatching task",
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    try:
        source, task = _dispatch_task(item)
        task_id = task.get("id") if isinstance(task, dict) else getattr(task, "id", None)
        task_status = task.get("status") if isinstance(task, dict) else getattr(task, "status", None)
        task_message = task.get("message") if isinstance(task, dict) else getattr(task, "message", None)
        if not task_id:
            raise ValueError("Task dispatcher returned no task id")
        run.task_source = source
        run.task_id = str(task_id)
        run.status = str(task_status or "queued")
        run.message = str(task_message or "Task dispatched")
        item.last_run_at = _now()
        item.last_status = run.status
        item.last_task_id = run.task_id
        item.last_error = None
        item.updated_at = _now()
        db.commit()
        db.refresh(run)
        return run
    except Exception as exc:
        reason = str(exc)
        # 记账本身也可能失败（前台行情同步跑两小时期间连接容易被拖垮）。二级异常
        # 绝不能顶掉原始原因，否则调用方只看到"数据库断了"，看不到"被优先级挡住"。
        try:
            db.rollback()
            run.status = "failed"
            run.message = reason
            item.last_run_at = _now()
            item.last_status = "failed"
            item.last_error = reason
            item.updated_at = _now()
            db.commit()
        except Exception:
            db.rollback()
            logger.exception(
                "Could not record dispatch failure for scheduled task %s", schedule_id
            )
        logger.warning("Scheduled task %s dispatch failed: %s", schedule_id, reason)
        raise ScheduleDispatchError(
            reason, schedule_id=schedule_id, run_id=getattr(run, "id", None)
        ) from exc


def _task_state(db: Session, source: str | None, task_id: str | None):
    if not source or not task_id:
        return None
    if source == "async":
        return db.get(AsyncTaskRecord, task_id)
    if source == "discovery":
        return db.get(DiscoveryTaskRecord, task_id)
    return None


def schedule_to_dict(db: Session, item: ScheduledTask) -> dict:
    latest_run = db.execute(
        select(ScheduledTaskRun)
        .where(ScheduledTaskRun.schedule_id == item.id)
        .order_by(desc(ScheduledTaskRun.created_at), desc(ScheduledTaskRun.id))
        .limit(1)
    ).scalars().first()
    task = _task_state(
        db,
        latest_run.task_source if latest_run else None,
        latest_run.task_id if latest_run else None,
    )
    return {
        "id": item.id,
        "name": item.name,
        "task_type": item.task_type,
        "frequency": item.frequency,
        "time_of_day": item.time_of_day,
        "weekdays": _json(item.weekdays_json, []),
        "interval_minutes": item.interval_minutes,
        "timezone": item.timezone,
        "payload": _json(item.payload_json, {}),
        "enabled": bool(item.enabled),
        "next_run_at": item.next_run_at,
        "last_run_at": item.last_run_at,
        "last_status": item.last_status,
        "last_task_id": item.last_task_id,
        "last_task_status": getattr(task, "status", item.last_status),
        "last_message": getattr(task, "message", latest_run.message if latest_run else None),
        "last_error": item.last_error,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def run_to_dict(db: Session, run: ScheduledTaskRun) -> dict:
    task = _task_state(db, run.task_source, run.task_id)
    schedule = db.get(ScheduledTask, run.schedule_id)
    return {
        "id": run.id,
        "schedule_id": run.schedule_id,
        "schedule_name": schedule.name if schedule else None,
        "task_type": schedule.task_type if schedule else None,
        "trigger_source": run.trigger_source,
        "task_source": run.task_source,
        "task_id": run.task_id,
        "status": getattr(task, "status", run.status),
        "message": getattr(task, "message", run.message),
        "created_at": run.created_at,
        "finished_at": getattr(task, "finished_at", None),
    }


def _schedule_dispatch_retry(
    schedule_id: int,
    reason: str,
    run_id: int | None = None,
) -> None:
    """把派发失败记到干净会话里，并决定下一次什么时候再试。

    前台行情同步跑两个小时期间，调度那一轮的会话很容易被连接问题拖垮；此时若在
    同一个会话里补写"失败原因"，写不进去就什么都没留下（实测：run 一直停在
    `dispatching`、`last_status` 为 NULL，看板上等于"从未跑过"）。所以这里另开
    会话，先落账，再安排重试。

    `run_due_schedules` 是先推进 `next_run_at` 再派发的（防止重复派发），所以派发
    一旦失败，这一轮已经把今天用掉了：短延迟重来；但连续失败（配置错、类型不支持）
    不能变成每 15 分钟一次的空转，超过当日上限就让位给下一个正常时段。
    """
    db = get_session_local()()
    try:
        item = db.get(ScheduledTask, schedule_id)
        if item is None:
            return
        item.last_run_at = _now()
        item.last_status = "failed"
        item.last_error = reason
        item.updated_at = _now()
        if run_id is not None:
            run = db.get(ScheduledTaskRun, run_id)
            if run is not None:
                run.status = "failed"
                run.message = reason
        today_start = _now().replace(hour=0, minute=0, second=0, microsecond=0)
        attempts = int(db.execute(
            select(func.count(ScheduledTaskRun.id)).where(
                ScheduledTaskRun.schedule_id == schedule_id,
                ScheduledTaskRun.status == "failed",
                ScheduledTaskRun.created_at >= today_start,
            )
        ).scalar() or 0)
        if attempts >= DISPATCH_MAX_RETRIES_PER_DAY:
            logger.warning(
                "Scheduled task %s hit the daily dispatch retry limit (%s); next run stays at %s",
                schedule_id, DISPATCH_MAX_RETRIES_PER_DAY, item.next_run_at,
            )
            db.commit()
            return
        item.next_run_at = _now() + timedelta(minutes=DISPATCH_RETRY_DELAY_MINUTES)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Could not record dispatch failure for scheduled task %s", schedule_id)
    finally:
        db.close()


def disable_retired_schedules(db: Session) -> int:
    """Persistently stop both legacy schedule forms without deleting run history."""
    rows = db.execute(select(ScheduledTask).where(
        ScheduledTask.task_type.in_(("tail_proxy_snapshot", "external_data_sync")),
    )).scalars().all()
    changed = 0
    for item in rows:
        if is_tail_proxy_schedule(item.task_type, _json(item.payload_json, {})) and (
            item.enabled or item.next_run_at is not None
        ):
            item.enabled = 0
            item.next_run_at = None
            item.last_error = TAIL_PROXY_DISABLED_REASON
            item.updated_at = _now()
            changed += 1
    db.flush()
    return changed


def run_due_schedules(*, task_types: frozenset[str] | None = None) -> int:
    SessionLocal = get_session_local()
    db = SessionLocal()
    claimed: list[int] = []
    now = _now()
    try:
        if task_types is None:
            disable_retired_schedules(db)
        db.commit()
        query = select(ScheduledTask).where(
                ScheduledTask.enabled == 1,
                ScheduledTask.next_run_at.is_not(None),
                ScheduledTask.next_run_at <= now,
            )
        if task_types is not None:
            query = query.where(ScheduledTask.task_type.in_(task_types))
        candidates = db.execute(query.order_by(ScheduledTask.next_run_at, ScheduledTask.id)).scalars().all()
        for item in candidates:
            old_next = item.next_run_at
            next_run = _next_for_model(item, after=now + timedelta(seconds=1))
            result = db.execute(
                update(ScheduledTask)
                .where(
                    ScheduledTask.id == item.id,
                    ScheduledTask.enabled == 1,
                    ScheduledTask.next_run_at == old_next,
                )
                .values(next_run_at=next_run, updated_at=now)
            )
            db.commit()
            if result.rowcount == 1:
                claimed.append(item.id)
        for schedule_id in claimed:
            try:
                execute_schedule(db, schedule_id, trigger_source="scheduled")
            except ValueError as exc:
                logger.warning("Scheduled task %s failed to dispatch: %s", schedule_id, exc)
                _schedule_dispatch_retry(
                    schedule_id, str(exc), getattr(exc, "run_id", None)
                )
            except Exception as exc:
                # 一条调度的意外（含二级数据库异常）不得吃掉同一轮里其它到期任务。
                # 会话是否已脏不用在这里判断：重试帮助函数自己另开新会话。
                logger.exception(
                    "Scheduled task %s dispatch raised unexpectedly: %s", schedule_id, exc
                )
                db.rollback()
                _schedule_dispatch_retry(
                    schedule_id, f"{type(exc).__name__}: {exc}", None
                )
        return len(claimed)
    finally:
        db.close()


async def scheduler_loop(*, task_types: frozenset[str] | None = None) -> None:
    """Poll due schedules while the FastAPI process is alive."""
    while True:
        try:
            await asyncio.to_thread(run_due_schedules, task_types=task_types)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Scheduled task loop failed")
        await asyncio.sleep(SCHEDULER_CHECK_INTERVAL_SECONDS)


__all__ = [
    "ScheduleDispatchError",
    "TASK_DEFINITIONS",
    "calculate_next_run",
    "create_schedule",
    "delete_schedule",
    "execute_schedule",
    "run_due_schedules",
    "run_to_dict",
    "schedule_to_dict",
    "scheduler_loop",
    "seed_default_schedules",
    "update_schedule",
]
