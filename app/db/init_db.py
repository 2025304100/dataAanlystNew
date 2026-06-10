from pathlib import Path

from app.db.base import Base
from app.db.session import engine
from app.core.config import settings
from app.models import daily_bar, factor, journal_entry, news_event, portfolio, scan, score, signal_rule, sim_account, symbol, trade_setup, watchlist


def init_db() -> None:
    if settings.database_url.startswith("sqlite:///"):
        db_path = Path(settings.database_url.replace("sqlite:///", "", 1))
        db_path.parent.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(bind=engine)
