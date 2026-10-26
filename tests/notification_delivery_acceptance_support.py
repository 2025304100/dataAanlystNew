"""Real loopback receiver and synchronized worker processes for delivery tests."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from threading import Lock, Thread
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import time

from app.models.notification import NotificationChannel
from app.services.notifications.outbox import enqueue
from app.utils.secret_mask import encrypt_config

ROOT = Path(__file__).resolve().parents[1]


class LocalReceiver:
    def __init__(self, *, close_first=False):
        self.receipts, self.effects = [], []
        self.close_first = close_first
        self.lock = Lock()

    def __enter__(self):
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                key = self.headers.get("Idempotency-Key")
                with owner.lock:
                    owner.receipts.append(dict(key=key, body=body))
                    if not key or not any(item["key"] == key for item in owner.effects):
                        owner.effects.append(dict(key=key, body=body))
                    disconnect = owner.close_first and len(owner.receipts) == 1
                if disconnect:
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"accepted":true}')
            def log_message(self, *args):
                pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/acceptance"
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


def seed_delivery(db, receiver, *, name="delivery", retries=0, max_attempts=5):
    channel = NotificationChannel(name=name, channel_type="webhook", enabled=True, status="enabled",
        config_encrypted_json=encrypt_config(json.dumps({"url":receiver.url,"timeout":3,"max_retries":retries})))
    db.add(channel)
    db.commit()
    outbox = enqueue(db, event_key=f"market_news_collect_failed:{name}", source_type="system", source_id=None,
        event_type="market_news_collect_failed", severity="warn", payload={"title":"隔离采集失败","body":"受控投递验收"},
        channel_id=channel.id)
    outbox.max_attempts = max_attempts
    db.commit()
    return outbox


def run_workers(engine, folder, *, synchronized=False, count=2):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=False)
    env = dict(os.environ, NOTIFICATION_ACCEPTANCE_URL=engine.url.render_as_string(hide_password=False),
        NOTIFICATION_WORKER_BARRIER=str(folder) if synchronized else "", DATABASE_URL="sqlite:///:memory:",
        PYTHONIOENCODING="utf-8", NO_PROXY="127.0.0.1,localhost", AUTO_UNIVERSE_SYNC_ON_STARTUP="0",
        AUTO_TRADE_CALENDAR_REFRESH="0", FACTOR_WAREHOUSE_PATH=str(folder / "isolated.duckdb"))
    processes = []
    try:
        for index in range(count):
            processes.append(subprocess.Popen([sys.executable,"-m","tests.notification_delivery_worker"],
                cwd=ROOT,env=dict(env,NOTIFICATION_WORKER_ID=str(index)),stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                text=True,encoding="utf-8",creationflags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0))
        if synchronized:
            deadline = time.monotonic()+45
            while len(list(folder.glob("ready-*"))) < count:
                assert not any(process.poll() is not None for process in processes), "Worker exited before queue synchronization"
                assert time.monotonic()<deadline, "Workers did not both read the pending queue"
                time.sleep(0.02)
            (folder/"go").touch()
        results=[]
        for process in processes:
            stdout,stderr=process.communicate(timeout=45)
            assert process.returncode==0, stderr[-1500:]
            results.append(json.loads(stdout.strip().splitlines()[-1]))
        return results
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
