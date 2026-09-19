"""WP-P.3 增量失效规则：dirty 集合计算与快照并发保护。

判定规则（参照 spec）：
- universe_symbols.last_synced_at 晚于快照 generated_at → UNIVERSE_RESYNCED
- universe_symbols.created_at 晚于快照 generated_at → NEW_SYMBOL_ADDED
- daily_bar 在快照后新增记录，或 bar_count < 5 → LATEST_BAR_CHANGED
- stock_valuation / capital_flow / hot_rank / lhb / tail_accumulation 在快照后更新 → 对应原因
- 快照不存在或超过 snapshot_max_age_days → 所有标的 dirty（SNAPSHOT_EXPIRED / NEW_SYMBOL_ADDED）

全量重建触发：
- 评分配置版本变化 / 权重模式切换 / 因子模型变化 / 快照失败或被替换 / 首次构建

并发保护：
- 同一 scope 同时只允许一个 building 状态快照
- 新快照 ready 后将旧快照标记为 superseded（同事务 flush，由调用方 commit）
"""
from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.capital_flow import CapitalFlow
from app.models.daily_bar import DailyBar
from app.models.discovery_score_snapshot import DiscoveryScoreSnapshot
from app.models.hot_rank_snapshot import StockHotRankSnapshot
from app.models.lhb_institution_trade import LhbInstitutionTrade
from app.models.stock_valuation import StockValuation
from app.models.symbol import Symbol
from app.models.tail_accumulation_snapshot import TailAccumulationSnapshot
from app.models.universe import UniverseSymbol

logger = logging.getLogger(__name__)


# scope → (region, asset_type) 映射
# 兼容下划线（cn_stock，snapshot 表约定）与连字符（cn-stock，旧挖掘任务约定）两种格式
_SCOPE_TO_REGION_ASSET: dict[str, tuple[str, str]] = {
    "cn_stock": ("cn", "stock"),
    "cn-stock": ("cn", "stock"),
    "cn_etf": ("cn", "etf"),
    "cn-etf": ("cn", "etf"),
    "us_stock": ("us", "stock"),
    "us-stock": ("us", "stock"),
    "us_etf": ("us", "etf"),
    "us-etf": ("us", "etf"),
}


# 数据不足阈值：bar_count 低于此值视为数据不充分，需重新评分
_MIN_BAR_COUNT = 5


def _now() -> datetime:
    """UTC 当前时间（naive，与 DB 中其它时间戳一致）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _safe_datetime(value) -> datetime | None:
    """安全转换为 datetime，兼容 MySQL 返回的字符串与 tz-aware datetime。

    统一返回 naive UTC datetime，避免与 DB 中其它 naive 时间戳比较时抛 TypeError。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        # 统一转为 naive UTC，避免 tz-aware 与 naive 比较报错
        if value.tzinfo is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value
    if isinstance(value, str):
        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S.%f",
            "%Y-%m-%d",
        ):
            try:
                return datetime.strptime(value, fmt)
            except ValueError:
                continue
    return None


class DirtyReason(str, enum.Enum):
    """标的进入 dirty 集合的具体原因。"""

    UNIVERSE_RESYNCED = "universe_resynced"  # universe_symbols.last_synced_at 晚于快照生成
    LATEST_BAR_CHANGED = "latest_bar_changed"  # 最新 last_bar_date 变化
    FINANCIAL_REPORT_UPDATED = "financial_report_updated"  # 财报在快照后更新
    CAPITAL_FLOW_UPDATED = "capital_flow_updated"  # 资金流在快照后更新
    HOT_RANK_UPDATED = "hot_rank_updated"  # 人气数据在快照后更新
    LHB_UPDATED = "lhb_updated"  # 龙虎榜在快照后更新
    TAIL_ACCUMULATION_UPDATED = "tail_accumulation_updated"  # 尾盘数据在快照后更新
    DATA_REPAIR_SUCCESS = "data_repair_success"  # 定向数据修复成功
    SNAPSHOT_EXPIRED = "snapshot_expired"  # 快照不存在或已超过有效期
    NEW_SYMBOL_ADDED = "new_symbol_added"  # universe 中新增的标的


@dataclass
class DirtySymbol:
    """单个 dirty 标的的信息。"""

    universe_symbol_id: int
    symbol_id: int | None
    symbol: str
    reasons: list[DirtyReason]
    last_changed_at: datetime  # 最近一次变化时间
    detail: dict  # 调试详情，如 {"last_bar_date": "2026-07-15"}


