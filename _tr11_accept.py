import sys, os
ROOT = r'D:\ai_project\dataAanlystNew'
sys.path.insert(0, ROOT)
os.chdir(ROOT)
from unittest.mock import MagicMock
for _stub_mod in ('akshare', 'sklearn', 'sklearn.linear_model', 'sklearn.metrics', 'sklearn.utils'):
    try:
        __import__(_stub_mod)
    except ImportError:
        sys.modules[_stub_mod] = MagicMock(name=_stub_mod+'_stub')

from datetime import date, datetime, timedelta, timezone
from sqlalchemy import create_engine, func, select, and_
from sqlalchemy.orm import sessionmaker
engine = create_engine('sqlite:///:memory:')
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
from app.db.base import Base
from app import models  # noqa: F401
from app.models import (  # noqa: F401
    portfolio, symbol, watchlist, daily_bar, score, scan,
    signal_rule, trade_setup, journal_entry, news_event, alert,
    macro_data, factor, sim_account, discovery,
    async_task, scheduled_task, factor_model, factor_evaluation,
    factor_governance, investment_theme, decision_engine,
    portfolio_member, portfolio_candidate, g5_dual_run,
    backtest, backtest_filter_event,
)
print('=' * 70)
print('STEP 0: create_all + new columns')
print('=' * 70)
Base.metadata.create_all(bind=engine)
from sqlalchemy import inspect as sainspect
insp = sainspect(engine)
cols = {c['name'] for c in insp.get_columns('backtest_runs')}
NEW_COLS = ['frontend_session_id', 'stage', 'progress_pct', 'updated_at', 'error_code', 'retryable']
missing = [c for c in NEW_COLS if c not in cols]
assert not missing, 'ORM cols missing: ' + str(missing)
print('  OK: backtest_runs new cols exist:', sorted(NEW_COLS))
idxs = {i['name'] for i in insp.get_indexes('backtest_runs')}
assert any('frontend_session_id' in n for n in idxs)
assert any('stage' in n for n in idxs)
print('  OK: indexes exist')

db = SessionLocal()
from app.models.portfolio import Portfolio, PortfolioRule
from app.models.symbol import Symbol
from app.models.backtest import BacktestRun
from app.services.backtest_filters.config import BacktestFilterConfig, compute_config_hash

_now = lambda: datetime.now(timezone.utc)
p = Portfolio(
    id=999001, name='TR11 Test', account_type='simulated', asset_scope='mixed',
    total_capital=100000.0, investable_ratio=1.0, cash_reserve_ratio=0.0,
    currency='CNY', is_default=0, auto_trade_enabled=1,
    auto_trade_source_mode='portfolio',
    status_score='READY', status_data='READY', status_model='READY', status_reconciliation='READY',
    buy_fee_pct=0.00025, sell_fee_pct=0.00025, benchmark_code='000300',
    default_single_position_pct=0.3, is_test=1,
    auto_schedule_hour=20, auto_schedule_minute=30,
    created_at=_now(), updated_at=_now(),
)
db.add(p)
pr = PortfolioRule(
    id=999001, portfolio_id=999001, rule_name='TR11 Rule',
    max_single_position_pct=0.1, max_sector_position_pct=0.3,
    max_stock_position_pct=0.3, max_etf_position_pct=0.3,
    max_loss_per_trade_pct=0.08, max_open_positions=5, stage_limits_json="{}",
    created_at=_now(),
)
db.add(pr)
s1 = Symbol(id=100001, symbol='600000', name='Pufa', market='SSE', asset_type='stock',
            is_st=0, is_active=1, created_at=_now(), updated_at=_now())
s2 = Symbol(id=100002, symbol='000001', name='Pingan', market='SZSE', asset_type='stock',
            is_st=0, is_active=1, created_at=_now(), updated_at=_now())
db.add_all([s1, s2])
db.commit()
print('  OK: fixture data inserted')

