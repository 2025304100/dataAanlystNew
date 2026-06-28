import json

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.backtest import BacktestRun, BacktestRuleTemplate, BacktestTrade
from app.schemas.backtest import (
    BacktestRunDetail, BacktestRunRead, BacktestRunRequest,
    RuleTemplateCreate, RuleTemplateUpdate, RuleTemplateResponse,
)
from app.services.backtest import build_backtest_detail_context, run_backtest


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
    
    extra = build_backtest_detail_context(db, run)
    return BacktestRunDetail(**run.__dict__, trades=trades, **extra)


@router.delete("/backtest/runs/{run_id}")
def delete_backtest_run(run_id: int, db: Session = Depends(get_db)):
    """删除回测记录"""
    run = db.get(BacktestRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Backtest run not found")
    
    db.delete(run)
    db.commit()
    return {"success": True}


# ---------- 规则模板 CRUD ----------

def _format_template(t: BacktestRuleTemplate) -> dict:
    try:
        rule_config = json.loads(t.rule_config) if isinstance(t.rule_config, str) else t.rule_config
    except (json.JSONDecodeError, TypeError):
        rule_config = {}
    return {
        "id": t.id,
        "name": t.name,
        "description": t.description or "",
        "rule_config": rule_config,
        "created_at": t.created_at,
        "updated_at": t.updated_at,
    }


@router.get("/backtest/templates", response_model=list[RuleTemplateResponse])
def list_rule_templates(db: Session = Depends(get_db)):
    """列出所有回测规则模板"""
    templates = db.execute(
        select(BacktestRuleTemplate).order_by(desc(BacktestRuleTemplate.updated_at))
    ).scalars().all()
    return [_format_template(t) for t in templates]


@router.post("/backtest/templates", response_model=RuleTemplateResponse, status_code=201)
def create_rule_template(payload: RuleTemplateCreate, db: Session = Depends(get_db)):
    """创建回测规则模板"""
    existing = db.execute(
        select(BacktestRuleTemplate).where(BacktestRuleTemplate.name == payload.name)
    ).scalars().first()
    if existing:
        raise HTTPException(status_code=409, detail="Template name already exists")
    template = BacktestRuleTemplate(
        name=payload.name,
        description=payload.description,
        rule_config=json.dumps(payload.rule_config, ensure_ascii=False),
    )
    db.add(template)
    db.commit()
    db.refresh(template)
    return _format_template(template)


@router.put("/backtest/templates/{template_id}", response_model=RuleTemplateResponse)
def update_rule_template(template_id: int, payload: RuleTemplateUpdate, db: Session = Depends(get_db)):
    """更新回测规则模板"""
    template = db.get(BacktestRuleTemplate, template_id)
    if template is None:
        raise HTTPException(status_code=404, detail="Template not found")
    if payload.name is not None:
        template.name = payload.name
    if payload.description is not None:
        template.description = payload.description
    if payload.rule_config is not None:
        template.rule_config = json.dumps(payload.rule_config, ensure_ascii=False)
    db.commit()
    db.refresh(template)
    return _format_template(template)


@router.delete("/backtest/templates/{template_id}")
def delete_rule_template(template_id: int, db: Session = Depends(get_db)):
    """删除回测规则模板"""
    template = db.get(BacktestRuleTemplate, template_id)
    if template is None:
        raise HTTPException(status_code=404, detail="Template not found")
    db.delete(template)
    db.commit()
    return {"success": True}