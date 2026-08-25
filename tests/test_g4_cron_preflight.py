"""G4：auto_simulation cron 门禁 API 契约测试。

T_G4_CRON_01  3 层检查全部通过：READY + 20:30 + 对账连续性 OK
            → preflight 返回 proceed=True；from=READY, to=RUNNING_AUTO_SIMULATION
              transition_trace.gate_status="ALLOWED"
T_G4_CRON_02  门禁失败场景（3 分支）：
            a) ADMIN_PAUSED → PORTFOLIO_NOT_READY（审计 AUTO_SIMULATION_RESULT FAILED/error_code=PORTFOLIO_NOT_READY）
            b) decision_at=15:05 → SCHEDULE_TOO_EARLY
            c) 模拟同日已经有 SUCCEEDED DecisionRun（auto_simulation）→ DUAL_EXECUTION_RISK_PROHIBITED
T_G4_CRON_03  对账间隔 > 1：last_decision=1/15，last_rec=1/10 → RECONCILIATION_GAP_WARNING
            → proceed 仍 True（不阻断调度），warnings 非空 + skip_reason=RECONCILIATION_GAP_WARNING
"""
from __future__ import annotations

import os
import tempfile
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

import pytest

os.environ.setdefault("ALEMBIC_DATABASE_URL", "")


@pytest.fixture(scope="function")
def tmp_alembic_db():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="qa_g4cron_")
    os.close(fd)
    url = f"sqlite:///{path}"
    os.environ["ALEMBIC_DATABASE_URL"] = url
    from alembic.config import Config
    from alembic import command as ac
    cfg = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parent.parent / "alembic"))
    ac.upgrade(cfg, "head")
    from sqlalchemy import create_engine
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


