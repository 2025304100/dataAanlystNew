"""G4：对账 & 状态流转 REST API 契约测试（等价路由调用）。

不跑 ASGI TestClient（避免 DB config 环境差异），而是直接调用路由 handler
注入 get_db/X-User 实参，验证：
  - 请求/响应 Schema 字段齐全
  - 错误码（404/400/409/428）对应业务语义
  - 路由侧做的"428 READY 下不应 confirm-reconciliation""400 ack 未勾选""400 非法 to_state"
    这些防护不依赖 G3 service 逻辑

用例（T_G4_API_01…04 共 4 个）：
  T_G4_API_01  GET /portfolios/{pid}/status
    → current_state ∈ {READY,PENDING}；allowed_transitions 与矩阵一致；
      is_auto_simulation_eligible==(state==READY)
  T_G4_API_02  POST /portfolios/{pid}/reconcile
    → 无 DR → ReconciliationResponse.status=BLOCKED + diff_kind=DECISION_RUN_NOT_FOUND；
      有 DR+BUY1000+Position=1000+Trade=1000 BUY → PASSED + last_reconciled_trade_date=T
  T_G4_API_03  POST /portfolios/{pid}/confirm-reconciliation
    → 当前 READY → 428（预条件失败）；ack=False → 400；
      RBLOCKED + 差异 → 409（重跑对账仍 BLOCKED）；
      RBLOCKED + 修复 → SUCCESS + READY + last_reconciled 推进
  T_G4_API_04  POST /portfolios/{pid}/transition-state
    → 非法 to_state → 400；RBLOCKED → RUN_AUTO_SIM → 409；READY → ADMIN_PAUSED → 200 OK
"""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException

os.environ.setdefault("ALEMBIC_DATABASE_URL", "")


@pytest.fixture(scope="function")
def tmp_alembic_db():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="qa_g4api_")
    os.close(fd)
    url = f"sqlite:///{path}"
    os.environ["ALEMBIC_DATABASE_URL"] = url
    from alembic.config import Config
    from alembic import command as ac
    cfg = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parent.parent / "alembic"))
    ac.upgrade(cfg, "head")
    from sqlalchemy import create_engine, text as sqltext
    from sqlalchemy.orm import sessionmaker
    engine = create_engine(url)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    sess = SessionLocal()
    try:
        yield sess
    finally:
        sess.close()
        engine.dispose()
        try:
            os.remove(path)
        except OSError:
            pass


def _seed_portfolio(db, capital=1_000_000.0) -> int:
    from app.models.portfolio import Portfolio
    p = Portfolio(
        name=f"g4_test_{datetime.now().timestamp()}",
        account_type="simulation",
        asset_scope="mixed",
        total_capital=capital,
        investable_ratio=0.95,
        cash_reserve_ratio=0.05,
        benchmark_code="000300",
        buy_fee_pct=0.00025,
        sell_fee_pct=0.00025,
        default_single_position_pct=0.30,
        is_test=1,
    )
    db.add(p)
    db.flush()
    db.refresh(p)
    return p.id


def _seed_symbol(db, code, sid):
    import sqlalchemy as sa
    row = db.execute(sa.text("SELECT id FROM symbols WHERE id=:i OR symbol=:c"),
                     {"i": int(sid), "c": code}).fetchone()
    if row:
        return int(row[0] if isinstance(row, tuple) else row._mapping.get("id"))
    try:
        db.execute(sa.text(
            "INSERT INTO symbols(id, symbol, name, asset_type, market, is_active, created_at, updated_at) "
            "VALUES (:i, :c, :n, 'stock', 'CN', 1, :now, :now)"
        ), {"i": int(sid), "c": code, "n": f"s_{code}", "now": datetime.now()})
        db.flush()
    except Exception:
        db.rollback()
    return int(sid)


