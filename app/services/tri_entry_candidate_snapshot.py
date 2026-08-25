"""portfolio-factor-backtest-full-linkage #4：三入口候选池快照（纯函数实现）。

入口差异（pf-linkage tasks.md N5）：
  ① dry_run          → 今天 today 的候选快照（前台手动试跑入口，不能"挑日期"绕过）
  ② backtest         → backtest_trade_date 当日精确 SCD2（不能用 today，正式 PIT 直接 BLOCKED）
  ③ auto_simulation  → auto_sim_trade_date 的候选快照（20:30 每日跑 T 日用 T 日快照，不是 today）

SCD2 选取规则：
  1. effective_from ≤ ref_date 且（effective_to is None 或 ref_date ≤ effective_to）→ 行在该日期"生效"
  2. 对同一 symbol_id 可能有多个生效行（同日多次变更的 SCD2 "当日合并最终态"规则，出现不同 audit_version 时）
     → 选 audit_version 最大的那一行
"""
from __future__ import annotations

from datetime import date
from typing import Any, Iterable, Literal, Sequence, TypeVar


EntryPoint = Literal["dry_run", "backtest", "auto_simulation"]
VALID_ENTRY_POINTS: frozenset[str] = frozenset({"dry_run", "backtest", "auto_simulation"})


T = TypeVar("T")


def _attr(row: Any, name: str, default: Any = None) -> Any:
    """兼容 dataclass / ORM 对象 / Mapping：按属性名取值。"""
    if isinstance(row, dict):
        return row.get(name, default)
    val = getattr(row, name, default)
    if val is None:
        # ORM Mapped 列在某些构造是 None，再尝试用 dict 形式兜底
        if isinstance(row, dict):
            return row.get(name, default)
    return val


def _effective_on(row: Any, ref_date: date) -> bool:
    ef = _attr(row, "effective_from")
    et = _attr(row, "effective_to")
    if ef is None:
        return False
    if not isinstance(ef, date):
        raise TypeError(
            f"row.effective_from must be date; got {type(ef).__name__}: {ef!r}"
        )
    if ef > ref_date:
        return False
    if et is None:
        return True
    if not isinstance(et, date):
        raise TypeError(
            f"row.effective_to must be date|None; got {type(et).__name__}: {et!r}"
        )
    return ref_date <= et


def _audit_version(row: Any) -> int:
    v = _attr(row, "audit_version")
    if v is None:
        return 0
    try:
        return int(v)
    except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
        raise ValueError(f"audit_version must be int-like: {v!r}") from exc


def _symbol_id(row: Any) -> int:
    sid = _attr(row, "symbol_id")
    if sid is None:
        raise ValueError("row.symbol_id is required")
    return int(sid)


# ────────────────────────────────────────────────────────────────────────────
# Seam：三入口候选池快照取数（纯函数）
# ────────────────────────────────────────────────────────────────────────────
def pick_candidate_snapshot_rows(
    candidate_rows: Sequence[T] | Iterable[T],
    entry_point: EntryPoint,
    *,
    today: date,
    backtest_trade_date: date | None = None,
    auto_sim_trade_date: date | None = None,
) -> list[T]:
    if entry_point not in VALID_ENTRY_POINTS:
        raise ValueError(
            f"entry_point must be one of {sorted(VALID_ENTRY_POINTS)!r}; got {entry_point!r}"
        )
    if not isinstance(today, date):
        raise TypeError(f"today must be datetime.date; got {type(today).__name__}")

    # 根据入口确定"参考日期 ref_date"
    if entry_point == "dry_run":
        # dry_run 只能用 today（其他日期参数如果多传也一律忽略，避免"日期绕过"漏洞）
        ref_date = today
    elif entry_point == "backtest":
        if backtest_trade_date is None:
            raise ValueError(
                "entry_point='backtest' requires keyword argument 'backtest_trade_date': date"
            )
        if not isinstance(backtest_trade_date, date):
            raise TypeError(
                f"backtest_trade_date must be datetime.date; got {type(backtest_trade_date).__name__}"
            )
        ref_date = backtest_trade_date
    else:  # auto_simulation
        if auto_sim_trade_date is None:
            raise ValueError(
                "entry_point='auto_simulation' requires keyword argument 'auto_sim_trade_date': date"
            )
        if not isinstance(auto_sim_trade_date, date):
            raise TypeError(
                f"auto_sim_trade_date must be datetime.date; got {type(auto_sim_trade_date).__name__}"
            )
        ref_date = auto_sim_trade_date

    # 1) 先过滤当日生效的行
    effective: list[tuple[int, int, T]] = []  # (symbol_id, audit_version, row)
    for row in candidate_rows:
        if not _effective_on(row, ref_date):
            continue
        effective.append((_symbol_id(row), _audit_version(row), row))

    # 2) 按 symbol_id 分组；多版本时保留 audit_version 最大的一行
    best_by_symbol: dict[int, tuple[int, T]] = {}
    for sid, ver, row in effective:
        cur = best_by_symbol.get(sid)
        if cur is None or ver > cur[0]:
            best_by_symbol[sid] = (ver, row)

    return [row for _ver, row in best_by_symbol.values()]
