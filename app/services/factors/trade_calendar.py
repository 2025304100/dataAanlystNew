"""完整交易日判定算法（WPD-02）。

只读模块，不修改 schema，不依赖 EvaluationRun 表，不依赖 DuckDB。
基于 MySQL ``universe_daily_bars`` JOIN ``universe_symbols`` 的横截面快照，
用过去 20 个完整交易日中位数作为基准，判定当日是否完整。

设计要点：
- 不使用 ``MAX(trade_date)`` 直接决定评估截止日；``MAX(trade_date)`` 仅用于
  确定回溯窗口的锚点，最终截止日由完整性阈值决定。
- 基准中位数排除当前评估的候选日，避免残缺日（如 150/218 标的）污染基准。
- 返回结构化证据对象，供后续 EvaluationRun 持久化使用。
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.universe import UniverseDailyBar, UniverseSymbol

EPOCH_DATE = date(1970, 1, 1)


@dataclass(frozen=True)
class CompleteTradeDayEvidence:
    """完整交易日判定证据，对应 EvaluationRun 的 5 个核心字段加调试信息。"""

    selected_trade_date: date
    observed_symbols: int  # 实际观测到的标的数（评估目标日）
    expected_symbols: int  # 基准预期标的数（中位数）
    completeness_ratio: float  # observed / expected，四舍五入 6 位
    fallback_reason: str | None  # None 表示目标日完整；否则说明为何回退
    median_baseline: int  # 过去 20 个完整交易日中位数（调试用）
    evaluated_candidate_dates: list[date] = field(default_factory=list)
    candidate_ratios: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class TradeDaySnapshot:
    """单个交易日的横截面快照。"""

    trade_date: date
    symbol_count: int


def _symbol_filters(
    *, asset_type: str, region: str, require_synced: bool
) -> list:
    filters = [
        UniverseSymbol.asset_type == asset_type,
        UniverseSymbol.region == region,
    ]
    if require_synced:
        filters.append(UniverseSymbol.is_synced == 1)
    return filters


def fetch_recent_trade_day_snapshots(
    db_session: Session,
    *,
    asset_type: str = "stock",
    region: str = "cn",
    require_synced: bool = True,
    lookback_days: int = 30,
    max_snapshots: int = 25,
) -> list[TradeDaySnapshot]:
    """从 universe_daily_bars 查询最近 N 天的横截面快照。

    过滤条件：asset_type='stock' AND region='cn'（is_synced=1 当 require_synced=True）。
    返回按 trade_date DESC 排序的列表。回溯窗口锚定到过滤后数据的 MAX(trade_date)，
    使算法对历史数据可重复运行，不受执行时刻影响。
    """
    sym_filters = _symbol_filters(
        asset_type=asset_type, region=region, require_synced=require_synced
    )

    anchor_stmt = (
        select(func.max(UniverseDailyBar.trade_date))
    )
    # 锚点只用于定位回溯窗口的起点，因此**刻意不 JOIN universe_symbols**：
    # 一旦带上 JOIN，MySQL 要为 3560 只标的逐一回查 K 线（实测 3.9s），而不带
    # JOIN 时 `ix_universe_bar_trade_date` 反向扫描第一行即可（实测 0.001s）。
    # 窗口位置最多差一两天，对「按横截面完整度选交易日」没有实质影响。
    anchor = db_session.execute(anchor_stmt).scalar()
    if anchor is None:
        return []

    cutoff = anchor - timedelta(days=lookback_days)

    stmt = (
        select(
            UniverseDailyBar.trade_date,
            func.count(UniverseDailyBar.universe_symbol_id.distinct()).label(
                "symbol_count"
            ),
        )
        # MySQL 默认会从 universe_symbols 侧驱动、逐标的回查 K 线（实测约 193 万次
        # 索引查找 / 4.4s）。显式提示走 `ix_universe_bar_trade_date` 的日期区间扫描
        # 之后降到 0.19s，结果集完全一致（同一 22 行、同样的 symbol_count）。
        # 该索引由 wps_0023_065 迁移建立；SQLite（测试库）不支持此提示，会自动忽略。
        .with_hint(
            UniverseDailyBar,
            "USE INDEX (ix_universe_bar_trade_date)",
            "mysql",
        )
        .join(
            UniverseSymbol,
            UniverseSymbol.id == UniverseDailyBar.universe_symbol_id,
        )
        .where(UniverseDailyBar.trade_date >= cutoff, *sym_filters)
        .group_by(UniverseDailyBar.trade_date)
        .order_by(UniverseDailyBar.trade_date.desc())
        .limit(max_snapshots)
    )
    rows = db_session.execute(stmt).all()
    return [
        TradeDaySnapshot(trade_date=row[0], symbol_count=int(row[1]))
        for row in rows
    ]


def compute_median_baseline(
    snapshots: Sequence[TradeDaySnapshot],
    *,
    sample_size: int = 20,
) -> int:
    """从最近 N 天的快照中取前 20 个交易日的 symbol_count 中位数。

    如果快照不足 20 个，则用全部可用快照的中位数。
    返回整数中位数（向上取整）。
    """
    counts = [snap.symbol_count for snap in snapshots[:sample_size]]
    if not counts:
        return 0
    return math.ceil(statistics.median(counts))


def _ratio(observed: int, expected: int) -> float:
    if expected <= 0:
        return 0.0
    return round(observed / expected, 6)


def _baseline_excluding(
    snapshots: Sequence[TradeDaySnapshot],
    exclude_date: date,
    *,
    sample_size: int,
) -> int:
    return compute_median_baseline(
        [s for s in snapshots if s.trade_date != exclude_date],
        sample_size=sample_size,
    )


def _no_data_evidence() -> CompleteTradeDayEvidence:
    return CompleteTradeDayEvidence(
        selected_trade_date=EPOCH_DATE,
        observed_symbols=0,
        expected_symbols=0,
        completeness_ratio=0.0,
        fallback_reason="no_universe_data",
        median_baseline=0,
        evaluated_candidate_dates=[],
        candidate_ratios={},
    )


def _search_complete_day(
    snapshots: Sequence[TradeDaySnapshot],
    *,
    completeness_threshold: float,
    sample_size: int,
) -> tuple[TradeDaySnapshot | None, list[date], dict[str, float]]:
    """按 DESC 搜索第一个完整交易日。

    返回 (selected_snapshot, evaluated_dates, candidate_ratios)。
    - selected_snapshot 为 None 表示所有候选日均不达标。
    - candidate_ratios 每个候选日的比率（基准排除该候选日）。
    """
    evaluated_dates: list[date] = []
    candidate_ratios: dict[str, float] = {}

    for snap in snapshots:
        evaluated_dates.append(snap.trade_date)
        baseline = _baseline_excluding(
            snapshots, snap.trade_date, sample_size=sample_size
        )
        ratio = _ratio(snap.symbol_count, baseline)
        candidate_ratios[snap.trade_date.isoformat()] = ratio
        if baseline > 0 and ratio >= completeness_threshold:
            return snap, evaluated_dates, candidate_ratios

    return None, evaluated_dates, candidate_ratios


def latest_complete_trade_date(
    db_session: Session,
    *,
    completeness_threshold: float = 0.9,
    sample_size: int = 20,
    lookback_days: int = 30,
    asset_type: str = "stock",
    region: str = "cn",
    require_synced: bool = True,
) -> CompleteTradeDayEvidence:
    """主入口：返回最近一个完整交易日的证据。

    算法：
    1. 拉取最近 lookback_days 天的 universe_daily_bars 横截面快照
    2. 用前 20 个完整交易日的中位数作为 expected_symbols 基准
    3. 从最新日期开始按 DESC 检查：
       - 若 observed/expected >= completeness_threshold，则 selected = 当日
       - 否则继续检查前一日
    4. 若所有候选日都不满足阈值，回退到中位数最高的那一天

    证据字段语义：
    - observed_symbols / completeness_ratio 描述评估目标日（最新交易日），
      解释为何需要回退；selected_trade_date 是实际选用的完整交易日。
    - 基准中位数排除评估目标日，避免残缺日污染基准。
    """
    snapshots = fetch_recent_trade_day_snapshots(
        db_session,
        asset_type=asset_type,
        region=region,
        require_synced=require_synced,
        lookback_days=lookback_days,
        max_snapshots=max(sample_size + 5, 25),
    )
    if not snapshots:
        return _no_data_evidence()

    target = snapshots[0]
    target_baseline = _baseline_excluding(
        snapshots, target.trade_date, sample_size=sample_size
    )
    target_ratio = _ratio(target.symbol_count, target_baseline)

    selected, evaluated_dates, candidate_ratios = _search_complete_day(
        snapshots,
        completeness_threshold=completeness_threshold,
        sample_size=sample_size,
    )

    if selected is not None:
        fallback_reason = (
            None
            if selected.trade_date == target.trade_date
            else "below_90pct_median_20d"
        )
    else:
        selected = max(snapshots, key=lambda s: s.symbol_count)
        fallback_reason = "all_below_threshold_fallback_to_max"

    return CompleteTradeDayEvidence(
        selected_trade_date=selected.trade_date,
        observed_symbols=target.symbol_count,
        expected_symbols=target_baseline,
        completeness_ratio=target_ratio,
        fallback_reason=fallback_reason,
        median_baseline=target_baseline,
        evaluated_candidate_dates=evaluated_dates,
        candidate_ratios=candidate_ratios,
    )


def evaluate_trade_date_completeness(
    db_session: Session,
    candidate_date: date,
    *,
    completeness_threshold: float = 0.9,
    sample_size: int = 20,
    lookback_days: int = 30,
    asset_type: str = "stock",
    region: str = "cn",
    require_synced: bool = True,
) -> CompleteTradeDayEvidence:
    """评估指定候选日的完整性，返回证据对象。

    若候选日不完整，则 fallback 到最近一个完整交易日。
    证据字段描述候选日（评估目标）；selected_trade_date 为实际选用的交易日。
    """
    snapshots = fetch_recent_trade_day_snapshots(
        db_session,
        asset_type=asset_type,
        region=region,
        require_synced=require_synced,
        lookback_days=lookback_days,
        max_snapshots=max(sample_size + 5, 25),
    )
    if not snapshots:
        return _no_data_evidence()

    candidate = next(
        (s for s in snapshots if s.trade_date == candidate_date), None
    )
    if candidate is None:
        # 候选日不在回溯窗口内，视为无观测数据
        observed = 0
        candidate_baseline = compute_median_baseline(
            snapshots, sample_size=sample_size
        )
        candidate_ratio = 0.0
    else:
        observed = candidate.symbol_count
        candidate_baseline = _baseline_excluding(
            snapshots, candidate_date, sample_size=sample_size
        )
        candidate_ratio = _ratio(observed, candidate_baseline)

    if candidate_baseline > 0 and candidate_ratio >= completeness_threshold:
        return CompleteTradeDayEvidence(
            selected_trade_date=candidate_date,
            observed_symbols=observed,
            expected_symbols=candidate_baseline,
            completeness_ratio=candidate_ratio,
            fallback_reason=None,
            median_baseline=candidate_baseline,
            evaluated_candidate_dates=[candidate_date],
            candidate_ratios={candidate_date.isoformat(): candidate_ratio},
        )

    # 候选日不完整，回退到最近一个完整交易日
    selected, evaluated_dates, candidate_ratios = _search_complete_day(
        snapshots,
        completeness_threshold=completeness_threshold,
        sample_size=sample_size,
    )
    fallback_reason = "below_90pct_median_20d"
    if selected is None:
        selected = max(snapshots, key=lambda s: s.symbol_count)
        fallback_reason = "all_below_threshold_fallback_to_max"

    # 保留候选日自身的比率记录，便于审计
    candidate_ratios.setdefault(candidate_date.isoformat(), candidate_ratio)

    return CompleteTradeDayEvidence(
        selected_trade_date=selected.trade_date,
        observed_symbols=observed,
        expected_symbols=candidate_baseline,
        completeness_ratio=candidate_ratio,
        fallback_reason=fallback_reason,
        median_baseline=candidate_baseline,
        evaluated_candidate_dates=evaluated_dates,
        candidate_ratios=candidate_ratios,
    )


def previous_complete_trade_date(
    db_session: Session,
    *,
    before: date | None = None,
    completeness_threshold: float = 0.9,
    sample_size: int = 20,
    lookback_days: int = 30,
    asset_type: str = "stock",
    region: str = "cn",
    require_synced: bool = True,
) -> CompleteTradeDayEvidence:
    """返回 **严格早于 ``before``**（缺省为今天）的最近一个完整交易日。

    与 :func:`latest_complete_trade_date` 只差一件事：排除 ``before`` 当天。

    为什么需要它：对账、结算这类「T+1 回看」口径要的是「上一个**已收盘**的
    交易日」。盘后数据同步完成后，``latest_complete_trade_date`` 会把今天本身
    选出来 —— 而今天还没走完，拿它当对账目标日等于用未来当基准。

    判定口径与 ``latest_complete_trade_date`` 完全一致（见模块 docstring）：
    以 ``universe_daily_bars`` 的横截面完整度（≥ 阈值 × 过去 20 日中位数）为准，
    不查交易日历表 —— A 股节假日不固定（春节/中秋按农历、调休逐年通知），
    日历跟不上就会把假期算成交易日。
    """
    ref = before or date.today()
    snapshots = fetch_recent_trade_day_snapshots(
        db_session,
        asset_type=asset_type,
        region=region,
        require_synced=require_synced,
        lookback_days=lookback_days,
        max_snapshots=max(sample_size + 5, 25),
    )
    past = [s for s in snapshots if s.trade_date < ref]
    if not past:
        return _no_data_evidence()

    selected, evaluated_dates, candidate_ratios = _search_complete_day(
        past, completeness_threshold=completeness_threshold, sample_size=sample_size
    )
    if selected is None:
        selected = max(past, key=lambda s: s.symbol_count)
        fallback_reason = "all_below_threshold_fallback_to_max"
    else:
        fallback_reason = (
            None if selected is past[0] else "below_90pct_median_20d"
        )

    target = past[0]
    target_baseline = _baseline_excluding(
        past, target.trade_date, sample_size=sample_size
    )
    return CompleteTradeDayEvidence(
        selected_trade_date=selected.trade_date,
        observed_symbols=target.symbol_count,
        expected_symbols=target_baseline,
        completeness_ratio=_ratio(target.symbol_count, target_baseline),
        fallback_reason=fallback_reason,
        median_baseline=target_baseline,
        evaluated_candidate_dates=evaluated_dates,
        candidate_ratios=candidate_ratios,
    )
