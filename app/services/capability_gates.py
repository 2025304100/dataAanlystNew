"""功能就绪状态门禁服务（WP-S.7 前置条件引导与页面门禁）。

提供 `GET /api/v1/system/capabilities` 聚合接口所需的 8 个域判定函数与 1 个聚合函数。
每个 `check_*` 函数签名 `(db: Session) -> CapabilityStatus`，独立可测试，
全程 best-effort：单个 check 异常不阻塞聚合，对应 capability 状态置 blocked 并 reason="check_failed"。

覆盖域：
- 基础数据采集 `check_market_data_capability`
- 评分配置激活 `check_scoring_capability`
- Ridge 因子仓库与活动模型 `check_factor_ridge_capability`
- 机会扫描快照 `check_discovery_capability`
- 组合操作前 `check_portfolio_capability`
- 自动交易前 `check_auto_trade_capability`
- AI 配置 `check_ai_capability`
- 外部消息渠道 `check_message_channel_capability`

聚合：`get_all_capabilities(db) -> CapabilitiesResponse`
- overall_status：所有 ready → "ready"；任一 blocked → "blocked"；否则 "degraded"
- 单个 check 异常被捕获，对应 capability 标记为 blocked + reason="check_failed"
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, inspect, select
from sqlalchemy.orm import Session

from app.models.daily_bar import DailyBar
from app.models.discovery import DiscoveryTaskRecord
from app.models.factor_runtime import FactorRuntimeState
from app.models.portfolio import Portfolio, PortfolioRule, Position
from app.models.scan import ScanResult
from app.models.score import Score
from app.models.scoring_config import ScoringConfig
from app.models.signal_rule import SignalRule
from app.models.sim_account import CashLedger, SimOrder
from app.models.symbol import Symbol
from app.schemas.capability import (
    CapabilityAction,
    CapabilityPrerequisite,
    CapabilityStatus,
    CapabilitiesResponse,
)

logger = logging.getLogger(__name__)


# ── 工具函数 ───────────────────────────────────────────────


def _utcnow_naive() -> datetime:
    """返回当前 UTC 时间（无时区），与现有 system.py 的 _now() 保持一致。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse_date(value: Any) -> date | None:
    """将可能是字符串的 date 值安全转为 date 对象。"""
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(value, fmt).date()
            except ValueError:
                continue
    return None


def _parse_datetime(value: Any) -> datetime | None:
    """将可能是字符串的 datetime 值安全转为 datetime 对象。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%S.%f",
        ):
            try:
                return datetime.strptime(value, fmt)
            except ValueError:
                continue
        # 兼容带时区后缀（Z 或 +HH:MM）的 ISO 字符串：截断后再尝试解析
        if value.endswith("Z"):
            stripped = value[:-1]
            for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
                try:
                    return datetime.strptime(stripped, fmt)
                except ValueError:
                    continue
    return None


def _age_days(value: Any) -> int | None:
    """计算 value 距今天数（None 时返回 None）。"""
    if value is None:
        return None
    parsed = _parse_date(value) if not isinstance(value, (date, datetime)) else value
    if isinstance(parsed, datetime):
        parsed = parsed.date()
    if not isinstance(parsed, date):
        return None
    today = _utcnow_naive().date()
    return max(0, (today - parsed).days)


def _has_table(db: Session, table_name: str) -> bool:
    """检查表是否存在于当前数据库（best-effort，失败返回 False）。"""
    try:
        inspector = inspect(db.bind)
        return table_name in inspector.get_table_names()
    except Exception:
        return False


def _to_datetime(value: Any) -> datetime | None:
    """将 date/datetime/str 转换为 datetime（date 自动补 00:00:00）。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    parsed_dt = _parse_datetime(value)
    if parsed_dt is not None:
        return parsed_dt
    parsed_date = _parse_date(value)
    if parsed_date is not None:
        return datetime(parsed_date.year, parsed_date.month, parsed_date.day)
    return None


# ── 2.1 基础数据采集 ────────────────────────────────────────


