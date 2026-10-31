"""M06.3 R1 gate decisions at the public candidate seam."""
from copy import deepcopy

import pytest

from app.services.governance_price_gates import evaluate_price_gate, evaluate_suspension_bar
from tests.test_governance_writer import system, approval
from tests.test_governance_schema import candidate
from tests.test_governance_master_data import annual_page

pytestmark = pytest.mark.whitebox


def examples():
    dates = [f'2026-03-{i:02d}' for i in range(1, 32)] + [f'2026-04-{i:02d}' for i in range(1, 31)]
    return {
        'P01': ('stock', {'raw_open':'10','raw_high':'11','raw_low':'9','raw_close':'10.5'}, {'raw_close':'12'}, {'raw_high':None}),
        'P02': ('stock', {'volume_shares':'100','amount_cny':'1000','raw_low':'9.9','raw_high':'10.1'}, {'amount_cny':'1500'}, {'raw_low':None}),
        'P03': ('stock', {'raw_close':'10','shares':'1000','market_cap_cny':'10000','share_kind':'float','currency':'CNY','price_date':'2026-06-01','share_date':'2026-06-01','market_date':'2026-06-01'}, {'market_cap_cny':'12000'}, {'shares':None}),
        'P04': ('stock', {'volume_shares':'100','float_shares':'1000','turnover_rate_pct':'10','share_definition':'cn-issued-tradable-freefloat-v1','reported_share_definition':'cn-issued-tradable-freefloat-v1'}, {'turnover_rate_pct':'13'}, {'float_shares':None}),
        'P05': ('stock', {'raw_low':'9','raw_high':'11','official_lower':'9','official_upper':'11','tick':'0.01','official_rule_version':'SSE:2026-06-01'}, {'raw_high':'11.02'}, {'official_rule_version':None}),
        'P06': ('stock', {'previous_raw_close':'10','cash_dividend':'1','stock_ratio':'0','rights_ratio':'0','rights_price':None,'official_ex_reference':'9','tick':'0.01'}, {'official_ex_reference':'8.98'}, {'official_ex_reference':None}),
        'P07': ('stock', {'rows':[{'trade_date':'2026-06-01','adj_close':'10','adj_factor_version':'f1','base_date':'2026-06-02','definition_id':'d1','available_at':'2026-06-02T08:00:00Z'},
                                   {'trade_date':'2026-06-02','adj_close':'11','adj_factor_version':'f1','base_date':'2026-06-02','definition_id':'d1','available_at':'2026-06-02T08:00:00Z'}],
                         'expected_dates':['2026-06-01','2026-06-02'],'as_of':'2026-06-02T10:00:00Z','adj_factor_version':'f1','base_date':'2026-06-02','definition_id':'d1'},
                 {'rows':[{'trade_date':'2026-06-01','adj_close':'10','adj_factor_version':'f2','base_date':'2026-06-02','definition_id':'d1','available_at':'2026-06-02T08:00:00Z'},
                          {'trade_date':'2026-06-02','adj_close':'11','adj_factor_version':'f1','base_date':'2026-06-02','definition_id':'d1','available_at':'2026-06-02T08:00:00Z'}]}, {'expected_dates':['2026-06-01','2026-06-02','2026-06-03']}),
        'P08': ('etf', {'raw_price':'1.05','iopv':'1','currency':'CNY','iopv_currency':'CNY','valuation_market':'CN','iopv_market':'CN','price_observation_at':'2026-06-02T07:00:00Z','iopv_observation_at':'2026-06-02T07:00:30Z'}, {'raw_price':'0'}, {'iopv_observation_at':'2026-06-02T07:01:01Z'}),
        'P09': ('etf', {'raw_close':'1.05','nav_official':'1','trade_date':'2026-06-02','valuation_date':'2026-06-02','nav_available_at':'2026-06-02T10:00:00Z','as_of':'2026-06-03T02:00:00Z','currency':'CNY','nav_currency':'CNY'}, {'nav_official':'0'}, {'as_of':'2026-06-02T08:00:00Z'}),
        'P10': ('etf', {'nav_official':'1.2','total_shares':'1000','fund_size_cny':'1200','nav_date':'2026-06-02','shares_date':'2026-06-02','size_date':'2026-06-02','nav_share_class':'A','shares_share_class':'A','size_share_class':'A'}, {'fund_size_cny':'1300'}, {'total_shares':None}),
        'P11': ('etf', {'nav_total_returns':['0.01']*30+['0.02']*30,'benchmark_total_returns':['0.01']*60,'nav_return_kind':'dividend_reinvested','benchmark_return_kind':'total_return','nav_point_dates':dates,'nav_dates':dates[1:],'benchmark_dates':dates[1:]}, {'nav_total_returns':['NaN']+['0.01']*59}, {'nav_point_dates':dates[:-1]}),
        'P12': ('index', {'index_open':'3000','index_high':'3100','index_low':'2900','index_close':'3050','volume':'100','amount':None,'volume_unit':'index_lots'}, {'index_close':'3200'}, {'volume_unit':None}),
        'P13': ('stock', {'primary_observation':'2026-06-02','backup_observation':'2026-06-02','primary_definition':'raw-a','backup_definition':'raw-a','primary_raw_close':'10','backup_raw_close':'10.04','primary_volume_shares':'1000','backup_volume_shares':'1005','primary_amount_cny':'10000','backup_amount_cny':None}, {'backup_raw_close':'10.06'}, {'backup_volume_shares':None}),
    }


