"""Native TDX byte fixtures and adjustment boundaries; no network or database."""
from datetime import date
from types import SimpleNamespace
import struct

import pytest

from app.services.market_data_sources.sources.base import SourceUnavailable
from app.services.market_data_sources.sources.tdx_source import TDXHistorySource
from app.services.market_data_sources.tdx_parser import parse_day_file

pytestmark = pytest.mark.whitebox


def native_record(day=20240816):
    # Native .day layout, independently fixed to the pytdx reader's wire
    # contract. Do not import the production parser's struct format here.
    return struct.pack("<IIIIIfII", day, 1234, 1356, 1112, 1298, 12345678.0, 1234567, 0)


def test_native_little_endian_record_decodes_all_prices_and_quantity(tmp_path):
    path = tmp_path / "sz000001.day"
    payload = native_record()
    assert len(payload) == 32
    path.write_bytes(payload)
    row = parse_day_file(path).iloc[0]
    assert row["trade_date"] == date(2024, 8, 16)
    assert [row[name] for name in ("open", "high", "low", "close")] == [12.34, 13.56, 11.12, 12.98]
    assert row["amount"] == 12345678.0
    assert row["volume"] == 1234567


@pytest.mark.parametrize("day", [0, 20240230, 20241301, 2024081])
def test_invalid_record_date_rejects_whole_file_even_outside_requested_range(tmp_path, day):
    path = tmp_path / "sz000001.day"
    path.write_bytes(native_record() + native_record(day))
    with pytest.raises(ValueError, match="date"):
        parse_day_file(path, start=date(2024, 8, 16), end=date(2024, 8, 16))


@pytest.mark.parametrize("tail_bytes", [1, 16, 31])
def test_truncated_tail_rejects_whole_file(tmp_path, tail_bytes):
    path = tmp_path / "sz000001.day"
    path.write_bytes(native_record() + b"\0" * tail_bytes)
    with pytest.raises(ValueError, match="record|truncat"):
        parse_day_file(path)


@pytest.mark.parametrize("adjust", ["qfq", "hfq", "unrecognized"])
def test_tdx_cannot_supply_adjusted_prices(tmp_path, adjust):
    path = tmp_path / "vipdoc" / "sz" / "lday" / "sz000001.day"
    path.parent.mkdir(parents=True)
    path.write_bytes(native_record())
    source = TDXHistorySource()
    source.tdx_path = str(tmp_path)
    symbol = SimpleNamespace(symbol="000001", market="SZ", asset_type="stock")
    with pytest.raises(SourceUnavailable, match="TDX does not support adjustment"):
        source.fetch(symbol, date(2024, 8, 16), date(2024, 8, 16), adjust)


def test_raw_tdx_file_does_not_claim_verified_cn_share_units(tmp_path):
    path = tmp_path / "vipdoc" / "sz" / "lday" / "sz000001.day"
    path.parent.mkdir(parents=True)
    path.write_bytes(native_record())
    source = TDXHistorySource()
    source.tdx_path = str(tmp_path)
    symbol = SimpleNamespace(symbol="000001", market="SZ", asset_type="stock")
    result = source.fetch(symbol, date(2024, 8, 16), date(2024, 8, 16), "")
    assert result["turnover_rate"].isna().all()
    assert result.iloc[0]["volume"] == 1234567
    assert "history_contract" not in result.attrs