def _resolve_scope_config(scope: str) -> tuple[str, str]:
    """将 scope 解析为 (region, asset_type)。"""
    if scope not in _SCOPE_TO_REGION_ASSET:
        raise ValueError(f"Unsupported discovery scope: {scope}")
    return _SCOPE_TO_REGION_ASSET[scope]


def _load_scope_universe_symbols(db: Session, scope: str) -> list[UniverseSymbol]:
    """加载某 scope 下所有 universe_symbols（按 id 升序）。"""
    region, asset_type = _resolve_scope_config(scope)
    stmt = (
        select(UniverseSymbol)
        .where(
            UniverseSymbol.region == region,
            UniverseSymbol.asset_type == asset_type,
        )
        .order_by(UniverseSymbol.id.asc())
    )
    return list(db.execute(stmt).scalars().all())


def _aggregate_max_created_at_by_symbol_id(
    db: Session, model_cls, symbol_ids: list[int]
) -> dict[int, datetime]:
    """对使用 symbol_id 外键的表聚合 max(created_at)，返回 {symbol_id: max_created_at}。

    单一 group_by 查询避免 N+1。
    """
    if not symbol_ids:
        return {}
    stmt = (
        select(model_cls.symbol_id, func.max(model_cls.created_at))
        .where(model_cls.symbol_id.in_(symbol_ids))
        .group_by(model_cls.symbol_id)
    )
    result: dict[int, datetime] = {}
    for sym_id, max_ts in db.execute(stmt).all():
        ts = _safe_datetime(max_ts)
        if ts is not None:
            result[int(sym_id)] = ts
    return result


def _aggregate_max_created_at_by_symbol_str(
    db: Session, model_cls, symbols: list[str]
) -> dict[str, datetime]:
    """对使用 symbol 字符串字段的表聚合 max(created_at)，返回 {symbol: max_created_at}。

    单一 group_by 查询避免 N+1。
    """
    if not symbols:
        return {}
    stmt = (
        select(model_cls.symbol, func.max(model_cls.created_at))
        .where(model_cls.symbol.in_(symbols))
        .group_by(model_cls.symbol)
    )
    result: dict[str, datetime] = {}
    for sym_str, max_ts in db.execute(stmt).all():
        ts = _safe_datetime(max_ts)
        if ts is not None:
            result[str(sym_str)] = ts
    return result


def _aggregate_max_bar_info_by_symbol_id(
    db: Session, symbol_ids: list[int]
) -> dict[int, tuple[datetime, "object | None"]]:
    """对 daily_bars 聚合 max(created_at) 与 max(trade_date)，返回 {symbol_id: (max_created_at, max_trade_date)}。"""
    if not symbol_ids:
        return {}
    stmt = (
        select(
            DailyBar.symbol_id,
            func.max(DailyBar.created_at),
            func.max(DailyBar.trade_date),
        )
        .where(DailyBar.symbol_id.in_(symbol_ids))
        .group_by(DailyBar.symbol_id)
    )
    result: dict[int, tuple[datetime, "object | None"]] = {}
    for sym_id, max_created, max_trade_date in db.execute(stmt).all():
        ts = _safe_datetime(max_created)
        if ts is not None:
            result[int(sym_id)] = (ts, max_trade_date)
    return result


