import json
from datetime import date as date_type
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.portfolio import Portfolio, PortfolioRule, Position
from app.models.review import Review
from app.models.sim_account import CashLedger, SimOrder, SimTrade
from app.models.symbol import Symbol
from app.models.portfolio_candidate import PortfolioCandidate
from app.models.discovery_candidate import DiscoveryCandidate
from app.schemas.attribution import AttributionReport, ReviewCreate, ReviewRead
from app.schemas.portfolio import (
    AllocationSummary,
    AutoTradeExecuteRequest,
    AutoTradeResult,
    PortfolioCreate,
    PortfolioRead,
    PortfolioRuleUpsert,
    PortfolioUpdate,
    PositionRead,
    PositionUpsert,
)
from app.schemas.portfolio_member import (
    BackfillRequest,
    BackfillResult,
    MemberHasPositionErrorSchema,
    PortfolioMemberArchiveRequest,
    PortfolioMemberCreate,
    PortfolioMemberRead,
    PortfolioMemberUpdate,
)
from app.schemas.portfolio_candidate import PortfolioCandidateCreate, PortfolioCandidateRead
from app.services.allocation import compute_allocation, get_active_rule
from app.services.auto_trade_dual_run import (
    AutoTradeNotReadyError,
    run_dual_trade,
)
from app.services.portfolio_equity_snapshot import (
    list_portfolio_equity_snapshots,
    snapshot_to_dict,
)
from app.services.portfolio_member_backfill import backfill_positions_to_members
from app.services.portfolio_members import (
    MemberHasPositionError,
    archive_member,
    create_member,
    list_members,
    pause_member,
    restore_member,
    update_member,
)
from app.services.portfolio_performance import compute_portfolio_performance
from app.services.attribution import get_attribution_report
from app.services.sim_accounts import ensure_sim_account_seed
from app.services.investment_themes import get_active_theme_opportunities
from app.services.portfolio_asset_scope import ensure_symbol_in_scope


router = APIRouter()


def _json_object(value: str | None) -> dict:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return [value]
    return [str(item) for item in parsed] if isinstance(parsed, list) else [str(parsed)]


def _candidate_read(candidate: PortfolioCandidate, symbol: Symbol) -> PortfolioCandidateRead:
    return PortfolioCandidateRead(
        id=candidate.id,
        portfolio_id=candidate.portfolio_id,
        symbol_id=candidate.symbol_id,
        source_candidate_id=candidate.source_candidate_id,
        source_type=candidate.source_type,
        source_scan_run_id=candidate.source_scan_run_id,
        pool_memberships=_json_list(candidate.pool_memberships_json),
        priority_score=candidate.priority_score,
        recommended_position_pct=candidate.recommended_position_pct,
        factor_tag=candidate.factor_tag,
        symbol=symbol.symbol,
        name=symbol.name,
        created_at=candidate.created_at.isoformat() if candidate.created_at else None,
        admission_snapshot=_json_object(candidate.admission_snapshot_json),
    )


@router.get("/portfolios/{portfolio_id}/candidates", response_model=list[PortfolioCandidateRead])
def list_portfolio_candidates(portfolio_id: int, db: Session = Depends(get_db)) -> list[PortfolioCandidateRead]:
    rows = db.execute(
        select(PortfolioCandidate, Symbol)
        .join(Symbol, Symbol.id == PortfolioCandidate.symbol_id)
        .where(PortfolioCandidate.portfolio_id == portfolio_id)
        .order_by(PortfolioCandidate.created_at.desc())
    ).all()
    return [_candidate_read(candidate, symbol) for candidate, symbol in rows]


