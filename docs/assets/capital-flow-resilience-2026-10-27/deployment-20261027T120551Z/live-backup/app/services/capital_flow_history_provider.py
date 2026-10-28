"""Bounded, dated Eastmoney capital-flow history with identity/field validation."""
from __future__ import annotations

from datetime import date
import math
import os
import re
import threading
import time

import pandas as pd
import requests

URL = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
# 2026-10-26 实测：部分网络会直接 reset 该主机的 TLS 连接（RemoteDisconnected），
# 同一公开接口走 HTTP 正常。保留 HTTPS 为主路径，仅在连接失败时降级，并在
# 本进程内记住降级结果，避免每只标的都先等一次失败握手。
FALLBACK_URL = "http://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
_https_available = True
# 同一 IP 短时间连发数百次后，该主机会直接 RST 所有连接（curl 也一样），
# 属于接口级限流而非网络故障。这里做进程级限速 + 失败指数退避：默认每只标的
# 间隔 15 秒（实测约 4 条请求后开始被拒，需要放在可持续区间），失败后退避
# 30 秒起翻倍、最长 90 秒（低于 MySQL wait_timeout=120 秒）；可用环境变量调整。
_MAX_BACKOFF_SECONDS = 90.0
_pace_lock = threading.Lock()
_next_request_at = 0.0
_backoff_seconds = 0.0
# 补全浏览器请求头：风控对无 UA 的客户端更容易直接拒连。
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://quote.eastmoney.com/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}
HISTORY_LIMIT = 120
COLUMNS = ["日期", "主力净流入-净额", "小单净流入-净额", "中单净流入-净额",
           "大单净流入-净额", "超大单净流入-净额", "主力净流入-净占比",
           "小单净流入-净占比", "中单净流入-净占比", "大单净流入-净占比",
           "超大单净流入-净占比", "收盘价", "涨跌幅", "reserved_1", "reserved_2"]


def _env_seconds(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return max(float(raw), 0.0)
    except ValueError:
        return default


def _wait_turn() -> None:
    """Serialize provider calls across threads with a fixed minimum interval."""
    global _next_request_at
    interval = _env_seconds("CAPITAL_FLOW_REQUEST_INTERVAL_SECONDS", 15.0)
    with _pace_lock:
        now = time.monotonic()
        wait = _next_request_at - now
        _next_request_at = max(now, _next_request_at) + interval
    if wait > 0:
        time.sleep(wait)


def _record_failure() -> None:
    global _next_request_at, _backoff_seconds
    base = _env_seconds("CAPITAL_FLOW_BACKOFF_BASE_SECONDS", 30.0)
    cap = _env_seconds("CAPITAL_FLOW_BACKOFF_MAX_SECONDS", _MAX_BACKOFF_SECONDS)
    with _pace_lock:
        _backoff_seconds = min(max(_backoff_seconds * 2, base), cap)
        _next_request_at = max(_next_request_at, time.monotonic() + _backoff_seconds)


def _record_success() -> None:
    global _backoff_seconds
    _backoff_seconds = 0.0


def fetch_capital_flow_history(*, stock: str, market: str) -> pd.DataFrame:
    global _https_available
    if not re.fullmatch(r"[0-9]{6}", stock) or market not in {"sh", "sz", "bj"}:
        raise ValueError("Invalid capital-flow instrument")
    market_id = 1 if market == "sh" else 0
    params = {"lmt": str(HISTORY_LIMIT), "klt": "101", "secid": f"{market_id}.{stock}",
              "fields1": "f1,f2,f3,f7", "fields2": ",".join(f"f{i}" for i in range(51, 66)),
              "ut": "b2884a393a59ad64002292a3e90d46a5"}

    def _request(session, url):
        response = session.get(url, params=params, timeout=(5, 15), headers=HEADERS,
                               allow_redirects=False)
        response.raise_for_status()
        if response.status_code != 200 or len(response.content) > 1_000_000:
            raise ValueError("Invalid capital-flow HTTP response")
        return response.json()

    with requests.Session() as session:
        session.trust_env = False
        _wait_turn()
        url = URL if _https_available else FALLBACK_URL
        try:
            payload = _request(session, url)
        except requests.exceptions.ConnectionError:
            if url != URL:
                _record_failure()
                raise
            # HTTPS 被拒是网络属性，不当作限流；降级后同一 slot 内立即重试。
            _https_available = False
            url = FALLBACK_URL
            try:
                payload = _request(session, url)
            except requests.exceptions.ConnectionError:
                _record_failure()
                raise
    _record_success()
    frame = parse_capital_flow_payload(payload, stock=stock, market=market)
    frame.attrs["source_receipt"] = {"url": url, "params": params, "payload": payload}
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
