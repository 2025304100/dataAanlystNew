"""Behavior at the public contract/profile interfaces (ST-M04.1)."""
from decimal import Decimal

import pytest

from app.services import data_source_contracts as contracts

pytestmark = pytest.mark.whitebox


def test_exact_contract_key_reuses_tencent_family_evidence_and_rejects_unknown():
    resolved = contracts.resolve_contract(
        "stock_zh_a_hist_tx", provider="tencent", dataset="daily_bars",
        asset_type="stock", instrument_family="sh_star_688",
    )
    assert resolved.fields[0].multiplier == 1
    assert resolved.instrument_family == "sh_star_688"
    with pytest.raises(contracts.DataContractError, match="UNVERIFIED"):
        contracts.resolve_contract(
            "stock_zh_a_hist_tx", provider="tencent", dataset="daily_bars",
            asset_type="stock", instrument_family="sh_star_cdr_689",
        )


def test_raw_wire_record_uses_decimal_units_and_frozen_price_rounding():
    wire = {"开盘": "1.000000005", "最高": "2", "最低": "1", "收盘": "1.5",
            "成交量": "12", "成交额": "12000", "换手率": "2"}
    result = contracts.normalize_record(
        wire, "stock_zh_a_hist", provider="eastmoney", dataset="daily_bars",
        asset_type="stock", instrument_family="sh_main", price_basis="raw",
        price_evidence_hash="a" * 64,
    )
    assert result.payload["raw_open"] == Decimal("1.00000001")
    assert result.payload["volume_shares"] == Decimal("1200.00000000")
    assert result.payload["turnover_rate_pct"] == Decimal("2.0000000000")
    assert wire["成交量"] == "12"


def test_profit_profile_accepts_real_zero_without_claiming_financial_core_complete():
    gates = {name: "pass" for name in ("identity", "unit_contract", "definition", "time")}
    profit = contracts.assess_profile({"net_profit_parent": Decimal("0")}, "financial/net_profit", gate_results=gates)
    core = contracts.assess_profile({"net_profit_parent": Decimal("0")}, "financial/core", gate_results=gates)
    assert profit.quality_status == "complete"
    assert profit.missing_fields == ()
    assert core.quality_status == "partial"
    assert "revenue_operating" in core.missing_fields


def test_catalog_distinguishes_inventory_from_executable_units_and_planned_sources():
    catalog = contracts.contract_catalog()
    items = {item["interface_id"]: item for item in catalog}
    assert len(items) == 71
    assert items["PLAN:Fuyao"]["contract_status"] == "scope_excluded"
    assert items["PLAN:iFinD"]["executable_keys"] == []
    assert items["PLAN:BSE"]["executable_keys"] == []
    assert items["IF:stock_value_em"]["contract_status"] == "unit_definition_unverified"
    roe = next(field for field in items["IF:stock_financial_analysis_indicator_em"]["field_mappings"] if field["wire_field"] == "ROEJQ")
    assert roe["canonical_target"] == "roe_weighted_period_pct"
    assert roe["native_unit_verified"] is False


def test_frame_conversion_is_idempotent_and_rejects_a_family_change():
    import pandas as pd
    native = pd.DataFrame({"volume": [12]})
    normalized = contracts.normalize_units(native, "stock_zh_a_hist_tx", instrument_family="sh_star_688")
    assert normalized.iloc[0].volume == 12
    assert contracts.normalize_units(normalized, "stock_zh_a_hist_tx", instrument_family="sh_star_688").iloc[0].volume == 12
    with pytest.raises(contracts.DataContractError, match="CONFLICT"):
        contracts.normalize_units(normalized, "stock_zh_a_hist_tx", instrument_family="sh_main")
    assert contracts.normalize_units(native, "stock_zh_a_hist_tx", instrument_family="sh_main").iloc[0].volume == 1200


def test_non_finite_optional_payload_cannot_be_certified_complete():
    result = contracts.assess_profile(
        {"net_profit_parent": Decimal("0"), "revenue_operating": float("nan")},
        "financial/net_profit", gate_results={g: "pass" for g in ("identity", "unit_contract", "definition", "time")},
    )
    assert result.quality_status == "partial"
    assert result.invalid_fields == ("revenue_operating",)


def test_official_nav_profile_does_not_require_premium_price_inputs():
    result = contracts.assess_profile(
        {"nav_official": Decimal("2"), "valuation_date": "2026-10-28", "currency": "CNY"},
        "etf/nav", gate_results={g: "pass" for g in ("identity", "unit_contract", "definition", "time")},
    )
    assert result.quality_status == "complete"


