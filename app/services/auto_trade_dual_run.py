"""双跑切换（WP6.4）。

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
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.portfolio_member import PortfolioMember


logger = logging.getLogger(__name__)


# 环境变量开关
ENV_FLAG = "AUTO_TRADE_MEMBER_SOURCE_ENABLED"


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
) -> dict:
    """运行双跑（WP6.4 主入口）。

    根据开关决定实际执行来源：
    - 新来源启用：新逻辑实际执行，旧逻辑 Dry Run
    - 新来源未启用：旧逻辑实际执行，新逻辑 Dry Run

    同时捕获差异（如果 save_diff=True）。
    """
    member_source_enabled = is_member_source_enabled(portfolio_id)

    result = {
        "portfolio_id": portfolio_id,
        "member_source_enabled": member_source_enabled,
        "old_trade_set": None,
        "new_trade_set": None,
        "diffs": [],
        "executed_source": "new" if member_source_enabled else "old",
    }

    # 捕获旧逻辑交易集合（Dry Run）
    old_set = capture_trade_set_from_old_logic(db, portfolio_id=portfolio_id)
    result["old_trade_set"] = old_set.to_dict()

    # 捕获新逻辑交易集合（Dry Run）
    new_set = capture_trade_set_from_new_logic(db, portfolio_id=portfolio_id)
    result["new_trade_set"] = new_set.to_dict()

    # 计算差异
    if save_diff:
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

    # 实际执行
    if member_source_enabled:
        # 新来源实际执行
        from app.services.auto_trade_member_source import run_idempotent_member_source

        exec_result = run_idempotent_member_source(db, portfolio_id=portfolio_id)
        result["execution_result"] = exec_result
    else:
        # 旧来源实际执行（dry_run=False 实际下单）
        from app.services.auto_trade_task import run_auto_trade

        exec_result = run_auto_trade(db, portfolio_id=portfolio_id, dry_run=False)
        result["execution_result"] = exec_result

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