def check_market_data_capability(db: Session) -> CapabilityStatus:
    """基础数据采集就绪状态。

    判定依据：
    - 标的库总数（is_active=1）
    - 行情最新交易日
    - 行情覆盖率（covered_symbols / total_symbols）
    - 过期标的数（超过 7 天未更新）

    状态规则：
    - total=0 → blocked, reason="symbols_empty"
    - coverage_pct < 50 → blocked, reason="low_bar_coverage"
    - coverage_pct < 80 → degraded, reason="partial_bar_coverage"
    - latest_trade_date 距今 > 3 天 → degraded, reason="stale_bars"
    - 否则 → ready
    """
    now = _utcnow_naive()
    today = now.date()
    stale_cutoff = today - timedelta(days=7)

    total_symbols = db.execute(
        select(func.count(Symbol.id)).where(Symbol.is_active == 1)
    ).scalar_one()

    latest_bar_subq = (
        select(DailyBar.symbol_id, func.max(DailyBar.trade_date).label("latest_trade_date"))
        .group_by(DailyBar.symbol_id)
        .subquery()
    )
    latest_trade_date = _parse_date(
        db.execute(select(func.max(DailyBar.trade_date))).scalar_one()
    )
    covered_symbols = db.execute(
        select(func.count(Symbol.id))
        .join(latest_bar_subq, latest_bar_subq.c.symbol_id == Symbol.id)
        .where(Symbol.is_active == 1)
    ).scalar_one()
    coverage_pct = round((covered_symbols / total_symbols) * 100, 2) if total_symbols else 0.0

    outdated_bar_symbols = db.execute(
        select(func.count(Symbol.id))
        .join(latest_bar_subq, latest_bar_subq.c.symbol_id == Symbol.id)
        .where(Symbol.is_active == 1, latest_bar_subq.c.latest_trade_date < stale_cutoff)
    ).scalar_one() if total_symbols else 0

    symbols_initialized = total_symbols > 0
    bars_fresh = (
        latest_trade_date is not None
        and (_age_days(latest_trade_date) is None or _age_days(latest_trade_date) <= 3)
    )

    prerequisites = [
        CapabilityPrerequisite(
            key="symbols_initialized",
            label="标的库已初始化",
            satisfied=symbols_initialized,
            detail=f"当前活跃标的 {total_symbols} 个" if symbols_initialized else "标的库为空",
        ),
        CapabilityPrerequisite(
            key="bars_fresh",
            label="行情数据已同步且较新",
            satisfied=bars_fresh,
            detail=(
                f"最新交易日 {latest_trade_date.isoformat()}（覆盖 {coverage_pct:.1f}%）"
                if latest_trade_date is not None
                else "尚未同步任何行情"
            ),
        ),
    ]
    recommended_actions = [
        CapabilityAction(
            label="去同步行情",
            action_type="redirect",
            target="/market-data",
            reason="补全标的库与最新 K 线",
        )
    ]

    data_cutoff_at = _to_datetime(latest_trade_date)

    if total_symbols == 0:
        return CapabilityStatus(
            key="market_data",
            label="基础数据采集",
            status="blocked",
            reason_code="symbols_empty",
            user_message="标的库为空，请先初始化全市场标的再同步行情。",
            prerequisites=prerequisites,
            recommended_actions=recommended_actions,
            data_cutoff_at=data_cutoff_at,
            last_checked_at=now,
        )

    if coverage_pct < 50:
        return CapabilityStatus(
            key="market_data",
            label="基础数据采集",
            status="blocked",
            reason_code="low_bar_coverage",
            user_message=f"行情覆盖率仅 {coverage_pct:.1f}%，超过半数标的无 K 线，无法支持扫描。",
            prerequisites=prerequisites,
            recommended_actions=recommended_actions,
            data_cutoff_at=data_cutoff_at,
            last_checked_at=now,
        )

    if coverage_pct < 80:
        return CapabilityStatus(
            key="market_data",
            label="基础数据采集",
            status="degraded",
            reason_code="partial_bar_coverage",
            user_message=(
                f"行情覆盖率 {coverage_pct:.1f}%，部分标的缺少 K 线，"
                f"扫描结果可能不完整（数据截至 {latest_trade_date}）。"
            ),
            prerequisites=prerequisites,
            recommended_actions=recommended_actions,
            data_cutoff_at=data_cutoff_at,
            last_checked_at=now,
        )

    bar_age_days = _age_days(latest_trade_date)
    if bar_age_days is None or bar_age_days > 3:
        age_display = bar_age_days if bar_age_days is not None else "未知"
        return CapabilityStatus(
            key="market_data",
            label="基础数据采集",
            status="degraded",
            reason_code="stale_bars",
            user_message=(
                f"最新行情日期 {latest_trade_date}，距今 {age_display} 天，"
                f"建议补拉最近 5 个交易日数据（数据截至 {latest_trade_date}）。"
            ),
            prerequisites=prerequisites,
            recommended_actions=recommended_actions,
            data_cutoff_at=data_cutoff_at,
            last_checked_at=now,
        )

    return CapabilityStatus(
        key="market_data",
        label="基础数据采集",
        status="ready",
        reason_code=None,
        user_message=(
            f"基础数据正常：{total_symbols} 只活跃标的，行情覆盖率 {coverage_pct:.1f}%，"
            f"最新交易日 {latest_trade_date}。"
        ),
        prerequisites=prerequisites,
        recommended_actions=recommended_actions,
        data_cutoff_at=data_cutoff_at,
        last_checked_at=now,
    )


# ── 2.2 评分配置激活 ────────────────────────────────────────


def check_scoring_capability(db: Session) -> CapabilityStatus:
    """评分配置激活就绪状态。

    判定依据：
    - ScoringConfig where is_active=1 的数量
    - Score max(trade_date)
    - 已评分标的数

    状态规则：
    - 无 active config → blocked, reason="no_active_scoring_config"
    - 已有 active config 但无评分 → degraded, reason="scores_not_computed"
    - 评分距今 > 7 天 → degraded, reason="stale_scores"
    - 否则 → ready
    """
    now = _utcnow_naive()

    active_config_count = db.execute(
        select(func.count(ScoringConfig.id)).where(ScoringConfig.is_active == 1)
    ).scalar_one()

    latest_score_date = _parse_date(
        db.execute(select(func.max(Score.trade_date))).scalar_one()
    )
    scored_symbols = db.execute(
        select(func.count(func.distinct(Score.symbol_id)))
        .join(Symbol, Symbol.id == Score.symbol_id)
        .where(Symbol.is_active == 1)
    ).scalar_one()

    scoring_config_active = active_config_count > 0
    market_data_ready = scored_symbols > 0 and latest_score_date is not None

    prerequisites = [
        CapabilityPrerequisite(
            key="scoring_config_active",
            label="已激活评分配置",
            satisfied=scoring_config_active,
            detail=f"当前激活配置 {active_config_count} 套" if scoring_config_active else "尚未激活评分配置",
        ),
        CapabilityPrerequisite(
            key="market_data_ready",
            label="基础数据与评分已生成",
            satisfied=market_data_ready,
            detail=(
                f"已评分 {scored_symbols} 只标的，最新评分日 {latest_score_date}"
                if market_data_ready
                else "尚未计算任何评分"
            ),
        ),
    ]
    recommended_actions = [
        CapabilityAction(
            label="去评分配置",
            action_type="redirect",
            target="/settings/scoring",
            reason="激活评分配置或重新计算评分",
        )
    ]
    data_cutoff_at = _to_datetime(latest_score_date)

    if active_config_count == 0:
        return CapabilityStatus(
            key="scoring",
            label="评分配置激活",
            status="blocked",
            reason_code="no_active_scoring_config",
            user_message="尚未激活评分配置，请先在设置中激活一套评分预设。",
            prerequisites=prerequisites,
            recommended_actions=recommended_actions,
            data_cutoff_at=data_cutoff_at,
            last_checked_at=now,
        )

    if latest_score_date is None or scored_symbols == 0:
        return CapabilityStatus(
            key="scoring",
            label="评分配置激活",
            status="degraded",
            reason_code="scores_not_computed",
            user_message="已激活评分配置，但尚未计算评分。请运行一次评分计算。",
            prerequisites=prerequisites,
            recommended_actions=recommended_actions,
            data_cutoff_at=data_cutoff_at,
            last_checked_at=now,
        )

    score_age_days = _age_days(latest_score_date) or 0
    if score_age_days > 7:
        return CapabilityStatus(
            key="scoring",
            label="评分配置激活",
            status="degraded",
            reason_code="stale_scores",
            user_message=(
                f"最新评分日期 {latest_score_date}，距今 {score_age_days} 天，"
                f"建议重新计算评分（数据截至 {latest_score_date}）。"
            ),
            prerequisites=prerequisites,
            recommended_actions=recommended_actions,
            data_cutoff_at=data_cutoff_at,
            last_checked_at=now,
        )

    return CapabilityStatus(
        key="scoring",
        label="评分配置激活",
        status="ready",
        reason_code=None,
        user_message=(
            f"评分配置正常：{active_config_count} 套激活，"
            f"已评分 {scored_symbols} 只标的，最新评分日 {latest_score_date}。"
        ),
        prerequisites=prerequisites,
        recommended_actions=recommended_actions,
        data_cutoff_at=data_cutoff_at,
        last_checked_at=now,
    )