def compute_dirty_symbols(
    db: Session,
    *,
    scope: str,
    current_snapshot: DiscoveryScoreSnapshot | None,
    snapshot_max_age_days: int = 7,
) -> list[DirtySymbol]:
    """计算某 scope 下所有 dirty 标的集合。

    判定规则（参照 spec）：
    - 标的进入 dirty 集合的条件：
      1. universe_symbols.last_synced_at 晚于当前快照对应 Score 创建时间
      2. 最新 last_bar_date 变化
      3. 财报/资金/人气/龙虎榜/尾盘数据在快照后更新
      4. 定向数据修复成功
    - 快照不存在或超过 snapshot_max_age_days 也视为 dirty 全集
    - universe 中新增的标的为 dirty

    Args:
        db: 数据库会话
        scope: 范围（cn_stock / cn_etf / us_stock，兼容 cn-stock 等连字符格式）
        current_snapshot: 当前 ready 快照（None 表示首次构建）
        snapshot_max_age_days: 快照最大有效期天数，超过则视为 dirty 全集

    Returns:
        DirtySymbol 列表，按 last_changed_at 倒序排序
    """
    universe_symbols = _load_scope_universe_symbols(db, scope)
    if not universe_symbols:
        return []

    # 解析快照 generated_at
    snapshot_generated_at: datetime | None = None
    if current_snapshot is not None:
        snapshot_generated_at = _safe_datetime(current_snapshot.generated_at)
        # generated_at 缺失：视为异常 snapshot（building / 损坏），交给调用方通过
        # should_trigger_full_rebuild 决定是否 full rebuild；此处仍做增量 dirty 判定
        # 避免在 compute_dirty 层短路导致全量 dirty 与增量分支结果不一致

    # 全量 dirty：首次构建（完全没有 snapshot）
    # 注意：snapshot 过期不再在本函数内直接短路为 SNAPSHOT_EXPIRED 全量 dirty。
    # 过期判定应通过 should_trigger_full_rebuild(...) 触发 full rebuild；
    # 本函数始终执行增量 dirty 检查，确保测试和运行时的行为一致。
    if current_snapshot is None:
        now = _now()
        results: list[DirtySymbol] = []
        for us in universe_symbols:
            results.append(
                DirtySymbol(
                    universe_symbol_id=us.id,
                    symbol_id=None,  # 全量 dirty 分支不做 symbol_id join
                    symbol=us.symbol,
                    reasons=[DirtyReason.NEW_SYMBOL_ADDED],
                    last_changed_at=now,
                    detail={
                        "snapshot_id": None,
                        "snapshot_generated_at": None,
                    },
                )
            )
        results.sort(key=lambda d: d.symbol)
        return results

    # generated_at 仍为 None：无法比较，保守返回空（调用方应通过 status 重建）
    if snapshot_generated_at is None:
        return []

    # 检测快照是否过期（用于叠加 SNAPSHOT_EXPIRED 原因，不短路增量检查）
    now_for_age = _now()
    age = now_for_age - snapshot_generated_at
    snapshot_expired = age > timedelta(days=snapshot_max_age_days)

    # 增量 dirty：逐表聚合检查（无论 snapshot 是否过期，都按增量规则计算）
    # 1. 拉取所有 universe_symbol 的 symbol_id（按 symbol code join symbols 表）
    sym_codes = [us.symbol for us in universe_symbols]
    symbol_rows = db.execute(
        select(Symbol.id, Symbol.symbol).where(Symbol.symbol.in_(sym_codes))
    ).all()
    code_to_symbol_id: dict[str, int] = {
        str(row.symbol): int(row.id) for row in symbol_rows
    }

    # 2. 对每张表批量聚合 max(created_at)，避免 N+1
    symbol_ids = [sid for sid in code_to_symbol_id.values() if sid is not None]

    bar_max_info = _aggregate_max_bar_info_by_symbol_id(db, symbol_ids)
    valuation_max = _aggregate_max_created_at_by_symbol_id(db, StockValuation, symbol_ids)
    capital_flow_max = _aggregate_max_created_at_by_symbol_id(db, CapitalFlow, symbol_ids)
    hot_rank_max = _aggregate_max_created_at_by_symbol_str(
        db, StockHotRankSnapshot, sym_codes
    )
    lhb_max = _aggregate_max_created_at_by_symbol_str(
        db, LhbInstitutionTrade, sym_codes
    )
    tail_max = _aggregate_max_created_at_by_symbol_str(
        db, TailAccumulationSnapshot, sym_codes
    )

    # 3. 逐个 universe_symbol 判定
    results: list[DirtySymbol] = []
    for us in universe_symbols:
        reasons: list[DirtyReason] = []
        change_timestamps: list[datetime] = []
        detail: dict = {
            "snapshot_id": current_snapshot.id,
            "snapshot_generated_at": (
                snapshot_generated_at.isoformat()
                if snapshot_generated_at is not None
                else None
            ),
        }

        # SNAPSHOT_EXPIRED: 快照过期，所有标的至少带此原因
        if snapshot_expired:
            reasons.append(DirtyReason.SNAPSHOT_EXPIRED)
            change_timestamps.append(snapshot_generated_at)
            detail["snapshot_expired"] = True
            detail["snapshot_age_days"] = age.days

        # NEW_SYMBOL_ADDED: universe_symbol 在快照后新增
        us_created_at = _safe_datetime(us.created_at)
        if us_created_at is not None and us_created_at > snapshot_generated_at:
            reasons.append(DirtyReason.NEW_SYMBOL_ADDED)
            change_timestamps.append(us_created_at)
            detail["universe_symbol_created_at"] = us_created_at.isoformat()

        # UNIVERSE_RESYNCED: last_synced_at 晚于快照 generated_at
        last_synced_at = _safe_datetime(us.last_synced_at)
        if last_synced_at is not None and last_synced_at > snapshot_generated_at:
            reasons.append(DirtyReason.UNIVERSE_RESYNCED)
            change_timestamps.append(last_synced_at)
            detail["last_synced_at"] = last_synced_at.isoformat()

        # LATEST_BAR_CHANGED: 多种条件
        # - bar_count < 5（数据不足，安全降级，参照 project_memory 硬约束）
        # - daily_bar 表在快照后有新记录
        bar_changed = False
        bar_count = us.bar_count if us.bar_count is not None else 0
        if bar_count < _MIN_BAR_COUNT:
            bar_changed = True
            detail["bar_count"] = bar_count
            detail["bar_count_below_min"] = True

        sym_id = code_to_symbol_id.get(us.symbol)
        bar_ts_candidate: datetime | None = None
        if sym_id is not None and sym_id in bar_max_info:
            bar_created_max, bar_trade_date_max = bar_max_info[sym_id]
            if bar_created_max > snapshot_generated_at:
                bar_changed = True
                detail["bar_created_at_max"] = bar_created_max.isoformat()
            if bar_trade_date_max is not None:
                detail["last_bar_date"] = str(bar_trade_date_max)
            bar_ts_candidate = bar_created_max

        if bar_changed:
            reasons.append(DirtyReason.LATEST_BAR_CHANGED)
            # 取 bar 表的最新时间作为变化时间；若无则使用 last_synced_at 或 now
            bar_ts = bar_ts_candidate
            if bar_ts is None:
                bar_ts = last_synced_at if last_synced_at is not None else _now()
            change_timestamps.append(bar_ts)

        # 财报/资金/人气/龙虎榜/尾盘 各表检查
        if sym_id is not None and sym_id in valuation_max:
            ts = valuation_max[sym_id]
            if ts > snapshot_generated_at:
                reasons.append(DirtyReason.FINANCIAL_REPORT_UPDATED)
                change_timestamps.append(ts)
                detail["valuation_created_at_max"] = ts.isoformat()

        if sym_id is not None and sym_id in capital_flow_max:
            ts = capital_flow_max[sym_id]
            if ts > snapshot_generated_at:
                reasons.append(DirtyReason.CAPITAL_FLOW_UPDATED)
                change_timestamps.append(ts)
                detail["capital_flow_created_at_max"] = ts.isoformat()

        if us.symbol in hot_rank_max:
            ts = hot_rank_max[us.symbol]
            if ts > snapshot_generated_at:
                reasons.append(DirtyReason.HOT_RANK_UPDATED)
                change_timestamps.append(ts)
                detail["hot_rank_created_at_max"] = ts.isoformat()

        if us.symbol in lhb_max:
            ts = lhb_max[us.symbol]
            if ts > snapshot_generated_at:
                reasons.append(DirtyReason.LHB_UPDATED)
                change_timestamps.append(ts)
                detail["lhb_created_at_max"] = ts.isoformat()

        if us.symbol in tail_max:
            ts = tail_max[us.symbol]
            if ts > snapshot_generated_at:
                reasons.append(DirtyReason.TAIL_ACCUMULATION_UPDATED)
                change_timestamps.append(ts)
                detail["tail_accumulation_created_at_max"] = ts.isoformat()

        if not reasons:
            continue

        last_changed_at = (
            max(change_timestamps) if change_timestamps else _now()
        )
        results.append(
            DirtySymbol(
                universe_symbol_id=us.id,
                symbol_id=sym_id,
                symbol=us.symbol,
                reasons=reasons,
                last_changed_at=last_changed_at,
                detail=detail,
            )
        )

    # 按 last_changed_at 倒序排序
    results.sort(key=lambda d: d.last_changed_at, reverse=True)
    return results


