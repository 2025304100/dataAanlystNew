"""External clock seam only; production app, APIs and DB remain real."""
import os
from datetime import datetime
from pathlib import Path
from tests.market_news_e2e_server import app
from app.services import market_signal_sources
from app.services.notifications import dispatcher, outbox

clock_file = Path(os.environ["MARKET_SIGNALS_CLOCK_FILE"])
def utcnow():
    return datetime.fromisoformat(clock_file.read_text(encoding="utf-8").strip())

market_signal_sources.utcnow = utcnow
dispatcher._now_utc_naive = utcnow
outbox._now_utc = utcnow
