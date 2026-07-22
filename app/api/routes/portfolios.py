import json
from datetime import date as date_type
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.portfolio import Portfolio, PortfolioRule, Position
from app.models.sim_account import CashLedger, SimOrder, SimTrade
from app.models.symbol import Symbol
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
from app.services.allocation import compute_allocation, get_active_rule
from app.services.auto_trade_task import run_auto_trade
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
from app.services.sim_accounts import ensure_sim_account_seed


router = APIRouter()


@router.get("/portfolios", response_model=list[PortfolioRead])
def list_portfolios(db: Session = Depends(get_db)):
    return db.execute(select(Portfolio).order_by(Portfolio.id.desc())).scalars().all()


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
        total_capital=payload.total_capital,
        investable_ratio=payload.investable_ratio,
        cash_reserve_ratio=payload.cash_reserve_ratio,
        currency=payload.currency,
        is_default=int(payload.is_default),
        auto_trade_enabled=int(payload.auto_trade_enabled),
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
    """P2-3：手动触发组合自动交易评估与下单。

    首次使用建议 dry_run=True 查看计划，确认无误后再 dry_run=False 实际下单。

    前置条件：
    - 组合必须 account_type="simulated"
    - 组合必须已开启 auto_trade_enabled（在组合管理 Modal 中开启）

    返回：
    - sells: 卖出计划/执行结果（action=exit 全卖，action=reduce 卖一半）
    - buys: 买入计划/执行结果（action=open/buy_dip 且 can_open=True）
    - errors: 单笔失败原因
    """
    try:
        result = run_auto_trade(
            db=db,
            portfolio_id=portfolio_id,
            dry_run=payload.dry_run,
            buy_candidate_limit=payload.buy_candidate_limit,
        )
        return AutoTradeResult(**result)
    except ValueError as exc:
        msg = str(exc)
        if "not found" in msg:
            raise HTTPException(status_code=404, detail=msg)
        if "not simulated" in msg:
            raise HTTPException(status_code=400, detail=msg)
        if "auto_trade_enabled" in msg:
            raise HTTPException(status_code=409, detail=msg)
        raise HTTPException(status_code=400, detail=msg)
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
    return db.execute(select(Position).where(Position.portfolio_id == portfolio_id).order_by(Position.id.desc())).scalars().all()


@router.post("/portfolios/{portfolio_id}/positions", response_model=PositionRead)
def upsert_position(portfolio_id: int, payload: PositionUpsert, db: Session = Depends(get_db)):
    portfolio = db.get(Portfolio, portfolio_id)
    symbol = db.get(Symbol, payload.symbol_id)
    if portfolio is None or symbol is None:
        raise HTTPException(status_code=404, detail="Portfolio or symbol not found")

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
    return position


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
    return [PortfolioMemberRead(**_member_to_dict(m)) for m in members]


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
