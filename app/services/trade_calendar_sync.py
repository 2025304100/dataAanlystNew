"""Cache the exchange's published annual A-share closure schedule locally."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
from datetime import date, datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup
from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.app_setting import AppSetting
from app.models.market_data import MarketCalendar
from app.services.data_source_execution import source_task

SOURCE_URL = "https://www.sse.com.cn/disclosure/dealinstruc/closed/"
STATE_KEY = "market.cn_trade_calendar"
_lock = threading.Lock()
logger = logging.getLogger(__name__)


def parse_exchange_calendar(html: str) -> tuple[int, dict[date, bool]]:
    """Reject changed/incomplete pages rather than guessing missing holidays."""
    soup = BeautifulSoup(html, "html.parser")
    match = re.search(r"(\d{4})年休市安排", soup.get_text(" ", strip=True))
    if not match:
        raise ValueError("交易所页面缺少年度休市安排")
    year = int(match[1])
    holidays = {"元旦", "春节", "清明节", "劳动节", "端午节", "中秋节", "国庆节"}
    closures: set[date] = set()
    found = set()
    for row in soup.select("table tr"):
        cells = row.find_all("td")
        if len(cells) != 2:
            continue
        label = cells[0].get_text(strip=True).rstrip("：:")
        if label not in holidays:
            continue
        if label in found:
            raise ValueError("交易所年度休市安排存在重复节日，保留本地日历")
        text = cells[1].get_text("", strip=True)
        if "休市" not in text:
            raise ValueError(f"{label}休市区间无法识别")
        period = text.split("休市", 1)[0]
        values = re.findall(r"(\d{1,2})月(\d{1,2})日", period)
        if len(values) not in (1, 2) or (len(values) == 2 and "至" not in period):
            raise ValueError(f"{label}休市日期格式变化")
        start = date(year, *map(int, values[0]))
        end = date(year, *map(int, values[-1]))
        if not 0 <= (end - start).days <= 15:
            raise ValueError("交易所休市区间异常")
        closures.update(start + timedelta(days=i) for i in range((end - start).days + 1))
        found.add(label)
    if found != holidays:
        raise ValueError("交易所年度休市安排不完整，保留本地日历")
    start, end = date(year, 1, 1), date(year, 12, 31)
    calendar = {
        day: day.weekday() < 5 and day not in closures
        for day in (start + timedelta(days=i) for i in range((end - start).days + 1))
    }
    if not 235 <= sum(calendar.values()) <= 250:
        raise ValueError("年度交易日数量异常，保留本地日历")
    return year, calendar


@source_task()
def capture_cn_calendar_version(master_service) -> dict:
    """Capture the existing SSE source into an explicitly supplied master store.

    No legacy calendar projection or global read switch is changed. Historical
    annual pages can also be imported through the master's public publisher.
    """
    from app.services.akshare_utils import call_akshare_with_retry
    response=call_akshare_with_retry(requests.get,SOURCE_URL,timeout=(5,20),api_key='CAL:official')
    response.raise_for_status()
    version=master_service.publish_calendar(exchange='SSE',source_uri=SOURCE_URL,evidence=response.content)
    return {'calendar_version':version,'exchange':'SSE','source_uri':SOURCE_URL}


@source_task()
def refresh_cn_trade_calendar(*, force: bool = False) -> dict:
    """One small download per day; a failure never removes cached dates."""
    if not _lock.acquire(blocking=False):
        return {"status": "busy"}
    try:
        with SessionLocal() as db:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            state = db.get(AppSetting, STATE_KEY)
            if not force and state and state.updated_at > now - timedelta(days=1):
                return {"status": "cached", **json.loads(state.value_json)}
            from app.services.akshare_utils import call_akshare_with_retry
            response = call_akshare_with_retry(requests.get, SOURCE_URL, timeout=(5, 20), api_key='CAL:official')
            response.raise_for_status()
            year, calendar = parse_exchange_calendar(response.content.decode("utf-8"))
            current_year = (now + timedelta(hours=8)).year
            if year not in (current_year, current_year + 1):
                raise ValueError("交易所返回的年度日历已过期")
            existing = {row.trade_date: row for row in db.scalars(select(MarketCalendar).where(
                MarketCalendar.trade_date >= date(year, 1, 1),
                MarketCalendar.trade_date <= date(year, 12, 31),
            ))}
            changed = 0
            for day, is_open in calendar.items():
                entry = existing.get(day)
                if entry is None:
                    db.add(MarketCalendar(trade_date=day, is_trading_day=int(is_open)))
                    changed += 1
                elif entry.is_trading_day != int(is_open):
                    entry.is_trading_day = int(is_open)
                    changed += 1
            evidence = {"year": year, "source": SOURCE_URL, "trading_days": sum(calendar.values()),
                        "refreshed_at": now.isoformat(), "changed": changed}
            if state is None:
                state = AppSetting(key=STATE_KEY)
                db.add(state)
            state.value_json = json.dumps(evidence)
            state.updated_by = "exchange-calendar-sync"
            state.updated_at = now
            db.commit()
            from app.services.factors.formula_catalog import invalidate_formula_catalog_cache

            invalidate_formula_catalog_cache()
            logger.info("Exchange calendar refreshed: %s", evidence)
            return {"status": "done", **evidence}
    finally:
        _lock.release()


async def calendar_refresh_loop() -> None:
    if os.getenv("AUTO_TRADE_CALENDAR_REFRESH", "1") == "0":
        return
    while True:
        try:
            await asyncio.to_thread(refresh_cn_trade_calendar)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Exchange calendar refresh failed; retaining local calendar", exc_info=True)
        # A failed download is retried at most hourly; successful refreshes
        # use the persisted 24-hour cache across process restarts.
        await asyncio.sleep(3600)
