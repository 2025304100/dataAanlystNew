"""G1-WP0-3b：Score 精确查询服务 + 严格 PIT 契约。

Q22 强制规则：
  factor_model_run_id = bound_id       # 必须绑定 exact；禁止按 weight_mode/target_code 推导
  AND trade_date <= decision_date     # 绝不用未来 Score
  AND factor_data_cutoff_at <= data_cutoff_at     # 截止时间双重校验
  每 symbol_id 取最新一条（ROW_NUMBER() OVER partition by symbol order by ... desc）
  published_at 必须存在且 <= data_cutoff_at 才算 PIT_SAFE（否则 NOT_PIT_SAFE）

新增联合索引（SQL 侧由 G0-WP0-2b 迁移脚本提供）：
  idx_score_fmr_sym_dt ON scores(factor_model_run_id, symbol_id, trade_date)
"""
from __future__ import annotations

import logging
from bisect import bisect_left
from dataclasses import dataclass, field as dc_field
from datetime import date, datetime
from typing import Any

import json as _json

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.models.decision_engine import StrategyExecutionSnapshot
from app.models.index_price import IndexPrice
from app.models.portfolio import Portfolio
from app.models.score import Score

logger = logging.getLogger(__name__)


@dataclass
class ScoreRow:
    """规范化输出行，隐藏列名差异。"""
    symbol_id: int
    score_id: int
    score_value: float  # 取 model_alpha_score，兜底 factor_quality_score*0.5 + factor_timing_score*0.5
    score_rank: int | None
    trade_date: date
    factor_data_cutoff_at: datetime | None
    published_at: datetime | None
    pit_safe_flag: str  # PIT_SAFE / NOT_PIT_SAFE
    factor_contributions: dict[str, Any] = dc_field(default_factory=dict)


def fetch_latest_pit_scores(
    db: Session,
    *,
    factor_model_run_id: str,
    symbol_ids: list[int],
    decision_date: date,
    data_cutoff_at: datetime,
    factor_set_id: str | None = None,
    require_published_at: bool = False,
) -> list[ScoreRow]:
    """Q22 精确查询入口。

    symbol_ids 空数组直接返回空（避免全表扫）。
    require_published_at=True：严格 PIT 模式；published_at NULL 的行被过滤（NOT_PIT_SAFE）。
    """
    if not symbol_ids:
        return []
    if not factor_model_run_id:
        raise ValueError("factor_model_run_id 必须显式绑定（Q21方案A/Q22精确契约）")

    # ── 窗口函数：每 symbol 取符合时间边界的最新一条
    # 严格模式必须在窗口计算前过滤披露时间。若先选未来修订再过滤，较早但
    # 已经可见的修订会被错误丢弃。
    where = [
        Score.factor_model_run_id == factor_model_run_id,
        Score.symbol_id.in_(symbol_ids),
        Score.trade_date <= decision_date,
        or_(
            Score.factor_data_cutoff_at.is_(None),
            Score.factor_data_cutoff_at <= data_cutoff_at,
        ),
    ]
    if factor_set_id:
        where.append(Score.factor_set_id == factor_set_id)
    if require_published_at:
        where.extend([
            Score.published_at.is_not(None),
            Score.published_at <= data_cutoff_at,
        ])
    # 排序优先级：trade_date DESC, factor_data_cutoff_at DESC, id DESC
    subq = (
        select(
            Score,
            func.row_number().over(
                partition_by=Score.symbol_id,
                order_by=(
                    Score.trade_date.desc(),
                    Score.factor_data_cutoff_at.desc().nullslast(),
                    Score.id.desc(),
                ),
            ).label("rn"),
        )
        .where(and_(*where))
        .subquery()
    )
    stmt = select(subq).where(subq.c.rn == 1)
    rows = db.execute(stmt).all()

    results: list[ScoreRow] = []
    import json as _json
    for r in rows:
        raw_published_at = getattr(r, "published_at", None)
        pit_flag = "NOT_PIT_SAFE"
        if raw_published_at is not None:
            pit_flag = "PIT_SAFE" if raw_published_at <= data_cutoff_at else "NOT_PIT_SAFE"
        # Q4.2：严格 PIT 模式下，published_at 为 NULL 或晚于 cutoff 均剔除
        if require_published_at:
            if raw_published_at is None:
                continue
            if raw_published_at > data_cutoff_at:
                # 未来披露：NOT_PIT_SAFE，正式 PIT 必须阻断 (Q4.2 / WP0-6a 反例)
                continue
        # 分值提取：model_alpha_score 优先（真实模型输出），否则组合股质+时点
        value: float | None = getattr(r, "model_alpha_score", None)
        if value is None:
            fq = getattr(r, "factor_quality_score", None) or 0.0
            ft = getattr(r, "factor_timing_score", None) or 0.0
            value = fq * 0.5 + ft * 0.5
        # 因子贡献：优先 factor_scores_json，其次 dimension_scores_json
        contribs: dict[str, Any] = {}
        for col_name in ("factor_scores_json", "dimension_scores_json"):
            raw = getattr(r, col_name, None)
            if raw:
                try:
                    contribs = _json.loads(raw) if isinstance(raw, str) else dict(raw)
                    if contribs:
                        break
                except Exception:
                    continue
        results.append(ScoreRow(
            symbol_id=int(r.symbol_id),
            score_id=int(r.id),
            score_value=float(value or 0.0),
            score_rank=None,  # rank 后处理
            trade_date=r.trade_date,
            factor_data_cutoff_at=r.factor_data_cutoff_at,
            published_at=raw_published_at,
            pit_safe_flag=pit_flag,
            factor_contributions=contribs,
        ))
    # ── 计算 rank（1 = 最高 score_value）
    results.sort(key=lambda x: x.score_value, reverse=True)
    for idx, row in enumerate(results, start=1):
        row.score_rank = idx
    return results


