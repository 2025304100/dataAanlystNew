"""backtest_filter_events 审计事件批量写入与汇总查询。

使用 bulk_insert_mappings 保证性能；get_filter_events_summary 返回
PRD §6.3 excluded_symbol_days 结构（new_listing / st / suspended /
delisting_period / status_unknown / price_or_volume_invalid）。
"""
from __future__ import annotations

import logging
from typing import Any, Iterable, Sequence

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.models.backtest_filter_event import BacktestFilterEvent
from app.services.backtest_filters.rules import (
    FilterEventDTO,
    RULE_DELISTING_PERIOD_EXCLUDE,
    RULE_DELISTING_PRICE_MISSING,
    RULE_NEW_LISTING_EXCLUDE,
    RULE_NEW_LISTING_LISTING_DATE_UNKNOWN,
    RULE_PRICE_INVALID,
    RULE_STATUS_UNKNOWN_BLOCK,
    RULE_ST_EXCLUDE,
    RULE_SUSPENDED_EXCLUDE,
    RULE_SUSPENDED_FREEZE,
    RULE_VOLUME_INVALID,
)

logger = logging.getLogger(__name__)

BATCH_SIZE = 5000


def _event_to_mapping(ev: FilterEventDTO) -> dict[str, Any]:
    """FilterEventDTO -> ORM row dict（列名严格对应 backtest_filter_events 表）。"""
    return {
        "run_id": ev.run_id,
        "trade_date": ev.trade_date,
        "symbol_id": ev.symbol_id,
        "action": ev.action,
        "rule_code": ev.rule_code,
        "reason": ev.reason,
        "raw_status_json": ev.raw_status_json,
        "effective_status": ev.effective_status,
        "price_used": ev.price_used,
        "config_hash": ev.config_hash,
        "data_batch_id": ev.data_batch_id,
    }


def bulk_write_filter_events(
    db: Session,
    events: Sequence[FilterEventDTO],
    batch_size: int = BATCH_SIZE,
) -> int:
    """批量写入审计事件。不 commit，由调用方在外层事务控制。

    Args:
        db: SQLAlchemy Session（已启用事务）
        events: 要写入的 FilterEventDTO 序列
        batch_size: 每批 bulk_insert 大小，默认 5000

    Returns:
        实际写入的行数（等于 len(events)）
    """
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    total = len(events)
    if total == 0:
        return 0
    # run_id 非空校验：若有 run_id=None 的事件说明调用方在纯函数阶段就写库了，应给 WARNING
    missing_run = [i for i, e in enumerate(events) if e.run_id is None]
    if missing_run:
        logger.warning(
            "bulk_write_filter_events: %s/%s 事件 run_id=None，将写入 NULL（允许历史/纯函数离线落库）",
            len(missing_run), total,
        )
    for start in range(0, total, batch_size):
        chunk = [_event_to_mapping(e) for e in events[start:start + batch_size]]
        # 使用 bulk_insert_mappings - 不经过 ORM unit_of_work，性能好
        db.bulk_insert_mappings(BacktestFilterEvent, chunk)
        # 每批 flush 一次，避免内存堆积（但仍在外层统一 commit）
        db.flush()
    return total


# PRD §6.3 excluded_symbol_days 键集合
EXCLUDED_SYMBOL_DAY_KEYS = (
    "new_listing",
    "st",
    "suspended",
    "delisting_period",
    "status_unknown",
    "price_or_volume_invalid",
)

# rule_code -> key 映射
_RULE_TO_AGGR_KEY: dict[str, str] = {
    RULE_NEW_LISTING_EXCLUDE: "new_listing",
    RULE_NEW_LISTING_LISTING_DATE_UNKNOWN: "new_listing",
    RULE_ST_EXCLUDE: "st",
    RULE_SUSPENDED_EXCLUDE: "suspended",
    RULE_SUSPENDED_FREEZE: "suspended",
    RULE_DELISTING_PERIOD_EXCLUDE: "delisting_period",
    RULE_STATUS_UNKNOWN_BLOCK: "status_unknown",
    RULE_PRICE_INVALID: "price_or_volume_invalid",
    RULE_VOLUME_INVALID: "price_or_volume_invalid",
    RULE_DELISTING_PRICE_MISSING: "price_or_volume_invalid",
}


def get_filter_events_summary(
    db: Session,
    run_id: int,
) -> dict[str, int]:
    """按 PRD §6.3 返回 excluded_symbol_days 聚合。

    一次 SQL（CASE WHEN 分组计数）。返回字典键集为 EXCLUDED_SYMBOL_DAY_KEYS，
    值为非负整数。
    """
    stmt = select(
        func.sum(
            case(
                (BacktestFilterEvent.rule_code.in_([
                    RULE_NEW_LISTING_EXCLUDE,
                    RULE_NEW_LISTING_LISTING_DATE_UNKNOWN,
                ]), 1),
                else_=0,
            )
        ).label("new_listing"),
        func.sum(case((BacktestFilterEvent.rule_code == RULE_ST_EXCLUDE, 1), else_=0)).label("st"),
        func.sum(case((BacktestFilterEvent.rule_code.in_([
            RULE_SUSPENDED_EXCLUDE,
            RULE_SUSPENDED_FREEZE,
        ]), 1), else_=0)).label("suspended"),
        func.sum(case((BacktestFilterEvent.rule_code == RULE_DELISTING_PERIOD_EXCLUDE, 1), else_=0)).label("delisting_period"),
        func.sum(case((BacktestFilterEvent.rule_code == RULE_STATUS_UNKNOWN_BLOCK, 1), else_=0)).label("status_unknown"),
        func.sum(case((BacktestFilterEvent.rule_code.in_([
            RULE_PRICE_INVALID,
            RULE_VOLUME_INVALID,
            RULE_DELISTING_PRICE_MISSING,
        ]), 1), else_=0)).label("price_or_volume_invalid"),
    ).where(BacktestFilterEvent.run_id == int(run_id))
    row = db.execute(stmt).one_or_none()
    result: dict[str, int] = {k: 0 for k in EXCLUDED_SYMBOL_DAY_KEYS}
    if row is None:
        return result
    # SQLAlchemy 返回可能为 None（空 run），替换为 0
    for i, key in enumerate(EXCLUDED_SYMBOL_DAY_KEYS):
        val = row[i] if hasattr(row, '__getitem__') else getattr(row, key, 0)
        result[key] = int(val or 0)
    return result
