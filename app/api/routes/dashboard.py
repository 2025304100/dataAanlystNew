from fastapi import APIRouter, Depends, HTTPException, Query
from datetime import datetime, timezone
import json
import logging

logger = logging.getLogger(__name__)

from sqlalchemy import case, desc, func, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.journal_entry import JournalEntry
from app.models.portfolio import Portfolio, Position
from app.models.daily_bar import DailyBar
from app.models.scan import ScanResult, ScanRun
from app.models.score import Score
from app.models.symbol import Symbol
from app.models.trade_setup import TradeSetup
from app.models.universe import UniverseSymbol
from app.models.watchlist import Watchlist, WatchlistItem
from app.schemas.dashboard import (
    WorkbenchActiveRule,
    DashboardOverview,
    WorkbenchBar,
    DashboardWorkbench,
    WorkbenchCandidate,
    WorkbenchJournal,
    WorkbenchLatestScan,
    WorkbenchMarketScope,
    WorkbenchPosition,
    WorkbenchScore,
    WorkbenchSymbolDetail,
    WorkbenchTrade,
    WorkbenchWatchlist,
)
from app.services.allocation import compute_allocation, get_active_rule
from app.services.regions import markets_for_region, region_from_market
from app.services.signal_stats import build_similar_signal_stats
from app.services.scoring_config_engine import calculate_universe_symbol_score
from app.services.sim_accounts import build_sim_account_summary, recent_sim_trades
from app.services.trade_plans import build_trade_setup_view, load_recent_bars, upsert_trade_setup


router = APIRouter()


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _safe_datetime(value):
    """安全地将值转换为 datetime 类型，处理 MySQL 返回的字符串。"""
    from datetime import datetime
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d"):
            try:
                return datetime.strptime(value, fmt)
            except ValueError:
                continue
    return None


def _safe_iso_date(value) -> str | None:
    """Serialize SQL Date/DateTime/string values without dropping plain date objects."""
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, str):
        return value
    return None


def _is_scan_result_valid(result: ScanResult) -> bool:
    if result.is_frozen:
        return True
    return (_now() - _safe_datetime(result.created_at)).days < result.valid_days


def _delete_expired_scan_results(db: Session, rows: list[tuple[ScanResult, Symbol]]) -> list[tuple[ScanResult, Symbol]]:
    valid_rows: list[tuple[ScanResult, Symbol]] = []
    deleted = False
    for result, symbol in rows:
        if _is_scan_result_valid(result):
            valid_rows.append((result, symbol))
            continue
        db.delete(result)
        deleted = True
    if deleted:
        db.commit()
    return valid_rows


def _latest_score_map(db: Session, symbol_ids: list[int]) -> dict[int, Score]:
    if not symbol_ids:
        return {}
    rows = (
        db.execute(
            select(Score).where(
                Score.symbol_id.in_(symbol_ids),
                Score.id.in_(
                    select(func.max(Score.id))
                    .where(Score.symbol_id.in_(symbol_ids))
                    .group_by(Score.symbol_id)
                ),
            )
        )
        .scalars()
        .all()
    )
    return {row.symbol_id: row for row in rows}


@router.get("/dashboard/overview", response_model=DashboardOverview)
def get_dashboard_overview(
    portfolio_id: int = Query(...),
    db: Session = Depends(get_db),
):
    portfolio = db.get(Portfolio, portfolio_id)
    if portfolio is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")

    symbols_count = db.execute(select(func.count(Symbol.id))).scalar_one()
    watchlists_count = db.execute(select(func.count(Watchlist.id))).scalar_one()
    allocation = compute_allocation(db, portfolio_id)

    latest_run = db.execute(
        select(ScanRun).where(ScanRun.portfolio_id == portfolio_id, ScanRun.status == "done").order_by(ScanRun.id.desc())
    ).scalars().first()
    top_candidates = []
    if latest_run is not None:
        rows = db.execute(
            select(ScanResult, Symbol)
            .join(Symbol, Symbol.id == ScanResult.symbol_id)
            .where(ScanResult.scan_run_id == latest_run.id, ScanResult.result_type == "executable")
            .order_by(ScanResult.rank_no.asc())
            .limit(5)
        ).all()
        top_candidates = [
            {
                "symbol_id": result.symbol_id,
                "symbol": symbol.symbol,
                "name": symbol.name,
                "quality_score": result.quality_score,
                "timing_score": result.timing_score,
                "stage": result.stage,
                "action": result.action,
                "recommended_position_pct": result.recommended_position_pct,
            }
            for result, symbol in rows
        ]

    risk_flags = []
    if allocation["cash_pct"] < 0.1:
        risk_flags.append("Cash reserve is below 10%")
    if allocation["total_position_pct"] > 0.9:
        risk_flags.append("Total exposure is above 90%")

    return DashboardOverview(
        symbols_count=symbols_count,
        watchlists_count=watchlists_count,
        total_position_pct=allocation["total_position_pct"],
        cash_pct=allocation["cash_pct"],
        top_candidates=top_candidates,
        risk_flags=risk_flags,
    )


