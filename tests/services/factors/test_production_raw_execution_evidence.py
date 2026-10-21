"""Unadjusted BaoStock receipts accompany, never replace, training prices."""
from datetime import date, datetime, timezone, timedelta
import pandas as pd
import pytest

pytestmark = pytest.mark.whitebox


def test_verified_raw_receipt_is_retained_separately_from_qfq_training_prices(tmp_path, monkeypatch):
    from app.services import market_history_evidence as history
    from app.services.factors.production_independent_evidence import verified_raw_prices
    monkeypatch.setattr(history, 'TRUSTED_HISTORY_DATASET_ROOT', tmp_path)
    day = date(2026, 10, 19); directory = tmp_path / 'raw-and-qfq'; refs = []
    for adjust, close in (('qfq', 10), ('', 20)):
        frame = pd.DataFrame([{'date':str(day),'code':'sz.000001','open':str(close),
            'high':str(close+1),'low':str(close-1),'close':str(close),'volume':'1000000',
            'amount':'20000000','turn':'2','adjustflag':'2' if adjust else '3'}])
        refs.append(history.capture_baostock_history(artifact_root=directory/'receipts',
            capture_id='qfq' if adjust else 'raw', request=history.HistoryRequest('sz000001',day,day,adjust),
            sdk_version='isolated-provider',raw_frame=frame))
    history.write_trusted_history_dataset(dataset_id='raw-and-qfq',receipts=refs,
        cohort_symbols=['000001'],calendar=[day],plan_sha256='a'*64)
    dataset = history.load_trusted_history_dataset('raw-and-qfq')
    assert dataset.bars.iloc[0].close == 10
    prices, evidence = verified_raw_prices(dataset,{day},as_of=datetime.now(timezone.utc)+timedelta(seconds=1))
    assert prices[('000001',day)]['close'] == 20
    assert len(evidence) == 1 and evidence[0]['receipt_sha256'] == refs[1]['sha256']
    path = __import__('pathlib').Path(refs[1]['path'])
    path.write_text('{}')
    with pytest.raises(ValueError):
        history.load_trusted_history_dataset('raw-and-qfq')
