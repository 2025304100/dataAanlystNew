from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.portfolio import Portfolio
from app.models.score import Score
from app.models.symbol import Symbol
from app.schemas.signal_rule import SignalRulePreset, SignalRulePreviewRead, SignalRulePreviewRequest, SignalRuleRead, SignalRuleUpsert
from app.services.signal_stats import build_similar_signal_stats
from app.services.signal_rules import get_active_signal_rule, preset_list, upsert_active_signal_rule


router = APIRouter()


@router.get("/signal-rules/presets", response_model=list[SignalRulePreset])
def list_signal_rule_presets():
    return preset_list()


@router.get("/signal-rules/stats/{symbol_id}")
def get_signal_stats(symbol_id: int, portfolio_id: int = Query(default=1), db: Session = Depends(get_db)):
    """获取指定标的的相似信号统计，用于信号验证。"""
    symbol = db.get(Symbol, symbol_id)
    if symbol is None:
        raise HTTPException(status_code=404, detail="Symbol not found")
    latest_score = (
        db.execute(
            select(Score).where(Score.symbol_id == symbol.id).order_by(desc(Score.trade_date), desc(Score.id))
        )
        .scalars()
        .first()
    )
    stats = build_similar_signal_stats(db, symbol, latest_score, portfolio_id=portfolio_id)
    if stats is None:
        raise HTTPException(status_code=404, detail="当前标的还没有最新评分，先同步并扫描")
    return stats


@router.get("/portfolios/{portfolio_id}/signal-rule", response_model=SignalRuleRead)
def get_signal_rule(portfolio_id: int, db: Session = Depends(get_db)):
    if db.get(Portfolio, portfolio_id) is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    return get_active_signal_rule(db, portfolio_id)


@router.post("/portfolios/{portfolio_id}/signal-rule", response_model=SignalRuleRead)
def save_signal_rule(portfolio_id: int, payload: SignalRuleUpsert, db: Session = Depends(get_db)):
    if db.get(Portfolio, portfolio_id) is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    return upsert_active_signal_rule(db, portfolio_id, payload)


@router.post("/portfolios/{portfolio_id}/signal-rule/preview", response_model=SignalRulePreviewRead)
def preview_signal_rule(portfolio_id: int, payload: SignalRulePreviewRequest, db: Session = Depends(get_db)):
    if db.get(Portfolio, portfolio_id) is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    if payload.symbol_id is None:
        return SignalRulePreviewRead(status="idle", message="请选择一个标的后预览规则")

    symbol = db.get(Symbol, payload.symbol_id)
    if symbol is None:
        raise HTTPException(status_code=404, detail="Symbol not found")
    latest_score = (
        db.execute(
            select(Score).where(Score.symbol_id == symbol.id).order_by(desc(Score.trade_date), desc(Score.id))
        )
        .scalars()
        .first()
    )
    stats = build_similar_signal_stats(db, symbol, latest_score, portfolio_id=portfolio_id, rule_override=payload)
    if stats is None:
        return SignalRulePreviewRead(
            symbol_id=symbol.id,
            symbol=symbol.symbol,
            name=symbol.name,
            status="warn",
            message="当前标的还没有最新评分，先同步并扫描",
            stats=None,
        )

    sample_count = stats.get("sample_count") or 0
    min_sample_count = stats.get("min_sample_count") or payload.min_sample_count
    avg_return_20d = stats.get("avg_return_20d")
    win_rate_20d = stats.get("win_rate_20d")
    avg_drawdown_20d = stats.get("avg_max_drawdown_20d")
    if sample_count < min_sample_count:
        status = "warn"
        message = "规则偏严或历史样本不足，当前信号只适合观察"
    elif avg_return_20d is not None and avg_return_20d < 0:
        status = "warn"
        message = "历史相似信号 20 日平均收益偏弱，建议收紧条件或降低仓位"
    elif win_rate_20d is not None and win_rate_20d < 0.5:
        status = "warn"
        message = "历史相似信号胜率低于 50%，建议谨慎"
    elif avg_drawdown_20d is not None and avg_drawdown_20d < -0.08:
        status = "warn"
        message = "历史最大回撤偏大，建议提高样本门槛或收紧容差"
    else:
        status = "ok"
        message = "当前规则可用，但仍需结合仓位上限和止损计划"

    return SignalRulePreviewRead(
        symbol_id=symbol.id,
        symbol=symbol.symbol,
        name=symbol.name,
        status=status,
        message=message,
        stats=stats,
    )
