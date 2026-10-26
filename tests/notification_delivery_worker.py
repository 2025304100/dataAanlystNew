"""Explicit isolated DB worker; never falls back to application configuration."""
import json
import os
from pathlib import Path
import re
import tempfile
import time

from sqlalchemy import event
from sqlalchemy.engine import make_url


def main():
    raw = os.environ["NOTIFICATION_ACCEPTANCE_URL"]
    url = make_url(raw)
    if url.get_backend_name() == "mysql":
        assert url.host in ("127.0.0.1","localhost","::1")
        assert re.fullmatch(r"codex_market_[a-z0-9_]{1,48}",url.database or "")
    else:
        assert url.get_backend_name()=="sqlite"
        path=Path(url.database).resolve()
        assert path.is_relative_to(Path(tempfile.gettempdir()).resolve()) and path.name.startswith("qa_test_")
    from app.db.manager import DatabaseManager
    manager=DatabaseManager.get()
    manager.initialize(raw,db_type=url.get_backend_name())
    barrier=os.environ.get("NOTIFICATION_WORKER_BARRIER")
    if barrier:
        waiting=[False]
        @event.listens_for(manager.engine,"after_cursor_execute")
        def synchronize_queue_read(connection,cursor,statement,parameters,context,executemany):
            # Pause real SELECTs only to force both processes to have observed
            # the pending message. No query result or business API is mocked.
            if not waiting[0] and "order by notification_outbox.next_retry_at" in statement.lower():
                waiting[0]=True
                folder=Path(barrier)
                (folder/f"ready-{os.environ['NOTIFICATION_WORKER_ID']}").touch()
                deadline=time.monotonic()+45
                while not (folder/"go").exists():
                    assert time.monotonic()<deadline, "Queue synchronization timed out"
                    time.sleep(0.02)
    try:
        from app.services.notifications.dispatcher import Dispatcher
        print(json.dumps({"processed":Dispatcher().run_once()}),flush=True)
    finally:
        manager.dispose()


if __name__=="__main__":
    main()
