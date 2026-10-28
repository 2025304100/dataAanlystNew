"""Shared guards and Alembic execution for explicitly isolated MySQL acceptance."""
import json
import os
from pathlib import Path
import re

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
REV89 = "wps_0024_089_ai_session_surface"
REV90 = "wps_0024_090_market_sector_source"
REV91 = "wps_0024_091_market_compat"
REV92 = "wps_0024_092_market_signals"


def isolated_url(variable="MARKET_NEWS_MYSQL57_URL"):
    raw = os.environ.get(variable)
    if not raw:
        raise RuntimeError(f"{variable} is required; no application-config fallback")
    url = make_url(raw)
    if url.get_backend_name() != "mysql" or not re.fullmatch(r"codex_market_[a-z0-9_]{1,48}", url.database or ""):
        raise RuntimeError("Refusing a non-isolated database")
    return url


def engine_for(url):
    return create_engine(url, hide_parameters=True, pool_pre_ping=True,
                         connect_args={"connect_timeout": 10, "charset": "utf8mb4"})


def assert_identity(connection, url):
    identity = dict(connection.execute(text("SELECT VERSION() AS version, DATABASE() AS database_name, "
        "@@server_uuid AS server_uuid, @@sql_mode AS sql_mode, @@tx_isolation AS isolation_level")).mappings().one())
    if not identity["version"].startswith("5.7.") or identity["database_name"] != url.database:
        raise RuntimeError("Expected the explicit isolated MySQL 5.7 database")
    return identity


def migrate(url, revision, *, downgrade=False):
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    engine = engine_for(url)
    try:
        with engine.connect() as connection:
            assert_identity(connection, url)
            connection.commit()
            config.attributes["connection"] = connection
            (command.downgrade if downgrade else command.upgrade)(config, revision)
    finally:
        engine.dispose()


def evidence(name, data):
    folder = Path(os.environ["MARKET_NEWS_EVIDENCE_DIR"])
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