# ── 2.3 Ridge 因子仓库与活动模型 ────────────────────────────


def check_factor_ridge_capability(db: Session) -> CapabilityStatus:
    """Ridge 因子仓库与活动模型就绪状态。

    判定依据：
    - FactorWarehouse.health() 是否 available 且 raw_daily_bars > 0
    - FactorRuntimeState.active_model_run_id 是否非空

    状态规则：
    - 仓库为空 → blocked, reason="factor_warehouse_empty"
    - 仓库有数据但无活动模型 → degraded, reason="no_active_factor_model"
    - 否则 → ready

    注意：本函数 best-effort 处理 DuckDB 不可用情况，将其视为仓库为空。
    """
    now = _utcnow_naive()

    # 检查因子仓库健康状态
    warehouse_available = False
    warehouse_bars = 0
    warehouse_latest: str | None = None
    warehouse_error: str | None = None
    try:
        from app.services.factors.store import FactorWarehouse

        warehouse = FactorWarehouse()
        health = warehouse.health()
        warehouse_available = bool(health.available)
        warehouse_bars = int(health.raw_daily_bars or 0)
        warehouse_latest = health.latest_trade_date
        warehouse_error = health.error
    except Exception as exc:
        # DuckDB 未安装或仓库未初始化都视为仓库为空
        warehouse_error = f"{type(exc).__name__}: {exc}"
        logger.debug("Factor warehouse health check failed: %s", warehouse_error)

    # 检查活动因子模型
    active_model_run_id: str | None = None
    try:
        runtime_state = db.execute(
            select(FactorRuntimeState).where(FactorRuntimeState.id == 1)
        ).scalars().first()
        if runtime_state is not None:
            active_model_run_id = runtime_state.active_model_run_id
    except Exception as exc:
        logger.debug("Factor runtime state query failed: %s", exc)

    factor_warehouse_initialized = warehouse_available and warehouse_bars > 0
    active_factor_model = active_model_run_id is not None

    prerequisites = [
        CapabilityPrerequisite(
            key="factor_warehouse_initialized",
            label="因子仓库已初始化",
            satisfied=factor_warehouse_initialized,
            detail=(
                f"已加载 {warehouse_bars} 条原始 K 线，最新日期 {warehouse_latest}"
                if factor_warehouse_initialized
                else (
                    f"仓库未就绪：{warehouse_error}"
                    if warehouse_error
                    else "仓库尚未初始化或为空"
                )
            ),
        ),
        CapabilityPrerequisite(
            key="active_factor_model",
            label="已激活因子模型",
            satisfied=active_factor_model,
            detail=(
                f"活动模型运行 ID：{active_model_run_id}"
                if active_factor_model
                else "尚未激活因子模型（运行时仍使用 manual 权重模式）"
            ),
        ),
    ]
    recommended_actions = [
        CapabilityAction(
            label="去因子配置",
            action_type="redirect",
            target="/factors",
            reason="初始化因子仓库或激活因子模型",
        )
    ]

    data_cutoff_at = _to_datetime(warehouse_latest) if warehouse_latest else None

    if not factor_warehouse_initialized:
        return CapabilityStatus(
            key="factor_ridge",
            label="Ridge 因子仓库",
            status="blocked",
            reason_code="factor_warehouse_empty",
            user_message="因子仓库为空，请先运行因子数据流水线初始化仓库。",
            prerequisites=prerequisites,
            recommended_actions=recommended_actions,
            data_cutoff_at=data_cutoff_at,
            last_checked_at=now,
        )

    if not active_factor_model:
        return CapabilityStatus(
            key="factor_ridge",
            label="Ridge 因子仓库",
            status="degraded",
            reason_code="no_active_factor_model",
            user_message=(
                f"因子仓库已就绪（{warehouse_bars} 条 K 线，最新 {warehouse_latest}），"
                f"但未激活因子模型，当前使用 manual 权重模式。"
            ),
            prerequisites=prerequisites,
            recommended_actions=recommended_actions,
            data_cutoff_at=data_cutoff_at,
            last_checked_at=now,
        )

    return CapabilityStatus(
        key="factor_ridge",
        label="Ridge 因子仓库",
        status="ready",
        reason_code=None,
        user_message=(
            f"因子仓库就绪：{warehouse_bars} 条 K 线，最新日期 {warehouse_latest}，"
            f"活动模型 {active_model_run_id}。"
        ),
        prerequisites=prerequisites,
        recommended_actions=recommended_actions,
        data_cutoff_at=data_cutoff_at,
        last_checked_at=now,
    )


