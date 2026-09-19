"""证券状态数据来源适配器（时效化）。

Point-in-Time 治理原则：
- 任何当前快照（Symbol.is_st / is_active / listed_at）都不能直接判定为"历史状态"。
- 对历史状态未知的日期，写入 status=UNKNOWN，并标注 status_source="missing_historical" 或 "inferred_current_snapshot"。
- 严禁使用当前名称回填历史 ST 状态。
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.security_status import SecurityStatusDaily
from app.models.symbol import Symbol

logger = logging.getLogger(__name__)

# 源名称常量
SRC_AKSHARE = "akshare"
SRC_TDX = "tdx"
SRC_UNIVERSE = "raw_universe_snapshot"
SRC_CURRENT_SYMBOL = "inferred_current_snapshot"
SRC_MISSING = "missing_historical"


# ---------------------------------------------------------------------------
# 数据提取：从现有 Symbol 快照提取"当前已知"的上市/摘牌日期，标记为 inferred
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SymbolStatusSnapshot:
    """从 Symbol 表（当前时点）读取的快照近似值。

    注意：仅用于 listing_date/delisting_date 的近似推断，
    不用于判断历史 ST/停牌，这两项目前一律默认 UNKNOWN 直到有可靠历史源。
    """
    symbol_id: int
    current_is_st: bool
    current_is_active: bool
    listing_date: date | None
    # delisting_date: 若当前 is_active==0 且没有 listing_date，则认为可能已退市，但 delisting_date=None 填 UNKNOWN
    delisting_date: date | None


def extract_current_symbol_snapshots(db: Session, symbol_ids: Sequence[int] | None = None) -> list[SymbolStatusSnapshot]:
    """从当前 symbols 表批量提取当前快照（仅近似，不用于历史判断）。"""
    stmt = select(Symbol.id, Symbol.is_st, Symbol.is_active, Symbol.listed_at)
    if symbol_ids is not None:
        stmt = stmt.where(Symbol.id.in_([int(s) for s in symbol_ids]))
    rows = db.execute(stmt).all()
    out: list[SymbolStatusSnapshot] = []
    for r in rows:
        is_active = bool(getattr(r, "is_active", 1))
        listing_date: date | None = getattr(r, "listed_at", None)
        # 若当前不活跃，但缺乏 delisting_date，则明确保持 delisting_date = None（UNKNOWN）
        delisting_date: date | None = None
        out.append(SymbolStatusSnapshot(
            symbol_id=int(r.id),
            current_is_st=bool(getattr(r, "is_st", 0)),
            current_is_active=is_active,
            listing_date=listing_date,
            delisting_date=delisting_date,
        ))
    return out


# ---------------------------------------------------------------------------
# Status Daily 行构造（保守策略：不可判定一律 UNKNOWN）
# ---------------------------------------------------------------------------
def _build_unknown_row(
    symbol_id: int,
    trade_date: date,
    status_source: str,
    as_of_batch_id: str,
    now_ts: datetime,
) -> SecurityStatusDaily:
    return SecurityStatusDaily(
        symbol_id=symbol_id,
        trade_date=trade_date,
        is_st=False,           # UNKNOWN 时一律取安全默认，不视为 ST
        is_suspended=False,    # 不视为停牌（实际会在过滤层标记 unknown 并阻断生产回测）
        is_delisting_period=False,
        is_listed=True,        # 默认假定仍上市，后续由真实源纠正
        listing_date=None,
        delisting_date=None,
        status_source=status_source,
        source_updated_at=now_ts,
        as_of_batch_id=as_of_batch_id,
    )


def _build_snapshot_inferred_row(
    snap: SymbolStatusSnapshot,
    trade_date: date,
    as_of_batch_id: str,
    now_ts: datetime,
) -> SecurityStatusDaily:
    """用 Symbol 当前快照近似推断（保守），明确标 inferred 源。

    上市日期我们可以拿来近似，但 ST / 停牌 / 整理期不应用当前值回填历史。
    """
    # 判断历史当天是否已上市（从 listing_date 近似）
    listing_date = snap.listing_date
    # ST / 停牌 / 整理期：历史上不使用当前 is_st、is_active 做判断，写 False
    return SecurityStatusDaily(
        symbol_id=snap.symbol_id,
        trade_date=trade_date,
        is_st=False,
        is_suspended=False,
        is_delisting_period=False,
        is_listed=True,   # 近似：不主动写摘牌（除非明确 delisting_date < trade_date 但我们不知道）
        listing_date=listing_date,
        delisting_date=snap.delisting_date,
        status_source=SRC_CURRENT_SYMBOL,
        source_updated_at=now_ts,
        as_of_batch_id=as_of_batch_id,
    )


# ---------------------------------------------------------------------------
# 批量 backfill：对 [start_date, end_date] × symbol_ids 叉积，缺失行写 UNKNOWN
# ---------------------------------------------------------------------------
def backfill_status_daily(
    db: Session,
    start_date: date,
    end_date: date,
    symbol_ids: list[int] | None = None,
    batch_size: int = 10000,
    source_hint: str = "missing_historical",
) -> dict[str, int]:
    """保守批量填充 security_status_daily。

    策略：对每一 trade_date × symbol_id，若已存在行则 SKIP；
    否则写入 UNKNOWN 占位（status_source=missing_historical 或 inferred_current_snapshot），
    保证 status_batch 调用方不会因空表而看不到数据，但调用方可根据 status=UNKNOWN 阻断生产回测。

    返回统计 dict：{'inserted': N, 'skipped_existing': M, 'symbols': K, 'trade_days': D}
    """
    if start_date > end_date:
        raise ValueError(f"backfill_status_daily: start_date {start_date} > end_date {end_date}")
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")

    now_ts = datetime.utcnow()
    batch_id = f"bfg_backfill_{now_ts.strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}"

    # 1) 取全量 symbol_ids
    snapshots = extract_current_symbol_snapshots(db, symbol_ids)
    snap_by_id = {s.symbol_id: s for s in snapshots}
    if not snap_by_id:
        return {"inserted": 0, "skipped_existing": 0, "symbols": 0, "trade_days": 0, "batch_id": batch_id}

    # 2) 展开日期序列（按自然日 1 天步长；后续通过交易日过滤由调用方控制）
    days: list[date] = []
    d = start_date
    while d <= end_date:
        days.append(d)
        d += timedelta(days=1)
    if not days:
        return {"inserted": 0, "skipped_existing": 0, "symbols": len(snap_by_id), "trade_days": 0, "batch_id": batch_id}

    inserted = 0
    skipped = 0

    # 3) 先查询范围内已存在的 (symbol_id, trade_date) 集合并做 in-memory 避免重复 SELECT N+1
    #    但一次性加载太多可能内存紧张，按日期分块走。
    date_chunk_size = max(1, min(50, batch_size // max(1, len(snap_by_id))))
    sid_list = list(snap_by_id.keys())
    for chunk_start in range(0, len(days), date_chunk_size):
        date_chunk = days[chunk_start:chunk_start + date_chunk_size]
        # 查询现有 (symbol_id, trade_date) pairs
        existing = set(db.execute(
            select(SecurityStatusDaily.symbol_id, SecurityStatusDaily.trade_date).where(
                SecurityStatusDaily.symbol_id.in_(sid_list),
                SecurityStatusDaily.trade_date.in_(date_chunk),
            )
        ).all())
        rows_to_insert: list[SecurityStatusDaily] = []
        for td in date_chunk:
            for sid in sid_list:
                key = (sid, td)
                if key in existing:
                    skipped += 1
                    continue
                snap = snap_by_id.get(sid)
                # 如果该 symbol 有 listing_date 或已知信息，用 inferred；否则 UNKNOWN
                # 但不使用当前 is_st/is_active 回填历史 ST/停牌
                if source_hint == SRC_CURRENT_SYMBOL and snap is not None:
                    row = _build_snapshot_inferred_row(snap, td, batch_id, now_ts)
                else:
                    row = _build_unknown_row(sid, td, source_hint or SRC_MISSING, batch_id, now_ts)
                rows_to_insert.append(row)
                if len(rows_to_insert) >= batch_size:
                    db.add_all(rows_to_insert)
                    inserted += len(rows_to_insert)
                    rows_to_insert = []
        if rows_to_insert:
            db.add_all(rows_to_insert)
            inserted += len(rows_to_insert)
        # 每日期块 flush 一次（不 commit 由外层事务控制）
        db.flush()

    return {
        "inserted": inserted,
        "skipped_existing": skipped,
        "symbols": len(snap_by_id),
        "trade_days": len(days),
        "batch_id": batch_id,
    }
