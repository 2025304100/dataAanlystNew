"""Sina evidence boundaries; all payloads are local and no request is made."""
from datetime import date
import json

import pandas as pd
import pytest

from app.services.market_data_sources.sources import sina_history_evidence as evidence

pytestmark = pytest.mark.whitebox


SYMBOL = "sz000425"


def payloads(shares=None, factors=None):
    history = b'var KLC_K2_sz000425="K2/ABC"; /* response comment */'
    shares = [{"date": "2024-01-01", "amount": 10}] if shares is None else shares
    factors = [{"d": "2024-01-01", "f": "2"}] if factors is None else factors
    return (history, ("/* comment */ var KKE_ShareAmount_sz000425=(" + json.dumps(shares) + ");").encode(),
            ("var sz000425qfq=" + json.dumps({"total": len(factors), "data": factors}) + ";").encode())


def rows():
    return [{"date": "2024-01-02T00:00:00.000Z", "open": 10, "high": 12, "low": 8,
             "close": 11, "volume": 2000, "amount": 21000},
            {"date": "2024-01-03T00:00:00.000Z", "open": 10, "high": 12, "low": 8,
             "close": 11, "volume": 2000, "amount": 21000}]


def parse(monkeypatch, history_rows=None, bundle=None, start=date(2024, 1, 1), end=date(2024, 1, 31), **kwargs):
    monkeypatch.setattr(evidence, "_decode_compressed_history", lambda encoded: rows() if history_rows is None else history_rows)
    return evidence.parse_sina_history_bundle(SYMBOL, *(bundle or payloads()), start, end, **kwargs)


def test_backwards_asof_units_and_identical_real_days_preserved(monkeypatch):
    bundle = payloads(shares=[{"date": "2024-01-01", "amount": 10}, {"date": "2024-01-03", "amount": 20}],
                      factors=[{"d": "2024-01-01", "f": "2"}, {"d": "2024-01-03", "f": "1"}])
    frame = parse(monkeypatch, bundle=bundle)
    assert len(frame) == 2  # identical raw OHLCV on different days are genuine rows
    assert list(frame["trade_date"].dt.strftime("%Y-%m-%d")) == ["2024-01-02", "2024-01-03"]
    assert frame["close"].tolist() == [5.5, 11]
    assert frame["volume"].tolist() == [2000, 2000]
    assert frame["amount"].tolist() == [21000, 21000]
    assert frame["turnover_rate"].tolist() == [2.0, 1.0]
    assert frame.attrs["sina_evidence"]["turnover_method"] == "raw_volume / effective_outstanding_share * 100"


def test_no_synthetic_days_from_share_or_factor_calendar(monkeypatch):
    bundle = payloads(shares=[{"date": "2024-01-01", "amount": 10}, {"date": "2024-01-04", "amount": 99}],
                      factors=[{"d": "2024-01-01", "f": "2"}, {"d": "2024-01-05", "f": "3"}])
    assert len(parse(monkeypatch, bundle=bundle)) == 2


def test_future_share_capital_is_not_backfilled(monkeypatch):
    frame = parse(monkeypatch, bundle=payloads(shares=[{"date": "2024-01-03", "amount": 20}]))
    assert pd.isna(frame.iloc[0]["turnover_rate"])
    assert frame.iloc[1]["turnover_rate"] == 1


def test_missing_effective_qfq_factor_rejects(monkeypatch):
    with pytest.raises(ValueError, match="qfq factor"):
        parse(monkeypatch, bundle=payloads(factors=[{"d": "2024-01-03", "f": "1"}]))


@pytest.mark.parametrize("index", range(3))
def test_each_response_must_bind_requested_security(monkeypatch, index):
    bundle = list(payloads())
    bundle[index] = bundle[index].replace(b"000425", b"000001")
    with pytest.raises(ValueError, match="binding"):
        parse(monkeypatch, bundle=bundle)


@pytest.mark.parametrize("kind", ["history", "shares", "qfq"])
def test_duplicate_dates_reject_even_if_payload_equal(monkeypatch, kind):
    if kind == "history":
        call = lambda: parse(monkeypatch, history_rows=[rows()[0], rows()[0]])
    elif kind == "shares":
        call = lambda: parse(monkeypatch, bundle=payloads(shares=[{"date": "2024-01-01", "amount": 10}] * 2))
    else:
        call = lambda: parse(monkeypatch, bundle=payloads(factors=[{"d": "2024-01-01", "f": "2"}] * 2))
    with pytest.raises(ValueError, match="duplicate"):
        call()


@pytest.mark.parametrize("value", ["2024-02-30T00:00:00.000Z", "2024-01-02T01:00:00.000Z"])
def test_invalid_or_non_midnight_history_dates_reject(monkeypatch, value):
    data = rows()
    data[0]["date"] = value
    with pytest.raises(ValueError, match="date"):
        parse(monkeypatch, history_rows=data)


