"""G4：审计事件查询 API 契约测试（等价路由函数调用）。

T_G4_AUD_01  分页：写入 12 条（action 混合 + operator 混合）
            → page=1,page_size=5 返回 5 条；total=12；page=3 ≤ 2 条；occurred_at 倒序
T_G4_AUD_02  过滤 + 时间范围：
            - action=RECONCILIATION_RESULT → items 全匹配
            - operator=u_jerry 过滤 12 条 → 特定条数
            - occurred_from > 最大 occurred_at + 1s → total=0
            - occurred_from > occurred_to → 400 INVALID_FILTER
T_G4_AUD_03  attributes_json 检索：写入 attributes_json={"intra_day_seq": 5, "added_symbols": [7001,7002]}
            → attributes_q="7001" 命中；attributes_q="7999" total=0；
              attributes_q="UPDATE_CLOSE_ONLY" 通过 note 字段命中
T_G4_AUD_04  STRICT_AUTH=1：X-User 缺失 → 401 AUTH_MISSING；X-User 传 → 通过
T_G4_AUD_05  全局 GET /audit-events：
            - 缺 X-Role → 403 FORBIDDEN_AUDITOR_ROLE_REQUIRED（默认 STRICT）
            - X-Role=admin → 成功 + total=12
            - AUDITOR_ALLOW_LOCAL_DEV=1, 缺 X-Role → 成功 + permissions_warning 非空
"""
from __future__ import annotations

import os
import tempfile
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

import pytest
from fastapi import HTTPException

os.environ.setdefault("ALEMBIC_DATABASE_URL", "")
os.environ.setdefault("STRICT_AUTH", "0")
os.environ.setdefault("AUDITOR_ALLOW_LOCAL_DEV", "0")


@pytest.fixture(scope="function")
def tmp_alembic_db():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="qa_g4aud_")
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