def estimate_score_coverage(
    db: Session,
    *,
    factor_model_run_id: str,
    symbol_ids: list[int],
    decision_date: date,
    data_cutoff_at: datetime,
    factor_set_id: str | None = None,
    require_published_at: bool = False,
) -> tuple[int, int, int | None]:
    """Q5：覆盖率计算（expected=len(symbol_ids), actual=存在 Score 的数量, max_age_days）。

    与 fetch_latest_pit_scores 使用完全相同的 WHERE 契约，保证统计一致。
    ``require_published_at=True`` 与严格 PIT 查询保持同一口径：只有 cutoff
    前已发布的版本才计入覆盖率。
    """
    expected = len(symbol_ids)
    if expected == 0 or not factor_model_run_id:
        return expected, 0, None
    where = [
        Score.factor_model_run_id == factor_model_run_id,
        Score.symbol_id.in_(symbol_ids),
        Score.trade_date <= decision_date,
        or_(Score.factor_data_cutoff_at.is_(None),
            Score.factor_data_cutoff_at <= data_cutoff_at),
    ]
    if factor_set_id:
        where.append(Score.factor_set_id == factor_set_id)
    if require_published_at:
        where.extend([
            Score.published_at.is_not(None),
            Score.published_at <= data_cutoff_at,
        ])
    # 取每个 symbol 最新一条的 trade_date (最大日期) 与 cutoff 相比估算 age
    subq = (
        select(
            Score.symbol_id.label("sym"),
            func.max(Score.trade_date).label("max_td"),
        )
        .where(and_(*where))
        .group_by(Score.symbol_id)
        .subquery()
    )
    actual_rows = db.execute(select(subq)).all()
    actual = len(actual_rows)
    max_age_days: int | None = None
    if actual_rows:
        import datetime as _dt
        ages = [(decision_date - r.max_td).days for r in actual_rows if r.max_td]
        if ages:
            max_age_days = max(ages)
    return expected, actual, max_age_days