@pytest.mark.parametrize('rule', [f'P{i:02d}' for i in range(1, 14)])
def test_each_rule_accepts_rejects_and_reports_missing_dependency(rule):
    asset, good, bad, missing = examples()[rule]
    assert evaluate_price_gate(rule, asset_type=asset, inputs=good)['status'] == 'pass'
    wrong = deepcopy(good); wrong.update(bad)
    if rule == 'P11':
        from app.services.governance_fact_contracts import WriterError
        with pytest.raises(WriterError, match='PRICE_GATE_INPUT_INVALID'):
            evaluate_price_gate(rule, asset_type=asset, inputs=wrong)
    else:
        assert evaluate_price_gate(rule, asset_type=asset, inputs=wrong)['status'] == 'fail'
    absent = deepcopy(good); absent.update(missing)
    assert evaluate_price_gate(rule, asset_type=asset, inputs=absent)['status'] == 'insufficient'


@pytest.mark.parametrize('rule', [f'P{i:02d}' for i in range(1, 14)])
def test_nonmatching_asset_does_not_claim_gate_success(rule):
    asset = 'index' if rule != 'P12' else 'stock'
    assert evaluate_price_gate(rule, asset_type=asset, inputs={}) == {'status':'not_applicable','reason':'ASSET_TYPE','value':None}


def test_etf_premium_and_size_values_keep_percentage_and_cny_units():
    cases = examples()
    assert evaluate_price_gate('P08',asset_type='etf',inputs=cases['P08'][1])['value'] == '5.00'
    assert evaluate_price_gate('P09',asset_type='etf',inputs=cases['P09'][1])['value'] == '5.00'
    assert evaluate_price_gate('P10',asset_type='etf',inputs=cases['P10'][1])['value'] == '1200.0'


def test_index_amount_does_not_get_stock_vwap_and_unknown_unit_is_partial():
    index = {**examples()['P12'][1], 'amount':'999999999999', 'amount_unit':'CNY'}
    assert evaluate_price_gate('P12',asset_type='index',inputs=index)['status'] == 'pass'
    index['amount_unit'] = None
    assert evaluate_price_gate('P12',asset_type='index',inputs=index)['status'] == 'insufficient'


def test_cross_source_different_definition_is_incompatible_and_missing_amount_stays_unknown():
    base = examples()['P13'][1]
    assert base['backup_amount_cny'] is None
    assert evaluate_price_gate('P13',asset_type='stock',inputs=base)['status'] == 'pass'
    changed = {**base, 'backup_definition':'qfq'}
    assert evaluate_price_gate('P13',asset_type='stock',inputs=changed)['reason'] == 'INCOMPATIBLE_BASIS'


