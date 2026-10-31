from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from app.services import market_history_evidence as evidence
from app.services.market_history_composition import merge_history_sources, write_composed_history
from tests.test_trusted_history_training_pipeline import _dataset

pytestmark = pytest.mark.whitebox


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def leaves(tmp_path, monkeypatch):
    refs=[]
    common=tmp_path/'common'; common.mkdir()
    for name,count in [('original-leaf',28),('recent-leaf',35)]:
        _,_,directory=_dataset(tmp_path/name,monkeypatch,day_count=count)
        destination=common/name
        evidence._commit_staged_directory(directory, destination)
        quality=json.loads((destination/'quality.json').read_bytes());quality['dataset_id']=name
        (destination/'quality.json').write_text(json.dumps(quality),encoding='utf-8')
        manifest=json.loads((destination/'manifest.json').read_bytes());manifest.update(id=name,quality_sha256=_sha(destination/'quality.json'))
        (destination/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
        refs.append(dict(dataset_id=name,manifest_sha256=_sha(destination/'manifest.json')))
    monkeypatch.setattr(evidence,'TRUSTED_HISTORY_DATASET_ROOT',common)
    return refs


def test_real_leaf_graph_is_replayed_and_union_stays_research_only(leaves):
    before={r['dataset_id']:_sha(evidence.TRUSTED_HISTORY_DATASET_ROOT/r['dataset_id']/'manifest.json') for r in leaves}
    symbols=[f'{i+1:06d}'for i in range(25)]
    dataset=write_composed_history(dataset_id='union',references=leaves,symbols=symbols,plan_id='a'*64,plan_sha256='b'*64)
    assert len(dataset.calendar)==35 and len(dataset.bars)==35*24
    assert dataset.symbols==tuple(symbols)  # Missing stock remains in denominator.
    assert dataset.identity['research_only'] and not dataset.identity['point_in_time_certified']
    assert dataset.identity['conflict_rows_excluded']==0
    pd.testing.assert_frame_equal(evidence.load_trusted_history_dataset('union').bars,dataset.bars)
    assert before=={r['dataset_id']:_sha(evidence.TRUSTED_HISTORY_DATASET_ROOT/r['dataset_id']/'manifest.json')for r in leaves}
    with pytest.raises(ValueError,match='immutable_conflict'):
        write_composed_history(dataset_id='union',references=leaves,symbols=symbols,plan_id='a'*64,plan_sha256='b'*64)


@pytest.mark.parametrize('changed',['bars','selection','parent'])
def test_updated_container_digest_cannot_bypass_leaf_or_selection_evidence(leaves,changed):
    dataset=write_composed_history(dataset_id='union',references=leaves,symbols=[f'{i+1:06d}'for i in range(24)],plan_id='a'*64,plan_sha256='b'*64)
    directory=dataset.directory;manifest=json.loads((directory/'manifest.json').read_bytes())
    if changed=='bars':
        frame=pd.read_parquet(directory/'bars.parquet');frame.loc[0,'close']+=1
        frame.to_parquet(directory/'bars.parquet',index=False);manifest['bars_sha256']=_sha(directory/'bars.parquet')
    elif changed=='selection':
        quality=json.loads((directory/'quality.json').read_bytes());quality['selection'][0]['dates']=[]
        (directory/'quality.json').write_text(json.dumps(quality),encoding='utf-8');manifest['quality_sha256']=_sha(directory/'quality.json')
    else:
        parent=evidence.TRUSTED_HISTORY_DATASET_ROOT/leaves[0]['dataset_id']/'manifest.json'
        parent.write_bytes(parent.read_bytes()+b'\n')
    (directory/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
    with pytest.raises(ValueError,match='composition_'):
        evidence.load_trusted_history_dataset('union')


def test_composition_commit_retries_transient_access_denial(leaves, monkeypatch):
    from pathlib import Path
    original = Path.rename; calls = []
    def rename(stage, destination):
        if stage.name.startswith('.composition-'):
            calls.append(stage)
            if len(calls) < 3: raise PermissionError('synthetic Windows sharing denial')
        return original(stage, destination)
    monkeypatch.setattr(Path, 'rename', rename)
    dataset = write_composed_history(dataset_id='union', references=leaves,
        symbols=[f'{i+1:06d}' for i in range(24)], plan_id='a'*64, plan_sha256='b'*64)
    assert len(calls) == 3 and len(dataset.bars) == 35*24
    pd.testing.assert_frame_equal(evidence.load_trusted_history_dataset('union').bars, dataset.bars)


def test_composition_commit_access_denial_cannot_overwrite_concurrent_publisher(leaves, monkeypatch):
    from pathlib import Path
    original = Path.rename; winner = b'synthetic concurrent publisher'
    def rename(stage, destination):
        if stage.name.startswith('.composition-'):
            destination.mkdir(); (destination/'manifest.json').write_bytes(winner)
            raise PermissionError('synthetic concurrent publish')
        return original(stage, destination)
    monkeypatch.setattr(Path, 'rename', rename)
    with pytest.raises(ValueError, match='immutable_conflict'):
        write_composed_history(dataset_id='union', references=leaves,
            symbols=[f'{i+1:06d}' for i in range(24)], plan_id='a'*64, plan_sha256='b'*64)
    assert (evidence.TRUSTED_HISTORY_DATASET_ROOT/'union/manifest.json').read_bytes() == winner


def test_cycles_and_nested_composition_fail_before_recursive_loading(leaves):
    dataset=write_composed_history(dataset_id='union',references=leaves,symbols=[f'{i+1:06d}'for i in range(24)],plan_id='a'*64,plan_sha256='b'*64)
    path=dataset.directory/'manifest.json';manifest=json.loads(path.read_bytes())
    manifest['composition']['parents'][0]['dataset_id']='union'
    path.write_text(json.dumps(manifest),encoding='utf-8')
    with pytest.raises(ValueError,match='requires_leaf_sources'):
        evidence.load_trusted_history_dataset('union')


def test_conflicting_keys_are_excluded_exactly_and_calendar_is_not_shortened():
    days=tuple(pd.bdate_range('2024-01-02',periods=3).date)
    frame=pd.DataFrame([dict(symbol=s,trade_date=pd.Timestamp(day),open=1.,high=1.,low=1.,close=1.,volume=100.,amount=100.,turnover_rate=1.) for s in ('000001','000002')for day in days])
    newer=frame.copy();newer.loc[0,'turnover_rate']+=.000001  # No loosened precision tolerance.
    parents=[SimpleNamespace(dataset_id='old',bars=frame,symbols=('000001','000002'),calendar=days),
             SimpleNamespace(dataset_id='new',bars=newer,symbols=('000001','000002'),calendar=days)]
    result,calendar,quality=merge_history_sources(parents,('000001','000002','000003'))
    assert len(result)==5 and calendar==days
    assert quality['conflict_rows']==1 and quality['cohort_size']==3
    assert quality['per_symbol']['000003']['accepted_rows']==0
    assert quality['performance_based_selection']is False
    assert quality['conflicts'][0]['observations']['old']['turnover_rate']==1.
    newer.loc[newer.trade_date.eq(pd.Timestamp(days[0])),'turnover_rate']+=1
    with pytest.raises(ValueError,match='whole_calendar_day_quarantined'):
        merge_history_sources(parents,('000001','000002','000003'))


def test_source_id_is_not_a_filesystem_path(leaves):
    bad=deepcopy(leaves);bad[0]['dataset_id']='../escape'
    with pytest.raises(ValueError,match='invalid_source_identity'):
        write_composed_history(dataset_id='union',references=bad,symbols=['000001'],plan_id='a'*64,plan_sha256='b'*64)