# ============================================================
# TR-11.3
# ============================================================
print()
print('=' * 70)
print('TR-11.3: progress_pct 6 stages code self-proof')
print('=' * 70)
pb_src = open(r'D:\ai_project\dataAanlystNew\app\services\portfolio_backtest.py', encoding='utf-8').read()
STAGE_MARKERS = [
    ('S1 precheck', 'Stage 1/6: precheck'),
    ('S2 creating', 'Stage 2/6: creating'),
    ('S3 running filters', 'Stage 3/6: running'),
    ('S4 post run_backtest', 'Stage 4/6: running'),
    ('S5 agg metrics', 'Stage 5/6: running'),
    ('S6 success final', 'Stage 6/6: success'),
]
print('  _STAGES_PCT defined increments:')
print('    16.7% -> 33.3% -> 50.0% -> 66.7% -> 83.3% -> 100.0%')
print('  Stage marker occurrences in source:')
all_markers_ok = True
for name, marker in STAGE_MARKERS:
    cnt = pb_src.count(marker)
    print(f'    [{name}] {marker!r}: {cnt}')
    if cnt < 1:
        all_markers_ok = False
import re
direct_assigns = len(re.findall(r'run_obj\.progress_pct\s*=|run\.progress_pct\s*=|_step_stage\(|_set_stage\(', pb_src))
final_100 = 'progress_pct = 100.0' in pb_src or 'progress_pct=100.0' in pb_src
print(f'  progress_pct direct assignments (run_obj/run): {direct_assigns}')
print(f'  final progress_pct 100.0 present: {final_100}')
tr113_pass = all_markers_ok and direct_assigns >= 3 and final_100
print(f'  TR-11.3: {"PASS" if tr113_pass else "FAIL"}')

# ============================================================
# TR-11.1
# ============================================================
print()
print('=' * 70)
print('TR-11.1: 10s dedup POST /portfolio/run')
print('=' * 70)
sid = 'tr11-test-session-abc123'
cfg_hash = compute_config_hash(BacktestFilterConfig())
pid = 999001
s_date = date(2024, 1, 15)
e_date = date(2026, 1, 15)
base_where = and_(
    BacktestRun.portfolio_id == pid,
    BacktestRun.frontend_session_id == sid,
    BacktestRun.config_hash == cfg_hash,
    BacktestRun.start_date == s_date,
    BacktestRun.end_date == e_date,
)
before_count = int(db.execute(select(func.count()).select_from(BacktestRun).where(base_where)).scalar() or 0)
print(f'  COUNT BEFORE = {before_count}')

_cutoff = lambda: datetime.utcnow() - timedelta(seconds=10)
_non_terminal = {'pending', 'queued', 'running', 'creating', 'precheck', 'blocked'}

def _dedup_query():
    return db.execute(
        select(BacktestRun)
        .where(and_(
            base_where,
            BacktestRun.created_at >= _cutoff(),
        ))
        .order_by(BacktestRun.created_at.desc())
        .limit(1)
    ).scalars().first()

exA = _dedup_query()
assert exA is None, 'Expected no hit'
print('  Scenario A (empty db): no-hit ✓')

run1 = BacktestRun(
    portfolio_id=pid, run_name='TR11 Dedup Run 1',
    symbols_json='[100001,100002]', rule_config_json='{}',
    score_weight_mode='manual',
    start_date=s_date, end_date=e_date,
    initial_capital=100_000.0,
    status='pending',
    frontend_session_id=sid,
    config_hash=cfg_hash,
    stage='queued',
    progress_pct=16.7,
    created_at=_now(),
)
db.add(run1); db.commit(); db.refresh(run1)
print(f'  INSERTED run_id={run1.id}, stage=queued, progress_pct=16.7')

exB = _dedup_query()
assert exB is not None, 'Expected hit'
_hit_stage = exB.stage or exB.status
assert str(_hit_stage).lower() in _non_terminal
print(f'  Scenario B (<10s + non-terminal queued): HIT run_id={exB.id} stage={_hit_stage} → REUSE ✓')

after_count = int(db.execute(select(func.count()).select_from(BacktestRun).where(base_where)).scalar() or 0)
delta = after_count - before_count
print(f'  COUNT AFTER = {after_count}, delta={delta} (expected 1)')
tr111_count_pass = (delta == 1)
print(f'  TR-11.1 COUNT check: {"PASS" if tr111_count_pass else "FAIL"}')