def test_suspension_requires_official_status_and_is_never_tradable():
    base = dict(is_suspended=True,synthetic_bar=True,official_status_evidence=b'SYNTHETIC official status fixture',
                official_reference=None,previous_raw_close='10',action_on_day=False,
                raw_open='10',raw_high='10',raw_low='10',raw_close='10',volume_shares='0',amount_cny='0',turnover_rate_pct='0')
    assert evaluate_suspension_bar(**base) == {'status':'pass','reason':'NONTRADABLE_REFERENCE','tradable':False}
    assert evaluate_suspension_bar(**{**base,'action_on_day':True})['reason'] == 'SUSPENSION_REFERENCE_MISSING'
    assert evaluate_suspension_bar(**{**base,'action_on_day':True,'official_reference':'9'})['status'] == 'fail'
    assert evaluate_suspension_bar(**{**base,'official_status_evidence':None})['status'] == 'insufficient'
    assert evaluate_suspension_bar(**{**base,'volume_shares':'1'})['status'] == 'fail'
    assert evaluate_suspension_bar(**{**base,'synthetic_bar':False})['status'] == 'fail'


def test_writer_downgrades_false_ohlc_and_vwap_pass_before_activation(system):
    from app.services.governance_fact_contracts import WriterError
    writer, request, _, _ = system
    request['wire_record'].update({'最高':'1.1','最低':'1','收盘':'1.05','成交额':'2000'})
    request['gate_results'].update(P01='pass',P02='pass')
    assert writer.preview(request)['request_quality_status'] == 'partial'
    with pytest.raises(WriterError, match='QUALITY_BLOCKED'):
        writer.write(request, approval(writer,request))
    request['wire_record']['成交额'] = '1050'
    assert writer.preview(request)['request_quality_status'] == 'complete'
    request['wire_record']['最高'] = '1.01'
    assert writer.preview(request)['request_quality_status'] == 'partial'


def test_suspended_candidate_needs_bound_official_status_and_cannot_trade(system):
    from app.services.governance_fact_contracts import WriterError, canonical
    writer, request, _, _ = system
    request['flags'].update(is_suspended=True, synthetic_bar=True)
    request['wire_record'].update({'开盘':'1.05','最高':'1.05','最低':'1.05','收盘':'1.05','成交量':'0','成交额':'0','换手率':'0'})
    with pytest.raises(WriterError, match='SUSPENSION_EVIDENCE_REQUIRED'):
        writer.write(request)
    request['suspension_evidence'] = {'status_bytes':canonical({'instrument_id':request['key']['instrument_id'],
        'trade_date':'2026-06-01','is_suspended':True,'official_reference':'1.05'}).encode(),
        'status_source_uri':'https://www.sse.com.cn/synthetic-status','official_reference':'1.05',
        'previous_raw_close':None,'action_on_day':False}
    assert writer.preview(request)['request_quality_status'] == 'complete'
    result = writer.write(request, approval(writer,request))
    assert result['accepted']
    fact = writer.fact(result['data_version'])
    assert fact['payload']['synthetic_bar'] and fact['payload']['is_suspended']
    assert fact['payload']['volume_shares'] == fact['payload']['amount_cny'] == '0.00000000'
    assert evaluate_suspension_bar(is_suspended=True,synthetic_bar=True,
        official_status_evidence=request['suspension_evidence']['status_bytes'],
        official_reference='1.05',previous_raw_close=None,action_on_day=False,
        **{k:fact['payload'][k] for k in ('raw_open','raw_high','raw_low','raw_close','volume_shares','amount_cny','turnover_rate_pct')})['tradable'] is False
    bad = deepcopy(request); bad['suspension_evidence']['official_reference'] = '1.04'
    with pytest.raises(WriterError, match='SUSPENSION_EVIDENCE_INVALID'):
        writer.preview(bad)
