import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.score import Score
from app.models.symbol import Symbol
from app.schemas.score import ScoreCalculationRequest, ScoreRead
from app.services.analysis import calculate_symbol_score
from app.services.factors.score_scope import apply_active_score_scope

logger = logging.getLogger(__name__)


router = APIRouter()


@router.post("/scores/calculate", response_model=list[ScoreRead])
def calculate_scores(payload: ScoreCalculationRequest, db: Session = Depends(get_db)):
    # 风控加固：批量评分单 symbol 失败隔离
    # 任一 symbol 评分失败（数据缺失/计算异常）不应中断整个批量，已成功的 scores 必须保留
    # 每个 symbol 成功后立即 commit，避免后续 symbol 失败 rollback 时回滚已成功的 score
    scores: list[Score] = []
    failed: list[dict] = []

    # 风控加固：批量预加载 Symbol 避免 N+1（原循环内 db.get 改为 dict 查找）
    symbol_rows = db.execute(
        select(Symbol).where(Symbol.id.in_(payload.symbol_ids))
    ).scalars().all()
    symbol_map: dict[int, Symbol] = {s.id: s for s in symbol_rows}

    for symbol_id in payload.symbol_ids:
        symbol = symbol_map.get(symbol_id)
        if symbol is None:
            failed.append({"symbol_id": symbol_id, "error": "Symbol not found"})
            continue
        try:
            score = calculate_symbol_score(db, symbol, payload.trade_date)
            db.commit()  # 立即提交，保护已成功 score 不被后续 rollback
            db.refresh(score)
            scores.append(score)
        except Exception as exc:
            db.rollback()
            failed.append({"symbol_id": symbol_id, "symbol": symbol.symbol, "error": str(exc)})
            logger.warning("calculate_score failed for %s: %s", symbol.symbol, exc)
    if not scores:
        # 全部失败 → 返回 422 让前端感知
        raise HTTPException(
            status_code=422,
            detail=f"All {len(payload.symbol_ids)} symbol(s) scoring failed: {failed[:3]}",
        )
    if failed:
        logger.warning("calculate_scores partial failure: %d ok / %d failed", len(scores), len(failed))
    return scores


@router.get("/scores/latest/{symbol_id}", response_model=ScoreRead)
def get_latest_score(symbol_id: int, db: Session = Depends(get_db)):
    score = db.execute(
        apply_active_score_scope(
            select(Score).where(Score.symbol_id == symbol_id),
            db,
        ).order_by(desc(Score.trade_date), desc(Score.id))
    ).scalars().first()
    if score is None:
        raise HTTPException(status_code=404, detail="Score not found")
    return score


@router.get("/scores/history/{symbol_id}", response_model=list[ScoreRead])
def get_score_history(symbol_id: int, limit: int = 60, db: Session = Depends(get_db)):
    return db.execute(
        apply_active_score_scope(
            select(Score).where(Score.symbol_id == symbol_id),
            db,
        )
        .order_by(desc(Score.trade_date), desc(Score.id))
        .limit(limit)
    ).scalars().all()
