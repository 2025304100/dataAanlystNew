"""Independent offline dispatcher process; only local DB sockets are permitted."""
from pathlib import Path
import json
import os
import socket
import time

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

url = make_url(os.environ['OFFLINE_QUEUE_URL'])
assert url.drivername.startswith('sqlite') or (url.drivername == 'mysql+pymysql'
    and url.host in ('127.0.0.1', 'localhost') and url.database.startswith('codex_worker_review_'))
original_connect = socket.socket.connect
def database_only(sock, address):
    if url.drivername.startswith('mysql') and address[0] in ('127.0.0.1', 'localhost') and address[1] == (url.port or 3306):
        return original_connect(sock, address)
    raise AssertionError('Offline worker permits database access only')
socket.socket.connect = database_only
socket.socket.connect_ex = database_only

from app.services.notifications import dispatcher as dispatch
from app.services.notifications.base import SendResult

engine = create_engine(url, hide_parameters=True)
dispatch.SessionLocal = lambda: Session(engine, autoflush=False)
calls = []
class Substitute:
    def send_message(self, **kwargs):
        calls.append(kwargs['title'])
        return SendResult(success=True, status_code=200)
    def normalize_error(self, exc):
        return type(exc).__name__, str(exc)
dispatch.get_adapter = lambda kind: Substitute()
original_poll = dispatch.get_pending_outbox
barrier = Path(os.environ['OFFLINE_QUEUE_BARRIER'])
worker_id = os.environ['OFFLINE_WORKER_ID']
def synchronized_poll(db, **kwargs):
    result = original_poll(db, **kwargs)
    (barrier / ('ready-' + worker_id)).write_text(str(len(result)), encoding='utf-8')
    deadline = time.monotonic() + 12
    while not (barrier / 'go').exists():
        assert time.monotonic() < deadline
        time.sleep(0.02)
    return result
dispatch.get_pending_outbox = synchronized_poll
processed = dispatch.Dispatcher().run_once()
engine.dispose()
print(json.dumps({'worker': worker_id, 'processed': processed, 'adapter_substitute_calls': len(calls)}))