@router.get("/dashboard/workbench", response_model=DashboardWorkbench)
def get_dashboard_workbench(
    portfolio_id: int = Query(...),
    candidate_limit: int = Query(default=8, ge=1, le=80),
    score_limit: int = Query(default=10, ge=1, le=120),
    market_group: str = Query(default="all"),
    db: Session = Depends(get_db),
):
    portfolio = db.get(Portfolio, portfolio_id)
    if portfolio is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")

    overview = get_dashboard_overview(portfolio_id=portfolio_id, db=db)
    active_rule = get_active_rule(db, portfolio_id)
    market_codes = markets_for_region(market_group)

    latest_run = db.execute(
        select(ScanRun).where(ScanRun.portfolio_id == portfolio_id, ScanRun.status == "done").order_by(ScanRun.id.desc())
    ).scalars().first()

    latest_scan = WorkbenchLatestScan()
    candidates: list[WorkbenchCandidate] = []
    if latest_run is not None:
        candidate_stmt = (
            select(ScanResult, Symbol)
            .join(Symbol, Symbol.id == ScanResult.symbol_id)
            .where(ScanResult.scan_run_id == latest_run.id, ScanResult.result_type == "executable")
        )
        if market_codes:
            candidate_stmt = candidate_stmt.where(Symbol.market.in_(market_codes))
        candidate_rows = _delete_expired_scan_results(db, db.execute(candidate_stmt).all())
        candidate_rows.sort(
            key=lambda item: (
                int(item[0].is_frozen),
                float(item[0].priority_score or 0),
                item[0].created_at,
            ),
            reverse=True,
        )
        candidate_rows = candidate_rows[:candidate_limit]
        candidate_score_map = _latest_score_map(db, [symbol.id for _, symbol in candidate_rows])
        candidates = [
            WorkbenchCandidate(
                id=result.id,
                scan_result_id=result.id,
                symbol_id=symbol.id,
                symbol=symbol.symbol,
                name=symbol.name,
                market=symbol.market,
                region=region_from_market(symbol.market),
                asset_type=symbol.asset_type,
                quality_score=result.quality_score,
                timing_score=result.timing_score,
                priority_score=result.priority_score,
                trend_score=candidate_score_map.get(symbol.id).trend_score if candidate_score_map.get(symbol.id) else None,
                momentum_score=candidate_score_map.get(symbol.id).momentum_score if candidate_score_map.get(symbol.id) else None,
                volatility_score=candidate_score_map.get(symbol.id).volatility_score if candidate_score_map.get(symbol.id) else None,
                liquidity_score=candidate_score_map.get(symbol.id).liquidity_score if candidate_score_map.get(symbol.id) else None,
                breadth_score=candidate_score_map.get(symbol.id).breadth_score if candidate_score_map.get(symbol.id) else None,
                event_score=candidate_score_map.get(symbol.id).event_score if candidate_score_map.get(symbol.id) else None,
                stage=result.stage,
                action=result.action,
                recommended_position_pct=result.recommended_position_pct,
                rank_no=result.rank_no,
                created_at=result.created_at,
                warning_days=result.warning_days,
                valid_days=result.valid_days,
                is_frozen=bool(result.is_frozen),
            )
            for result, symbol in candidate_rows
        ]
        total_results_stmt = select(func.count(ScanResult.id)).where(ScanResult.scan_run_id == latest_run.id)
        executable_count_stmt = select(func.count(ScanResult.id)).where(
            ScanResult.scan_run_id == latest_run.id,
            ScanResult.result_type == "executable",
        )
        if market_codes:
            total_results_stmt = (
                select(func.count(ScanResult.id))
                .join(Symbol, Symbol.id == ScanResult.symbol_id)
                .where(ScanResult.scan_run_id == latest_run.id, Symbol.market.in_(market_codes))
            )
            executable_count_stmt = (
                select(func.count(ScanResult.id))
                .join(Symbol, Symbol.id == ScanResult.symbol_id)
                .where(
                    ScanResult.scan_run_id == latest_run.id,
                    ScanResult.result_type == "executable",
                    Symbol.market.in_(market_codes),
                )
            )
        total_results = db.execute(total_results_stmt).scalar_one()
        executable_count = db.execute(executable_count_stmt).scalar_one()
        latest_scan = WorkbenchLatestScan(
            scan_run_id=latest_run.id,
            run_name=latest_run.run_name,
            created_at=latest_run.created_at,
            executable_count=executable_count,
            total_results=total_results,
            auto_scan=latest_run.run_name == "post-sync-auto-scan",
        )

    score_stmt = (
        select(Score, Symbol)
        .join(Symbol, Symbol.id == Score.symbol_id)
        .where(
            Score.id.in_(
                select(func.max(Score.id)).group_by(Score.symbol_id)
            )
        )
        .order_by(Score.priority_score.desc())
        .limit(score_limit)
    )
    if market_codes:
        score_stmt = score_stmt.where(Symbol.market.in_(market_codes))
    score_rows = db.execute(score_stmt).all()
    latest_scores = [
        WorkbenchScore(
            symbol_id=symbol.id,
            symbol=symbol.symbol,
            name=symbol.name,
            market=symbol.market,
            region=region_from_market(symbol.market),
            asset_type=symbol.asset_type,
            trade_date=score.trade_date,
            quality_score=score.quality_score,
            timing_score=score.timing_score,
            stage=score.stage,
            action=score.action,
            priority_score=score.priority_score,
            trend_score=score.trend_score,
            momentum_score=score.momentum_score,
            volatility_score=score.volatility_score,
            liquidity_score=score.liquidity_score,
            breadth_score=score.breadth_score,
            event_score=score.event_score,
            created_at=score.created_at,
            warning_days=3,
            valid_days=5,
            is_frozen=False,
        )
        for score, symbol in score_rows
    ]

    watchlist_rows = db.execute(
        select(
            Watchlist.id,
            Watchlist.name,
            Watchlist.list_type,
            func.count(WatchlistItem.id).label("item_count"),
        )
        .outerjoin(WatchlistItem, WatchlistItem.watchlist_id == Watchlist.id)
        .group_by(Watchlist.id, Watchlist.name, Watchlist.list_type)
        .order_by(Watchlist.id.asc())
    ).all()
    watchlists = [
        WorkbenchWatchlist(
            id=row.id,
            name=row.name,
            list_type=row.list_type,
            item_count=row.item_count,
        )
        for row in watchlist_rows
    ]

    journal_rows = db.execute(
        select(JournalEntry)
        .where(JournalEntry.portfolio_id == portfolio_id)
        .order_by(desc(JournalEntry.created_at), desc(JournalEntry.id))
        .limit(6)
    ).scalars().all()
    journals = [
        WorkbenchJournal(
            id=journal.id,
            title=journal.title,
            entry_type=journal.entry_type,
            symbol_id=journal.symbol_id,
            created_at=journal.created_at,
            trade_setup_id=journal.trade_setup_id,
            content=journal.content,
            outcome=journal.outcome,
            review_note=journal.review_note,
            follow_system=journal.follow_system,
            score_id=journal.score_id,
            stage=journal.stage,
            action=journal.action,
            actual_action=journal.actual_action,
        )
        for journal in journal_rows
    ]

    portfolio_payload = None
    if portfolio is not None:
        portfolio_payload = {
            "id": portfolio.id,
            "name": portfolio.name,
            "account_type": portfolio.account_type,
            "total_capital": portfolio.total_capital,
            "investable_ratio": portfolio.investable_ratio,
            "cash_reserve_ratio": portfolio.cash_reserve_ratio,
            "currency": portfolio.currency,
        }

    region_expr = case(
        (Symbol.market.in_(("sh", "sz", "bj", "cn", "SH", "SZ", "BJ", "CN")), "cn"),
        (Symbol.market.in_(("us", "nasdaq", "nyse", "amex", "US", "NASDAQ", "NYSE", "AMEX")), "us"),
        else_="other",
    )
    region_rows = db.execute(
        select(region_expr.label("region"), func.count(Symbol.id).label("cnt"))
        .where(Symbol.is_active == 1)
        .group_by(region_expr)
    ).all()
    region_counts = {"cn": 0, "us": 0, "other": 0}
    for region, cnt in region_rows:
        region_counts[region] = cnt
    total_symbols = sum(region_counts.values())
    filtered_symbols = total_symbols if market_group == "all" else region_counts.get(market_group, 0)
    account_summary = build_sim_account_summary(db, portfolio)
    recent_trades = [WorkbenchTrade(**item) for item in recent_sim_trades(db, portfolio_id, limit=8)]

    # 构建所有持仓
    position_rows = db.execute(
        select(Position, Symbol)
        .join(Symbol, Symbol.id == Position.symbol_id)
        .where(Position.portfolio_id == portfolio_id)
        .order_by(Position.market_value.desc())
    ).all()

    # 风控加固：批量预加载每个 symbol 的最新 bar，避免循环内 N+1 查询
    position_symbol_ids = [sym.id for _, sym in position_rows]
    latest_close_map: dict[int, float] = {}
    if position_symbol_ids:
        from sqlalchemy import func as sa_func
        latest_bar_subq = (
            select(sa_func.max(DailyBar.id))
            .where(DailyBar.symbol_id.in_(position_symbol_ids))
            .group_by(DailyBar.symbol_id)
            .scalar_subquery()
        )
        latest_bars = db.execute(
            select(DailyBar.symbol_id, DailyBar.close)
            .where(DailyBar.id.in_(latest_bar_subq))
        ).all()
        for sym_id, close in latest_bars:
            latest_close_map[sym_id] = float(close)

    positions = []
    for pos, sym in position_rows:
        lp = latest_close_map.get(sym.id) or pos.latest_price or pos.avg_cost
        lp = float(lp)
        mv = round(pos.quantity * lp, 2)
        pnl = round((lp - pos.avg_cost) * pos.quantity, 2)
        pnl_pct = round((lp - pos.avg_cost) / pos.avg_cost, 4) if pos.avg_cost else 0.0
        pct = round(mv / portfolio.total_capital, 4) if portfolio.total_capital else 0.0
        positions.append(
            WorkbenchPosition(
                symbol_id=sym.id,
                symbol=sym.symbol,
                name=sym.name,
                quantity=pos.quantity,
                avg_cost=pos.avg_cost,
                latest_price=lp,
                market_value=mv,
                position_pct=pct,
                unrealized_pnl=pnl,
                unrealized_pnl_pct=pnl_pct,
            )
        )

    return DashboardWorkbench(
        portfolio=portfolio_payload,
        active_rule=None
        if active_rule is None
        else WorkbenchActiveRule(
            id=active_rule.id,
            rule_name=active_rule.rule_name,
            max_single_position_pct=active_rule.max_single_position_pct,
            max_stock_position_pct=active_rule.max_stock_position_pct,
            max_etf_position_pct=active_rule.max_etf_position_pct,
            max_sector_position_pct=active_rule.max_sector_position_pct,
            max_loss_per_trade_pct=active_rule.max_loss_per_trade_pct,
            max_open_positions=active_rule.max_open_positions,
            stage_limits_json=json.loads(active_rule.stage_limits_json) if active_rule.stage_limits_json else None,
        ),
        market_scope=WorkbenchMarketScope(
            selected_group=market_group,
            available_groups=["all", "cn", "us"],
            total_symbols=total_symbols,
            filtered_symbols=filtered_symbols,
            region_counts=region_counts,
        ),
        overview=overview,
        account_summary=account_summary,
        latest_scan=latest_scan,
        candidates=candidates,
        latest_scores=latest_scores,
        recent_trades=recent_trades,
        positions=positions,
        watchlists=watchlists,
        journals=journals,
    )