@pytest.mark.parametrize("field,value", [("volume", -1), ("amount", float("nan")), ("close", None)])
def test_invalid_raw_values_never_filled(monkeypatch, field, value):
    data = rows()
    data[0][field] = value
    with pytest.raises(ValueError, match=field):
        parse(monkeypatch, history_rows=data)


def test_remote_javascript_is_never_executed(monkeypatch):
    bundle = list(payloads())
    bundle[0] = b'var KLC_K2_sz000425="K2/ABC";globalThis.remoteCode=1;'
    with pytest.raises(ValueError):
        parse(monkeypatch, bundle=bundle)


def test_raw_day_range_is_inclusive_without_calendar_fill(monkeypatch):
    result = parse(monkeypatch, start=date(2024, 1, 3), end=date(2024, 1, 3))
    assert len(result) == 1
    assert result.iloc[0]["trade_date"] == pd.Timestamp("2024-01-03")


def test_raw_mode_preserves_unadjusted_prices_and_units(monkeypatch):
    data = rows()
    data[0]["open"] = 10.12345
    result = parse(monkeypatch, history_rows=data, adjust="")
    assert result.iloc[0]["open"] == 10.12345
    assert result["close"].tolist() == [11, 11]
    assert result["volume"].tolist() == [2000, 2000]
    assert result["amount"].tolist() == [21000, 21000]
    assert result["turnover_rate"].tolist() == [2, 2]
    info = result.attrs["sina_evidence"]
    assert info["adjust"] == ""
    assert info["price_method"] == "raw_ohlc"
    assert len(info["decoder"]["code_sha256"]) == 64
    json.dumps(result.attrs, allow_nan=False)


def test_raw_mode_does_not_require_an_effective_qfq_factor(monkeypatch):
    result = parse(monkeypatch, adjust="", bundle=payloads(factors=[{"d": "2024-01-20", "f": "2"}]))
    assert len(result) == 2
    assert result.iloc[0]["close"] == 11


def test_raw_mode_still_validates_qfq_security_binding(monkeypatch):
    bundle = list(payloads())
    bundle[2] = bundle[2].replace(b"000425", b"000001")
    with pytest.raises(ValueError, match="binding"):
        parse(monkeypatch, adjust="", bundle=bundle)


@pytest.mark.parametrize("adjust", ["hfq", "raw", None])
def test_unknown_adjustment_rejected(monkeypatch, adjust):
    with pytest.raises(ValueError, match="adjustment"):
        parse(monkeypatch, adjust=adjust)


def test_old_zero_share_observation_does_not_reject_later_known_interval(monkeypatch):
    result = parse(monkeypatch, bundle=payloads(shares=[
        {"date": "1997-08-19", "amount": 0}, {"date": "2000-12-25", "amount": 10}]))
    assert result.turnover_rate.tolist() == [2, 2]
    assert result.attrs["sina_evidence"]["shares_unknown_effective_records"] == 1


def test_zero_shares_before_window_stay_unknown_until_next_positive(monkeypatch):
    result = parse(monkeypatch, bundle=payloads(shares=[
        {"date": "2024-01-01", "amount": 0}, {"date": "2024-01-03", "amount": 20}]))
    assert pd.isna(result.iloc[0].turnover_rate)
    assert result.iloc[1].turnover_rate == 1


def test_zero_share_event_inside_window_blocks_older_positive_observation(monkeypatch):
    result = parse(monkeypatch, bundle=payloads(shares=[
        {"date": "2024-01-01", "amount": 10}, {"date": "2024-01-02", "amount": 0},
        {"date": "2024-01-03", "amount": 20}]))
    assert pd.isna(result.iloc[0].turnover_rate)
    assert result.iloc[1].turnover_rate == 1


def test_consecutive_zero_share_boundaries_never_backfill_or_use_old_value(monkeypatch):
    data = rows()
    data.append({**data[-1], "date": "2024-01-04T00:00:00.000Z"})
    result = parse(monkeypatch, history_rows=data, bundle=payloads(shares=[
        {"date": "2024-01-01", "amount": 10}, {"date": "2024-01-02", "amount": 0},
        {"date": "2024-01-03", "amount": 0}, {"date": "2024-01-04", "amount": 20}]))
    assert result.turnover_rate.iloc[:2].isna().all()
    assert result.iloc[2].turnover_rate == 1
    assert result.attrs["sina_evidence"]["turnover_unknown_selected_rows"] == 2
    json.dumps(result.attrs, allow_nan=False)


@pytest.mark.parametrize("bad", [-1, "NaN", "Infinity"])
def test_invalid_share_observation_still_rejects(monkeypatch, bad):
    with pytest.raises(ValueError, match="outstanding_share"):
        parse(monkeypatch, bundle=payloads(shares=[{"date": "2024-01-01", "amount": bad}]))


def test_zero_qfq_factor_still_rejects_even_when_old(monkeypatch):
    with pytest.raises(ValueError, match="qfq_factor"):
        parse(monkeypatch, bundle=payloads(factors=[
            {"d": "1997-08-19", "f": "0"}, {"d": "2024-01-01", "f": "1"}]))