# ── 2.4 机会扫描快照 ────────────────────────────────────────


def check_discovery_capability(db: Session) -> CapabilityStatus:
    """机会扫描快照就绪状态。

    判定依据：
    - 最近一次 DiscoveryTaskRecord（status / updated_at）
    - ScanResult 数量、warning/expired/frozen 分布

    状态规则：
    - 无扫描历史 → blocked, reason="no_scan_history"
    - 最近扫描 failed → degraded, reason="last_scan_failed"
    - 全部扫描结果已过期 → degraded, reason="all_results_expired"
    - 否则 → ready
    """
    from app.db.dialect import days_since

    now = _utcnow_naive()

    latest_task = db.execute(
        select(DiscoveryTaskRecord).order_by(DiscoveryTaskRecord.updated_at.desc())
    ).scalars().first()

    total_results = db.execute(select(func.count(ScanResult.id))).scalar_one()
    frozen_results = db.execute(
        select(func.count(ScanResult.id)).where(ScanResult.is_frozen == 1)
    ).scalar_one()
    warning_results = db.execute(
        select(func.count(ScanResult.id)).where(
            ScanResult.is_frozen == 0,
            days_since(ScanResult.created_at) >= ScanResult.warning_days,
            days_since(ScanResult.created_at) < ScanResult.valid_days,
        )
    ).scalar_one()
    expired_results = db.execute(
        select(func.count(ScanResult.id)).where(
            ScanResult.is_frozen == 0,
            days_since(ScanResult.created_at) >= ScanResult.valid_days,
        )
    ).scalar_one()

    market_data_ready = latest_task is not None
    scoring_ready = total_results > 0

    prerequisites = [
        CapabilityPrerequisite(
            key="market_data_ready",
            label="基础数据与扫描任务已存在",
            satisfied=market_data_ready,
            detail=(
                f"最近扫描任务 {latest_task.id}（{latest_task.status}）"
                if market_data_ready
                else "尚未运行过扫描任务"
            ),
        ),
        CapabilityPrerequisite(
            key="scoring_ready",
            label="已有扫描结果",
            satisfied=scoring_ready,
            detail=(
                f"扫描结果 {total_results} 条（冻结 {frozen_results} / 预警 {warning_results} / 过期 {expired_results}）"
                if scoring_ready
                else "尚无扫描结果"
            ),
        ),
    ]
    recommended_actions = [
        CapabilityAction(
            label="去机会扫描",
            action_type="redirect",
            target="/discovery",
            reason="运行机会扫描或查看历史扫描记录",
        )
    ]

    data_cutoff_at = (
        latest_task.updated_at if latest_task and latest_task.updated_at else None
    )

    if latest_task is None and total_results == 0:
        return CapabilityStatus(
            key="discovery",
            label="机会扫描快照",
            status="blocked",
            reason_code="no_scan_history",
            user_message="尚未运行过机会扫描，请先完成基础数据准备后运行一次扫描。",
            prerequisites=prerequisites,
            recommended_actions=recommended_actions,
            data_cutoff_at=data_cutoff_at,
            last_checked_at=now,
        )

    if latest_task is not None and latest_task.status in {"failed", "cancelled", "expired"}:
        return CapabilityStatus(
            key="discovery",
            label="机会扫描快照",
            status="degraded",
            reason_code="last_scan_failed",
            user_message=(
                f"最近一次扫描状态为 {latest_task.status}，建议检查任务日志后重试。"
                f"{'已有历史结果可用作参考。' if total_results > 0 else ''}"
            ),
            prerequisites=prerequisites,
            recommended_actions=recommended_actions,
            data_cutoff_at=data_cutoff_at,
            last_checked_at=now,
        )

    # 全部结果已过期（无冻结、无有效、无预警）
    if total_results > 0 and (frozen_results + warning_results) == 0 and expired_results > 0:
        return CapabilityStatus(
            key="discovery",
            label="机会扫描快照",
            status="degraded",
            reason_code="all_results_expired",
            user_message=(
                f"{expired_results} 条扫描结果已全部过期，建议重新扫描获取最新候选。"
            ),
            prerequisites=prerequisites,
            recommended_actions=recommended_actions,
            data_cutoff_at=data_cutoff_at,
            last_checked_at=now,
        )

    if latest_task is None and total_results > 0:
        # 没有任务记录但有扫描结果（兼容旧数据）
        return CapabilityStatus(
            key="discovery",
            label="机会扫描快照",
            status="ready",
            reason_code=None,
            user_message=f"已有 {total_results} 条扫描结果可用（冻结 {frozen_results} / 预警 {warning_results} / 过期 {expired_results}）。",
            prerequisites=prerequisites,
            recommended_actions=recommended_actions,
            data_cutoff_at=data_cutoff_at,
            last_checked_at=now,
        )

    return CapabilityStatus(
        key="discovery",
        label="机会扫描快照",
        status="ready",
        reason_code=None,
        user_message=(
            f"扫描就绪：最近任务 {latest_task.id}（{latest_task.status}），"
            f"候选结果 {total_results} 条（冻结 {frozen_results} / 预警 {warning_results} / 过期 {expired_results}）。"
        ),
        prerequisites=prerequisites,
        recommended_actions=recommended_actions,
        data_cutoff_at=data_cutoff_at,
        last_checked_at=now,
    )


