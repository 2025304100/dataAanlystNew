from __future__ import annotations

import math
from datetime import date

import pandas as pd
import pytest

from app.services.factors.contracts import DataContractError
from app.services.factors.tail_proxy import (
    calculate_tail_proxy,
    normalize_minute_frame,
)


pytestmark = pytest.mark.whitebox


def minute_frame(trade_date: date, *, tail_amount: float = 200.0):
    morning = pd.date_range(
        f"{trade_date} 09:30:00",
        f"{trade_date} 11:30:00",
        freq="1min",
    )
    afternoon = pd.date_range(
        f"{trade_date} 13:00:00",
        f"{trade_date} 15:00:00",
        freq="1min",
    )
    timestamps = morning.append(afternoon)
    rows = []
    for index, timestamp in enumerate(timestamps):
        in_tail = timestamp.time() >= pd.Timestamp("14:30").time()
        close = 10.0 + index / max(len(timestamps) - 1, 1)
        rows.append(
            {
                "时间": timestamp,
                "收盘": close,
                "最高": close + 0.1,
                "最低": close - 0.1,
                "成交额": tail_amount if in_tail else 100.0,
            }
        )
    return pd.DataFrame(rows)


def test_tail_proxy_combines_activity_return_and_close_location():
    trade_date = date(2026, 7, 14)
    result = calculate_tail_proxy(
        minute_frame(trade_date), trade_date=trade_date
    )

    assert result is not None
    assert result.minute_count >= 240
    assert result.tail_minute_count == 31
    assert result.tail_activity_ratio == pytest.approx(2.0)
    assert result.tail_amount_share > 0
    assert result.tail_return > 0
    assert 0 <= result.close_location <= 1
    assert result.proxy_score == pytest.approx(
        math.log(result.tail_activity_ratio)
        + 20 * result.tail_return
        + result.close_location
        - 0.5
    )


def test_tail_proxy_rejects_incomplete_session():
    trade_date = date(2026, 7, 14)
    partial = minute_frame(trade_date).tail(10)
    assert calculate_tail_proxy(
        partial, trade_date=trade_date
    ) is None
    premature = minute_frame(trade_date)
    premature = premature[
        pd.to_datetime(premature["时间"]).dt.time
        <= pd.Timestamp("14:50").time()
    ]
    assert len(premature) >= 180
    assert calculate_tail_proxy(
        premature, trade_date=trade_date
    ) is None


def test_minute_contract_rejects_missing_amount():
    with pytest.raises(DataContractError) as exc_info:
        normalize_minute_frame(
            pd.DataFrame(
                [
                    {
                        "时间": "2026-07-14 14:30:00",
                        "收盘": 10,
                        "最高": 11,
                        "最低": 9,
                    }
                ]
            )
        )
    assert exc_info.value.api_key == "stock_zh_a_hist_min_em"
