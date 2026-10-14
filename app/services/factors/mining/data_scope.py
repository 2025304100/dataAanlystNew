"""Frozen experimental universe shared by mining execution and final evaluation."""
from __future__ import annotations

import json

from app.models.mining_candidate_pool import TrainingCandidatePoolSnapshot
from app.schemas.errors import factor_structured_error


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
