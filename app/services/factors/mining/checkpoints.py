"""Durable completed-generation checkpoints for the public mining worker lifecycle."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from sqlalchemy import select

from app.models.factor_mining import FactorTrainingCheckpoint


def input_hash(payload: Mapping[str, Any]) -> str:
    inputs = {k: v for k, v in payload.items() if k != "resume_from_checkpoint"}
    return hashlib.sha256(json.dumps(inputs, sort_keys=True, default=str).encode()).hexdigest()


def latest_checkpoint(db: Any, run_id: str) -> FactorTrainingCheckpoint | None:
    return db.execute(select(FactorTrainingCheckpoint).where(
        FactorTrainingCheckpoint.run_id == run_id,
        FactorTrainingCheckpoint.stage == "evolution",
    ).order_by(FactorTrainingCheckpoint.generation.desc())).scalars().first()


def save_checkpoint(db: Any, *, run_id: str, payload: Mapping[str, Any],
                    state: Mapping[str, Any], resumable: bool = True) -> None:
    generation = int(state["next_generation"])
    row = db.execute(select(FactorTrainingCheckpoint).where(
        FactorTrainingCheckpoint.run_id == run_id,
        FactorTrainingCheckpoint.stage == "evolution",
        FactorTrainingCheckpoint.generation == generation,
    )).scalar_one_or_none()
    if row is None:
        row = FactorTrainingCheckpoint(run_id=run_id, stage="evolution", generation=generation)
        db.add(row)
    row.input_snapshot_hash = input_hash(payload)
    row.random_seed = int(payload.get("random_seed", 42))
    row.resumable = int(resumable)
    row.result_location = json.dumps(dict(state), ensure_ascii=False, allow_nan=False)
    db.commit()


def restore_checkpoint(db: Any, *, run_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    row = latest_checkpoint(db, run_id)
    if row is None or not row.resumable:
        raise ValueError("MINING_CHECKPOINT_UNAVAILABLE: 无可恢复的完整代断点，请复制为新草稿。")
    if row.input_snapshot_hash != input_hash(payload):
        raise ValueError("MINING_CHECKPOINT_INPUT_CHANGED: 断点与原实验输入不一致，请复制为新草稿。")
    return json.loads(row.result_location or "{}")
