"""Read Baidu's public calendar JSON without a brittle website-cookie preflight.

No user cookies, authentication bypass, or browser state is used. HTTP/business
errors and schema changes remain explicit supplier failures.
"""
from datetime import datetime
import re

import pandas as pd
import requests
from app.services.data_source_execution import source_boundary

CALENDAR_URL = "https://finance.pae.baidu.com/sapi/v1/financecalendar"


@source_boundary('CAL:baidu')
def fetch_baidu_calendar(*, date: str, category: str) -> pd.DataFrame:
    day = datetime.strptime(date, "%Y%m%d").strftime("%Y-%m-%d")
    if category not in ("economic_data", "report_time"):
        raise ValueError("Unsupported Baidu calendar category")
    response = requests.get(CALENDAR_URL, params={"start_date":day,"end_date":day,"pn":"0",
        "rn":"100","cate":category,"finClientType":"pc"}, headers={
        "Referer":"https://gushitong.baidu.com/", "Origin":"https://gushitong.baidu.com",
    }, timeout=(5, 12))
    response.raise_for_status()
    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError("Baidu calendar returned non-JSON data") from exc
    return parse_baidu_calendar(data, date=date, category=category)


def parse_baidu_calendar(data, *, date: str, category: str) -> pd.DataFrame:
    """Parse retained vendor bytes without granting this excluded source I/O."""
    day = datetime.strptime(date, "%Y%m%d").strftime("%Y-%m-%d")
    if category not in ("economic_data", "report_time"):
        raise ValueError("Unsupported Baidu calendar category")
    if not isinstance(data, dict) or str(data.get("ResultCode")) != "0":
        raise RuntimeError("Baidu calendar rejected the request")
    result = data.get("Result")
    calendars = result.get("calendarInfo") if isinstance(result, dict) else None
    if not isinstance(calendars, list):
        raise RuntimeError("Baidu calendar response schema changed")
    matching = [item for item in calendars if isinstance(item, dict) and item.get("date") == day]
    if calendars and not matching:
        raise RuntimeError("Baidu calendar returned a different date")
    rows = []
    for calendar in matching:
        items = calendar.get("list")
        if not isinstance(items, list):
            raise RuntimeError("Baidu calendar item schema changed")
        for item in items:
            if not isinstance(item, dict):
                raise RuntimeError("Baidu calendar item schema changed")
            time = str(item.get("time") or "")
            stamp = f"{day} {time}" if re.fullmatch(r"\d{2}:\d{2}(?::\d{2})?", time) else day
            if category == "economic_data":
                name = str(item.get("title") or "").strip()
                if not name:
                    raise RuntimeError("Baidu economic calendar is missing its event title")
                title = f"经济日历：{item.get('region') or ''} {name}".strip()
                details = [("前值",item.get("formerVal")),("预期",item.get("indicateVal")),("公布",item.get("pubVal"))]
            else:
                name, code = str(item.get("name") or "").strip(), str(item.get("code") or "").strip()
                if not name or not code:
                    raise RuntimeError("Baidu report calendar is missing its company identity")
                title = f"财报日历：{name}（{code}）{item.get('reportType') or ''}"
                details = [("交易所",item.get("exchange")),("财报类型",item.get("reportType"))]
            summary = "日历计划时间：" + stamp + "；" + "；".join(
                f"{label}：{value}" for label,value in details if value is not None and str(value).strip())
            rows.append(dict(title=title,summary=summary,date=stamp,url="https://gushitong.baidu.com/calendar"))
    # Existing market-news wrappers consume at most the first 40 records. The
    # endpoint's first page (100) covers that bound; this is not a full-calendar export.
    return pd.DataFrame(rows, columns=["title","summary","date","url"])