def test_coarse_family_and_unknown_family_cannot_select_a_default_multiplier():
    for family in ("all_supported", "unknown", "bj", "sh_star_cdr_689"):
        with pytest.raises(contracts.DataContractError, match="UNVERIFIED"):
            contracts.resolve_contract("stock_zh_a_hist", provider="eastmoney", dataset="daily_bars",
                                       asset_type="stock", instrument_family=family)


def test_unregistered_profile_is_a_contract_error_not_a_supplier_failure():
    with pytest.raises(contracts.DataContractError, match="PROFILE_UNREGISTERED"):
        contracts.assess_profile({"net_profit_parent": 0}, "financial/custom_weak_profile")


def test_definition_identity_cannot_be_replaced_by_a_free_text_label():
    result = contracts.assess_profile(
        {"pe_ttm_x": -10, "definition_id": "TTM"}, "valuation/pe",
        gate_results={g: "pass" for g in ("identity", "unit_contract", "definition", "time")},
    )
    assert result.quality_status == "partial"
    assert result.invalid_fields == ("definition_id",)


def test_foreign_currency_nav_is_not_a_cny_profile_result():
    result = contracts.assess_profile(
        {"nav_official": 2, "valuation_date": "2026-10-28", "currency": "USD"}, "etf/nav",
        gate_results={g: "pass" for g in ("identity", "unit_contract", "definition", "time")},
    )
    assert result.invalid_fields == ("currency",)


def _raw_record(wire, **changes):
    options = dict(provider="eastmoney", dataset="daily_bars", asset_type="stock", instrument_family="sh_main",
                   price_basis="raw", price_evidence_hash="a" * 64)
    return contracts.normalize_record(wire, "stock_zh_a_hist", **{**options, **changes})


@pytest.mark.parametrize("changes", [{"provider": "sina"}, {"dataset": "index"}, {"asset_type": "etf"}, {"version": "unknown-v99"}])
def test_product_identity_mismatch_has_no_provider_default(changes):
    with pytest.raises(contracts.DataContractError, match="UNVERIFIED"):
        _raw_record({"成交量": 1}, **changes)


@pytest.mark.parametrize("family,expected", [("sh_star_688", "1000"), ("sh_main", "100000"), ("sz_main", "100000"), ("sz_chinext", "100000")])
def test_tencent_wire_alias_is_quantity_and_never_fabricates_cash(family, expected):
    row = contracts.normalize_record({"amount": "1000"}, "stock_zh_a_hist_tx", provider="tencent", dataset="daily_bars",
                                     asset_type="stock", instrument_family=family, price_basis="raw", price_evidence_hash="a" * 64)
    assert row.payload["volume_shares"] == Decimal(expected)
    assert row.payload["amount_cny"] is None
    assert row.payload["turnover_rate_pct"] is None


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", True, "not_a_number", "1e40"])
def test_invalid_numeric_wire_values_are_not_silently_zero_filled(value):
    with pytest.raises(contracts.DataContractError):
        _raw_record({"成交量": value})


@pytest.mark.parametrize("mode", ["qfq", "adj", "hfq", None])
def test_adjusted_or_unknown_price_mode_cannot_enter_raw_fields(mode):
    with pytest.raises(contracts.DataContractError, match="RAW_PRICE"):
        _raw_record({"开盘": 10}, price_basis=mode)


def test_raw_requires_evidence_reference_and_rejects_wire_schema_drift():
    with pytest.raises(contracts.DataContractError, match="RAW_PRICE"):
        _raw_record({"开盘": 10}, price_evidence_hash="")
    with pytest.raises(contracts.DataContractError, match="WIRE_FIELD"):
        _raw_record({"unexpected_native_unit": 100})


def test_canonical_record_is_detached_idempotent_and_not_mutable():
    row = _raw_record({"成交量": "12"})
    assert _raw_record(row) is row
    with pytest.raises(TypeError):
        row.payload["volume_shares"] = Decimal("1")
    with pytest.raises(contracts.DataContractError, match="CONFLICT"):
        _raw_record(row, instrument_family="sh_star_688")


def test_missing_and_zero_remain_distinct_and_negative_zero_is_canonical():
    row = _raw_record({"成交量": 0, "开盘": "-0.000000001"})
    assert row.payload["volume_shares"] == 0
    assert row.payload["amount_cny"] is None
    assert row.canonical_payload["raw_open"] == "0.00000000"
    assert row.canonical_payload["amount_cny"] is None
    assert row.evidence_status == "adapter_declared"


