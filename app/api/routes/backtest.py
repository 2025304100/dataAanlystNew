import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db
from app.models.backtest import BacktestRun, BacktestRuleTemplate, BacktestTrade
from app.models.portfolio import Portfolio
from app.models.symbol import Symbol
from app.schemas.backtest import (
    BacktestApplyRequest,
    BacktestApplyResult,
    BacktestRunDetail,
    BacktestRunRead,
    BacktestRunRequest,
    BacktestTradeRead,
    PortfolioBacktestCompareRequest,
    PortfolioBacktestCompareResult,
    PortfolioBacktestRequest,
    PortfolioBacktestResult,
    PortfolioBacktestSourceStatus,
    RuleTemplateCreate,
    RuleTemplateResponse,
    RuleTemplateUpdate,
)
from app.services.backtest import (
    assess_backtest_score_coverage,
    build_backtest_detail_context,
    run_backtest,
)
from app.services.backtest_apply import apply_backtest_run_to_portfolio
from app.services.factors.runtime import get_factor_runtime_snapshot
from app.services.portfolio_backtest import compare_new_old_engine, run_portfolio_backtest
from app.services.portfolio_asset_scope import ensure_symbol_ids_in_scope


router = APIRouter()


@router.post("/backtest/run", response_model=BacktestRunRead)
def create_backtest_run(payload: BacktestRunRequest, db: Session = Depends(get_db)):
    try:
        portfolio = db.get(Portfolio, payload.portfolio_id)
        if portfolio is None:
            raise HTTPException(status_code=404, detail=f"Portfolio {payload.portfolio_id} not found")
        try:
            ensure_symbol_ids_in_scope(db, portfolio, payload.symbol_ids)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        runtime = get_factor_runtime_snapshot(db)
        score_weight_mode = (
            payload.score_weight_mode or runtime.score_weight_mode
        )
        factor_model_run_id = (
            payload.factor_model_run_id or runtime.active_model_run_id
            if score_weight_mode == 'ridge'
            else None
        )
        coverage = assess_backtest_score_coverage(
            db=db,
            symbol_ids=payload.symbol_ids,
            start_date=payload.start_date,
            end_date=payload.end_date,
            score_weight_mode=score_weight_mode,
            factor_model_run_id=factor_model_run_id,
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
            score_weight_mode=score_weight_mode,
            factor_model_run_id=factor_model_run_id,
        )
        return result
    except HTTPException:
        raise
    except ValueError as exc:
        # 将可由用户修复的数据问题转换为明确的 4xx，避免被全局异常处理器
        # 包装成“服务暂时不可用”。前端可据 error_code 提供直达修复入口。
        if str(exc) == "No trading data found in date range":
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "BACKTEST_MARKET_DATA_MISSING",
                    "message": "该标的在回测区间内没有行情数据，请先初始化历史行情。",
                },
            ) from exc
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logging.getLogger(__name__).exception("回测执行失败")
        raise HTTPException(status_code=500, detail="回测执行失败，请检查配置或稍后重试") from exc


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


@router.post("/backtest/runs/{run_id}/apply-to-portfolio", response_model=BacktestApplyResult)
def apply_backtest_to_portfolio(
    run_id: int,
    payload: BacktestApplyRequest,
    db: Session = Depends(get_db),
):
    """P2-1：将回测结果应用到模拟组合。

    将回测的每笔交易重放为 SimOrder/SimTrade，重建组合的持仓、现金、交易历史。
    时间戳使用回测的 entry_date/exit_date，不污染最近交易统计。

    clear_existing=False 时若目标组合已有 sim orders，返回 409。
    """
    try:
        result = apply_backtest_run_to_portfolio(
            db=db,
            run_id=run_id,
            portfolio_id=payload.portfolio_id,
            clear_existing=payload.clear_existing,
        )
        return BacktestApplyResult(**result)
    except ValueError as exc:
        msg = str(exc)
        if "not found" in msg or "not simulated" in msg:
            raise HTTPException(status_code=404, detail=msg)
        if "already has sim orders" in msg:
            raise HTTPException(status_code=409, detail=msg)
        if "status is" in msg:
            raise HTTPException(status_code=409, detail=msg)
        raise HTTPException(status_code=400, detail=msg)
    except Exception as exc:
        logging.getLogger(__name__).exception("应用回测到组合失败")
        raise HTTPException(status_code=500, detail="应用回测到组合失败，请稍后重试") from exc



