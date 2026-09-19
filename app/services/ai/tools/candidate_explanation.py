"""get_candidate_explanation 工具：返回候选解释。"""
from __future__ import annotations

import json
import logging

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.services.ai.context_pack import ContextPack

logger = logging.getLogger(__name__)


def get_candidate_explanation(db: Session, context: ContextPack) -> dict:
    """返回候选解释：评分来源、因子贡献、排名。

    依赖 context.references.candidate_id 或 scan_result_id。

    Returns:
        {
            "status": "ok",
            "data": {
                "candidate": {...},  # 候选基本信息
                "scores": {...},  # 各维度评分
                "rank": int|None,
                "reason_tags": [...],
                "scoring_config": {...},
            }
        }
        或 {"status": "no_data", "message": "..."}
    """
    candidate_id = context.references.get("candidate_id")
    scan_result_id = context.references.get("scan_result_id")

    # 优先用 DiscoveryCandidate，其次 ScanResult
    if candidate_id is not None:
        try:
            from app.models.discovery_candidate import DiscoveryCandidate
            cand = db.execute(
                select(DiscoveryCandidate).where(DiscoveryCandidate.id == candidate_id)
            ).scalars().first()
            if cand is None:
                return {"status": "no_data", "message": f"候选 candidate_id={candidate_id} 不存在"}

            dimension_scores = {}
            if cand.dimension_scores_json:
                try:
                    dimension_scores = json.loads(cand.dimension_scores_json) or {}
                except (ValueError, TypeError):
                    dimension_scores = {}

            scoring_config = {}
            if cand.scoring_config_snapshot_json:
                try:
                    scoring_config = json.loads(cand.scoring_config_snapshot_json) or {}
                except (ValueError, TypeError):
                    scoring_config = {}

            reason_tags = []
            if cand.reason_tags:
                try:
                    parsed = json.loads(cand.reason_tags)
                    if isinstance(parsed, list):
                        reason_tags = parsed
                    else:
                        reason_tags = [str(parsed)]
                except (ValueError, TypeError):
                    reason_tags = []

            return {
                "status": "ok",
                "data": {
                    "candidate": {
                        "id": cand.id,
                        "symbol": cand.symbol,
                        "name": cand.name,
                        "asset_type": cand.asset_type,
                        "stage": cand.stage,
                        "action": cand.action,
                        "is_promoted": bool(cand.is_promoted),
                    },
                    "scores": {
                        "quality_score": cand.quality_score,
                        "timing_score": cand.timing_score,
                        "priority_score": cand.priority_score,
                        "dimension_scores": dimension_scores,
                    },
                    "rank": None,  # DiscoveryCandidate 无 rank 字段
                    "reason_tags": reason_tags,
                    "scoring_config": scoring_config,
                    "warning_days": cand.warning_days,
                    "valid_days": cand.valid_days,
                },
            }
        except Exception as exc:
            logger.warning("get_candidate_explanation discovery query failed: %s", exc)
            return {"status": "no_data", "message": f"候选查询失败：{type(exc).__name__}"}

    # 退回 ScanResult
    if scan_result_id is not None:
        try:
            from app.models.scan import ScanResult
            result = db.execute(
                select(ScanResult).where(ScanResult.id == scan_result_id)
            ).scalars().first()
            if result is None:
                return {"status": "no_data", "message": f"扫描结果 scan_result_id={scan_result_id} 不存在"}

            reason_tags = []
            if result.reason_tags:
                try:
                    parsed = json.loads(result.reason_tags)
                    if isinstance(parsed, list):
                        reason_tags = parsed
                    else:
                        reason_tags = [str(parsed)]
                except (ValueError, TypeError):
                    reason_tags = []

            return {
                "status": "ok",
                "data": {
                    "candidate": {
                        "id": result.id,
                        "symbol_id": result.symbol_id,
                        "result_type": result.result_type,
                        "stage": result.stage,
                        "action": result.action,
                    },
                    "scores": {
                        "quality_score": result.quality_score,
                        "timing_score": result.timing_score,
                        "priority_score": result.priority_score,
                    },
                    "rank": result.rank_no,
                    "reason_tags": reason_tags,
                    "scoring_config": {},
                    "warning_days": result.warning_days,
                    "valid_days": result.valid_days,
                    "is_frozen": bool(result.is_frozen),
                    "recommended_position_pct": result.recommended_position_pct,
                },
            }
        except Exception as exc:
            logger.warning("get_candidate_explanation scan_result query failed: %s", exc)
            return {"status": "no_data", "message": f"扫描结果查询失败：{type(exc).__name__}"}

    return {"status": "no_data", "message": "未指定 candidate_id 或 scan_result_id"}


__all__ = ["get_candidate_explanation"]
