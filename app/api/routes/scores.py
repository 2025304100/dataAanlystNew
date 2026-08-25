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

    # WP0-7：对请求传入的 weight_mode / factor_set_id / factor_model_run_id 进行基本合法性校验
    #   - weight_mode 仅允许 manual / ridge（与 ScoreRead.weight_mode 对齐）
    #   - 若 weight_mode == "ridge"，要求至少 factor_model_run_id 或 factor_set_id 其中之一非空
    valid_weight_modes = {"manual", "ridge"}
    request_weight_mode = (payload.weight_mode or "manual").strip().lower()
    if request_weight_mode not in valid_weight_modes:
        raise HTTPException(
            status_code=422,
            detail=f"weight_mode 仅允许 manual/ridge，实际 '{payload.weight_mode}'",
        )
    if request_weight_mode == "ridge":
        if not payload.factor_model_run_id or not payload.factor_set_id:
            raise HTTPException(
                status_code=422,
                detail=(
                    "weight_mode=ridge 时 factor_model_run_id 和 factor_set_id 均为必填，"
                    "用于精确的模型/因子版本溯源"
                ),
            )
        from app.services.factor_model_contract import assess_factor_model_readiness

        readiness = assess_factor_model_readiness(
            db,
            model_run_id=payload.factor_model_run_id,
            expected_factor_set_id=payload.factor_set_id,
            require_runtime_active=True,
        )
        if not readiness.ready:
            raise HTTPException(
                status_code=422,
                detail={
                    "error_code": readiness.code,
                    "message": readiness.message,
                    "detail": readiness.to_dict(),
                },
            )

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
            # WP0-7：写入 Score 溯源字段（覆盖 calculate_symbol_score 的默认值，
            #  保证 Score → FactorSet → FactorModelRun 的 factor_set_id 溯源链完整）
            if request_weight_mode:
                score.weight_mode = request_weight_mode
            if payload.factor_set_id:
                score.factor_set_id = payload.factor_set_id
            if payload.factor_model_run_id:
                score.factor_model_run_id = payload.factor_model_run_id
            db.add(score)
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
