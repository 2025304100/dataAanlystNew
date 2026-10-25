"""Loopback-only full app test runtime. Never imported by production startup."""
import os
import re
from sqlalchemy.engine import make_url

url = make_url(os.environ["DATABASE_URL"])
if url.get_backend_name() != "mysql" or not re.fullmatch(r"codex_combination_[A-Za-z0-9_]+", url.database or ""):
    raise RuntimeError("Explicit isolated MySQL database is required")

from app.main import app
from fastapi import HTTPException
from pydantic import BaseModel
import sitecustomize

class ClockRequest(BaseModel):
    utc: str

@app.post("/__isolated_e2e__/clock", include_in_schema=False)
def move_clock(body: ClockRequest):
    if sitecustomize.CLOCK is None:
        raise HTTPException(409, "Clock boundary was not enabled")
    from datetime import datetime
    stamp = datetime.fromisoformat(body.utc)
    if stamp.year != 2026 or stamp.month != 10:
        raise HTTPException(422, "Only the documented October 2026 test calendar is allowed")
    sitecustomize.CLOCK.move_to(stamp)
    os.environ["COMBINATION_E2E_CLOCK_UTC"] = body.utc
    return {"database": url.database, "utc": body.utc}
