"""One verified legacy-bar correction and append-only rule scores; no trading/model writes."""
from datetime import date, datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sys
from urllib.request import urlopen

root = Path(__file__).parent
sys.path.insert(0, str(root.resolve().parents[1]))
sys.stdout.reconfigure(encoding='utf-8')
import requests
from sqlalchemy import create_engine, event, select, update, text
from sqlalchemy.orm import Session
from app.core.config import build_mysql_url, load_db_config
from app.models.daily_bar import DailyBar
from app.models.score import Score
from app.models.scoring_config import ScoringConfig
from app.models.symbol import Symbol
from app.services.data_governance_audit import write_audit_event
from app.services.portfolio_inspection_diagnostics import resolve_inspection_calendar
from app.services.scoring_config_engine import calculate_symbol_score_with_config, parse_config_json, _enabled_external_factor_keys

output_file = root / 'stock-score-refresh-result.json'
if output_file.exists():
    raise SystemExit('Execution receipt already exists; refusing to repeat')
with urlopen('http://127.0.0.1:8000/api/v1/inspection-acceptance/status', timeout=5) as response:
    mode = json.load(response)
assert mode['mode'] == 'inspection_acceptance'
assert not any(mode[key] for key in ['background_jobs_enabled', 'automatic_trading_enabled',
    'trading_mutations_allowed', 'final_reviews_allowed'])
before = json.loads((root / 'preflight.json').read_text(encoding='utf-8'))
proofs = {item['symbol']: item for item in json.loads((root / 'stock-inputs-final-proof.json').read_text(encoding='utf-8'))}
assert set(proofs) == {'000001', '600428'}
assert proofs['000001']['status'] == 'VERIFIED_UNCHANGED' and proofs['000001']['correction'] is None
assert proofs['600428']['status'] == 'VERIFIED_AFTER_SPECIFIED_SINGLE_DAY_CORRECTION'
for proof in proofs.values():
    assert not proof['unresolved_price_conflicts'] and proof['cross_provider_price_atol_CNY'] == 0.02
    assert proof['recent_amount_max_gap_CNY'] == 0
    for receipt in proof['receipts']:
        path = root / receipt['path']
        assert path.resolve().is_relative_to(root.resolve())
        assert hashlib.sha256(path.read_bytes()).hexdigest() == receipt['sha256']
target = date.fromisoformat(before['trade_date'])
assert target == date(2026, 10, 23)
requests.sessions.Session.request = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('No HTTP during live maintenance'))
engine = create_engine(build_mysql_url(load_db_config()))
writes = []
@event.listens_for(engine, 'before_cursor_execute')
def restricted_writes(conn, cursor, statement, parameters, context, executemany):
    verb = statement.lstrip().split(None, 1)[0].upper()
    if verb in {'SELECT', 'SHOW', 'DESCRIBE', 'SET'}:
        return
    compiled = getattr(getattr(context, 'compiled', None), 'statement', None)
    table = getattr(getattr(compiled, 'table', None), 'name', None)
    if verb == 'UPDATE' and table == 'daily_bars' and not any(row['verb'] == 'UPDATE' for row in writes):
        writes.append({'verb': verb, 'table': table})
        return
    if verb == 'INSERT' and table in {'scores', 'data_governance_audit_events'}:
        writes.append({'verb': verb, 'table': table})
        return
    raise RuntimeError('Unexpected mutation refused: ' + verb + ':' + str(table))

def row_values(row):
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}

def digest(value):
    return hashlib.sha256(json.dumps(value, default=str, sort_keys=True).encode()).hexdigest()

