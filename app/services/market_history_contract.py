"""Persist only explicitly normalized CN stock units, never infer old units."""
from __future__ import annotations

from collections.abc import Mapping
import re


_ENDPOINTS = {
    "eastmoney": ("stock_zh_a_hist", "akshare:em:cn-stock:v1"),
    "sina": ("stock_zh_a_daily", "akshare:sina:cn-stock:v1"),
    "tencent": ("stock_zh_a_hist_tx", "akshare:tx:cn-stock:v1"),
    "eastmoney_etf": ("fund_etf_hist_em", "akshare:em:cn-etf:v1"),
    "baostock_etf": ("query_history_k_data_plus", "baostock:cn-etf:v1"),
}


TENCENT_QUANTITY_CONTRACT_VERSION = "tencent-cn-stock-wire-quantity-v2"


def cn_stock_code_family(code: str) -> str:
    if not isinstance(code, str) or not re.fullmatch(r"\d{6}", code):
        raise ValueError("unknown_cn_stock_code_family")
    if code.startswith("688"):
        return "sh_star_688"
    if code.startswith("689"):
        return "sh_star_cdr_689"
    if code.startswith(("600", "601", "603", "605")):
        return "sh_main"
    if code.startswith(("000", "001", "002", "003")):
        return "sz_main"
    if code.startswith(("300", "301")):
        return "sz_chinext"
    if code.startswith(("4", "8", "920")):
        return "bj"
    raise ValueError("unknown_cn_stock_code_family")


def resolve_cn_prefixed_symbol(code: str, market: str | None, asset_type: str | None = "stock") -> str:
    """Honor an explicit exchange; infer generic CN only from known families."""
    if not isinstance(code, str) or not re.fullmatch(r"\d{6}", code):
        raise ValueError("unknown_cn_symbol_code")
    exchange = (market or "").lower()
    if exchange in {"sh", "sz", "bj"}:
        return exchange + code
    if exchange not in {"cn", ""}:
        raise ValueError("unsupported_cn_symbol_market")
    if asset_type == "etf":
        if code.startswith("5"):
            return "sh" + code
        if code.startswith(("15", "16")):
            return "sz" + code
        raise ValueError("unknown_cn_etf_code_family")
    family = cn_stock_code_family(code)
    return ("sh" if family.startswith("sh_") else "sz" if family.startswith("sz_") else "bj") + code


def cn_history_instrument(code: str) -> dict[str, str]:
    """Resolve traded CN ETF/stock families before preparing research inputs.

    A six-digit code alone does not make an instrument a stock. Unknown
    families fail instead of defaulting to the Shenzhen exchange.
    """
    if not isinstance(code, str) or not re.fullmatch(r"\d{6}", code):
        raise ValueError("unknown_cn_symbol_code")
    # SSE code allocation guide (2026, revision 3). LOFs, REITs, fund
    # subscriptions and exchange money funds must not become equity ETFs.
    sh_etf_prefixes = ('510', '512', '513', '515', '516', '517', '518', '526',
                      '530', '551', '560', '561', '562', '563', '565', '566',
                      '567', '581', '587', '588', '589')
    is_etf = (code.startswith(sh_etf_prefixes) or code.startswith('159')
              or '511000' <= code <= '511599' or '520500' <= code <= '520999')
    asset_type = "etf" if is_etf else "stock"
    prefixed = resolve_cn_prefixed_symbol(code, None, asset_type)
    return {"asset_type": asset_type, "market": prefixed[:2]}


def tencent_quantity_input_contract(symbol: str) -> dict:
    """Declare verified Tencent wire units by instrument family, never size."""
    if not isinstance(symbol, str) or not re.fullmatch(r"(?:sh|sz)\d{6}", symbol):
        raise ValueError("unverified_tencent_quantity_symbol_family")
    family = cn_stock_code_family(symbol[2:])
    if (family not in {"sh_star_688", "sh_main", "sz_main", "sz_chinext"}
            or not family.startswith(symbol[:2] + "_")):
        raise ValueError("unverified_tencent_quantity_symbol_family")
    native_shares = family == "sh_star_688"
    return {"version": TENCENT_QUANTITY_CONTRACT_VERSION, "provider": "tencent", "symbol": symbol,
        "instrument_family": family, "wire_field": "amount",
        "volume_input_unit": "share" if native_shares else "lot_100_shares",
        "volume_to_shares": 1 if native_shares else 100,
        "evidence_sha256": "bdf2c1f31aa282942156c591d050102730f33993e16425ceade8df0be475409b"}