def _seed_portfolio(db, capital=1_000_000.0,
                    last_decision=None, last_reconciled=None) -> int:
    from app.models.portfolio import Portfolio
    p = Portfolio(
        name=f"g4_cron_{datetime.now().timestamp()}",
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
    if last_decision:
        p.last_decision_trade_date = last_decision
    if last_reconciled:
        p.last_reconciled_trade_date = last_reconciled
    db.flush()
    return p.id


def _to_ready(db, pid: int) -> None:
    from app.services.portfolio_state_machine import transition_portfolio_state
    # 可能是 PENDING_INITIAL_REVIEW 或 READY
    transition_portfolio_state(db, pid, "READY", operator_id="u_test")


# ===========================================================================
# T_G4_CRON_01 — 全通过
# ===========================================================================
def test_g4_cron_01_all_passed(tmp_alembic_db):
    db = tmp_alembic_db
    pid = _seed_portfolio(db,
                          last_decision=date(2025, 1, 14),
                          last_reconciled=date(2025, 1, 14))
    _to_ready(db, pid)
    db.commit()

    from app.api.routes.portfolio_governance import (
        AutoSimulationPreflightRequest, preflight_auto_simulation,
    )
    td = date(2025, 1, 15)
    decision_at = datetime(2025, 1, 15, 20, 30, 0, tzinfo=timezone.utc)
    req = AutoSimulationPreflightRequest(trade_date=td, decision_at=decision_at)
    r = preflight_auto_simulation(pid, req, db=db, actor="cron_worker")
    assert r.proceed is True
    assert r.from_state == "READY"
    assert r.to_state == "RUNNING_AUTO_SIMULATION"
    assert r.transition_trace is not None
    # gate_allowed=True；gate_error_code 为 None
    assert r.transition_trace["gate_allowed"] is True


# ===========================================================================
# T_G4_CRON_02 — 3 个失败分支
# ===========================================================================
def test_g4_cron_02_failure_branches(tmp_alembic_db):
    db = tmp_alembic_db
    from app.api.routes.portfolio_governance import (
        AutoSimulationPreflightRequest, preflight_auto_simulation,
    )
    from app.services.portfolio_state_machine import transition_portfolio_state as tps
    from app.services.data_governance_audit import DataGovernanceAuditEvent
    from sqlalchemy import select

    # (a) ADMIN_PAUSED → PORTFOLIO_NOT_READY
    pid_a = _seed_portfolio(db)
    _to_ready(db, pid_a)
    tps(db, pid_a, "ADMIN_PAUSED", operator_id="u_admin")
    db.commit()
    r_a = preflight_auto_simulation(
        pid_a,
        AutoSimulationPreflightRequest(
            trade_date=date(2025, 1, 15),
            decision_at=datetime(2025, 1, 15, 20, 30, 0, tzinfo=timezone.utc),
        ),
        db=db, actor="cron_worker",
    )
    assert r_a.proceed is False
    assert r_a.skip_reason == "PORTFOLIO_NOT_READY"
    # FAILED 审计行
    rows = db.execute(
        select(DataGovernanceAuditEvent).where(
            DataGovernanceAuditEvent.action == "AUTO_SIMULATION_RESULT",
            DataGovernanceAuditEvent.portfolio_id == pid_a,
        )
    ).scalars().all()
    assert len(rows) == 1
    import json
    attr = json.loads(rows[0].attributes_json)
    assert attr["result_status"] == "FAILED"
    assert attr["error_code"] == "PORTFOLIO_NOT_READY"

    # (b) decision_at=15:05（T 日下午） → SCHEDULE_TOO_EARLY
    pid_b = _seed_portfolio(db)
    _to_ready(db, pid_b)
    db.commit()
    r_b = preflight_auto_simulation(
        pid_b,
        AutoSimulationPreflightRequest(
            trade_date=date(2025, 1, 15),
            decision_at=datetime(2025, 1, 15, 15, 5, 0, tzinfo=timezone.utc),
        ),
        db=db, actor="cron_worker",
    )
    assert r_b.proceed is False
    assert r_b.skip_reason == "SCHEDULE_TOO_EARLY"

    # (c) 模拟同日 SUCCEEDED DecisionRun → DUAL_EXECUTION_RISK_PROHIBITED
    pid_c = _seed_portfolio(db)
    _to_ready(db, pid_c)
    from sqlalchemy import select as s
    from app.models.decision_engine import StrategyExecutionSnapshot, DecisionRun as DR
    import hashlib, json
    ssid = f"ss_{pid_c}"
    exists = db.execute(s(StrategyExecutionSnapshot.id).where(
        StrategyExecutionSnapshot.id == ssid)).scalar_one_or_none()
    if not exists:
        clock = json.dumps({"decision_at": "20:30"}, sort_keys=True)
        mem = json.dumps({"members": []}, sort_keys=True)
        snap = StrategyExecutionSnapshot(
            id=ssid, snapshot_no=1, portfolio_id=pid_c,
            decision_clock_json=clock, member_snapshot_json=mem,
            snapshot_hash=hashlib.sha256((clock + "|" + mem).encode()).hexdigest(),
            effective_from=date(2025, 1, 15), created_by="u_seed",
        )
        db.add(snap)
    td_c = date(2025, 1, 15)
    dr = DR(
        id=f"dr_exists_{pid_c}", portfolio_id=pid_c, trade_date=td_c,
        strategy_snapshot_id=ssid,
        run_type="auto_simulation", status="SUCCEEDED",
        universe_count=10, member_count=5,
        decision_at=datetime(2025, 1, 15, 20, 30, tzinfo=timezone.utc),
        data_cutoff_at=datetime(2025, 1, 15, 20, 0, tzinfo=timezone.utc),
        execution_at=datetime(2025, 1, 15, 20, 31, tzinfo=timezone.utc),
    )
    db.add(dr)
    db.flush()
    db.commit()
    r_c = preflight_auto_simulation(
        pid_c,
        AutoSimulationPreflightRequest(
            trade_date=td_c,
            decision_at=datetime(2025, 1, 15, 20, 32, 0, tzinfo=timezone.utc),
        ),
        db=db, actor="cron_worker",
    )
    assert r_c.proceed is False
    assert r_c.skip_reason == "DUAL_EXECUTION_RISK_PROHIBITED"


# ===========================================================================
# T_G4_CRON_03 — RECONCILIATION_GAP_WARNING（不阻断 proceed=True）
# ===========================================================================
def test_g4_cron_03_reconciliation_gap_warning(tmp_alembic_db):
    db = tmp_alembic_db
    pid = _seed_portfolio(db,
                          last_decision=date(2025, 1, 15),
                          last_reconciled=date(2025, 1, 10))  # 间隔 5 天
    _to_ready(db, pid)
    db.commit()
    from app.api.routes.portfolio_governance import (
        AutoSimulationPreflightRequest, preflight_auto_simulation,
    )
    r = preflight_auto_simulation(
        pid,
        AutoSimulationPreflightRequest(
            trade_date=date(2025, 1, 16),
            decision_at=datetime(2025, 1, 16, 20, 30, 0, tzinfo=timezone.utc),
        ),
        db=db, actor="cron_worker",
    )
    # proceed=True（不阻断，但告警）
    assert r.proceed is True
    assert r.skip_reason == "RECONCILIATION_GAP_WARNING"
    assert len(r.warnings) >= 1
    assert any("5" in w for w in r.warnings)  # 间隔 5 天
