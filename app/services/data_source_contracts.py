"""Executable unit contracts shared by adapters and interface management.

Only contracts backed by the existing adapters are executable. Missing entries
are deliberately not inferred from provider names or observed magnitudes.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from types import MappingProxyType
import re
from pathlib import Path
import json
from app.services.data_contract_profiles import assess_profile, get_profile
from app.services.data_contract_errors import DataContractError


@dataclass(frozen=True)
class FieldContract:
    field: str
    input_unit: str
    output_unit: str
    multiplier: float
    wire_field: str | None = None
    canonical_field: str | None = None
    offset: str = "0"
    precision: int = 28
    scale: int = 8


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
    # Immutable M02/M04 input snapshot of the existing adapter declaration.
    # This is not a supplier response, historical-validity or PIT certificate.
    evidence_hash: str = "99cad94f7d40d3b4c9eaf48324c2a1cd54d0acfc4f1b563df35bd0131480a002"
    evidence_level: str = "adapter_declaration"
    definition_id: str | None = None
    valid_from: str | None = None
    valid_to: str | None = None

    def describe(self) -> dict:
        return asdict(self)


_CONTRACTS = (
    SourceContract("stock_zh_a_hist", "eastmoney", "daily_bars", "stock", "cn-stock-shares-v1", (
        FieldContract("volume", "lot_100_shares", "share", 100, "成交量", "volume_shares"),
        FieldContract("amount", "CNY", "CNY", 1, "成交额", "amount_cny"),
        FieldContract("turnover_rate", "percentage_point", "percentage_point", 1, "换手率", "turnover_rate_pct", precision=24, scale=10),
    )),
    SourceContract("stock_zh_a_daily", "sina", "daily_bars", "stock", "cn-stock-shares-v1", (
        FieldContract("volume", "share", "share", 1, "volume", "volume_shares"),
        FieldContract("amount", "CNY", "CNY", 1, "amount", "amount_cny"),
        FieldContract("turnover_rate", "ratio", "percentage_point", 100, "turnover", "turnover_rate_pct", precision=24, scale=10),
    )),
    SourceContract("fund_etf_hist_em", "eastmoney", "daily_bars", "etf", "cn-etf-shares-v1", (
        FieldContract("volume", "lot_100_units", "share", 100, "成交量", "volume_shares"),
        FieldContract("amount", "CNY", "CNY", 1, "成交额", "amount_cny"),
        FieldContract("turnover_rate", "percentage_point", "percentage_point", 1, "换手率", "turnover_rate_pct", precision=24, scale=10),
    )),
)
_FAMILIES = {"stock": ("sh_star_688", "sh_main", "sz_main", "sz_chinext"), "etf": ("sh_etf", "sz_etf")}


def endpoint_contracts(endpoint: str) -> list[dict]:
    """Return detached metadata; callers cannot mutate the executable rules."""
    contracts = [replace(c, instrument_family=family).describe() for c in _CONTRACTS if c.endpoint == endpoint
                 for family in _FAMILIES[c.asset_type]]
    if endpoint == "stock_zh_a_hist_tx":
        # Reuse the verified family resolver rather than duplicating its rules.
        from app.services.market_history_contract import tencent_quantity_input_contract
        for symbol in ("sh688001", "sh600000", "sz000001", "sz300001"):
            wire = tencent_quantity_input_contract(symbol)
            contracts.append(SourceContract(endpoint, "tencent", "daily_bars", "stock", wire["version"], (
                FieldContract("volume", wire["volume_input_unit"], "share", wire["volume_to_shares"], "amount", "volume_shares"),
            ), ("amount", "turnover_rate"), wire["instrument_family"], evidence_hash=wire["evidence_sha256"]).describe())
    return contracts


def resolve_contract(endpoint: str, *, provider: str, dataset: str, asset_type: str,
                     instrument_family: str, version: str | None = None) -> SourceContract:
    """Select an explicit product/family key; a provider name is never a default."""
    matches = []
    for entry in endpoint_contracts(endpoint):
        if (entry["provider"], entry["dataset"], entry["asset_type"]) != (provider, dataset, asset_type):
            continue
        if version is not None and entry["version"] != version:
            continue
        if entry["instrument_family"] != instrument_family:
            continue
        fields = tuple(FieldContract(**field) for field in entry["fields"])
        matches.append(replace(SourceContract(**{**entry, "fields": fields}), instrument_family=instrument_family))
    if len(matches) != 1:
        raise DataContractError(f"UNIT_CONTRACT_UNVERIFIED: {endpoint}/{instrument_family}")
    return matches[0]


@dataclass(frozen=True)
class NormalizedRecord:
    """Unit-normalized candidate payload; not production or PIT certification."""

    payload: Mapping[str, Decimal | None]
    contract_key: tuple[str, str, str, str, str, str]
    price_evidence_hash: str
    evidence_status: str = "adapter_declared"

    @property
    def canonical_payload(self) -> Mapping[str, str | None]:
        """Fixed-scale JSON values; str(Decimal) may otherwise emit 0E-8."""
        return MappingProxyType({key: None if value is None else format(value, "f") for key, value in self.payload.items()})


def _decimal_value(value, field: FieldContract) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise DataContractError(f"INVALID_NUMERIC_VALUE: {field.field}")
    try:
        with localcontext() as context:
            context.prec = 80
            native = Decimal(str(value))
            if not native.is_finite():
                raise DataContractError(f"NON_FINITE_VALUE: {field.field}")
            normalized = native * Decimal(str(field.multiplier)) + Decimal(field.offset)
            normalized = normalized.quantize(Decimal(1).scaleb(-field.scale), rounding=ROUND_HALF_UP)
            if abs(normalized) >= Decimal(10) ** (field.precision - field.scale):
                raise DataContractError(f"NUMERIC_OVERFLOW: {field.field}")
            return abs(normalized) if normalized == 0 else normalized
    except (InvalidOperation, ValueError) as error:
        if isinstance(error, DataContractError):
            raise
        raise DataContractError(f"INVALID_NUMERIC_VALUE: {field.field}") from error


def normalize_record(record: Mapping | NormalizedRecord, endpoint: str, *, provider: str,
                     dataset: str, asset_type: str, instrument_family: str,
                     price_basis: str, price_evidence_hash: str,
                     version: str | None = None) -> NormalizedRecord:
    """Convert explicit raw wire fields into candidate R1 DECIMAL units once.

    Adjustment evidence is a required reference, not verified by this converter.
    R1 quality gates, availability and ADMIT-01 remain separate checks.
    """
    contract = resolve_contract(endpoint, provider=provider, dataset=dataset,
                                asset_type=asset_type, instrument_family=instrument_family, version=version)
    if price_basis != "raw" or not isinstance(price_evidence_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", price_evidence_hash):
        raise DataContractError("RAW_PRICE_EVIDENCE_REQUIRED")
    key = (provider, endpoint, dataset, asset_type, instrument_family, contract.version)
    if isinstance(record, NormalizedRecord):
        if record.contract_key != key or record.price_evidence_hash != price_evidence_hash:
            raise DataContractError("UNIT_CONTRACT_CONFLICT")
        return record
    if not isinstance(record, Mapping):
        raise DataContractError("INVALID_WIRE_RECORD")
    chinese = endpoint in {"stock_zh_a_hist", "fund_etf_hist_em"}
    price_fields = tuple(FieldContract(name, "CNY", "CNY", 1,
        wire, "raw_" + name, precision=24) for name, wire in (
        (("open", "开盘"), ("high", "最高"), ("low", "最低"), ("close", "收盘")) if chinese else
        (("open", "open"), ("high", "high"), ("low", "low"), ("close", "close"))))
    fields = contract.fields + price_fields
    allowed = {f.wire_field or f.field for f in fields}
    # Known transport-only date/summary fields are retained in raw evidence,
    # never silently inserted into the canonical fact payload.
    ignored = {"日期", "振幅", "涨跌幅", "涨跌额"} if chinese else {"date", "outstanding_share"}
    if set(record) - allowed - ignored:
        raise DataContractError("UNEXPECTED_WIRE_FIELD")
    payload = {f.canonical_field: _decimal_value(record.get(f.wire_field or f.field), f) for f in fields}
    for missing in contract.unavailable_fields:
        payload[{"amount": "amount_cny", "turnover_rate": "turnover_rate_pct"}[missing]] = None
    return NormalizedRecord(MappingProxyType(payload), key, price_evidence_hash)


def contract_catalog() -> list[dict]:
    """Detached declarations for every inventoried product, including blocked ones.

    Only the existing adapter-backed rules below become executable keys. Unit
    claims in the catalog never create converters or production admission.
    This packaged catalog has no dependency on workspace documentation at runtime.
    """
    items = json.loads(Path(__file__).with_name("data_source_contract_catalog.json").read_text(encoding="utf-8"))["items"]
    for item in items:
        endpoint = item["endpoint"]
        for description in endpoint_contracts(endpoint):
            family = description["instrument_family"]
            families = (family,)
            for exact_family in families:
                item["executable_keys"].append({"provider": description["provider"], "endpoint": endpoint,
                    "dataset": description["dataset"], "asset_type": description["asset_type"],
                    "instrument_family": exact_family, "contract_version": description["version"]})
        if item["executable_keys"] and item["in_current_release"]:
            item["contract_status"] = "adapter_declared"
        item["profile_versions"] = {p: get_profile(p).version for p in item["profile_ids"]}
    return items


def normalize_units(frame, endpoint: str, *, instrument_family: str | None = None):
    """Normalize renamed columns once; reject conflicting normalization tags."""
    import pandas as pd

    contract = next((c for c in _CONTRACTS if c.endpoint == endpoint), None)
    if instrument_family is not None:
        descriptions = endpoint_contracts(endpoint)
        if descriptions:
            identity = descriptions[0]
            contract = resolve_contract(endpoint, provider=identity["provider"], dataset=identity["dataset"],
                                        asset_type=identity["asset_type"], instrument_family=instrument_family)
    if contract is None:
        raise DataContractError(f"UNIT_CONTRACT_UNVERIFIED: {endpoint}")
    expected = {"endpoint": endpoint, "version": contract.version}
    if instrument_family is not None:
        expected["instrument_family"] = instrument_family
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
