"""portfolio-factor-backtest-full-linkage #3：五维守恒对账（纯函数实现）。

五维（Vector 1..5）：
  1. Decisions（T 日 BUY/SELL/HOLD 决策 × 目标价/量）
  2. OrderPlans（订单计划：按决策生成的计划订单）
  3. MatchResults（撮合结果：FILLED / PARTIAL_FILL / REJECTED）
  4. EndPositionsSnapshot（T 日收盘后或 T+1 开盘前的最终持仓快照）
  5. CashAndNav（期初/期末现金 + 收盘价做的 Σ 持仓价值，资产守恒校验）

R1 ~ R4 + 任意差异 → 统一 overall=BLOCKED（对应 pf-linkage spec 要求：任何 diff 统一 RECONCILIATION_BLOCKED FSM 状态）。

Seam 为纯函数：不依赖 Session/DB，接受 dict-like 或 dataclass 的输入行（和测试 fx 同形）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable, Literal, Sequence


DiffKind = Literal[
    "MISSING_ORDER_PLAN",
    "UNFILLED_PLAN",
    "POSITION_MISMATCH",
    "NAV_BROKEN",
]


@dataclass
class ConservationDiff:
    source_vector: Literal[
        "decisions_vs_order_plans",
        "order_plans_vs_match_results",
        "match_results_vs_end_positions",
        "nav_conservation",
    ]
    kind: DiffKind
    symbol_id: int | None = None
    expected: Any = None
    actual: Any = None
    detail: str | None = None


@dataclass
class FiveVectorConservationReport:
    overall: Literal["PASSED", "BLOCKED"]
    diffs: list[ConservationDiff] = field(default_factory=list)
    nav_start: float = 0.0
    nav_end: float = 0.0


# ────────────────────────────────────────────────────────────────────────────
# 字段统一取值（兼容 dataclass / dict / ORM-like 对像）
# ────────────────────────────────────────────────────────────────────────────
def _a(row: Any, name: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(name, default)
    return getattr(row, name, default)


def _get_symbol_id(x: Any) -> int | None:
    s = _a(x, "symbol_id")
    return None if s is None else int(s)


def _signed_fill_delta(match_leg: Any) -> float:
    """Match 结果对持仓的变化量（BUY +、SELL -），只算 FILLED/PARTIAL_FILL 的 filled_qty。"""
    status = str(_a(match_leg, "status", "") or "").upper()
    if status not in {"FILLED", "PARTIAL_FILL"}:
        return 0.0  # REJECTED / 未知态都认为"没填"
    filled = float(_a(match_leg, "filled_qty", 0.0) or 0.0)
    side = str(_a(match_leg, "side", "") or "").upper()
    sign = 1.0 if side == "BUY" else (-1.0 if side == "SELL" else 0.0)
    return sign * filled


def _plan_signed_qty(plan: Any) -> float:
    qty = float(_a(plan, "plan_qty", 0.0) or 0.0)
    side = str(_a(plan, "side", "") or "").upper()
    sign = 1.0 if side == "BUY" else (-1.0 if side == "SELL" else 0.0)
    return sign * qty


# ────────────────────────────────────────────────────────────────────────────
# 主 Seam
# ────────────────────────────────────────────────────────────────────────────
DiffSourceVec = Literal[
    "decisions_vs_order_plans",
    "order_plans_vs_match_results",
    "match_results_vs_end_positions",
    "nav_conservation",
]


def evaluate_five_vector_conservation(input: dict[str, Any]) -> FiveVectorConservationReport:
    trade_date: date = input["trade_date"]
    if not isinstance(trade_date, date):
        raise TypeError(f"trade_date must be date, got {type(trade_date).__name__}")

    start_cash = float(input.get("start_cash", 0.0) or 0.0)
    end_cash = float(input.get("end_cash", 0.0) or 0.0)
    start_positions: dict[int, float] = {
        int(sid): float(qty) for sid, qty in (input.get("start_positions") or {}).items()
    }
    end_positions: dict[int, float] = {
        int(sid): float(qty) for sid, qty in (input.get("end_positions") or {}).items()
    }
    end_prices: dict[int, float] = {
        int(sid): float(p) for sid, p in (input.get("end_prices") or {}).items()
    }
    decisions: Sequence[Any] = list(input.get("decisions") or [])
    order_plans: Sequence[Any] = list(input.get("order_plans") or [])
    match_results: Sequence[Any] = list(input.get("match_results") or [])
    tolerance = float(input.get("tolerance", 1e-6) or 1e-6)

    diffs: list[ConservationDiff] = []

    # ═══════════════════════════════════════════════════════════════════════
    # R1 / decisions vs order_plans：
    #  对每个 BUY/SELL 决策必须存在至少一条同 symbol_id + 同 side 的订单计划（不要求 1:1 数量匹配，数量匹配交给 R3）。
    #  HOLD 决策不要求订单计划（否则会误报 MISSING）。
    # ═══════════════════════════════════════════════════════════════════════
    plans_by_symbol_side: dict[tuple[int, str], list[Any]] = {}
    for p in order_plans:
        psid = _get_symbol_id(p)
        if psid is None:
            continue
        pside = str(_a(p, "side", "") or "").upper()
        if pside not in {"BUY", "SELL"}:
            continue
        plans_by_symbol_side.setdefault((psid, pside), []).append(p)

    for dec in decisions:
        dsid = _get_symbol_id(dec)
        daction = str(_a(dec, "action", "") or "").upper()
        if daction in {"BUY", "SELL"}:
            if dsid is None:
                continue
            target_qty = float(_a(dec, "target_qty", 0.0) or 0.0)
            if target_qty == 0.0 and daction == "BUY":
                # 目标量 0（= 真的不想买）→ 不应该报 MISSING_ORDER_PLAN
                continue
            if not plans_by_symbol_side.get((dsid, daction)):
                diffs.append(ConservationDiff(
                    source_vector="decisions_vs_order_plans",
                    kind="MISSING_ORDER_PLAN",
                    symbol_id=dsid,
                    expected={
                        "action": daction,
                        "target_qty": target_qty,
                        "price_ref": _a(dec, "price_ref"),
                    },
                    actual={
                        "order_plans_found": 0,
                    },
                    detail=(
                        f"Decision {daction} symbol_id={dsid} target_qty={target_qty} "
                        f"has no matching order_plan of same side"
                    ),
                ))

    # ═══════════════════════════════════════════════════════════════════════
    # R2 / order_plans vs match_results：
    #  每张订单计划，如果它的 match.status 不是 REJECTED（拒绝是正常不成交），则必须有实际成交（filled_qty > 0 或至少 PARTIAL/FILL）。
    #  关联方式：先按 order_plan_id；找不到 order_plan_id 时按 (symbol_id, side, plan_qty) 匹配。
    # ═══════════════════════════════════════════════════════════════════════
    matches_by_plan_id: dict[str, list[Any]] = {}
    for m in match_results:
        pid = _a(m, "order_plan_id")
        if pid:
            matches_by_plan_id.setdefault(str(pid), []).append(m)

    for p in order_plans:
        plan_id = str(_a(p, "order_plan_id") or "")
        plan_side = str(_a(p, "side", "") or "").upper()
        plan_qty = float(_a(p, "plan_qty", 0.0) or 0.0)
        plan_sid = _get_symbol_id(p)
        matched_ms: list[Any] = matches_by_plan_id.get(plan_id, [])
        if not matched_ms:
            # fallback: symbol + side + plan_qty exact search
            if plan_sid is not None:
                for m in match_results:
                    if (_get_symbol_id(m) == plan_sid
                            and str(_a(m, "side", "") or "").upper() == plan_side
                            and abs(float(_a(m, "filled_qty", 0.0) or 0.0) - plan_qty) <= max(1e-9, tolerance)
                    ):
                        matched_ms.append(m)
        if not matched_ms:
            # 没撮合结果关联 → 视为 UNFILLED_PLAN
            diffs.append(ConservationDiff(
                source_vector="order_plans_vs_match_results",
                kind="UNFILLED_PLAN",
                symbol_id=plan_sid,
                expected={"order_plan_id": plan_id, "plan_qty": plan_qty, "side": plan_side},
                actual={"match_results_found": 0},
                detail=(
                    f"order_plan_id={plan_id!r} of symbol_id={plan_sid} side={plan_side} "
                    f"has zero match_result bindings and no symbol fallback; not even REJECTED record"
                ),
            ))
            continue
        # 有匹配结果：不是 REJECTED（显式拒绝 OK）时，要求 signed filled_delta 总和非 0
        statuses = {str(_a(m, "status", "") or "").upper() for m in matched_ms}
        if "REJECTED" in statuses:
            # 被显式拒绝（合法拒单）→ 不记 UNFILLED_PLAN；调用方如果希望"决策拒绝也应进入证据"可在 DecisionEvidence 里记录
            continue
        total_filled = sum(abs(_signed_fill_delta(m)) for m in matched_ms)
        if total_filled <= tolerance:
            # PARTIAL_FILL 但 filled_qty == 0 → UNFILLED_PLAN
            diffs.append(ConservationDiff(
                source_vector="order_plans_vs_match_results",
                kind="UNFILLED_PLAN",
                symbol_id=plan_sid,
                expected={"plan_qty_abs": plan_qty, "need_filled_gt": 0},
                actual={"total_filled_abs": total_filled, "statuses": sorted(statuses)},
                detail=(
                    f"order_plan_id={plan_id!r} statuses={sorted(statuses)} "
                    f"total_filled_abs={total_filled} <= tolerance {tolerance}"
                ),
            ))

    # ═══════════════════════════════════════════════════════════════════════
    # R3 / match_results 对持仓的影响 → 对比 end_positions
    #  预期持仓 = start_positions + Σ（signed_fill_delta of each FILLED/PARTIAL match）
    #  对所有在 "start_positions 或 end_positions 或 match_results" 出现过的 symbol 做并集校验。
    # ═══════════════════════════════════════════════════════════════════════
    start_positions_float: dict[int, float] = {int(k): float(v) for k, v in start_positions.items()}
    expected_end = dict(start_positions_float)
    matched_symbols: set[int] = set()
    for m in match_results:
        sid = _get_symbol_id(m)
        if sid is None:
            continue
        matched_symbols.add(sid)
        delta = _signed_fill_delta(m)
        expected_end[sid] = expected_end.get(sid, 0.0) + delta

    all_symbols = set(start_positions_float) | set(end_positions) | matched_symbols
    for sid in sorted(all_symbols):
        exp = expected_end.get(sid, 0.0)
        act = end_positions.get(sid, 0.0)
        if abs(exp - act) > max(tolerance, tolerance * max(1.0, abs(exp), abs(act))):
            diffs.append(ConservationDiff(
                source_vector="match_results_vs_end_positions",
                kind="POSITION_MISMATCH",
                symbol_id=sid,
                expected={"end_position_qty": exp},
                actual={"end_position_qty": act},
                detail=(
                    f"symbol_id={sid} expected_end_qty={exp:.9f} actual_end_qty={act:.9f}; "
                    f"delta_expected_vs_actual={exp - act:.9f}"
                ),
            ))

    # ═══════════════════════════════════════════════════════════════════════
    # R4 / NAV 守恒（现金守恒实现）：
    #  "持仓因收盘价格波动导致的市值变化"属于正常投资收益，不是对账矛盾，因此 NAV 守恒
    #  以最坚实的现金守恒为判定基准（pf-linkage 注释第 216 行：NAV 上升不用对守恒负责）：
    #
    #    expected_end_cash = start_cash
    #                      + Σ（SELL 成交 filled_qty × avg_price）  （卖出收到现金）
    #                      - Σ（BUY  成交 filled_qty × avg_price）  （买入支出现金）
    #
    #    |expected_end_cash - actual_end_cash| / denom > tolerance → NAV_BROKEN
    #
    #  报告的 nav_start / nav_end 展示字段按"start_cash + Σ start_qty × end_price（对称）"
    #  和 "end_cash + Σ end_qty × end_price"填充（仅用于 T+1 连续追踪，不决定 PASSED）。
    # ═══════════════════════════════════════════════════════════════════════
    expected_end_cash = start_cash
    for m in match_results:
        status = str(_a(m, "status", "") or "").upper()
        if status not in {"FILLED", "PARTIAL_FILL"}:
            continue
        side = str(_a(m, "side", "") or "").upper()
        qty = abs(float(_a(m, "filled_qty", 0.0) or 0.0))
        if qty <= 0.0:
            continue
        avg = float(_a(m, "avg_price", 0.0) or 0.0)
        cash_flow = qty * avg
        if side == "BUY":
            expected_end_cash -= cash_flow
        elif side == "SELL":
            expected_end_cash += cash_flow
        # 非 BUY/SELL 忽略（等价于"没影响现金"）。

    denom_cash = max(1.0, abs(expected_end_cash), abs(end_cash))
    rel_cash = abs(expected_end_cash - end_cash) / denom_cash
    if rel_cash > tolerance:
        diffs.append(ConservationDiff(
            source_vector="nav_conservation",
            kind="NAV_BROKEN",
            symbol_id=None,
            expected={"end_cash_via_trades": expected_end_cash, "rel_tolerance": tolerance},
            actual={"end_cash": end_cash, "rel_diff": rel_cash},
            detail=(
                f"expected_end_cash(cash_conservation)={expected_end_cash:.6f} "
                f"actual_end_cash={end_cash:.6f} rel_diff={rel_cash:.9f} > tolerance={tolerance}"
            ),
        ))

    # 展示字段（纯展示，不影响 PASSED / BLOCKED）
    nav_start = start_cash
    for sid, q in start_positions_float.items():
        nav_start += float(q) * float(end_prices.get(sid, 0.0) or 0.0)
    nav_end = end_cash
    for sid, q in end_positions.items():
        nav_end += float(q) * float(end_prices.get(sid, 0.0) or 0.0)

    overall: Literal["PASSED", "BLOCKED"] = "PASSED" if not diffs else "BLOCKED"
    return FiveVectorConservationReport(
        overall=overall, diffs=diffs, nav_start=nav_start, nav_end=nav_end
    )
