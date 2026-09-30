"""标签库覆盖 gate：共享目标批次能否支撑一次运行的唯一判定入口。

背景：`factor_targets` 的批次 ID 不再隐含窗口（共享批次装的是"全镜像标的 ×
全可用交易日"），所以"这次 run 要的窗口到底有没有标签"必须由显式断言回答，
不能靠调用方假设。历史上挖掘每个 run 自产一份全窗口快照，用冗余掩盖了这个
问题——窗口对不齐时面板静默变短，run 照跑照出零候选。

判定口径的两条硬约束：
1. 期望交易日只能来自镜像 bars 日历（`raw_daily_bars` 的 DISTINCT trade_date），
   不能用自然日，否则停牌/周末会让缺口永远补不齐。
2. 覆盖上界要扣掉 horizon：信号日 T 的标签需要 T+5 收盘才定值
   （`target_engine` 里 entry=T+1、exit=T+5），所以最近 5 个交易日天然不可能
   有可用标签，不能算作缺口。回推起点是全库最新交易日，不是请求末端——请求
   末端早于库末端时，末端标签其实已经可定值。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

# target_engine._load_target_panel: entry_calendar = trade_index + 1,
# exit_calendar = trade_index + 5 —— 标签需要 5 个未来交易日才能定值。
TARGET_HORIZON_TRADING_DAYS = 5

# 缺口日期在消息里只展示前若干个，避免长窗口把提示撑成屏。
_MISSING_PREVIEW_LIMIT = 10


def _as_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


@dataclass(frozen=True)
class LabelCoverageReport:
    """一次窗口覆盖判定的结果，可直接进 API 响应与任务 blocker。"""

    satisfied: bool
    batch_id: str
    target_code: str
    adjust: str
    requested_start: date | None
    requested_end: date | None
    effective_end: date | None
    expected_days: int
    covered_days: int
    missing_dates: tuple[date, ...] = field(default_factory=tuple)
    missing_days: int = 0
    missing_backfillable: bool | None = None
    bars_cover_start: date | None = None
    bars_latest_date: date | None = None
    symbol_count: int | None = None
    blocker: str | None = None
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "satisfied": self.satisfied,
            "batch_id": self.batch_id,
            "target_code": self.target_code,
            "adjust": self.adjust,
            "requested_start": str(self.requested_start) if self.requested_start else None,
            "requested_end": str(self.requested_end) if self.requested_end else None,
            "effective_end": str(self.effective_end) if self.effective_end else None,
            "expected_days": self.expected_days,
            "covered_days": self.covered_days,
            "missing_days": self.missing_days,
            "missing_dates": [str(d) for d in self.missing_dates],
            "missing_backfillable": self.missing_backfillable,
            "bars_cover_start": str(self.bars_cover_start) if self.bars_cover_start else None,
            "bars_latest_date": str(self.bars_latest_date) if self.bars_latest_date else None,
            "symbol_count": self.symbol_count,
            "blocker": self.blocker,
            "message": self.message,
        }


def _span_text(days: list[date], limit: int = _MISSING_PREVIEW_LIMIT) -> str:
    """把缺口日期压成人类可读的区间串，最多 limit 个。"""
    if not days:
        return ""
    spans: list[str] = []
    start = prev = days[0]
    for current in days[1:]:
        if (current - prev).days == 1:
            prev = current
            continue
        spans.append(str(start) if start == prev else f"{start}..{prev}")
        start = prev = current
    spans.append(str(start) if start == prev else f"{start}..{prev}")
    shown = spans[:limit]
    suffix = f" 等 {len(spans)} 段" if len(spans) > limit else ""
    return "、".join(shown) + suffix


def evaluate_label_coverage(
    warehouse: Any,
    *,
    batch_id: str,
    target_code: str = "target_5d_return",
    adjust: str = "qfq",
    start_date: date | None,
    end_date: date | None,
) -> LabelCoverageReport:
    """判定 [start_date, end_date] 的标签是否齐备。

    只读，不改任何数据；`satisfied=False` 时 `message` 给出可直接展示的真实
    缺口区间与"能不能补"（区分标签缺口与 K 线缺口，后者补标签也救不了）。
    """
    start = _as_date(start_date)
    end = _as_date(end_date)
    base = dict(
        batch_id=batch_id,
        target_code=target_code,
        adjust=adjust,
        requested_start=start,
        requested_end=end,
    )
    if start is None or end is None:
        return LabelCoverageReport(
            satisfied=False,
            effective_end=None,
            expected_days=0,
            covered_days=0,
            blocker="window_required",
            message="缺少判定窗口：需要同时给出开始与结束日期",
            **base,
        )
    if start > end:
        return LabelCoverageReport(
            satisfied=False,
            effective_end=None,
            expected_days=0,
            covered_days=0,
            blocker="invalid_window",
            message=f"窗口非法：开始日期 {start} 晚于结束日期 {end}",
            **base,
        )

    calendar = warehouse.list_trading_days(start, end, adjust=adjust)
    if not calendar:
        return LabelCoverageReport(
            satisfied=False,
            effective_end=None,
            expected_days=0,
            covered_days=0,
            missing_backfillable=False,
            blocker="bars_gap",
            message=(
                f"镜像 K 线未覆盖 {start}..{end}（口径 {adjust}），"
                "无法判定标签覆盖，需先补 K 线镜像"
            ),
            **base,
        )

    global_first = _as_date(warehouse.earliest_bar_date(adjust))
    bars_cover_start = global_first or calendar[0]
    # horizon 上界要从"全库最新交易日"往回推，而不是从请求末端推：请求末端
    # 早于库末端时，末端那些交易日其实早已可定值，按请求末端推会把它们误当成
    # 不可判（实测表现为 392/397，方向保守但数字不准）。
    global_latest = _as_date(warehouse.latest_bar_date(adjust))
    bars_latest = global_latest or calendar[-1]
    coverage = warehouse.get_label_coverage(batch_id, target_code, adjust=adjust)
    symbol_count = coverage.get("symbol_count") if coverage is not None else None

    # 覆盖上界：最近 horizon 个交易日的标签必然还没定值，不能算缺口。
    horizon_anchor = warehouse.shift_back_trading_days(
        bars_latest, TARGET_HORIZON_TRADING_DAYS, adjust=adjust
    )
    effective_end = end
    if horizon_anchor is not None:
        effective_end = min(end, horizon_anchor)
    expected = [d for d in calendar if d <= effective_end]

    covered_dates = warehouse.list_covered_signal_dates(
        batch_id, target_code, start, effective_end
    )
    covered = set(covered_dates)
    missing = [d for d in expected if d not in covered]

    # K 线缺口必须先判：expected 是由 bars 日历生成的，只看标签缺口会让
    # "请求起点早于镜像起点"这一整段静默通过，表现为面板悄悄变短。
    if bars_cover_start > start:
        message = (
            f"K 线缺口：镜像只覆盖到 {bars_cover_start}，请求起点 {start} 之前"
            "没有 K 线，需先补 K 线镜像再补标签"
        )
        if missing:
            message += (
                f"；此外已有 K 线的区间内还缺 {len(missing)} 个交易日标签"
                f"（{_span_text(missing)}）"
            )
        return LabelCoverageReport(
            satisfied=False,
            effective_end=effective_end,
            expected_days=len(expected),
            covered_days=len(covered),
            missing_dates=tuple(missing[:_MISSING_PREVIEW_LIMIT]),
            missing_days=len(missing),
            missing_backfillable=False,
            bars_cover_start=bars_cover_start,
            bars_latest_date=bars_latest,
            symbol_count=symbol_count,
            blocker="bars_gap",
            message=message,
            **base,
        )

    if not expected:
        return LabelCoverageReport(
            satisfied=False,
            effective_end=effective_end,
            expected_days=0,
            covered_days=len(covered),
            bars_cover_start=bars_cover_start,
            bars_latest_date=bars_latest,
            symbol_count=symbol_count,
            blocker="horizon_only",
            message=(
                f"{start}..{end} 全部落在最近 {TARGET_HORIZON_TRADING_DAYS} "
                "个交易日内，标签尚未定值（需要未来 K 线）"
            ),
            **base,
        )

    if not missing:
        return LabelCoverageReport(
            satisfied=True,
            effective_end=effective_end,
            expected_days=len(expected),
            covered_days=len(expected),
            bars_cover_start=bars_cover_start,
            bars_latest_date=bars_latest,
            symbol_count=symbol_count,
            message=(
                f"标签覆盖完整：{start}..{effective_end} 共 {len(expected)} 个交易日"
            ),
            **base,
        )

    # 走到这里说明 K 线覆盖到了请求起点，缺口只可能出在标签本身 → 可补。
    message = (
        f"标签缺口：需要 {start}..{effective_end} 共 {len(expected)} 个交易日，"
        f"实有 {len(covered)} 个，缺 {len(missing)} 个（{_span_text(missing)}）；"
        f"该区间 K 线已镜像（起点 {bars_cover_start}），可补标签"
    )
    return LabelCoverageReport(
        satisfied=False,
        effective_end=effective_end,
        expected_days=len(expected),
        covered_days=len(covered),
        missing_dates=tuple(missing[:_MISSING_PREVIEW_LIMIT]),
        missing_days=len(missing),
        missing_backfillable=True,
        bars_cover_start=bars_cover_start,
        bars_latest_date=bars_latest,
        symbol_count=symbol_count,
        blocker="coverage_gap",
        message=message,
        **base,
    )
