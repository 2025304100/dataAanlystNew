"""External BaoStock I/O boundary. Raw responses are retained before interpretation."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from typing import Callable


class SourceDownloadError(RuntimeError):
    pass


class DownloadCancelled(RuntimeError):
    pass


def _save(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def capture_history(request: dict, directory: Path, progress: Callable[[int, str], None],
                    *, timeout_seconds: float = 120) -> None:
    """One isolated SDK session, bounded even when a remote socket never ends."""
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    _save(directory / "source-request.json", request)
    started = time.monotonic()
    process = None
    try:
        with (directory / "sdk-output.log").open("wb") as log:
            process = subprocess.Popen(
                [sys.executable, "-B", "-m", "app.services.security_status.baostock_source", str(directory)],
                cwd=Path(__file__).resolve().parents[3], stdout=log, stderr=subprocess.STDOUT,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            while True:
                try:
                    capture = json.loads((directory / "capture.json").read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    capture = {}
                queries = capture.get("queries", [])
                progress(len(queries), queries[-1]["key"] if queries else "连接数据来源")
                if process.poll() is not None:
                    # The child may have replaced its final receipt after the
                    # progress read and just before poll observed its exit.
                    try:
                        capture = json.loads((directory / "capture.json").read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        capture = {}
                    if process.returncode != 0 or not capture.get("complete"):
                        raise SourceDownloadError(capture.get("error") or "数据来源连接失败，已保留取得的原始文件，请稍后重试。")
                    return
                if time.monotonic() - started >= timeout_seconds:
                    raise SourceDownloadError("下载等待超时，已停止连接并保留取得的原始文件，请稍后重试。")
                try:
                    process.wait(timeout=.25)
                except subprocess.TimeoutExpired:
                    pass
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def _capture_worker(directory: Path) -> int:
    # The SDK uses a module-global socket. Keeping it in a child process also
    # bounds its EOF/reconnect loops without changing application networking.
    socket.setdefaulttimeout(8)
    request = json.loads((directory / "source-request.json").read_text(encoding="utf-8"))
    capture = {"provider": "baostock", "sdk_version": "0.9.4", "queries": [], "complete": False}
    _save(directory / "capture.json", capture)
    try:
        import baostock as bs
    except ImportError:
        capture["error"] = "未安装历史资料下载依赖，请更新应用依赖后重试。"
        _save(directory / "capture.json", capture)
        return 1

    def collect(key, endpoint, parameters):
        raw = {"fields": [], "rows": [], "error_code": None, "error_msg": ""}
        try:
            response = getattr(bs, endpoint)(**parameters)
            raw["fields"] = response.fields
            while response.error_code == "0" and response.next():
                raw["rows"].append(response.get_row_data())
                if len(raw["rows"]) > (3660 if request.get('history_kind') == 'etf_prices' else 366):
                    raise ValueError("source_row_budget_exceeded")
            raw.update(error_code=response.error_code, error_msg=response.error_msg)
        except Exception as error:
            raw.update(error_code="capture_error", error_msg="来源请求未完成", error_type=type(error).__name__)
        value = {"endpoint": endpoint, "parameters": parameters,
                 "fetched_at": datetime.now(timezone.utc).isoformat(), "source_updated_at": None, "raw": raw}
        path = directory / (key + ".json")
        _save(path, value)
        capture["queries"].append({"key": key, "file": path.name,
                                   "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        _save(directory / "capture.json", capture)

    try:
        login = bs.login()
        capture["login_error_code"] = login.error_code
        capture["login_error_msg"] = login.error_msg
        _save(directory / "capture.json", capture)
        if login.error_code != "0":
            raise SourceDownloadError("历史资料来源暂时无法连接，请稍后重试。")
        dates = {"start_date": request["start_date"], "end_date": request["end_date"]}
        collect("calendar", "query_trade_dates", dates)
        for symbol in request["symbols"]:
            collect("basic_" + symbol["symbol"], "query_stock_basic", {"code": symbol["provider_code"]})
            modes = [('raw', '3'), ('qfq', '2')] if request.get('history_kind') == 'etf_prices' else [('daily', '3')]
            for label, flag in modes:
                collect(label + "_" + symbol["symbol"], "query_history_k_data_plus", {
                    "code": symbol["provider_code"], **dates,
                    "fields": "date,code,open,high,low,close,preclose,volume,amount,adjustflag,turn,tradestatus,isST",
                    "frequency": "d", "adjustflag": flag})
        capture["complete"] = True
        _save(directory / "capture.json", capture)
        return 0
    except Exception as error:
        capture["error"] = (str(error) if isinstance(error, SourceDownloadError)
                            else "历史资料来源连接中断，已保留取得的原始文件，请稍后重试。")
        capture["error_type"] = type(error).__name__
        _save(directory / "capture.json", capture)
        return 1
    finally:
        try:
            bs.logout()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(_capture_worker(Path(sys.argv[1])))