def tencent_quantity_to_shares_multiplier(frame) -> int:
    contract = frame.attrs.get("tencent_quantity_contract")
    if (not isinstance(contract, Mapping) or type(contract.get("volume_to_shares")) is not int
            or contract != tencent_quantity_input_contract(contract.get("symbol"))):
        raise ValueError("unverified_tencent_history_quantity_contract")
    return contract["volume_to_shares"]


def history_source_tag(frame) -> str:
    """A tag proves an adapter unit contract, not historical/as-of provenance.

    Unmarked legacy/import frames retain their original unknown-unit tag. A
    malformed explicit contract must fail instead of silently claiming v1.
    """
    contract = frame.attrs.get("history_contract")
    if contract is None:
        return "akshare"
    if not isinstance(contract, Mapping):
        raise ValueError("Invalid market history unit contract")
    provider = contract.get("provider")
    definition = _ENDPOINTS.get(provider) if isinstance(provider, str) else None
    if definition is None or any(contract.get(key) != expected for key, expected in {
        "version": "cn-etf-shares-v1" if provider in {'eastmoney_etf', 'baostock_etf'} else "cn-stock-shares-v1",
        "endpoint": definition[0],
        "volume_unit": "share", "amount_unit": "CNY", "turnover_unit": "percentage_point",
    }.items()):
        raise ValueError("Invalid CN stock history unit contract")
    return definition[1]


def is_cn_stock_history(symbol) -> bool:
    return (getattr(symbol, "asset_type", None) == "stock"
            and (getattr(symbol, "market", None) or "").lower() in {"", "cn", "sh", "sz", "bj"})


def is_cn_etf_history(symbol) -> bool:
    return (getattr(symbol, 'asset_type', None) == 'etf'
            and (getattr(symbol, 'market', None) or '').lower() in {'', 'cn', 'sh', 'sz'})


def require_cn_stock_qfq(symbol, adjust: str) -> None:
    """Single-version production CN bars cannot store RAW/HFQ alongside qfq."""
    if is_cn_stock_history(symbol) and adjust != "qfq":
        raise ValueError("cn_stock_production_requires_qfq")
    if is_cn_etf_history(symbol) and adjust != 'qfq':
        raise ValueError('cn_etf_production_requires_qfq')


def _declared_price_modes(frame) -> list:
    modes = []
    for name, fields in (
        ("production_price_mode", ("requested_adjustment", "actual_adjustment")),
        ("history_response", ("requested_adjustment", "actual_adjustment")),
        ("sina_evidence", ("adjust",)),
    ):
        block = frame.attrs.get(name)
        if block is None:
            continue
        if not isinstance(block, Mapping):
            raise ValueError("invalid_production_price_mode")
        modes.extend(block[field] for field in fields if field in block and block[field] is not None)
        if name == "history_response" and block.get("returned_mode") in {"day", "qfqday", "hfqday"}:
            modes.append({"day": "", "qfqday": "qfq", "hfqday": "hfq"}[block["returned_mode"]])
    return modes


def mark_cn_stock_production_mode(frame, symbol, adjust: str):
    require_cn_stock_qfq(symbol, adjust)
    if not (is_cn_stock_history(symbol) or is_cn_etf_history(symbol)):
        return frame
    if any(mode != "qfq" for mode in _declared_price_modes(frame)):
        raise ValueError("cn_stock_production_price_mode_conflict")
    result = frame.copy(deep=False)
    response = result.attrs.get("history_response", {})
    result.attrs["production_price_mode"] = {
        "requested_adjustment": "qfq",
        "actual_adjustment": response.get("actual_adjustment"),
        "evidence": response.get("adjustment_evidence", "request_only"),
        "historical_availability_proven": False,
    }
    return result


def production_history_source_tag(frame, *, cn_stock: bool | None = None) -> str:
    """`:qfq` records production intent, never server/as-of certification.

    Old unmarked/import frames keep their unknown-mode tag; do not infer their
    price basis from their values or from the normalizer's unit contract.
    """
    source = history_source_tag(frame)
    if cn_stock is None:
        cn_stock = (frame.attrs.get("history_contract", {}).get("provider") in _ENDPOINTS
                    or "production_price_mode" in frame.attrs)
    if not cn_stock:
        return source
    modes = _declared_price_modes(frame)
    if any(mode != "qfq" for mode in modes):
        raise ValueError("cn_stock_production_price_mode_conflict")
    return source + ":qfq" if modes else source


def explicit_source_price_mode(source: str | None) -> str | None:
    """Read only explicit suffixes; all historic suffix-free tags are unknown."""
    match = re.search(r":(qfq|hfq|raw)$", source or "")
    return {"raw": "", "qfq": "qfq", "hfq": "hfq"}[match.group(1)] if match else None
