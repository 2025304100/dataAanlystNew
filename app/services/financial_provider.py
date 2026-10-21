"""Date-bounded financial announcements using the F10 data source."""
from datetime import date
import re

import requests

from app.services.akshare_utils import assert_akshare_api_enabled, call_akshare_with_retry
from app.services.request_coalescing import coalesced_read, request_key
from app.services.market_data import _proxy_bypass

URL = "https://datacenter.eastmoney.com/securities/api/data/get"
COLUMNS = "SECUCODE,REPORT_DATE,NOTICE_DATE,ROEJQ,PARENTNETPROFIT,TOTALOPERATEREVE,PARENTNETPROFITTZ,TOTALOPERATEREVETZ"
BATCH_SIZE = 100


def fetch_financial_rows(db, code: str | list[str], start: date, end: date, *, should_cancel=None) -> list[dict]:
    codes = list(dict.fromkeys([code] if isinstance(code, str) else code))
    if not codes:
        return []
    if len(codes) > BATCH_SIZE or any(not re.fullmatch(r"[0-9]{6}\.(SH|SZ|BJ)", value) for value in codes) or start > end or end > date.today():
        raise ValueError("财报请求代码或公告日期范围无效")
    page_size = 100
    symbol_filter = '(SECUCODE in (' + ','.join(f'"{value}"' for value in codes) + '))'
    params = {"type": "RPT_F10_FINANCE_MAINFINADATA", "sty": COLUMNS,
              "filter": symbol_filter + f"(NOTICE_DATE>='{start}')(NOTICE_DATE<='{end}')",
              "ps": str(page_size), "sr": "1,1", "st": "NOTICE_DATE,REPORT_DATE", "source": "HSF10", "client": "PC"}
    rows, seen = [], set()
    page, pages, expected = 1, 1, None
    while page <= pages:
        if should_cancel and should_cancel():
            raise InterruptedError("财报同步已取消")
        def request():
            with _proxy_bypass():
                response = requests.get(URL, params={**params, "p": str(page)}, timeout=(5, 15))
                response.raise_for_status()
                return response.json()
        api_key = "stock_financial_analysis_indicator_em"
        assert_akshare_api_enabled(api_key)
        payload = coalesced_read(request_key(api_key, URL, {**params, "p": str(page)}),
            lambda: call_akshare_with_retry(request, api_key=api_key, db=db, max_attempts=2), should_cancel=should_cancel)
        assert_akshare_api_enabled(api_key)
        if not isinstance(payload, dict):
            raise ValueError("财报接口返回结构异常")
        if payload.get("success") is False:
            if payload.get("code") == 9201 and page == 1 and not payload.get("result"):
                return []
            raise ValueError(f"财报日期接口不可用：{payload.get('message', 'unknown error')}")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise ValueError("财报接口返回结构异常")
        count, reported_pages = int(result.get("count", 0)), int(result.get("pages", 0))
        if count < 0 or count > len(codes) * 10000:
            raise ValueError("财报接口返回记录数异常")
        if page == 1:
            expected, pages = count, reported_pages
            if not count:
                return []
            if pages != (count + page_size - 1) // page_size:
                raise ValueError("财报接口分页信息异常")
        elif count != expected or reported_pages != pages:
            raise ValueError("财报接口分页发生变化，请重试")
        data = result.get("data")
        if not isinstance(data, list) or not data or len(data) > page_size:
            raise ValueError("财报接口分页不完整")
        for row in data:
            notice = date.fromisoformat(str(row.get("NOTICE_DATE", ""))[:10])
            period = date.fromisoformat(str(row.get("REPORT_DATE", ""))[:10])
            key = (row.get("SECUCODE"), notice, period)
            if row.get("SECUCODE") not in codes or not start <= notice <= end or period > notice or key in seen:
                raise ValueError("财报接口返回范围外、日期无效或重复记录")
            seen.add(key)
            rows.append(row)
        page += 1
    if len(rows) != expected:
        raise ValueError("财报接口记录数量不完整，未写入部分结果")
    return rows