# ── 2.5 组合操作前 ──────────────────────────────────────────


def check_portfolio_capability(db: Session) -> CapabilityStatus:
    """组合操作前就绪状态。

    判定依据：
    - Portfolio 数量
    - 组合成员数（Position 表）
    - 组合规则数（PortfolioRule 表）
    - 模拟账户数（CashLedger 视为账户活动；SimOrder 视为模拟账户）

    状态规则：
    - 无组合 → blocked, reason="no_portfolio"
    - 组合无成员 → blocked, reason="portfolio_empty"
    - 无模拟账户 → degraded, reason="no_sim_account"
    - 否则 → ready

    注意：portfolio_members 表在 WP4 才新增，这里基于 Position 表判定成员。
    """
    now = _utcnow_naive()

    portfolio_count = db.execute(select(func.count(Portfolio.id))).scalar_one()
    position_count = db.execute(select(func.count(Position.id))).scalar_one()
    active_rule_count = db.execute(
        select(func.count(PortfolioRule.id)).where(PortfolioRule.is_active == 1)
    ).scalar_one()
    ledger_count = db.execute(select(func.count(CashLedger.id))).scalar_one()
    sim_order_count = db.execute(select(func.count(SimOrder.id))).scalar_one()

    portfolio_exists = portfolio_count > 0
    portfolio_has_members = position_count > 0
    sim_account_exists = ledger_count > 0 or sim_order_count > 0

    prerequisites = [
        CapabilityPrerequisite(
            key="portfolio_exists",
            label="已创建组合",
            satisfied=portfolio_exists,
            detail=f"已创建 {portfolio_count} 个组合" if portfolio_exists else "尚未创建组合",
        ),
        CapabilityPrerequisite(
            key="portfolio_has_members",
            label="组合有持仓/成员",
            satisfied=portfolio_has_members,
            detail=f"已持仓 {position_count} 笔" if portfolio_has_members else "组合无任何持仓",
        ),
        CapabilityPrerequisite(
            key="sim_account_exists",
            label="已配置模拟账户",
            satisfied=sim_account_exists,
            detail=(
                f"已记账 {ledger_count} 笔 / 已下模拟单 {sim_order_count} 笔"
                if sim_account_exists
                else "尚无模拟账户活动记录"
            ),
        ),
    ]
    recommended_actions = [
        CapabilityAction(
            label="去组合",
            action_type="redirect",
            target="/portfolio",
            reason="创建组合或添加成员",
        )
    ]

    data_cutoff_at = None

    if portfolio_count == 0:
        return CapabilityStatus(
            key="portfolio",
            label="组合操作前",
            status="blocked",
            reason_code="no_portfolio",
            user_message="尚未创建组合，请先在组合交易页新建一个组合。",
            prerequisites=prerequisites,
            recommended_actions=recommended_actions,
            data_cutoff_at=data_cutoff_at,
            last_checked_at=now,
        )

    if position_count == 0:
        return CapabilityStatus(
            key="portfolio",
            label="组合操作前",
            status="blocked",
            reason_code="portfolio_empty",
            user_message=f"已创建 {portfolio_count} 个组合，但尚无持仓/成员。请先添加成员或导入持仓。",
            prerequisites=prerequisites,
            recommended_actions=recommended_actions,
            data_cutoff_at=data_cutoff_at,
            last_checked_at=now,
        )

    if not sim_account_exists:
        return CapabilityStatus(
            key="portfolio",
            label="组合操作前",
            status="degraded",
            reason_code="no_sim_account",
            user_message=(
                f"组合与成员已就绪（{portfolio_count} 组合 / {position_count} 持仓 / {active_rule_count} 规则），"
                f"但尚无模拟账户活动记录，建议初始化账户现金。"
            ),
            prerequisites=prerequisites,
            recommended_actions=recommended_actions,
            data_cutoff_at=data_cutoff_at,
            last_checked_at=now,
        )

    return CapabilityStatus(
        key="portfolio",
        label="组合操作前",
        status="ready",
        reason_code=None,
        user_message=(
            f"组合就绪：{portfolio_count} 个组合 / {position_count} 笔持仓 / "
            f"{active_rule_count} 条激活规则 / {ledger_count} 笔现金记录。"
        ),
        prerequisites=prerequisites,
        recommended_actions=recommended_actions,
        data_cutoff_at=data_cutoff_at,
        last_checked_at=now,
    )


# ── 2.6 自动交易前 ──────────────────────────────────────────


