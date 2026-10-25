"""Bounded, dated Eastmoney capital-flow history with identity/field validation."""
from __future__ import annotations

from datetime import date
import math
import re

import pandas as pd
import requests

URL = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
HISTORY_LIMIT = 120
COLUMNS = ["日期", "主力净流入-净额", "小单净流入-净额", "中单净流入-净额",
           "大单净流入-净额", "超大单净流入-净额", "主力净流入-净占比",
           "小单净流入-净占比", "中单净流入-净占比", "大单净流入-净占比",
           "超大单净流入-净占比", "收盘价", "涨跌幅", "reserved_1", "reserved_2"]


def fetch_capital_flow_history(*, stock: str, market: str) -> pd.DataFrame:
    if not re.fullmatch(r"[0-9]{6}", stock) or market not in {"sh", "sz", "bj"}:
        raise ValueError("Invalid capital-flow instrument")
    market_id = 1 if market == "sh" else 0
    params = {"lmt": str(HISTORY_LIMIT), "klt": "101", "secid": f"{market_id}.{stock}",
              "fields1": "f1,f2,f3,f7", "fields2": ",".join(f"f{i}" for i in range(51, 66)),
              "ut": "b2884a393a59ad64002292a3e90d46a5"}
    with requests.Session() as session:
        session.trust_env = False
        response = session.get(URL, params=params, timeout=(5, 15), allow_redirects=False)
        response.raise_for_status()
        if response.status_code != 200 or len(response.content) > 1_000_000:
            raise ValueError("Invalid capital-flow HTTP response")
        payload = response.json()
    frame = parse_capital_flow_payload(payload, stock=stock, market=market)
    frame.attrs["source_receipt"] = {"url": URL, "params": params, "payload": payload}
    return frame


def parse_capital_flow_payload(payload, *, stock: str, market: str) -> pd.DataFrame:
    """Also validate archived real responses, without another provider call."""
    if not re.fullmatch(r"[0-9]{6}", stock) or market not in {"sh", "sz", "bj"}:
        raise ValueError("Invalid capital-flow instrument")
    market_id = 1 if market == "sh" else 0
    if not isinstance(payload, dict) or payload.get("rc") != 0:
        raise ValueError("Capital-flow provider returned failure")
    data = payload.get("data")
    if data is None:
        frame = pd.DataFrame(columns=COLUMNS)
    else:
        if data.get("code") != stock or data.get("market") != market_id:
            raise ValueError("Capital-flow provider instrument mismatch")
        lines = data.get("klines")
        if not isinstance(lines, list) or len(lines) > HISTORY_LIMIT:
            raise ValueError("Capital-flow provider exceeded bounded history")
        rows, seen = [], set()
        for line in lines:
            parts = line.split(",")
            if len(parts) != len(COLUMNS):
                raise ValueError("Capital-flow column count changed")
            day = date.fromisoformat(parts[0])
            if parts[0] != day.isoformat() or day in seen or day > date.today():
                raise ValueError("Invalid/duplicate capital-flow date")
            seen.add(day)
            # Six stored source fields are required; never coerce bad values to NULL.
            values = [float(value) for value in parts[1:7]]
            if not all(math.isfinite(value) for value in values):
                raise ValueError("Incomplete capital-flow key fields")
            rows.append([day.isoformat(), *values, *parts[7:]])
        frame = pd.DataFrame(rows, columns=COLUMNS)
    return frame
