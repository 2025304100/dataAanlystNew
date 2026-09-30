"""常驻标签批次的解析与就地补齐（VIZ-0930-27 / 29 的写读收口点）。

取代"谁要用谁抄一份全窗口快照"的老做法：调用方给窗口，这里返回能支撑该窗口
的常驻批次；缺哪段就补哪段，且**补写回同一个批次**（冲突键
`(symbol, signal_date, target_code, calc_batch_id)` + upsert），因此补齐不会
新增第二份覆盖。

四条不可让的口径：
1. 缺口判定只认镜像 bars 日历，并且上界要扣掉 horizon —— 见 `label_coverage`；
2. K 线缺口、以及窗口整段落在最近 horizon 个交易日内，都补不动，必须直接报错，
   不能"再跑一次就有了"；
3. 其它 horizon 的 target_code 不在常驻批次里（`target_engine` 只产 5d），
   必须回落到原有"最新批次"语义，不能假装常驻批次里有；
4. 给了 `as_of_exit_date` 就按 PIT 截窗：标签要 `exit_date <= as_of` 才算可用。
   老实现按"运行时可见的 K 线"算快照，`end_date` 晚于 `data_cutoff_at` 时会把
   cutoff 之后才定的标签一起用掉——那是前视，不是冻结。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from app.services.factors.label_coverage import (
    TARGET_HORIZON_TRADING_DAYS,
    LabelCoverageReport,
    evaluate_label_coverage,
)
from app.services.factors.store import SHARED_TARGET_BATCH_ID
from app.services.factors.target_engine import TARGET_CODE

_HORIZON_PREFIX = "target_"
_HORIZON_SUFFIX = "d_return"


class LabelCoverageError(ValueError):
    """窗口无法被标签库支撑。携带可直接展示的真实缺口说明。"""

    def __init__(self, report: LabelCoverageReport) -> None:
        self.report = report
        super().__init__(report.message or f"label_coverage_missing:{report.blocker}")


@dataclass(frozen=True)
class ResolvedLabelBatch:
    batch_id: str
    target_code: str
    backfilled: bool
    rows_written: int
    coverage: LabelCoverageReport
    usable_end: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "target_code": self.target_code,
            "backfilled": self.backfilled,
            "rows_written": self.rows_written,
            "usable_end": str(self.usable_end) if self.usable_end else None,
            "coverage": self.coverage.to_dict(),
        }


def horizon_trading_days_of(target_code: str) -> int:
    """从 `target_{n}d_return` 解析未来交易日数；解析不了按 5d。"""
    if target_code.startswith(_HORIZON_PREFIX) and target_code.endswith(_HORIZON_SUFFIX):
        middle = target_code[len(_HORIZON_PREFIX):-len(_HORIZON_SUFFIX)]
        if middle.isdigit():
            return int(middle)
    return TARGET_HORIZON_TRADING_DAYS


def usable_end(
    warehouse: Any,
    *,
    end_date: date,
    as_of_exit_date: date | None = None,
    target_code: str = TARGET_CODE,
    adjust: str = "qfq",
) -> date:
    """PIT 上界：as_of 之前已定值的最晚信号日。

    公开给只读判定用（`/factors/label-coverage`），否则接口按"今天全库 bars"
    判、挖掘与评估按各自 cutoff 判，同一窗口会给出两种结论，UI 上看就是
    "永远缺最后 horizon 天"。
    """
    if as_of_exit_date is None:
        return end_date
    cap = warehouse.shift_back_trading_days(
        as_of_exit_date, horizon_trading_days_of(target_code), adjust=adjust
    )
    return min(end_date, cap) if cap is not None else end_date


def resolve_label_batch(
    warehouse: Any,
    *,
    start_date: date,
    end_date: date,
    target_code: str = TARGET_CODE,
    adjust: str = "qfq",
    as_of_exit_date: date | None = None,
    allow_backfill: bool = True,
    limit_threshold: float = 0.095,
) -> ResolvedLabelBatch:
    """返回可支撑窗口的标签批次，必要时就地补齐。

    `allow_backfill=False` 用于"只读不许写"的场景（预检、心跳）：缺口只报错。
    返回的 `usable_end` 应当作读取上界传给 `get_target_panel`——判定与读取必须
    用同一个窗口，否则会出现"按截过的窗口判够、按全窗口取数"的错位。
    """
    capped_end = usable_end(
        warehouse,
        end_date=end_date,
        as_of_exit_date=as_of_exit_date,
        target_code=target_code,
        adjust=adjust,
    )

    if target_code != TARGET_CODE:
        return _resolve_other_target_code(
            warehouse,
            start_date=start_date,
            end_date=capped_end,
            target_code=target_code,
            adjust=adjust,
        )

    report = evaluate_label_coverage(
        warehouse,
        batch_id=SHARED_TARGET_BATCH_ID,
        target_code=target_code,
        adjust=adjust,
        start_date=start_date,
        end_date=capped_end,
    )
    if report.satisfied:
        return ResolvedLabelBatch(
            batch_id=SHARED_TARGET_BATCH_ID,
            target_code=target_code,
            backfilled=False,
            rows_written=0,
            coverage=report,
            usable_end=capped_end,
        )
    if not allow_backfill:
        raise LabelCoverageError(report)
    if report.blocker != "coverage_gap":
        # 只有"K 线已镜像、只缺标签"才补得动：bars_gap 要先补 K 线；
        # horizon_only 的窗口整段落在最近 horizon 个交易日内，补了也只能写出
        # insufficient_future_calendar 的废行——必须报错，不能先写再报。
        raise LabelCoverageError(report)

    from app.services.factors.target_engine import calculate_targets

    result = calculate_targets(
        warehouse,
        start_date=start_date,
        end_date=capped_end,
        calc_batch_id=SHARED_TARGET_BATCH_ID,
        adjust=adjust,
        limit_threshold=limit_threshold,
    )
    after = evaluate_label_coverage(
        warehouse,
        batch_id=SHARED_TARGET_BATCH_ID,
        target_code=target_code,
        adjust=adjust,
        start_date=start_date,
        end_date=capped_end,
    )
    if not after.satisfied:
        # 补完仍不齐（例如镜像在窗口中途有停牌空档）：报真实缺口而不是继续跑
        raise LabelCoverageError(after)
    return ResolvedLabelBatch(
        batch_id=SHARED_TARGET_BATCH_ID,
        target_code=target_code,
        backfilled=True,
        rows_written=int(result.rows_written),
        coverage=after,
        usable_end=capped_end,
    )


def _resolve_other_target_code(
    warehouse: Any,
    *,
    start_date: date,
    end_date: date,
    target_code: str,
    adjust: str,
) -> ResolvedLabelBatch:
    """非 5d 口径：常驻批次里没有它的标签，回落到原有"最新批次"语义。

    这里不回退到"自己造一批"——那正是 VIZ-0930-27 的老路。没有就明确报错。
    """
    batch_id = warehouse.get_latest_target_batch_id(target_code)
    if not batch_id:
        raise LabelCoverageError(
            evaluate_label_coverage(
                warehouse,
                batch_id=SHARED_TARGET_BATCH_ID,
                target_code=target_code,
                adjust=adjust,
                start_date=start_date,
                end_date=end_date,
            )
        )
    report = evaluate_label_coverage(
        warehouse,
        batch_id=str(batch_id),
        target_code=target_code,
        adjust=adjust,
        start_date=start_date,
        end_date=end_date,
    )
    if not report.satisfied:
        raise LabelCoverageError(report)
    return ResolvedLabelBatch(
        batch_id=str(batch_id),
        target_code=target_code,
        backfilled=False,
        rows_written=0,
        coverage=report,
        usable_end=end_date,
    )