def check_auto_trade_capability(db: Session) -> CapabilityStatus:
    """自动交易前就绪状态。

    判定依据：
    - Portfolio.auto_trade_enabled
    - 基础数据健康（调用 check_market_data_capability 的 prerequisites）
    - 规则配置完整性（PortfolioRule + SignalRule）
    - 模拟账户余额（CashLedger.balance_after > 0）

    状态规则：
    - 无组合 → blocked, reason="no_portfolio"
    - 组合未开启 auto_trade → blocked, reason="auto_trade_disabled"
    - 数据不健康 → degraded, reason="data_unhealthy"
    - 规则不完整 → degraded, reason="rules_incomplete"
    - 否则 → ready
    """
    now = _utcnow_naive()

    portfolio_count = db.execute(select(func.count(Portfolio.id))).scalar_one()
    auto_trade_portfolios = db.execute(
        select(func.count(Portfolio.id)).where(Portfolio.auto_trade_enabled == 1)
    ).scalar_one()

    # 数据健康（简化版：检查最新行情日期）
    latest_trade_date = _parse_date(
        db.execute(select(func.max(DailyBar.trade_date))).scalar_one()
    )
    bar_age_days = _age_days(latest_trade_date)
    data_healthy = bar_age_days is not None and bar_age_days <= 3

    # 规则完整性：组合规则 + 信号规则
    active_rule_count = db.execute(
        select(func.count(PortfolioRule.id)).where(PortfolioRule.is_active == 1)
    ).scalar_one()
    active_signal_rule_count = db.execute(
        select(func.count(SignalRule.id)).where(SignalRule.is_active == 1)
    ).scalar_one()
    rules_complete = active_rule_count > 0 and active_signal_rule_count > 0

    # 模拟账户余额
    latest_ledger = db.execute(
        select(CashLedger).order_by(CashLedger.id.desc())
    ).scalars().first()
    account_balance = latest_ledger.balance_after if latest_ledger else 0.0
    account_ready = account_balance > 0

    portfolio_ready = portfolio_count > 0
    auto_trade_enabled = auto_trade_portfolios > 0

    prerequisites = [
        CapabilityPrerequisite(
            key="portfolio_ready",
            label="组合已就绪",
            satisfied=portfolio_ready,
            detail=f"已创建 {portfolio_count} 个组合" if portfolio_ready else "尚未创建组合",
        ),
        CapabilityPrerequisite(
            key="auto_trade_enabled",
            label="已开启自动交易",
            satisfied=auto_trade_enabled,
            detail=(
                f"{auto_trade_portfolios} 个组合已开启自动交易"
                if auto_trade_enabled
                else "尚未在任何组合开启自动交易"
            ),
        ),
        CapabilityPrerequisite(
            key="data_healthy",
            label="基础数据健康",
            satisfied=data_healthy,
            detail=(
                f"最新行情日期 {latest_trade_date}（距今 {bar_age_days if bar_age_days is not None else '未知'} 天）"
                if latest_trade_date is not None
                else "尚无行情数据"
            ),
        ),
        CapabilityPrerequisite(
            key="rules_complete",
            label="交易规则已配置",
            satisfied=rules_complete,
            detail=f"组合规则 {active_rule_count} 条 / 信号规则 {active_signal_rule_count} 条",
        ),
    ]
    recommended_actions = [
        CapabilityAction(
            label="去自动交易配置",
            action_type="redirect",
            target="/portfolio/auto-trade",
            reason="开启自动交易或完善规则配置",
        )
    ]

    data_cutoff_at = _to_datetime(latest_trade_date)

    if portfolio_count == 0:
        return CapabilityStatus(
            key="auto_trade",
            label="自动交易前",
            status="blocked",
            reason_code="no_portfolio",
            user_message="尚未创建组合，自动交易无对象。",
            prerequisites=prerequisites,
            recommended_actions=recommended_actions,
            data_cutoff_at=data_cutoff_at,
            last_checked_at=now,
        )

    if not auto_trade_enabled:
        return CapabilityStatus(
            key="auto_trade",
            label="自动交易前",
            status="blocked",
            reason_code="auto_trade_disabled",
            user_message=f"已创建 {portfolio_count} 个组合，但未在任何组合开启自动交易。",
            prerequisites=prerequisites,
            recommended_actions=recommended_actions,
            data_cutoff_at=data_cutoff_at,
            last_checked_at=now,
        )

    if not data_healthy:
        age_display = bar_age_days if bar_age_days is not None else "未知"
        return CapabilityStatus(
            key="auto_trade",
            label="自动交易前",
            status="degraded",
            reason_code="data_unhealthy",
            user_message=(
                f"行情数据距今 {age_display} 天，超过自动交易安全阈值（3 天），"
                f"已开启 fail-closed 模式禁止买入（数据截至 {latest_trade_date}）。"
            ),
            prerequisites=prerequisites,
            recommended_actions=recommended_actions,
            data_cutoff_at=data_cutoff_at,
            last_checked_at=now,
        )

    if not rules_complete:
        return CapabilityStatus(
            key="auto_trade",
            label="自动交易前",
            status="degraded",
            reason_code="rules_incomplete",
            user_message=(
                f"规则配置不完整：组合规则 {active_rule_count} 条 / 信号规则 {active_signal_rule_count} 条。"
                f"建议补齐 PortfolioRule 与 SignalRule 后再开启自动交易。"
            ),
            prerequisites=prerequisites,
            recommended_actions=recommended_actions,
            data_cutoff_at=data_cutoff_at,
            last_checked_at=now,
        )

    return CapabilityStatus(
        key="auto_trade",
        label="自动交易前",
        status="ready",
        reason_code=None,
        user_message=(
            f"自动交易就绪：{auto_trade_portfolios} 个组合已开启，"
            f"账户余额 {account_balance:.2f}，规则配置完整。"
            if account_ready
            else f"自动交易就绪：{auto_trade_portfolios} 个组合已开启，规则配置完整（建议初始化账户余额）。"
        ),
        prerequisites=prerequisites,
        recommended_actions=recommended_actions,
        data_cutoff_at=data_cutoff_at,
        last_checked_at=now,
    )


# ── 2.7 AI 配置 ─────────────────────────────────────────────