# ──────────────────────────────────────────────────────────── DecisionEngine Scorer 适配器
def make_decision_scorer(
    factor_model_run_id_override: str | None = None,
    require_published_at: bool = False,
):
    """返回可直接注入 DecisionEngine 的 Scorer 闭包。"""
    from app.services.decision_engine import (
        DataHealthChecker, GateChecker, LoadedSnapshot, ResolvedClock,
        ScoredUniverse, UniverseAndEligibility, date, datetime,
    )

    def _scorer(
        db: Session,
        snap: LoadedSnapshot,
        universe: UniverseAndEligibility,
        cutoff_utc: datetime,
        decision_date: date,
    ) -> ScoredUniverse:
        fmr_id = factor_model_run_id_override or snap.factor_model_run_id
        sym_ids = [m.get("symbol_id") for m in universe.universe if m.get("symbol_id")]
        sym_ids = [s for s in sym_ids if isinstance(s, int)]
        expected, actual, max_age = estimate_score_coverage(
            db, factor_model_run_id=fmr_id, symbol_ids=sym_ids,
            decision_date=decision_date, data_cutoff_at=cutoff_utc,
            factor_set_id=snap.factor_set_id,
            require_published_at=require_published_at,
        )
        coverage = (actual / expected * 100.0) if expected > 0 else 100.0
        rows = fetch_latest_pit_scores(
            db, factor_model_run_id=fmr_id, symbol_ids=sym_ids,
            decision_date=decision_date, data_cutoff_at=cutoff_utc,
            factor_set_id=snap.factor_set_id,
            require_published_at=require_published_at,
        )
        actual_ids = {row.symbol_id for row in rows}
        missing = [s for s in sym_ids if s not in actual_ids]
        stale = [row.symbol_id for row in rows if row.pit_safe_flag != "PIT_SAFE"]
        return ScoredUniverse(
            items=[
                {
                    "symbol_id": row.symbol_id,
                    "score_id": row.score_id,
                    "score_value": row.score_value,
                    "score_rank": row.score_rank,
                    "trade_date": row.trade_date,
                    "factor_data_cutoff_at": row.factor_data_cutoff_at,
                    "published_at": row.published_at,
                    "pit_safe_flag": row.pit_safe_flag,
                    "factor_contributions": row.factor_contributions,
                }
                for row in rows
            ],
            expected=expected,
            actual=actual,
            coverage_pct=coverage,
            max_age_days=max_age,
            stale_score_symbols=stale,
            missing_symbols=missing,
        )

    return _scorer


def _classify_level(rate: float) -> str:
    if rate >= 0.95:
        return "PASS"
    elif rate >= 0.92:
        return "WARN"
    else:
        return "FAIL"


