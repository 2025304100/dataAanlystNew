from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.portfolio import Position
from app.models.scan import ScanResult, ScanRun
from app.models.score import Score
from app.models.symbol import Symbol
from app.models.watchlist import WatchlistItem
from app.services.allocation import compute_recommended_position_pct


def _apply_filters_to_score(score: Score, filters_snapshot: dict) -> tuple[bool, list[str]]:
    """对单个 score 应用维度阈值和 min_score 过滤。

    优先使用 score 自带的 dimension_scores_json + 配置快照进行过滤；
    若 score 无配置快照，则按 filters_snapshot 中的 dimension_filters 兜底。
    返回 (passed, failed_dim_names)。
    """
    failed: list[str] = []

    # 1) min_score 优先级阈值（保留旧逻辑）
    min_score = filters_snapshot.get("min_score")
    if min_score is not None and score.priority_score is not None and score.priority_score < float(min_score):
        failed.append(f"priority_score < {min_score}")

    # 2) 配置预设中的维度硬性阈值
    dim_scores_dict: dict[str, float] = {}
    config_json_dict: dict = {}
    try:
        if score.dimension_scores_json:
            dim_scores_dict = json.loads(score.dimension_scores_json)
        if score.scoring_config_snapshot_json:
            config_json_dict = json.loads(score.scoring_config_snapshot_json)
    except (json.JSONDecodeError, TypeError):
        dim_scores_dict, config_json_dict = {}, {}

    if config_json_dict:
        for dim in config_json_dict.get("dimensions", []):
            if not dim.get("enabled", True):
                continue
            flt = dim.get("filter") or {}
            if not flt.get("enabled", False):
                continue
            dkey = dim["key"]
            dscore = dim_scores_dict.get(dkey)
            if dscore is None:
                failed.append(dim.get("name", dkey))
                continue
            op = flt.get("operator", "gte")
            threshold = float(flt.get("value", 0))
            if op == "gte" and not (dscore >= threshold):
                failed.append(dim.get("name", dkey))
            elif op == "lte" and not (dscore <= threshold):
                failed.append(dim.get("name", dkey))
            elif op == "gt" and not (dscore > threshold):
                failed.append(dim.get("name", dkey))
            elif op == "lt" and not (dscore < threshold):
                failed.append(dim.get("name", dkey))
    else:
        # 3) 无配置快照兜底：filters_snapshot.dimension_filters
        for f in filters_snapshot.get("dimension_filters", []) or []:
            field = f.get("field")
            op = f.get("operator", "gte")
            value = float(f.get("value", 0))
            # 从 score 上取对应字段
            attr_val = getattr(score, field, None) if field else None
            if attr_val is None:
                # 兼容 dimension_scores_json 中的 key
                attr_val = dim_scores_dict.get(field or "")
            if attr_val is None:
                failed.append(field or "unknown")
                continue
            try:
                attr_val_f = float(attr_val)
            except (TypeError, ValueError):
                failed.append(field or "unknown")
                continue
            if op == "gte" and not (attr_val_f >= value):
                failed.append(field or "unknown")
            elif op == "lte" and not (attr_val_f <= value):
                failed.append(field or "unknown")
            elif op == "gt" and not (attr_val_f > value):
                failed.append(field or "unknown")
            elif op == "lt" and not (attr_val_f < value):
                failed.append(field or "unknown")

    return (len(failed) == 0, failed)


def _with_active_scoring_snapshot(db: Session, scope_snapshot: dict, filters_snapshot: dict | None) -> dict:
    """Attach active scoring preset info when the scan scope has one clear asset type."""
    filters = dict(filters_snapshot or {})
    if filters.get("scoring_config_id") is not None:
        return filters

    asset_types = scope_snapshot.get("asset_types") or []
    if isinstance(asset_types, str):
        asset_types = [asset_types]
    asset_types = [item for item in asset_types if item in ("stock", "etf")]
    if len(set(asset_types)) != 1:
        return filters

    try:
        from app.services.scoring_config_engine import get_active_scoring_config
        active = get_active_scoring_config(db, asset_types[0])
    except Exception:
        active = None
    if active is None:
        return filters

    filters.setdefault("scoring_config_id", active.id)
    filters.setdefault("scoring_preset_key", active.preset_key)
    filters.setdefault("scoring_preset_name", active.name)
    filters.setdefault("scoring_config_version", active.version)
    return filters


