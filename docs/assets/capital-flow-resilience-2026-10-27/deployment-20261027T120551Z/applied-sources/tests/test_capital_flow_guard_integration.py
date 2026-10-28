"""Preserve the newer global supplier cooldown when installing flow recovery."""
from datetime import date

import pytest
from sqlalchemy import func, select

from app.models.akshare_api_config import AkshareApiConfig
from app.models.capital_flow import CapitalFlow
from app.models.symbol import Symbol
from app.services import akshare_guard, akshare_registry
from app.services.capital_flow_data import FundFlowProviderError, sync_symbol_capital_flow_range
from tests.test_capital_flow_retry_contract import body, external  # noqa: F401

pytestmark = pytest.mark.whitebox


def test_supplier_cooldown_rejects_history_before_io_and_preserves_recovery(external, db_session):
    _, calls, install = external
    api_key = "stock_individual_fund_flow"
    symbol = Symbol(symbol="600000", name="隔离联合回归", market="sh", asset_type="stock")
    db_session.add(symbol)
    db_session.add(AkshareApiConfig(api_key=api_key, enabled=True, anti_risk_strategy="standard",
                                  delay_min_ms=0, delay_max_ms=0))
    db_session.commit()
    akshare_registry.load_config_cache(db_session)
    install([(200, {}, body())])
    akshare_guard.reset_all()
    try:
        for _ in range(5):
            akshare_guard.record_failure(api_key, severe=True, error="isolated supplier cooldown")
        assert akshare_guard.is_open(api_key)
        with pytest.raises(FundFlowProviderError, match="SupplierCoolingDown") as failure:
            sync_symbol_capital_flow_range(db_session, symbol, start_date=date(2026, 10, 26),
                                           end_date=date(2026, 10, 26), recover_connection=True)
        assert isinstance(failure.value.__cause__, akshare_guard.SupplierCoolingDown)
        assert calls == []
        assert db_session.scalar(select(func.count()).select_from(CapitalFlow)) == 0

        # A cleared global guard permits the real adapter path and exactly one
        # external response. No nested retry or fabricated database row is used.
        akshare_guard.reset_all()
        assert sync_symbol_capital_flow_range(db_session, symbol, start_date=date(2026, 10, 26),
                                             end_date=date(2026, 10, 26), recover_connection=True) == 1
        db_session.commit()
        assert len(calls) == 1
        saved = db_session.scalar(select(CapitalFlow).where(CapitalFlow.symbol_id == symbol.id))
        assert saved.main_net_inflow == 100
    finally:
        akshare_guard.reset_all()
