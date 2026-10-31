"""Date- and symbol-bounded valuation requests, shared by daily and backfill jobs."""
from __future__ import annotations

from datetime import date
from typing import Callable

import requests

from app.services.akshare_utils import assert_akshare_api_enabled, call_akshare_with_retry
from app.services.request_coalescing import coalesced_read, request_key
from app.services.market_data import _proxy_bypass
from app.services.data_source_execution import source_task

URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
COLUMNS = "SECURITY_CODE,TRADE_DATE,PE_TTM,PB_MRQ,TOTAL_MARKET_CAP,NOTLIMITED_MARKETCAP_A"
DAILY_BATCH_SIZE = 200


@source_task(lane='incremental')
def fetch_valuation_rows(db, codes: list[str], start: date, end: date,
                         *, should_cancel: Callable[[], bool] | None = None) -> list[dict]:
    """Every page carries server-side date filters; never fetch an unbounded history."""
    codes = list(dict.fromkeys(codes))
    if not codes:
        return []
    if start > end or any(len(code) != 6 or not code.isascii() or not code.isdigit() for code in codes):
        raise ValueError("估值请求日期或股票代码无效")
    if len(codes) > DAILY_BATCH_SIZE:
        raise ValueError("估值请求超过单批标的上限")
    daily = start == end
    date_filter = f"(TRADE_DATE='{start.isoformat()}')" if daily else (
        f"(TRADE_DATE>='{start.isoformat()}')(TRADE_DATE<='{end.isoformat()}')"
    )
    symbol_filter = '(SECURITY_CODE in (' + ','.join(f'"{code}"' for code in codes) + '))'
    page_size = len(codes) if daily else 1000
    params = {"reportName": "RPT_VALUEANALYSIS_DET", "columns": COLUMNS,
              "filter": date_filter + symbol_filter, "pageSize": str(page_size),
              "sortColumns": "TRADE_DATE,SECURITY_CODE", "sortTypes": "1,1",
              "source": "WEB", "client": "WEB"}
    rows, seen = [], set()
    page, pages, expected = 1, 1, None
    while page <= pages:
        if should_cancel and should_cancel():
            raise InterruptedError("估值同步已取消")

        def request_page():
            with _proxy_bypass():
                response = requests.get(URL, params={**params, "pageNumber": str(page)}, timeout=(5, 15))
                response.raise_for_status()
                return response.json()

        # No provider-internal retries: the whole page has a two-attempt cap.
        api_key = "stock_value_em"
        assert_akshare_api_enabled(api_key)
        payload = coalesced_read(request_key(api_key, URL, {**params, "pageNumber": str(page)}),
            lambda: call_akshare_with_retry(request_page, api_key=api_key, max_attempts=2, db=db), should_cancel=should_cancel)
        assert_akshare_api_enabled(api_key)
        if payload.get("success") is False:
            if payload.get("code") == 9201 and not payload.get("result") and page == 1:
                return []
            raise RuntimeError(f"估值日期接口不可用：{payload.get('message', 'unknown error')}")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise ValueError("估值接口返回结构异常，已停止同步")
        count, reported_pages = int(result.get("count", 0)), int(result.get("pages", 0))
        # The maximum possible response has one observation per symbol/day.
        if count < 0 or count > len(codes) * ((end - start).days + 1):
            raise ValueError("估值接口未遵守日期或标的过滤，已停止下载")
        if page == 1:
            expected, pages = count, reported_pages
            if not count:
                return []
            if pages != (count + page_size - 1) // page_size:
                raise ValueError("估值接口分页信息异常")
        elif count != expected or reported_pages != pages:
            raise ValueError("估值接口分页在下载期间发生变化，请重试")
        data = result.get("data")
        if not isinstance(data, list) or not data or len(data) > page_size:
            raise ValueError("估值接口分页数据不完整或超过请求上限")
        for raw in data:
            code = str(raw.get("SECURITY_CODE", ""))
            day = date.fromisoformat(str(raw.get("TRADE_DATE", ""))[:10])
            key = (code, day)
            if code not in codes or not start <= day <= end or key in seen:
                raise ValueError("估值接口返回范围外或重复记录，已停止同步")
            seen.add(key)
            rows.append({"symbol": code, "trade_date": day, "pe_ttm": raw.get("PE_TTM"),
                         "pb": raw.get("PB_MRQ"), "total_market_cap": raw.get("TOTAL_MARKET_CAP"),
                         "circulating_market_cap": raw.get("NOTLIMITED_MARKETCAP_A")})
        page += 1
    if len(rows) != expected:
        raise ValueError("估值接口记录数量不完整，未写入部分结果")
    return rows
