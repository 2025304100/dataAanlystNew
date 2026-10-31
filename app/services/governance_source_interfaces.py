"""Explicit candidate contract-to-policy bindings, separate from HTTP admission.

Derived/local products have no supplier request permission. Alias bindings below
require the M07 product factory exactly, never a provider-name inference.
"""
import json
from pathlib import Path


def source_interface(contract):
    doc = contract.document; adapter = doc['adapter']; endpoint = adapter['endpoint']; dataset = adapter['dataset']
    items = json.loads(Path(__file__).with_name('data_source_contract_catalog.json').read_text(encoding='utf-8'))['items']
    exact = next((i for i in items if i['endpoint'] == endpoint and i['in_current_release'] and dataset in i['dataset'].split('/')), None)
    if exact: return exact['interface_id']
    interface = None
    if dataset == 'macro' and endpoint.startswith('public_release:'):
        from app.services.governance_macro import macro_contract
        try: expected = macro_contract(endpoint.split(':', 1)[1], evidence=contract.evidence)
        except ValueError: return None
        if expected.document != doc: return None
        interface = {'NBS': 'PLAN:NBS_direct', 'PBOC': 'PLAN:PBOC_direct',
                     'ChinaBond': 'PLAN:ChinaBond_direct', 'Chinamoney': 'PLAN:Chinamoney_direct'}.get(adapter['provider'])
    elif dataset in {'announcement', 'share_capital', 'corporate_action'}:
        # Event validator independently requires the official HTTPS authority and
        # raw publication evidence. Custom generic financial/daily profiles do
        # not acquire this binding merely by claiming the same provider.
        interface = {'SSE': 'PLAN:SSE_direct', 'SZSE': 'PLAN:SZSE_direct', 'CNINFO': 'PLAN:CNINFO_web'}.get(adapter['provider'])
    elif dataset == 'etf' and endpoint in {'SSE:etf_shares', 'GOV:etf_size', 'GOV:etf_tracking'}:
        from app.services.governance_etf import etf_contract
        product = {'SSE:etf_shares': 'shares', 'GOV:etf_size': 'size', 'GOV:etf_tracking': 'tracking'}[endpoint]
        if etf_contract(product, evidence=contract.evidence).document != doc: return None
        if product != 'shares': return 'LOCAL:etf_' + product
        interface = 'PLAN:SSE_direct'
    elif dataset == 'valuation' and endpoint == 'BAO:query_history_k_data_plus':
        from app.services.governance_fundamentals import fundamental_contract
        sdk = {k: doc['sdk'][k] for k in ('name', 'version')} if doc['sdk'] else None
        expected = fundamental_contract('baostock_valuation', evidence=contract.evidence, profile=doc['profile'], sdk=sdk, sdk_evidence=contract.sdk_evidence)
        if expected.document != doc: return None
        interface = 'BAO:query_history_k_data_plus'
    return interface if any(i['interface_id'] == interface and i['in_current_release'] for i in items) else None