def estimate_score_coverage_window(
    db: Session,
    *,
    portfolio_id: int,
    start_date: date,
    end_date: date,
    bound_factor_model_run_id: str,
    data_cutoff_at: datetime | None = None,
    explicit_member_ids: list[int] | None = None,
) -> dict:
    """Q5.1 覆盖率按"组合成员 × 交易日"计算。

    Returns {
        # 分母
        "member_count_day_count_expected_denominator": int,   # = len(member_ids) × len(trade_dates_window)
        "member_count_from_snapshot": int,                   # 成员数，要求来自 snapshot，不是当前表
        "trade_date_count_in_window": int,                   # 交易日数（实际可交易日，不是自然日！）
        # 分子
        "score_rows_found_numerator": int,                   # Score 记录数
        # 指标
        "coverage_rate": float,                              # numerator / denominator
        "coverage_level": "PASS" | "WARN" | "FAIL",          # 3 档
        # 辅助
        "member_ids_used": list[int],
        "trade_dates_used": list[date],
        # 每只成员单独的覆盖率
        "per_member_coverage": list[dict],
    }
    """
    import json as _json_internal

    # ── 2.1 成员列表来源（PIT 要求） ──
    member_ids: list[int] = []
    member_count_from_snapshot: int = 0
    if explicit_member_ids is not None:
        member_ids = list(explicit_member_ids)
        member_count_from_snapshot = len(member_ids)
    else:
        # 从 StrategyExecutionSnapshot 查 portfolio_id 且 created_at 最接近 start_date 的 1 条
        snap_stmt = (
            select(StrategyExecutionSnapshot)
            .where(StrategyExecutionSnapshot.portfolio_id == portfolio_id)
            .order_by(
                func.abs(
                    func.julianday(StrategyExecutionSnapshot.created_at)
                    - func.julianday(func.datetime(start_date.isoformat()))
                )
            )
            .limit(1)
        )
        snap = db.execute(snap_stmt).scalar_one_or_none()
        if snap is None:
            logger.warning(
                f"[estimate_score_coverage_window] portfolio_id={portfolio_id} 未找到 StrategyExecutionSnapshot，"
                f"start_date={start_date}；成员数视为 0"
            )
            member_ids = []
        else:
            try:
                raw_members = _json_internal.loads(snap.member_snapshot_json)
            except Exception:
                raw_members = []
            extracted = []
            for m in raw_members:
                if isinstance(m, dict):
                    sid = m.get("symbol_id")
                    if isinstance(sid, int):
                        extracted.append(sid)
            member_ids = extracted
        member_count_from_snapshot = len(member_ids)

    # ── 2.2 交易日窗口（从 IndexPrice 沪深300 查） ──
    td_stmt = (
        select(IndexPrice.trade_date)
        .where(
            IndexPrice.symbol == "000300",
            IndexPrice.trade_date >= start_date,
            IndexPrice.trade_date <= end_date,
        )
        .order_by(IndexPrice.trade_date)
    )
    trade_dates_rows = db.execute(td_stmt).all()
    trade_dates_window: list[date] = [r.trade_date for r in trade_dates_rows if r.trade_date]
    trade_date_count_in_window = len(trade_dates_window)

    # ── 分母 ──
    denominator = len(member_ids) * trade_date_count_in_window

    # ── 2.3 Score 查询（PIT 严格模式） ──
    numerator = 0
    per_member_actual: dict[int, int] = {mid: 0 for mid in member_ids}
    if member_ids and trade_dates_window and bound_factor_model_run_id:
        score_where = [
            Score.factor_model_run_id == bound_factor_model_run_id,
            Score.symbol_id.in_(member_ids),
            Score.trade_date >= start_date,
            Score.trade_date <= end_date,
        ]
        if data_cutoff_at is not None:
            score_where.append(
                or_(
                    Score.factor_data_cutoff_at.is_(None),
                    Score.factor_data_cutoff_at <= data_cutoff_at,
                )
            )
        subq = (
            select(Score.trade_date, Score.symbol_id)
            .where(and_(*score_where))
            .group_by(Score.trade_date, Score.symbol_id)
            .subquery()
        )
        found_rows = db.execute(select(subq)).all()
        numerator = len(found_rows)
        for r in found_rows:
            sid = int(r.symbol_id)
            if sid in per_member_actual:
                per_member_actual[sid] += 1

    # ── 2.4 & 2.6 计算 coverage_rate ──
    if denominator == 0:
        coverage_rate = 1.0
        logger.warning(
            f"[estimate_score_coverage_window] 分母=0 (members={len(member_ids)}, trade_days={trade_date_count_in_window})，"
            f"coverage_rate 视为 1.0"
        )
    else:
        coverage_rate = round(numerator / max(denominator, 1), 9)

    coverage_level = _classify_level(coverage_rate)

    # ── per_member_coverage ──
    per_member_coverage: list[dict] = []
    for mid in member_ids:
        expected_days = trade_date_count_in_window
        actual_days = per_member_actual.get(mid, 0)
        if expected_days == 0:
            m_rate = 1.0
        else:
            m_rate = round(actual_days / max(expected_days, 1), 9)
        per_member_coverage.append({
            "symbol_id": mid,
            "expected_days": expected_days,
            "actual_days": actual_days,
            "rate": m_rate,
            "level": _classify_level(m_rate),
        })

    return {
        "member_count_day_count_expected_denominator": denominator,
        "member_count_from_snapshot": member_count_from_snapshot,
        "trade_date_count_in_window": trade_date_count_in_window,
        "score_rows_found_numerator": numerator,
        "coverage_rate": coverage_rate,
        "coverage_level": coverage_level,
        "member_ids_used": list(member_ids),
        "trade_dates_used": list(trade_dates_window),
        "per_member_coverage": per_member_coverage,
    }


SLA_THRESHOLD_TRADE_DAYS = 1
ENUM_READY = 1
ENUM_SCORE_STALE = 3