def check_ai_capability(db: Session) -> CapabilityStatus:
    """AI 配置就绪状态。

    判定依据：参照 `app/api/routes/ai_config.py` 的 `_load_ai_config()`，
    检查配置文件是否持久化、enabled、service_url、api_key 是否完整。

    状态规则：
    - 未配置 AI → blocked, reason="ai_not_configured"
    - 已配置但测试未通过 → degraded, reason="ai_test_failed"
    - 否则 → ready

    注意：本函数不发起任何网络请求，仅检查配置完整性。
    """
    now = _utcnow_naive()

    try:
        from app.api.routes.ai_config import AI_CONFIG_PATH, _load_ai_config

        cfg = _load_ai_config()
        persisted = AI_CONFIG_PATH.exists()
    except Exception as exc:
        logger.debug("AI config load failed: %s", exc)
        return CapabilityStatus(
            key="ai",
            label="AI 配置",
            status="blocked",
            reason_code="ai_not_configured",
            user_message=f"AI 配置加载失败：{type(exc).__name__}",
            prerequisites=[
                CapabilityPrerequisite(
                    key="ai_configured",
                    label="已配置 AI 服务",
                    satisfied=False,
                    detail=f"配置加载异常：{exc}",
                ),
                CapabilityPrerequisite(
                    key="ai_test_passed",
                    label="连接测试已通过",
                    satisfied=False,
                    detail="未进行测试",
                ),
            ],
            recommended_actions=[
                CapabilityAction(
                    label="去 AI 配置",
                    action_type="redirect",
                    target="/settings/ai",
                    reason="配置 AI 服务地址与 API Key",
                )
            ],
            data_cutoff_at=None,
            last_checked_at=now,
        )

    ai_configured = (
        persisted
        and bool(cfg.get("enabled"))
        and bool(cfg.get("service_url"))
        and bool(cfg.get("api_key"))
    )
    # 测试通过判定：配置过且 updated_at 在最近 7 天内（视为近期测试通过）
    updated_at_str = cfg.get("updated_at")
    updated_at = _parse_datetime(updated_at_str)
    test_age_days = _age_days(updated_at) if updated_at else None
    ai_test_passed = ai_configured and test_age_days is not None and test_age_days <= 7

    prerequisites = [
        CapabilityPrerequisite(
            key="ai_configured",
            label="已配置 AI 服务",
            satisfied=ai_configured,
            detail=(
                f"提供商 {cfg.get('provider')} / 模型 {cfg.get('model')}"
                if ai_configured
                else "AI 服务地址或 API Key 未配置"
            ),
        ),
        CapabilityPrerequisite(
            key="ai_test_passed",
            label="连接测试已通过",
            satisfied=ai_test_passed,
            detail=(
                f"最近配置/测试时间 {updated_at_str}"
                if ai_test_passed
                else (
                    f"配置时间 {updated_at_str} 距今 {test_age_days} 天，建议重新测试连接"
                    if test_age_days is not None
                    else "尚未进行连接测试"
                )
            ),
        ),
    ]
    recommended_actions = [
        CapabilityAction(
            label="去 AI 配置",
            action_type="redirect",
            target="/settings/ai",
            reason="配置 AI 服务或重新测试连接",
        )
    ]

    data_cutoff_at = updated_at

    if not ai_configured:
        return CapabilityStatus(
            key="ai",
            label="AI 配置",
            status="blocked",
            reason_code="ai_not_configured",
            user_message="AI 助手未配置或未启用，请在设置中配置 AI 服务地址与 API Key。",
            prerequisites=prerequisites,
            recommended_actions=recommended_actions,
            data_cutoff_at=data_cutoff_at,
            last_checked_at=now,
        )

    if not ai_test_passed:
        return CapabilityStatus(
            key="ai",
            label="AI 配置",
            status="degraded",
            reason_code="ai_test_failed",
            user_message=(
                "AI 配置已保存，但近 7 天内未进行连接测试或测试已过期，"
                "建议在设置中点击「测试连接」验证可用性。"
            ),
            prerequisites=prerequisites,
            recommended_actions=recommended_actions,
            data_cutoff_at=data_cutoff_at,
            last_checked_at=now,
        )

    return CapabilityStatus(
        key="ai",
        label="AI 配置",
        status="ready",
        reason_code=None,
        user_message=(
            f"AI 助手就绪：提供商 {cfg.get('provider')}，模型 {cfg.get('model')}，"
            f"最近配置时间 {updated_at_str}。"
        ),
        prerequisites=prerequisites,
        recommended_actions=recommended_actions,
        data_cutoff_at=data_cutoff_at,
        last_checked_at=now,
    )


# ── 2.8 外部消息渠道 ────────────────────────────────────────


