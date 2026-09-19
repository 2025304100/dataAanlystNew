"""G3-WP0-2 专项测试（对账 + 审计 + 7 状态状态机 + 人工对账确认）。

用例：
  T_G3_AUD_01  审计事件：候选池 SCD2 同日变更(seq=2) + 基准主备切换 + auto_sim 成功/失败，action=
                PORTFOLIO_CANDIDATE_SCD2_CHANGE / BENCHMARK_SOURCE_FAILOVER / AUTO_SIMULATION_RESULT
                写入 attributes_json（intra_day_seq=2 / reason=FetcherError / result_status=FAILED）
  T_G3_AUD_02  canonical JSON：action 非法值 → ILLEGAL_STATE_TRANSITION 仍写入（note 追加 INVALID_ACTION）
  T_G3_REC_01  对账 PASSED：DecisionEvidence(target_qty_delta=+1000 BUY) + trades_provider 返回 1000 BUY
                + Position=1000 → status=PASSED；last_reconciled_trade_date 单调推进
  T_G3_REC_02  对账 BLOCKED：BUY 1000 撮合只 700（部分成交）→ diffs UNFILLED_PLAN；
                last_reconciled_trade_date NOT 推进；REC_RESULT 审计 attributes.diffs_count_non_checked≥1
  T_G3_REC_03  无 DecisionRun：DECISION_RUN_NOT_FOUND diff，status BLOCKED，last_reconciled_trade_date 不动
  T_G3_STM_01  状态机：READY→RUNNING_AUTO_SIM→RECONCILIATION_BLOCKED；
                RBLOCKED→RUNNING_AUTO_SIM 非法 → ILLEGAL_STATE_TRANSITION 审计记录；错误类型 StateTransitionError
  T_G3_STM_02  TR-02.7b2：人工 confirm_reconciliation_fixed → 先重跑 reconcile（若仍有 diff 失败）→
                修复后再 confirm → RBLOCKED→READY，last_reconciled_trade_date 推进；
                acknowledge_all_diffs_cleared=False → ValueError(prevent mis-click)
"""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("ALEMBIC_DATABASE_URL", "")


@pytest.fixture(scope="function")
def tmp_alembic_db():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="qa_g3_")
    os.close(fd)
    url = f"sqlite:///{path}"
    os.environ["ALEMBIC_DATABASE_URL"] = url
    from alembic.config import Config
    from alembic import command as ac
    cfg = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parent.parent / "alembic"))
    ac.upgrade(cfg, "head")
    from sqlalchemy import create_engine, select, text as sqltext
    from sqlalchemy.orm import sessionmaker
    engine = create_engine(url)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    sess = SessionLocal()

    # 显式补列 portfolio_status；使用"独立连接 + DDL + 立即 commit"，避免事务干扰
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
        try:
            # 补 portfolio_status
            try:
                conn.execute(sqltext("SELECT portfolio_status FROM portfolios LIMIT 0"))
            except Exception:
                conn.execute(sqltext(
                    "ALTER TABLE portfolios ADD COLUMN portfolio_status VARCHAR(32)"
                ))
            # 补 data_governance_audit_events 表
            try:
                conn.execute(sqltext("SELECT id FROM data_governance_audit_events LIMIT 0"))
            except Exception:
                conn.execute(sqltext(
                    "CREATE TABLE IF NOT EXISTS data_governance_audit_events ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "action VARCHAR(64) NOT NULL, "
                    "portfolio_id INTEGER, business_key VARCHAR(128), "
                    "occurred_at DATETIME NOT NULL, "
                    "operator_id VARCHAR(128) NOT NULL DEFAULT 'system', "
                    "correlation_id VARCHAR(128), "
                    "symbol_id INTEGER, "
                    "before_json TEXT, after_json TEXT, attributes_json TEXT, note TEXT"
                    ")"
                ))
            conn.commit()
        finally:
            conn.exec_driver_sql("PRAGMA foreign_keys = ON")

    try:
        yield sess
    finally:
        sess.close()
        engine.dispose()
        try:
            os.remove(path)
        except OSError:
            pass