def _ensure_utc_naive(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt
    try:
        from app.services.decision_clock import shanghai_to_utc_naive
    except Exception:
        shanghai_to_utc_naive = None
    try:
        from zoneinfo import ZoneInfo
    except Exception:
        ZoneInfo = None
    try:
        import pytz
    except Exception:
        pytz = None
    if shanghai_to_utc_naive is not None:
        try:
            return shanghai_to_utc_naive(dt)
        except Exception:
            pass
    from datetime import timezone as tz
    utc_aware = dt.astimezone(tz.utc)
    return utc_aware.replace(tzinfo=None)


def score_staleness_check(
    db: Session,
    *,
    bound_factor_model_run_id: str,
    decision_at: datetime,
    portfolio_id: int | None = None,
) -> dict:
    decision_at_utc_naive = _ensure_utc_naive(decision_at)
    decision_date = decision_at_utc_naive.date()

    td_stmt = (
        select(IndexPrice.trade_date)
        .where(
            IndexPrice.symbol == "000300",
            IndexPrice.trade_date < decision_date,
        )
        .order_by(IndexPrice.trade_date.desc())
    )
    td_rows = db.execute(td_stmt).all()
    trade_dates_desc: list[date] = [r.trade_date for r in td_rows if r.trade_date]
    trade_dates_sorted_asc: list[date] = list(reversed(trade_dates_desc))

    latest_trade_date_before_decision: date | None = trade_dates_desc[0] if trade_dates_desc else None
    preceding_trade_date_before_decision: date | None = (
        trade_dates_desc[1] if len(trade_dates_desc) >= 2 else None
    )

    cutoff_stmt = (
        select(func.max(Score.factor_data_cutoff_at))
        .where(Score.factor_model_run_id == bound_factor_model_run_id)
    )
    latest_cutoff_at: datetime | None = db.execute(cutoff_stmt).scalar_one_or_none()

    trade_day_gap: int
    if latest_cutoff_at is None or latest_trade_date_before_decision is None:
        trade_day_gap = 999
    else:
        latest_cutoff_date = latest_cutoff_at.date()
        if latest_cutoff_date >= latest_trade_date_before_decision:
            trade_day_gap = 0
        elif not trade_dates_sorted_asc:
            trade_day_gap = 999
        else:
            cutoff_idx = bisect_left(trade_dates_sorted_asc, latest_cutoff_date)
            if cutoff_idx >= len(trade_dates_sorted_asc):
                cutoff_idx = len(trade_dates_sorted_asc) - 1
            if trade_dates_sorted_asc[cutoff_idx] > latest_cutoff_date and cutoff_idx > 0:
                cutoff_idx -= 1
            latest_idx = len(trade_dates_sorted_asc) - 1
            trade_day_gap = latest_idx - cutoff_idx

    level = "PASS" if trade_day_gap <= SLA_THRESHOLD_TRADE_DAYS else "STALE"
    portfolio_score_status_enum_3 = ENUM_READY if level == "PASS" else ENUM_SCORE_STALE

    _ = portfolio_id

    return {
        "level": level,
        "latest_score_factor_data_cutoff_at": latest_cutoff_at,
        "latest_trade_date_before_decision": latest_trade_date_before_decision,
        "preceding_trade_date_before_decision": preceding_trade_date_before_decision,
        "trade_day_gap": trade_day_gap,
        "decision_at_utc_naive": decision_at_utc_naive,
        "sla_threshold_trade_days": SLA_THRESHOLD_TRADE_DAYS,
        "portfolio_score_status_enum_3": portfolio_score_status_enum_3,
    }


def _parse_key_members_from_json(raw: Any) -> list[int] | None:
    """解析 key_members_json（可能是 str 或 list 或 None）→ list[int] | None。"""
    if raw is None:
        return None
    if isinstance(raw, list):
        return [int(x) for x in raw if isinstance(x, int)]
    if isinstance(raw, str):
        if not raw.strip():
            return None
        try:
            parsed = _json.loads(raw)
            if isinstance(parsed, list):
                return [int(x) for x in parsed if isinstance(x, int)]
        except Exception:
            return None
    return None


def _resolve_effective_key_members(
    *,
    strategy_snapshot_key_members_json: list[int] | None = None,
    portfolio_key_members_json: list[int] | None = None,
    default_member_ids: list[int] | None = None,
) -> list[int]:
    """按优先级解析关键成员：snapshot > portfolio > 默认(全组合)。"""
    # 优先级 1：snapshot 级
    if strategy_snapshot_key_members_json is not None:
        snap = strategy_snapshot_key_members_json
        if isinstance(snap, list) and len(snap) > 0:
            return [int(x) for x in snap if isinstance(x, int)]
    # 优先级 2：portfolio 级
    if portfolio_key_members_json is not None:
        pf = portfolio_key_members_json
        if isinstance(pf, list) and len(pf) > 0:
            return [int(x) for x in pf if isinstance(x, int)]
    # 优先级 3：默认全组合关键
    if default_member_ids is not None:
        return list(default_member_ids)
    return []


def _fetch_trade_dates_window(
    db: Session,
    start_date: date,
    end_date: date,
) -> list[date]:
    """与 estimate_score_coverage_window 相同的交易日取法（沪深300 index_price）。"""
    td_stmt = (
        select(IndexPrice.trade_date)
        .where(
            IndexPrice.symbol == "000300",
            IndexPrice.trade_date >= start_date,
            IndexPrice.trade_date <= end_date,
        )
        .order_by(IndexPrice.trade_date)
    )
    rows = db.execute(td_stmt).all()
    return [r.trade_date for r in rows if r.trade_date]


def _fetch_member_ids_from_snapshot(
    db: Session,
    portfolio_id: int,
    start_date: date,
) -> list[int]:
    """与 estimate_score_coverage_window 相同的 member_ids_used 取法。"""
    import json as _json_internal
    snap_stmt = (
        select(StrategyExecutionSnapshot)
        .where(StrategyExecutionSnapshot.portfolio_id == portfolio_id)
        .order_by(
            func.abs(
                func.julianday(StrategyExecutionSnapshot.created_at)
                - func.julianday(func.datetime(start_date.isoformat()))
            )
        )
        .limit(1)
    )
    snap = db.execute(snap_stmt).scalar_one_or_none()
    if snap is None:
        return []
    try:
        raw_members = _json_internal.loads(snap.member_snapshot_json)
    except Exception:
        return []
    extracted = []
    for m in raw_members:
        if isinstance(m, dict):
            sid = m.get("symbol_id")
            if isinstance(sid, int):
                extracted.append(sid)
    return extracted


def _fetch_portfolio_key_members(db: Session, portfolio_id: int) -> list[int] | None:
    stmt = select(Portfolio).where(Portfolio.id == portfolio_id).limit(1)
    pf = db.execute(stmt).scalar_one_or_none()
    if pf is None:
        return None
    return _parse_key_members_from_json(getattr(pf, "key_members_json", None))


def _fetch_snapshot_key_members(
    db: Session,
    portfolio_id: int,
    start_date: date,
) -> list[int] | None:
    """从最近 snapshot 取 key_members_json（若存在）。"""
    snap_stmt = (
        select(StrategyExecutionSnapshot)
        .where(StrategyExecutionSnapshot.portfolio_id == portfolio_id)
        .order_by(
            func.abs(
                func.julianday(StrategyExecutionSnapshot.created_at)
                - func.julianday(func.datetime(start_date.isoformat()))
            )
        )
        .limit(1)
    )
    snap = db.execute(snap_stmt).scalar_one_or_none()
    if snap is None:
        return None
    return _parse_key_members_from_json(getattr(snap, "key_members_json", None))


def _format_date_list(dates: list[date], max_show: int = 10) -> str:
    if not dates:
        return ""
    sorted_dates = sorted(dates)
    iso = [d.isoformat() for d in sorted_dates]
    if len(iso) <= max_show:
        return ",".join(iso)
    return ",".join(iso[:max_show]) + f"...(+{len(iso) - max_show})"


def check_key_member_score_coverage(
    db: Session,
    *,
    portfolio_id: int,
    start_date: date,
    end_date: date,
    bound_factor_model_run_id: str,
    strategy_snapshot_key_members_json: list[int] | None = None,
    portfolio_key_members_json: list[int] | None = None,
    data_cutoff_at: datetime | None = None,
) -> dict:
    """Q5.3 关键成员 Score 门禁。

    优先级：snapshot 级 > portfolio 级；都空则 key_members = estimate_score_coverage_window
    返回的 member_ids_used（默认全组合关键）。
    """
    import json as _json_internal

    # ── 1) 解 key_members ──
    # 1.1 显式传入参数优先（用于 preflight / test）
    snap_explicit = _parse_key_members_from_json(strategy_snapshot_key_members_json)
    pf_explicit = _parse_key_members_from_json(portfolio_key_members_json)
    # 1.2 未显式传时 → 从 DB 查最近 snapshot + portfolio
    snap_km: list[int] | None = snap_explicit
    if snap_km is None:
        snap_km = _fetch_snapshot_key_members(db, portfolio_id, start_date)
    pf_km: list[int] | None = pf_explicit
    if pf_km is None:
        pf_km = _fetch_portfolio_key_members(db, portfolio_id)

    # 1.3 默认成员列表（全组合关键 fallback）
    default_member_ids = _fetch_member_ids_from_snapshot(db, portfolio_id, start_date)

    # 1.4 按优先级生效
    key_members_effective: list[int] = _resolve_effective_key_members(
        strategy_snapshot_key_members_json=snap_km,
        portfolio_key_members_json=pf_km,
        default_member_ids=default_member_ids,
    )

    # ── 2) 交易日窗口 ──
    trade_dates_window: list[date] = _fetch_trade_dates_window(db, start_date, end_date)

    # ── 3) Score 查询：按 (symbol_id, trade_date) 去重后得到有 Score 的对 ──
    found_set: set[tuple[int, date]] = set()
    if key_members_effective and trade_dates_window and bound_factor_model_run_id:
        score_where = [
            Score.factor_model_run_id == bound_factor_model_run_id,
            Score.symbol_id.in_(key_members_effective),
            Score.trade_date >= start_date,
            Score.trade_date <= end_date,
        ]
        if data_cutoff_at is not None:
            score_where.append(
                or_(
                    Score.factor_data_cutoff_at.is_(None),
                    Score.factor_data_cutoff_at <= data_cutoff_at,
                )
            )
        subq = (
            select(Score.trade_date, Score.symbol_id)
            .where(and_(*score_where))
            .group_by(Score.trade_date, Score.symbol_id)
            .subquery()
        )
        found_rows = db.execute(select(subq)).all()
        for r in found_rows:
            found_set.add((int(r.symbol_id), r.trade_date))

    # ── 4) 每 key_member 算缺的日期 ──
    per_key_member_missing_detail: list[dict] = []
    actual_found_count = 0
    for mid in key_members_effective:
        missing_dates = []
        for td in trade_dates_window:
            if (mid, td) in found_set:
                actual_found_count += 1
            else:
                missing_dates.append(td)
        per_key_member_missing_detail.append({
            "symbol_id": mid,
            "missing_dates": list(missing_dates),
            "missing_count": len(missing_dates),
        })

    # ── 5) 判定 level / block_code / block_reason ──
    any_missing = any(d["missing_count"] > 0 for d in per_key_member_missing_detail)
    level: str = "REJECTED" if any_missing else "PASS"
    block_code: str | None = "KEY_MEMBER_SCORE_MISSING" if any_missing else None
    block_reason: str | None = None

    if any_missing:
        missing_entries = [d for d in per_key_member_missing_detail if d["missing_count"] > 0]
        missing_entries.sort(key=lambda x: (-x["missing_count"], x["symbol_id"]))
        parts = []
        for entry in missing_entries[:5]:
            sid = entry["symbol_id"]
            mc = entry["missing_count"]
            dates_str = _format_date_list(entry["missing_dates"], max_show=5)
            parts.append(f"symbol_id={sid}, 缺 {mc} 交易日({dates_str})")
        if len(missing_entries) > 5:
            parts.append(f"...(其余 {len(missing_entries) - 5} 只省略)")
        block_reason = "关键成员缺 Score: [" + "; ".join(parts) + "]"

    total_expected = len(key_members_effective) * len(trade_dates_window)

    return {
        "level": level,
        "block_code": block_code,
        "block_reason": block_reason,
        "key_members_effective": list(key_members_effective),
        "key_members_total_expected_coverage_count": total_expected,
        "key_members_actual_found_count": actual_found_count,
        "per_key_member_missing_detail": per_key_member_missing_detail,
    }