@router.post("/portfolios/{portfolio_id}/candidates", response_model=PortfolioCandidateRead, status_code=201)
def add_portfolio_candidate(
    portfolio_id: int, payload: PortfolioCandidateCreate, db: Session = Depends(get_db)
) -> PortfolioCandidateRead:
    portfolio = db.get(Portfolio, portfolio_id)
    symbol = db.get(Symbol, payload.symbol_id)
    if portfolio is None or symbol is None:
        raise HTTPException(status_code=404, detail="Portfolio or symbol not found")
    try:
        ensure_symbol_in_scope(portfolio, symbol)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    existing = db.execute(
        select(PortfolioCandidate).where(
            PortfolioCandidate.portfolio_id == portfolio_id, PortfolioCandidate.symbol_id == payload.symbol_id
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="Candidate already exists in this portfolio")

    source = None
    if payload.source_candidate_id is not None:
        source = db.get(DiscoveryCandidate, payload.source_candidate_id)
        if source is None or source.symbol != symbol.symbol:
            raise HTTPException(status_code=422, detail="source_candidate_id does not match symbol_id")

    memberships = [pool for pool in payload.pool_memberships if pool in {"factor", "technical", "theme"}]
    source_type = payload.source_type if payload.source_type in {"factor", "technical", "theme", "manual"} else None
    if source is not None:
        # Server owns source identity and scan version; the browser cannot point
        # a portfolio record at a different discovery candidate or scan run.
        source_type = source_type or (memberships[0] if memberships else "manual")
        theme_opportunity = get_active_theme_opportunities(db, [symbol.id]).get(symbol.id)
        if source_type == "theme" and theme_opportunity is None:
            raise HTTPException(status_code=422, detail="theme source is no longer backed by an active catalyst")
        admission_snapshot = {
            "source": "discovery_candidate",
            "candidate_id": source.id,
            "scan_run_id": source.scan_run_id,
            "symbol": source.symbol,
            "theme": theme_opportunity,
            "pool_memberships": memberships,
            "scores": {
                "quality": source.quality_score,
                "timing": source.timing_score,
                "priority": source.priority_score,
                "dimensions": _json_object(source.dimension_scores_json),
            },
            "technical": {"stage": source.stage, "action": source.action},
            "reason_tags": _json_list(source.reason_tags),
            "scoring_config": _json_object(source.scoring_config_snapshot_json),
        }
    else:
        source_type = source_type or "manual"
        admission_snapshot = {"source": "manual", "symbol": symbol.symbol, "pool_memberships": memberships}

    candidate = PortfolioCandidate(
        portfolio_id=portfolio_id,
        symbol_id=payload.symbol_id,
        source_candidate_id=source.id if source else None,
        source_type=source_type,
        source_scan_run_id=source.scan_run_id if source else payload.source_scan_run_id,
        pool_memberships_json=json.dumps(memberships, ensure_ascii=False),
        admission_snapshot_json=json.dumps(admission_snapshot, ensure_ascii=False),
        priority_score=payload.priority_score if payload.priority_score is not None else (source.priority_score if source else None),
        recommended_position_pct=payload.recommended_position_pct,
        factor_tag=payload.factor_tag,
    )
    db.add(candidate)
    db.commit()
    db.refresh(candidate)
    return _candidate_read(candidate, symbol)


@router.get("/portfolios", response_model=list[PortfolioRead])
def list_portfolios(
    include_test: bool = False,
    auto_tag_tests: bool = True,
    db: Session = Depends(get_db),
):
    """列出组合。

    - 默认 include_test=False：过滤测试组合，仅展示生产级组合。
    - include_test=true：同时返回 is_test=1 的测试组合（用于验收/研发）。
    - auto_tag_tests=true：首次访问时按"已知关键字"智能将遗留未标记记录标记为 is_test=1，
      避免数据库升级后遗留旧记录无法被过滤掉。auto_tag_tests=false 可跳过（纯读场景）。
    """
    rows = db.execute(select(Portfolio).order_by(Portfolio.id.desc())).scalars().all()

    if auto_tag_tests:
        # 已知测试组合关键字：匹配名称大小写不敏感。
        # 例：API_TEST_PORTFOLIO、ZERO_CAPITAL_TEST、浏览器验证组合、验证测试、验收残留等。
        TEST_NAME_KEYWORDS = (
            "api_test_portfolio",
            "zero_capital_test",
            "test_portfolio",
            "demo_portfolio",
            "browser_test",
            "browser_verify",
            "manual_test",
            "smoke_test",
            "unit_test",
            "integration_test",
            "dev_test",
            "staging_test",
            "e2e_test",
            "perf_test",
            "测试",
            "验收",
            "验证",
            "演示",
            "mock",
            "dummy",
        )
        changed = False
        for p in rows:
            if int(getattr(p, "is_test", 0) or 0) == 1:
                continue
            name_lower = (p.name or "").lower()
            if any(k in name_lower for k in TEST_NAME_KEYWORDS):
                try:
                    p.is_test = 1
                    changed = True
                except Exception:
                    # 某些旧 SQLite 可能字段尚未补齐，直接忽略，交给兜底名称匹配
                    pass
        if changed:
            try:
                db.commit()
            except Exception:
                db.rollback()

    def _is_test_row(p: Portfolio) -> bool:
        if int(getattr(p, "is_test", 0) or 0) == 1:
            return True
        # 兜底：即使 DB 没补齐 is_test 列，也按关键字隐藏（向前兼容无 migration 的环境）
        try:
            # 兜底关键字集（与上方 auto_tag 同步，避免用户刷新两遍才生效）
            FALLBACK_KEYWORDS = (
                "api_test_portfolio", "zero_capital_test", "test_portfolio", "demo_portfolio",
                "browser_test", "browser_verify", "manual_test", "smoke_test", "unit_test",
                "integration_test", "dev_test", "staging_test", "e2e_test", "perf_test",
                "测试", "验收", "验证", "演示", "mock", "dummy",
            )
            n = (p.name or "").lower()
            return any(k in n for k in FALLBACK_KEYWORDS)
        except Exception:
            return False

    if include_test:
        return rows
    return [p for p in rows if not _is_test_row(p)]


@router.post("/portfolios", response_model=PortfolioRead)
def create_portfolio(payload: PortfolioCreate, db: Session = Depends(get_db)):
    # 同名查重（与 watchlists 保持一致的 409 语义）
    existing = db.execute(select(Portfolio).where(Portfolio.name == payload.name)).scalars().first()
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"Portfolio with name '{payload.name}' already exists")

    # 如果设为默认，先取消其他默认
    if payload.is_default:
        _clear_other_defaults(db, exclude_id=None)

    portfolio = Portfolio(
        name=payload.name,
        account_type=payload.account_type,
        asset_scope=payload.asset_scope,
        total_capital=payload.total_capital,
        investable_ratio=payload.investable_ratio,
        cash_reserve_ratio=payload.cash_reserve_ratio,
        currency=payload.currency,
        is_default=int(payload.is_default),
        auto_trade_enabled=int(payload.auto_trade_enabled),
        # P1-FIX: 新建组合时接收表单提交的佣金/单票上限/基准指数（之前只保存在前端本地状态未提交）
        buy_fee_pct=payload.buy_fee_pct,
        sell_fee_pct=payload.sell_fee_pct,
        benchmark_code=payload.benchmark_code,
        default_single_position_pct=payload.default_single_position_pct,
    )
    db.add(portfolio)
    db.commit()
    db.refresh(portfolio)
    if portfolio.account_type == "simulated":
        ensure_sim_account_seed(db, portfolio)
        db.commit()
    return portfolio