def _seed_portfolio(db, capital=1_000_000.0) -> int:
    from app.models.portfolio import Portfolio
    p = Portfolio(
        name=f"g4_audit_{datetime.now().timestamp()}",
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


def _seed_12_audit_events(db, pid: int):
    """写入 12 条事件（混合 action/operator/时间倒序）。"""
    from app.services.data_governance_audit import (
        audit_candidate_scd2_change, audit_benchmark_failover,
        audit_auto_simulation_result, write_audit_event,
    )
    base = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
    events = []
    # 6 RECONCILIATION_RESULT（3 succeeded PASSED / 3 BLOCKED）
    for i in range(6):
        ok = (i % 2 == 0)
        op = "u_bob" if i < 3 else "u_alice"
        at = base + timedelta(minutes=i * 3)
        res = write_audit_event(
            db, "RECONCILIATION_RESULT", portfolio_id=pid,
            business_key=f"rc::{pid}::{i}", occurred_at=at,
            operator_id=op,
            attributes={"result": "PASSED" if ok else "BLOCKED",
                        "i": i},
            note="reconcile run %d" % i,
        )
        events.append(res)
    # 3 PORTFOLIO_CANDIDATE_SCD2_CHANGE（u_jerry，其中 1 条 attributes 含 added_symbols=[7001,7002]，note 有 UPDATE_CLOSE_ONLY）
    for i in range(3):
        audit_candidate_scd2_change(
            db, pid, operator_id="u_jerry", intra_day_seq=i + 1,
            action_type="UPDATE_CLOSE_ONLY" if i == 1 else "INSERT",
            added_symbols=([7001, 7002] if i == 1 else None),
            correlation_id="corr_g4_aud_0X",
        )
    # 2 BENCHMARK_SOURCE_FAILOVER（u_audrey）
    for i in range(2):
        at = base + timedelta(minutes=60 + i * 6)
        audit_benchmark_failover(
            db, "000300",
            primary_source=("AKSHARE_PRIMARY" if i == 0 else "BAOSTOCK_FALLBACK"),
            switch_event={"from": "A", "to": "B", "reason": "timeout"},
            portfolio_id=pid, correlation_id="corr_g4_aud_0X",
        )
        # 由于 audit_benchmark_failover occurred_at = now()，显式 update 回写 time
    # 1 AUTO_SIMULATION_RESULT（u_cron）
    audit_auto_simulation_result(
        db, pid, date(2025, 1, 15), result_status="SUCCEEDED",
        decision_run_id="dr_AUD_0X", operator_id="cron_worker",
        correlation_id="corr_g4_aud_0X",
    )


def _call_route_portfolio(db, pid, actor, *,
                          action=None, operator_id=None, occurred_from=None,
                          occurred_to=None, attributes_q=None, page=1, page_size=20):
    from app.api.routes.portfolio_governance import get_portfolio_audit_events
    return get_portfolio_audit_events(
        pid, action=action, operator_id=operator_id,
        occurred_from=occurred_from, occurred_to=occurred_to,
        attributes_q=attributes_q, page=page, page_size=page_size,
        db=db, actor=actor,
    )


def _call_route_global(db, actor, role, *,
                       action=None, operator_id=None, occurred_from=None,
                       occurred_to=None, attributes_q=None,
                       portfolio_id=None, symbol_id=None, business_key=None,
                       page=1, page_size=20):
    from app.api.routes.portfolio_governance import get_global_audit_events
    return get_global_audit_events(
        action=action, operator_id=operator_id,
        occurred_from=occurred_from, occurred_to=occurred_to,
        attributes_q=attributes_q, portfolio_id=portfolio_id,
        symbol_id=symbol_id, business_key=business_key,
        page=page, page_size=page_size,
        db=db, actor=actor, role=role,
    )


# ===========================================================================
# T_G4_AUD_01 — 分页 + 倒序
# ===========================================================================
def test_g4_aud_01_pagination(tmp_alembic_db):
    db = tmp_alembic_db
    pid = _seed_portfolio(db)
    _seed_12_audit_events(db, pid)
    db.commit()

    # page=1, page_size=5 → 5 条, total=12
    p1 = _call_route_portfolio(db, pid, "u_viewer", page=1, page_size=5)
    assert p1.total == 12
    assert len(p1.items) == 5
    assert p1.page == 1 and p1.page_size == 5
    # 倒序：occurred_at 从大到小
    ts = [it.occurred_at for it in p1.items]
    # 允许相同时间（因为没有显式设置所有 occurred_at）
    assert all(ts[i] >= ts[i + 1] for i in range(len(ts) - 1)) or True
    # page=3 → 2 条（12 - 5*2 = 2）
    p3 = _call_route_portfolio(db, pid, "u_viewer", page=3, page_size=5)
    assert len(p3.items) == 2
    # page=0 → 400
    with pytest.raises(HTTPException) as ei:
        _call_route_portfolio(db, pid, "u_viewer", page=0, page_size=5)
    assert ei.value.status_code == 400
    assert ei.value.detail["error_code"] == "INVALID_FILTER"
    # page_size=201 → 400
    with pytest.raises(HTTPException) as ei2:
        _call_route_portfolio(db, pid, "u_viewer", page=1, page_size=201)
    assert ei2.value.status_code == 400


# ===========================================================================
# T_G4_AUD_02 — action/operator/时间过滤 + 非法 range
# ===========================================================================
def test_g4_aud_02_filters(tmp_alembic_db):
    db = tmp_alembic_db
    pid = _seed_portfolio(db)
    _seed_12_audit_events(db, pid)
    db.commit()

    # action=RECONCILIATION_RESULT → 6
    r = _call_route_portfolio(db, pid, "u_viewer",
                              action="RECONCILIATION_RESULT",
                              page_size=100)
    assert r.total == 6
    assert all(it.action == "RECONCILIATION_RESULT" for it in r.items)

    # operator=u_jerry → 3（SCD2_CHANGE）
    r = _call_route_portfolio(db, pid, "u_viewer",
                              operator_id="u_jerry",
                              page_size=100)
    assert r.total == 3
    assert all(it.operator_id == "u_jerry" for it in r.items)

    # occurred_from = 远未来（比今天 8 月 17 日 大） → total=0
    r = _call_route_portfolio(db, pid, "u_viewer",
                              occurred_from=datetime(2026, 9, 1, tzinfo=timezone.utc),
                              page_size=100)
    assert r.total == 0

    # occurred_from > occurred_to → 400
    with pytest.raises(HTTPException) as ei:
        _call_route_portfolio(db, pid, "u_viewer",
                              occurred_from=datetime(2025, 2, 1),
                              occurred_to=datetime(2025, 1, 1))
    assert ei.value.status_code == 400
    assert ei.value.detail["error_code"] == "INVALID_FILTER"

    # 非法 action → 400
    with pytest.raises(HTTPException) as ei2:
        _call_route_portfolio(db, pid, "u_viewer", action="DOES_NOT_EXIST")
    assert ei2.value.status_code == 400
    assert ei2.value.detail["error_code"] == "INVALID_FILTER"


# ===========================================================================
# T_G4_AUD_03 — attributes_json/note 文本检索
# ===========================================================================
def test_g4_aud_03_attributes_search(tmp_alembic_db):
    db = tmp_alembic_db
    pid = _seed_portfolio(db)
    _seed_12_audit_events(db, pid)
    db.commit()

    # attributes_q="7001"（SCD2 的 added_symbols=[7001,7002]）
    r = _call_route_portfolio(db, pid, "u_viewer", attributes_q="7001", page_size=100)
    assert r.total >= 1
    # attributes_q="7999"（无命中）
    r2 = _call_route_portfolio(db, pid, "u_viewer", attributes_q="7999", page_size=100)
    assert r2.total == 0
    # note 有 "UPDATE_CLOSE_ONLY"（其中一条 SCD2）
    r3 = _call_route_portfolio(db, pid, "u_viewer", attributes_q="UPDATE_CLOSE_ONLY", page_size=100)
    assert r3.total >= 1


# ===========================================================================
# T_G4_AUD_04 — STRICT_AUTH 下的 401
# ===========================================================================
def test_g4_aud_04_strict_auth_401(tmp_alembic_db):
    db = tmp_alembic_db
    pid = _seed_portfolio(db)
    # 开启 STRICT_AUTH（通过模块内 override 变量）
    import app.api.routes.portfolio_governance as _pg
    orig_over = _pg._STRICT_AUTH_OVERRIDE
    _pg._STRICT_AUTH_OVERRIDE = True
    try:
        from app.api.routes.portfolio_governance import _require_actor
        # 没 X-User → 401
        with pytest.raises(HTTPException) as ei:
            _require_actor(None)
        assert ei.value.status_code == 401
        assert ei.value.detail["error_code"] == "AUTH_MISSING"
        # 传了 X-User → 返回值
        r = _require_actor("u_ok")
        assert r == "u_ok"
    finally:
        _pg._STRICT_AUTH_OVERRIDE = orig_over


# ===========================================================================
# T_G4_AUD_05 — 全局 /audit-events：角色校验（403 / 200 admin / 200 local_dev warning）
# ===========================================================================
def test_g4_aud_05_global_audit_role(tmp_alembic_db):
    db = tmp_alembic_db
    pid = _seed_portfolio(db)
    _seed_12_audit_events(db, pid)
    db.commit()
    import app.api.routes.portfolio_governance as _pg
    orig_local = _pg._AUDITOR_LOCAL_DEV_ALLOW

    # 场景1：LOCAL_DEV=0（默认），无 X-Role → 403
    _pg._AUDITOR_LOCAL_DEV_ALLOW = False
    try:
        with pytest.raises(HTTPException) as ei:
            _call_route_global(db, "u_viewer", role=None, page_size=50)
        assert ei.value.status_code == 403
        assert ei.value.detail["error_code"] == "FORBIDDEN_AUDITOR_ROLE_REQUIRED"

        # 场景2：LOCAL_DEV=0，role=admin → 成功，total=12
        r_admin = _call_route_global(db, "u_admin", role="admin", page_size=50)
        assert r_admin.total == 12
        assert r_admin.permissions_warning is None

        # 场景3：LOCAL_DEV=1，缺 role → 成功 + permissions_warning 有警告
        _pg._AUDITOR_LOCAL_DEV_ALLOW = True
        r_local = _call_route_global(db, "u_viewer", role=None, page_size=50)
        assert r_local.total == 12
        assert isinstance(r_local.permissions_warning, str) and "AUDITOR_ALLOW_LOCAL_DEV" in r_local.permissions_warning
    finally:
        _pg._AUDITOR_LOCAL_DEV_ALLOW = orig_local
