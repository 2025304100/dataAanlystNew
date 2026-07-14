from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.daily_bar import DailyBar
from app.models.scan import ScanResult
from app.models.symbol import Symbol
from app.models.custom_indicator import CustomIndicator
from app.models.score import Score
from app.schemas.discovery import DiscoveryIndicatorEvaluateRequest
from app.services.backtest import _resolve_formula_expr
from app.schemas.discovery import DiscoveryResultUpdate
from app.services.analysis import calculate_symbol_score
from app.services.factors.score_scope import get_active_score_scope
from app.services.market_data import sync_symbol_daily_bars
from app.services.symbol_names import refresh_symbol_name


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _safe_datetime(value):
    """Safely convert a value to datetime, handling strings from MySQL."""
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


def _serialize_result(result: ScanResult) -> dict:
    # 风控：created_at 为 None 或解析失败时 _safe_datetime 返回 None，相减会 TypeError
    # 兜底为 _now() 使 age_days=0，避免单条脏数据导致整个列表接口 500
    created_ts = _safe_datetime(result.created_at) or _now()
    age_days = max(0, (_now() - created_ts).days)
    return {
        "id": result.id,
        "scan_run_id": result.scan_run_id,
        "symbol_id": result.symbol_id,
        "is_frozen": bool(result.is_frozen),
        "warning_days": result.warning_days,
        "valid_days": result.valid_days,
        "age_days": age_days,
        "is_warning": not result.is_frozen and age_days >= result.warning_days,
        "is_expired": not result.is_frozen and age_days >= result.valid_days,
        "created_at": result.created_at,
    }


def update_discovery_result(db: Session, scan_result_id: int, payload: DiscoveryResultUpdate) -> dict | None:
    result = db.get(ScanResult, scan_result_id)
    if result is None:
        return None
    if payload.is_frozen is not None:
        result.is_frozen = int(payload.is_frozen)
    if payload.warning_days is not None:
        result.warning_days = payload.warning_days
    if payload.valid_days is not None:
        result.valid_days = payload.valid_days
    db.commit()
    db.refresh(result)
    return _serialize_result(result)


def update_discovery_symbol(db: Session, scan_result_id: int) -> dict | None:
    result = db.get(ScanResult, scan_result_id)
    if result is None:
        return None
    if result.is_frozen:
        payload = _serialize_result(result)
        payload["skipped"] = True
        payload["reason"] = "frozen"
        return payload
    symbol = db.get(Symbol, result.symbol_id)
    if symbol is None:
        return None
    refresh_symbol_name(symbol)
    sync_symbol_daily_bars(db=db, symbol=symbol)
    latest_bar = (
        db.execute(select(DailyBar).where(DailyBar.symbol_id == symbol.id).order_by(DailyBar.trade_date.desc()))
        .scalars()
        .first()
    )
    if latest_bar is not None:
        score = calculate_symbol_score(db=db, symbol=symbol, trade_date=latest_bar.trade_date)
        result.quality_score = score.quality_score
        result.timing_score = score.timing_score
        result.priority_score = score.priority_score
        result.stage = score.stage
        result.action = score.action
        result.created_at = _now()
    db.commit()
    db.refresh(result)
    return _serialize_result(result)


