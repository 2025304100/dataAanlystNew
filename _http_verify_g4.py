"""修正后的 HTTP 验证脚本：
- STRICT_AUTH / LOCAL_DEV 动态读 env（路由已改），同进程可切换
- X-User / X-Role 通过 Query alias（作为 query 参数，而不是 HTTP header）
- operator 过滤 query 别名：operator 不是 operator_id
- transition-state body 字段：to_state 不是 to_status
- 错误码检查：HTTPException 包装后的 error_code 现在会用 detail.error_code（main.py 已修）
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

FD, DB_PATH = tempfile.mkstemp(suffix=".db", prefix="qa_http_g4_")
os.close(FD)
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["ALEMBIC_DATABASE_URL"] = os.environ["DATABASE_URL"]
# 初始值（验证脚本稍后动态切换）
os.environ["AUDITOR_ALLOW_LOCAL_DEV"] = "0"
os.environ["STRICT_AUTH"] = "0"
PORT = 8765
BASE = f"http://127.0.0.1:{PORT}/api/v1"


def step(msg):
    print(f"\n=== {msg} ===", flush=True)


# ── 1. alembic upgrade head ────────────────────────────────────────────────
step("1. Alembic upgrade head on temp SQLite")
from alembic.config import Config
from alembic import command as acmd

cfg = Config(str(ROOT / "alembic.ini"))
cfg.set_main_option("script_location", str(ROOT / "alembic"))
acmd.upgrade(cfg, "head")
print("  -> OK")

# ── 2. 启动 uvicorn（线程） ────────────────────────────────────────────────
step(f"2. Start uvicorn worker at 127.0.0.1:{PORT}")


def _run_server():
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host="127.0.0.1", port=PORT,
        log_level="warning", access_log=False,
        reload=False,
    )


srv_thread = threading.Thread(target=_run_server, daemon=True, name="uvicorn-g4")
srv_thread.start()

import httpx
alive = False
for i in range(50):
    try:
        r = httpx.get(f"http://127.0.0.1:{PORT}/docs", timeout=0.5)
        if r.status_code < 500:
            alive = True
            break
    except Exception:
        pass
    time.sleep(0.4)
if not alive:
    print("  !! Server failed to alive")
    sys.exit(1)
print("  -> Server alive")

# ── 3. 同进程共享 DBManager 种子 ───────────────────────────────────────────
step("3. Seed via shared DatabaseManager session")
from app.db.manager import DatabaseManager
from app.models.portfolio import Portfolio
from app.services.portfolio_state_machine import (
    transition_portfolio_state as tps,
)
from importlib import import_module

mgr = DatabaseManager.get()
Sess = mgr.session_factory
db = Sess()

p = Portfolio(
    name=f"g4_h_A_{int(time.time()*1000)}", account_type="simulation", asset_scope="mixed",
    total_capital=1_000_000.0, investable_ratio=0.95, cash_reserve_ratio=0.05,
    benchmark_code="000300", buy_fee_pct=0.00025, sell_fee_pct=0.00025,
    default_single_position_pct=0.30, is_test=1,
    last_decision_trade_date=date(2025, 1, 14),
    last_reconciled_trade_date=date(2025, 1, 14),
)
db.add(p); db.flush(); db.refresh(p)
PID_READY = p.id
tps(db, PID_READY, "READY", operator_id="u_admin")

tmod = import_module("tests.test_g4_audit_events_api")
tmod._seed_12_audit_events(db, PID_READY)

pb = Portfolio(
    name=f"g4_h_B_{int(time.time()*1000)}", account_type="simulation", asset_scope="mixed",
    total_capital=500_000.0, investable_ratio=0.9, cash_reserve_ratio=0.1,
    benchmark_code="000905", buy_fee_pct=0.0003, sell_fee_pct=0.0003,
    default_single_position_pct=0.25, is_test=1,
)
db.add(pb); db.flush(); db.refresh(pb)
PID_PAUSED = pb.id
tps(db, PID_PAUSED, "READY", operator_id="u_admin")
tps(db, PID_PAUSED, "ADMIN_PAUSED", operator_id="u_admin")

pc = Portfolio(
    name=f"g4_h_C_{int(time.time()*1000)}", account_type="simulation", asset_scope="mixed",
    total_capital=1_000_000.0, investable_ratio=0.95, cash_reserve_ratio=0.05,
    benchmark_code="000300", buy_fee_pct=0.00025, sell_fee_pct=0.00025,
    default_single_position_pct=0.30, is_test=1,
    last_decision_trade_date=date(2025, 1, 15),
    last_reconciled_trade_date=date(2025, 1, 10),
)
db.add(pc); db.flush(); db.refresh(pc)
PID_GAP = pc.id
tps(db, PID_GAP, "READY", operator_id="u_admin")

db.commit(); db.close()
print(f"  PID_READY={PID_READY}  PID_PAUSED={PID_PAUSED}  PID_GAP={PID_GAP}")
print("  -> Seeded")

# ── 4. HTTP 验证 ──────────────────────────────────────────────────────────
RESULTS: list[tuple[str, bool, str]] = []

def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}  {'' if cond else ' :: ' + detail}")


def user_actor(p):
    """在 URL query 中加上 X-User 等 Query alias 参数，与路由参数定义对齐。"""
    # p: dict (merged into query params list for httpx)
    return p


# 常用 client：传 query 参数而不是 header（因为路由用 Query(alias=...)）
def g(url, /, *, actor=None, role=None, **params):
    if actor:
        params["X-User"] = actor
    if role:
        params["X-Role"] = role
    return httpx.get(url, params=params, timeout=10)

def pj(url, json_body, /, *, actor=None, role=None, **params):
    if actor:
        params["X-User"] = actor
    if role:
        params["X-Role"] = role
    return httpx.post(url, params=params, json=json_body, timeout=10)


step("4. Audit API HTTP 验证")

# 4.1 单组合分页
r = g(f"{BASE}/portfolios/{PID_READY}/audit-events",
     actor="u_viewer", page=1, page_size=3)
j = r.json() if r.status_code == 200 else r.text
check("(4.1) HTTP 200", r.status_code == 200, f"status={r.status_code} {str(j)[:220]}")
if r.status_code == 200:
    check("(4.1) len(items)=3", len(j.get("items", [])) == 3, f"len={len(j.get('items', []))}")
    check("(4.1) total=12", j.get("total") == 12, f"total={j.get('total')}")
    check("(4.1) page=1, page_size=3",
          j.get("page") == 1 and j.get("page_size") == 3,
          f"page={j.get('page')} ps={j.get('page_size')}")

# 4.2 action 过滤
r2 = g(f"{BASE}/portfolios/{PID_READY}/audit-events",
       actor="u_viewer", action="BENCHMARK_SOURCE_FAILOVER", page_size=100)
check("(4.2) action=BENCHMARK HTTP 200", r2.status_code == 200, f"status={r2.status_code}")
if r2.status_code == 200:
    j2 = r2.json()
    check("(4.2) total=2", j2.get("total") == 2, f"total={j2.get('total')}")

# 4.3 operator 过滤（query 别名 operator）
r2b = g(f"{BASE}/portfolios/{PID_READY}/audit-events",
        actor="u_viewer", operator="u_jerry", page_size=100)
check("(4.3) operator=u_jerry HTTP 200", r2b.status_code == 200, f"status={r2b.status_code}")
if r2b.status_code == 200:
    j2b = r2b.json()
    check("(4.3) total=3", j2b.get("total") == 3, f"total={j2b.get('total')}")
    check("(4.3) items.operator_id 均为 u_jerry",
          all(it.get("operator_id") == "u_jerry" for it in j2b.get("items", [])),
          f"operators={[it.get('operator_id') for it in j2b.get('items', [])[:5]]}")

# 4.4 attributes_q=7001
r3 = g(f"{BASE}/portfolios/{PID_READY}/audit-events",
       actor="u_viewer", attributes_q="7001", page_size=100)
check("(4.4) attributes_q=7001 HTTP 200", r3.status_code == 200, f"status={r3.status_code}")
if r3.status_code == 200:
    j3 = r3.json()
    check("(4.4) total>=1", j3.get("total", 0) >= 1, f"total={j3.get('total')}")

# 4.5 非法 action → 400 且 error_code=INVALID_FILTER
r4 = g(f"{BASE}/portfolios/{PID_READY}/audit-events",
       actor="u_viewer", action="MADE_UP_ACT", page_size=50)
check("(4.5) illegal action → 400", r4.status_code == 400, f"status={r4.status_code}")
if r4.status_code == 400:
    j4 = r4.json()
    ec = (j4 or {}).get("error_code")
    check("(4.5) error_code=INVALID_FILTER", ec == "INVALID_FILTER",
          f"ec={ec!r} payload={str(j4)[:260]}")

# 4.6 occurred_from > occurred_to → 400 + INVALID_FILTER
r5 = g(f"{BASE}/portfolios/{PID_READY}/audit-events",
       actor="u_viewer",
       occurred_from="2026-08-01T00:00:00Z",
       occurred_to="2026-01-01T00:00:00Z",
       page_size=50)
check("(4.6) occurred_from>to → 400", r5.status_code == 400, f"status={r5.status_code}")
if r5.status_code == 400:
    j5 = r5.json() or {}
    ec = j5.get("error_code")
    check("(4.6) error_code=INVALID_FILTER", ec == "INVALID_FILTER",
          f"ec={ec!r} payload={str(j5)[:240]}")

# 4.7 STRICT_AUTH=1 无 X-User → 401 AUTH_MISSING
prev_sa = os.environ.get("STRICT_AUTH")
os.environ["STRICT_AUTH"] = "1"
try:
    r6 = g(f"{BASE}/portfolios/{PID_READY}/audit-events",
           page=1, page_size=5)  # 不传 actor（X-User 为空）
    check("(4.7) STRICT_AUTH=1 & no X-User → 401", r6.status_code == 401,
          f"status={r6.status_code} body={r6.text[:220]}")
    if r6.status_code == 401:
        j6 = r6.json() or {}
        ec = j6.get("error_code")
        check("(4.7) error_code=AUTH_MISSING", ec == "AUTH_MISSING",
              f"ec={ec!r} payload={str(j6)[:220]}")
finally:
    if prev_sa is None:
        os.environ.pop("STRICT_AUTH", None)
    else:
        os.environ["STRICT_AUTH"] = prev_sa

# 4.8 全局 /audit-events LOCAL_DEV=0 无 X-Role → 403 FORBIDDEN_AUDITOR_ROLE_REQUIRED
os.environ["AUDITOR_ALLOW_LOCAL_DEV"] = "0"
r7 = g(f"{BASE}/audit-events", actor="u_viewer", page=1, page_size=50)
check("(4.8) global no role & LOCAL_DEV=0 → 403", r7.status_code == 403,
      f"status={r7.status_code} body={r7.text[:240]}")
if r7.status_code == 403:
    j7 = r7.json() or {}
    ec = j7.get("error_code")
    check("(4.8) error_code=FORBIDDEN_AUDITOR_ROLE_REQUIRED",
          ec == "FORBIDDEN_AUDITOR_ROLE_REQUIRED",
          f"ec={ec!r} payload={str(j7)[:240]}")

# 4.9 全局 /audit-events X-Role=admin → 200 total>=12
r8 = g(f"{BASE}/audit-events", actor="u_admin", role="admin", page=1, page_size=200)
check("(4.9) global X-Role=admin → 200", r8.status_code == 200,
      f"status={r8.status_code} body={r8.text[:240]}")
if r8.status_code == 200:
    j8 = r8.json()
    check("(4.9) total>=12", j8.get("total", 0) >= 12, f"total={j8.get('total')}")
    # permissions_warning 应该为空（有真实 role）
    check("(4.9) permissions_warning=None (admin role)",
          j8.get("permissions_warning") is None,
          f"warn={j8.get('permissions_warning')}")


step("5. Cron 门禁 HTTP 验证")

# 5.1 READY + 20:30 → proceed=True / READY→RUNNING_AUTO_SIMULATION
r = pj(f"{BASE}/portfolios/{PID_READY}/auto-simulation/preflight",
       {"trade_date": "2025-01-15", "decision_at": "2025-01-15T20:30:00+00:00"},
       actor="cron_worker", role="admin")
check("(5.1) cron READY 20:30 → HTTP 200", r.status_code == 200,
      f"status={r.status_code} body={r.text[:260]}")
if r.status_code == 200:
    jr = r.json()
    check("(5.1) proceed=True", jr.get("proceed") is True, f"proceed={jr.get('proceed')}")
    check("(5.1) from=READY → to=RUNNING_AUTO_SIMULATION",
          jr.get("from_state") == "READY" and jr.get("to_state") == "RUNNING_AUTO_SIMULATION",
          f"from={jr.get('from_state')} to={jr.get('to_state')}")
    check("(5.1) transition_trace 包含 correlation_id",
          isinstance(jr.get("transition_trace"), dict)
          and bool(jr["transition_trace"].get("correlation_id")),
          f"trace={jr.get('transition_trace')}")

# 5.2 PID_PAUSED → PORTFOLIO_NOT_READY
r2 = pj(f"{BASE}/portfolios/{PID_PAUSED}/auto-simulation/preflight",
       {"trade_date": "2025-01-15", "decision_at": "2025-01-15T20:30:00+00:00"},
       actor="cron_worker", role="admin")
check("(5.2) cron ADMIN_PAUSED → HTTP 200", r2.status_code == 200,
      f"status={r2.status_code} body={r2.text[:260]}")
if r2.status_code == 200:
    j2 = r2.json()
    check("(5.2) proceed=False", j2.get("proceed") is False, f"proceed={j2.get('proceed')}")
    check("(5.2) skip_reason=PORTFOLIO_NOT_READY",
          j2.get("skip_reason") == "PORTFOLIO_NOT_READY", f"reason={j2.get('skip_reason')}")

# 5.3 transition-state（to_state，不是 to_status）回滚 READY 后，测 SCHEDULE_TOO_EARLY
rb = pj(f"{BASE}/portfolios/{PID_READY}/transition-state",
        {"to_state": "READY", "reason": "HTTP verify 5.3 reset"},
        actor="u_admin", role="admin")
check("(5.3-pre) transition-state to READY → 2xx", rb.status_code < 400,
      f"status={rb.status_code} body={rb.text[:200]}")
r3 = pj(f"{BASE}/portfolios/{PID_READY}/auto-simulation/preflight",
       {"trade_date": "2025-01-15", "decision_at": "2025-01-15T15:05:00+00:00"},
       actor="cron_worker", role="admin")
check("(5.3) cron 15:05 → HTTP 200", r3.status_code == 200,
      f"status={r3.status_code} body={r3.text[:260]}")
if r3.status_code == 200:
    j3 = r3.json()
    check("(5.3) proceed=False", j3.get("proceed") is False, f"proceed={j3.get('proceed')}")
    check("(5.3) skip_reason=SCHEDULE_TOO_EARLY",
          j3.get("skip_reason") == "SCHEDULE_TOO_EARLY",
          f"reason={j3.get('skip_reason')}")

# 5.4 PID_GAP → RECONCILIATION_GAP_WARNING（不阻断 proceed=True）
r4 = pj(f"{BASE}/portfolios/{PID_GAP}/auto-simulation/preflight",
       {"trade_date": "2025-01-16", "decision_at": "2025-01-16T20:30:00+00:00"},
       actor="cron_worker", role="admin")
check("(5.4) cron GAP portfolio → HTTP 200", r4.status_code == 200,
      f"status={r4.status_code} body={r4.text[:260]}")
if r4.status_code == 200:
    j4 = r4.json()
    check("(5.4) proceed=True (警告不阻断)",
          j4.get("proceed") is True, f"proceed={j4.get('proceed')}")
    check("(5.4) skip_reason=RECONCILIATION_GAP_WARNING",
          j4.get("skip_reason") == "RECONCILIATION_GAP_WARNING",
          f"reason={j4.get('skip_reason')}")
    check("(5.4) warnings 非空", bool(j4.get("warnings")), f"warnings={j4.get('warnings')}")
    # 状态已 RUNNING_AUTO_SIMULATION（但 proceed=True 的情况）
    check("(5.4) from=READY → RUNNING_AUTO_SIMULATION",
          j4.get("from_state") == "READY" and j4.get("to_state") == "RUNNING_AUTO_SIMULATION",
          f"from={j4.get('from_state')} to={j4.get('to_state')}")


step("6. OpenAPI /openapi.json 端点")
r = httpx.get(f"http://127.0.0.1:{PORT}/openapi.json", timeout=15)
check("(6.1) GET /openapi.json HTTP 200", r.status_code == 200, f"status={r.status_code}")
if r.status_code == 200:
    sch = r.json()
    paths = sch.get("paths", {})
    want = [
        "/api/v1/audit-events",
        "/api/v1/portfolios/{portfolio_id}/audit-events",
        "/api/v1/portfolios/{portfolio_id}/auto-simulation/preflight",
    ]
    for w in want:
        check(f"(6.2) OpenAPI 包含 {w}", w in paths, f"found={'yes' if w in paths else 'no'}")
    check("(6.3) schema paths >= 100", len(paths) >= 100, f"paths={len(paths)}")
    schemas = sch.get("components", {}).get("schemas", {})
    check("(6.4) schema components >= 150", len(schemas) >= 150, f"schemas={len(schemas)}")
    # 确认核心 schema 都有
    want_schemas = ["AuditEventPage", "AuditEventRead",
                    "AutoSimulationPreflightRequest", "AutoSimulationPreflightResponse"]
    for ws in want_schemas:
        check(f"(6.5) schema 组件含 {ws}", ws in schemas, f"{'yes' if ws in schemas else 'no'}")


# ── 汇总 ──────────────────────────────────────────────────────────────────
step("7. HTTP 验证 汇总")
passed = sum(1 for _, ok, _ in RESULTS if ok)
total = len(RESULTS)
width = max(len(n) for n, _, _ in RESULTS)
for n, ok, d in RESULTS:
    print(f"  {'PASS' if ok else 'FAIL':>4}  {n:<{width}}  {'' if ok else ' :: ' + d}")
print()
print(f"  总计 {passed}/{total} 通过")
print(f"  临时 DB 文件: {DB_PATH}")
print(f"  服务地址: http://127.0.0.1:{PORT}/docs")
print(f"    示例（本地浏览器手动体验）：")
print(f"    X-User=u_admin 作为 query 参数：/docs?X-User=u_admin&X-Role=admin")
print(f"    GET /api/v1/audit-events?X-User=u_admin&X-Role=admin&page=1&page_size=20")
print(f"    POST /api/v1/portfolios/{PID_READY}/auto-simulation/preflight?X-User=cron_worker&X-Role=admin")
print()
try:
    time.sleep(60)
except KeyboardInterrupt:
    pass
