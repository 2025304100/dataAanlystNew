"""BFG 预检服务（Task 4 改造：交易日历 Adapter + 真实 SSD 排除统计）。

Fail-closed 严格策略：
- 交易日历：优先 trade_calendar 表 → calendar_utils 模块，禁止 5/7 自然日粗估。
- 排除统计：真实查 security_status_daily，覆盖率 < 70% 或空表 → 阻断。
- 不破坏 Task 1 改动：INSUFFICIENT_TRADE_DAYS severity=error、INVALID_DATE_RANGE、空 array autofill 均保留。
"""
from __future__ import annotations

import logging
import uuid
from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session
from sqlalchemy import select, union_all, literal_column, func, text
from app.models.portfolio_member import PortfolioMember
from app.models.portfolio_candidate import PortfolioCandidate
from app.models.symbol import Symbol
from app.models.universe import UniverseDailyBar
from app.models.security_status import SecurityStatusDaily

from app.schemas.backtest import BacktestFilterConfigDTO
from app.schemas.bfg_precheck import (
    BacktestPrecheckRequest,
    BacktestPrecheckResponse,
    DataCutoffInfo,
    ExcludedSymbolDays,
    FactorEvaluationPrecheckRequest,
    FactorEvaluationPrecheckResponse,
    PrecheckBlockerItem,
    TargetCoverageInfo,
)

logger = logging.getLogger(__name__)
from app.services.backtest_filters.config import (
    BacktestFilterConfig,
    compute_config_hash,
    validate_production_fidelity,
)
from app.services.bfg_trade_calendar_adapter import (
    TradeCalendarUnavailableError,
    get_trading_days,
)
from app.services.security_status.provenance import is_verified_status_row, latest_verified_status_date
from app.models.security_status import SecurityStatusDaily

if TYPE_CHECKING:
    pass


# ---------------------------------------------------------------------------
# Task 12.1: 数据截止日期 5+1 要素计算
# ---------------------------------------------------------------------------
def compute_data_cutoff(db: Session) -> tuple[DataCutoffInfo | None, bool, str | None]:
    """查询行情/状态/因子三类数据的截止日期。

    Returns:
        (data_cutoff, cutoff_mismatch, cutoff_warning_zh)
        - factors_date: 若 factor_scores / scores 表缺失则返回 None，不能抛错
        - cutoff_mismatch: bars/status/factors 有效值不全相等 => True
        - cutoff_warning_zh: mismatch 时返回中文提示
    """
    try:
        bars_date: date | None = db.execute(
            select(func.max(UniverseDailyBar.trade_date))
        ).scalar_one_or_none()
    except Exception as exc:  # pragma: no cover - 表不存在等极端情况
        logger.warning("查询 universe_daily_bars 截止日期失败: %s", exc)
        bars_date = None

    try:
        status_date: date | None = latest_verified_status_date(db)
    except Exception as exc:  # pragma: no cover
        logger.warning("查询 security_status_daily 截止日期失败: %s", exc)
        status_date = None

    factors_date: date | None = None
    # 依次尝试 scores 表 -> factor_scores 表；两者都不存在 -> None
    for candidate_table in ("scores", "factor_scores"):
        try:
            row = db.execute(
                text(f"SELECT MAX(trade_date) FROM {candidate_table}")
            ).scalar_one_or_none()
        except Exception as exc:
            logger.debug("查询 %s 截止日期跳过: %s", candidate_table, exc)
            continue
        if row is not None:
            if isinstance(row, date):
                factors_date = row
            else:
                try:
                    factors_date = date.fromisoformat(str(row))
                except (TypeError, ValueError):
                    factors_date = None
            break

    valid_dates = [d for d in (bars_date, status_date, factors_date) if d is not None]
    # A date for only the available subsets must not imply that all required
    # datasets can be used through that date. Keep each known source date below.
    unified_earliest: date | None = min(valid_dates) if len(valid_dates) == 3 else None
    sync_at = datetime.utcnow()
    version = f"bfg-dataset-{sync_at.strftime('%Y%m%d')}"

    info = DataCutoffInfo(
        bars_date=bars_date,
        status_date=status_date,
        factors_date=factors_date,
        unified_earliest=unified_earliest,
        sync_at=sync_at,
        version=version,
    )

    # mismatch 判断：有效值 >= 2 且不全相等
    distinct = {d.isoformat() for d in valid_dates}
    mismatch = len(valid_dates) >= 2 and len(distinct) > 1
    warning_zh = None
    if mismatch:
        warning_zh = ("数据截止日期不一致，回测按最早截止日期计算" if unified_earliest is not None
            else "行情、股票状态或评分资料不完整，暂时无法确定共同可用的截止日期。")
    return info, mismatch, warning_zh


