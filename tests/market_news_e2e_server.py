"""Real production app/router/static assets, isolated DB, no lifespan workers."""
import os

from tests.market_news_acceptance_support import assert_identity, engine_for, isolated_url

url = isolated_url("MARKET_NEWS_E2E_MYSQL57_URL")
probe = engine_for(url)
with probe.connect() as connection:
    assert_identity(connection, url)
probe.dispose()

os.environ["DATABASE_URL"] = url.render_as_string(hide_password=False)
os.environ["AUTO_UNIVERSE_SYNC_ON_STARTUP"] = "0"
os.environ["AUTO_TRADE_CALENDAR_REFRESH"] = "0"
from app import main
from app.db.manager import DatabaseManager

DatabaseManager.get().initialize(os.environ["DATABASE_URL"], db_type="mysql")
# The normal app can proxy a developer's port 5173. This process must serve the
# checked-in application's production build instead; no business API is mocked.
main.FRONTEND_DEV_PORT = 0
app = main.app