@router.post("/backtest/portfolio/run", response_model=PortfolioBacktestResult)
def create_portfolio_backtest_run(payload: PortfolioBacktestRequest, db: Session = Depends(get_db)):
    """P2-2：组合整体回测。

    自动从组合持仓 + 最新 scan executable 候选推导 symbol_ids，
    自动构造与 auto_trade 信号逻辑一致的 rule_config（基于 Score.action），
    复用 portfolio 的 active rule 限制仓位与持仓数。

    前提：portfolio 必须是 simulated 账户且 auto_trade_enabled=1。
    否则回测的信号源（Score.action）与实际执行逻辑不一致，结果无意义。

    WP7.3：接受 only_auto 参数，仅在 member 来源生效时跳过 manual/confirm 成员。
    """
    try:
        result = run_portfolio_backtest(
            db=db,
            portfolio_id=payload.portfolio_id,
            start_date=payload.start_date,
            end_date=payload.end_date,
            run_name=payload.run_name,
            only_auto=payload.only_auto,
            current_universe=payload.current_universe,
            benchmark=payload.benchmark,
        )
        return PortfolioBacktestResult(**result)
    except ValueError as exc:
        msg = str(exc)
        if "not found" in msg:
            raise HTTPException(status_code=404, detail=msg)
        if "auto_trade_enabled is 0" in msg:
            raise HTTPException(status_code=409, detail=msg)
        if "is not simulated" in msg:
            raise HTTPException(status_code=409, detail=msg)
        if "total_capital is" in msg or "Cannot run whole-portfolio backtest" in msg:
            raise HTTPException(status_code=400, detail=msg)
        if "manual/confirm 成员" in msg:
            raise HTTPException(status_code=409, detail=msg)
        raise HTTPException(status_code=400, detail=msg)
    except Exception as exc:
        logging.getLogger(__name__).exception("组合整体回测执行失败")
        raise HTTPException(status_code=500, detail="组合整体回测执行失败，请稍后重试") from exc


# ----------------------------------------------------------------------------
# WP7.4：组合回测来源状态 + 新旧引擎对比
# ----------------------------------------------------------------------------

_PORTFOLIO_BACKTEST_SOURCE_ENV_FLAG = "PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED"


def _get_portfolio_or_404(db: Session, portfolio_id: int) -> Portfolio:
    portfolio = db.get(Portfolio, portfolio_id)
    if portfolio is None:
        raise HTTPException(status_code=404, detail=f"Portfolio {portfolio_id} not found")
    return portfolio


@router.get(
    "/portfolios/{portfolio_id}/backtest/source-status",
    response_model=PortfolioBacktestSourceStatus,
    tags=["backtest"],
)
def get_portfolio_backtest_source_status(
    portfolio_id: int,
    db: Session = Depends(get_db),
):
    """WP7.4 组合回测标的来源开关状态。

    返回 {enabled, env_flag, source_label}：
    - enabled：PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED 当前是否开启
    - env_flag：环境变量名（便于 UI 展示）
    - source_label：当前生效的来源标签 "legacy" / "members"
    """
    _get_portfolio_or_404(db, portfolio_id)
    enabled = bool(settings.PORTFOLIO_BACKTEST_MEMBER_SOURCE_ENABLED)
    return PortfolioBacktestSourceStatus(
        enabled=enabled,
        env_flag=_PORTFOLIO_BACKTEST_SOURCE_ENV_FLAG,
        source_label="members" if enabled else "legacy",
    )


@router.post(
    "/portfolios/{portfolio_id}/backtest/compare",
    response_model=PortfolioBacktestCompareResult,
    tags=["backtest"],
)
def compare_portfolio_backtest_engines(
    portfolio_id: int,
    payload: PortfolioBacktestCompareRequest,
    db: Session = Depends(get_db),
):
    """WP7.4 新旧引擎对比。

    使用相同日期/资金/成本对比新旧来源回测结果：
    - 旧来源（legacy）：持仓 + 最新 scan 候选池
    - 新来源（members）：按有效日期读取历史成员

    返回 {old, new, diff}，详见 PortfolioBacktestCompareResult。
    """
    _get_portfolio_or_404(db, portfolio_id)
    if payload.portfolio_id != portfolio_id:
        raise HTTPException(
            status_code=400,
            detail="payload.portfolio_id must match path portfolio_id",
        )
    try:
        result = compare_new_old_engine(
            db=db,
            portfolio_id=portfolio_id,
            start_date=payload.start_date,
            end_date=payload.end_date,
            initial_capital=payload.initial_capital,
            run_name_prefix=payload.run_name_prefix,
        )
        return PortfolioBacktestCompareResult(**result)
    except ValueError as exc:
        msg = str(exc)
        if "not found" in msg:
            raise HTTPException(status_code=404, detail=msg)
        if "auto_trade_enabled is 0" in msg or "is not simulated" in msg:
            raise HTTPException(status_code=409, detail=msg)
        if "total_capital is" in msg or "Cannot run whole-portfolio backtest" in msg:
            raise HTTPException(status_code=400, detail=msg)
        if "manual/confirm 成员" in msg:
            raise HTTPException(status_code=409, detail=msg)
        raise HTTPException(status_code=400, detail=msg)
    except Exception as exc:
        logging.getLogger(__name__).exception("组合回测新旧引擎对比失败")
        raise HTTPException(
            status_code=500,
            detail="组合回测新旧引擎对比失败，请稍后重试",
        ) from exc



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