@router.get("/dashboard/symbol-detail", response_model=WorkbenchSymbolDetail)
def get_symbol_detail_panel(
    symbol_id: int = Query(...),
    portfolio_id: int = Query(...),
    sample_limit: int | None = Query(default=None, ge=5, le=240),
    bar_limit: int = Query(default=120, ge=20, le=1000),
    db: Session = Depends(get_db),
):
    portfolio = db.get(Portfolio, portfolio_id)
    if portfolio is None:
        raise HTTPException(status_code=404, detail="Portfolio not found")

    symbol = db.get(Symbol, symbol_id)
    if symbol is None:
        raise HTTPException(status_code=404, detail="Symbol not found")

    # Merge tracked and universe bars first so stale business rows cannot hide fresher market data.
    # Scores dated after the latest available bar are invalid for forward-return statistics.
    bar_rows = list(reversed(load_recent_bars(db, symbol_id, limit=bar_limit)))
    latest_bar_date = bar_rows[0].trade_date if bar_rows else None
    raw_latest_score = db.execute(
        select(Score).where(Score.symbol_id == symbol_id).order_by(desc(Score.trade_date), desc(Score.id))
    ).scalars().first()
    score_stmt = select(Score).where(Score.symbol_id == symbol_id)
    if latest_bar_date is not None:
        score_stmt = score_stmt.where(Score.trade_date <= latest_bar_date)
    latest_score = db.execute(
        score_stmt.order_by(desc(Score.trade_date), desc(Score.id))
    ).scalars().first()
    needs_market_date_score = (
        latest_bar_date is not None
        and raw_latest_score is not None
        and raw_latest_score.trade_date > latest_bar_date
        and (latest_score is None or latest_score.trade_date < latest_bar_date)
    )
    if needs_market_date_score:
        universe_symbol = db.execute(
            select(UniverseSymbol).where(UniverseSymbol.symbol == symbol.symbol)
        ).scalars().first()
        if universe_symbol is not None:
            try:
                latest_score = calculate_universe_symbol_score(
                    db,
                    universe_symbol,
                    symbol,
                    latest_bar_date,
                )
                db.commit()
            except Exception:
                logger.warning(
                    "symbol-detail: failed to repair future-dated score for symbol_id=%s, market_date=%s",
                    symbol_id,
                    latest_bar_date,
                    exc_info=True,
                )
                db.rollback()
                latest_score = db.execute(
                    score_stmt.order_by(desc(Score.trade_date), desc(Score.id))
                ).scalars().first()
    if latest_score is None:
        latest_score = raw_latest_score
    latest_setup = db.execute(
        select(TradeSetup)
        .where(TradeSetup.symbol_id == symbol_id, TradeSetup.portfolio_id == portfolio_id)
        .order_by(desc(TradeSetup.created_at), desc(TradeSetup.id))
    ).scalars().first()
    score_rows = db.execute(
        score_stmt.order_by(desc(Score.trade_date), desc(Score.id)).limit(20)
    ).scalars().all()
    if not score_rows and latest_score is not None:
        score_rows = [latest_score]
    journal_rows = db.execute(
        select(JournalEntry)
        .where(JournalEntry.symbol_id == symbol_id, JournalEntry.portfolio_id == portfolio_id)
        .order_by(desc(JournalEntry.created_at), desc(JournalEntry.id))
        .limit(8)
    ).scalars().all()
    position = db.execute(
        select(Position).where(Position.portfolio_id == portfolio_id, Position.symbol_id == symbol_id)
    ).scalars().first()
    recent_trades = [WorkbenchTrade(**item) for item in recent_sim_trades(db, portfolio_id, limit=6, symbol_id=symbol_id)]
    latest_position_price = bar_rows[0].close if bar_rows else None

    if bar_rows and latest_score is not None and (latest_setup is None or latest_setup.score_id != latest_score.id):
        try:
            latest_setup = upsert_trade_setup(
                db=db,
                portfolio_id=portfolio_id,
                symbol=symbol,
                score=latest_score,
            )
            db.commit()
            db.refresh(latest_setup)
        except ValueError as exc:
            # 风控加固：机会挖掘候选可能已有评分但 K线尚未同步（无 daily bars），
            # 此时无法生成交易计划。降级为 latest_setup=None，避免 500 阻塞详情面板。
            logger.warning(
                "symbol-detail: upsert_trade_setup failed for symbol_id=%s (bars=%d): %s",
                symbol_id, len(bar_rows), exc,
            )
            latest_setup = None
            db.rollback()

    # latest_trade_setup 同样依赖 daily bars：无 K线时降级为 None
    latest_trade_setup_view = None
    if latest_setup is not None and latest_score is not None and bar_rows:
        try:
            latest_trade_setup_view = {
                **build_trade_setup_view(
                    db=db,
                    portfolio_id=portfolio_id,
                    symbol=symbol,
                    score=latest_score,
                    setup=latest_setup,
                    bars=list(reversed(bar_rows)),
                ),
            }
        except ValueError as exc:
            logger.warning(
                "symbol-detail: build_trade_setup_view failed for symbol_id=%s: %s",
                symbol_id, exc,
            )
            latest_trade_setup_view = None

    return WorkbenchSymbolDetail(
        symbol={
            "id": symbol.id,
            "symbol": symbol.symbol,
            "name": symbol.name,
            "market": symbol.market,
            "region": region_from_market(symbol.market),
            "asset_type": symbol.asset_type,
            "theme": symbol.theme,
            "industry": symbol.industry,
        },
        latest_score=None
        if latest_score is None
        else {
            "id": latest_score.id,
            "trade_date": _safe_iso_date(latest_score.trade_date),
            "quality_score": latest_score.quality_score,
            "quality_grade": latest_score.quality_grade,
            "timing_score": latest_score.timing_score,
            "stage": latest_score.stage,
            "action": latest_score.action,
            "priority_score": latest_score.priority_score,
            "trend_score": latest_score.trend_score,
            "momentum_score": latest_score.momentum_score,
            "volatility_score": latest_score.volatility_score,
            "liquidity_score": latest_score.liquidity_score,
            "breadth_score": latest_score.breadth_score,
            "event_score": latest_score.event_score,
            "breakout_score": latest_score.breakout_score,
            "pullback_score": latest_score.pullback_score,
            "overheat_penalty": latest_score.overheat_penalty,
        },
        latest_trade_setup=latest_trade_setup_view,
        signal_stats=build_similar_signal_stats(db, symbol, latest_score, portfolio_id=portfolio_id, sample_limit=sample_limit),
        position=None
        if position is None
        else {
            "quantity": position.quantity,
            "avg_cost": position.avg_cost,
            "latest_price": latest_position_price or position.latest_price,
            "market_value": round(position.quantity * (latest_position_price or position.latest_price), 2),
            "position_pct": round((position.quantity * (latest_position_price or position.latest_price)) / portfolio.total_capital, 4)
            if portfolio.total_capital
            else position.position_pct,
            "asset_type": position.asset_type,
        },
        score_history=[
            {
                "id": row.id,
                "trade_date": _safe_iso_date(row.trade_date),
                "quality_score": row.quality_score,
                "timing_score": row.timing_score,
                "stage": row.stage,
                "action": row.action,
                "priority_score": row.priority_score,
            }
            for row in reversed(score_rows)
        ],
        bars=[
            WorkbenchBar(
                trade_date=row.trade_date,
                open=row.open,
                high=row.high,
                low=row.low,
                close=row.close,
                volume=row.volume,
            )
            for row in reversed(bar_rows)
        ],
        journals=[
            WorkbenchJournal(
                id=row.id,
                title=row.title,
                entry_type=row.entry_type,
                symbol_id=row.symbol_id,
                created_at=row.created_at,
                trade_setup_id=row.trade_setup_id,
                content=row.content,
                outcome=row.outcome,
                review_note=row.review_note,
                follow_system=row.follow_system,
                score_id=row.score_id,
                stage=row.stage,
                action=row.action,
                actual_action=row.actual_action,
            )
            for row in journal_rows
        ],
        recent_trades=recent_trades,
    )
