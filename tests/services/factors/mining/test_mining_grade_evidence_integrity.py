"""Read evidence from the actual evaluation and persisted grade via HTTP."""
import json
from datetime import datetime

import pytest

from app.models.factor import Factor
from app.models.factor_evaluation import EvaluationRun
from app.models.factor_mining import FactorMiningCandidate, FactorMiningRun
from app.models.factor_model import FactorVersion
from test_factor_mining_routes import _client

pytestmark = pytest.mark.whitebox


@pytest.fixture
def evaluated_candidate(db_session):
    db = db_session
    factor = Factor(code='evidence_truth', name='Evidence truth', category='trend',
                    direction='positive', status='draft', is_active=0)
    db.add(factor)
    db.flush()
    metrics = dict(icir=.6, p_value=.001, p_adj_bonferroni=.02, ci_lower=.1,
                   ci_upper=.7, perm_p_value=.005, coverage=.9, oos_icir=.5,
                   correlation=.2, turnover=.2, complexity=2, decay_ratio=.8,
                   frequency='daily', degraded=False)
    version = FactorVersion(factor_id=factor.id, version=1, formula_expr='close',
                            quality_grade='A', grade_manual_adjusted=0,
                            grade_metrics_json=json.dumps({'grade': 'A',
                                'reason': '真实评估记录', 'metrics': metrics}))
    db.add(version)
    db.flush()
    run = FactorMiningRun(id='evidence-run', status='succeeded',
        candidate_pool_snapshot_id='evidence-snapshot',
        data_cutoff_at=datetime(2026, 10, 1), start_date=datetime(2025, 1, 1),
        end_date=datetime(2026, 9, 1), rebalance_frequency='daily')
    evaluation = EvaluationRun(id='evidence-eval', factor_version_id=version.id,
        metrics_json=json.dumps({'ic': {'icir': .6}, 'icir_raw': .6,
            'coverage': {'coverage': .9}, 'turnover': {'avg_turnover': .2},
            'correlation': .2,
            'stats': {'p_value': .001, 'p_adj_bonferroni': .02, 'q_value_fdr': .001,
                'ci_lower': .1, 'ci_upper': .7, 'perm_p_value': .005,
                'decay_ratio': .8, 'total_trials': 20, 'degraded': False}}))
    db.add_all([run, evaluation])
    db.flush()
    candidate = FactorMiningCandidate(id='evidence-candidate', run_id=run.id,
        factor_version_id=version.id, latest_evaluation_id=evaluation.id,
        latest_evaluation_status='done', canonical_formula='close', formula_expr='close',
        formula_hash='evidence-truth', generation_icir=.99, generation_coverage=.1,
        generation_complexity=2)
    db.add(candidate)
    db.commit()
    return db, candidate.id


def test_evidence_uses_saved_grade_and_real_out_of_sample_statistics(evaluated_candidate):
    db, candidate_id = evaluated_candidate
    response = _client(db).get(f'/factor-mining/candidates/{candidate_id}/grade/evidence')
    assert response.status_code == 200, response.text
    evidence = response.json()
    assert evidence['grade'] == 'A'
    assert evidence['metrics']['icir'] == .6
    assert evidence['stats_view']['bonferroni_p'] == .02
    assert evidence['stats_view']['total_trials'] == 20
    assert evidence['stat_thresholds']['bonferroni']['value'] == .05


def test_manual_grade_is_authoritative_until_restore_recomputes_from_real_evidence(evaluated_candidate):
    db, candidate_id = evaluated_candidate
    client = _client(db)
    base = f'/factor-mining/candidates/{candidate_id}/grade'
    changed = client.post(base + '/manual', json={'grade': 'S', 'reason': '人工复核后明确调整本次等级结论'})
    assert changed.status_code == 200, changed.text
    evidence = client.get(base + '/evidence').json()
    assert evidence['grade'] == 'S' and evidence['manual_adjusted'] is True
    assert evidence['stats_view']['bonferroni_p'] == .02
    restored = client.post(base + '/restore-auto')
    assert restored.status_code == 200, restored.text
    evidence = client.get(base + '/evidence').json()
    assert evidence['grade'] == 'A' and evidence['manual_adjusted'] is False
    assert evidence['grade_history'][0]['trigger'] == 'auto'


def test_legacy_nonfinite_evidence_remains_readable_as_missing(evaluated_candidate):
    db, candidate_id = evaluated_candidate
    candidate = db.get(FactorMiningCandidate, candidate_id)
    version = db.get(FactorVersion, candidate.factor_version_id)
    saved = json.loads(version.grade_metrics_json)
    saved['metrics']['correlation'] = float('nan')
    version.grade_metrics_json = json.dumps(saved)
    evaluation = db.get(EvaluationRun, candidate.latest_evaluation_id)
    actual = json.loads(evaluation.metrics_json)
    actual['stats']['ci_lower'] = float('nan')
    actual['stats']['walk_forward'] = {'icirs': [float('inf')]}
    evaluation.metrics_json = json.dumps(actual)
    db.commit()
    response = _client(db).get(f'/factor-mining/candidates/{candidate_id}/grade/evidence')
    assert response.status_code == 200, response.text
    evidence = response.json()
    correlation = next(d for d in evidence['dimensions'] if d['key'] == 'correlation')
    assert correlation['current'] is None and correlation['passed'] is None
    assert evidence['stats_view']['bootstrap_ci'] is None
    assert evidence['stats']['walk_forward']['icirs'] == [None]