completed, corrections = [], []
with Session(engine) as db:
    assert db.scalar(text("SELECT COUNT(*) FROM async_tasks WHERE status IN ('queued','running')")) == 0
    assert resolve_inspection_calendar(db, current_read=True)['expected_trade_date'] == target
    for entry in before['score_inputs']:
        code = entry['symbol']['symbol']
        if code not in proofs:
            continue
        proof = proofs[code]
        symbol = db.get(Symbol, entry['symbol']['id'], with_for_update=True)
        config = db.get(ScoringConfig, entry['config']['id'], with_for_update=True)
        assert config.is_active and config.is_latest and config.version == entry['config']['version']
        assert config.asset_type == symbol.asset_type and digest(config.config_json) == entry['config_sha256']
        assert not _enabled_external_factor_keys(parse_config_json(config))
        bars = list(db.scalars(select(DailyBar).where(DailyBar.symbol_id == symbol.id, DailyBar.trade_date <= target)
            .order_by(DailyBar.trade_date.desc()).limit(80).with_for_update()))[::-1]
        assert digest([row_values(bar) for bar in bars]) == entry['bars_sha256']
        correction = proof['correction']
        if correction:
            original, replacement = correction['before'], correction['after']
            assert symbol.id == 3215 and original['trade_date'] == '2026-08-07'
            bar = next(bar for bar in bars if bar.id == original['id'])
            assert digest(row_values(bar)) == digest(original)
            fields = ['open', 'high', 'low', 'close', 'volume', 'amount', 'turnover_rate', 'source']
            statement = update(DailyBar).where(DailyBar.id == original['id'], DailyBar.symbol_id == 3215,
                DailyBar.trade_date == date(2026, 8, 7)).values({key: replacement[key] for key in fields})
            response = db.execute(statement.execution_options(synchronize_session=False))
            assert response.rowcount == 1
            db.refresh(bar)
            assert digest(row_values(bar)) == digest(replacement)
            audit = write_audit_event(db, 'UNKNOWN_AUDIT_ACTION', portfolio_id=1, symbol_id=3215,
                business_key='main-verified-one-bar-correction-20261024:600428:20260807',
                occurred_at=datetime.now(timezone.utc).replace(tzinfo=None), operator_id='codex-local-maintenance',
                before=original, after=replacement,
                attributes={'original_action': 'HISTORICAL_BAR_CORRECTED', 'scope': 'single_existing_score_input_bar',
                    'objects': [{'symbol_id': 3215, 'symbol': code, 'trade_date': '2026-08-07'}],
                    'input_verification': proof, 'model_strategy_or_trading_changed': False})
            corrections.append({'symbol': code, 'trade_date': '2026-08-07', 'before': original,
                'after': replacement, 'audit_event_id': audit.event_id})
        batch = f'sc-{config.id}-v{config.version}-{target.isoformat()}'
        assert db.scalar(select(Score.id).where(Score.symbol_id == symbol.id, Score.trade_date == target,
            Score.calc_batch_id == batch).with_for_update()) is None
        score = calculate_symbol_score_with_config(db, symbol, target, config=config)
        assert score.weight_mode == 'manual' and score.factor_model_run_id is None and score.factor_set_id is None
        assert score.pit_safety == 'NOT_PIT_SAFE'
        assert all(math.isfinite(getattr(score, key)) for key in ['quality_score', 'timing_score', 'priority_score'])
        output = {'symbol': code, 'name': symbol.name, 'score_id': score.id, 'trade_date': target.isoformat(),
            'config_id': config.id, 'config_name': config.name, 'config_version': config.version,
            'quality_score': score.quality_score, 'timing_score': score.timing_score, 'priority_score': score.priority_score,
            'weight_mode': score.weight_mode, 'pit_safety': score.pit_safety,
            'published_at': score.published_at.isoformat(), 'input_bars_sha256': digest([row_values(bar) for bar in bars]),
            'configuration_sha256': entry['config_sha256'], 'production_model_score': False,
            'runtime_state_changed': False, 'formal_binding_remains_blocked': True}
        audit = write_audit_event(db, 'UNKNOWN_AUDIT_ACTION', portfolio_id=1, symbol_id=symbol.id,
            business_key='main-safe-rule-score-20261024:' + str(symbol.id),
            occurred_at=datetime.now(timezone.utc).replace(tzinfo=None), operator_id='codex-local-maintenance', after=output,
            attributes={'original_action': 'INSPECTION_RULE_SCORE_REFRESH', 'scope': 'current_active_rule_only',
                'objects': [{'symbol_id': symbol.id, 'symbol': code, 'trade_date': target.isoformat()}],
                'does_not_resolve_frozen_model_score': True, 'input_verification': proof})
        output['audit_event_id'] = audit.event_id
        completed.append(output)
    assert len(corrections) == 1 and len(completed) == 2
    assert sum(row['verb'] == 'UPDATE' for row in writes) == 1
    db.commit()
engine.dispose()
result = {'completed': completed, 'historical_bar_corrections': corrections, 'writes': writes,
    'old_score_rows_overwritten': False, 'successful_sync_steps_rerun': False, 'formal_binding_remains_blocked': True,
    'model_or_strategy_changed': False, 'trading_resumed': False, 'orders_created': False,
    'baseline_accepted': False, 'admission_unfrozen': False}
output_file.write_text(json.dumps(result, ensure_ascii=False, default=str, indent=2), encoding='utf-8')
print(json.dumps({'scores': completed, 'corrected_bar': '600428/2026-08-07', 'writes': writes}, ensure_ascii=False))