def evaluate_discovery_indicators(db: Session, payload: DiscoveryIndicatorEvaluateRequest) -> list[dict]:
    result_ids = [int(item) for item in payload.scan_result_ids if int(item) > 0]
    indicator_keys = [str(item).strip() for item in payload.indicator_keys if str(item).strip()]
    if not result_ids or not indicator_keys:
        return []

    indicator_rows = db.execute(
        select(CustomIndicator).where(
            CustomIndicator.key.in_(indicator_keys),
            CustomIndicator.enabled == True,
        )
    ).scalars().all()
    indicator_map = {row.key: row for row in indicator_rows}
    if not indicator_map:
        return []

    rows = db.execute(
        select(ScanResult, Symbol)
        .join(Symbol, Symbol.id == ScanResult.symbol_id)
        .where(ScanResult.id.in_(result_ids))
    ).all()
    if not rows:
        return []

    # 风控加固：批量预加载 latest_bar / history_bars / score 避免 N+1（原每个 scan_result 查 3 次 = 3N DB 查询）
    symbol_ids = [symbol.id for _, symbol in rows]
    latest_bar_map: dict[int, DailyBar] = {}
    history_bars_map: dict[int, list[DailyBar]] = {}
    score_map: dict[int, Score] = {}

    if symbol_ids:
        from sqlalchemy import func as sa_func
        # 1. 批量取每个 symbol 的最新 bar
        latest_date_subq = (
            select(DailyBar.symbol_id, sa_func.max(DailyBar.trade_date).label("latest_date"))
            .where(DailyBar.symbol_id.in_(symbol_ids))
            .group_by(DailyBar.symbol_id)
        ).subquery()
        latest_bars = db.execute(
            select(DailyBar)
            .join(latest_date_subq,
                  (DailyBar.symbol_id == latest_date_subq.c.symbol_id)
                  & (DailyBar.trade_date == latest_date_subq.c.latest_date))
        ).scalars().all()
        for bar in latest_bars:
            latest_bar_map[bar.symbol_id] = bar

        # 2. 批量取每个 symbol 的 history_bars（最多 250 条）
        latest_dates = {b.symbol_id: b.trade_date for b in latest_bars}
        all_history = db.execute(
            select(DailyBar)
            .where(DailyBar.symbol_id.in_(symbol_ids))
            .order_by(DailyBar.symbol_id, DailyBar.trade_date.desc())
        ).scalars().all()
        history_grouped: dict[int, list[DailyBar]] = {}
        for bar in all_history:
            history_grouped.setdefault(bar.symbol_id, []).append(bar)
        for sym_id, bars_list in history_grouped.items():
            latest_d = latest_dates.get(sym_id)
            if latest_d is None:
                continue
            filtered = [b for b in bars_list if b.trade_date < latest_d][:250]
            history_bars_map[sym_id] = list(reversed(filtered))

        # 3. 批量取每个 symbol 在 latest_bar.trade_date 时的最新 score
        score_pairs = [(sym_id, latest_bar_map[sym_id].trade_date) for sym_id in symbol_ids if sym_id in latest_bar_map]
        if score_pairs:
            min_date = min(d for _, d in score_pairs)
            max_date = max(d for _, d in score_pairs)
            from app.services.backtest import _build_score_map, _latest_score_on_or_before
            bt_score_map = _build_score_map(db, symbol_ids, min_date, max_date)
            for sym_id, target_date in score_pairs:
                score_map[sym_id] = _latest_score_on_or_before(bt_score_map, sym_id, target_date)

    response: list[dict] = []
    for scan_result, symbol in rows:
        latest_bar = latest_bar_map.get(symbol.id)
        values: dict[str, bool | float | None] = {}
        if latest_bar is not None:
            history_bars = history_bars_map.get(symbol.id, [])
            prev_bar = history_bars[-1] if history_bars else None
            score = score_map.get(symbol.id)
            for key in indicator_keys:
                indicator = indicator_map.get(key)
                if indicator is None:
                    values[key] = None
                    continue
                result = _resolve_formula_expr(score, latest_bar, prev_bar, history_bars, indicator.formula)
                if indicator.value_type == "number":
                    try:
                        values[key] = float(result)
                    except (TypeError, ValueError):
                        values[key] = None
                else:
                    values[key] = bool(result)
        else:
            for key in indicator_keys:
                values[key] = None

        response.append({
            "scan_result_id": scan_result.id,
            "symbol_id": symbol.id,
            "values": values,
        })
    return response