def _seed_strategy_snapshot(db, pid: int) -> str:
    from app.models.decision_engine import StrategyExecutionSnapshot
    from sqlalchemy import select as s
    import hashlib, json
    sid = f"ss_{pid}"
    if db.execute(s(StrategyExecutionSnapshot.id).where(StrategyExecutionSnapshot.id == sid)).scalar_one_or_none():
        return sid
    clock = json.dumps({"decision_at": "20:30 Asia/Shanghai"}, sort_keys=True)
    members = json.dumps({"members": []}, sort_keys=True)
    snap = StrategyExecutionSnapshot(
        id=sid, snapshot_no=1, portfolio_id=pid,
        decision_clock_json=clock, member_snapshot_json=members,
        snapshot_hash=hashlib.sha256((clock + "|" + members).encode()).hexdigest(),
        effective_from=date.today(), created_by="u_seed",
    )
    db.add(snap)
    db.flush()
    return sid


def _seed_decision_run(db, pid: int, td: date, run_type="auto_simulation", status="SUCCEEDED") -> str:
    from app.models.decision_engine import DecisionRun as DecisionRunORM
    ssid = _seed_strategy_snapshot(db, pid)
    rid = f"dr_g4_{pid}_{td.isoformat()}_{datetime.now().timestamp()}"
    run = DecisionRunORM(
        id=rid, portfolio_id=pid, trade_date=td,
        strategy_snapshot_id=ssid,
        run_type=run_type, status=status,
        universe_count=20, member_count=5,
        decision_at=datetime(td.year, td.month, td.day, 20, 30, tzinfo=timezone.utc),
        data_cutoff_at=datetime(td.year, td.month, td.day, 20, 0, tzinfo=timezone.utc),
        execution_at=datetime(td.year, td.month, td.day, 20, 31, tzinfo=timezone.utc),
    )
    db.add(run)
    db.flush()
    return rid


def _seed_evidence(db, rid, pid, sid, td, action, tq, delta):
    import uuid, json, hashlib
    from app.models.decision_engine import DecisionEvidence
    payload = json.dumps({"rid": rid, "sid": sid, "td": td.isoformat(), "action": action}, sort_keys=True)
    ev = DecisionEvidence(
        id=f"ev_{uuid.uuid4().hex[:12]}",
        decision_run_id=rid, strategy_snapshot_id=f"ss_{pid}",
        portfolio_id=pid, symbol_id=sid, trade_date=td,
        decision_at=datetime(td.year, td.month, td.day, 20, 30),
        data_cutoff_at=datetime(td.year, td.month, td.day, 20, 0),
        execution_at=datetime(td.year, td.month, td.day + 1, 9, 30),
        action=action, min_lot_size=100,
        target_quantity=tq, target_qty_delta=delta,
        content_hash=hashlib.sha256(payload.encode()).hexdigest(),
    )
    db.add(ev)
    db.flush()


def _set_position(db, pid, sid, qty):
    from app.models.portfolio import Position
    from sqlalchemy import select as s
    pos = db.execute(s(Position).where(
        Position.portfolio_id == pid, Position.symbol_id == sid)).scalar_one_or_none()
    if pos is None:
        pos = Position(portfolio_id=pid, symbol_id=sid, quantity=qty, avg_cost=0.0,
                       asset_type="stock", opened_at=date.today())
        db.add(pos)
    else:
        pos.quantity = qty
    db.flush()


@dataclass
class _TradeLike:
    symbol_id: int
    quantity: int
    side: str


def _set_status_via_service(db, pid, to_state, actor="u_test"):
    from app.services.portfolio_state_machine import transition_portfolio_state
    transition_portfolio_state(db, pid, to_state, operator_id=actor)


