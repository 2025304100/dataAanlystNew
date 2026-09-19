from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.score import Score
from app.models.symbol import Symbol
from app.schemas.errors import UserError
from app.schemas.symbol import SymbolCreate, SymbolRead
from app.schemas.symbol_relationships import SymbolRelationships
from app.services.factors.score_scope import apply_active_score_scope
from app.services.regions import region_from_market
from app.services.symbol_cleanup import cleanup_stale_discovery_symbols
from app.services.symbol_names import refresh_symbol_name
from app.services.symbol_relationships import get_symbol_relationships


router = APIRouter()


def _serialize_symbol(symbol: Symbol) -> SymbolRead:
    return SymbolRead.model_validate({**symbol.__dict__, "region": region_from_market(symbol.market)})


@router.get("/symbols", response_model=list[SymbolRead])
def list_symbols(
    keyword: str | None = Query(default=None),
    asset_type: str | None = Query(default=None),
    market: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    include_inactive: bool = Query(default=False, description="是否包含已停用（is_active=0）的僵尸标的，默认只返回活跃标的"),
    db: Session = Depends(get_db),
):
    stmt = select(Symbol)
    # 默认只返回 is_active=1 的活跃标的，避免挖掘清理后的僵尸标的污染前端下拉框
    if not include_inactive:
        stmt = stmt.where(Symbol.is_active == 1)
    if keyword:
        # 转义用户输入中的 LIKE 通配符（%、_、\），防止关键词被当作通配符导致误匹配
        escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like_pattern = f"%{escaped}%"
        stmt = stmt.where(
            (Symbol.symbol.like(like_pattern, escape="\\")) | (Symbol.name.like(like_pattern, escape="\\"))
        )
    if asset_type:
        stmt = stmt.where(Symbol.asset_type == asset_type)
    if market:
        stmt = stmt.where(Symbol.market == market)
    stmt = stmt.order_by(Symbol.id.desc()).offset((page - 1) * page_size).limit(page_size)
    return [_serialize_symbol(symbol) for symbol in db.execute(stmt).scalars().all()]


@router.post("/symbols", response_model=SymbolRead)
def create_symbol(payload: SymbolCreate, db: Session = Depends(get_db)):
    existing = db.execute(select(Symbol).where(Symbol.symbol == payload.symbol)).scalars().first()
    if existing:
        raise HTTPException(status_code=409, detail="Symbol already exists")
    symbol = Symbol(**payload.model_dump())
    refresh_symbol_name(symbol)
    db.add(symbol)
    db.commit()
    db.refresh(symbol)
    return _serialize_symbol(symbol)


@router.get("/symbols/{symbol_id}")
def get_symbol_detail(symbol_id: int, db: Session = Depends(get_db)):
    symbol = db.get(Symbol, symbol_id)
    if symbol is None:
        raise HTTPException(status_code=404, detail="Symbol not found")
    latest_score = db.execute(
        apply_active_score_scope(
            select(Score).where(Score.symbol_id == symbol_id),
            db,
        ).order_by(desc(Score.trade_date), desc(Score.id))
    ).scalars().first()
    return {
        "symbol": _serialize_symbol(symbol),
        "latest_score": None
        if latest_score is None
        else {
            "quality_score": latest_score.quality_score,
            "quality_grade": latest_score.quality_grade,
            "timing_score": latest_score.timing_score,
            "stage": latest_score.stage,
            "action": latest_score.action,
            "priority_score": latest_score.priority_score,
            "trade_date": latest_score.trade_date,
            # 股质评分分项
            "trend_score": latest_score.trend_score,
            "momentum_score": latest_score.momentum_score,
            "volatility_score": latest_score.volatility_score,
            "liquidity_score": latest_score.liquidity_score,
            "breadth_score": latest_score.breadth_score,
            "event_score": latest_score.event_score,
            # 时点评分分项（P1 新增）
            "breakout_score": latest_score.breakout_score,
            "pullback_score": latest_score.pullback_score,
            "overheat_penalty": latest_score.overheat_penalty,
            # 数据可信度（P0-4.3）
            "data_credibility": latest_score.data_credibility,
            "weight_mode": latest_score.weight_mode,
            "factor_model_run_id": latest_score.factor_model_run_id,
            "factor_data_cutoff_at": latest_score.factor_data_cutoff_at,
            "factor_quality_score": latest_score.factor_quality_score,
            "factor_timing_score": latest_score.factor_timing_score,
            "model_alpha_score": latest_score.model_alpha_score,
            "macro_regime": latest_score.macro_regime,
            "macro_position_multiplier": latest_score.macro_position_multiplier,
            "factor_scores_json": latest_score.factor_scores_json,
        },
    }


@router.post("/symbols/cleanup-stale")
def cleanup_stale_symbols(db: Session = Depends(get_db)) -> dict:
    """手动清理挖掘遗留的僵尸标的（paused 超 24h / running 僵死超 30m）。"""
    result = cleanup_stale_discovery_symbols(db)
    return {
        "ok": True,
        "cleaned_count": result["total_cleaned"],
        "cleaned_tasks": result["cleaned_task_ids"],
    }


@router.get(
    "/symbols/{symbol_id}/relationships",
    response_model=SymbolRelationships,
    responses={
        404: {"model": UserError, "description": "Symbol not found"},
        500: {"model": UserError, "description": "Internal server error"},
    },
)
def get_symbol_relationships_endpoint(
    symbol_id: int,
    db: Session = Depends(get_db),
) -> SymbolRelationships:
    """获取标的统一关联状态（WP1.5）。

    返回候选/观察/组合成员/持仓/告警五类状态。
    子查询失败时降级（degraded=True）但不抛异常，
    保证前端徽标显示"状态未知"而非误报"未加入"。
    """
    symbol = db.get(Symbol, symbol_id)
    if symbol is None:
        raise HTTPException(status_code=404, detail="Symbol not found")
    return get_symbol_relationships(db, symbol_id=symbol_id)
