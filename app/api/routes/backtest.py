from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.backtest import BacktestRun, BacktestTrade
from app.schemas.backtest import BacktestRunDetail, BacktestRunRead, BacktestRunRequest
from app.services.backtest import run_backtest


router = APIRouter()


@router.post("/backtest/run", response_model=BacktestRunRead)
def create_backtest_run(payload: BacktestRunRequest, db: Session = Depends(get_db)):
    """创建并运行回测任务"""
    try:
        result = run_backtest(
            db=db,
            portfolio_id=payload.portfolio_id,
            symbol_ids=payload.symbol_ids,
            start_date=payload.start_date,
            end_date=payload.end_date,
            rule_config=payload.rule_config.model_dump(),
            cost_config=payload.cost_config.model_dump() if payload.cost_config else None,
            run_name=payload.run_name,
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/backtest/runs", response_model=list[BacktestRunRead])
def list_backtest_runs(
    portfolio_id: int = Query(...),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """列出回测历史记录"""
    runs = db.execute(
        select(BacktestRun)
        .where(BacktestRun.portfolio_id == portfolio_id)
        .order_by(desc(BacktestRun.created_at))
        .limit(limit)
    ).scalars().all()
    return runs


@router.get("/backtest/runs/{run_id}", response_model=BacktestRunDetail)
def get_backtest_run(run_id: int, db: Session = Depends(get_db)):
    """获取回测详情（含交易记录）"""
    run = db.get(BacktestRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Backtest run not found")
    
    trades = db.execute(
        select(BacktestTrade)
        .where(BacktestTrade.run_id == run_id)
        .order_by(BacktestTrade.entry_date)
    ).scalars().all()
    
    return BacktestRunDetail(**run.__dict__, trades=trades)


@router.delete("/backtest/runs/{run_id}")
def delete_backtest_run(run_id: int, db: Session = Depends(get_db)):
    """删除回测记录"""
    run = db.get(BacktestRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Backtest run not found")
    
    db.delete(run)
    db.commit()
    return {"success": True}