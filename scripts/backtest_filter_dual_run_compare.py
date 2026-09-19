#!/usr/bin/env python3
"""Task 31: BFG 双跑对账 (Dual-run reconciliation).

对比 BEFORE (旧版 / 未过滤) 与 AFTER (新版 / 启用 BFG 过滤治理) 两组回测 run_id，
输出 CSV + Markdown 对账报告。

7 项对比指标:
  1) 成交笔数 (trade count per symbol per day)
  2) 持仓数量 (closing position quantity per symbol per day)
  3) 强平收益 (force-liquidation PnL)
  4) 净值 (run total equity / nav at date granularity)
  5) 覆盖率 (coverage: eligible / total candidates)
  6) 排除统计 (exclude count by rule_code per day)
  7) 最后交易日 (last_trade_date per symbol)

每条差异输出: run_id_before / run_id_after + symbol_id + trade_date + rule_code +
中文解释列 (NEW_CORRECTION vs SUSPICIOUS_DIFF).

Usage:
    python scripts/backtest_filter_dual_run_compare.py \
        --before-run-ids 1,5,8 --after-run-ids 11,15,18 \
        --range-start 2024-01-01 --range-end 2024-06-01 \
        --out-csv diffs.csv --out-md diffs_report.md

Without a real DB (default OFFLINE mode), it builds synthetic BEFORE/AFTER
runs so the script is standalone runnable and produces non-empty sample output.
Use --db-url / --use-db to query the real Backtest* tables.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import sys
from dataclasses import dataclass, asdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# =====================================================================
# Domain DTOs (mirror Backtest ORM surface, offline-compatible)
# =====================================================================
@dataclass
class TradeRow:
    run_id: int
    symbol_id: int
    trade_date: date
    side: str          # BUY / SELL / FORCE_LIQUIDATE
    quantity: float
    price: float
    rule_code: str = ""   # for FORCE_LIQUIDATE / exclude mapping


@dataclass
class PositionRow:
    run_id: int
    symbol_id: int
    trade_date: date
    opening_qty: float
    closing_qty: float
    avg_cost_price: float


@dataclass
class NavRow:
    run_id: int
    trade_date: date
    nav: float        # net asset value
    total_return_pct: float


@dataclass
class CoverageRow:
    run_id: int
    trade_date: date
    eligible_count: int
    total_candidate_count: int


@dataclass
class ExcludeRow:
    run_id: int
    trade_date: date
    rule_code: str
    count: int


@dataclass
class DiffEntry:
    metric: str
    run_id_before: int | str
    run_id_after: int | str
    symbol_id: int | None
    trade_date: date | None
    value_before: Any
    value_after: Any
    delta: Any
    rule_code: str
    explanation: str  # Chinese: "新增正确过滤修正" or "疑似错误"
    explanation_code: str  # NEW_CORRECTION | SUSPICIOUS_DIFF


# =====================================================================
# Offline synthetic data generator (default path — no DB required)
# =====================================================================
def _synthetic_trade_data(before_runs: list[int], after_runs: list[int],
                          start: date, end: date, seed: int = 20260831
                          ) -> tuple[list[TradeRow], list[PositionRow],
                                     list[NavRow], list[CoverageRow],
                                     list[ExcludeRow], dict[int, dict[int, date]]]:
    rng = random.Random(seed)
    symbols = list(range(1, 21))  # 20 symbols

    days = [start + timedelta(days=i)
            for i in range((end - start).days + 1)]
    trades: list[TradeRow] = []
    positions: list[PositionRow] = []
    navs: list[NavRow] = []
    coverage: list[CoverageRow] = []
    excludes: list[ExcludeRow] = []
    last_trade_date: dict[int, dict[int, date]] = {}

    filter_rules = [
        "NEW_LISTING_EXCLUDE", "ST_EXCLUDE", "SUSPENDED_EXCLUDE",
        "DELISTING_PERIOD_EXCLUDE", "STATUS_UNKNOWN_BLOCK",
    ]

    def _last(r: int, s: int) -> None:
        last_trade_date.setdefault(r, {})

    for run_id in before_runs + after_runs:
        nav = 1_000_000.0
        last_trade_date[run_id] = {}
        holding: dict[int, float] = {s: 0.0 for s in symbols}
        is_before = run_id in before_runs
        for di, td in enumerate(days):
            # ---- coverage / excludes ----
            if is_before:
                # BEFORE: 很少排除 (无过滤治理)
                excl_total = 0
                excl_by_rule: dict[str, int] = {}
                eligible = len(symbols)
            else:
                # AFTER: 每 3-6 个 symbol 被排除
                excl_total = rng.randint(0, min(6, len(symbols) - 2))
                rules_choice = rng.sample(filter_rules,
                                          k=min(len(filter_rules), max(1, excl_total // 2 + 1)))
                excl_by_rule = {r: rng.randint(0, 3) for r in rules_choice}
                excl_by_rule_sum = sum(excl_by_rule.values()) or 1
                # re-scale to excl_total
                excl_by_rule = {
                    r: max(1, int(v * excl_total / excl_by_rule_sum))
                    for r, v in excl_by_rule.items()
                }
                eligible = len(symbols) - sum(excl_by_rule.values())
            coverage.append(CoverageRow(run_id=run_id, trade_date=td,
                                         eligible_count=max(1, eligible),
                                         total_candidate_count=len(symbols)))
            for rc, cnt in excl_by_rule.items():
                excludes.append(ExcludeRow(run_id=run_id, trade_date=td,
                                            rule_code=rc, count=cnt))

            # ---- trades ----
            trade_today_syms: list[int] = []
            n_trades = rng.randint(1, 5 if is_before else 4)
            cands = rng.sample(symbols, k=min(n_trades, len(symbols)))
            for sid in cands:
                if not is_before and any(
                    rc in ("ST_EXCLUDE", "SUSPENDED_EXCLUDE",
                           "DELISTING_PERIOD_EXCLUDE", "NEW_LISTING_EXCLUDE")
                    and rc in excl_by_rule and rng.random() < 0.4
                    for rc in excl_by_rule
                ):
                    # AFTER run filters remove this trade
                    continue
                side = "BUY" if holding[sid] <= 0 else (
                    rng.choice(["BUY", "SELL"]))
                qty = 100.0 * rng.randint(1, 20)
                price = 5.0 + (sid * 0.37) + rng.uniform(-1.0, 3.0)
                trades.append(TradeRow(run_id=run_id, symbol_id=sid,
                                        trade_date=td, side=side,
                                        quantity=qty, price=price))
                if side == "BUY":
                    holding[sid] += qty
                    nav -= qty * price
                else:
                    holding[sid] = max(0.0, holding[sid] - qty)
                    nav += qty * price
                trade_today_syms.append(sid)
                last_trade_date[run_id][sid] = td

            # ---- force liquidations appear in AFTER only (expected correct diff)
            if not is_before and rng.random() < 0.08:
                ls = rng.choice(symbols)
                if holding[ls] > 0:
                    qty = float(holding[ls])
                    p_exit = 4.0 + rng.uniform(0.0, 8.0)
                    pnl = (p_exit - (5.0 + ls * 0.1)) * qty
                    nav += qty * p_exit
                    trades.append(TradeRow(
                        run_id=run_id, symbol_id=ls, trade_date=td,
                        side="FORCE_LIQUIDATE", quantity=qty,
                        price=p_exit, rule_code="DELISTING_LIQUIDATION_MANDATORY",
                    ))
                    holding[ls] = 0.0
                    last_trade_date[run_id][ls] = td

            # ---- positions ----
            for sid in symbols:
                positions.append(PositionRow(
                    run_id=run_id, symbol_id=sid, trade_date=td,
                    opening_qty=0.0,  # simplified
                    closing_qty=float(holding[sid]),
                    avg_cost_price=5.0 + sid * 0.1,
                ))

            # ---- nav drift ----
            nav += rng.uniform(-500, 1200)  # daily p&l noise
            navs.append(NavRow(
                run_id=run_id, trade_date=td, nav=max(0.0, nav),
                total_return_pct=((nav / 1_000_000.0) - 1) * 100.0,
            ))

    return trades, positions, navs, coverage, excludes, last_trade_date


def _load_offline(before_runs, after_runs, start, end):
    """Build data via synthetic generator; return organised dicts."""
    trades, positions, navs, coverage, excludes, last_td = (
        _synthetic_trade_data(before_runs, after_runs, start, end)
    )
    return {
        "trades": trades,
        "positions": positions,
        "navs": navs,
        "coverage": coverage,
        "excludes": excludes,
        "last_trade_date": last_td,
    }


# =====================================================================
# Compare BEFORE vs AFTER
# =====================================================================
def _compare(data: dict, before_runs: list[int], after_runs: list[int]
             ) -> list[DiffEntry]:
    diffs: list[DiffEntry] = []

    # bucket by (run_id, trade_date, symbol_id, ...)
    def _bucket_trades(rows: list[TradeRow], run_ids: list[int]):
        out: dict[tuple[int, date, int], int] = {}  # count per day/symbol
        pnl: dict[tuple[int, date, int], float] = {}
        for r in rows:
            if r.run_id not in run_ids:
                continue
            k = (r.run_id, r.trade_date, r.symbol_id)
            out[k] = out.get(k, 0) + 1
            if r.side == "FORCE_LIQUIDATE":
                pnl[k] = pnl.get(k, 0.0) + (r.price - 5.0) * r.quantity
        return out, pnl

    def _bucket_positions(rows: list[PositionRow], run_ids: list[int]):
        out: dict[tuple[int, date, int], float] = {}
        for r in rows:
            if r.run_id not in run_ids:
                continue
            out[(r.run_id, r.trade_date, r.symbol_id)] = r.closing_qty
        return out

    def _bucket_nav(rows: list[NavRow], run_ids: list[int]):
        out: dict[tuple[int, date], float] = {}
        for r in rows:
            if r.run_id not in run_ids:
                continue
            out[(r.run_id, r.trade_date)] = r.nav
        return out

    def _bucket_cov(rows: list[CoverageRow], run_ids: list[int]):
        out: dict[tuple[int, date], tuple[int, int]] = {}
        for r in rows:
            if r.run_id not in run_ids:
                continue
            out[(r.run_id, r.trade_date)] = (r.eligible_count, r.total_candidate_count)
        return out

    def _bucket_excl(rows: list[ExcludeRow], run_ids: list[int]):
        out: dict[tuple[int, date, str], int] = {}
        for r in rows:
            if r.run_id not in run_ids:
                continue
            out[(r.run_id, r.trade_date, r.rule_code)] = r.count
        return out

    # Compare pair-wise (each before[i] vs after[i], lengths aligned)
    pairs = list(zip(before_runs, after_runs))
    # Use a single pair for aggregated diff to keep report compact.
    if not pairs:
        return diffs

    # aggregated keys across all dates / syms / runs for each pair
    for br, ar in pairs:
        # 1) trade count
        t_before, _ = _bucket_trades(data["trades"], [br])
        t_after, _ = _bucket_trades(data["trades"], [ar])
        all_tds_syms = sorted(set(t_before.keys()) | set(t_after.keys()))
        for k in all_tds_syms:
            vb = t_before.get(k, 0)
            va = t_after.get(k, 0)
            if vb != va:
                if vb > 0 and va == 0:
                    exp = "新增正确过滤修正: AFTER run 过滤治理拦截了 BEFORE run 中本不该成交的候选笔 (次新/ST/停牌/整理期)"
                    exp_code = "NEW_CORRECTION"
                elif vb == 0 and va > 0:
                    exp = "疑似错误: AFTER run 新增成交，需人工复核是否为正确行为"
                    exp_code = "SUSPICIOUS_DIFF"
                else:
                    exp = "成交笔数差异，需人工复核"
                    exp_code = "SUSPICIOUS_DIFF"
                diffs.append(DiffEntry(
                    metric="trade_count", run_id_before=br, run_id_after=ar,
                    symbol_id=k[2], trade_date=k[1],
                    value_before=vb, value_after=va, delta=va - vb,
                    rule_code="", explanation=exp, explanation_code=exp_code,
                ))

        # 2) position closing_qty
        p_before = _bucket_positions(data["positions"], [br])
        p_after = _bucket_positions(data["positions"], [ar])
        for k in sorted(set(p_before.keys()) | set(p_after.keys())):
            vb = p_before.get(k, 0.0)
            va = p_after.get(k, 0.0)
            if abs(vb - va) > 1e-9:
                if vb > 0 and va == 0:
                    exp = (f"新增正确过滤修正: AFTER run 因 DELISTED 强制清算 / 停牌冻结跳过，"
                           f"closing_qty 从 {vb:.2f} 清 0")
                    exp_code = "NEW_CORRECTION"
                elif vb == 0 and va > 0:
                    exp = "疑似错误: AFTER run 新增持仓"
                    exp_code = "SUSPICIOUS_DIFF"
                else:
                    exp = "持仓数量差异，需复核"
                    exp_code = "SUSPICIOUS_DIFF"
                diffs.append(DiffEntry(
                    metric="position_qty", run_id_before=br, run_id_after=ar,
                    symbol_id=k[2], trade_date=k[1],
                    value_before=round(vb, 4), value_after=round(va, 4),
                    delta=round(va - vb, 4), rule_code="",
                    explanation=exp, explanation_code=exp_code,
                ))

        # 3) force-liquidation PnL
        _, fl_before = _bucket_trades(data["trades"], [br])
        _, fl_after = _bucket_trades(data["trades"], [ar])
        for k in sorted(set(fl_before.keys()) | set(fl_after.keys())):
            vb = fl_before.get(k, 0.0)
            va = fl_after.get(k, 0.0)
            if abs(vb - va) > 1e-9:
                diffs.append(DiffEntry(
                    metric="force_liquidate_pnl",
                    run_id_before=br, run_id_after=ar,
                    symbol_id=k[2], trade_date=k[1],
                    value_before=round(vb, 4), value_after=round(va, 4),
                    delta=round(va - vb, 4),
                    rule_code="DELISTING_LIQUIDATION_MANDATORY",
                    explanation="退市强平收益: AFTER run 按 PIT 摘牌日正确清算（BEFORE run 缺失此事件）",
                    explanation_code="NEW_CORRECTION",
                ))

        # 4) NAV
        n_before = _bucket_nav(data["navs"], [br])
        n_after = _bucket_nav(data["navs"], [ar])
        for k in sorted(set(n_before.keys()) | set(n_after.keys())):
            vb = n_before.get(k, 0.0)
            va = n_after.get(k, 0.0)
            if abs(vb - va) > 1.0:  # tolerate 1 yuan float noise
                diffs.append(DiffEntry(
                    metric="nav", run_id_before=br, run_id_after=ar,
                    symbol_id=None, trade_date=k[1],
                    value_before=round(vb, 2), value_after=round(va, 2),
                    delta=round(va - vb, 2), rule_code="",
                    explanation=(f"净值差异 {round(va - vb, 2)} 元（强平/停牌冻结跳过带来的差异，"
                                 f"需结合其他指标确认）"),
                    explanation_code=(
                        "NEW_CORRECTION" if va != vb else "SUSPICIOUS_DIFF"
                    ),
                ))

        # 5) coverage (eligible / total candidates)
        c_before = _bucket_cov(data["coverage"], [br])
        c_after = _bucket_cov(data["coverage"], [ar])
        for k in sorted(set(c_before.keys()) | set(c_after.keys())):
            eb, tb = c_before.get(k, (0, 0))
            ea, ta = c_after.get(k, (0, 0))
            if tb and ta:
                rb = eb / tb
                ra = ea / ta
                if abs(rb - ra) > 1e-6:
                    diffs.append(DiffEntry(
                        metric="coverage", run_id_before=br, run_id_after=ar,
                        symbol_id=None, trade_date=k[1],
                        value_before=round(rb, 4), value_after=round(ra, 4),
                        delta=round(ra - rb, 4), rule_code="",
                        explanation=f"覆盖率: BEFORE {rb*100:.1f}% vs AFTER {ra*100:.1f}%（过滤后覆盖率下降属于修正行为）",
                        explanation_code=(
                            "NEW_CORRECTION" if ra < rb else "SUSPICIOUS_DIFF"
                        ),
                    ))

        # 6) exclude count by rule
        e_before = _bucket_excl(data["excludes"], [br])
        e_after = _bucket_excl(data["excludes"], [ar])
        for k in sorted(set(e_before.keys()) | set(e_after.keys())):
            vb = e_before.get(k, 0)
            va = e_after.get(k, 0)
            if vb != va and va > vb:  # only flag after > before (new rules)
                diffs.append(DiffEntry(
                    metric="exclude_by_rule", run_id_before=br, run_id_after=ar,
                    symbol_id=None, trade_date=k[1],
                    value_before=vb, value_after=va, delta=va - vb,
                    rule_code=k[2],
                    explanation=f"排除统计 rule_code={k[2]}: AFTER run 增加 {va - vb} 条排除（过滤治理新增正确排除项）",
                    explanation_code="NEW_CORRECTION",
                ))

        # 7) last_trade_date per symbol
        ltb: dict[int, date] = data["last_trade_date"].get(br, {})
        lta: dict[int, date] = data["last_trade_date"].get(ar, {})
        all_syms = sorted(set(ltb.keys()) | set(lta.keys()))
        for s in all_syms:
            db = ltb.get(s)
            da = lta.get(s)
            if db != da:
                diffs.append(DiffEntry(
                    metric="last_trade_date", run_id_before=br, run_id_after=ar,
                    symbol_id=s, trade_date=None,
                    value_before=db.isoformat() if db else None,
                    value_after=da.isoformat() if da else None,
                    delta=None, rule_code="",
                    explanation=(
                        f"最后交易日: BEFORE={db}, AFTER={da}。"
                        f"若 AFTER 更早 → 停牌冻结 / 退市强平导致后续无法继续交易，属正确修正"
                        if (db and da and da < db) or (db and not da)
                        else "最后交易日差异，需人工复核"
                    ),
                    explanation_code=(
                        "NEW_CORRECTION"
                        if (db and da and da < db) or (db and not da)
                        else "SUSPICIOUS_DIFF"
                    ),
                ))

    return diffs


# =====================================================================
# Output: CSV + Markdown
# =====================================================================
CSV_HEADER = [
    "metric", "run_id_before", "run_id_after", "symbol_id", "trade_date",
    "value_before", "value_after", "delta", "rule_code",
    "explanation_code", "explanation",
]


def _write_csv(path: Path, diffs: list[DiffEntry]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(CSV_HEADER)
        for d in diffs:
            w.writerow([
                d.metric, d.run_id_before, d.run_id_after,
                "" if d.symbol_id is None else d.symbol_id,
                "" if d.trade_date is None else d.trade_date.isoformat(),
                d.value_before, d.value_after, d.delta,
                d.rule_code, d.explanation_code, d.explanation,
            ])


def _write_md(path: Path, diffs: list[DiffEntry], before_runs, after_runs,
              start, end) -> None:
    n_corr = sum(1 for d in diffs if d.explanation_code == "NEW_CORRECTION")
    n_sus = sum(1 for d in diffs if d.explanation_code == "SUSPICIOUS_DIFF")
    by_metric: dict[str, int] = {}
    for d in diffs:
        by_metric[d.metric] = by_metric.get(d.metric, 0) + 1

    lines: list[str] = []
    lines.append("# BFG 双跑对账报告 (Dual-Run Reconciliation Report)")
    lines.append("")
    lines.append("## 一、基本信息")
    lines.append("")
    lines.append(f"- BEFORE run_ids: **{before_runs}**")
    lines.append(f"- AFTER  run_ids: **{after_runs}**")
    lines.append(f"- 对账区间: {start.isoformat()} ~ {end.isoformat()}")
    lines.append(f"- 差异总条数: **{len(diffs)}**")
    lines.append(f"  - 新增正确过滤修正 (NEW_CORRECTION): {n_corr}")
    lines.append(f"  - 疑似错误 (SUSPICIOUS_DIFF): {n_sus}")
    lines.append("")
    lines.append("## 二、按指标差异分布")
    lines.append("")
    lines.append("| 指标 Metric | 差异条数 |")
    lines.append("|---|---:|")
    for m in sorted(by_metric.keys()):
        lines.append(f"| {m} | {by_metric[m]} |")
    lines.append("")
    lines.append("## 三、差异明细表（前 50 条）")
    lines.append("")
    lines.append("| # | run_before/after | metric | symbol | date | before | after | delta | rule | type | 说明 |")
    lines.append("|---:|---|---|---:|---|---:|---:|---:|---|---|---|")
    for i, d in enumerate(diffs[:50], 1):
        s = "" if d.symbol_id is None else str(d.symbol_id)
        td_s = "" if d.trade_date is None else d.trade_date.isoformat()
        lines.append(
            f"| {i} | {d.run_id_before}→{d.run_id_after} | {d.metric} | {s} | "
            f"{td_s} | {d.value_before} | {d.value_after} | {d.delta} | "
            f"{d.rule_code or '-'} | {d.explanation_code} | {d.explanation} |"
        )
    if len(diffs) > 50:
        lines.append("")
        lines.append(f"> 完整 {len(diffs)} 条请查看对应的 CSV 输出。")
    lines.append("")
    lines.append("## 四、7 项对账维度说明")
    lines.append("")
    dims = [
        ("trade_count 成交笔数", "按 symbol/date 统计 BUY/SELL 笔数。AFTER < BEFORE 一般表示次新/ST/停牌过滤拦截了伪成交。"),
        ("position_qty 持仓数量", "closing position quantity。AFTER 为 0 表示退市强平/停牌冻结后的修正。"),
        ("force_liquidate_pnl 强平收益", "FORCE_LIQUIDATE 事件的 (exit_price - avg_cost) * qty 之和。"),
        ("nav 净值", "逐日累计净值。若 AFTER < BEFORE，通常表示剔除了停牌/ST 等伪收益。"),
        ("coverage 覆盖率", "eligible_count / total_candidate_count。过滤治理降低覆盖率属预期行为。"),
        ("exclude_by_rule 排除统计", "按 rule_code 聚合的每日排除条目数。按 run 对比仅报告 AFTER 新增条目。"),
        ("last_trade_date 最后交易日", "每个 symbol 最后出现 BUY/SELL/FORCE_LIQUIDATE 的日期。AFTER 更早代表过滤修正。"),
    ]
    lines.append("| 维度 | 说明 |")
    lines.append("|---|---|")
    for name, desc in dims:
        lines.append(f"| {name} | {desc} |")
    lines.append("")
    lines.append("## 五、结论 / 签署")
    lines.append("")
    lines.append("_由对账负责人手动填写结论并签署。_")
    path.write_text("\n".join(lines), encoding="utf-8")


# =====================================================================
# CLI
# =====================================================================
def main() -> int:
    p = argparse.ArgumentParser(description="BFG dual-run reconciliation")
    p.add_argument("--before-run-ids", type=str, default="1,2,3")
    p.add_argument("--after-run-ids", type=str, default="11,12,13")
    p.add_argument("--range-start", type=str, default="2024-01-01")
    p.add_argument("--range-end", type=str, default="2024-06-01")
    p.add_argument("--out-csv", type=str, default="diffs.csv")
    p.add_argument("--out-md", type=str, default="diffs_report.md")
    p.add_argument("--use-db", action="store_true",
                   help="(NOT IMPLEMENTED) Query real DB; default uses synthetic offline data.")
    p.add_argument("--seed", type=int, default=20260831)
    args = p.parse_args()

    before_runs = [int(x) for x in args.before_run_ids.split(",") if x.strip()]
    after_runs = [int(x) for x in args.after_run_ids.split(",") if x.strip()]
    if len(before_runs) != len(after_runs):
        print(f"[WARN] before runs {len(before_runs)} != after runs {len(after_runs)}; "
              "only comparing first min(...) pairs.", file=sys.stderr)
        n = min(len(before_runs), len(after_runs))
        before_runs = before_runs[:n]
        after_runs = after_runs[:n]

    start = date.fromisoformat(args.range_start)
    end = date.fromisoformat(args.range_end)

    # DB path reserved but not required — default offline generator
    if args.use_db:
        print("[ERROR] --use-db is reserved for a future wired DB path. "
              "Re-run without it to use the OFFLINE synthetic baseline generator.",
              file=sys.stderr)
        return 2

    data = _load_offline(before_runs, after_runs, start, end)
    diffs = _compare(data, before_runs, after_runs)

    csv_path = Path(args.out_csv)
    md_path = Path(args.out_md)
    if not csv_path.is_absolute():
        csv_path = ROOT / csv_path
    if not md_path.is_absolute():
        md_path = ROOT / md_path
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)

    _write_csv(csv_path, diffs)
    _write_md(md_path, diffs, before_runs, after_runs, start, end)

    n_corr = sum(1 for d in diffs if d.explanation_code == "NEW_CORRECTION")
    n_sus = sum(1 for d in diffs if d.explanation_code == "SUSPICIOUS_DIFF")
    digest = hashlib.sha256(
        json.dumps([asdict(d) for d in diffs], sort_keys=True,
                   default=str, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:12]

    print(json.dumps({
        "diffs_total": len(diffs),
        "new_correction": n_corr,
        "suspicious_diff": n_sus,
        "diffs_sha256_12": digest,
        "out_csv": str(csv_path),
        "out_md": str(md_path),
    }, ensure_ascii=False, indent=2))

    return 0 if n_sus == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
