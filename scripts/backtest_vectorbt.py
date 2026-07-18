"""Run a VectorBT research backtest against the configured project database."""
from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from sqlalchemy import select

from app.models.portfolio import Portfolio
from app.models.symbol import Symbol
from app.services.vectorbt_backtest import (
    SIGNAL_MODES,
    SCORE_VISIBILITY_MODES,
    VectorBTConfig,
    run_project_vectorbt_backtest,
)
from scripts.initialize_factor_data import _initialize_manager


def _normalized_symbol(value: str) -> str:
    text = str(value or "").strip().upper()
    if "." in text:
        text = text.split(".", 1)[0]
    if len(text) == 8 and text[:2] in {"SH", "SZ", "BJ"}:
        text = text[2:]
    return text


def _resolve_symbols(db, requested: list[str]) -> list[Symbol]:
    rows = db.execute(
        select(Symbol).where(Symbol.is_active == 1).order_by(Symbol.id)
    ).scalars().all()
    by_code: dict[str, Symbol] = {}
    for row in rows:
        by_code.setdefault(_normalized_symbol(row.symbol), row)
    result: list[Symbol] = []
    missing: list[str] = []
    for value in requested:
        row = by_code.get(_normalized_symbol(value))
        if row is None:
            missing.append(value)
        elif row.id not in {item.id for item in result}:
            result.append(row)
    if missing:
        raise ValueError(f"symbols not found: {', '.join(missing)}")
    if not result:
        raise ValueError("at least one symbol is required")
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "VectorBT multi-symbol research backtest using local project "
            "daily bars and optional Score history"
        )
    )
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--start-date", type=date.fromisoformat, required=True)
    parser.add_argument("--end-date", type=date.fromisoformat, required=True)
    parser.add_argument("--portfolio-id", type=int, default=1)
    parser.add_argument(
        "--signal-mode",
        choices=sorted(SIGNAL_MODES),
        default="score_trend",
    )
    parser.add_argument(
        "--score-weight-mode",
        choices=["manual", "ridge"],
        default="manual",
    )
    parser.add_argument("--factor-model-run-id")
    parser.add_argument(
        "--score-visibility-mode",
        choices=sorted(SCORE_VISIBILITY_MODES),
        default="strict",
        help="strict excludes scores created after their trade date",
    )
    parser.add_argument(
        "--score-max-age-days",
        type=int,
        default=5,
        help="maximum trading rows to forward-fill a visible Score",
    )
    parser.add_argument("--initial-cash", type=float)
    parser.add_argument("--fast-window", type=int, default=10)
    parser.add_argument("--slow-window", type=int, default=20)
    parser.add_argument("--quality-min", type=float, default=60.0)
    parser.add_argument("--timing-min", type=float, default=55.0)
    parser.add_argument("--quality-exit", type=float, default=45.0)
    parser.add_argument("--timing-exit", type=float, default=40.0)
    parser.add_argument("--position-pct", type=float, default=0.1)
    parser.add_argument(
        "--max-positions",
        type=int,
        default=10,
        help="daily Top-N entry candidates in score_trend mode",
    )
    parser.add_argument("--commission-rate", type=float, default=0.0003)
    parser.add_argument("--min-commission", type=float, default=5.0)
    parser.add_argument("--stamp-tax-rate", type=float, default=0.001)
    parser.add_argument("--slippage-rate", type=float, default=0.001)
    parser.add_argument("--stop-loss-pct", type=float)
    parser.add_argument("--take-profit-pct", type=float)
    parser.add_argument(
        "--execution-lag",
        type=int,
        default=1,
        help="shift signals by N trading rows; default 1 avoids same-close look-ahead",
    )
    parser.add_argument(
        "--enable-numba",
        action="store_true",
        help="enable Numba JIT; first startup can be slow on Windows",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="optional JSON output path",
    )
    return parser


def run(args: argparse.Namespace) -> dict:
    if args.start_date > args.end_date:
        raise ValueError("--start-date must not be after --end-date")
    manager = _initialize_manager()
    db = manager.get_session()
    try:
        portfolio = db.get(Portfolio, args.portfolio_id)
        if portfolio is None:
            raise ValueError(f"portfolio not found: {args.portfolio_id}")
        symbols = _resolve_symbols(db, args.symbols)
        initial_cash = (
            args.initial_cash
            if args.initial_cash is not None
            else float(portfolio.total_capital)
        )
        config = VectorBTConfig(
            signal_mode=args.signal_mode,
            initial_cash=initial_cash,
            fast_window=args.fast_window,
            slow_window=args.slow_window,
            quality_min=args.quality_min,
            timing_min=args.timing_min,
            quality_exit=args.quality_exit,
            timing_exit=args.timing_exit,
            position_pct=args.position_pct,
            max_positions=args.max_positions,
            commission_rate=args.commission_rate,
            min_commission=args.min_commission,
            stamp_tax_rate=args.stamp_tax_rate,
            slippage_rate=args.slippage_rate,
            stop_loss_pct=args.stop_loss_pct,
            take_profit_pct=args.take_profit_pct,
            execution_lag=args.execution_lag,
            disable_numba=not args.enable_numba,
            score_visibility_mode=args.score_visibility_mode,
            score_max_age_days=args.score_max_age_days,
        )
        result = run_project_vectorbt_backtest(
            db,
            symbol_ids=[item.id for item in symbols],
            start_date=args.start_date,
            end_date=args.end_date,
            config=config,
            score_weight_mode=args.score_weight_mode,
            factor_model_run_id=args.factor_model_run_id,
        )
        result["portfolio"] = {
            "id": portfolio.id,
            "name": portfolio.name,
        }
        return result
    finally:
        db.close()


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    try:
        result = run(args)
    except ValueError as exc:
        parser.error(str(exc))
    payload = json.dumps(
        result,
        ensure_ascii=False,
        indent=2,
        allow_nan=False,
    )
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + chr(10), encoding="utf-8")
    print(payload)


if __name__ == "__main__":
    main()