class StatusDataUnavailableError(Exception):
    """security_status_daily 缺失/为空/覆盖率不足（fail-closed）。"""

    def __init__(self, message: str, *, unknown_count: int | None = None):
        super().__init__(message)
        self.unknown_count = unknown_count


def _cfg_from_dto(dto: BacktestFilterConfigDTO | None) -> BacktestFilterConfig:
    if dto is None:
        return BacktestFilterConfig()
    return BacktestFilterConfig(
        filter_new_listing=dto.filter_new_listing,
        min_listing_age_calendar_days=dto.min_listing_age_calendar_days,
        filter_st=dto.filter_st,
        filter_suspended=dto.filter_suspended,
        force_delisting_liquidation=dto.force_delisting_liquidation,
        delisting_period_excluded=dto.delisting_period_excluded,
        min_history_days=dto.min_history_days,
        production_fidelity=dto.production_fidelity,
    )


def _estimate_trade_days(start: date, end: date) -> int:
    """5/7 自然日估算 —— 仅保留给 factor_eval_precheck 内部使用（非回测预检主路径）。

    回测预检（run_backtest_precheck）绝不调用该函数，强制使用 Adapter。
    """
    total_days = max(0, (end - start).days + 1)
    return max(1, int(total_days * 5 / 7)) if total_days else 0


def _blocker(
    code: str,
    severity: str,
    category: str,
    title_zh: str,
    detail_zh: str,
    evidence: dict | None = None,
    fix_link: dict | None = None,
) -> PrecheckBlockerItem:
    return PrecheckBlockerItem(
        code=code,
        severity=severity,  # type: ignore[arg-type]
        category=category,  # type: ignore[arg-type]
        title_zh=title_zh,
        detail_zh=detail_zh or title_zh,
        correlation_id=uuid.uuid4().hex[:8],
        evidence=dict(evidence or {}),
        fix_link=fix_link,
    )