def check_message_channel_capability(db: Session) -> CapabilityStatus:
    """外部消息渠道就绪状态。

    判定依据：
    - 若 `notification_channels` 表存在（WP-MSG.1 后），按表查询已配置/已测试渠道
    - 否则降级到基于 AlertRule 判定（视为消息渠道配置）

    状态规则：
    - 无渠道 → blocked, reason="no_message_channel"
    - 已配置但测试未通过 → degraded, reason="channel_test_failed"
    - 否则 → ready
    """
    now = _utcnow_naive()

    channel_configured = False
    channel_test_passed = False
    channel_count = 0
    tested_count = 0
    channel_detail = ""

    # 优先使用 notification_channels 表（WP-MSG.1 后才存在）
    if _has_table(db, "notification_channels"):
        try:
            from sqlalchemy import text

            rows = db.execute(
                text(
                    "SELECT name, status, last_test_at FROM notification_channels "
                    "WHERE enabled = 1"
                )
            ).all()
            channel_count = len(rows)
            channel_configured = channel_count > 0
            tested_count = sum(
                1 for row in rows if row.last_test_at is not None
            )
            channel_test_passed = channel_configured and tested_count == channel_count
            channel_detail = (
                f"已配置 {channel_count} 个渠道，已测试 {tested_count} 个"
                if channel_configured
                else "尚未配置任何消息渠道"
            )
        except Exception as exc:
            logger.debug("notification_channels query failed: %s", exc)
            channel_detail = f"渠道表查询失败：{exc}"
    else:
        # 降级：基于 AlertRule 判定（WP-MSG 之前视为"消息渠道配置"）
        try:
            from app.models.alert import AlertRule

            rule_count = db.execute(select(func.count(AlertRule.id))).scalar_one()
            enabled_rule_count = db.execute(
                select(func.count(AlertRule.id)).where(AlertRule.enabled == 1)
            ).scalar_one()
            triggered_count = db.execute(
                select(func.count(AlertRule.id)).where(AlertRule.last_triggered_at.is_not(None))
            ).scalar_one()
            channel_count = enabled_rule_count
            channel_configured = enabled_rule_count > 0
            channel_test_passed = channel_configured and triggered_count > 0
            channel_detail = (
                f"已启用 {enabled_rule_count} 条告警规则，{triggered_count} 条曾触发"
                if channel_configured
                else "尚未配置任何告警规则"
            )
        except Exception as exc:
            logger.debug("AlertRule fallback query failed: %s", exc)
            channel_detail = f"渠道查询失败：{exc}"

    prerequisites = [
        CapabilityPrerequisite(
            key="channel_configured",
            label="已配置消息渠道",
            satisfied=channel_configured,
            detail=channel_detail or "尚未配置任何消息渠道",
        ),
        CapabilityPrerequisite(
            key="channel_test_passed",
            label="渠道测试已通过",
            satisfied=channel_test_passed,
            detail=(
                f"已测试 {tested_count}/{channel_count} 个渠道"
                if channel_configured
                else "未配置渠道，无法测试"
            ) if _has_table(db, "notification_channels") else (
                "已触发过告警视为渠道可用"
                if channel_test_passed
                else "尚未触发过任何告警，建议手动测试"
            ),
        ),
    ]
    recommended_actions = [
        CapabilityAction(
            label="去消息渠道",
            action_type="redirect",
            target="/settings/notifications",
            reason="配置消息渠道或测试连通性",
        )
    ]

    data_cutoff_at = None

    if not channel_configured:
        return CapabilityStatus(
            key="message_channel",
            label="外部消息渠道",
            status="blocked",
            reason_code="no_message_channel",
            user_message="尚未配置任何消息渠道，请先在设置中配置至少一个推送渠道。",
            prerequisites=prerequisites,
            recommended_actions=recommended_actions,
            data_cutoff_at=data_cutoff_at,
            last_checked_at=now,
        )

    if not channel_test_passed:
        return CapabilityStatus(
            key="message_channel",
            label="外部消息渠道",
            status="degraded",
            reason_code="channel_test_failed",
            user_message=(
                f"已配置 {channel_count} 个渠道，但部分渠道尚未测试或测试失败。"
                f"建议在设置中点击「测试」验证连通性。"
            ),
            prerequisites=prerequisites,
            recommended_actions=recommended_actions,
            data_cutoff_at=data_cutoff_at,
            last_checked_at=now,
        )

    return CapabilityStatus(
        key="message_channel",
        label="外部消息渠道",
        status="ready",
        reason_code=None,
        user_message=f"消息渠道就绪：{channel_detail}。",
        prerequisites=prerequisites,
        recommended_actions=recommended_actions,
        data_cutoff_at=data_cutoff_at,
        last_checked_at=now,
    )


# ── 2.9 聚合函数 ────────────────────────────────────────────


# 所有 check 函数注册表（顺序决定前端展示顺序）
_CHECK_FUNCTIONS = (
    ("market_data", "基础数据采集", check_market_data_capability),
    ("scoring", "评分配置激活", check_scoring_capability),
    ("factor_ridge", "Ridge 因子仓库", check_factor_ridge_capability),
    ("discovery", "机会扫描快照", check_discovery_capability),
    ("portfolio", "组合操作前", check_portfolio_capability),
    ("auto_trade", "自动交易前", check_auto_trade_capability),
    ("ai", "AI 配置", check_ai_capability),
    ("message_channel", "外部消息渠道", check_message_channel_capability),
)


def _compute_overall_status(capabilities: list[CapabilityStatus]) -> str:
    """根据所有 capability 状态计算 overall_status。

    - 任一 blocked → "blocked"
    - 否则任一 degraded → "degraded"
    - 否则 → "ready"
    """
    if any(c.status == "blocked" for c in capabilities):
        return "blocked"
    if any(c.status == "degraded" for c in capabilities):
        return "degraded"
    return "ready"


def get_all_capabilities(db: Session) -> CapabilitiesResponse:
    """聚合查询所有功能的就绪状态、前置条件、推荐操作。

    全程 best-effort：单个 check 异常不阻塞聚合，
    对应 capability 状态置 blocked 并 reason="check_failed"。
    """
    now = _utcnow_naive()
    capabilities: list[CapabilityStatus] = []

    for key, label, check_fn in _CHECK_FUNCTIONS:
        try:
            status = check_fn(db)
        except Exception as exc:
            # best-effort：单个 check 异常不阻塞聚合
            logger.exception("Capability check '%s' failed: %s", key, exc)
            status = CapabilityStatus(
                key=key,
                label=label,
                status="blocked",
                reason_code="check_failed",
                user_message=f"该功能检查过程发生异常：{type(exc).__name__}",
                prerequisites=[],
                recommended_actions=[
                    CapabilityAction(
                        label="稍后重试",
                        action_type="retry",
                        reason="检查服务暂不可用",
                    )
                ],
                data_cutoff_at=None,
                last_checked_at=now,
            )
        capabilities.append(status)

    overall_status = _compute_overall_status(capabilities)
    return CapabilitiesResponse(
        overall_status=overall_status,
        capabilities=capabilities,
        checked_at=now,
    )


__all__ = [
    "check_market_data_capability",
    "check_scoring_capability",
    "check_factor_ridge_capability",
    "check_discovery_capability",
    "check_portfolio_capability",
    "check_auto_trade_capability",
    "check_ai_capability",
    "check_message_channel_capability",
    "get_all_capabilities",
]