@router.put("/portfolios/{portfolio_id}", response_model=PortfolioRead)
def update_portfolio(portfolio_id: int, payload: PortfolioUpdate, db: Session = Depends(get_db)):
    """更新组合属性（名称/资金/比例/默认标记）。

    不允许修改 account_type（避免模拟账户与普通账户切换导致数据不一致）。
    若设为默认，会自动取消其他组合的默认标记。
    """
    portfolio = db.get(Portfolio, portfolio_id)
    if portfolio is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")

    # 名称查重（排除自身）
    if payload.name is not None and payload.name != portfolio.name:
        existing = db.execute(
            select(Portfolio).where(Portfolio.name == payload.name, Portfolio.id != portfolio_id)
        ).scalars().first()
        if existing is not None:
            raise HTTPException(status_code=409, detail=f"Portfolio with name '{payload.name}' already exists")
        portfolio.name = payload.name

    if payload.total_capital is not None:
        portfolio.total_capital = payload.total_capital
    if payload.asset_scope is not None:
        # Never let an edit turn an already mixed/non-empty portfolio into an
        # invalid single-asset portfolio. The user must first remove/archive
        # incompatible candidates, members and positions.
        scope_symbol_ids = set(
            db.execute(select(Position.symbol_id).where(Position.portfolio_id == portfolio_id)).scalars().all()
        )
        scope_symbol_ids.update(
            db.execute(select(PortfolioCandidate.symbol_id).where(PortfolioCandidate.portfolio_id == portfolio_id)).scalars().all()
        )
        from app.models.portfolio_member import PortfolioMember
        scope_symbol_ids.update(
            db.execute(select(PortfolioMember.symbol_id).where(PortfolioMember.portfolio_id == portfolio_id)).scalars().all()
        )
        try:
            from app.services.portfolio_asset_scope import ensure_symbol_ids_in_scope
            scope_probe = Portfolio(asset_scope=payload.asset_scope)
            ensure_symbol_ids_in_scope(db, scope_probe, list(scope_symbol_ids))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"无法修改组合资产范围：{exc}") from exc
        portfolio.asset_scope = payload.asset_scope
    if payload.investable_ratio is not None:
        portfolio.investable_ratio = payload.investable_ratio
    if payload.cash_reserve_ratio is not None:
        portfolio.cash_reserve_ratio = payload.cash_reserve_ratio
    if payload.currency is not None:
        portfolio.currency = payload.currency

    if payload.is_default is not None:
        if payload.is_default:
            _clear_other_defaults(db, exclude_id=portfolio_id)
        portfolio.is_default = int(payload.is_default)

    # P2-3：自动交易开关
    if payload.auto_trade_enabled is not None:
        portfolio.auto_trade_enabled = int(payload.auto_trade_enabled)

    # P1-FIX: 新增补充字段增量更新（佣金/基准/单票上限）
    if payload.buy_fee_pct is not None:
        portfolio.buy_fee_pct = payload.buy_fee_pct
    if payload.sell_fee_pct is not None:
        portfolio.sell_fee_pct = payload.sell_fee_pct
    if payload.benchmark_code is not None:
        portfolio.benchmark_code = payload.benchmark_code
    if payload.default_single_position_pct is not None:
        portfolio.default_single_position_pct = payload.default_single_position_pct

    db.commit()
    db.refresh(portfolio)
    return portfolio


@router.delete("/portfolios/{portfolio_id}")
def delete_portfolio(portfolio_id: int, db: Session = Depends(get_db)):
    """删除组合及其所有关联数据。

    级联清理（依赖 DB 外键 ondelete=CASCADE）：
    - PortfolioRule / Position（ORM cascade）
    - CashLedger / SimOrder / SimTrade（FK ondelete=CASCADE）

    安全检查：
    - 默认组合不允许删除（需先取消默认或切换默认到其他组合）
    - 至少保留一个组合（避免删空导致前端无组合可选）
    """
    portfolio = db.get(Portfolio, portfolio_id)
    if portfolio is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")

    if portfolio.is_default:
        raise HTTPException(
            status_code=400,
            detail="Cannot delete the default portfolio. Please set another portfolio as default first.",
        )

    # 检查是否是最后一个组合
    total_count = db.execute(select(Portfolio)).scalars().all()
    if len(total_count) <= 1:
        raise HTTPException(status_code=400, detail="Cannot delete the last portfolio. At least one portfolio must remain.")

    db.delete(portfolio)
    db.commit()
    return {"deleted": True, "id": portfolio_id}


def _clear_other_defaults(db: Session, exclude_id: int | None) -> None:
    """取消其他组合的默认标记（内部辅助函数）。"""
    stmt = select(Portfolio).where(Portfolio.is_default == 1)
    if exclude_id is not None:
        stmt = stmt.where(Portfolio.id != exclude_id)
    others = db.execute(stmt).scalars().all()
    for p in others:
        p.is_default = 0


