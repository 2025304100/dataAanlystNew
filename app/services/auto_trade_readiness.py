"""自动交易就绪检查服务（P0-AutoTrade readiness）。

统一对外输出：ready / enabled / account_ready / data_ready / source_ready /
schedule_ready / counts / blockers / warnings。

fail-closed 原则：
- 真实执行前必须 ready=True；
- dry-run 仍可用于诊断，readiness 只影响真实执行与状态展示；
- 定时任务触发时（for_schedule=True）把 schedule_ready 升级为 blocker。

核心判定（对齐最终收口方案）：
    ready = enabled AND account_ready AND data_ready AND source_ready
        AND rule_configured
        AND (position_count > 0 OR eligible_buy_count > 0)
        AND (NOT for_schedule OR schedule_ready)
        AND len(blockers) == 0
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.models.portfolio import (
    AUTO_TRADE_SOURCE_LEGACY_SCAN,
    AUTO_TRADE_SOURCE_MEMBERS_ONLY,
    AUTO_TRADE_SOURCE_MODES,
    AUTO_TRADE_SOURCE_PORTFOLIO,
    Portfolio,
    PortfolioRule,
    Position,
)
from app.models.portfolio_candidate import PortfolioCandidate
from app.models.portfolio_member import (
    EXECUTION_AUTO,
    PortfolioMember,
    STATUS_ACTIVE,
)
from app.models.scheduled_task import ScheduledTask
from app.models.score import Score
from app.models.symbol import Symbol
from app.services.allocation import get_active_rule
from app.services.auto_trade_member_source import _BUY_ACTIONS, _get_latest_signal
from app.services.auto_trade_safety import check_data_health
from app.services.portfolio_members import has_position, list_members
from app.services.portfolio_asset_scope import allows_asset_type


logger = logging.getLogger(__name__)

SCHEDULED_TASK_TYPE = "portfolio_auto_trade"

BLOCKER_PORTFOLIO_DISABLED = "PORTFOLIO_DISABLED"
BLOCKER_ACCOUNT_NOT_SIMULATED = "ACCOUNT_NOT_SIMULATED"
BLOCKER_RULE_NOT_CONFIGURED = "RULE_NOT_CONFIGURED"
BLOCKER_RULE_INVALID_PARAMS = "RULE_INVALID_PARAMS"
BLOCKER_SOURCE_MODE_INVALID = "SOURCE_MODE_INVALID"
BLOCKER_NO_TRADEABLE_RANGE = "NO_TRADEABLE_RANGE"
BLOCKER_MARKET_DATA_STALE = "MARKET_DATA_STALE"
BLOCKER_SCHEDULE_DISABLED = "SCHEDULE_DISABLED"
# Execution-only prerequisites are displayed by action-items. They do not
# change this legacy readiness service or the actual execution gate.
BLOCKER_DECISION_ENGINE_SOURCE_REQUIRED = "DECISION_ENGINE_SOURCE_REQUIRED"
BLOCKER_STRATEGY_SNAPSHOT_REQUIRED = "STRATEGY_SNAPSHOT_REQUIRED"
BLOCKER_STRATEGY_BINDING_INVALID = "STRATEGY_BINDING_INVALID"

WARNING_SCHEDULE_DISABLED = "SCHEDULE_DISABLED"
WARNING_SOURCE_MODE_LEGACY_SCAN = "SOURCE_LEGACY_SCAN"
WARNING_SOURCE_ENV_OVERRIDE = "SOURCE_ENV_OVERRIDE"
WARNING_NO_AUTO_MEMBERS = "NO_AUTO_MEMBERS"
WARNING_NO_CANDIDATES = "NO_CANDIDATES"
WARNING_SIGNAL_EMPTY_TODAY = "NO_BUY_SIGNALS_TODAY"


@dataclass(slots=True)
class ReadinessIssue:
    code: str
    message: str
    detail: str | None = None
    # P0 行动项：基础级别由「进入 blockers / warnings 哪个列表」决定，
    # title/fix_link 由 READINESS_FIX_REGISTRY 统一补齐（见下方工厂）。
    severity: str = "blocker"
    title: str | None = None
    fix_link: dict[str, Any] | None = None


# 主动作类型与前端契约一致：toggle=原地开关 / redirect=跨页 / subtab=组合内子页 /
# open-modal=打开既有弹窗 / guided=分步引导 / none=仅说明。
READINESS_ACTION_TYPES: frozenset[str] = frozenset(
    {"toggle", "redirect", "subtab", "open-modal", "guided", "none"}
)

# title ≤12 字、impact 一句话；target 对 redirect 填 settings 区、subtab 填子页键、
# open-modal 填弹窗键，toggle/guided/none 为 None。
# display_severity 仅用于聚合层的「展示降级」（如今日无信号 warning→info）。
READINESS_FIX_REGISTRY: dict[str, dict[str, Any]] = {
    BLOCKER_PORTFOLIO_DISABLED: {
        "title": "自动接管未开启",
        "impact": "开关关闭，今天不会自动下单",
        "action_type": "toggle",
        "target": None,
    },
    BLOCKER_ACCOUNT_NOT_SIMULATED: {
        "title": "账户类型不支持",
        "impact": "自动接管当前仅支持模拟账户",
        # P0 前端暂无「编辑组合账户类型」入口，降级为说明；待入口就绪再改 subtab/redirect。
        "action_type": "none",
        "target": None,
    },
    BLOCKER_RULE_NOT_CONFIGURED: {
        "title": "未配置激活策略",
        "impact": "没有可执行的选股与风控规则",
        "action_type": "subtab",
        "target": "strategy",
    },
    BLOCKER_RULE_INVALID_PARAMS: {
        "title": "策略参数不合法",
        "impact": "仓位或止损等字段超出允许范围",
        "action_type": "subtab",
        "target": "strategy",
    },
    BLOCKER_SOURCE_MODE_INVALID: {
        "title": "标的来源配置异常",
        "impact": "决策引擎取不到选股范围",
        # P0 前端无来源模式配置控件，降级为说明。
        "action_type": "none",
        "target": None,
    },
    BLOCKER_NO_TRADEABLE_RANGE: {
        "title": "没有可交易对象",
        "impact": "当前既无持仓，也没有合格买入计划",
        "action_type": "open-modal",
        "target": "add-candidate",
    },
    BLOCKER_MARKET_DATA_STALE: {
        "title": "行情或评分过期",
        "impact": "今天不会真实下单，回测与手动分析不受影响",
        "action_type": "redirect",
        "target": "settings:data-center",
    },
    BLOCKER_SCHEDULE_DISABLED: {
        "title": "自动调度未启用",
        "impact": "不会每日自动执行，手动 Dry Run 不受影响",
        "action_type": "redirect",
        "target": "settings:tasks",
    },
    WARNING_SOURCE_MODE_LEGACY_SCAN: {
        "title": "使用旧扫描来源",
        "impact": "不阻断执行，新鲜度检查较宽松，建议迁移",
        "action_type": "none",
        "target": None,
    },
    WARNING_SOURCE_ENV_OVERRIDE: {
        "title": "来源被环境开关熔断",
        "impact": "紧急熔断中，真实自动交易已阻断，需恢复成员来源开关",
        "action_type": "none",
        "target": None,
    },
    WARNING_NO_AUTO_MEMBERS: {
        "title": "无自动模式成员",
        "impact": "成员不会被自动调仓，候选池建仓不受影响",
        "action_type": "subtab",
        "target": "members",
    },
    WARNING_NO_CANDIDATES: {
        "title": "组合还没有标的",
        "impact": "自动交易没有工作对象，不会产生买卖",
        "action_type": "open-modal",
        "target": "add-candidate",
    },
    WARNING_SIGNAL_EMPTY_TODAY: {
        "title": "今日无买入信号",
        "impact": "属于正常节奏，不代表系统故障",
        "action_type": "none",
        "target": None,
        "display_severity": "info",
    },
    BLOCKER_DECISION_ENGINE_SOURCE_REQUIRED: {
        "title": "执行来源未启用",
        "impact": "真实自动交易需要成员来源开关，旧扫描仅用于双跑对照",
        "action_type": "none",
        "target": None,
    },
    BLOCKER_STRATEGY_SNAPSHOT_REQUIRED: {
        "title": "策略尚未保存应用",
        "impact": "请完成策略预检并保存应用，生成可执行的策略快照",
        "action_type": "subtab",
        "target": "strategy",
    },
    BLOCKER_STRATEGY_BINDING_INVALID: {
        "title": "策略绑定需要更新",
        "impact": "当前快照的模型或策略绑定不满足执行条件，请重新保存应用",
        "action_type": "subtab",
        "target": "strategy",
    },
}


def _make_issue(
    code: str,
    message: str,
    severity: str,
    detail: str | None = None,
) -> ReadinessIssue:
    """从注册表补 title/fix_link；未登记 code 走安全兜底（动作 none、不阻断序列化）。"""
    meta = READINESS_FIX_REGISTRY.get(code, {})
    action_type = meta.get("action_type", "none")
    if action_type not in READINESS_ACTION_TYPES:
        action_type = "none"
    fix_link: dict[str, Any] = {"action_type": action_type}
    target = meta.get("target")
    if target:
        fix_link["target"] = target
    return ReadinessIssue(
        code=code,
        message=message,
        detail=detail,
        severity=severity,
        title=meta.get("title"),
        fix_link=fix_link,
    )


def _blocker(code: str, message: str, detail: str | None = None) -> ReadinessIssue:
    return _make_issue(code, message, "blocker", detail)


def _warning(code: str, message: str, detail: str | None = None) -> ReadinessIssue:
    return _make_issue(code, message, "warning", detail)


@dataclass(slots=True)
class AutoTradeReadinessResult:
    ready: bool
    enabled: bool
    account_ready: bool
    data_ready: bool
    source_ready: bool
    schedule_ready: bool
    position_count: int
    candidate_count: int
    eligible_buy_count: int
    auto_member_count: int
    blockers: list[ReadinessIssue] = field(default_factory=list)
    warnings: list[ReadinessIssue] = field(default_factory=list)
    source_mode: str = AUTO_TRADE_SOURCE_PORTFOLIO
    rule_configured: bool = False
    updated_at: str | None = None
    last_real_run_at: str | None = None
    last_schedule_run_at: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


def _naive_utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    try:
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt.replace(tzinfo=timezone.utc).isoformat()
    except Exception:  # noqa: BLE001
        logger.warning("readiness iso 时间格式化失败: %s", dt, exc_info=True)
        return None


def _parse_stage_limits(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _validate_rule_fields(rule: PortfolioRule | None) -> tuple[bool, str | None]:
    if rule is None:
        return False, "缺少激活策略规则"
    if not (0.0 <= float(rule.max_single_position_pct or 0.0) <= 1.0):
        return False, "max_single_position_pct 超出合法范围"
    if not (0.0 <= float(rule.max_sector_position_pct or 0.0) <= 1.0):
        return False, "max_sector_position_pct 超出合法范围"
    if not (0.0 <= float(rule.max_stock_position_pct or 0.0) <= 1.0):
        return False, "max_stock_position_pct 超出合法范围"
    if not (0.0 <= float(rule.max_etf_position_pct or 0.0) <= 1.0):
        return False, "max_etf_position_pct 超出合法范围"
    if not (0.0 <= float(rule.max_loss_per_trade_pct or 0.0) <= 1.0):
        return False, "max_loss_per_trade_pct 超出合法范围"
    if not (int(rule.max_open_positions or 0) >= 0):
        return False, "max_open_positions 必须非负"
    stage_limits = _parse_stage_limits(rule.stage_limits_json)
    if not stage_limits:
        return False, "stage_limits_json 为空"
    return True, None


def _normalize_source_mode(raw: Any) -> tuple[str, bool]:
    value = str(raw or AUTO_TRADE_SOURCE_PORTFOLIO).strip() or AUTO_TRADE_SOURCE_PORTFOLIO
    if value not in AUTO_TRADE_SOURCE_MODES:
        return AUTO_TRADE_SOURCE_PORTFOLIO, False
    return value, True


def _find_schedule_for_portfolio(
    db: Session,
    portfolio_id: int,
) -> ScheduledTask | None:
    """查找对应组合的自动交易调度。

    兼容两阶段：
    - 全局自动交易调度（payload 为空，扫描所有 enabled 组合）
    - 组合级调度（payload.portfolio_id == target_id）
    存在多个时优先返回 enabled=1 的、再按 id 倒序取最近一个。
    """
    stmt = select(ScheduledTask).where(ScheduledTask.task_type == SCHEDULED_TASK_TYPE)
    rows = db.execute(stmt).scalars().all()
    matching: list[ScheduledTask] = []
    for row in rows:
        try:
            payload = json.loads(row.payload_json or "{}")
        except Exception:  # noqa: BLE001
            payload = {}
        if not payload:
            matching.append(row)
            continue
        payload_pid = payload.get("portfolio_id")
        if payload_pid is None:
            matching.append(row)
        elif int(payload_pid) == int(portfolio_id):
            matching.append(row)
    if not matching:
        return None
    matching.sort(key=lambda x: (int(x.enabled or 0), int(x.id or 0)), reverse=True)
    return matching[0]


def _count_positions(db: Session, portfolio_id: int) -> int:
    stmt = select(func.count(Position.id)).where(
        and_(
            Position.portfolio_id == portfolio_id,
            Position.quantity != 0,
        )
    )
    return int(db.execute(stmt).scalar() or 0)


def _count_candidates(db: Session, portfolio_id: int) -> int:
    stmt = select(func.count(PortfolioCandidate.id)).where(
        PortfolioCandidate.portfolio_id == portfolio_id
    )
    return int(db.execute(stmt).scalar() or 0)


def _list_auto_member_ids(db: Session, portfolio_id: int) -> set[int]:
    active_members = list_members(
        db,
        portfolio_id=portfolio_id,
        status=STATUS_ACTIVE,
        include_archived=False,
    )
    return {int(m.symbol_id) for m in active_members if m.execution_mode == EXECUTION_AUTO}


def _list_portfolio_candidate_ids(db: Session, portfolio_id: int) -> set[int]:
    stmt = select(PortfolioCandidate.symbol_id).where(
        PortfolioCandidate.portfolio_id == portfolio_id
    )
    return {int(row) for row in db.execute(stmt).scalars().all() if row is not None}


def _symbol_eligible_buy(
    db: Session,
    *,
    portfolio_id: int,
    symbol_id: int,
    entry_rule_version_id: int | None = None,
) -> bool:
    """与真实执行链路一致的最小组合可买判定：无持仓 + 信号 + 数据健康。

    不做真实风控下单，只做就绪层判断。
    """
    has_pos, _ = has_position(db, portfolio_id=portfolio_id, symbol_id=symbol_id)
    if has_pos:
        return False
    score_dict, _signal_id, _snapshot = _get_latest_signal(db, symbol_id)
    if not score_dict:
        return False
    if score_dict.get("action", "hold") not in _BUY_ACTIONS:
        return False
    health = check_data_health(
        db,
        symbol_id=symbol_id,
        rule_version_id=entry_rule_version_id,
    )
    if not health.healthy:
        return False
    return True


def _entry_rule_version_for_member(
    member: PortfolioMember,
) -> int | None:
    version_id = getattr(member, "entry_rule_version_id", None)
    return int(version_id) if version_id else None


def get_auto_trade_readiness(
    db: Session,
    portfolio_id: int,
    *,
    for_schedule: bool = False,
) -> AutoTradeReadinessResult:
    """计算单个组合的自动交易就绪状态。

    Args:
        db: 数据库会话
        portfolio_id: 组合 ID
        for_schedule: 是否来自调度触发。为 True 时会把调度关闭升级为 blocker。

    Returns:
        AutoTradeReadinessResult：可直接序列化为字典返回给 API
    """
    portfolio: Portfolio | None = db.get(Portfolio, portfolio_id)
    if portfolio is None:
        raise ValueError(f"Portfolio {portfolio_id} not found")

    blockers: list[ReadinessIssue] = []
    warnings: list[ReadinessIssue] = []

    # 1. 开关与账户
    enabled = int(portfolio.auto_trade_enabled or 0) == 1
    account_ready = portfolio.account_type == "simulated"

    if not enabled:
        blockers.append(_blocker(BLOCKER_PORTFOLIO_DISABLED, "自动交易开关未开启"))
    if not account_ready:
        blockers.append(_blocker(BLOCKER_ACCOUNT_NOT_SIMULATED, "仅模拟组合可执行自动交易"))

    # 2. 来源模式（组合自身持久化字段为唯一来源；env 仅作为紧急熔断参考并记录 warning）
    source_mode, source_mode_valid = _normalize_source_mode(
        getattr(portfolio, "auto_trade_source_mode", AUTO_TRADE_SOURCE_PORTFOLIO)
    )
    if not source_mode_valid:
        blockers.append(
            _blocker(
                BLOCKER_SOURCE_MODE_INVALID,
                "自动交易来源模式非法，已回退为默认值",
                detail=f"raw={getattr(portfolio, 'auto_trade_source_mode', None)}",
            )
        )
    if source_mode == AUTO_TRADE_SOURCE_LEGACY_SCAN:
        warnings.append(
            _warning(
                WARNING_SOURCE_MODE_LEGACY_SCAN,
                "当前仍使用旧全局扫描来源，建议迁移为 portfolio/members_only",
            )
        )
    from app.services.auto_trade_dual_run import is_member_source_enabled as _is_member_source_enabled
    env_source_enabled = bool(_is_member_source_enabled(portfolio_id))
    if source_mode != AUTO_TRADE_SOURCE_LEGACY_SCAN and not env_source_enabled:
        warnings.append(
            _warning(
                WARNING_SOURCE_ENV_OVERRIDE,
                "成员来源全局开关已关闭，真实自动模拟执行将被阻断",
            )
        )
    # source_ready：来源模式合法 + 非 legacy 或已在迁移期显式启用
    source_ready = source_mode_valid and not (
        source_mode == AUTO_TRADE_SOURCE_LEGACY_SCAN and not env_source_enabled
    )

    # 3. 策略规则
    active_rule = get_active_rule(db, portfolio_id=portfolio_id)
    rule_configured, rule_invalid_reason = _validate_rule_fields(active_rule)
    if not rule_configured:
        blockers.append(
            _blocker(
                BLOCKER_RULE_NOT_CONFIGURED
                if "缺少激活策略规则" in (rule_invalid_reason or "")
                else BLOCKER_RULE_INVALID_PARAMS,
                rule_invalid_reason or "策略规则未配置",
            )
        )

    # 4. 标的范围计数
    position_count = _count_positions(db, portfolio_id)
    candidate_count = _count_candidates(db, portfolio_id)
    auto_member_ids = _list_auto_member_ids(db, portfolio_id)
    auto_member_count = len(auto_member_ids)

    # 4.1 汇总候选集合：根据不同来源模式决定 eligible 评估对象集合
    candidate_ids = _list_portfolio_candidate_ids(db, portfolio_id)
    if source_mode == AUTO_TRADE_SOURCE_MEMBERS_ONLY:
        eligible_symbol_ids = set(auto_member_ids)
    elif source_mode == AUTO_TRADE_SOURCE_PORTFOLIO:
        eligible_symbol_ids = set(auto_member_ids) | candidate_ids
    else:  # legacy_scan：就绪层不依赖全局扫描，仅按现有成员/候选保守估算
        eligible_symbol_ids = set(auto_member_ids) | candidate_ids

    # 与真实执行一致：旧记录或旁路写入的异类资产不能让单一资产组合
    # 被误判为“可自动交易”。
    if eligible_symbol_ids:
        eligible_symbols = db.execute(
            select(Symbol).where(Symbol.id.in_(eligible_symbol_ids))
        ).scalars().all()
        eligible_symbol_ids = {
            symbol.id
            for symbol in eligible_symbols
            if allows_asset_type(portfolio.asset_scope, symbol.asset_type)
        }

    # 5. eligible_buy_count：逐标的与执行链路同口径
    active_members_map = {}
    for m in list_members(
        db,
        portfolio_id=portfolio_id,
        status=STATUS_ACTIVE,
        include_archived=False,
    ):
        if int(m.symbol_id) not in active_members_map:
            active_members_map[int(m.symbol_id)] = []
        active_members_map[int(m.symbol_id)].append(m)

    eligible_buy_count = 0
    fresh_symbol_count = 0
    symbols_with_data_scope: set[int] = set()
    if source_mode != AUTO_TRADE_SOURCE_LEGACY_SCAN:
        symbols_with_data_scope = set(eligible_symbol_ids)
    for symbol_id in sorted(eligible_symbol_ids):
        members_for_symbol = active_members_map.get(int(symbol_id), [])
        member_version_ids = {
            _entry_rule_version_for_member(m)
            for m in members_for_symbol
            if m.execution_mode == EXECUTION_AUTO
        }
        version_id = next((v for v in member_version_ids if v), None)
        # eligible buy
        if _symbol_eligible_buy(
            db,
            portfolio_id=portfolio_id,
            symbol_id=symbol_id,
            entry_rule_version_id=version_id,
        ):
            eligible_buy_count += 1
        # 数据新鲜度：只要任一 K线+评分 healthy 即认为该符号属于 fresh
        health = check_data_health(db, symbol_id=symbol_id, rule_version_id=version_id)
        if health.healthy:
            fresh_symbol_count += 1

    # 6. data_ready：只有存在需要数据覆盖的范围时，才会阻塞
    data_ready = True
    if symbols_with_data_scope:
        if fresh_symbol_count <= 0:
            data_ready = False
            blockers.append(
                _blocker(
                    BLOCKER_MARKET_DATA_STALE,
                    "行情或评分数据已过期，当前组合候选均未通过新鲜度检查",
                )
            )
    if candidate_count == 0 and auto_member_count == 0 and position_count == 0:
        warnings.append(
            _warning(
                WARNING_NO_CANDIDATES,
                "当前组合既无持仓，也没有候选或自动成员，自动交易不会产生任何动作",
            )
        )
    if source_mode != AUTO_TRADE_SOURCE_LEGACY_SCAN and auto_member_count == 0:
        warnings.append(
            _warning(
                WARNING_NO_AUTO_MEMBERS,
                "当前组合没有配置为自动模式的成员，自动买入可能仅依赖组合候选池",
            )
        )
    if (position_count == 0 or source_mode == AUTO_TRADE_SOURCE_MEMBERS_ONLY) and eligible_buy_count == 0:
        if not any(
            b.code == BLOCKER_MARKET_DATA_STALE for b in blockers
        ):
            warnings.append(
                _warning(
                    WARNING_SIGNAL_EMPTY_TODAY,
                    "今日暂无满足条件的买入计划，仍可卖出已有持仓",
                )
            )

    # 7. 可交易范围判定（核心）：有持仓可卖 或 有可买入
    tradeable_range_ok = position_count > 0 or eligible_buy_count > 0
    if not tradeable_range_ok:
        blockers.append(
            _blocker(
                BLOCKER_NO_TRADEABLE_RANGE,
                "当前组合既无可卖出持仓，也无可执行买入计划",
            )
        )

    # 8. 调度状态：默认警告；来自调度入口则升级为 blocker
    schedule = _find_schedule_for_portfolio(db, portfolio_id)
    schedule_ready = bool(schedule and int(schedule.enabled or 0) == 1)
    if schedule is None:
        issue = (_blocker if for_schedule else _warning)(
            BLOCKER_SCHEDULE_DISABLED,
            "尚未创建自动交易定时任务，只能手动触发",
        )
        (blockers if for_schedule else warnings).append(issue)
    elif not schedule_ready:
        if for_schedule:
            blockers.append(
                _blocker(
                    BLOCKER_SCHEDULE_DISABLED,
                    "自动交易定时任务已暂停，本次调度执行跳过",
                )
            )
        else:
            warnings.append(
                _warning(
                    WARNING_SCHEDULE_DISABLED,
                    "自动交易定时任务已暂停",
                )
            )
    last_schedule_run_at = _iso(schedule.last_run_at) if schedule is not None else None

    # 9. 最终 ready
    ready = (
        enabled
        and account_ready
        and data_ready
        and source_ready
        and rule_configured
        and tradeable_range_ok
        and (not for_schedule or schedule_ready)
        and len(blockers) == 0
    )

    return AutoTradeReadinessResult(
        ready=ready,
        enabled=enabled,
        account_ready=account_ready,
        data_ready=data_ready,
        source_ready=source_ready,
        schedule_ready=schedule_ready,
        position_count=position_count,
        candidate_count=candidate_count,
        eligible_buy_count=eligible_buy_count,
        auto_member_count=auto_member_count,
        blockers=blockers,
        warnings=warnings,
        source_mode=source_mode,
        rule_configured=rule_configured,
        updated_at=_iso(_naive_utc_now()),
        last_real_run_at=_iso(portfolio.auto_trade_last_run_at),
        last_schedule_run_at=last_schedule_run_at,
        details={
            "fresh_symbol_count": fresh_symbol_count,
            "data_scope_symbol_count": len(symbols_with_data_scope),
            "schedule_id": schedule.id if schedule is not None else None,
            "schedule_enabled": bool(schedule and int(schedule.enabled or 0) == 1),
        },
    )


def readiness_to_dict(result: AutoTradeReadinessResult) -> dict[str, Any]:
    """把结果序列化为最终响应字典结构。"""
    base = asdict(result)
    # asdict 对 slots dataclass 在所有支持版本下均能工作；若失败则手动兜底
    if not isinstance(base, dict):
        base = {
            "ready": result.ready,
            "enabled": result.enabled,
            "account_ready": result.account_ready,
            "data_ready": result.data_ready,
            "source_ready": result.source_ready,
            "schedule_ready": result.schedule_ready,
            "position_count": result.position_count,
            "candidate_count": result.candidate_count,
            "eligible_buy_count": result.eligible_buy_count,
            "auto_member_count": result.auto_member_count,
            "blockers": [asdict(b) for b in result.blockers],
            "warnings": [asdict(w) for w in result.warnings],
            "source_mode": result.source_mode,
            "rule_configured": result.rule_configured,
            "updated_at": result.updated_at,
            "last_real_run_at": result.last_real_run_at,
            "last_schedule_run_at": result.last_schedule_run_at,
            "details": dict(result.details),
        }
    return base
