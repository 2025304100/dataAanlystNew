from datetime import date

import pytest
from sqlalchemy import event

from app.models.portfolio import Portfolio
from app.models.sim_account import CashLedger
from app.services.cash_ledger_diagnosis import diagnose_cash_ledger
from app.api.routes.portfolio_governance import preview_portfolio_reconciliation

pytestmark = pytest.mark.whitebox

def test_full_book_includes_old_trades_and_cash_movements():
    rows = [dict(id=i, entry_type=kind, amount=amount, balance_after=balance)
        for i, kind, amount, balance in [(1,'deposit',100000,100000),
            (2,'buy',-1100,98900),(3,'withdrawal',-500,98400),(4,'deposit',200,98600),
            (5,'sell',2000,100600)]]
    result = diagnose_cash_ledger(rows)
    assert result['status'] == 'CONSISTENT'
    assert result['expected_cash'] == 100600

def test_cancelling_local_gaps_are_not_hidden_by_final_total():
    result = diagnose_cash_ledger([
        dict(id=1, entry_type='deposit',amount=500000,balance_after=500000),
        dict(id=2, entry_type='buy',amount=-100,balance_after=499900.2),
        dict(id=3, entry_type='sell',amount=100,balance_after=500000)])
    assert result['status']=='BROKEN' and result['delta']==0
    assert len(result['differences'])==2

def test_trade_fees_and_missing_cash_postings_are_checked():
    rows=[dict(id=1,entry_type='deposit',amount=100000,balance_after=100000),
        dict(id=2,entry_type='buy',amount=-1000,balance_after=99000,ref_type='sim_order',ref_id=7)]
    result=diagnose_cash_ledger(rows,[dict(order_id=7,side='buy',amount=1000,fee=5)])
    assert result['status']=='BROKEN'
    assert result['execution_differences']==[dict(order_id=7,expected=-1005,actual=-1000)]
    assert result['trade_derived_cash']==98995

def test_unknown_opening_is_not_certified():
    result=diagnose_cash_ledger([dict(id=9,entry_type='buy',amount=-100,balance_after=900)])
    assert result['status']=='PARTIAL' and result['opening_verified'] is False

def test_preview_missing_execution_still_exposes_cash_gap_without_any_dml(db_session):
    p=Portfolio(name='preview',account_type='simulated',asset_scope='stock',total_capital=500000,
        investable_ratio=.9,cash_reserve_ratio=.1)
    db_session.add(p);db_session.flush()
    db_session.add_all([CashLedger(portfolio_id=p.id,entry_type='deposit',amount=500000,balance_after=500000),
        CashLedger(portfolio_id=p.id,entry_type='withdrawal',amount=-100,balance_after=499899.74)])
    db_session.commit()
    statements=[]
    def capture(conn,cursor,statement,parameters,context,executemany): statements.append(statement)
    event.listen(db_session.get_bind(),'before_cursor_execute',capture)
    try:
        first=preview_portfolio_reconciliation(p.id,date(2026,10,16),db_session)
        second=preview_portfolio_reconciliation(p.id,date(2026,10,16),db_session)
        assert first.read_only and first.audit_event_id is None
        assert second.cash_audit['delta']==-.26
        assert {d.kind for d in first.differences}=={'DECISION_RUN_NOT_FOUND','NAV_BROKEN'}
        assert p.last_reconciled_trade_date is None
        assert not any(s.lstrip().upper().startswith(('INSERT','UPDATE','DELETE','ALTER','CREATE')) for s in statements)
    finally: event.remove(db_session.get_bind(),'before_cursor_execute',capture)
