"""Explicit acquisition-mode receipts; a unit contract alone cannot prove raw."""
import json
from app.services.governance_fact_contracts import WriterError, canonical, digest


def verify_price_evidence(evidence, raw_response, source_uri, contract_version):
    if type(evidence) is not bytes or not evidence:
        raise WriterError('RAW_PRICE_EVIDENCE_REQUIRED')
    try:
        document=json.loads(evidence)
        if type(document) is not dict or set(document)!={'request_basis','response_basis','raw_hash','contract_version'}:
            raise ValueError()
        if document['request_basis']!='raw' or document['response_basis']!='raw':
            raise WriterError('RAW_PRICE_BASIS_CONFLICT')
        if document['raw_hash']!=digest(raw_response):
            raise WriterError('RAW_PRICE_RESPONSE_MISMATCH')
        if document['contract_version']!=contract_version:
            raise WriterError('RAW_PRICE_CONTRACT_MISMATCH')
        if source_uri.endswith((':qfq',':hfq')):
            raise WriterError('RAW_PRICE_BASIS_CONFLICT')
        if canonical(document).encode('utf-8')!=evidence:
            raise ValueError()
        return document
    except WriterError: raise
    except (ValueError,TypeError,UnicodeError):
        raise WriterError('RAW_PRICE_EVIDENCE_INVALID') from None
