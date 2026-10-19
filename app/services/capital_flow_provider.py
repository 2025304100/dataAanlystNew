"""Selected A-share current fund flow; one bounded quote batch, no history."""
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from app.services.akshare_utils import call_akshare_with_retry
from app.services.market_data import _proxy_bypass

URL = "https://push2.eastmoney.com/api/qt/ulist.np/get"
BATCH_SIZE = 100
FIELD_MAP = {
    "main_net_inflow": "f62", "main_net_inflow_pct": "f184",
    "super_large_net_inflow": "f66", "large_net_inflow": "f72",
    "medium_net_inflow": "f78", "small_net_inflow": "f84",
}


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

    def request():
        with _proxy_bypass():
            response = requests.get(URL, params=params, timeout=(5, 15))
            response.raise_for_status()
            return response.json()

    payload = call_akshare_with_retry(request, api_key="stock_individual_fund_flow_rank", db=db, max_attempts=2)
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
        quotes[code] = {"trade_date": day, **{field: row.get(key) for field, key in FIELD_MAP.items()}}
    return quotes
