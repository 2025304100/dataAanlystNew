"""Frozen R1 payload profiles. Assessment is separate from source admission."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from collections.abc import Mapping
import hashlib
import json
import re
from app.services.data_contract_errors import DataContractError


@dataclass(frozen=True)
class QualityProfile:
    profile_id: str
    required_fields: tuple[str, ...]
    optional_fields: tuple[str, ...] = ()
    required_gates: tuple[str, ...] = ()

    @property
    def version(self) -> str:
        payload = ["GOV-1.4.4", self.profile_id, self.required_fields, self.optional_fields, self.required_gates]
        return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class ProfileAssessment:
    field_status: str
    quality_status: str
    missing_fields: tuple[str, ...]
    invalid_fields: tuple[str, ...]
    missing_gates: tuple[str, ...]
    profile_version: str


FINANCIAL = ("net_profit_parent", "revenue_operating", "net_profit_yoy_pct", "revenue_yoy_pct", "roe_weighted_period_pct")
FLOW = ("main", "super_large", "large", "medium", "small", "flow_definition")
VALUATION = ("pe_ttm_x", "pb_mrq_x", "definition_id")
GLOBAL_GATES = ("identity", "unit_contract", "definition", "time")

PROFILES = (
    QualityProfile("daily_bars/core", ("raw_open", "raw_high", "raw_low", "raw_close", "volume_shares", "is_suspended", "synthetic_bar"),
                   ("amount_cny", "turnover_rate_pct"), ("P01",)),
    QualityProfile("valuation/core", VALUATION, ("total_market_cap_cny", "float_market_cap_cny", "dividend_yield_pct")),
    QualityProfile("valuation/pe", ("pe_ttm_x", "definition_id"), ("pb_mrq_x", "total_market_cap_cny", "float_market_cap_cny", "dividend_yield_pct")),
    QualityProfile("financial/core", FINANCIAL),
    QualityProfile("financial/net_profit", ("net_profit_parent",), FINANCIAL[1:]),
    QualityProfile("capital_flow/core", FLOW),
    QualityProfile("capital_flow/main", ("main", "flow_definition"), FLOW[1:-1]),
    QualityProfile("lhb/core", ("institution_buy", "institution_sell", "institution_net", "reason_scope", "dedup_rule_id")),
    QualityProfile("hot_rank/core", ("rank", "ranking_method", "observation_at"), ("total", "pct")),
    QualityProfile("etf/nav", ("nav_official", "valuation_date", "currency"), ("premium_official_pct",)),
    QualityProfile("etf/shares", ("total_shares", "share_class", "valuation_date")),
    QualityProfile("etf/size", ("fund_size_cny", "nav_data_version", "shares_data_version"), (), ("P10",)),
    QualityProfile("etf/tracking", ("tracking_error_pct", "window", "benchmark_id", "nav_return_version", "benchmark_return_version"), (), ("P11",)),
    QualityProfile("etf/iopv", ("iopv", "valuation_date", "observation_at", "currency")),
    QualityProfile("index/core", ("index_open", "index_high", "index_low", "index_close", "index_id"), ("volume", "amount"), ("P12",)),
    QualityProfile("macro/core", ("indicator_key", "period", "frequency", "value", "unit")),
    QualityProfile("announcement/core", ("authority", "announcement_id", "content_hash"), ("published_at", "publication_date")),
    QualityProfile("share_capital/core", ("share_class", "share_definition", "total_shares", "float_shares",
                   "announcement_id", "announcement_data_version", "event_type", "change_reason"), ("free_float_shares",)),
    QualityProfile("corporate_action/core", ("event_type", "cash_dividend", "stock_ratio", "rights_ratio",
                   "announcement_id", "announcement_data_version"), ("rights_price", "record_date", "split_ratio")),
)
_PROFILES = {p.profile_id: p for p in PROFILES}
_TEXT_FIELDS = {"flow_definition", "reason_scope", "dedup_rule_id", "ranking_method", "currency", "share_class", "share_definition", "event_type", "change_reason",
                "benchmark_id", "index_id", "indicator_key", "period", "frequency", "unit", "authority", "announcement_id"}
_HASH_FIELDS = {"definition_id", "content_hash", "nav_data_version", "shares_data_version", "nav_return_version", "benchmark_return_version", "announcement_data_version"}


def get_profile(profile_id: str) -> QualityProfile:
    try:
        return _PROFILES[profile_id]
    except KeyError as error:
        raise DataContractError(f"PROFILE_UNREGISTERED: {profile_id}") from error


def _valid(field: str, value) -> bool:
    if field in ("is_suspended", "synthetic_bar"):
        return type(value) is bool
    if field == "currency":
        return value == "CNY"
    if field in _TEXT_FIELDS:
        return isinstance(value, str) and bool(value.strip())
    if field in _HASH_FIELDS:
        return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{64}", value))
    if field in ("valuation_date", "publication_date", "record_date"):
        try:
            date.fromisoformat(value.isoformat() if type(value) is date else value)
            return True
        except (ValueError, TypeError):
            return False
    if field in ("published_at", "observation_at"):
        try:
            parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.tzinfo is not None and parsed.utcoffset() is not None
        except (ValueError, TypeError, AttributeError):
            return False
    if field in ("rank", "total", "window"):
        return type(value) is int and value > 0 and (field != "window" or value == 60)
    if isinstance(value, bool):
        return False
    try:
        number = Decimal(str(value))
        if not number.is_finite():
            return False
        if field.startswith(("raw_", "index_")) or field in ("nav_official", "iopv"):
            return number > 0
        if field in ("volume_shares", "amount_cny", "turnover_rate_pct", "total_shares", "float_shares", "free_float_shares", "fund_size_cny", "institution_buy", "institution_sell", "tracking_error_pct"):
            return number >= 0
        return True  # PE/profit/net flows/YoY may legally be zero or negative.
    except (ValueError, InvalidOperation):
        return False


def assess_profile(payload: Mapping, profile_id: str, *, gate_results: Mapping[str, str] | None = None) -> ProfileAssessment:
    profile = get_profile(profile_id)
    if not isinstance(payload, Mapping):
        raise DataContractError("PROFILE_PAYLOAD_NOT_MAPPING")
    allowed = set(profile.required_fields + profile.optional_fields)
    missing = {field for field in profile.required_fields if payload.get(field) is None}
    invalid = set(payload) - allowed
    invalid.update(field for field in allowed if payload.get(field) is not None and not _valid(field, payload[field]))
    # Missing optional values remain unknown. Present invalid values must be
    # quarantined/sanitized by the adapter, never certified as finite payload.
    required_gates = set(GLOBAL_GATES + profile.required_gates)
    if profile_id == "daily_bars/core" and payload.get("amount_cny") is not None:
        required_gates.add("P02")
    if profile_id == "etf/nav" and payload.get("premium_official_pct") is not None:
        required_gates.add("P09")
    if profile_id == "announcement/core" and payload.get("published_at") is None and payload.get("publication_date") is None:
        missing.add("publication_time")
    gates = gate_results or {}
    unmet = tuple(sorted(g for g in required_gates if gates.get(g) != "pass"))
    field_status = "complete" if not missing and not invalid else "partial"
    return ProfileAssessment(field_status, "complete" if field_status == "complete" and not unmet else "partial",
                             tuple(sorted(missing)), tuple(sorted(invalid)), unmet, profile.version)
