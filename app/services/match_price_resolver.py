"""T-B2 Q2.1：NEXT_OPEN 真实撮合价格提取 + 涨跌停顺延。

实现两层（方便测试 + 便于接入回测/生产两种上下文）：
  1) 纯函数 `resolve_next_open_bar(rows, *, prev_close=None, max_roll_days=10)`：
        输入已经按 trade_date 升序排好的 T+1 起之后的 N 根 DailyBar 行；
        从第 0 根往后扫，遇到 "有效" open（非停牌 + 非涨跌停 + volume>0）返回；
        每跳过一根记一条 rejection（LIMIT_UP_DOWN / SUSPENDED），含当天的 intended_open。
  2) DB 入口 `get_match_price_next_open(db, symbol_id, trade_date, *, max_roll_days=10)`：
        查 DailyBar：WHERE symbol_id=? AND trade_date > trade_date ORDER BY trade_date ASC
        LIMIT max_roll_days；同时**额外查 T-1 的 close** 作为 prev_close（用来判断涨跌停）。

顺延判断规则（T-B2 tasks.md 定义）：
  - 跳过条件 A（SUSPENDED）：当日 `volume 为 NULL 或 <= 0` → 视为停牌/无成交；
        rejection_reason="SUSPENDED"
  - 跳过条件 B（LIMIT_UP_DOWN）：**同时满足**
        a) 当日 open == high == low == close（一字板，没有任何成交区间）；
        b) 已知 prev_close 时，满足 ±10% 或 ±20% A 股涨跌停范围浮动（容差 1e-4，兼容 float 误差）。
  - 有效（不跳过）：不满足 A **且**不满足 B，且 volume>0，open>0。

返回结构体（后续 T-B3 会把 rejections 写入 DecisionEvidence.rejections_trace_json）：
    MatchPriceResult(final_trade_date, final_open, roll_forward_days, rejections)
        - rejections: [ {"date": "YYYY-MM-DD",
                         "reason": "LIMIT_UP_DOWN"|"SUSPENDED",
                         "intended_open": float} ]
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable, Sequence

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.models.daily_bar import DailyBar
from app.services.decision_clock import MatchMode  # 用于后续 T-B3 注解扩展
from app.services.decision_clock import DEFAULT_MATCH_MODE  # noqa: F401 (公开默认口径)


# ═════════════════════════════════════════════════════════════════════════════
# 公开 rejection reason 常量（T-B3 会与 DecisionEvidence.rejection_reason 共享同一套）
# ═════════════════════════════════════════════════════════════════════════════
REJECTION_REASON_SUSPENDED = "SUSPENDED"
REJECTION_REASON_LIMIT_UP_DOWN = "LIMIT_UP_DOWN"
REJECTION_REASON_EXHAUSTED_ROLL_WINDOW = "EXHAUSTED_ROLL_WINDOW"

# A 股涨跌停容差（默认主板 10%，科创板/创业板 20%；此处取 union 更宽松的区间匹配）
_LIMIT_UP_TOLERANCES_BPS = (1000, 2000)  # 10%, 20%
_LIMIT_FLOAT_EPS = 1e-4


@dataclass
class MatchPriceRejection:
    """单次顺延（单日跳过）记录。T-B3 要整包塞进 rejections_trace_json。"""

    trade_date: date
    reason: str  # REJECTION_REASON_*
    intended_open: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "date": self.trade_date.isoformat(),
            "reason": self.reason,
            "intended_open": float(self.intended_open),
        }


@dataclass
class MatchPriceResult:
    """NEXT_OPEN 撮合解析结果。"""

    match_mode: str = "NEXT_OPEN"
    success: bool = False
    final_trade_date: date | None = None
    final_open: float | None = None
    roll_forward_days: int = 0  # 从 T+1 算往后几天，0 表示 T+1 直接命中
    rejections: list[MatchPriceRejection] = field(default_factory=list)
    exhausted_reason: str | None = None  # 若 max_roll_days 全跳过，这里记 EXHAUSTED_ROLL_WINDOW

    @property
    def rejection_reasons_list(self) -> list[str]:
        """顺延后被跳过的每日 reason 聚合（用于 evidence 的 reject_summary）。"""
        return [r.reason for r in self.rejections]

    def as_dict(self) -> dict[str, Any]:
        return {
            "match_mode": self.match_mode,
            "success": self.success,
            "final_trade_date": self.final_trade_date.isoformat() if self.final_trade_date else None,
            "final_open": float(self.final_open) if self.final_open is not None else None,
            "roll_forward_days": int(self.roll_forward_days),
            "rejections": [r.as_dict() for r in self.rejections],
            "exhausted_reason": self.exhausted_reason,
        }


def _is_one_price_bar(open_f: float, high_f: float, low_f: float, close_f: float) -> bool:
    """一字板判定（open=high=low=close，通常是涨跌停或极端停牌后集合竞价封板）。"""
    o, h, l, c = float(open_f), float(high_f), float(low_f), float(close_f)
    return (abs(o - h) <= _LIMIT_FLOAT_EPS and
            abs(h - l) <= _LIMIT_FLOAT_EPS and
            abs(l - c) <= _LIMIT_FLOAT_EPS)


def _is_limit_up_down(prev_close: float | None, open_f: float) -> bool:
    """涨跌停判定。有 prev_close 时按 ±10% / ±20% 任一命中即真；无 prev_close 仅当 open<=0 保守认为未通过（假）。"""
    if prev_close is None:
        return False
    if float(prev_close) <= 0:
        return False
    p = float(open_f)
    c = float(prev_close)
    # A 股价格最小单位 0.01；为避免 float 误差，比对容差 1e-3 已足够
    for bps in _LIMIT_UP_TOLERANCES_BPS:
        ratio = bps / 10_000.0
        up_target = c * (1.0 + ratio)
        dn_target = c * (1.0 - ratio)
        if abs(p - up_target) <= max(1e-3, up_target * 1e-4):
            return True
        if abs(p - dn_target) <= max(1e-3, abs(dn_target) * 1e-4):
            return True
    return False


def _is_suspended(volume: Any) -> bool:
    if volume is None:
        return True
    try:
        v = float(volume)
    except Exception:
        return True
    return v <= 0.0


def resolve_next_open_bar(
    rows: Sequence[tuple[date, float, float, float, float, Any]],
    *,
    prev_close: float | None = None,
    max_roll_days: int = 10,
) -> MatchPriceResult:
    """纯函数版 NEXT_OPEN 解析（**无 DB、无 IO**）。便于单测覆盖 100% 路径。

    Args:
        rows: **已按 trade_date 升序（T+1 排首位）**的 N 根候选 K 线。
              每格 = (trade_date, open, high, low, close, volume)；volume 允许 None。
        prev_close: T-1 日收盘价（可选。无此值时涨跌停规则更宽松：仅"一字板"无法确定涨跌停，因此不会被 LIMIT_UP_DOWN 跳过）。
        max_roll_days: 最多往后找几根 K 线。

    Returns:
        MatchPriceResult。若所有候选 K 线都被跳过，则 success=False + exhausted_reason=EXHAUSTED_ROLL_WINDOW。
    """
    result = MatchPriceResult(match_mode=MatchMode.NEXT_OPEN.value)

    if max_roll_days <= 0:
        raise ValueError(f"max_roll_days must be >= 1, got {max_roll_days!r}")

    scanned = 0
    for idx, row in enumerate(rows):
        if scanned >= max_roll_days:
            break
        if len(row) < 6:
            # 防御：缺字段的行直接视为坏数据 → 停牌风格跳过
            raise ValueError(
                f"resolve_next_open_bar row must have 6 elements (date,open,high,low,close,volume); got row={row!r}"
            )
        d, o, h, l, c, vol = row
        scanned += 1

        # 有效日判定顺序：先停牌；再涨跌停；最后 open<=0 异常
        if _is_suspended(vol):
            result.rejections.append(MatchPriceRejection(
                trade_date=d,
                reason=REJECTION_REASON_SUSPENDED,
                intended_open=float(o),
            ))
            continue

        if _is_one_price_bar(o, h, l, c) and _is_limit_up_down(prev_close, o):
            result.rejections.append(MatchPriceRejection(
                trade_date=d,
                reason=REJECTION_REASON_LIMIT_UP_DOWN,
                intended_open=float(o),
            ))
            continue

        if float(o) <= 0:
            # 没标停牌但 open<=0 是数据异常 → 视作停牌
            result.rejections.append(MatchPriceRejection(
                trade_date=d,
                reason=REJECTION_REASON_SUSPENDED,
                intended_open=float(o),
            ))
            continue

        # 通过所有筛选
        result.success = True
        result.final_trade_date = d
        result.final_open = float(o)
        result.roll_forward_days = idx  # 从 0 开始：0 表示 T+1 直接命中
        return result

    # 扫描窗口耗尽（没命中）
    if not result.success:
        result.exhausted_reason = REJECTION_REASON_EXHAUSTED_ROLL_WINDOW
    return result


# ---------------------------------------------------------------------------
# DB 入口（T-B2 规范）
# ---------------------------------------------------------------------------

def get_match_price_next_open(
    db: Session,
    symbol_id: int,
    trade_date: date,
    *,
    max_roll_days: int = 10,
) -> MatchPriceResult:
    """按 Q2.1 规范：从 T+1 起向后找首个有效 NEXT_OPEN open。

    注意：生产/回测调用方应先构造 price_data_by_symbol（例如 T-A9 注入手动价）。
    当 price_data_by_symbol 已经覆盖了某 symbol_id 的下一日 open 时，应走 T-A9 的
    直接价格注入路径（本函数只用于 pure NEXT_OPEN 无人工覆盖场景）。
    """
    # Step 1: 取 T-1 close 作为涨跌停基准（允许缺失 → 涨跌停规则退化为只按一字板+成交量>0宽松判定）
    prev_close: float | None = None
    prev_q = (
        select(DailyBar.close)
        .where(and_(DailyBar.symbol_id == int(symbol_id), DailyBar.trade_date < trade_date))
        .order_by(DailyBar.trade_date.desc())
        .limit(1)
    )
    prev_row = db.execute(prev_q).scalar_one_or_none()
    if prev_row is not None:
        prev_close = float(prev_row)

    # Step 2: 取 T+1 起 max_roll_days 根 K 线（升序）
    q = (
        select(
            DailyBar.trade_date,
            DailyBar.open,
            DailyBar.high,
            DailyBar.low,
            DailyBar.close,
            DailyBar.volume,
        )
        .where(and_(DailyBar.symbol_id == int(symbol_id), DailyBar.trade_date > trade_date))
        .order_by(DailyBar.trade_date.asc())
        .limit(max_roll_days)
    )
    rows = list(db.execute(q).all())
    return resolve_next_open_bar(rows, prev_close=prev_close, max_roll_days=max_roll_days)


__all__ = [
    "MatchPriceResult",
    "MatchPriceRejection",
    "REJECTION_REASON_SUSPENDED",
    "REJECTION_REASON_LIMIT_UP_DOWN",
    "REJECTION_REASON_EXHAUSTED_ROLL_WINDOW",
    "resolve_next_open_bar",
    "get_match_price_next_open",
]
