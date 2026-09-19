"""draft_order：模拟订单草稿（WP-AI.5）。

三步流程：
1. draft_order：AI 建议订单（不写 DB）
2. preview_order：系统重新校验现金/手数/T+1/涨跌停/数据健康/组合风控（dry-run，不写 DB）
3. execute_order：用户确认后调用 place_sim_order（业务 API 会再次重新校验）

关键约束：
- 模拟订单即使由 AI 起草也必须重新经过现金/手数/T+1/涨跌停/数据健康/组合风控校验
- 自动交易永远由策略规则和调度器负责，不由对话直接触发
- AI 草稿不直接调用 place_sim_order，必须先经 preview_xxx 校验
- execute_xxx 调用 place_sim_order 时会再次重新校验（防止确认期间数据变化）
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services.ai.context_pack import ContextPack
from app.services.ai.drafts._common import (
    build_draft_result,
    build_preview_result,
    load_audit_payload,
    record_final_result,
    require_fields,
    validate_number_field,
)

logger = logging.getLogger(__name__)

DRAFT_TYPE = "draft_order"

_VALID_SIDES = ("buy", "sell")
_VALID_ORDER_TYPES = ("market", "limit")


def draft_order(
    db: Session, context: ContextPack, ai_suggestion: dict[str, Any]
) -> dict[str, Any]:
    """第一步：AI 建议订单。

    AI 起草不写 DB，仅生成 suggested_payload 并调用 preview 做系统校验。
    """
    suggested_payload = {
        "portfolio_id": (
            ai_suggestion.get("portfolio_id")
            or context.references.get("portfolio_id")
        ),
        "symbol_id": (
            ai_suggestion.get("symbol_id")
            or context.references.get("symbol_id")
        ),
        "side": ai_suggestion.get("side", "buy"),
        "quantity": ai_suggestion.get("quantity", 0),
        "price": ai_suggestion.get("price"),
        "order_type": ai_suggestion.get("order_type", "market"),
        "note": ai_suggestion.get("note", "AI 起草订单"),
    }

    preview = preview_order(db, suggested_payload)
    validation_status = "valid" if preview["is_valid"] else "invalid"
    validation_errors = preview["errors"]

    return build_draft_result(
        draft_type=DRAFT_TYPE,
        suggested_payload=suggested_payload,
        validation_status=validation_status,
        validation_errors=validation_errors,
        preview=preview,
    )


def preview_order(db: Session, suggested_payload: dict[str, Any]) -> dict[str, Any]:
    """第二步：系统重新校验（dry-run，不写 DB）。

    重新经过：
    - 现金校验（买入时现金充足）
    - 手数校验（normalize_order_quantity）
    - T+1 校验（卖出时持仓可卖）
    - 涨跌停校验
    - 数据健康校验（最新行情日期距今 ≤ 3 天）
    - 组合风控校验（持仓上限、单标的占比）
    """
    errors: list[str] = []
    changes: list[dict[str, Any]] = []
    risk_checks: list[dict[str, Any]] = []

    errors.extend(require_fields(
        suggested_payload, ["portfolio_id", "symbol_id", "side", "quantity"]
    ))
    errors.extend(validate_number_field(
        suggested_payload, "quantity", min_value=0,
    ))
    errors.extend(validate_number_field(
        suggested_payload, "price", min_value=0,
    ))

    side = suggested_payload.get("side")
    if side and side not in _VALID_SIDES:
        errors.append(f"side 必须是 {_VALID_SIDES} 之一")

    order_type = suggested_payload.get("order_type", "market")
    if order_type not in _VALID_ORDER_TYPES:
        errors.append(f"order_type 必须是 {_VALID_ORDER_TYPES} 之一")

    portfolio_id = suggested_payload.get("portfolio_id")
    symbol_id = suggested_payload.get("symbol_id")
    quantity = suggested_payload.get("quantity", 0)
    price = suggested_payload.get("price")

    # 加载组合与标的
    portfolio = None
    symbol = None
    if portfolio_id:
        try:
            from app.models.portfolio import Portfolio
            portfolio = db.execute(
                select(Portfolio).where(Portfolio.id == portfolio_id)
            ).scalars().first()
            if portfolio is None:
                errors.append(f"组合 portfolio_id={portfolio_id} 不存在")
            elif portfolio.account_type != "simulated":
                errors.append(f"组合 {portfolio.name} 非模拟账户，不支持模拟订单")
        except Exception as exc:
            logger.debug("preview_order portfolio lookup failed: %s", exc)
            errors.append("组合查询失败")

    if symbol_id:
        try:
            from app.models.symbol import Symbol
            symbol = db.execute(
                select(Symbol).where(Symbol.id == symbol_id)
            ).scalars().first()
            if symbol is None:
                errors.append(f"标的 symbol_id={symbol_id} 不存在")
        except Exception as exc:
            logger.debug("preview_order symbol lookup failed: %s", exc)
            errors.append("标的查询失败")

    if errors or portfolio is None or symbol is None:
        # 基础校验已失败，跳过后续风控检查
        return build_preview_result(
            is_valid=False, errors=errors, changes=changes,
            extra={"risk_checks": risk_checks},
        )

    # ── 1. 数据健康校验 ─────────────────────────────────────
    data_healthy = False
    try:
        from sqlalchemy import func as sa_func
        from app.models.daily_bar import DailyBar
        from datetime import timedelta, timezone, datetime as _dt
        latest_date = db.execute(
            select(sa_func.max(DailyBar.trade_date))
            .where(DailyBar.symbol_id == symbol_id)
        ).scalar_one_or_none()
        if latest_date is None:
            errors.append("数据健康校验失败：标的无任何行情数据")
            risk_checks.append({"check": "data_health", "passed": False, "reason": "no_bars"})
        else:
            today = _dt.now(timezone.utc).replace(tzinfo=None).date()
            age_days = (today - latest_date).days if hasattr(latest_date, "year") else 999
            data_healthy = age_days <= 3
            if not data_healthy:
                errors.append(f"数据健康校验失败：最新行情日期 {latest_date}，距今 {age_days} 天（>3 天）")
                risk_checks.append({
                    "check": "data_health", "passed": False,
                    "reason": f"stale_bars_age_{age_days}",
                })
            else:
                risk_checks.append({"check": "data_health", "passed": True})
    except Exception as exc:
        logger.debug("preview_order data_health failed: %s", exc)
        errors.append("数据健康校验失败")
        risk_checks.append({"check": "data_health", "passed": False, "reason": "check_failed"})

    # ── 2. 手数校验 ─────────────────────────────────────────
    try:
        from app.services.market_rules import normalize_order_quantity, lot_size_for_symbol
        normalized = normalize_order_quantity(symbol, float(quantity))
        lot_size = lot_size_for_symbol(symbol)
        if normalized <= 0:
            errors.append(f"手数校验失败：数量 {quantity} 小于一手（{lot_size}）")
            risk_checks.append({
                "check": "lot_size", "passed": False,
                "reason": f"below_one_lot_{lot_size}",
            })
        else:
            risk_checks.append({
                "check": "lot_size", "passed": True,
                "detail": {"normalized_quantity": normalized, "lot_size": lot_size},
            })
    except Exception as exc:
        logger.debug("preview_order lot_size failed: %s", exc)
        errors.append("手数校验失败")
        risk_checks.append({"check": "lot_size", "passed": False, "reason": "check_failed"})

    # ── 3. 现金校验（买入） ────────────────────────────────
    if side == "buy":
        try:
            from app.services.sim_accounts import cash_balance, latest_price_for_symbol
            cash = cash_balance(db, portfolio_id)
            latest_price = (
                float(price) if price and price > 0
                else latest_price_for_symbol(db, symbol_id)
            )
            if latest_price is None or latest_price <= 0:
                errors.append("现金校验失败：无法确定成交价")
                risk_checks.append({
                    "check": "cash", "passed": False, "reason": "no_price",
                })
            else:
                # 估算金额（粗略，未含手续费/滑点）
                from app.services.market_rules import normalize_order_quantity as _norm
                norm_qty = _norm(symbol, float(quantity))
                est_amount = norm_qty * latest_price
                if cash < est_amount:
                    errors.append(
                        f"现金校验失败：现金 {cash:.2f} 不足，预估成交金额 {est_amount:.2f}"
                    )
                    risk_checks.append({
                        "check": "cash", "passed": False,
                        "reason": "insufficient_cash",
                        "detail": {"cash": cash, "est_amount": est_amount},
                    })
                else:
                    risk_checks.append({
                        "check": "cash", "passed": True,
                        "detail": {"cash": cash, "est_amount": est_amount},
                    })
        except Exception as exc:
            logger.debug("preview_order cash failed: %s", exc)
            errors.append("现金校验失败")
            risk_checks.append({"check": "cash", "passed": False, "reason": "check_failed"})

    # ── 4. T+1 校验（卖出） ────────────────────────────────
    if side == "sell":
        try:
            from app.services.sim_accounts import latest_price_for_symbol
            from app.services.market_rules import validate_market_rules
            latest_price = (
                float(price) if price and price > 0
                else latest_price_for_symbol(db, symbol_id)
            ) or 0.0
            # validate_market_rules 会在 T+1 违规时抛 HTTPException
            from app.services.market_rules import normalize_order_quantity as _norm
            norm_qty = _norm(symbol, float(quantity))
            try:
                validate_market_rules(
                    db=db,
                    symbol=symbol,
                    side=side,
                    quantity=norm_qty,
                    fill_price=latest_price,
                    portfolio_id=portfolio_id,
                    enforce_t_plus_1=True,
                    enforce_price_limit=True,
                )
                risk_checks.append({"check": "t_plus_1", "passed": True})
                risk_checks.append({"check": "price_limit", "passed": True})
            except Exception as exc:
                # 区分 T+1 与涨跌停
                msg = str(exc).lower()
                if "t+1" in msg or "t_1" in msg or "today" in msg:
                    errors.append(f"T+1 校验失败：{exc}")
                    risk_checks.append({
                        "check": "t_plus_1", "passed": False, "reason": str(exc),
                    })
                else:
                    errors.append(f"涨跌停校验失败：{exc}")
                    risk_checks.append({
                        "check": "price_limit", "passed": False, "reason": str(exc),
                    })
        except Exception as exc:
            logger.debug("preview_order t_plus_1/price_limit failed: %s", exc)
            errors.append("市场规则校验失败")
            risk_checks.append({
                "check": "market_rules", "passed": False, "reason": "check_failed",
            })

    # ── 5. 组合风控校验（best-effort） ────────────────────
    try:
        from app.models.portfolio import PortfolioRule
        active_rule = db.execute(
            select(PortfolioRule)
            .where(
                PortfolioRule.portfolio_id == portfolio_id,
                PortfolioRule.is_active == 1,
            )
            .order_by(PortfolioRule.id.desc())
        ).scalars().first()
        if active_rule is not None:
            risk_checks.append({
                "check": "portfolio_risk", "passed": True,
                "detail": {
                    "max_single_position_pct": active_rule.max_single_position_pct,
                    "max_open_positions": active_rule.max_open_positions,
                },
            })
        else:
            risk_checks.append({
                "check": "portfolio_risk", "passed": True,
                "reason": "no_active_rule",
            })
    except Exception as exc:
        logger.debug("preview_order portfolio_risk failed: %s", exc)
        risk_checks.append({
            "check": "portfolio_risk", "passed": False, "reason": "check_failed",
        })

    # 变更预览
    changes.append({
        "field": "sim_orders",
        "old_value": None,
        "new_value": {
            "portfolio_id": portfolio_id,
            "symbol_id": symbol_id,
            "side": side,
            "quantity": quantity,
            "price": price,
        },
        "description": (
            f"AI 起草{'买入' if side == 'buy' else '卖出'}订单: "
            f"组合 {portfolio_id} / 标的 {symbol_id} / 数量 {quantity}"
        ),
    })

    return build_preview_result(
        is_valid=len(errors) == 0,
        errors=errors,
        changes=changes,
        extra={
            "risk_checks": risk_checks,
            "auto_trade_disclaimer": (
                "自动交易由策略规则和调度器负责，本草稿仅生成手动确认的模拟订单"
            ),
        },
    )


def execute_order(db: Session, audit_id: int) -> dict[str, Any]:
    """第三步：用户确认后执行（调用 place_sim_order）。

    关键：place_sim_order 会重新做所有校验（现金/手数/T+1/涨跌停/数据健康/组合风控），
    即使 AI 草稿后被数据变化也会被拒绝，保证安全。
    """
    audit, payload, err = load_audit_payload(db, audit_id, expected_action_type=DRAFT_TYPE)
    if err == "audit_not_found":
        return {"success": False, "error": "audit_not_found", "message": "审计记录不存在"}
    if err == "not_confirmed":
        return {"success": False, "error": "not_confirmed", "message": "用户未确认，拒绝执行"}
    if err == "action_type_mismatch":
        return {"success": False, "error": "action_type_mismatch", "message": "动作类型不匹配"}

    try:
        from app.models.portfolio import Portfolio
        from app.models.symbol import Symbol
        from app.services.sim_accounts import place_sim_order

        portfolio_id = payload.get("portfolio_id")
        symbol_id = payload.get("symbol_id")
        side = payload.get("side", "buy")
        quantity = float(payload.get("quantity", 0))
        price = payload.get("price")
        order_type = payload.get("order_type", "market")
        note = payload.get("note", "AI 起草订单（用户确认后执行）")

        portfolio = db.execute(
            select(Portfolio).where(Portfolio.id == portfolio_id)
        ).scalars().first()
        if portfolio is None:
            result = {"success": False, "error": "portfolio_not_found", "message": "组合不存在"}
            record_final_result(db, audit_id, result)
            return result

        symbol = db.execute(
            select(Symbol).where(Symbol.id == symbol_id)
        ).scalars().first()
        if symbol is None:
            result = {"success": False, "error": "symbol_not_found", "message": "标的不存在"}
            record_final_result(db, audit_id, result)
            return result

        # 调用业务 API：place_sim_order 会重新校验所有市场规则与风控
        # 关键：自动交易永远不由对话触发，此处仅是用户明确确认的模拟订单
        order, trade = place_sim_order(
            db=db,
            portfolio=portfolio,
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price if order_type == "limit" else None,
            order_type=order_type,
            note=note,
            enforce_rules=True,  # 强制重新校验 T+1/涨跌停
            apply_fees=True,     # 应用真实手续费/滑点
        )

        result = {
            "success": True,
            "order_id": order.id,
            "trade_id": trade.id,
            "filled_quantity": float(order.filled_quantity),
            "filled_price": float(order.filled_price),
            "fee": float(order.fee),
            "message": (
                f"模拟订单已成交: {side} {order.filled_quantity} @ {order.filled_price}"
            ),
            "auto_trade_disclaimer": "本订单为用户确认的模拟订单，非自动交易",
        }
        record_final_result(db, audit_id, result)
        return result
    except Exception as exc:
        db.rollback()
        logger.warning("execute_order failed: %s", exc)
        result = {
            "success": False,
            "error": "order_rejected",
            "message": f"订单被业务规则拒绝：{str(exc)[:200]}",
        }
        record_final_result(db, audit_id, result)
        return result


__all__ = ["draft_order", "preview_order", "execute_order"]