@router.get("/portfolios/{portfolio_id}")
def get_portfolio(portfolio_id: int, db: Session = Depends(get_db)):
    portfolio = db.get(Portfolio, portfolio_id)
    if portfolio is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    rule = get_active_rule(db, portfolio_id)
    allocation = compute_allocation(db, portfolio_id)
    return {
        "portfolio": PortfolioRead.model_validate(portfolio),
        "active_rule": None
        if rule is None
        else {
            "id": rule.id,
            "rule_name": rule.rule_name,
            "max_single_position_pct": rule.max_single_position_pct,
            "max_sector_position_pct": rule.max_sector_position_pct,
            "max_stock_position_pct": rule.max_stock_position_pct,
            "max_etf_position_pct": rule.max_etf_position_pct,
            "max_loss_per_trade_pct": rule.max_loss_per_trade_pct,
            "max_open_positions": rule.max_open_positions,
            "stage_limits_json": json.loads(rule.stage_limits_json),
        },
        "allocation": allocation,
    }


@router.get("/portfolios/{portfolio_id}/equity-snapshots")
def list_portfolio_equity_snapshots_route(
    portfolio_id: int,
    start_date: date_type | None = Query(None, description="起始日期（含），格式 YYYY-MM-DD"),
    end_date: date_type | None = Query(None, description="结束日期（含），格式 YYYY-MM-DD"),
    limit: int = Query(400, ge=1, le=2000, description="最多返回条数，按日期升序"),
    db: Session = Depends(get_db),
):
    """P0-10：返回组合净值快照时序数据（绩效统计基础数据）。

    用途：
    - 前端绘制组合净值曲线
    - 计算最大回撤、Sharpe、日收益率序列
    - 多组合横向对比（归一化净值）

    历史数据无法回溯，仅有系统上线后写入的数据。
    """
    portfolio = db.get(Portfolio, portfolio_id)
    if portfolio is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    if portfolio.account_type != "simulated":
        raise HTTPException(
            status_code=400,
            detail="Equity snapshots are only available for simulated portfolios",
        )
    snaps = list_portfolio_equity_snapshots(
        db,
        portfolio_id,
        start_date=start_date,
        end_date=end_date,
        limit=limit,
    )
    return [snapshot_to_dict(s) for s in snaps]


@router.get("/portfolios/{portfolio_id}/performance")
def get_portfolio_performance_route(
    portfolio_id: int,
    start_date: date_type | None = Query(None, description="起始日期（含），格式 YYYY-MM-DD"),
    end_date: date_type | None = Query(None, description="结束日期（含），格式 YYYY-MM-DD"),
    snapshot_limit: int = Query(1000, ge=1, le=5000, description="snapshot 查询上限，防止超大组合爆内存"),
    benchmark: str | None = Query("000300", description="基准指数代码（如 000300 沪深300）；空串表示不返回 benchmark 曲线"),
    db: Session = Depends(get_db),
):
    """P1-1：返回组合实盘绩效指标（最大回撤/夏普/胜率/盈亏比等）。

    用途：
    - 前端展示组合收益率统计卡片
    - 与单标的回测结果对比，验证策略实盘表现
    - 多组合横向对比

    数据来源：
    - equity_curve: PortfolioEquitySnapshot 时序
    - trades: SimTrade 中已平仓的卖出交易
    - benchmark_curve: IndexPrice 时序（归一化到 initial_capital 起点）

    边界处理：
    - 无 snapshot：所有指标为 0
    - 非模拟组合：返回 400
    - 无 benchmark 数据：benchmark_curve 为空，benchmark_name 仍返回
    """
    portfolio = db.get(Portfolio, portfolio_id)
    if portfolio is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    if portfolio.account_type != "simulated":
        raise HTTPException(
            status_code=400,
            detail="Performance metrics are only available for simulated portfolios",
        )
    return compute_portfolio_performance(
        db,
        portfolio_id,
        start_date=start_date,
        end_date=end_date,
        snapshot_limit=snapshot_limit,
        benchmark=benchmark,
    )


@router.post("/portfolios/{portfolio_id}/auto-trade/execute", response_model=AutoTradeResult)
def execute_auto_trade(
    portfolio_id: int,
    payload: AutoTradeExecuteRequest,
    db: Session = Depends(get_db),
):
    """P2-3：手动触发组合自动交易评估与下单（统一执行入口）。

    首次使用建议 dry_run=True 查看计划，确认无误后再 dry_run=False 实际下单。

    P0-AutoTrade 统一流程（对齐最终收口方案）：
    1) 执行锁：同组合并发执行直接 fail-closed（避免重复下单）
    2) 就绪检查：dry_run=False 必须 ready=True，不满足则 409 含 blockers
    3) 来源切换：按 Portfolio.auto_trade_source_mode + env 熔断选择执行来源
    4) 双跑 diff：迁移期仍捕获旧/新来源交易集合，便于人工验收
    5) 执行或计划：dry_run=True 保持可用，用于诊断为什么没有计划

    返回：
    - sells / buys / errors：兼容旧 AutoTradeResult 字段
    - readiness / blockers / warnings / diffs / executed_source：就绪与来源诊断
    """
    try:
        result = run_dual_trade(
            db=db,
            portfolio_id=portfolio_id,
            dry_run=payload.dry_run,
            buy_candidate_limit=payload.buy_candidate_limit,
            for_schedule=False,
            save_diff=True,
            require_readiness=True,
        )
        return AutoTradeResult(**result)
    except ValueError as exc:
        msg = str(exc)
        if "not found" in msg:
            raise HTTPException(status_code=404, detail=msg) from exc
        if "not simulated" in msg:
            raise HTTPException(status_code=400, detail=msg) from exc
        if "auto_trade_enabled" in msg:
            raise HTTPException(status_code=409, detail=msg) from exc
        raise HTTPException(status_code=400, detail=msg) from exc
    except AutoTradeNotReadyError as exc:
        blocker_payload = [
            {
                "code": getattr(b, "code", "NOT_READY"),
                "message": getattr(b, "message", str(b)),
                "detail": getattr(b, "detail", None),
            }
            for b in list(getattr(exc, "blockers", []) or [])
        ]
        raise HTTPException(
            status_code=409,
            detail={
                "error_message": str(exc),
                "blockers": blocker_payload,
            },
        ) from exc
    except RuntimeError as exc:
        msg = str(exc)
        if "already running" in msg:
            raise HTTPException(status_code=409, detail=msg) from exc
        raise HTTPException(status_code=400, detail=msg) from exc
    except Exception as exc:
        import logging
        logging.getLogger(__name__).exception("自动交易执行失败")
        raise HTTPException(status_code=500, detail="自动交易执行失败，请稍后重试") from exc


