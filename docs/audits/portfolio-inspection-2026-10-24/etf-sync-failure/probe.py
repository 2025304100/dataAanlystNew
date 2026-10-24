"""Read the requested ETF range from the existing adapter; never touch the DB."""
from pathlib import Path
from types import SimpleNamespace
from datetime import date
import json
import logging
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
logging.disable(logging.CRITICAL)
from app.services.market_data import _fetch_history

result = {"symbol": "510300", "price_basis": "qfq", "start_date": "2026-10-21", "end_date": "2026-10-23", "database_writes": False}
try:
    frame = _fetch_history(SimpleNamespace(symbol="510300", asset_type="etf", market="sh"), date(2026, 10, 21), date(2026, 10, 23), "qfq")
    result.update(status="available" if not frame.empty else "empty", rows=len(frame), trade_dates=[str(day)[:10] for day in frame.get("trade_date", [])])
except Exception as exc:
    chain = []
    current = exc
    while current is not None and len(chain) < 6:
        chain.append({"type": type(current).__name__, "message": str(current)[:350]})
        current = current.__cause__
    result.update(status="unavailable", cause_chain=chain)
(Path(__file__).parent / "probe.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(result, ensure_ascii=False))