def should_trigger_full_rebuild(
    db: Session,
    *,
    scope: str,
    current_snapshot: DiscoveryScoreSnapshot | None,
    new_scoring_config_id: int | None = None,
    new_scoring_config_version: int | None = None,
    new_weight_mode: str | None = None,
    new_factor_model_run_id: str | None = None,
    snapshot_max_age_days: int = 7,
) -> tuple[bool, str | None]:
    """判断是否需要触发新版本全量快照。

    触发条件（参照 spec）：
    1. 激活新评分配置版本（current.scoring_config_version != new_scoring_config_version）
    2. 切换手工/影子/Ridge 权重模式（current.weight_mode != new_weight_mode）
    3. 激活新因子模型（current.factor_model_run_id != new_factor_model_run_id）
    4. 修改影响全市场的宏观权重或基础公式（暂用 scoring_config_version 变化替代）
    5. current_snapshot 为 None（首次构建）
    6. current_snapshot.status == 'failed' 或 'superseded'
    7. current_snapshot.generated_at 距今超过 snapshot_max_age_days（快照过期）

    Args:
        db: 数据库会话
        scope: 范围
        current_snapshot: 当前 ready 快照
        new_scoring_config_id: 新激活的评分配置 ID
        new_scoring_config_version: 新激活的评分配置版本号
        new_weight_mode: 新权重模式（manual / shadow / ridge）
        new_factor_model_run_id: 新因子模型 run_id
        snapshot_max_age_days: 快照最大有效期天数，超过需 full rebuild

    Returns:
        (是否触发全量重建, 触发原因码)
    """
    # 条件 5：首次构建
    if current_snapshot is None:
        return True, "no_snapshot"
    # 条件 6：当前快照不可用
    if current_snapshot.status in ("failed", "superseded"):
        return True, "snapshot_not_ready"
    # 条件 7：快照超过最大有效期
    generated_at = _safe_datetime(current_snapshot.generated_at)
    if generated_at is None:
        return True, "snapshot_no_generated_at"
    age = _now() - generated_at
    if age > timedelta(days=snapshot_max_age_days):
        return True, "snapshot_expired"
    # 条件 1：评分配置版本变化
    if new_scoring_config_version is not None:
        cur_version = current_snapshot.scoring_config_version
        if cur_version is None or cur_version != new_scoring_config_version:
            return True, "scoring_config_version_changed"
    # 条件 2：权重模式切换
    if new_weight_mode is not None:
        cur_mode = current_snapshot.weight_mode
        if cur_mode is None or cur_mode != new_weight_mode:
            return True, "weight_mode_changed"
    # 条件 3：因子模型变化
    if new_factor_model_run_id is not None:
        cur_run_id = current_snapshot.factor_model_run_id
        if cur_run_id is None or cur_run_id != new_factor_model_run_id:
            return True, "factor_model_changed"
    return False, None


