"""Risk-event bridge for simulated portfolios."""

from __future__ import annotations

import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.notification import NotificationOutbox
from app.models.portfolio import Portfolio, Position
from app.models.portfolio_equity_snapshot import PortfolioEquitySnapshot
from app.models.symbol import Symbol
from app.services.allocation import get_active_rule
from app.services.notifications.event_emitter import (
    emit_drawdown_warning,
    emit_max_loss_warning,
)


def _ratio(value: object) -> float:
    try:
        ratio = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    if ratio > 1:
        ratio /= 100.0
    return max(0.0, ratio)


def _event_exists(db: Session, event_key: str) -> bool:
    return (
        db.execute(
            select(NotificationOutbox.id)
            .where(NotificationOutbox.event_key == event_key)
            .limit(1)
        ).scalar_one_or_none()
        is not None
    )


def evaluate_snapshot_risk_notifications(
    db: Session,
    *,
    portfolio: Portfolio,
    snapshot: PortfolioEquitySnapshot,
) -> dict[str, int]:
    """Emit current-position loss and portfolio drawdown warnings.

    A stable key makes a repeated snapshot run idempotent for the same day.
    No outbox row is created when no policy matches, so enabling a policy and
    rerunning the snapshot can still deliver the current warning.
    """
    rule = get_active_rule(db, portfolio.id)
    if rule is None:
        return {"max_loss": 0, "drawdown": 0}

    emitted_loss = 0
    emitted_drawdown = 0
    max_loss_ratio = _ratio(rule.max_loss_per_trade_pct)

    if max_loss_ratio > 0:
        rows = db.execute(
            select(Position, Symbol)
            .join(Symbol, Symbol.id == Position.symbol_id)
            .where(
                Position.portfolio_id == portfolio.id,
                Position.quantity > 0,
            )
        ).all()
        for position, symbol in rows:
            avg_cost = float(position.avg_cost or 0)
            current_price = float(position.latest_price or 0)
            if avg_cost <= 0 or current_price <= 0:
                continue
            loss_ratio = (avg_cost - current_price) / avg_cost
            if loss_ratio < max_loss_ratio:
                continue

            event_key = (
                f"portfolio:max_loss_warning:position:{position.id}:"
                f"{snapshot.snapshot_date.isoformat()}"
            )
            if _event_exists(db, event_key):
                continue
            emitted_loss += len(
                emit_max_loss_warning(
                    db,
                    portfolio_id=portfolio.id,
                    source_id=position.id,
                    symbol=symbol.symbol,
                    loss_pct=loss_ratio * 100,
                    threshold_pct=max_loss_ratio * 100,
                    current_price=current_price,
                    avg_cost=avg_cost,
                    event_key=event_key,
                )
            )

    try:
        stage_limits = json.loads(rule.stage_limits_json or "{}")
    except (TypeError, json.JSONDecodeError):
        stage_limits = {}
    drawdown_ratio = _ratio(stage_limits.get("drawdown_circuit_pct"))
    if drawdown_ratio > 0:
        historical_peak = db.execute(
            select(func.max(PortfolioEquitySnapshot.total_equity)).where(
                PortfolioEquitySnapshot.portfolio_id == portfolio.id
            )
        ).scalar_one_or_none()
        peak = max(
            float(portfolio.total_capital or 0),
            float(historical_peak or 0),
        )
        current_equity = float(snapshot.total_equity or 0)
        current_drawdown = (peak - current_equity) / peak if peak > 0 else 0.0

        if current_drawdown >= drawdown_ratio:
            event_key = f"portfolio:drawdown_warning:snapshot:{snapshot.id}"
            if not _event_exists(db, event_key):
                emitted_drawdown += len(
                    emit_drawdown_warning(
                        db,
                        portfolio_id=portfolio.id,
                        drawdown_pct=current_drawdown * 100,
                        threshold_pct=drawdown_ratio * 100,
                        current_value=current_equity,
                        event_key=event_key,
                    )
                )

    return {"max_loss": emitted_loss, "drawdown": emitted_drawdown}


__all__ = ["evaluate_snapshot_risk_notifications"]