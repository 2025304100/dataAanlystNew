"""Frozen experimental universe shared by mining execution and final evaluation."""
from __future__ import annotations

import json
from datetime import date, datetime

from app.models.mining_candidate_pool import TrainingCandidatePoolSnapshot
from app.schemas.errors import factor_structured_error


def scoped_target_preview(db, warehouse, *, snapshot_id: str, start_date, end_date,
                          data_cutoff_at, target_horizon: int, frequency: str):
    """Production label rules + full-calendar schedule, with no warehouse writes."""
    from app.services.factors.mining.evaluation_adapter import select_signal_dates
    from app.services.factors.target_engine import preview_targets

    def as_date(value):
        return value.date() if isinstance(value, datetime) else (
            value if isinstance(value, date) else date.fromisoformat(str(value)[:10]))

    symbols = load_snapshot_symbols(db, snapshot_id)
    lo, hi, cutoff = as_date(start_date), as_date(end_date), as_date(data_cutoff_at)
    if lo >= hi or hi > cutoff:
        raise ValueError("时间范围无效，结束日必须不晚于冻结截止日。")
    targets = preview_targets(warehouse, start_date=lo, end_date=hi,
        target_horizon=int(target_horizon), symbols=symbols, as_of_exit_date=cutoff)
    targets = targets.loc[targets["is_tradable"] & targets["target_value"].notna()]
    scheduled = set(select_signal_dates(warehouse.list_trade_dates(), frequency))
    targets = targets.loc[targets["signal_date"].isin(scheduled)]
    return symbols, targets


def load_snapshot_symbols(db, snapshot_id: str) -> tuple[str, ...]:
    snapshot = db.get(TrainingCandidatePoolSnapshot, str(snapshot_id))
    try:
        members = json.loads(snapshot.members_json) if snapshot is not None else None
        if snapshot is None or not snapshot.is_locked or not isinstance(members, list) or not members:
            raise ValueError("missing or unlocked snapshot")
        symbols = []
        for member in members:
            symbol = member.get("symbol") if isinstance(member, dict) else member
            if not isinstance(symbol, str) or not symbol.strip():
                raise ValueError("snapshot member has no symbol")
            symbols.append(symbol.strip())
        return tuple(sorted(set(symbols)))
    except (ValueError, TypeError, AttributeError):
        raise factor_structured_error("MINING_SNAPSHOT_SCOPE_INVALID",
            title_zh="候选池快照标的不完整", detail_zh="快照未锁定、已损坏或缺少冻结标的，请重新生成挖掘物料。",
            impact="未执行挖掘，不会改用全市场标的", fix_link="/settings/factor-mining?step=1",
            retryable=False, extras={"snapshot_id": str(snapshot_id)}) from None