def get_latest_discovery_candidates(
    db: Session, *, min_score: float = 0.0, limit: int = 50, scope: str | None = None,
) -> list[dict]:
    """获取最新一次挖掘任务的全量候选（不依赖 executable 过滤）。

    挖掘任务的目的发现机会，结果展示不应依赖 portfolio 交易约束（active_rule → recommended_pct → executable）。
    此函数查最新 done 状态挖掘任务的 scan_run，取 quality 记录（每标的 1 条，去重），
    让用户看到全貌后再决定，而非只看可执行的。

    过期且未冻结的结果会被清理（与 workbench 行为一致）。

    scope: 可选，按 scope 过滤最新任务（如 "cn-stock"/"cn-etf"）。
        传入时只查该 scope 最新完成的任务，避免切换 scope 后丢失之前 scope 的结果。
    """
    from app.models.discovery import DiscoveryTaskRecord
    from app.services.regions import region_from_market

    # 1. 找最新 done 状态的挖掘任务（可选按 scope 过滤）
    task_stmt = (
        select(DiscoveryTaskRecord)
        .where(DiscoveryTaskRecord.status == "done")
    )
    if scope:
        task_stmt = task_stmt.where(DiscoveryTaskRecord.scope == scope)
    task_stmt = task_stmt.order_by(
        DiscoveryTaskRecord.finished_at.desc(), DiscoveryTaskRecord.id.desc()
    )
    latest_task = db.execute(task_stmt).scalars().first()
    if latest_task is None or latest_task.scan_run_id is None:
        return []

    scan_run_id = latest_task.scan_run_id

    # 2. 查 quality 记录（每标的 1 条，去重；quality 排名覆盖所有有评分的标的）
    rows = db.execute(
        select(ScanResult, Symbol)
        .join(Symbol, Symbol.id == ScanResult.symbol_id)
        .where(
            ScanResult.scan_run_id == scan_run_id,
            ScanResult.result_type == "quality",
        )
        .order_by(ScanResult.priority_score.desc(), ScanResult.created_at.desc())
    ).all()

    now = _now()
    valid_rows: list[tuple[ScanResult, Symbol]] = []
    for result, symbol in rows:
        # 过期清理：未冻结且超过 valid_days 的跳过
        if not result.is_frozen:
            age_days = max(0, (now - _safe_datetime(result.created_at)).days)
            if age_days >= result.valid_days:
                continue
        # min_score 过滤
        if float(result.priority_score or 0) < min_score:
            continue
        valid_rows.append((result, symbol))

    valid_rows = valid_rows[:limit]

    # 3. 批量查最新 score 的子分数（trend/momentum 等），与 dashboard workbench 行为一致
    symbol_ids = [s.id for _, s in valid_rows]
    score_map: dict[int, Score] = {}
    if symbol_ids:
        from sqlalchemy import func
        scope = get_active_score_scope(db)
        latest_score_subq = (
            select(Score.symbol_id, func.max(Score.trade_date).label("max_date"))
            .where(Score.symbol_id.in_(symbol_ids))
            .where(Score.weight_mode == scope.weight_mode)
            .where(
                Score.factor_model_run_id == scope.model_run_id
                if scope.weight_mode == 'ridge'
                else True
            )
            .group_by(Score.symbol_id)
            .subquery()
        )
        score_rows = db.execute(
            select(Score).join(
                latest_score_subq,
                (Score.symbol_id == latest_score_subq.c.symbol_id)
                & (Score.trade_date == latest_score_subq.c.max_date),
            )
            .where(Score.weight_mode == scope.weight_mode)
            .where(
                Score.factor_model_run_id == scope.model_run_id
                if scope.weight_mode == 'ridge'
                else True
            )
        ).scalars().all()
        score_map = {s.symbol_id: s for s in score_rows}

    # 4. 构造 WorkbenchCandidate 格式的响应
    candidates: list[dict] = []
    for result, symbol in valid_rows:
        score = score_map.get(symbol.id)
        candidates.append({
            "id": result.id,
            "scan_result_id": result.id,
            "symbol_id": symbol.id,
            "symbol": symbol.symbol,
            "name": symbol.name,
            "market": symbol.market,
            "region": region_from_market(symbol.market),
            "asset_type": symbol.asset_type,
            "quality_score": result.quality_score,
            "timing_score": result.timing_score,
            "priority_score": result.priority_score,
            "trend_score": score.trend_score if score else None,
            "momentum_score": score.momentum_score if score else None,
            "volatility_score": score.volatility_score if score else None,
            "liquidity_score": score.liquidity_score if score else None,
            "breadth_score": score.breadth_score if score else None,
            "event_score": score.event_score if score else None,
            "stage": result.stage,
            "action": result.action,
            "recommended_position_pct": result.recommended_position_pct,
            "rank_no": result.rank_no,
            "created_at": result.created_at,
            "warning_days": result.warning_days,
            "valid_days": result.valid_days,
            "is_frozen": bool(result.is_frozen),
            # P1：评分配置快照字段（用于前端按维度排序和"为什么入选"展示）
            "scoring_preset_key": score.scoring_preset_key if score else None,
            "scoring_preset_name": score.scoring_preset_name if score else None,
            "scoring_config_version": score.scoring_config_version if score else None,
            "dimension_scores_json": score.dimension_scores_json if score else None,
            "scoring_config_snapshot_json": score.scoring_config_snapshot_json if score else None,
        })
    return candidates