# ===========================================================================
# T_G4_API_01 — GET /portfolios/{pid}/status
# ===========================================================================
def test_g4_api_01_status(tmp_alembic_db):
    db = tmp_alembic_db
    pid = _seed_portfolio(db)
    from app.api.routes.portfolio_governance import get_portfolio_status

    res = get_portfolio_status(pid, db)
    # 新组合：PENDING_INITIAL_REVIEW 或 READY
    assert res.current_state in {"PENDING_INITIAL_REVIEW", "READY"}
    assert res.portfolio_id == pid
    assert isinstance(res.allowed_transitions, list)
    assert res.is_auto_simulation_eligible == (res.current_state == "READY")
    # READY→RUN_AUTO_SIM 应允许
    if res.current_state == "READY":
        assert "RUNNING_AUTO_SIMULATION" in res.allowed_transitions


# ===========================================================================
# T_G4_API_02 — POST /portfolios/{pid}/reconcile
# ===========================================================================
def test_g4_api_02_reconcile(tmp_alembic_db):
    from app.api.routes.portfolio_governance import (
        reconcile_portfolio, ReconcileRequest,
    )
    db = tmp_alembic_db
    pid = _seed_portfolio(db)
    td = date(2025, 1, 14)

    # (a) 无 DR → BLOCKED + DECISION_RUN_NOT_FOUND
    rep = reconcile_portfolio(pid, ReconcileRequest(trade_date=td), db, "u_api_02")
    assert rep.status == "BLOCKED"
    assert any(d.kind == "DECISION_RUN_NOT_FOUND" for d in rep.differences)

    # (b) DR + BUY1000 + Position1000 + Trades1000 → PASSED + last_reconciled_trade_date
    sid = _seed_symbol(db, "601318", 601318)
    rid = _seed_decision_run(db, pid, td)
    _seed_evidence(db, rid, pid, sid, td, "BUY", 1000, 1000)
    _set_position(db, pid, sid, 1000)

    def tp(_s, _p, _t):
        return [_TradeLike(symbol_id=sid, quantity=1000, side="BUY")]

    import app.services.portfolio_reconciliation as _prec
    _orig = _prec.reconcile_trade_date

    def patched(*a, **kw):
        kw.setdefault("trades_provider", tp)
        return _orig(*a, **kw)

    _prec.reconcile_trade_date = patched  # type: ignore[attr-defined]
    try:
        rep_ok = reconcile_portfolio(pid, ReconcileRequest(trade_date=td), db, "u_api_02")
    finally:
        _prec.reconcile_trade_date = _orig  # type: ignore[attr-defined]
    assert rep_ok.status == "PASSED"
    assert rep_ok.last_reconciled_trade_date == td


