from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.models.portfolio import Position
from app.models.scan import ScanResult, ScanRun
from app.models.score import Score
from app.models.symbol import Symbol
from app.models.watchlist import WatchlistItem
from app.services.allocation import compute_recommended_position_pct


def run_scan(
    db: Session,
    scope_snapshot: dict,
    filters_snapshot: dict | None,
    portfolio_id: int | None,
    portfolio_rule_id: int | None,
    run_name: str | None,
    preset_id: int | None,
) -> ScanRun:
    scan_run = ScanRun(
        preset_id=preset_id,
        run_name=run_name or f"scan-{datetime.now(timezone.utc).replace(tzinfo=None).strftime('%Y%m%d%H%M%S')}",
        scope_snapshot=json.dumps(scope_snapshot, ensure_ascii=True),
        filters_snapshot=json.dumps(filters_snapshot or {}, ensure_ascii=True),
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

    # 批量查询每个 symbol 的最新 score，避免 N+1
    symbol_ids = [s.id for s in symbols]
    latest_scores: list[tuple[Symbol, Score]] = []
    if symbol_ids:
        # 子查询：每个 symbol 的最新 trade_date 对应的 score
        latest_score_subq = (
            select(
                Score.symbol_id,
                func.max(Score.trade_date).label("max_date"),
            )
            .where(Score.symbol_id.in_(symbol_ids))
            .group_by(Score.symbol_id)
            .subquery()
        )
        score_rows = db.execute(
            select(Score)
            .join(
                latest_score_subq,
                (Score.symbol_id == latest_score_subq.c.symbol_id)
                & (Score.trade_date == latest_score_subq.c.max_date),
            )
        ).scalars().all()
        score_map = {s.symbol_id: s for s in score_rows}
        for symbol in symbols:
            score = score_map.get(symbol.id)
            if score is not None:
                latest_scores.append((symbol, score))

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