# ── helpers ──────────────────────────────────────────────────────────────────
def _seed_portfolio(db, capital=1_000_000.0) -> int:
    from app.models.portfolio import Portfolio
    p = Portfolio(
        name=f"g3_test_{datetime.now().timestamp()}",
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


def _seed_symbol(db, code="600519", symbol_id=600519):
    import sqlalchemy as sa
    from sqlalchemy import select as s
    # 先查，存在则直接返回 id
    row = db.execute(sa.text("SELECT id FROM symbols WHERE id=:i OR symbol=:c"),
                     {"i": int(symbol_id), "c": code}).fetchone()
    if row:
        actual_id = int(row[0] if isinstance(row, tuple) else row._mapping.get("id"))
        return actual_id
    try:
        db.execute(sa.text(
            "INSERT INTO symbols(id, symbol, name, asset_type, market, is_active, created_at, updated_at) "
            "VALUES (:i, :c, :n, 'stock', 'CN', 1, :now, :now)"
        ), {"i": int(symbol_id), "c": code, "n": f"sym_{code}", "now": datetime.now()})
        db.flush()
    except Exception:
        db.rollback()
        row = db.execute(sa.text("SELECT id FROM symbols WHERE id=:i OR symbol=:c"),
                         {"i": int(symbol_id), "c": code}).fetchone()
        if not row:
            raise
        return int(row[0] if isinstance(row, tuple) else row._mapping.get("id"))
    return int(symbol_id)


def _seed_strategy_snapshot(db, pid: int) -> str:
    from app.models.decision_engine import StrategyExecutionSnapshot
    sid = f"ss_{pid}"
    from sqlalchemy import select as s
    exists = db.execute(s(StrategyExecutionSnapshot.id).where(
        StrategyExecutionSnapshot.id == sid)).scalar_one_or_none()
    if exists:
        return sid
    import hashlib
    import json
    clock = json.dumps({
        "decision_at": "20:30 Asia/Shanghai",
        "data_cutoff_at": "20:00 Asia/Shanghai",
        "execution_at": "T+1 NEXT_OPEN Asia/Shanghai",
    }, sort_keys=True)
    member_json = json.dumps({"members": []}, sort_keys=True)
    shash = hashlib.sha256((clock + "|" + member_json).encode()).hexdigest()
    snap = StrategyExecutionSnapshot(
        id=sid, snapshot_no=1, portfolio_id=pid,
        decision_clock_json=clock,
        member_snapshot_json=member_json,
        snapshot_hash=shash,
        effective_from=date.today(),
        created_by="u_seed",
    )
    db.add(snap)
    db.flush()
    return sid


def _seed_decision_run(db, pid: int, td: date, status="SUCCEEDED", run_type="auto_simulation") -> str:
    from app.models.decision_engine import DecisionRun as DecisionRunORM
    rid = f"dr_g3_{pid}_{td.isoformat()}_{datetime.now().timestamp()}"
    ssid = _seed_strategy_snapshot(db, pid)
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


def _seed_evidence(db, rid: str, pid: int, sid: int, td: date, action,
                   target_quantity, target_qty_delta):
    import uuid
    import hashlib
    import json
    from app.models.decision_engine import DecisionEvidence
    content_payload = json.dumps({
        "rid": rid, "sid": sid, "td": td.isoformat(),
        "action": action, "tq": target_quantity, "delta": target_qty_delta,
    }, sort_keys=True)
    chash = hashlib.sha256(content_payload.encode()).hexdigest()
    ev = DecisionEvidence(
        id=f"ev_{uuid.uuid4().hex[:12]}",
        decision_run_id=rid, strategy_snapshot_id=f"ss_{pid}",
        portfolio_id=pid, symbol_id=sid, trade_date=td,
        decision_at=datetime(td.year, td.month, td.day, 20, 30),
        data_cutoff_at=datetime(td.year, td.month, td.day, 20, 0),
        execution_at=datetime(td.year, td.month, td.day + 1, 9, 30),
        action=action, min_lot_size=100,
        target_quantity=target_quantity, target_qty_delta=target_qty_delta,
        content_hash=chash,
    )
    db.add(ev)
    db.flush()


def _set_position(db, pid, sid, qty, cash=0.0):
    from app.models.portfolio import Position
    from sqlalchemy import select as s
    pos = db.execute(s(Position).where(
        Position.portfolio_id == pid, Position.symbol_id == sid
    )).scalar_one_or_none()
    if pos is None:
        pos = Position(portfolio_id=pid, symbol_id=sid, quantity=qty, avg_cost=0.0,
                       asset_type="stock", opened_at=date.today())
        db.add(pos)
    else:
        pos.quantity = qty
    db.flush()


# ===========================================================================
# T_G3_AUD_01 — 审计事件：候选池同日变更 / 主备切换 / auto_sim 失败
# ===========================================================================
def test_g3_aud_01_audit_candidates_failover_autosim(tmp_alembic_db):
    db = tmp_alembic_db
    pid = _seed_portfolio(db)
    from sqlalchemy import select
    from app.services.data_governance_audit import (
        audit_candidate_scd2_change, audit_benchmark_failover,
        audit_auto_simulation_result, DataGovernanceAuditEvent,
    )
    import json
    # 同日 seq=2 候选池变更（UPATE_CLOSE_ONLY + INSERT）
    r1 = audit_candidate_scd2_change(
        db, pid, operator_id="u_jerry", intra_day_seq=2,
        action_type="UPDATE_CLOSE_ONLY",
        removed_symbols=[7001], added_symbols=[7002],
        before_rows=[{"symbol_id": 7001, "effective_start": "2025-01-01"}],
        after_rows=[{"symbol_id": 7002, "effective_start": "2025-01-02"}],
        correlation_id="C_AUD_01",
    )
    # 基准主备切换
    r2 = audit_benchmark_failover(
        db, "000300", primary_source="BAOSTOCK_FALLBACK",
        switch_event={"from": "AKSHARE_PRIMARY", "to": "BAOSTOCK_FALLBACK",
                      "reason": "FetcherError: timeout"},
        portfolio_id=pid, correlation_id="C_AUD_01",
    )
    # auto_sim FAILED
    r3 = audit_auto_simulation_result(
        db, pid, trade_date=date(2025, 1, 2), result_status="FAILED",
        decision_run_id="dr_FAKE_999", operator_id="cron_worker",
        correlation_id="C_AUD_01", error_code="DECISION_ENGINE_TIMEOUT",
    )
    db.commit()

    rows = db.execute(select(DataGovernanceAuditEvent).order_by(DataGovernanceAuditEvent.id)).scalars().all()
    # 3 条
    assert len(rows) == 3
    by_action = {r.action: r for r in rows}
    assert "PORTFOLIO_CANDIDATE_SCD2_CHANGE" in by_action
    cand = by_action["PORTFOLIO_CANDIDATE_SCD2_CHANGE"]
    assert cand.operator_id == "u_jerry"
    attr = json.loads(cand.attributes_json)
    assert attr["intra_day_seq"] == 2
    assert attr["action_type"] == "UPDATE_CLOSE_ONLY"
    assert attr["removed_symbols"] == [7001]
    assert attr["added_symbols"] == [7002]

    failover = by_action["BENCHMARK_SOURCE_FAILOVER"]
    a2 = json.loads(failover.attributes_json)
    assert a2["primary_source"] == "BAOSTOCK_FALLBACK"
    assert a2["switch_event"]["to"] == "BAOSTOCK_FALLBACK"

    sim = by_action["AUTO_SIMULATION_RESULT"]
    a3 = json.loads(sim.attributes_json)
    assert a3["result_status"] == "FAILED"
    assert a3["error_code"] == "DECISION_ENGINE_TIMEOUT"


# ===========================================================================
# T_G3_AUD_02 — canonical JSON 非法 action → UNKNOWN_AUDIT_ACTION 映射写入（fail-soft 不阻断）
# ===========================================================================
def test_g3_aud_02_invalid_action_written_with_note(tmp_alembic_db):
    db = tmp_alembic_db
    from sqlalchemy import select
    import json
    from app.services.data_governance_audit import (
        write_audit_event, DataGovernanceAuditEvent,
    )
    write_audit_event(
        db, "NOT_IN_ALLOWED_LIST",  # type: ignore[arg-type]
        business_key="dummy", occurred_at=datetime.now(),
    )
    db.commit()
    row = db.execute(select(DataGovernanceAuditEvent)).scalar_one()
    # 按 DB 层 CHECK 约束（7+UNKNOWN）安全映射：写 UNKNOWN_AUDIT_ACTION，原始值放 attributes_json.original_action
    assert row.action == "UNKNOWN_AUDIT_ACTION"
    assert "[INVALID_ACTION: 'NOT_IN_ALLOWED_LIST']" in (row.note or "")
    if row.attributes_json:
        attr = json.loads(row.attributes_json)
        assert attr.get("original_action") == "NOT_IN_ALLOWED_LIST"


# ===========================================================================
# T_G3_REC_01 — 对账 PASSED + 单调推进 last_reconciled_trade_date
# ===========================================================================
@dataclass
class _TradeLike:
    symbol_id: int
    quantity: int
    side: str  # BUY/SELL


def test_g3_rec_01_passed_promotes_last_reconciled(tmp_alembic_db):
    db = tmp_alembic_db
    pid = _seed_portfolio(db)
    sid = _seed_symbol(db, code="600519", symbol_id=600519)
    td = date(2025, 1, 6)
    rid = _seed_decision_run(db, pid, td, run_type="auto_simulation")
    _seed_evidence(db, rid, pid, sid, td, "BUY",
                   target_quantity=1000, target_qty_delta=1000)
    # 实际最终持仓：1000
    _set_position(db, pid, sid, 1000)

    def tp(session, pid_arg, td_arg):
        return [_TradeLike(symbol_id=sid, quantity=1000, side="BUY")]

    from app.services.portfolio_reconciliation import reconcile_trade_date
    from app.models.portfolio import Portfolio
    rep = reconcile_trade_date(db, pid, td, trades_provider=tp,
                               correlation_id="C_REC_01")
    db.commit()
    assert rep.status == "PASSED"
    assert rep.has_any_diff is False
    p = db.get(Portfolio, pid)
    assert p.last_reconciled_trade_date == td


# ===========================================================================
# T_G3_REC_02 — UNFILLED_PLAN 差异 + 审计 diffs_count_non_checked≥1
# ===========================================================================
def test_g3_rec_02_partial_fill_unfilled_plan(tmp_alembic_db):
    db = tmp_alembic_db
    pid = _seed_portfolio(db)
    sid = _seed_symbol(db, code="000858", symbol_id=858)
    td = date(2025, 1, 7)
    rid = _seed_decision_run(db, pid, td)
    _seed_evidence(db, rid, pid, sid, td, "BUY",
                   target_quantity=1000, target_qty_delta=1000)
    # 只成交 700（部分成交）
    _set_position(db, pid, sid, 700)

    def tp(session, pid_arg, td_arg):
        return [_TradeLike(symbol_id=sid, quantity=700, side="BUY")]

    from app.services.portfolio_reconciliation import reconcile_trade_date
    from app.services.data_governance_audit import DataGovernanceAuditEvent
    import json
    from sqlalchemy import select
    rep = reconcile_trade_date(db, pid, td, trades_provider=tp, correlation_id="C_REC_02")
    db.commit()
    assert rep.status == "BLOCKED"
    kinds = [d.kind for d in rep.differences if d.kind != "NOT_CHECKED"]
    assert "UNFILLED_PLAN" in kinds
    # last_reconciled_trade_date 未推进
    from app.models.portfolio import Portfolio
    p = db.get(Portfolio, pid)
    assert p.last_reconciled_trade_date is None
    # 审计 RECONCILIATION_RESULT attributes_json → diffs_count_non_checked ≥ 1
    rows = db.execute(
        select(DataGovernanceAuditEvent).where(
            DataGovernanceAuditEvent.action == "RECONCILIATION_RESULT"
        )
    ).scalars().all()
    assert len(rows) == 1
    attrs = json.loads(rows[0].attributes_json)
    assert attrs["diffs_count_non_checked"] >= 1


# ===========================================================================
# T_G3_REC_03 — 无 DR：DECISION_RUN_NOT_FOUND  + 不推进
# ===========================================================================
def test_g3_rec_03_no_decision_run(tmp_alembic_db):
    db = tmp_alembic_db
    pid = _seed_portfolio(db)
    from app.services.portfolio_reconciliation import reconcile_trade_date
    from app.models.portfolio import Portfolio
    rep = reconcile_trade_date(db, pid, date(2025, 1, 10), correlation_id="C_REC_03")
    db.commit()
    assert rep.status == "BLOCKED"
    assert any(d.kind == "DECISION_RUN_NOT_FOUND" for d in rep.differences)
    p = db.get(Portfolio, pid)
    assert p.last_reconciled_trade_date is None


# ===========================================================================
# T_G3_STM_01 — 状态机合法/非法跳转 + 审计 ILLEGAL_STATE_TRANSITION
# ===========================================================================
def test_g3_stm_01_legal_and_illegal_transitions(tmp_alembic_db):
    db = tmp_alembic_db
    pid = _seed_portfolio(db)
    from app.services.portfolio_state_machine import (
        transition_portfolio_state, StateTransitionError,
    )
    from app.services.data_governance_audit import DataGovernanceAuditEvent
    from sqlalchemy import select

    # 先 PENDING_INITIAL_REVIEW → READY
    r0 = transition_portfolio_state(db, pid, "READY", correlation_id="C_STM_01")
    assert r0.status in ("OK", "NOOP")

    # READY → RUNNING_AUTO_SIMULATION : 合法
    res1 = transition_portfolio_state(db, pid, "RUNNING_AUTO_SIMULATION",
                                      correlation_id="C_STM_01")
    assert res1.status in ("OK", "NOOP")
    # RUNNING_AUTO_SIMULATION → RECONCILIATION_BLOCKED
    res2 = transition_portfolio_state(db, pid, "RECONCILIATION_BLOCKED",
                                      correlation_id="C_STM_01")
    assert res2.status == "OK"

    # RECONCILIATION_BLOCKED → RUNNING_AUTO_SIMULATION 非法
    with pytest.raises(StateTransitionError) as ei:
        transition_portfolio_state(db, pid, "RUNNING_AUTO_SIMULATION",
                                   correlation_id="C_STM_01")
    assert "RECONCILIATION_BLOCKED -> RUNNING_AUTO_SIMULATION" in str(ei.value)

    # 审计：至少 1 条 ILLEGAL_STATE_TRANSITION
    rows = db.execute(
        select(DataGovernanceAuditEvent).where(
            DataGovernanceAuditEvent.action == "ILLEGAL_STATE_TRANSITION"
        )
    ).scalars().all()
    assert len(rows) >= 1


# ===========================================================================
# T_G3_STM_02 — TR-02.7b2 人工对账确认
# ===========================================================================
def test_g3_stm_02_confirm_reconciliation_fixed(tmp_alembic_db):
    db = tmp_alembic_db
    pid = _seed_portfolio(db)
    sid = _seed_symbol(db, code="600000", symbol_id=600000)
    td = date(2025, 1, 13)

    from app.services.portfolio_state_machine import (
        transition_portfolio_state, confirm_reconciliation_fixed,
        StateTransitionError,
    )
    # 先 READY → RUNNING_AUTO_SIMULATION → RECONCILIATION_BLOCKED
    transition_portfolio_state(db, pid, "READY")
    transition_portfolio_state(db, pid, "RUNNING_AUTO_SIMULATION")
    transition_portfolio_state(db, pid, "RECONCILIATION_BLOCKED")

    # A) 未勾选 acknowledge → ValueError（防误触）
    with pytest.raises(ValueError) as ei_a:
        confirm_reconciliation_fixed(db, pid, td, operator_id="u_trader",
                                     acknowledge_all_diffs_cleared=False)
    assert "acknowledge_all_diffs_cleared=True" in str(ei_a.value)

    # B) 重跑仍有差异 → StateTransitionError/内部 reconcile 异常
    rid = _seed_decision_run(db, pid, td)
    _seed_evidence(db, rid, pid, sid, td, "BUY", target_quantity=1000, target_qty_delta=1000)
    _set_position(db, pid, sid, 700)  # 差 300 股

    def tp_bad(session, pid_arg, td_arg):
        return [_TradeLike(symbol_id=sid, quantity=700, side="BUY")]

    import app.services.portfolio_reconciliation as _prec_mod
    _orig_rec = _prec_mod.reconcile_trade_date

    def patched_bad(db_arg, pid_arg, td_arg, **kw):
        kw.setdefault("trades_provider", tp_bad)
        return _orig_rec(db_arg, pid_arg, td_arg, **kw)

    _prec_mod.reconcile_trade_date = patched_bad  # type: ignore[attr-defined]
    try:
        with pytest.raises(StateTransitionError):
            confirm_reconciliation_fixed(db, pid, td, operator_id="u_trader",
                                         acknowledge_all_diffs_cleared=True,
                                         force_skip_re_reconcile=False)
    finally:
        _prec_mod.reconcile_trade_date = _orig_rec  # type: ignore[attr-defined]

    # C) 修复后再 confirm：position=1000 + 撮合 1000 BUY → 对账通过 → RBLOCKED→READY，last_reconciled 推进
    _set_position(db, pid, sid, 1000)

    def tp_good(session, pid_arg, td_arg):
        return [_TradeLike(symbol_id=sid, quantity=1000, side="BUY")]

    def patched_good(db_arg, pid_arg, td_arg, **kw):
        kw.setdefault("trades_provider", tp_good)
        return _orig_rec(db_arg, pid_arg, td_arg, **kw)

    _prec_mod.reconcile_trade_date = patched_good  # type: ignore[attr-defined]
    try:
        res = confirm_reconciliation_fixed(db, pid, td, operator_id="u_trader",
                                           acknowledge_all_diffs_cleared=True,
                                           force_skip_re_reconcile=False,
                                           correlation_id="C_STM_02")
    finally:
        _prec_mod.reconcile_trade_date = _orig_rec  # type: ignore[attr-defined]
    assert res.portfolio_now_ready is True
    from app.models.portfolio import Portfolio
    p = db.get(Portfolio, pid)
    assert p.last_reconciled_trade_date == td
