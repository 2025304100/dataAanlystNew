"""双跑切换（WP6.4）与统一自动交易执行入口（P0-AutoTrade）。

新旧来源双轨运行：
- AUTO_TRADE_MEMBER_SOURCE_ENABLED=false（WP9.5 前默认）：旧来源实际执行，新来源仅 Dry Run
- AUTO_TRADE_MEMBER_SOURCE_ENABLED=true（WP9.5 后默认）：新来源实际执行，旧来源仅 Dry Run（对照）

差异捕获：
- 连续至少 5 个交易日或 3 次有效运行保存旧/新买卖集合差异
- 对每个差异给出原因：成员缺失、状态暂停、信号不同、数据过期、风控阻断

切换策略：
- 差异经人工确认后先对一个非默认测试组合开启新来源
- 再逐组合切换
- 开关关闭可立即回退

WP9.5 变更：
- 全局开关默认值由 False 改为 True（通过 settings.AUTO_TRADE_MEMBER_SOURCE_ENABLED）
- 环境变量仍可显式覆盖（用于 rollback_to_old_source 运行时回退）
- 如需回退到旧行为，设置环境变量 AUTO_TRADE_MEMBER_SOURCE_ENABLED=false

P0-AutoTrade 新增：
- 组合级来源模式由 Portfolio.auto_trade_source_mode 持久化主判定，env 仅作为全局默认或紧急熔断。
- 统一执行入口：就绪检查 → 构建/双跑 → 执行，所有真实执行与 dry-run 均走 run_dual_trade。
- 组合级执行锁：避免手动触发与调度任务重复执行同一组合。
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.portfolio import (
    AUTO_TRADE_SOURCE_LEGACY_SCAN,
    AUTO_TRADE_SOURCE_MEMBERS_ONLY,
    AUTO_TRADE_SOURCE_MODES,
    AUTO_TRADE_SOURCE_PORTFOLIO,
    Portfolio,
)
from app.models.portfolio_member import PortfolioMember
from app.services.auto_trade_readiness import (
    WARNING_SOURCE_ENV_OVERRIDE,
    WARNING_SOURCE_MODE_LEGACY_SCAN,
    ReadinessIssue,
    get_auto_trade_readiness,
    readiness_to_dict,
)


logger = logging.getLogger(__name__)


# 环境变量开关
ENV_FLAG = "AUTO_TRADE_MEMBER_SOURCE_ENABLED"

# 组合级执行锁（P0-AutoTrade：防止手动+调度/重复提交并发下单）
# 注：当前为进程内锁，在单 uvicorn worker 下即可满足 fail-closed 要求；
# 后续若迁移多 worker，应改为数据库唯一键或分布式锁。
_RUN_LOCK: dict[int, threading.Lock] = {}
_RUN_LOCK_GUARD = threading.Lock()
_RUN_LOCK_HELD: dict[int, bool] = {}


def _acquire_run_lock(portfolio_id: int) -> bool:
    key = int(portfolio_id)
    with _RUN_LOCK_GUARD:
        lock = _RUN_LOCK.setdefault(key, threading.Lock())
    acquired = lock.acquire(blocking=False)
    if acquired:
        with _RUN_LOCK_GUARD:
            _RUN_LOCK_HELD[key] = True
    return acquired


def _release_run_lock(portfolio_id: int) -> None:
    key = int(portfolio_id)
    with _RUN_LOCK_GUARD:
        lock = _RUN_LOCK.get(key)
        was_held = bool(_RUN_LOCK_HELD.pop(key, False))
    if lock is not None and was_held:
        try:
            lock.release()
        except Exception:  # noqa: BLE001
            logger.warning(
                "auto trade run lock release failed portfolio_id=%s",
                portfolio_id,
                exc_info=True,
            )


class AutoTradeNotReadyError(RuntimeError):
    """readiness 判定为不就绪时抛出，真实执行 fail-closed。"""

    def __init__(self, message: str, blockers: list[ReadinessIssue]) -> None:
        super().__init__(message)
        self.blockers = list(blockers)


def is_member_source_enabled(portfolio_id: int | None = None) -> bool:
    """检查是否启用新来源（WP6.4 / WP9.5）。

    支持：
    - 全局开关：优先读取环境变量 AUTO_TRADE_MEMBER_SOURCE_ENABLED（运行时覆盖），
      未设置时回退到 settings.AUTO_TRADE_MEMBER_SOURCE_ENABLED（WP9.5 默认 True）
    - 组合级开关：AUTO_TRADE_MEMBER_SOURCE_PORTFOLIOS=1,3,5（白名单）

    优先级：组合级白名单 > 全局开关
    - 全局 false + 组合在白名单 → 该组合启用
    - 全局 true + 组合在黑名单 → 不启用

    WP9.5：全局开关默认值由 False 改为 True。环境变量显式设置时优先于 settings 默认值，
    以支持 rollback_to_old_source 运行时回退（设置 os.environ[ENV_FLAG]="false"）。
    """
    env_value = os.environ.get(ENV_FLAG)
    if env_value is not None:
        # 环境变量显式设置时优先（支持运行时 rollback）
        global_flag = env_value.strip().lower() in ("true", "1", "yes")
    else:
        # 未设置环境变量时使用 settings 默认值（WP9.5: True）
        global_flag = settings.AUTO_TRADE_MEMBER_SOURCE_ENABLED

    if portfolio_id is not None:
        # 组合级白名单（即使全局 false，白名单内的组合也启用）
        whitelist = os.environ.get("AUTO_TRADE_MEMBER_SOURCE_PORTFOLIOS", "")
        if whitelist:
            whitelist_ids = {
                int(x.strip()) for x in whitelist.split(",") if x.strip().isdigit()
            }
            if portfolio_id in whitelist_ids:
                return True

        # 组合级黑名单（即使全局 true，黑名单内的组合也不启用）
        blacklist = os.environ.get("AUTO_TRADE_MEMBER_SOURCE_EXCLUDED_PORTFOLIOS", "")
        if blacklist:
            blacklist_ids = {
                int(x.strip()) for x in blacklist.split(",") if x.strip().isdigit()
            }
            if portfolio_id in blacklist_ids:
                return False

    return global_flag


@dataclass
class TradeSet:
    """交易集合（用于差异对比）。"""

    buys: list[dict] = field(default_factory=list)
    sells: list[dict] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "buys": self.buys,
            "sells": self.sells,
            "rejected": self.rejected,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TradeSet":
        return cls(
            buys=data.get("buys", []),
            sells=data.get("sells", []),
            rejected=data.get("rejected", []),
        )


@dataclass
class TradeDiff:
    """交易差异。"""

    symbol_id: int
    side: str  # buy/sell
    old_action: str | None  # 旧来源决策
    new_action: str | None  # 新来源决策
    reason: str  # 差异原因：member_missing/member_paused/signal_diff/data_expired/risk_blocked
    detail: str  # 详细说明


def capture_trade_set_from_old_logic(db: Session, *, portfolio_id: int) -> TradeSet:
    """从旧逻辑捕获交易集合（WP6.4）。

    调用现有 auto_trade_task 逻辑（dry_run 模式，不实际下单）。
    旧逻辑返回的 buys/sells 字段映射到 TradeSet；
    errors 为字符串列表，不兼容 rejected 结构，故忽略。
    """
    from app.services.auto_trade_task import run_auto_trade

    try:
        result = run_auto_trade(db, portfolio_id=portfolio_id, dry_run=True)
        trade_set = TradeSet(
            buys=result.get("buys", []),
            sells=result.get("sells", []),
            rejected=[],  # 旧逻辑无 rejected 概念
        )
        return trade_set
    except Exception as e:
        # 不暴露敏感信息，仅记录概要
        logger.warning("旧逻辑 dry_run 失败 portfolio_id=%s: %s", portfolio_id, e)
        return TradeSet()


def capture_trade_set_from_new_logic(db: Session, *, portfolio_id: int) -> TradeSet:
    """从新逻辑捕获交易集合（WP6.4）。

    调用 auto_trade_member_source 的 dry_run。
    """
    from app.services.auto_trade_member_source import execute_member_source

    result = execute_member_source(db, portfolio_id=portfolio_id, dry_run=True)
    trade_set = TradeSet(
        buys=result.get("buy_decisions", []),
        sells=result.get("sell_decisions", []),
        rejected=result.get("rejected_decisions", []),
    )
    return trade_set


def diff_trade_sets(
    old_set: TradeSet,
    new_set: TradeSet,
    db: Session,
    *,
    portfolio_id: int,
) -> list[TradeDiff]:
    """对比新旧交易集合差异（WP6.4）。

    对每个差异给出原因：
    - member_missing：旧有新无（新逻辑需成员，成员缺失）
    - member_paused：旧有新无（成员暂停）
    - signal_diff：新旧信号不同
    - data_expired：新逻辑数据过期拒绝
    - risk_blocked：新逻辑风控阻断
    """
    diffs: list[TradeDiff] = []

    # 构建索引：symbol_id + side → 记录
    old_buys = {(b.get("symbol_id"), "buy"): b for b in old_set.buys}
    new_buys = {(b.get("symbol_id"), "buy"): b for b in new_set.buys}
    old_sells = {(s.get("symbol_id"), "sell"): s for s in old_set.sells}
    new_sells = {(s.get("symbol_id"), "sell"): s for s in new_set.sells}

    all_keys = (
        set(old_buys.keys())
        | set(new_buys.keys())
        | set(old_sells.keys())
        | set(new_sells.keys())
    )

    for key in all_keys:
        symbol_id, side = key
        old = old_buys.get(key) or old_sells.get(key)
        new = new_buys.get(key) or new_sells.get(key)

        old_action = old.get("action") if old else None
        new_action = new.get("action") if new else None

        if old_action == new_action:
            continue  # 无差异

        # 分析差异原因
        reason, detail = _analyze_diff_reason(
            db,
            portfolio_id=portfolio_id,
            symbol_id=symbol_id,
            side=side,
            old_present=old is not None,
            new_present=new is not None,
            new_rejection=new.get("rejection_code") if new else None,
        )

        diffs.append(
            TradeDiff(
                symbol_id=symbol_id,
                side=side,
                old_action=old_action,
                new_action=new_action,
                reason=reason,
                detail=detail,
            )
        )

    return diffs


def _analyze_diff_reason(
    db: Session,
    *,
    portfolio_id: int,
    symbol_id: int,
    side: str,
    old_present: bool,
    new_present: bool,
    new_rejection: str | None,
) -> tuple[str, str]:
    """分析差异原因。"""
    # 新逻辑拒绝
    if new_present and new_rejection:
        rejection_upper = new_rejection.upper()
        if "DATA" in rejection_upper:
            return "data_expired", "新逻辑数据过期拒绝"
        if "RISK" in rejection_upper:
            return "risk_blocked", "新逻辑风控阻断"
        return "risk_blocked", f"新逻辑拒绝: {new_rejection}"

    # 旧有新无
    if old_present and not new_present:
        # 检查成员是否存在
        member = db.execute(
            select(PortfolioMember).where(
                and_(
                    PortfolioMember.portfolio_id == portfolio_id,
                    PortfolioMember.symbol_id == symbol_id,
                    PortfolioMember.effective_to.is_(None),
                )
            )
        ).scalars().first()

        if member is None:
            return "member_missing", "新逻辑需 PortfolioMember，但该标的无成员"

        if member.status == "paused":
            return "member_paused", "成员暂停（status=paused），新逻辑不买入"

        if member.execution_mode != "auto":
            return (
                "member_paused",
                f"成员执行模式={member.execution_mode}，非 auto 不自动买入",
            )

        return "signal_diff", "新旧信号不同"

    # 新有旧无
    if new_present and not old_present:
        return "signal_diff", "新逻辑有信号，旧逻辑无"

    return "signal_diff", "信号差异"


def run_dual_trade(
    db: Session,
    *,
    portfolio_id: int,
    save_diff: bool = True,
    dry_run: bool = True,
    buy_candidate_limit: int = 10,
    for_schedule: bool = False,
    require_readiness: bool = True,
) -> dict:
    """统一自动交易执行入口（P0-AutoTrade + WP6.4）。

    统一流程：
        执行锁 → 就绪检查（readiness fail-closed 真实执行）
        → 新旧来源双跑 diff（兼容迁移期）
        → 按 Portfolio.auto_trade_source_mode + env 熔断选择执行来源
        → dry_run 或真实执行 → 释放锁 → 返回扁平化 AutoTradeResult 兼容字段

    Args:
        db: 数据库会话
        portfolio_id: 组合 ID
        save_diff: 是否保存新旧来源差异记录（WP6.4 迁移期保留）
        dry_run: True 仅返回计划不实际下单；False 真实下单
        buy_candidate_limit: 旧扫描来源执行路径的买入侧上限（新成员来源按 auto 成员控制，忽略）
        for_schedule: 是否来自调度入口（影响 schedule_ready 是否升级为 blocker）
        require_readiness: True 时真实执行强制 readiness.ready=True；dry_run 仍会携带诊断
    """
    portfolio: Portfolio | None = db.get(Portfolio, int(portfolio_id))
    if portfolio is None:
        raise ValueError(f"Portfolio {portfolio_id} not found")

    # 1) 组合级执行锁：fail-closed 防止手动+调度/重复提交并发
    if not _acquire_run_lock(int(portfolio_id)):
        raise RuntimeError(
            f"Portfolio {portfolio_id} auto-trade already running. "
            "Please wait for the previous run to finish."
        )
    try:
        return _run_dual_trade_inner(
            db,
            portfolio=portfolio,
            save_diff=save_diff,
            dry_run=bool(dry_run),
            buy_candidate_limit=int(buy_candidate_limit or 0),
            for_schedule=bool(for_schedule),
            require_readiness=bool(require_readiness),
        )
    finally:
        _release_run_lock(int(portfolio_id))


def _iso_now() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=timezone.utc).isoformat()


def _normalize_source_mode(raw: Any) -> tuple[str, bool]:
    value = str(raw or AUTO_TRADE_SOURCE_PORTFOLIO).strip() or AUTO_TRADE_SOURCE_PORTFOLIO
    if value not in AUTO_TRADE_SOURCE_MODES:
        return AUTO_TRADE_SOURCE_PORTFOLIO, False
    return value, True


def _issue_to_dict(issue: ReadinessIssue) -> dict[str, Any]:
    return {
        "code": issue.code,
        "message": issue.message,
        "detail": issue.detail,
    }


def _run_dual_trade_inner(
    db: Session,
    *,
    portfolio: Portfolio,
    save_diff: bool,
    dry_run: bool,
    buy_candidate_limit: int,
    for_schedule: bool,
    require_readiness: bool,
) -> dict:
    portfolio_id = int(portfolio.id)
    extra_warnings: list[ReadinessIssue] = []

    # 2) 来源模式主判定：Portfolio.auto_trade_source_mode 优先，env 仅做紧急熔断参考
    source_mode, source_mode_valid = _normalize_source_mode(
        getattr(portfolio, "auto_trade_source_mode", AUTO_TRADE_SOURCE_PORTFOLIO)
    )
    if not source_mode_valid:
        extra_warnings.append(
            ReadinessIssue(
                code="SOURCE_MODE_INVALID",
                message=f"auto_trade_source_mode 非法，已回退为 {source_mode}",
            )
        )
    if source_mode == AUTO_TRADE_SOURCE_LEGACY_SCAN:
        extra_warnings.append(
            ReadinessIssue(
                code=WARNING_SOURCE_MODE_LEGACY_SCAN,
                message="当前仍使用旧全局扫描来源，建议迁移为 portfolio/members_only",
            )
        )

    env_member_enabled = is_member_source_enabled(portfolio_id)
    # 组合显式要求新来源，但 env 全局开关被紧急熔断时，安全回退为 legacy_scan 并发出强警告
    effective_member_source_enabled: bool
    if source_mode == AUTO_TRADE_SOURCE_LEGACY_SCAN:
        effective_member_source_enabled = bool(env_member_enabled)
    else:
        if not env_member_enabled:
            extra_warnings.append(
                ReadinessIssue(
                    code=WARNING_SOURCE_ENV_OVERRIDE,
                    message="成员来源全局开关已关闭，真实执行会被安全熔断回退为旧扫描逻辑",
                )
            )
            effective_member_source_enabled = False
        else:
            effective_member_source_enabled = True

    executed_source = "new" if effective_member_source_enabled else "old"

    # 3) readiness：dry_run=False 才 fail-closed；dry_run=True 用于诊断，不抛错
    readiness = get_auto_trade_readiness(
        db,
        portfolio_id=portfolio_id,
        for_schedule=for_schedule,
    )
    readiness_dict = readiness_to_dict(readiness)
    merged_warnings = list(readiness.warnings) + extra_warnings
    blockers_to_raise = list(readiness.blockers)
    if (
        require_readiness
        and not dry_run
        and not readiness.ready
    ):
        if not blockers_to_raise:
            blockers_to_raise = [
                ReadinessIssue(
                    code="NOT_READY",
                    message="自动交易未就绪，真实执行已阻断",
                )
            ]
        first_msg = blockers_to_raise[0].message
        raise AutoTradeNotReadyError(first_msg, blockers_to_raise)

    # 4) 双跑 diff（迁移期保留，save_diff=True）
    result: dict[str, Any] = {
        "portfolio_id": portfolio_id,
        "dry_run": bool(dry_run),
        "member_source_enabled": bool(effective_member_source_enabled),
        "source_mode": source_mode,
        "executed_source": executed_source,
        "old_trade_set": None,
        "new_trade_set": None,
        "diffs": [],
        "readiness": readiness_dict,
        "blockers": [_issue_to_dict(b) for b in readiness.blockers],
        "warnings": [_issue_to_dict(w) for w in merged_warnings],
        "errors": [],
        "sells": [],
        "buys": [],
        "executed_at": _iso_now(),
    }
    try:
        old_set = capture_trade_set_from_old_logic(db, portfolio_id=portfolio_id)
        result["old_trade_set"] = old_set.to_dict()
    except Exception as exc:  # noqa: BLE001
        logger.warning("捕获旧来源交易集合失败 portfolio_id=%s", portfolio_id, exc_info=True)
        result["errors"].append(f"old_trade_set capture failed: {exc}")
    try:
        new_set = capture_trade_set_from_new_logic(db, portfolio_id=portfolio_id)
        result["new_trade_set"] = new_set.to_dict()
    except Exception as exc:  # noqa: BLE001
        logger.warning("捕获新来源交易集合失败 portfolio_id=%s", portfolio_id, exc_info=True)
        result["errors"].append(f"new_trade_set capture failed: {exc}")
    if save_diff and result["old_trade_set"] is not None and result["new_trade_set"] is not None:
        try:
            diffs = diff_trade_sets(old_set, new_set, db, portfolio_id=portfolio_id)
            result["diffs"] = [
                {
                    "symbol_id": d.symbol_id,
                    "side": d.side,
                    "old_action": d.old_action,
                    "new_action": d.new_action,
                    "reason": d.reason,
                    "detail": d.detail,
                }
                for d in diffs
            ]
        except Exception as exc:  # noqa: BLE001
            logger.warning("diff 交易集合失败 portfolio_id=%s", portfolio_id, exc_info=True)
            result["errors"].append(f"diff failed: {exc}")

    # 5) 实际执行（或 dry-run 计划）
    exec_result: dict[str, Any] = {}
    try:
        if effective_member_source_enabled:
            from app.services.auto_trade_member_source import run_idempotent_member_source

            exec_result = run_idempotent_member_source(
                db,
                portfolio_id=portfolio_id,
                dry_run=bool(dry_run),
            )
        else:
            from app.services.auto_trade_task import run_auto_trade

            exec_result = run_auto_trade(
                db,
                portfolio_id=portfolio_id,
                dry_run=bool(dry_run),
                buy_candidate_limit=int(buy_candidate_limit or 10),
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("执行来源失败 portfolio_id=%s source=%s", portfolio_id, executed_source)
        result["errors"].append(f"execution failed: {exc}")
        exec_result = {}

    result["execution_result"] = exec_result

    # 6) 扁平化：为 AutoTradeResult 兼容 schema 提供 sells/buys/errors
    if exec_result:
        if "sells" in exec_result and isinstance(exec_result["sells"], list):
            result["sells"] = list(exec_result["sells"])
            result["buys"] = list(exec_result.get("buys", []) or [])
            result["errors"] = list(result["errors"]) + list(exec_result.get("errors", []) or [])
            if "executed_at" in exec_result and exec_result["executed_at"]:
                result["executed_at"] = str(exec_result["executed_at"])
        else:
            # 新来源（member_source）结构：sell_decisions / buy_decisions / rejected_decisions / executed_orders
            def _to_plan_item(decision: dict[str, Any], side: str) -> dict[str, Any]:
                item: dict[str, Any] = {
                    "symbol_id": int(decision.get("symbol_id") or 0),
                    "symbol": str(decision.get("symbol") or ""),
                    "name": str(decision.get("name") or ""),
                    "action": str(decision.get("action") or side),
                    "stage": decision.get("stage"),
                    "ref_price": float(decision.get("ref_price") or 0),
                    "executed": bool(decision.get("executed") or False),
                    "order_id": decision.get("order_id"),
                    "filled_price": decision.get("filled_price"),
                    "fee": decision.get("fee"),
                    "reason": decision.get("reason"),
                    "rejection_code": decision.get("rejection_code"),
                    "rejection_detail": decision.get("rejection_detail"),
                    "decision": decision.get("decision"),
                }
                if side == "sell":
                    item["held_quantity"] = decision.get("held_quantity")
                    item["sell_quantity"] = decision.get("sell_quantity")
                else:
                    item["can_open"] = decision.get("can_open")
                    item["blocked_reasons"] = list(decision.get("blocked_reasons") or [])
                    item["recommended_amount"] = decision.get("recommended_amount")
                    item["buy_quantity"] = decision.get("buy_quantity")
                return item

            result["sells"] = [
                _to_plan_item(d, "sell")
                for d in list(exec_result.get("sell_decisions", []) or [])
            ]
            result["buys"] = [
                _to_plan_item(d, "buy")
                for d in list(exec_result.get("buy_decisions", []) or [])
            ]
            rejected = list(exec_result.get("rejected_decisions", []) or [])
            for rej in rejected:
                reason = " | ".join(
                    x for x in [
                        str(rej.get("rejection_code") or ""),
                        str(rej.get("rejection_detail") or ""),
                    ] if x
                )
                if reason:
                    result["errors"].append(reason)
            result["errors"] = list(result["errors"]) + list(exec_result.get("errors", []) or [])
            last_time = exec_result.get("executed_at")
            if last_time:
                result["executed_at"] = str(last_time)

    return result


def save_diff_record(
    db: Session,
    *,
    portfolio_id: int,
    diffs: list[TradeDiff],
) -> None:
    """保存差异记录（WP6.4）。

    用于连续 5 个交易日或 3 次有效运行的差异追踪。
    """
    # TODO: 创建差异记录表（如需持久化）
    # 第一阶段：日志记录
    for diff in diffs:
        logger.info(
            "WP6.4 差异 portfolio_id=%s symbol_id=%s side=%s old=%s new=%s "
            "reason=%s detail=%s",
            portfolio_id,
            diff.symbol_id,
            diff.side,
            diff.old_action,
            diff.new_action,
            diff.reason,
            diff.detail,
        )


def can_switch_to_member_source(
    db: Session,
    *,
    portfolio_id: int,
    min_runs: int = 3,
    max_diff_ratio: float = 0.1,
) -> tuple[bool, str]:
    """检查是否可以切换到新来源（WP6.4）。

    条件：
    - 至少 3 次有效运行
    - 差异比例 < max_diff_ratio（10%）
    - 差异已人工确认
    """
    # TODO: 查询历史差异记录
    # 第一阶段：返回 True（占位）
    return True, "第一阶段默认允许切换"


def rollback_to_old_source(portfolio_id: int | None = None) -> None:
    """回退到旧来源（WP6.4）。

    关闭新来源开关，立即回退。
    """
    # 清除组合级白名单
    if portfolio_id is not None:
        whitelist = os.environ.get("AUTO_TRADE_MEMBER_SOURCE_PORTFOLIOS", "")
        if whitelist:
            ids = [
                x.strip()
                for x in whitelist.split(",")
                if x.strip() and x.strip() != str(portfolio_id)
            ]
            if ids:
                os.environ["AUTO_TRADE_MEMBER_SOURCE_PORTFOLIOS"] = ",".join(ids)
            else:
                os.environ.pop("AUTO_TRADE_MEMBER_SOURCE_PORTFOLIOS", None)

    # 全局关闭
    os.environ[ENV_FLAG] = "false"
    logger.info("已回退到旧来源 portfolio_id=%s", portfolio_id)


__all__ = [
    "ENV_FLAG",
    "TradeDiff",
    "TradeSet",
    "can_switch_to_member_source",
    "capture_trade_set_from_new_logic",
    "capture_trade_set_from_old_logic",
    "diff_trade_sets",
    "is_member_source_enabled",
    "rollback_to_old_source",
    "run_dual_trade",
    "save_diff_record",
]