@router.post("/portfolios/{portfolio_id}/rules")
def upsert_portfolio_rule(portfolio_id: int, payload: PortfolioRuleUpsert, db: Session = Depends(get_db)):
    portfolio = db.get(Portfolio, portfolio_id)
    if portfolio is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")

    if payload.is_active:
        active_rules = db.execute(select(PortfolioRule).where(PortfolioRule.portfolio_id == portfolio_id)).scalars().all()
        for rule in active_rules:
            rule.is_active = 0

    rule = PortfolioRule(
        portfolio_id=portfolio_id,
        rule_name=payload.rule_name,
        max_single_position_pct=payload.max_single_position_pct,
        max_sector_position_pct=payload.max_sector_position_pct,
        max_stock_position_pct=payload.max_stock_position_pct,
        max_etf_position_pct=payload.max_etf_position_pct,
        max_loss_per_trade_pct=payload.max_loss_per_trade_pct,
        max_open_positions=payload.max_open_positions,
        stage_limits_json=json.dumps(payload.stage_limits_json, ensure_ascii=True),
        is_active=int(payload.is_active),
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return {"id": rule.id, "portfolio_id": portfolio_id}


@router.get("/portfolios/{portfolio_id}/allocation", response_model=AllocationSummary)
def get_allocation(portfolio_id: int, db: Session = Depends(get_db)):
    portfolio = db.get(Portfolio, portfolio_id)
    if portfolio is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    summary = compute_allocation(db, portfolio_id)
    rule = get_active_rule(db, portfolio_id)
    if rule is None:
        summary["remaining_stock_pct"] = 0.0
        summary["remaining_etf_pct"] = 0.0
    else:
        summary["remaining_stock_pct"] = round(max(0.0, rule.max_stock_position_pct - summary["stock_position_pct"]), 4)
        summary["remaining_etf_pct"] = round(max(0.0, rule.max_etf_position_pct - summary["etf_position_pct"]), 4)
    return AllocationSummary(**summary)


@router.get("/portfolios/{portfolio_id}/positions", response_model=list[PositionRead])
def list_positions(portfolio_id: int, db: Session = Depends(get_db)):
    portfolio = db.get(Portfolio, portfolio_id)
    if portfolio is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    positions = db.execute(select(Position).where(Position.portfolio_id == portfolio_id).order_by(Position.id.desc())).scalars().all()
    # P1-FIX: 联表查 Symbol，补齐 symbol/name，前端可识别显示
    symbol_ids = {p.symbol_id for p in positions}
    symbol_map: dict[int, Symbol] = {}
    if symbol_ids:
        for s in db.execute(select(Symbol).where(Symbol.id.in_(symbol_ids))).scalars().all():
            symbol_map[s.id] = s
    # 显式构造 PositionRead（ORM 对象没有 symbol/name 字段）
    result: list[PositionRead] = []
    for p in positions:
        data = {
            "id": p.id,
            "portfolio_id": p.portfolio_id,
            "symbol_id": p.symbol_id,
            "symbol": symbol_map.get(p.symbol_id).symbol if symbol_map.get(p.symbol_id) else None,
            "name": symbol_map.get(p.symbol_id).name if symbol_map.get(p.symbol_id) else None,
            "quantity": p.quantity,
            "avg_cost": p.avg_cost,
            "latest_price": p.latest_price,
            "market_value": p.market_value,
            "position_pct": p.position_pct,
            "asset_type": p.asset_type,
            "theme": p.theme,
        }
        result.append(PositionRead(**data))
    return result


@router.post("/portfolios/{portfolio_id}/positions", response_model=PositionRead)
def upsert_position(portfolio_id: int, payload: PositionUpsert, db: Session = Depends(get_db)):
    portfolio = db.get(Portfolio, portfolio_id)
    symbol = db.get(Symbol, payload.symbol_id)
    if portfolio is None or symbol is None:
        raise HTTPException(status_code=404, detail="Portfolio or symbol not found")
    try:
        ensure_symbol_in_scope(portfolio, symbol)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    position = db.execute(
        select(Position).where(Position.portfolio_id == portfolio_id, Position.symbol_id == payload.symbol_id)
    ).scalars().first()
    if position is None:
        position = Position(
            portfolio_id=portfolio_id,
            symbol_id=payload.symbol_id,
            asset_type=symbol.asset_type,
            theme=symbol.theme,
        )
        db.add(position)

    # 如果未提供 latest_price，从最新行情获取
    latest_price = payload.latest_price
    if latest_price is None:
        from app.models.daily_bar import DailyBar
        from sqlalchemy import desc
        latest_bar = db.execute(
            select(DailyBar).where(DailyBar.symbol_id == payload.symbol_id).order_by(desc(DailyBar.trade_date)).limit(1)
        ).scalars().first()
        latest_price = float(latest_bar.close) if latest_bar else payload.avg_cost

    market_value = payload.quantity * latest_price
    position_pct = market_value / portfolio.total_capital if portfolio.total_capital else 0
    position.quantity = payload.quantity
    position.avg_cost = payload.avg_cost
    position.latest_price = latest_price
    position.market_value = market_value
    position.position_pct = round(position_pct, 4)
    position.asset_type = symbol.asset_type
    position.theme = symbol.theme
    db.commit()
    db.refresh(position)
    # P1-FIX: 返回显式 PositionRead dict（含 symbol/name）
    return PositionRead(
        id=position.id,
        portfolio_id=position.portfolio_id,
        symbol_id=position.symbol_id,
        symbol=symbol.symbol,
        name=symbol.name,
        quantity=position.quantity,
        avg_cost=position.avg_cost,
        latest_price=position.latest_price,
        market_value=position.market_value,
        position_pct=position.position_pct,
        asset_type=position.asset_type,
        theme=position.theme,
    )


@router.delete("/portfolios/{portfolio_id}/positions/{symbol_id}")
def delete_position(portfolio_id: int, symbol_id: int, db: Session = Depends(get_db)):
    position = db.execute(
        select(Position).where(Position.portfolio_id == portfolio_id, Position.symbol_id == symbol_id)
    ).scalars().first()
    if position is None:
        raise HTTPException(status_code=404, detail="Position not found")
    db.delete(position)
    db.commit()
    return {"deleted": True}


# ============================================================================
# WP4.4 组合成员接口（仅追加，不修改上方已稳定端点）
# ============================================================================


def _member_to_dict(member) -> dict:
    """将 PortfolioMember ORM 对象转为 dict（日期字段统一转 ISO 字符串）。"""
    return {
        "id": member.id,
        "portfolio_id": member.portfolio_id,
        "symbol_id": member.symbol_id,
        "status": member.status,
        "execution_mode": member.execution_mode,
        "source_type": member.source_type,
        "source_id": member.source_id,
        "entry_rule_version_id": member.entry_rule_version_id,
        "exit_rule_version_id": member.exit_rule_version_id,
        "effective_from": member.effective_from.isoformat() if member.effective_from else None,
        "effective_to": member.effective_to.isoformat() if member.effective_to else None,
        "manual_lock": member.manual_lock,
        "priority": member.priority,
        "note": member.note,
        "created_at": member.created_at.isoformat() if member.created_at else None,
        "updated_at": member.updated_at.isoformat() if member.updated_at else None,
    }


def _fetch_latest_orders_by_member(
    db: Session, member_ids: list[int]
) -> dict[int, SimOrder]:
    """按 member_id 批量查询最近一条 SimOrder（WP4-FIX）。

    查询逻辑：SELECT * FROM sim_orders WHERE member_id IN (?) ORDER BY created_at DESC
    然后在 Python 中按 member_id 去重，保留 created_at 最新的一条。
    单次查询避免 N+1。
    """
    if not member_ids:
        return {}
    rows = db.execute(
        select(SimOrder)
        .where(SimOrder.member_id.in_(member_ids))
        .order_by(SimOrder.created_at.desc())
    ).scalars().all()
    latest: dict[int, SimOrder] = {}
    for order in rows:
        # rows 已按 created_at DESC 排序，首次出现的 member_id 即为最新订单
        if order.member_id is not None and order.member_id not in latest:
            latest[order.member_id] = order
    return latest


def _build_latest_signal_fields(order: SimOrder | None) -> dict:
    """从 SimOrder 构造 latest_signal 相关字段（WP4-FIX）。

    返回 dict 含 latest_signal / latest_signal_at / latest_signal_action。
    无订单时三个字段均为 None。
    """
    if order is None:
        return {
            "latest_signal": None,
            "latest_signal_at": None,
            "latest_signal_action": None,
        }
    side = order.side  # buy/sell
    side_zh = "买入" if side == "buy" else "卖出" if side == "sell" else side
    date_str = order.created_at.strftime("%Y-%m-%d") if order.created_at else ""
    latest_signal = f"{side_zh} {date_str}".strip() if date_str else side_zh
    return {
        "latest_signal": latest_signal,
        "latest_signal_at": order.created_at.isoformat() if order.created_at else None,
        "latest_signal_action": side,
    }


@router.get(
    "/portfolios/{portfolio_id}/members",
    response_model=list[PortfolioMemberRead],
    tags=["portfolio_members"],
)
def list_portfolio_members(
    portfolio_id: int,
    status: str | None = None,
    source_type: str | None = None,
    include_archived: bool = False,
    limit: int = 100,
    offset: int = 0,
    db: Session = Depends(get_db),
) -> list[PortfolioMemberRead]:
    """列出组合成员（WP4.4）。

    默认不返回归档成员（effective_to 非空），可通过 include_archived=true 包含。
    每个成员附带最近信号信息（WP4-FIX）：按 member_id 联表查询最近一条 SimOrder，
    填充 latest_signal / latest_signal_at / latest_signal_action。
    """
    members = list_members(
        db,
        portfolio_id=portfolio_id,
        status=status,
        source_type=source_type,
        include_archived=include_archived,
        limit=limit,
        offset=offset,
    )
    if not members:
        return []

    latest_orders = _fetch_latest_orders_by_member(db, [m.id for m in members])
    result: list[PortfolioMemberRead] = []
    for m in members:
        data = _member_to_dict(m)
        data.update(_build_latest_signal_fields(latest_orders.get(m.id)))
        result.append(PortfolioMemberRead(**data))
    return result


@router.post(
    "/portfolios/{portfolio_id}/members",
    response_model=PortfolioMemberRead,
    tags=["portfolio_members"],
)
def create_portfolio_member(
    portfolio_id: int,
    payload: PortfolioMemberCreate,
    db: Session = Depends(get_db),
) -> PortfolioMemberRead:
    """创建组合成员（WP4.4）。

    同组合同标的已存在有效成员时返回 409。
    """
    portfolio = db.get(Portfolio, portfolio_id)
    symbol = db.get(Symbol, payload.symbol_id)
    if portfolio is None or symbol is None:
        raise HTTPException(status_code=404, detail="Portfolio or symbol not found")
    try:
        ensure_symbol_in_scope(portfolio, symbol)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        member = create_member(
            db,
            portfolio_id=portfolio_id,
            symbol_id=payload.symbol_id,
            status=payload.status,
            execution_mode=payload.execution_mode,
            source_type=payload.source_type,
            source_id=payload.source_id,
            entry_rule_version_id=payload.entry_rule_version_id,
            exit_rule_version_id=payload.exit_rule_version_id,
            effective_from=datetime.fromisoformat(payload.effective_from) if payload.effective_from else None,
            manual_lock=payload.manual_lock,
            priority=payload.priority,
            note=payload.note,
        )
        return PortfolioMemberRead(**_member_to_dict(member))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.patch(
    "/portfolios/{portfolio_id}/members/{member_id}",
    response_model=PortfolioMemberRead,
    tags=["portfolio_members"],
)
def update_portfolio_member(
    portfolio_id: int,
    member_id: int,
    payload: PortfolioMemberUpdate,
    db: Session = Depends(get_db),
) -> PortfolioMemberRead:
    """更新组合成员字段（WP4.4）。"""
    member = update_member(
        db,
        member_id=member_id,
        status=payload.status,
        execution_mode=payload.execution_mode,
        entry_rule_version_id=payload.entry_rule_version_id,
        exit_rule_version_id=payload.exit_rule_version_id,
        manual_lock=payload.manual_lock,
        priority=payload.priority,
        note=payload.note,
    )
    if member is None:
        raise HTTPException(status_code=404, detail="Member not found")
    return PortfolioMemberRead(**_member_to_dict(member))


@router.post(
    "/portfolios/{portfolio_id}/members/{member_id}/archive",
    response_model=PortfolioMemberRead,
    tags=["portfolio_members"],
)
def archive_portfolio_member(
    portfolio_id: int,
    member_id: int,
    payload: PortfolioMemberArchiveRequest | None = None,
    db: Session = Depends(get_db),
) -> PortfolioMemberRead:
    """归档组合成员（WP4.4，默认不物理删除）。

    存在持仓时返回 409，提示选择"仅停止买入"或先卖出。
    传 {"force": true} 时即使有持仓也强制归档（用于先卖出后归档的场景）。
    """
    force = payload.force if payload else False
    try:
        member = archive_member(db, member_id=member_id, force=force)
    except MemberHasPositionError as e:
        raise HTTPException(
            status_code=409,
            detail=MemberHasPositionErrorSchema(
                portfolio_id=e.portfolio_id,
                symbol_id=e.symbol_id,
                quantity=e.quantity,
                message=str(e),
            ).model_dump(),
        )
    if member is None:
        raise HTTPException(status_code=404, detail="Member not found")
    return PortfolioMemberRead(**_member_to_dict(member))


@router.post(
    "/portfolios/{portfolio_id}/members/{member_id}/pause",
    response_model=PortfolioMemberRead,
    tags=["portfolio_members"],
)
def pause_portfolio_member(
    portfolio_id: int,
    member_id: int,
    db: Session = Depends(get_db),
) -> PortfolioMemberRead:
    """暂停组合成员（WP4.4，仅停止买入，卖出规则继续）。"""
    member = pause_member(db, member_id=member_id)
    if member is None:
        raise HTTPException(status_code=404, detail="Member not found")
    return PortfolioMemberRead(**_member_to_dict(member))


@router.post(
    "/portfolios/{portfolio_id}/members/{member_id}/restore",
    response_model=PortfolioMemberRead,
    tags=["portfolio_members"],
)
def restore_portfolio_member(
    portfolio_id: int,
    member_id: int,
    db: Session = Depends(get_db),
) -> PortfolioMemberRead:
    """恢复归档的组合成员（WP4.4）。

    若同组合同标的已有新的有效成员，返回 409。
    """
    try:
        member = restore_member(db, member_id=member_id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    if member is None:
        raise HTTPException(status_code=404, detail="Member not found")
    return PortfolioMemberRead(**_member_to_dict(member))


@router.post(
    "/portfolios/{portfolio_id}/members/backfill",
    response_model=BackfillResult,
    tags=["portfolio_members"],
)
def backfill_portfolio_members(
    portfolio_id: int,
    payload: BackfillRequest | None = None,
    db: Session = Depends(get_db),
) -> BackfillResult:
    """回填持仓为组合成员（WP4.3/WP4.4 触发）。

    对每条未平仓持仓创建 source_type=legacy_position 成员，幂等。
    dry_run=true 时只返回统计，不实际写入。
    """
    dry_run = payload.dry_run if payload else False
    stats = backfill_positions_to_members(db, portfolio_id=portfolio_id, dry_run=dry_run)
    return BackfillResult(**stats)


# ============================================================================
# WP8 绩效归因接口（仅追加，不修改上方已稳定端点）
# ============================================================================


@router.get(
    "/portfolios/{portfolio_id}/attribution",
    response_model=AttributionReport,
    tags=["portfolio_attribution"],
)
def get_portfolio_attribution_route(
    portfolio_id: int,
    start_date: date_type | None = Query(None, description="起始日期（含），格式 YYYY-MM-DD"),
    end_date: date_type | None = Query(None, description="结束日期（含），格式 YYYY-MM-DD"),
    dimensions: str | None = Query(
        None,
        description="逗号分隔的维度列表，可选值：by_member,by_execution_mode,by_source,by_rule_signal,backtest_vs_sim,cost_impact；不传则返回全部维度",
    ),
    backtest_run_id: int | None = Query(None, description="回测运行 ID（仅 backtest_vs_sim 维度需要）"),
    db: Session = Depends(get_db),
) -> AttributionReport:
    """WP8：返回组合绩效归因报告。

    用途：
    - 解释组合收益来自哪些成员、执行模式、来源、规则版本
    - 对比回测与模拟账户同期偏差
    - 量化成本/滑点/未成交/风控阻断影响

    数据来源：
    - SimTrade（已平仓交易）join SimOrder（归因字段）
    - BacktestRun（回测快照）
    - PortfolioEquitySnapshot（组合净值时序）

    边界处理：
    - 样本不足时在对应维度返回 sample_warning 字段
    - 不存在的组合返回 404
    - backtest_vs_sim 维度未传 backtest_run_id 时返回 None
    """
    portfolio = db.get(Portfolio, portfolio_id)
    if portfolio is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")

    # 解析 dimensions 参数
    dims: list[str] | None = None
    if dimensions:
        dims = [d.strip() for d in dimensions.split(",") if d.strip()]

    try:
        report = get_attribution_report(
            db,
            portfolio_id,
            start_date=start_date,
            end_date=end_date,
            dimensions=dims,
            backtest_run_id=backtest_run_id,
        )
    except ValueError as exc:
        msg = str(exc)
        if "not found" in msg.lower():
            raise HTTPException(status_code=404, detail=msg)
        raise HTTPException(status_code=400, detail=msg)

    return AttributionReport(**report)


@router.get(
    "/portfolios/{portfolio_id}/reviews",
    response_model=list[ReviewRead],
    tags=["portfolio_attribution"],
)
def list_portfolio_reviews(
    portfolio_id: int,
    limit: int = Query(50, ge=1, le=500, description="最多返回条数"),
    db: Session = Depends(get_db),
) -> list[ReviewRead]:
    """WP8：列出组合的复盘记录（按创建时间降序）。"""
    portfolio = db.get(Portfolio, portfolio_id)
    if portfolio is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")

    rows = db.execute(
        select(Review)
        .where(Review.portfolio_id == portfolio_id)
        .order_by(Review.created_at.desc(), Review.id.desc())
        .limit(limit)
    ).scalars().all()
    return [ReviewRead.model_validate(r) for r in rows]


@router.post(
    "/portfolios/{portfolio_id}/reviews",
    response_model=ReviewRead,
    tags=["portfolio_attribution"],
)
def create_portfolio_review(
    portfolio_id: int,
    payload: ReviewCreate,
    db: Session = Depends(get_db),
) -> ReviewRead:
    """WP8：创建复盘记录。

    接收归因报告快照 + 备注，创建复盘记录关联 portfolio_id 和时间范围。
    若提供 report_snapshot，则直接保存；否则按 start_date/end_date/dimensions
    实时计算归因报告并保存为快照。
    """
    portfolio = db.get(Portfolio, portfolio_id)
    if portfolio is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")

    # 若未提供 report_snapshot，实时计算归因报告
    if payload.report_snapshot is not None:
        report_dict = payload.report_snapshot
    else:
        try:
            report_dict = get_attribution_report(
                db,
                portfolio_id,
                start_date=payload.start_date,
                end_date=payload.end_date,
                dimensions=payload.dimensions,
                backtest_run_id=payload.backtest_run_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    review = Review(
        portfolio_id=portfolio_id,
        start_date=payload.start_date,
        end_date=payload.end_date,
        report_snapshot_json=json.dumps(report_dict, ensure_ascii=False, default=str),
        note=payload.note,
        title=payload.title,
    )
    db.add(review)
    db.commit()
    db.refresh(review)
    return ReviewRead.model_validate(review)