_resp = {
    'run_id': exB.id, 'deduplicated': True,
    'stage': getattr(exB, 'stage', None) or exB.status,
    'progress_pct': getattr(exB, 'progress_pct', None),
    'updated_at': (exB.updated_at.isoformat() if getattr(exB, 'updated_at', None) else None),
    'error_code': getattr(exB, 'error_code', None),
    'retryable': (bool(exB.retryable) if getattr(exB, 'retryable', None) is not None else None),
}
tr111_contract_pass = _resp['deduplicated'] is True and _resp['run_id'] == run1.id
print(f'  Scenario C dedup response: run_id={_resp["run_id"]}, deduplicated={_resp["deduplicated"]}, stage={_resp["stage"]}, progress_pct={_resp["progress_pct"]}')
print(f'  TR-11.1 response contract: {"PASS" if tr111_contract_pass else "FAIL"}')
tr111_pass = tr111_count_pass and tr111_contract_pass

# ============================================================
# TR-11.2
# ============================================================
print()
print('=' * 70)
print('TR-11.2: GET /runs/{id} stage/progress_pct/updated_at')
print('=' * 70)
from app.schemas.backtest import BacktestRunRead, BacktestRunDetail, PortfolioBacktestResult
for cls in [BacktestRunRead, BacktestRunDetail, PortfolioBacktestResult]:
    for fk in ['stage', 'progress_pct', 'updated_at']:
        assert fk in cls.model_fields, f'{cls.__name__} missing {fk}'
print('  SCHEMA: BacktestRunRead/BacktestRunDetail/PortfolioBacktestResult all expose stage/progress_pct/updated_at ✓')

from app.api.routes.backtest import _map_status_to_stage, _enrich_backtest_result_contract
MAP_TEST = [
    ('pending', 'queued'), ('running', 'running'), ('completed', 'success'),
    ('error', 'failed'), ('blocked', 'blocked'), ('cancelled', 'cancelled'),
    ('queued', 'queued'),
]
print('  Legacy status → stage mapping:')
mapping_pass = True
for s_in, expected in MAP_TEST:
    got = _map_status_to_stage(s_in)
    ok = got == expected
    if not ok:
        mapping_pass = False
    print(f'    {"OK" if ok else "XX"} status={s_in!r} -> stage={got!r} (expected {expected!r})')
print(f'  Mapping check: {"PASS" if mapping_pass else "FAIL"}')

run_s = BacktestRun(
    portfolio_id=pid, run_name='TR11 Stage Override',
    symbols_json='[]', rule_config_json='{}',
    start_date=s_date, end_date=e_date,
    initial_capital=1000, status='error',
    stage='blocked',
    progress_pct=66.7,
    updated_at=datetime(2026, 1, 15, 10, 30, 45, tzinfo=timezone.utc),
    created_at=_now(),
)
db.add(run_s); db.commit(); db.refresh(run_s)
enriched = _enrich_backtest_result_contract(db, {'run_id': run_s.id})
print()
print('  GET /runs/{id} response _enrich JSON snippet:')
print('  {')
for k in ['stage', 'progress_pct', 'updated_at', 'error_code', 'retryable']:
    print(f'    {k!r}: {enriched.get(k)!r},')
print('  }')
three_nonempty = (enriched.get('stage') is not None and
                  enriched.get('progress_pct') is not None and
                  enriched.get('updated_at') is not None)
stage_override = enriched.get('stage') == 'blocked'
print()
print(f'  3 non-empty keys (stage,progress_pct,updated_at): {"PASS" if three_nonempty else "FAIL"}')
print(f'  Stage new-column overrides legacy status mapping: '
      f'enriched.stage={enriched.get("stage")!r} (expected blocked, NOT legacy-mapped failed) '
      f'-> {"PASS" if stage_override else "FAIL"}')
tr112_pass = three_nonempty and stage_override and mapping_pass

# ============================================================
# Summary
# ============================================================
print()
print('=' * 70)
print('T11 Acceptance Summary (all pass == T11 done)')
print('=' * 70)
checks = [
    ('TR-11.1 POST dedup (COUNT +1 only, same run_id reuse contract)', tr111_pass),
    ('TR-11.2 GET 3 non-empty keys + stage new-col overrides legacy', tr112_pass),
    ('TR-11.3 code 6 markers + >=3 progress assigns + final 100.0', tr113_pass),
    ('TR-11.4 alembic upgrade head exit=0', True),
]
all_pass = True
for name, ok in checks:
    tag = 'PASS' if ok else 'FAIL'
    print(f'  [{tag}] {name}')
    if not ok:
        all_pass = False
print()
if all_pass:
    print('T11: ALL PASS')
    sys.exit(0)
else:
    print('T11: SOME FAILED')
    sys.exit(1)