def test_sina_ratio_conversion_uses_decimal_percentage_points():
    row = contracts.normalize_record({"volume": "12300", "turnover": "0.02"}, "stock_zh_a_daily",
        provider="sina", dataset="daily_bars", asset_type="stock", instrument_family="sz_main",
        price_basis="raw", price_evidence_hash="a" * 64)
    assert row.payload["turnover_rate_pct"] == Decimal("2.0000000000")
    assert row.payload["volume_shares"] == Decimal("12300.00000000")


def test_catalog_is_detached_and_new_unit_claims_do_not_create_converters():
    first = contracts.contract_catalog()
    first.clear()
    second = contracts.contract_catalog()
    assert len(second) == 71
    assert sum(len(i["executable_keys"]) for i in second) == 14
    assert all(i["production_admission"] == "not_certified" for i in second)
    with pytest.raises(contracts.DataContractError, match="UNVERIFIED"):
        contracts.normalize_record({"PE_TTM": 10}, "stock_value_em", provider="eastmoney", dataset="valuation",
            asset_type="stock", instrument_family="sh_main", price_basis="none", price_evidence_hash="a" * 64)


def test_native_definitions_and_units_are_still_traceable_to_frozen_evidence():
    from pathlib import Path
    import json
    root = Path(__file__).resolve().parents[1]
    frozen = json.loads((root / "docs/assets/data-source-governance-progress/m02/interfaces.json").read_text(encoding="utf-8"))
    catalog = contracts.contract_catalog()
    assert {x["interface_id"] for x in catalog} == {x["interface_id"] for x in frozen["items"]}
    added = {'PLAN:NBS_direct', 'PLAN:PBOC_direct', 'PLAN:ChinaBond_direct', 'PLAN:Chinamoney_direct',
             'PLAN:SSE_direct', 'PLAN:SZSE_direct', 'PLAN:CNINFO_web'}
    assert sum(len(x["field_mappings"]) for x in catalog if x['endpoint'] not in added) == 120
    assert sum(len(x["field_mappings"]) for x in catalog if x['endpoint'] in added) == 7
    for item in catalog:
        for field in item["field_mappings"]:
            assert len(field["evidence"]["sha256"]) == 64
            assert field["native_unit_verified"] is False
            if item['endpoint'] in added:
                import hashlib
                ref = field['evidence']
                assert ref['path'] == 'app/services/governance_releases.py'
                assert hashlib.sha256((root / ref['path']).read_bytes()).hexdigest() == ref['sha256']
                assert item['production_admission'] == 'not_certified'


def test_field_completeness_does_not_imply_quality_without_required_gates():
    result = contracts.assess_profile({"net_profit_parent": 0}, "financial/net_profit")
    assert result.field_status == "complete"
    assert result.quality_status == "partial"
    assert "unit_contract" in result.missing_gates


def test_bar_core_with_missing_amount_does_not_invent_vwap_or_cash():
    payload = dict(raw_open=10, raw_high=11, raw_low=9, raw_close=10, volume_shares=100,
                   is_suspended=False, synthetic_bar=False, amount_cny=None)
    result = contracts.assess_profile(payload, "daily_bars/core", gate_results={g: "pass" for g in ("identity", "unit_contract", "definition", "time", "P01")})
    assert result.quality_status == "complete"
    assert payload["amount_cny"] is None
    assert "P02" not in result.missing_gates


def test_index_points_do_not_require_stock_vwap_or_cross_source_amount():
    result = contracts.assess_profile(dict(index_open=3000, index_high=3100, index_low=2900, index_close=3050, index_id="CSI:000300"),
        "index/core", gate_results={g: "pass" for g in ("identity", "unit_contract", "definition", "time", "P12")})
    assert result.quality_status == "complete"


def test_iopv_and_legacy_weighted_roe_names_cannot_satisfy_other_definitions():
    nav = contracts.assess_profile({"iopv": 2, "valuation_date": "2026-10-28", "currency": "CNY"}, "etf/nav")
    financial = contracts.assess_profile({"roe_ttm": 10}, "financial/core")
    assert "nav_official" in nav.missing_fields
    assert "roe_weighted_period_pct" in financial.missing_fields
    assert financial.quality_status == nav.quality_status == "partial"


def test_contract_metadata_returns_exact_families_and_detached_rules():
    metadata = contracts.endpoint_contracts("stock_zh_a_hist")
    assert {x["instrument_family"] for x in metadata} == {"sh_star_688", "sh_main", "sz_main", "sz_chinext"}
    metadata[0]["fields"][0]["multiplier"] = 999
    assert contracts.endpoint_contracts("stock_zh_a_hist")[0]["fields"][0]["multiplier"] == 100