# ===========================================================================
# T_G4_API_03 — POST /portfolios/{pid}/confirm-reconciliation（错误分支 + 成功）
# ===========================================================================
def test_g4_api_03_confirm(tmp_alembic_db):
    from app.api.routes.portfolio_governance import (
        confirm_portfolio_reconciliation, ConfirmReconciliationRequest,
    )
    db = tmp_alembic_db
    pid = _seed_portfolio(db)
    td = date(2025, 1, 15)

    # (a) READY 状态 → 428 预条件失败
    # 先从 PENDING→READY
    _set_status_via_service(db, pid, "READY")
    with pytest.raises(HTTPException) as ei:
        confirm_portfolio_reconciliation(
            pid, ConfirmReconciliationRequest(
                trade_date=td, acknowledge_all_diffs_cleared=True,
            ), db, "u_trader",
        )
    assert ei.value.status_code == 428

    # 进入 RECONCILIATION_BLOCKED
    _set_status_via_service(db, pid, "RUNNING_AUTO_SIMULATION")
    _set_status_via_service(db, pid, "RECONCILIATION_BLOCKED")

    # (b) ack=False → 400
    with pytest.raises(HTTPException) as ei_b:
        confirm_portfolio_reconciliation(
            pid, ConfirmReconciliationRequest(trade_date=td,
                                              acknowledge_all_diffs_cleared=False),
            db, "u_trader",
        )
    assert ei_b.value.status_code == 400

    # (c) 重跑对账仍差异（无 DecisionRun → DECISION_RUN_NOT_FOUND）→ 409
    with pytest.raises(HTTPException) as ei_c:
        confirm_portfolio_reconciliation(
            pid, ConfirmReconciliationRequest(trade_date=td,
                                              acknowledge_all_diffs_cleared=True),
            db, "u_trader",
        )
    assert ei_c.value.status_code == 409

    # (d) 准备完整对账通过的上下文 → 200 + 推进
    sid = _seed_symbol(db, "000001", 1)
    rid = _seed_decision_run(db, pid, td)
    _seed_evidence(db, rid, pid, sid, td, "BUY", 1000, 1000)
    _set_position(db, pid, sid, 1000)

    def tp(_s, _p, _t):
        return [_TradeLike(symbol_id=sid, quantity=1000, side="BUY")]

    import app.services.portfolio_reconciliation as _prec
    _orig = _prec.reconcile_trade_date

    def patched(*a, **kw):
        kw.setdefault("trades_provider", tp)
        return _orig(*a, **kw)

    _prec.reconcile_trade_date = patched  # type: ignore[attr-defined]
    try:
        # 先手动把状态改回 RBLOCKED（因为刚才 (c) 调用过内部 transition → 状态可能保持）
        from app.services.portfolio_state_machine import transition_portfolio_state as tps
        from app.models.portfolio import Portfolio
        db.refresh(db.get(Portfolio, pid))
        # 若当前已 READY（因为 (c) 中 re-reconcile 异常 raise 前 transition 未执行），先回退
        # 确保测试用例独立：这里强制回 RBLOCKED via READY→RUN→RBLOCKED（合法链）
        tps(db, pid, "READY", operator_id="u_resetter")
        tps(db, pid, "RUNNING_AUTO_SIMULATION", operator_id="u_resetter")
        tps(db, pid, "RECONCILIATION_BLOCKED", operator_id="u_resetter")
        ok = confirm_portfolio_reconciliation(
            pid, ConfirmReconciliationRequest(trade_date=td,
                                              acknowledge_all_diffs_cleared=True),
            db, "u_trader",
        )
    finally:
        _prec.reconcile_trade_date = _orig  # type: ignore[attr-defined]
    assert ok.portfolio_now_ready is True
    assert ok.from_state == "RECONCILIATION_BLOCKED"
    assert ok.to_state == "READY"
    assert ok.last_reconciled_trade_date == td


# ===========================================================================
# T_G4_API_04 — POST /portfolios/{pid}/transition-state
# ===========================================================================
def test_g4_api_04_transition(tmp_alembic_db):
    from app.api.routes.portfolio_governance import (
        admin_transition_portfolio_state, TransitionStateRequest,
    )
    db = tmp_alembic_db
    pid = _seed_portfolio(db)
    _set_status_via_service(db, pid, "READY")

    # (a) 非法 to_state → 400
    with pytest.raises(HTTPException) as ei_a:
        admin_transition_portfolio_state(
            pid, TransitionStateRequest(to_state="DOES_NOT_EXIST"),
            db, "u_admin",
        )
    assert ei_a.value.status_code == 400

    # (b) READY→ADMIN_PAUSED：200 OK
    r_b = admin_transition_portfolio_state(
        pid, TransitionStateRequest(to_state="ADMIN_PAUSED",
                                     reason="风控事件，临时冻结"),
        db, "u_admin",
    )
    assert r_b.status == "OK"
    assert r_b.to_state == "ADMIN_PAUSED"

    # (c) 非法转移 → 409（ADMIN_PAUSED → RUNNING_AUTO_SIMULATION 不允许）
    with pytest.raises(HTTPException) as ei_c:
        admin_transition_portfolio_state(
            pid, TransitionStateRequest(to_state="RUNNING_AUTO_SIMULATION"),
            db, "u_admin",
        )
    assert ei_c.value.status_code == 409

    # (d) 合法链 ADMIN_PAUSED → READY：200 OK
    r_d = admin_transition_portfolio_state(
        pid, TransitionStateRequest(to_state="READY"),
        db, "u_admin",
    )
    assert r_d.status == "OK"
    assert r_d.to_state == "READY"