# ---------------------------------------------------------------------------
# SSD 真实排除统计（fail-closed 覆盖率校验）
# ---------------------------------------------------------------------------
def _collect_security_status_daily_exclusions(
    session: Session,
    symbol_ids: list[int],
    start_date: date,
    end_date: date,
    trade_days: list[date],
    min_listing_age_days: int = 120,
) -> ExcludedSymbolDays:
    """逐 (sid, day) 对照 trade_days 集合，累加分项到 ExcludedSymbolDays。

    Fail-closed 规则：
      1. 表不存在 / query 抛异常 / 范围空 → StatusDataUnavailableError。
      2. 有明确历史来源及上市日的 (symbol, trade_date) / 应有股票日总数 < 0.7 → 阻断。
      3. 正常返回分项（至少在有真实状态 fixture 时 total > 0）。
    """
    if not symbol_ids:
        raise StatusDataUnavailableError("symbol_ids 为空，无法做 SSD 统计")
    if not trade_days:
        raise StatusDataUnavailableError("trade_days 为空，无法做 SSD 覆盖校验")

    trade_day_set = set(trade_days)
    expected_points = len(set(symbol_ids)) * len(trade_day_set)

    try:
        stmt = (
            select(
                SecurityStatusDaily.symbol_id,
                SecurityStatusDaily.trade_date,
                SecurityStatusDaily.is_st,
                SecurityStatusDaily.is_suspended,
                SecurityStatusDaily.is_delisting_period,
                SecurityStatusDaily.is_listed,
                SecurityStatusDaily.listing_date,
                SecurityStatusDaily.delisting_date,
                SecurityStatusDaily.status_source,
            )
            .where(
                SecurityStatusDaily.symbol_id.in_(symbol_ids),
                SecurityStatusDaily.trade_date >= start_date,
                SecurityStatusDaily.trade_date <= end_date,
            )
        )
        rows = session.execute(stmt).fetchall()
    except Exception as ex:
        raise StatusDataUnavailableError(
            f"查询 security_status_daily 失败：{type(ex).__name__}: {ex}"
        ) from ex

    if not rows:
        raise StatusDataUnavailableError(
            "所选股票和日期没有历史状态记录。", unknown_count=expected_points,
        )

    # 每只股票的每个交易日都必须有自己的记录；任意一只不能替其他股票提供覆盖。
    verified_points = {(r.symbol_id, r.trade_date) for r in rows
        if r.trade_date in trade_day_set and is_verified_status_row(r, r.trade_date)}
    unknown_points = expected_points - len(verified_points)
    covered_ratio = len(verified_points) / expected_points if expected_points else 0.0
    if covered_ratio < 0.7:
        raise StatusDataUnavailableError(
            f"历史状态资料覆盖不足 70%，已提供 {len(verified_points)}/{expected_points} 个股票交易日"
            f"（{covered_ratio:.1%}）。未知、测试资料及当前快照不计入有效历史记录。",
            unknown_count=unknown_points,
        )

    new_listing_cnt = 0
    st_cnt = 0
    suspended_cnt = 0
    delisting_period_cnt = 0
    status_unknown_cnt = unknown_points
    price_or_volume_invalid_cnt = 0

    for r in rows:
        if r.trade_date not in trade_day_set:
            continue  # 非交易日不计数

        is_unknown = not is_verified_status_row(r, r.trade_date)

        # 次新股判定：trade_date - listing_date < min_listing_age；未知 listing_date → UNKNOWN
        is_new_listing = False
        if r.listing_date is None:
            is_unknown = True
        else:
            age_days = (r.trade_date - r.listing_date).days
            if 0 <= age_days < min_listing_age_days:
                is_new_listing = True

        is_st = bool(r.is_st)
        is_suspended = bool(r.is_suspended)
        is_delisting = bool(r.is_delisting_period) or (
            (r.is_listed is not None) and (r.is_listed == 0)
        )

        if is_unknown:
            continue  # 缺失及未知已按完整 symbol×day 分母统计。
        else:
            if is_new_listing:
                new_listing_cnt += 1
            if is_st:
                st_cnt += 1
            if is_suspended:
                suspended_cnt += 1
            if is_delisting:
                delisting_period_cnt += 1

    return ExcludedSymbolDays(
        new_listing=new_listing_cnt,
        st=st_cnt,
        suspended=suspended_cnt,
        delisting_period=delisting_period_cnt,
        status_unknown=status_unknown_cnt,
        price_or_volume_invalid=price_or_volume_invalid_cnt,
    )


