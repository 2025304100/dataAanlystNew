import json

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.backtest import BacktestRun, BacktestRuleTemplate, BacktestTrade
from app.models.symbol import Symbol
from app.schemas.backtest import (
    BacktestRunDetail,
    BacktestRunRead,
    BacktestRunRequest,
    BacktestTradeRead,
    RuleTemplateCreate,
    RuleTemplateResponse,
    RuleTemplateUpdate,
)
from app.services.backtest import (
    assess_backtest_score_coverage,
    build_backtest_detail_context,
    run_backtest,
)


router = APIRouter()


@router.post("/backtest/run", response_model=BacktestRunRead)
def create_backtest_run(payload: BacktestRunRequest, db: Session = Depends(get_db)):
    try:
        coverage = assess_backtest_score_coverage(
            db=db,
            symbol_ids=payload.symbol_ids,
            start_date=payload.start_date,
            end_date=payload.end_date,
        )
        if coverage["issues"]:
            symbol_rows = db.execute(select(Symbol).where(Symbol.id.in_(payload.symbol_ids))).scalars().all()
            symbol_map = {
                row.id: {
                    "symbol": row.symbol,
                    "name": row.name,
                }
                for row in symbol_rows
            }
            for issue in coverage["issues"]:
                issue.update(symbol_map.get(issue["symbol_id"], {}))

            min_coverage = min(issue.get("coverage_pct", 0) for issue in coverage["issues"]) if coverage["issues"] else 0
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "BACKTEST_SCORE_COVERAGE_INSUFFICIENT",
                    "message": "Historical score coverage is insufficient for this backtest range. Initialize history data in Settings before running the backtest.",
                    "summary": {
                        **coverage,
                        "min_coverage_pct": min_coverage,
                    },
                    "issues": coverage["issues"],
                },
            )

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
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/backtest/runs", response_model=list[BacktestRunRead])
def list_backtest_runs(
    portfolio_id: int = Query(...),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    runs = db.execute(
        select(BacktestRun)
        .where(BacktestRun.portfolio_id == portfolio_id)
        .order_by(desc(BacktestRun.created_at))
        .limit(limit)
    ).scalars().all()
    return runs


@router.get("/backtest/runs/{run_id}", response_model=BacktestRunDetail)
def get_backtest_run(run_id: int, db: Session = Depends(get_db)):
    run = db.get(BacktestRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Backtest run not found")

    trades = db.execute(
        select(BacktestTrade)
        .where(BacktestTrade.run_id == run_id)
        .order_by(BacktestTrade.entry_date)
    ).scalars().all()

    extra = build_backtest_detail_context(db, run, trades)
    trade_annotations = extra.pop("trade_annotations", {})
    trade_rows = []
    for trade in trades:
        row = BacktestTradeRead.model_validate(trade).model_dump()
        row.update(trade_annotations.get(trade.id, {}))
        trade_rows.append(row)
    return BacktestRunDetail(**run.__dict__, trades=trade_rows, **extra)


@router.delete("/backtest/runs/{run_id}")
def delete_backtest_run(run_id: int, db: Session = Depends(get_db)):
    run = db.get(BacktestRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Backtest run not found")

    db.delete(run)
    db.commit()
    return {"success": True}



def _format_template(template: BacktestRuleTemplate) -> dict:
    try:
        rule_config = json.loads(template.rule_config) if isinstance(template.rule_config, str) else template.rule_config
    except (json.JSONDecodeError, TypeError):
        rule_config = {}
    return {
        "id": template.id,
        "name": template.name,
        "description": template.description or "",
        "rule_config": rule_config,
        "created_at": template.created_at,
        "updated_at": template.updated_at,
    }


@router.get("/backtest/templates", response_model=list[RuleTemplateResponse])
def list_rule_templates(db: Session = Depends(get_db)):
    templates = db.execute(
        select(BacktestRuleTemplate).order_by(desc(BacktestRuleTemplate.updated_at))
    ).scalars().all()
    return [_format_template(template) for template in templates]


@router.post("/backtest/templates", response_model=RuleTemplateResponse, status_code=201)
def create_rule_template(payload: RuleTemplateCreate, db: Session = Depends(get_db)):
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
    template = db.get(BacktestRuleTemplate, template_id)
    if template is None:
        raise HTTPException(status_code=404, detail="Template not found")
    db.delete(template)
    db.commit()
    return {"success": True}