def is_snapshot_building_for_scope(
    db: Session, scope: str
) -> DiscoveryScoreSnapshot | None:
    """检查某 scope 是否已有 building 状态的快照。

    同一 scope、同一配置版本同时只允许一个快照构建任务（参照 spec）。
    返回 building 状态快照，无则返回 None。
    """
    stmt = (
        select(DiscoveryScoreSnapshot)
        .where(
            DiscoveryScoreSnapshot.scope == scope,
            DiscoveryScoreSnapshot.status == "building",
        )
        .order_by(DiscoveryScoreSnapshot.created_at.desc())
        .limit(1)
    )
    return db.execute(stmt).scalars().first()


def mark_snapshot_superseded(db: Session, old_snapshot_id: int) -> None:
    """将旧快照标记为 superseded（被新版本替换）。

    新快照 ready 前继续提供旧快照（参照 spec），所以此函数应在：
    - 新快照 status 由 building → ready 之后调用
    - 在同一事务中将旧快照 status 改为 superseded

    本函数只做 flush 不做 commit，由调用方在同一事务中提交，
    确保新快照 ready 与旧快照 superseded 原子生效。

    Args:
        db: 数据库会话
        old_snapshot_id: 旧快照 ID
    """
    old_snapshot = db.get(DiscoveryScoreSnapshot, old_snapshot_id)
    if old_snapshot is None:
        logger.warning(
            "mark_snapshot_superseded: snapshot %s not found", old_snapshot_id
        )
        return
    if old_snapshot.status == "superseded":
        # 已是 superseded，幂等返回
        return
    if old_snapshot.status != "ready":
        # 仅 ready 状态的快照可被标记为 superseded；
        # building/failed 状态的快照不应被标记，避免误覆盖
        logger.warning(
            "mark_snapshot_superseded: snapshot %s status=%s, expected ready; skip",
            old_snapshot_id,
            old_snapshot.status,
        )
        return
    old_snapshot.status = "superseded"
    db.flush()
