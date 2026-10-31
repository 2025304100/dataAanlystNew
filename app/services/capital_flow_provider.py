"""Selected A-share current fund flow; one bounded quote batch, no history."""
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from app.services.akshare_utils import assert_akshare_api_enabled, call_akshare_with_retry
from app.services.request_coalescing import coalesced_read, request_key
from app.services.market_data import _proxy_bypass
from app.services.data_source_execution import source_task

# Anonymous clients use the public visitor route. The legacy /api route can
# disconnect before sending an HTTP response even when the quotes exist.
URL = "https://push2.eastmoney.com/webguest/api/qt/ulist.np/get"
_RETRY_URL = "https://82.push2.eastmoney.com/webguest/api/qt/ulist.np/get"
BATCH_SIZE = 100
FIELD_MAP = {
    "main_net_inflow": "f62", "main_net_inflow_pct": "f184",
    "super_large_net_inflow": "f66", "large_net_inflow": "f72",
    "medium_net_inflow": "f78", "small_net_inflow": "f84",
}


@source_task(lane='incremental')
def fetch_capital_flow_quotes(db, secids: list[str], *, should_cancel=None) -> dict[str, dict]:
    secids = list(dict.fromkeys(secids))
    if not secids:
        return {}
    if len(secids) > BATCH_SIZE or any(
        len(s) != 8 or s[:2] not in {"0.", "1."} or not s[2:].isascii() or not s[2:].isdigit()
        for s in secids
    ):
        raise ValueError("资金流请求代码或单批数量无效")
    if should_cancel and should_cancel():
        raise InterruptedError("资金流同步已取消")
    params = {"secids": ",".join(secids), "fields": "f12,f13,f124,f297," + ",".join(FIELD_MAP.values()),
              "fltt": "2", "invt": "2"}

    attempt = 0
    def request():
        nonlocal attempt
        if should_cancel and should_cancel():
            raise InterruptedError("资金流同步已取消")
        # Use the same quote endpoint on the provider's alternate host for the
        # existing second network attempt; never add retries or expand scope.
        url = URL if attempt == 0 else _RETRY_URL
        attempt += 1
        with _proxy_bypass():
            response = requests.get(url, params=params, timeout=(5, 15),
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"})
            response.raise_for_status()
            return response.json()

    api_key = "stock_individual_fund_flow_rank"
    assert_akshare_api_enabled(api_key)
    payload = coalesced_read(request_key(api_key, URL, params),
        lambda: call_akshare_with_retry(request, api_key=api_key, db=db, max_attempts=2), should_cancel=should_cancel)
    assert_akshare_api_enabled(api_key)
    if not isinstance(payload, dict) or payload.get("rc") != 0:
        raise ValueError("资金流当日接口返回异常，未切换历史下载")
    data = payload.get("data")
    if data is None:
        return {}
    if not isinstance(data, dict) or not isinstance(data.get("diff"), (list, dict)):
        raise ValueError("资金流当日接口数据结构异常")
    rows = data["diff"]
    rows = list(rows.values()) if isinstance(rows, dict) else rows
    if len(rows) > len(secids) or int(data.get("total", len(rows))) > len(secids):
        raise ValueError("资金流接口未遵守标的范围")
    quotes = {}
    codes = {s[2:] for s in secids}
    for row in rows:
        code = str(row.get("f12", ""))
        if code not in codes or code in quotes:
            raise ValueError("资金流接口返回范围外或重复代码")
        if "f13" in row and f"{row['f13']}.{code}" not in secids:
            raise ValueError("资金流接口返回错误市场")
        try:
            stamp = datetime.fromtimestamp(float(row["f124"]), ZoneInfo("Asia/Shanghai"))
            day = datetime.strptime(str(row["f297"]), "%Y%m%d").date()
            if stamp.date() != day:
                raise ValueError("行情日期与更新时间不一致")
        except (KeyError, TypeError, ValueError, OSError, OverflowError) as exc:
            raise ValueError(f"资金流 {code} 缺少可信日期：{exc}") from exc
        quotes[code] = {"trade_date": day, "updated_at": stamp.isoformat(),
                        **{field: row.get(key) for field, key in FIELD_MAP.items()}}
    return quotes