def run_scan(
    db: Session,
    scope_snapshot: dict,
    filters_snapshot: dict | None,
    portfolio_id: int | None,
    portfolio_rule_id: int | None,
    run_name: str | None,
    preset_id: int | None,
) -> ScanRun:
    filters_dict = _with_active_scoring_snapshot(db, scope_snapshot, filters_snapshot)
    scan_run = ScanRun(
        preset_id=preset_id,
        run_name=run_name or f"scan-{datetime.now(timezone.utc).replace(tzinfo=None).strftime('%Y%m%d%H%M%S')}",
        scope_snapshot=json.dumps(scope_snapshot, ensure_ascii=True),
        filters_snapshot=json.dumps(filters_dict, ensure_ascii=True),
        portfolio_id=portfolio_id,
        portfolio_rule_id=portfolio_rule_id,
        status="running",
        started_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db.add(scan_run)
    db.flush()

    stmt = select(Symbol).where(Symbol.is_active == 1)
    asset_types = scope_snapshot.get("asset_types") or []
    markets = scope_snapshot.get("markets") or []
    boards = scope_snapshot.get("boards") or []
    symbol_ids = scope_snapshot.get("symbol_ids") or []
    watchlist_id = scope_snapshot.get("watchlist_id")
    if asset_types:
        stmt = stmt.where(Symbol.asset_type.in_(asset_types))
    if markets:
        stmt = stmt.where(Symbol.market.in_(markets))
    if boards:
        stmt = stmt.where(Symbol.board.in_(boards))
    if symbol_ids:
        stmt = stmt.where(Symbol.id.in_(symbol_ids))
    if watchlist_id is not None:
        stmt = stmt.join(WatchlistItem, WatchlistItem.symbol_id == Symbol.id).where(WatchlistItem.watchlist_id == watchlist_id)

    symbols = db.execute(stmt).scalars().all()

    # 批量查询每个 symbol 的最新 score，避免 N+1。
    # 若扫描任务携带当前评分配置 id/version，则只使用该配置下的评分，避免同日多版本分数串用。
    symbol_ids = [s.id for s in symbols]
    latest_scores: list[tuple[Symbol, Score]] = []
    if symbol_ids:
        score_stmt = select(Score).where(Score.symbol_id.in_(symbol_ids))
        scoring_config_id = filters_dict.get("scoring_config_id")
        scoring_config_version = filters_dict.get("scoring_config_version")
        if scoring_config_id is not None:
            score_stmt = score_stmt.where(Score.scoring_config_id == int(scoring_config_id))
        if scoring_config_version is not None:
            score_stmt = score_stmt.where(Score.scoring_config_version == int(scoring_config_version))
        score_rows = db.execute(
            score_stmt.order_by(Score.symbol_id.asc(), Score.trade_date.desc(), Score.id.desc())
        ).scalars().all()
        score_map: dict[int, Score] = {}
        for score in score_rows:
            if score.symbol_id not in score_map:
                score_map[score.symbol_id] = score
        for symbol in symbols:
            score = score_map.get(symbol.id)
            if score is not None:
                latest_scores.append((symbol, score))

    # P0：应用维度阈值过滤
    if filters_dict:
        filtered: list[tuple[Symbol, Score]] = []
        for symbol, score in latest_scores:
            passed, _ = _apply_filters_to_score(score, filters_dict)
            if passed:
                filtered.append((symbol, score))
        latest_scores = filtered

    latest_scores.sort(key=lambda item: item[1].priority_score, reverse=True)

    executable_rank = 0
    quality_rank = 0
    timing_rank = 0
    for symbol, score in latest_scores:
        quality_rank += 1
        db.add(
            ScanResult(
                scan_run_id=scan_run.id,
                symbol_id=symbol.id,
                result_type="quality",
                rank_no=quality_rank,
                quality_score=score.quality_score,
                timing_score=score.timing_score,
                priority_score=score.priority_score,
                stage=score.stage,
                action=score.action,
                reason_tags="quality_rank",
            )
        )

    for symbol, score in sorted(latest_scores, key=lambda item: item[1].timing_score, reverse=True):
        timing_rank += 1
        db.add(
            ScanResult(
                scan_run_id=scan_run.id,
                symbol_id=symbol.id,
                result_type="timing",
                rank_no=timing_rank,
                quality_score=score.quality_score,
                timing_score=score.timing_score,
                priority_score=score.priority_score,
                stage=score.stage,
                action=score.action,
                reason_tags="timing_rank",
            )
        )

    for symbol, score in latest_scores:
        recommended_pct = None
        is_sector_overweight = 0
        is_asset_overweight = 0
        if portfolio_id is not None:
            recommended_pct, sector_flag, asset_flag = compute_recommended_position_pct(
                db=db,
                portfolio_id=portfolio_id,
                symbol=symbol,
                stage=score.stage,
            )
            is_sector_overweight = int(sector_flag)
            is_asset_overweight = int(asset_flag)

        if score.action in {"open", "buy_dip", "hold"} and (recommended_pct is None or recommended_pct > 0):
            executable_rank += 1
            db.add(
                ScanResult(
                    scan_run_id=scan_run.id,
                    symbol_id=symbol.id,
                    result_type="executable",
                    rank_no=executable_rank,
                    quality_score=score.quality_score,
                    timing_score=score.timing_score,
                    priority_score=score.priority_score,
                    stage=score.stage,
                    action=score.action,
                    recommended_position_pct=recommended_pct,
                    is_sector_overweight=is_sector_overweight,
                    is_asset_overweight=is_asset_overweight,
                    reason_tags="executable_candidate",
                )
            )

    scan_run.status = "done"
    scan_run.finished_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.flush()
    return scan_run

