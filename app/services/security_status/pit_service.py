"""Point-in-Time 证券状态查询服务。

所有历史状态查询必须通过 `status_at` / `status_batch`，
禁止直接读取当前静态名称、symbol.is_active 或 raw_asset_universe 作为历史判断。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.security_status import SecurityStatusDaily


# 生效状态枚举（字符串字面量，便于 JSON 传输）
SecurityStatusValue = Literal[
    "UNKNOWN",
    "LISTED",
    "ST",
    "SUSPENDED",
    "DELISTING_PERIOD",
    "DELISTED",
]


class SecurityStatusDTO(BaseModel):
    """证券状态 DTO（与调用方契约，不直接暴露 ORM）。"""
    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol_id: int
    trade_date: date
    status: SecurityStatusValue
    # 辅助字段：上市天数（None = 未知）
    listing_age_calendar_days: int | None = None
    # 辅助字段：距正式摘牌天数（正数=已摘牌 N 天；负数=还有 N 天摘牌；None=未知）
    delisting_days_ago: int | None = None
    # 原始来源，用于审计
    raw_source: str = "unknown"
    listing_date: date | None = None
    delisting_date: date | None = None
    is_st: bool = False
    is_suspended: bool = False
    is_delisting_period: bool = False
    is_listed: bool = True


@dataclass(frozen=True)
class SecurityStatusPitService:
    """无状态 PIT 查询服务，严格依赖传入 DB session。"""

    # ------------------------------------------------------------------
    # 单条查询
    # ------------------------------------------------------------------
    @staticmethod
    def status_at(db: Session, symbol_id: int, trade_date: date) -> SecurityStatusDTO:
        """返回某 symbol 在 trade_date 的 PIT 状态；查不到返回 UNKNOWN。"""
        row = db.execute(
            select(SecurityStatusDaily).where(
                SecurityStatusDaily.symbol_id == int(symbol_id),
                SecurityStatusDaily.trade_date == trade_date,
            ).limit(1)
        ).scalar_one_or_none()
        if row is None:
            return SecurityStatusDTO.model_construct(
                symbol_id=int(symbol_id),
                trade_date=trade_date,
                status="UNKNOWN",
                raw_source="missing_from_table",
            )
        return SecurityStatusPitService._row_to_dto(row, trade_date)

    # ------------------------------------------------------------------
    # 批量查询：一次 IN SQL，避免 N+1
    # ------------------------------------------------------------------
    @staticmethod
    def status_batch(
        db: Session,
        symbol_ids: list[int],
        trade_date: date,
    ) -> dict[int, SecurityStatusDTO]:
        """批量返回 symbol_ids 在 trade_date 的状态。

        返回 dict[symbol_id] = SecurityStatusDTO；对未查询到的 symbol 填充 UNKNOWN。
        仅发送 1 条 SQL。
        """
        normalized = [int(s) for s in dict.fromkeys(symbol_ids)]  # 去重保序
        result_map: dict[int, SecurityStatusDTO] = {}
        if not normalized:
            return result_map
        rows = db.execute(
            select(SecurityStatusDaily).where(
                SecurityStatusDaily.symbol_id.in_(normalized),
                SecurityStatusDaily.trade_date == trade_date,
            )
        ).scalars().all()
        for row in rows:
            result_map[int(row.symbol_id)] = SecurityStatusPitService._row_to_dto(row, trade_date)
        # 填充缺失的 symbol 为 UNKNOWN
        for sid in normalized:
            if sid not in result_map:
                result_map[sid] = SecurityStatusDTO.model_construct(
                    symbol_id=sid,
                    trade_date=trade_date,
                    status="UNKNOWN",
                    raw_source="missing_from_table",
                )
        return result_map

    # ------------------------------------------------------------------
    # 内部：ORM row -> DTO 映射（含优先级聚合逻辑）
    # ------------------------------------------------------------------
    @staticmethod
    def _row_to_dto(row: SecurityStatusDaily, trade_date: date) -> SecurityStatusDTO:
        # 优先级：DELISTED > SUSPENDED > ST > DELISTING_PERIOD > LISTED
        listing_date = row.listing_date
        delisting_date = row.delisting_date

        listing_age: int | None = None
        if listing_date is not None:
            listing_age = (trade_date - listing_date).days

        delisting_days: int | None = None
        if delisting_date is not None:
            delisting_days = (trade_date - delisting_date).days

        is_listed = bool(row.is_listed)
        is_st = bool(row.is_st)
        is_suspended = bool(row.is_suspended)
        is_dp = bool(row.is_delisting_period)

        # 状态聚合（优先级按需求文档"退市>停牌>ST>整理期>上市"）
        # 先判断摘牌
        if not is_listed or (delisting_date is not None and trade_date > delisting_date):
            status: SecurityStatusValue = "DELISTED"
        elif is_suspended:
            status = "SUSPENDED"
        elif is_st:
            status = "ST"
        elif is_dp:
            status = "DELISTING_PERIOD"
        else:
            status = "LISTED"

        # ORM 已由 SQLAlchemy 按列类型 materialize；避免在 5k-symbol 批量
        # 回测中对每个 DTO 重复执行 Pydantic schema 校验。
        return SecurityStatusDTO.model_construct(
            symbol_id=int(row.symbol_id),
            trade_date=trade_date,
            status=status,
            listing_age_calendar_days=listing_age,
            delisting_days_ago=delisting_days,
            raw_source=row.status_source or "unknown",
            listing_date=listing_date,
            delisting_date=delisting_date,
            is_st=is_st,
            is_suspended=is_suspended,
            is_delisting_period=is_dp,
            is_listed=is_listed,
        )
