"""Opt-in clock boundary shared by the isolated API and its real workers."""
import os
import re
from urllib.parse import urlsplit

CLOCK = None
if os.environ.get("COMBINATION_E2E_CLOCK_UTC"):
    database = urlsplit(os.environ.get("DATABASE_URL", "")).path.lstrip("/")
    if not re.fullmatch(r"codex_combination_[a-zA-Z0-9_]+", database):
        raise RuntimeError("The E2E clock requires an explicit isolated MySQL database")
    # Define FastAPI/Pydantic models against real datetime classes before
    # installing the test clock (Pydantic v1 has a date metaclass).
    import app.main
    # FastAPI lazily builds adapters during OpenAPI generation. Cache the
    # public contract against real datetime classes before freezing clocks.
    app.main.app.openapi()
    from freezegun import freeze_time
    _freezer = freeze_time(os.environ["COMBINATION_E2E_CLOCK_UTC"], tick=True, real_asyncio=True)
    CLOCK = _freezer.start()
