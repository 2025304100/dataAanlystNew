"""Executable unit contracts shared by adapters and interface management.

Only contracts backed by the existing adapters are executable. Missing entries
are deliberately not inferred from provider names or observed magnitudes.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass


class DataContractError(ValueError):
    """Invalid provider data: must not be hidden by a supplier fallback."""


@dataclass(frozen=True)
class FieldContract:
    field: str
    input_unit: str
    output_unit: str
    multiplier: float


@dataclass(frozen=True)
class SourceContract:
    endpoint: str
    provider: str
    dataset: str
    asset_type: str
    version: str
    fields: tuple[FieldContract, ...]
    unavailable_fields: tuple[str, ...] = ()
    instrument_family: str = "all_supported"

    def describe(self) -> dict:
        return asdict(self)


_CONTRACTS = (
    SourceContract("stock_zh_a_hist", "eastmoney", "daily_bars", "stock", "cn-stock-shares-v1", (
        FieldContract("volume", "lot_100_shares", "share", 100),
        FieldContract("amount", "CNY", "CNY", 1),
        FieldContract("turnover_rate", "percentage_point", "percentage_point", 1),
    )),
    SourceContract("stock_zh_a_daily", "sina", "daily_bars", "stock", "cn-stock-shares-v1", (
        FieldContract("volume", "share", "share", 1),
        FieldContract("amount", "CNY", "CNY", 1),
        FieldContract("turnover_rate", "ratio", "percentage_point", 100),
    )),
    SourceContract("fund_etf_hist_em", "eastmoney", "daily_bars", "etf", "cn-etf-shares-v1", (
        FieldContract("volume", "lot_100_units", "share", 100),
        FieldContract("amount", "CNY", "CNY", 1),
        FieldContract("turnover_rate", "percentage_point", "percentage_point", 1),
    )),
)


def endpoint_contracts(endpoint: str) -> list[dict]:
    """Return detached metadata; callers cannot mutate the executable rules."""
    contracts = [c.describe() for c in _CONTRACTS if c.endpoint == endpoint]
    if endpoint == "stock_zh_a_hist_tx":
        # Reuse the verified family resolver rather than duplicating its rules.
        from app.services.market_history_contract import tencent_quantity_input_contract
        for symbol in ("sh688001", "sh600000", "sz000001", "sz300001"):
            wire = tencent_quantity_input_contract(symbol)
            contracts.append(SourceContract(endpoint, "tencent", "daily_bars", "stock", wire["version"], (
                FieldContract("volume", wire["volume_input_unit"], "share", wire["volume_to_shares"]),
            ), ("amount", "turnover_rate"), wire["instrument_family"]).describe())
    return contracts


def normalize_units(frame, endpoint: str):
    """Normalize renamed columns once; reject conflicting normalization tags."""
    import pandas as pd

    contract = next((c for c in _CONTRACTS if c.endpoint == endpoint), None)
    if contract is None:
        raise DataContractError(f"UNIT_CONTRACT_UNVERIFIED: {endpoint}")
    expected = {"endpoint": endpoint, "version": contract.version}
    previous = frame.attrs.get("unit_normalization")
    if previous is not None and previous != expected:
        raise DataContractError("UNIT_CONTRACT_CONFLICT")
    normalized = frame.copy()
    if previous is None:
        for field in contract.fields:
            if field.field in normalized:
                normalized[field.field] = pd.to_numeric(normalized[field.field], errors="coerce") * field.multiplier
    normalized.attrs["unit_normalization"] = expected
    return normalized