# ---------------------------------------------------------------------------
# 回测预检入口（Task 1 + Task 4 契约整合）
# ---------------------------------------------------------------------------
def run_backtest_precheck(
    db: Session,
    req: BacktestPrecheckRequest,
) -> BacktestPrecheckResponse:
    cfg = _cfg_from_dto(req.filter_config)
    cfg_hash = compute_config_hash(cfg)
    ok, non_fidelity_reason = validate_production_fidelity(cfg)
    production_fidelity = ok
    non_fidelity = None if ok else non_fidelity_reason
    # Task 12.1: 截止日期快照（无论成功/阻断都统一回显）
    data_cutoff, cutoff_mismatch, cutoff_warning_zh = compute_data_cutoff(db)

    # ---------------- Auto-fill symbol_ids（T1 保留） ----------------
    universe_source_error: str | None = None
    if not req.symbol_ids:
        try:
            member_stmt = (
                select(PortfolioMember.symbol_id.label("sid"))
                .where(
                    PortfolioMember.portfolio_id == req.portfolio_id,
                    PortfolioMember.status == "active",
                    PortfolioMember.effective_to.is_(None),
                )
            )
            cand_stmt = (
                select(PortfolioCandidate.symbol_id.label("sid"))
                .where(
                    PortfolioCandidate.portfolio_id == req.portfolio_id,
                    PortfolioCandidate.effective_to.is_(None),
                    PortfolioCandidate.removed_manually_flag == 0,
                )
            )
            # SQLAlchemy 2.0 的 CompoundSelect 没有 .distinct()：原先写
            # `union_all(...).distinct().subquery()` 会抛
            # AttributeError: 'CompoundSelect' object has no attribute 'distinct'，
            # 而外层 except Exception 把它吞成"0 只标的"，于是**任何不带
            # symbol_ids 的回测请求都被误阻断**，并提示用户"去同步基础数据"
            # ——一条从来没工作过的 auto-fill 路径。
            # 去重本来由下面的 set 推导完成，这里直接用 union_all 即可。
            union_subq = union_all(member_stmt, cand_stmt).subquery()
            rows = db.execute(select(union_subq.c.sid)).all()
            effective_symbol_ids: list[int] = sorted(
                {int(r[0]) for r in rows if r and r[0] is not None}
            )
        except Exception as ex:
            # 这里以前静默返回 []，于是下面直接报 NO_BACKTEST_SYMBOLS、并让用户
            # “去同步基础数据”——把“查询本身报错”伪装成“数据缺失”，跟着提示做也不会好。
            # 保持 fail-closed（仍按 0 只处理、仍要阻断），但把真实异常放进日志与
            # evidence，让诊断与提示不再跑偏。
            universe_source_error = f"{type(ex).__name__}: {ex}"
            logger.warning(
                "回测预检 auto-fill 标的查询失败，按 0 只处理：%s",
                ex,
                exc_info=True,
            )
            effective_symbol_ids = []
    else:
        effective_symbol_ids = list(dict.fromkeys(int(s) for s in req.symbol_ids))
    effective_sids = effective_symbol_ids

    # ---------------- 0. INVALID_DATE_RANGE（T1 保留，return early） ----------------
    if req.start_date > req.end_date:
        _bl: list[PrecheckBlockerItem] = [
            _blocker(
                code="INVALID_DATE_RANGE",
                severity="error",
                category="config",
                title_zh="start_date must be <= end_date",
                detail_zh=(
                    f"start_date {req.start_date.isoformat()} is after end_date "
                    f"{req.end_date.isoformat()}. Please correct the backtest range."
                ),
                evidence={
                    "start_date": req.start_date.isoformat(),
                    "end_date": req.end_date.isoformat(),
                },
                fix_link={
                    "tab": "portfolio-backtest",
                    "label_zh": "Adjust backtest date range",
                },
            )
        ]
        _ex = ExcludedSymbolDays()
        return BacktestPrecheckResponse(
            requested_trade_days=0,
            usable_trade_days=0,
            minimum_trade_days=req.minimum_trade_days,
            excluded_symbol_days=_ex,
            target_coverage=None,
            warnings=[],
            blocking_reasons=_bl,
            filter_config_hash=cfg_hash,
            production_fidelity=production_fidelity,
            non_fidelity_reason=non_fidelity,
            data_cutoff=data_cutoff,
            cutoff_mismatch=cutoff_mismatch,
            cutoff_warning_zh=cutoff_warning_zh,
        )

    minimum = req.minimum_trade_days
    warnings: list[PrecheckBlockerItem] = []
    blockers: list[PrecheckBlockerItem] = []

    # ---------------- 1. 交易日历 Adapter（T4 fail-closed） ----------------
    trade_days: list[date] | None = None
    trade_calendar_ok = False
    try:
        trade_days = get_trading_days(req.start_date, req.end_date, session=db)
        trade_calendar_ok = True
    except TradeCalendarUnavailableError:
        blockers.append(
            _blocker(
                code="TRADE_CALENDAR_UNAVAILABLE",
                severity="error",
                category="data",
                title_zh="交易日历服务不可用（fail-closed）",
                detail_zh=(
                    "交易日历服务不可用（fail-closed，已禁用 5/7 粗估）。"
                    "请检查 trade_calendar 表是否建表并填充 is_trading_day 字段，"
                    "或实现 calendar_utils.get_trading_days。"
                ),
                evidence={
                    "start_date": req.start_date.isoformat(),
                    "end_date": req.end_date.isoformat(),
                    "rejected_fallback": "5_of_7_natural_day_estimate",
                },
                fix_link={
                    "tab": "portfolio-backtest",
                    "label_zh": "补建 trade_calendar 表或对接交易日历服务",
                },
            )
        )
    except Exception as ex:
        # 任何其它非预期异常也视作 fail-closed
        blockers.append(
            _blocker(
                code="TRADE_CALENDAR_UNAVAILABLE",
                severity="error",
                category="data",
                title_zh="交易日历服务不可用（fail-closed，未知异常）",
                detail_zh=(
                    f"交易日历查询抛 {type(ex).__name__}: {ex}（fail-closed，已禁用 5/7 粗估）。"
                ),
                evidence={
                    "exception_type": type(ex).__name__,
                    "rejected_fallback": "5_of_7_natural_day_estimate",
                },
                fix_link={
                    "tab": "portfolio-backtest",
                    "label_zh": "排查交易日历查询异常",
                },
            )
        )

    # requested/usable 主路径：来自真实交易日历长度（禁止 5/7 粗估赋值）
    requested_trade_days = len(trade_days) if trade_calendar_ok else 0
    usable_before_filter = requested_trade_days
    usable_after_filter = usable_before_filter  # 若 SSD 统计成功会再扣减

    # ---------------- 2. INSUFFICIENT_TRADE_DAYS（T1 保留 severity=error） ----------------
    # 仅当交易日历可用时才检查（否则 TRADE_CALENDAR_UNAVAILABLE 已优先阻断，
    # 且 requested_trade_days=0 时不能重复加 INSUFFICIENT 的假告警）。
    if trade_calendar_ok and usable_before_filter < minimum:
        blockers.append(
            _blocker(
                code="INSUFFICIENT_TRADE_DAYS",
                severity="error",
                category="config",
                title_zh="回测区间交易日不足（阻断）",
                detail_zh=(
                    f"当前可用交易日 {usable_before_filter} 天，低于最低要求 {minimum} 天，"
                    "请扩大回测区间或降低 minimum_trade_days。"
                ),
                evidence={
                    "actual_usable": usable_before_filter,
                    "minimum_required": minimum,
                    "gap_days": minimum - usable_before_filter,
                },
                fix_link={
                    "tab": "portfolio-backtest",
                    "label_zh": "调整回测区间参数",
                },
            )
        )

    # ---------------- 3. symbol_ids 过少 → warning（保留） ----------------
    # VIZ-0929-02：0 只标的时 run 阶段必然 400（_filter_symbol_ids_by_rule 后为空），
    # 预检必须同步阻断，避免"预检放行 → 执行被拒"的不一致；1~4 只仍为 warning。
    if len(effective_sids) == 0:
        blockers.append(
            _blocker(
                code="NO_BACKTEST_SYMBOLS",
                severity="error",
                category="universe",
                title_zh="无可回测标的（阻断）",
                detail_zh=(
                    "当前范围内没有任何可回测标的（候选池为空或全部被数据新鲜度/规则过滤），"
                    "回测无法产生有效结果。请先同步行情与评分数据，或调整选股池/策略规则。"
                ),
                evidence={
                    "symbol_count": 0,
                    # 只有真的出过报错才多这个键：避免把“确实没数据”的正常场景
                    # 染上似错非错的字段。
                    **(
                        {"universe_source_error": universe_source_error}
                        if universe_source_error
                        else {}
                    ),
                },
                fix_link={
                    "tab": "settings-data-center",
                    "label_zh": "去同步基础数据",
                },
            )
        )
    elif len(effective_sids) < 5:
        warnings.append(
            _blocker(
                code="SYMBOL_POOL_TOO_SMALL",
                severity="warning",
                category="universe",
                title_zh="回测标的数量过少",
                detail_zh=(
                    f"当前仅 {len(effective_sids)} 只标的，统计显著性不足（建议 >= 20 只）。"
                ),
                evidence={"symbol_count": len(effective_sids)},
            )
        )

    # ---------------- 4. SSD 真实排除统计（T4 fail-closed） ----------------
    excluded: ExcludedSymbolDays | None = None
    ssd_available = False
    if trade_calendar_ok and trade_days is not None and effective_sids:
        try:
            excluded = _collect_security_status_daily_exclusions(
                session=db,
                symbol_ids=effective_sids,
                start_date=req.start_date,
                end_date=req.end_date,
                trade_days=trade_days,
                min_listing_age_days=cfg.min_listing_age_calendar_days,
            )
            ssd_available = True
        except StatusDataUnavailableError as ssd_ex:
            if ssd_ex.unknown_count is not None:
                excluded = ExcludedSymbolDays(status_unknown=ssd_ex.unknown_count)
            blockers.append(
                _blocker(
                    code="STATUS_DATA_UNAVAILABLE",
                    severity="error",
                    category="data",
                    title_zh="历史股票状态资料不完整",
                    detail_zh=(
                        f"{ssd_ex} 请先核对并导入原始历史资料，不能用今天的股票状态补过去。"
                    ),
                    evidence={
                        "start_date": req.start_date.isoformat(),
                        "end_date": req.end_date.isoformat(),
                        "symbol_count": len(effective_sids),
                        "detail": str(ssd_ex),
                        "missing_or_unverified": ssd_ex.unknown_count,
                    },
                    fix_link={
                        "tab": "settings-data-center",
                        "label_zh": "查看历史股票状态资料",
                    },
                )
            )
        except Exception as ex:
            blockers.append(
                _blocker(
                    code="STATUS_DATA_UNAVAILABLE",
                    severity="error",
                    category="data",
                    title_zh="证券状态日度数据查询异常（fail-closed）",
                    detail_zh=(
                        "查询 security_status_daily 抛异常（fail-closed，禁止伪装 0 基线）。"
                        f"{type(ex).__name__}: {ex}"
                    ),
                    evidence={"exception_type": type(ex).__name__},
                    fix_link={
                        "tab": "portfolio-backtest",
                        "label_zh": "排查 security_status_daily 查询异常",
                    },
                )
            )
    elif not trade_calendar_ok:
        # 交易日历不可用 → SSD 也无法覆盖校验，附 info 级说明（非 blocker，因为 trade_calendar 已阻断主链路）
        pass

    if ssd_available and excluded is not None and excluded.status_unknown and cfg.production_fidelity:
        blockers.append(_blocker(
            code="STATUS_DATA_UNAVAILABLE", severity="error", category="data",
            title_zh="部分日期的股票状态仍未知",
            detail_zh=f"仍有 {excluded.status_unknown} 个股票交易日没有可靠历史状态。请补齐后再进行正式回测。",
            evidence={"missing_or_unverified": excluded.status_unknown},
            fix_link={"tab": "settings-data-center", "label_zh": "查看历史股票状态资料"},
        ))

    # The execution service applies the same check, so API/CLI callers cannot
    # bypass it by skipping preflight or disabling optional research filters.
    if trade_calendar_ok and trade_days and effective_sids and not any(
        item.code == "STATUS_DATA_UNAVAILABLE" for item in blockers
    ):
        from app.services.security_status.backtest_guard import BacktestHistoryUnavailable, load_backtest_history
        try:
            load_backtest_history(db, effective_sids, req.start_date, req.end_date, trade_days=trade_days)
        except BacktestHistoryUnavailable as exc:
            blockers.append(_blocker(
                code=exc.code, severity="error", category="data", title_zh="历史交易资料不完整",
                detail_zh=str(exc), evidence={"missing_count": len(exc.missing), "missing_sample": exc.missing[:20]},
                fix_link={"tab": "settings-data-center", "label_zh": "查看历史股票状态资料"},
            ))
        except Exception as exc:
            logger.exception("回测历史交易资料检查失败")
            blockers.append(_blocker(
                code="HISTORICAL_EXECUTION_DATA_UNAVAILABLE", severity="error", category="data",
                title_zh="历史交易资料检查失败", detail_zh="暂时无法读取历史交易资料，请检查本机数据库后重试。",
                evidence={"exception_type": type(exc).__name__},
                fix_link={"tab": "settings-data-center", "label_zh": "查看历史股票状态资料"},
            ))

    if not ssd_available and excluded is None:
        # 仅当 SSD 真实不可用时返回 0 基线，但会加 info 标注
        excluded = ExcludedSymbolDays()
        if trade_calendar_ok and effective_sids:
            # 已经通过 STATUS_DATA_UNAVAILABLE blocker 记录了原因，这里不重复加 warning
            # 但若 SSD 是因 symbol 为空等跳过（非抛出的情况），提示 baseline
            warnings.append(
                _blocker(
                    code="EXCLUDED_STATS_ESTIMATED_BASELINE",
                    severity="info",
                    category="filter",
                    title_zh="排除统计使用基线 0 值",
                    detail_zh=(
                        "因证券状态数据不可用或标的集合为空，排除计数返回基线 0；"
                        "回测正式运行时将写入真实数据。"
                    ),
                    evidence={"using_estimated_baseline": True},
                )
            )

    # 计算 usable_after_filter（Symbol-Days 级排除总和 / 标的数 = 每标的平均排除天数）
    if ssd_available and excluded is not None and effective_sids:
        total_excluded_symbol_days = (
            excluded.new_listing
            + excluded.st
            + excluded.suspended
            + excluded.delisting_period
            + excluded.status_unknown
            + excluded.price_or_volume_invalid
        )
        avg_per_symbol = total_excluded_symbol_days / len(effective_sids)
        usable_after_filter = max(0, int(usable_before_filter - avg_per_symbol))
    else:
        usable_after_filter = usable_before_filter

    # usable_trade_days = usable_after_filter（与 minimum_trade_days 阈值对齐的每标的维度）
    usable_trade_days = usable_after_filter

    # 如果原本 usable_before_filter >= minimum 但扣掉排除后 < minimum，仍然要补 INSUFFICIENT blocker
    # （仅当交易日历可用、SSD 可用且未加过 INSUFFICIENT 时）
    if (
        trade_calendar_ok
        and ssd_available
        and usable_before_filter >= minimum
        and usable_trade_days < minimum
    ):
        blockers.append(
            _blocker(
                code="INSUFFICIENT_TRADE_DAYS",
                severity="error",
                category="config",
                title_zh="回测区间排除后交易日不足（阻断）",
                detail_zh=(
                    f"过滤排除前交易日 {usable_before_filter} 天，排除后仅剩 {usable_trade_days} 天，"
                    f"低于最低要求 {minimum} 天。请扩大区间或放松过滤规则。"
                ),
                evidence={
                    "usable_before": usable_before_filter,
                    "usable_after": usable_trade_days,
                    "minimum_required": minimum,
                    "gap_days": minimum - usable_trade_days,
                },
                fix_link={
                    "tab": "portfolio-backtest",
                    "label_zh": "调整回测区间或放松排除过滤",
                },
            )
        )

    return BacktestPrecheckResponse(
        requested_trade_days=requested_trade_days,
        usable_trade_days=usable_trade_days,
        minimum_trade_days=minimum,
        symbols=[{"id": symbol.id, "symbol": symbol.symbol, "name": symbol.name}
                 for symbol in db.scalars(select(Symbol).where(Symbol.id.in_(effective_sids)).order_by(Symbol.id))],
        excluded_symbol_days=excluded,
        target_coverage=None,
        warnings=warnings,
        blocking_reasons=blockers,
        filter_config_hash=cfg_hash,
        production_fidelity=production_fidelity,
        non_fidelity_reason=non_fidelity,
        data_cutoff=data_cutoff,
        cutoff_mismatch=cutoff_mismatch,
        cutoff_warning_zh=cutoff_warning_zh,
    )


