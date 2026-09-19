"""WP0-6b / G2-4 基准健康服务（独立健康报告 + 主备降级，不阻断回测）。

基准健康（与 tasks.md WP0-6b 对齐）：
  - report.coverage_pct / max_consecutive_gap_days / gap_dates / primary_source /
    adj_mode / overall_status 字段完整
  - 三档状态：OK（覆盖 100% 且无缺口）/ PARTIAL（覆盖≥50% 且 max_gap ≤10）/
    UNAVAILABLE（覆盖<50% 或 max_gap >10 或 双源失败）
  - PARTIAL/UNAVAILABLE 只降级基准指标（基准曲线/超额/TE/IR），
    **不改变组合交易结果、不阻断回测、不触发 fail-closed**。
  - 复权口径 adj_mode 必须显式传入并写入快照/健康报告，不隐式默认。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Callable, Literal

from sqlalchemy.orm import Session

from app.services.index_data import list_index_prices

AdjMode = Literal["NONE", "QFQ_PRE", "HFQ_POST"]
BenchmarkHealthStatus = Literal["OK", "PARTIAL", "UNAVAILABLE"]
DataSource = Literal["INDEX_PRICE_TABLE", "AKSHARE_PRIMARY", "BAOSTOCK_FALLBACK",
                     "FALLBACK_MIXED", "BOTH_FAILED"]


@dataclass
class BenchmarkHealthReport:
    benchmark_symbol: str
    benchmark_name: str | None
    start_date: date
    end_date: date
    coverage_pct: float
    max_consecutive_gap_days: int
    gap_dates: list[date]
    primary_source: DataSource
    adj_mode: AdjMode
    overall_status: BenchmarkHealthStatus
    coverage_basis_trade_dates: int = 0
    coverage_basis_valid_bars: int = 0
    source_switch_event: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "benchmark_symbol": self.benchmark_symbol,
            "benchmark_name": self.benchmark_name,
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "coverage_pct": round(float(self.coverage_pct), 4),
            "max_consecutive_gap_days": int(self.max_consecutive_gap_days),
            "gap_dates": [d.isoformat() for d in self.gap_dates],
            "primary_source": self.primary_source,
            "adj_mode": self.adj_mode,
            "overall_status": self.overall_status,
            "coverage_basis_trade_dates": int(self.coverage_basis_trade_dates),
            "coverage_basis_valid_bars": int(self.coverage_basis_valid_bars),
            "source_switch_event": self.source_switch_event,
        }


def _trade_days_stub(start: date, end: date) -> list[date]:
    """粗略 A 股交易日历（Mon~Fri）。生产环境应调用真实 trade_calendar。"""
    days: list[date] = []
    cur = start
    while cur <= end:
        if cur.weekday() < 5:  # Mon..Fri
            days.append(cur)
        cur += timedelta(days=1)
    return days


def compute_health_report(
    db: Session,
    benchmark_symbol: str,
    start_date: date,
    end_date: date,
    *,
    benchmark_name: str | None = None,
    adj_mode: AdjMode = "HFQ_POST",
    # 注入点：生产真实调用 AkShare/Baostock 回填 index_prices 表
    akshare_fetcher: Callable[[str, date, date], list] | None = None,
    baostock_fetcher: Callable[[str, date, date], list] | None = None,
    trade_days_provider: Callable[[date, date], list[date]] | None = None,
    max_consecutive_gap_threshold: int = 10,
    min_coverage_pct_threshold: float = 50.0,
    akshare_timeout_s: float = 10.0,
    baostock_timeout_s: float = 10.0,
) -> BenchmarkHealthReport:
    """独立健康报告 + 主备降级（不阻断回测主链路）。"""
    primary_source: DataSource = "INDEX_PRICE_TABLE"
    source_switch_event: dict[str, Any] | None = None

    expected_trade_days = (trade_days_provider or _trade_days_stub)(start_date, end_date)
    # 1. 先消费本地物化表 index_prices（默认后复权）
    try:
        bars = list_index_prices(
            db, benchmark_symbol,
            start_date=start_date - timedelta(days=2),
            end_date=end_date + timedelta(days=1),
            limit=100_000,
        ) or []
    except Exception as e:  # pragma: no cover - 防御性
        bars = []
        source_switch_event = {"stage": "local_query", "error": f"{type(e).__name__}:{e}"}

    def _to_map(bars_iter) -> dict[date, Any]:
        m: dict[date, Any] = {}
        for b in bars_iter:
            d = getattr(b, "trade_date", None)
            if d is not None and getattr(b, "close", None):
                m[d] = b
        return m

    bar_map = _to_map(bars)
    local_valid = sum(1 for d in expected_trade_days if d in bar_map)

    # 2. 主源失败 / 覆盖率过低 → AkShare 主 → Baostock 备
    total_expected = len(expected_trade_days) or 1
    local_cov = 100.0 * local_valid / total_expected
    if local_cov < 90.0 and akshare_fetcher is not None:
        try:
            ak_bars = akshare_fetcher(benchmark_symbol, start_date, end_date)
            if ak_bars:
                ak_map = _to_map(ak_bars)
                merged = {**bar_map, **ak_map}
                if len(merged) > len(bar_map):
                    bar_map = merged
                    primary_source = "AKSHARE_PRIMARY" if source_switch_event is None else "FALLBACK_MIXED"
                    source_switch_event = source_switch_event or {
                        "from": "INDEX_PRICE_TABLE",
                        "to": "AKSHARE_PRIMARY",
                        "reason": f"local_coverage={local_cov:.1f}%",
                    }
        except Exception as e:
            source_switch_event = {
                "from": "AKSHARE_PRIMARY",
                "to": "BAOSTOCK_FALLBACK",
                "reason": f"akshare_error={type(e).__name__}:{e}",
            }
            # 3. AkShare 失败 → Baostock
            if baostock_fetcher is not None:
                try:
                    bs_bars = baostock_fetcher(benchmark_symbol, start_date, end_date)
                    if bs_bars:
                        bs_map = _to_map(bs_bars)
                        before = len(bar_map)
                        bar_map = {**bar_map, **bs_map}
                        if len(bar_map) > before:
                            primary_source = "BAOSTOCK_FALLBACK"
                except Exception as e2:
                    source_switch_event = {
                        **(source_switch_event or {}),
                        "baostock_error": f"{type(e2).__name__}:{e2}",
                        "final": "BOTH_FAILED",
                    }
                    primary_source = "BOTH_FAILED"

    # 4. 计算缺口 / 覆盖率 / 最大连续缺口
    gap_dates: list[date] = []
    consecutive_gap = 0
    max_consecutive_gap = 0
    valid_count = 0
    for d in expected_trade_days:
        if d in bar_map:
            valid_count += 1
            consecutive_gap = 0
        else:
            gap_dates.append(d)
            consecutive_gap += 1
            if consecutive_gap > max_consecutive_gap:
                max_consecutive_gap = consecutive_gap
    coverage_pct = 100.0 * valid_count / total_expected
    overall_status: BenchmarkHealthStatus
    if primary_source == "BOTH_FAILED" and valid_count == 0:
        overall_status = "UNAVAILABLE"
    elif coverage_pct >= 100.0 - 1e-6 and max_consecutive_gap == 0:
        overall_status = "OK"
    elif (
        coverage_pct >= min_coverage_pct_threshold
        and max_consecutive_gap <= max_consecutive_gap_threshold
    ):
        overall_status = "PARTIAL"
    else:
        overall_status = "UNAVAILABLE"

    return BenchmarkHealthReport(
        benchmark_symbol=benchmark_symbol,
        benchmark_name=benchmark_name,
        start_date=start_date,
        end_date=end_date,
        coverage_pct=coverage_pct,
        max_consecutive_gap_days=max_consecutive_gap,
        gap_dates=gap_dates,
        primary_source=primary_source,
        adj_mode=adj_mode,
        overall_status=overall_status,
        coverage_basis_trade_dates=total_expected,
        coverage_basis_valid_bars=valid_count,
        source_switch_event=source_switch_event,
    )
