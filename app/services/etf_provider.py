"""Small current ETF quote requests; never download a market list or NAV history."""
from __future__ import annotations

import math
from datetime import date, datetime
from typing import Callable
from zoneinfo import ZoneInfo

import requests

from app.services.akshare_utils import assert_akshare_api_enabled, call_akshare_with_retry
from app.services.request_coalescing import coalesced_read, request_key
from app.services.market_data import _proxy_bypass
from app.services.data_source_execution import source_task

URL = "https://push2.eastmoney.com/api/qt/ulist.np/get"
FIELDS = "f12,f2,f38,f441,f402,f297,f124"
BATCH_SIZE = 100


def _positive(value) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) and number > 0 else None
    except (ValueError, TypeError):
        return None


@source_task(lane='incremental')
def fetch_etf_quotes(db, codes: list[str], *, should_cancel: Callable[[], bool] | None = None) -> dict[str, dict]:
    codes = list(dict.fromkeys(codes))
    if not codes:
        return {}
    if len(codes) > BATCH_SIZE or any(
        len(code) != 6 or not code.isascii() or not code.isdigit() or code[0] not in "15"
        for code in codes
    ):
        raise ValueError("ETF 行情请求代码或单批数量无效")
    if should_cancel and should_cancel():
        raise InterruptedError("ETF 同步已取消")
    params = {"secids": ",".join(f"{'1' if code.startswith('5') else '0'}.{code}" for code in codes),
              "fields": FIELDS, "fltt": "2", "invt": "2"}

    def request():
        with _proxy_bypass():
            response = requests.get(URL, params=params, timeout=(5, 15))
            response.raise_for_status()
            return response.json()

    api_key = "fund_etf_spot_em"
    assert_akshare_api_enabled(api_key)
    payload = coalesced_read(request_key(api_key, URL, params),
        lambda: call_akshare_with_retry(request, api_key=api_key, max_attempts=2, db=db), should_cancel=should_cancel)
    assert_akshare_api_enabled(api_key)
    if not isinstance(payload, dict) or payload.get("rc") != 0:
        raise ValueError("ETF 行情接口返回异常，未切换全市场或历史下载")
    data = payload.get("data")
    if data is None:
        return {}
    if not isinstance(data, dict) or not isinstance(data.get("diff"), (list, dict)):
        raise ValueError("ETF 行情接口数据结构异常")
    rows = data["diff"]
    rows = list(rows.values()) if isinstance(rows, dict) else rows
    if len(rows) > len(codes) or int(data.get("total", len(rows))) > len(codes):
        raise ValueError("ETF 行情接口未遵守标的范围，已停止同步")
    quotes = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("ETF 行情记录结构异常")
        code = str(row.get("f12", ""))
        if code not in codes or code in quotes:
            raise ValueError("ETF 行情接口返回范围外或重复代码")
        try:
            updated_at = datetime.fromtimestamp(float(row["f124"]), ZoneInfo("Asia/Shanghai"))
            # Both provider date fields are evidence; never stamp undated quotes as today.
            reported_date = datetime.strptime(str(row["f297"]), "%Y%m%d").date()
            if updated_at.date() != reported_date:
                raise ValueError("行情日期与更新时间不一致")
        except (ValueError, TypeError, KeyError, OverflowError, OSError) as exc:
            raise ValueError(f"ETF {code} 行情缺少可信日期：{exc}") from exc
        quotes[code] = {"trade_date": reported_date, "updated_at": updated_at.isoformat(),
                        "close": _positive(row.get("f2")), "nav": _positive(row.get("f441")),
                        "total_shares": _positive(row.get("f38")), "provider_discount": row.get("f402"),
                        "nav_basis": "iopv"}
    return quotes