# ---------------------------------------------------------------------------
# 因子评价预检（暂保留 5/7 估算，后续单独 Task 改造）
# ---------------------------------------------------------------------------
def run_factor_eval_precheck(
    db: Session,
    req: FactorEvaluationPrecheckRequest,
) -> FactorEvaluationPrecheckResponse:
    cfg = _cfg_from_dto(req.filter_config)
    cfg_hash = compute_config_hash(cfg)
    ok, non_fidelity_reason = validate_production_fidelity(cfg)
    production_fidelity = ok
    non_fidelity = None if ok else non_fidelity_reason

    requested = _estimate_trade_days(req.start_date, req.end_date)
    minimum = req.minimum_trade_days

    # 目标覆盖：target_horizon 天后收益会缺失最后 N 天，估算 covered = requested - target_horizon（下界）
    covered_days = max(0, requested - int(req.target_horizon))
    usable = covered_days

    warnings: list[PrecheckBlockerItem] = []
    blockers: list[PrecheckBlockerItem] = []

    if usable < minimum:
        blockers.append(
            _blocker(
                code="INSUFFICIENT_TARGET_COVERAGE",
                severity="error",
                category="target",
                title_zh="目标标签覆盖不足（低于最小交易日要求）",
                detail_zh=(
                    f"估算目标覆盖 {covered_days} 天（requested {requested} 减去 "
                    f"horizon {req.target_horizon} 天），低于 {minimum} 天最低要求。"
                ),
                evidence={
                    "actual": covered_days,
                    "minimum_required": minimum,
                    "gap_days": minimum - covered_days,
                    "requested_days": requested,
                    "target_horizon": req.target_horizon,
                },
                fix_link={
                    "tab": "factors",
                    "subtab": "evaluation",
                    "label_zh": "扩大评价区间或降低前瞻收益天数",
                },
            )
        )
    elif covered_days < (requested - int(req.target_horizon) * 2):
        # 严重缺口 warning
        warnings.append(
            _blocker(
                code="TARGET_COVERAGE_GAP",
                severity="warning",
                category="target",
                title_zh="目标标签覆盖存在缺口",
                detail_zh=(
                    f"目标覆盖 {covered_days} 天 低于预期，部分交易日样本不参与 IC/分组统计。"
                ),
                evidence={"covered": covered_days, "requested": requested},
            )
        )

    # 目标覆盖缺口百分比 < 80% -> warning
    if requested > 0 and covered_days / requested < 0.8:
        warnings.append(
            _blocker(
                code="TARGET_COVERAGE_RATIO_LOW",
                severity="warning",
                category="target",
                title_zh="目标覆盖率 < 80%",
                detail_zh=(
                    f"覆盖率 {covered_days}/{requested} = "
                    f"{covered_days / max(1, requested):.1%}，可能影响指标稳定性。"
                ),
                evidence={"ratio": covered_days / max(1, requested)},
            )
        )

    excluded = ExcludedSymbolDays()

    return FactorEvaluationPrecheckResponse(
        requested_trade_days=requested,
        usable_trade_days=usable,
        minimum_trade_days=minimum,
        excluded_symbol_days=excluded,
        target_coverage=TargetCoverageInfo(
            covered_days=covered_days, requested_days=requested
        ),
        warnings=warnings,
        blocking_reasons=blockers,
        filter_config_hash=cfg_hash,
        production_fidelity=production_fidelity,
        non_fidelity_reason=non_fidelity,
    )
