"""WPD-02 完整交易日判定算法白盒测试。

覆盖 9 个场景，使用 SQLite 内存库 + SQLAlchemy 模型，不依赖 MySQL/DuckDB。
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.models.universe import UniverseDailyBar, UniverseSymbol
from app.services.factors.trade_calendar import (
    EPOCH_DATE,
    CompleteTradeDayEvidence,
    TradeDaySnapshot,
    compute_median_baseline,
    evaluate_trade_date_completeness,
    fetch_recent_trade_day_snapshots,
    latest_complete_trade_date,
)

pytestmark = pytest.mark.whitebox


# ── 测试数据辅助 ─────────────────────────────────────────

def _seed_universe(
    db_session,
    day_counts,
    *,
    extra_etf=0,
    extra_unsynced=0,
):
    """构造 universe_symbols + universe_daily_bars 测试数据。

    day_counts: [(trade_date, count), ...]，每个日期创建 count 个 synced stock 的 bar。
    同一日期出现多次时，后出现的 count 覆盖前者（便于基准日与特殊日叠加）。
    同步股票标的总数 = max(count)。
    extra_etf / extra_unsynced: 在最新日追加 ETF / 未同步股票的 bar，验证过滤。
    返回 synced stock symbol id 列表。
    """
    # 按日期去重，后出现的 count 覆盖前者
    counts_by_date: dict = {}
    for d, c in day_counts:
        counts_by_date[d] = c
    day_counts = list(counts_by_date.items())

    max_count = max((c for _, c in day_counts), default=0)

    stock_mappings = [
        {
            "symbol": f"S{i:05d}",
            "asset_type": "stock",
            "market": "sh",
            "region": "cn",
            "is_synced": 1,
        }
        for i in range(max_count)
    ]
    etf_mappings = [
        {
            "symbol": f"E{i:05d}",
            "asset_type": "etf",
            "market": "sh",
            "region": "cn",
            "is_synced": 1,
        }
        for i in range(extra_etf)
    ]
    unsynced_mappings = [
        {
            "symbol": f"U{i:05d}",
            "asset_type": "stock",
            "market": "sh",
            "region": "cn",
            "is_synced": 0,
        }
        for i in range(extra_unsynced)
    ]

    db_session.bulk_insert_mappings(UniverseSymbol, stock_mappings + etf_mappings + unsynced_mappings)
    db_session.commit()

    stock_ids = (
        db_session.execute(
            select(UniverseSymbol.id)
            .where(
                UniverseSymbol.asset_type == "stock",
                UniverseSymbol.region == "cn",
                UniverseSymbol.is_synced == 1,
            )
            .order_by(UniverseSymbol.id)
        )
        .scalars()
        .all()
    )
    etf_ids = (
        db_session.execute(
            select(UniverseSymbol.id).where(UniverseSymbol.asset_type == "etf")
        )
        .scalars()
        .all()
    )
    unsynced_ids = (
        db_session.execute(
            select(UniverseSymbol.id).where(UniverseSymbol.is_synced == 0)
        )
        .scalars()
        .all()
    )

    latest_date = max(d for d, _ in day_counts) if day_counts else None

    bar_rows = []
    for trade_date, count in day_counts:
        for i in range(count):
            bar_rows.append(
                {
                    "universe_symbol_id": stock_ids[i],
                    "trade_date": trade_date,
                    "close": 10.0,
                }
            )
    for sid in etf_ids:
        bar_rows.append(
            {"universe_symbol_id": sid, "trade_date": latest_date, "close": 10.0}
        )
    for sid in unsynced_ids:
        bar_rows.append(
            {"universe_symbol_id": sid, "trade_date": latest_date, "close": 10.0}
        )

    db_session.bulk_insert_mappings(UniverseDailyBar, bar_rows)
    db_session.commit()
    return stock_ids


def _trading_days_before(end: date, n: int) -> list[date]:
    """生成 end 之前 n 个工作日（跳过周末，不含 end）。"""
    days: list[date] = []
    cur = end - timedelta(days=1)
    while len(days) < n:
        if cur.weekday() < 5:  # 周一至周五
            days.append(cur)
        cur -= timedelta(days=1)
    return days


BASELINE_COUNT = 3650


def _seed_baseline_days(db_session, before: date, n_days: int = 10):
    """在 before 之前 n_days 个工作日每天创建 BASELINE_COUNT 个标的的 bar。

    使用工作日（跳过周末）避免在特殊交易日之间插入虚假的完整横截面。
    """
    return [(d, BASELINE_COUNT) for d in _trading_days_before(before, n_days)]


# ── 场景 1: 完整场景 ──────────────────────────────────────

def test_complete_day_selected_when_latest_meets_threshold(db_session):
    """当日 3,700 标的，过去 20 日中位数 3,650 → selected=当日，fallback=None。"""
    latest = date(2026, 7, 29)
    day_counts = _seed_baseline_days(db_session, latest, n_days=10)
    day_counts.append((latest, 3700))
    # 追加 ETF / 未同步标的，验证过滤排除
    _seed_universe(db_session, day_counts, extra_etf=5, extra_unsynced=5)

    evidence = latest_complete_trade_date(db_session)

    assert evidence.selected_trade_date == latest
    assert evidence.fallback_reason is None
    assert evidence.observed_symbols == 3700
    assert evidence.expected_symbols == 3650
    assert evidence.completeness_ratio == pytest.approx(3700 / 3650, rel=1e-4)
    assert evidence.median_baseline == 3650


# ── 场景 2: 残缺场景 ──────────────────────────────────────

def test_incomplete_latest_falls_back_to_previous_complete_day(db_session):
    """当日 150 标的，前一日完整 → 回退到前一日，fallback='below_90pct_median_20d'。"""
    latest = date(2026, 7, 29)
    prev_day = date(2026, 7, 28)
    day_counts = _seed_baseline_days(db_session, latest, n_days=10)
    day_counts.append((prev_day, 3700))
    day_counts.append((latest, 150))
    _seed_universe(db_session, day_counts)

    evidence = latest_complete_trade_date(db_session)

    assert evidence.selected_trade_date == prev_day
    assert evidence.fallback_reason == "below_90pct_median_20d"
    assert evidence.observed_symbols == 150
    assert evidence.expected_symbols == 3650
    assert evidence.completeness_ratio == pytest.approx(150 / 3650, rel=1e-3)
    # 07-29 应在已评估候选中且被跳过
    assert latest in evidence.evaluated_candidate_dates
    assert evidence.candidate_ratios[latest.isoformat()] < 0.9


# ── 场景 3: 边界 90% ─────────────────────────────────────

def test_boundary_90pct_passes_threshold(db_session):
    """当日 3,285 标的（3,650×0.9），中位数 3,650 → ratio=0.9，selected=当日。"""
    latest = date(2026, 7, 29)
    day_counts = _seed_baseline_days(db_session, latest, n_days=10)
    day_counts.append((latest, 3285))
    _seed_universe(db_session, day_counts)

    evidence = latest_complete_trade_date(db_session)

    assert evidence.selected_trade_date == latest
    assert evidence.fallback_reason is None
    assert evidence.observed_symbols == 3285
    assert evidence.expected_symbols == 3650
    assert evidence.completeness_ratio == pytest.approx(0.9, abs=1e-6)


# ── 场景 4: 2026-07-28/29 历史重现 ────────────────────────

def test_historical_0728_0729_falls_back_to_0724(db_session):
    """07-24=3699、07-27=2067、07-28=150、07-29=218 → selected=2026-07-24。"""
    day_0724 = date(2026, 7, 24)
    day_0727 = date(2026, 7, 27)
    day_0728 = date(2026, 7, 28)
    day_0729 = date(2026, 7, 29)

    day_counts = _seed_baseline_days(db_session, day_0729, n_days=10)
    day_counts.extend(
        [
            (day_0724, 3699),
            (day_0727, 2067),
            (day_0728, 150),
            (day_0729, 218),
        ]
    )
    _seed_universe(db_session, day_counts)

    evidence = latest_complete_trade_date(db_session)

    assert evidence.selected_trade_date == day_0724
    assert evidence.fallback_reason == "below_90pct_median_20d"
    assert evidence.observed_symbols == 218
    assert evidence.expected_symbols == 3650
    # 07-29 和 07-28 都被跳过（比率低于阈值）
    assert evidence.candidate_ratios[day_0729.isoformat()] < 0.9
    assert evidence.candidate_ratios[day_0728.isoformat()] < 0.9
    assert evidence.candidate_ratios[day_0727.isoformat()] < 0.9
    assert evidence.candidate_ratios[day_0724.isoformat()] >= 0.9


# ── 场景 5: 所有日均不达标 ────────────────────────────────

def test_all_below_threshold_falls_back_to_max(db_session):
    """所有候选日都无法建立基准（单日无对照）→ 回退到最大值日。"""
    latest = date(2026, 7, 29)
    _seed_universe(db_session, [(latest, 1000)])

    evidence = latest_complete_trade_date(db_session)

    assert evidence.selected_trade_date == latest
    assert evidence.fallback_reason == "all_below_threshold_fallback_to_max"
    assert evidence.observed_symbols == 1000
    # 单日无法构成基准（排除候选日后无快照）
    assert evidence.expected_symbols == 0
    assert evidence.completeness_ratio == 0.0


# ── 场景 6: 空数据 ───────────────────────────────────────

def test_no_universe_data_returns_epoch(db_session):
    """universe_daily_bars 无数据 → epoch + no_universe_data。"""
    evidence = latest_complete_trade_date(db_session)

    assert evidence.selected_trade_date == EPOCH_DATE
    assert evidence.fallback_reason == "no_universe_data"
    assert evidence.observed_symbols == 0
    assert evidence.expected_symbols == 0
    assert evidence.completeness_ratio == 0.0
    assert evidence.evaluated_candidate_dates == []


# ── 场景 7: 基准排除候选日 ────────────────────────────────

def test_baseline_excludes_candidate_to_avoid_pollution(db_session):
    """候选日是最新日且残缺时，基准不应包含该日，避免基准被拉低。

    数据：2 天 3650、3 天 100（含最新日 100）。
    若不排除最新日，中位数会被拉到 100，错误判定为完整；
    排除后中位数为 1875，正确判定为残缺。
    """
    day_0723 = date(2026, 7, 23)
    day_0724 = date(2026, 7, 24)
    day_0725 = date(2026, 7, 25)
    day_0728 = date(2026, 7, 28)
    day_0729 = date(2026, 7, 29)

    day_counts = [
        (day_0723, 100),
        (day_0724, 3650),
        (day_0725, 3650),
        (day_0728, 100),
        (day_0729, 100),
    ]
    _seed_universe(db_session, day_counts)

    evidence = latest_complete_trade_date(db_session)

    # 最新日残缺，应回退到 07-25（第一个 3650 日）
    assert evidence.selected_trade_date == day_0725
    assert evidence.fallback_reason == "below_90pct_median_20d"
    # 基准排除最新日后为 1875（而非被拉低到 100）
    assert evidence.median_baseline == 1875
    assert evidence.completeness_ratio == pytest.approx(100 / 1875, rel=1e-3)


# ── 场景 8: 快照数不足 20 ─────────────────────────────────

def test_fewer_than_20_snapshots_uses_all_available(db_session):
    """只有 5 个快照时，基准用全部可用快照的中位数。"""
    latest = date(2026, 7, 29)
    # 仅 4 个历史日 + 1 个最新日 = 5 个快照
    day_counts = [(latest - timedelta(days=i + 1), 3650) for i in range(4)]
    day_counts.append((latest, 3700))
    _seed_universe(db_session, day_counts)

    snapshots = fetch_recent_trade_day_snapshots(db_session)
    assert len(snapshots) == 5

    baseline = compute_median_baseline(
        [s for s in snapshots if s.trade_date != latest]
    )
    # 4 个 3650 的中位数 = 3650
    assert baseline == 3650

    evidence = latest_complete_trade_date(db_session)
    assert evidence.selected_trade_date == latest
    assert evidence.fallback_reason is None
    assert evidence.median_baseline == 3650


# ── 场景 9: evaluate_trade_date_completeness 显式候选日 ────

def test_evaluate_explicit_candidate_falls_back(db_session):
    """指定 2026-07-29，算法应回退到 2026-07-24。"""
    day_0724 = date(2026, 7, 24)
    day_0727 = date(2026, 7, 27)
    day_0728 = date(2026, 7, 28)
    day_0729 = date(2026, 7, 29)

    day_counts = _seed_baseline_days(db_session, day_0729, n_days=10)
    day_counts.extend(
        [
            (day_0724, 3699),
            (day_0727, 2067),
            (day_0728, 150),
            (day_0729, 218),
        ]
    )
    _seed_universe(db_session, day_counts)

    evidence = evaluate_trade_date_completeness(db_session, day_0729)

    assert evidence.selected_trade_date == day_0724
    assert evidence.fallback_reason == "below_90pct_median_20d"
    assert evidence.observed_symbols == 218
    assert evidence.expected_symbols == 3650
    assert evidence.completeness_ratio == pytest.approx(218 / 3650, rel=1e-3)


def test_evaluate_complete_candidate_returns_itself(db_session):
    """显式指定完整候选日时直接返回该日。"""
    latest = date(2026, 7, 29)
    day_counts = _seed_baseline_days(db_session, latest, n_days=10)
    day_counts.append((latest, 3700))
    _seed_universe(db_session, day_counts)

    evidence = evaluate_trade_date_completeness(db_session, latest)

    assert evidence.selected_trade_date == latest
    assert evidence.fallback_reason is None
    assert evidence.observed_symbols == 3700


# ── 附加：可重复性与过滤 ──────────────────────────────────

def test_algorithm_is_deterministic(db_session):
    """相同输入返回相同结果（可重复运行）。"""
    latest = date(2026, 7, 29)
    day_counts = _seed_baseline_days(db_session, latest, n_days=10)
    day_counts.append((latest, 150))
    day_counts.append((date(2026, 7, 28), 3700))
    _seed_universe(db_session, day_counts)

    first = latest_complete_trade_date(db_session)
    second = latest_complete_trade_date(db_session)

    assert first == second


def test_filter_excludes_non_stock_and_unsynced(db_session):
    """ETF 和未同步股票不应计入横截面快照。"""
    latest = date(2026, 7, 29)
    _seed_universe(
        db_session,
        [(latest, 100)],
        extra_etf=20,
        extra_unsynced=30,
    )

    snapshots = fetch_recent_trade_day_snapshots(db_session)
    assert len(snapshots) == 1
    assert snapshots[0].symbol_count == 100
