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


def _install_proxy_tunnel() -> None:
    """受限网络下经 HTTP CONNECT 代理隧道连接 baostock 数据端口。

    部分网络丢弃 baostock 私有协议端口（TCP 10030）的直连出站，而本地
    代理上游可达（2026-10-24 实测：直连超时、80 端口正常、代理 CONNECT
    10030 返回 200）。仅当环境变量 ``BAOSTOCK_PROXY=host:port`` 设置时启用，
    未设置时零行为变化；钩子只在本抓取子进程内生效，仅拦截目标端口 10030
    的 connect（先连代理并发送 CONNECT，建立隧道后 send/recv 透明复用）。
    """
    proxy = (os.environ.get("BAOSTOCK_PROXY") or "").strip()
    if not proxy:
        return
    proxy_host, _, proxy_port_text = proxy.partition(":")
    if not proxy_host or not proxy_port_text.isdigit():
        raise SourceDownloadError(
            f"BAOSTOCK_PROXY 配置无效：{proxy!r}（应为 host:port，如 127.0.0.1:7897）"
        )
    proxy_port = int(proxy_port_text)
    original_connect = socket.socket.connect

    def tunneled_connect(self, address):
        try:
            target_port = int(address[1])
        except (TypeError, ValueError, IndexError):
            target_port = -1
        if target_port != 10030:
            return original_connect(self, address)
        original_connect(self, (proxy_host, proxy_port))
        self.settimeout(30)
        target = f"{address[0]}:{address[1]}"
        self.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode("ascii"))
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = self.recv(4096)
            if not chunk:
                raise SourceDownloadError("代理隧道建立失败：代理关闭了连接")
            response += chunk
            if len(response) > 8192:
                raise SourceDownloadError("代理隧道建立失败：代理响应头过长")
        status_line = response.split(b"\r\n", 1)[0].decode("ascii", "ignore")
        if " 200 " not in status_line:
            raise SourceDownloadError(f"代理隧道建立失败：{status_line[:120]}")
        # 隧道已建立；交还阻塞模式，整体时限由父进程预算控制
        self.settimeout(None)

    socket.socket.connect = tunneled_connect


def _capture_worker(directory: Path) -> int:
    # The SDK uses a module-global socket. Keeping it in a child process also
    # bounds its EOF/reconnect loops without changing application networking.
    socket.setdefaulttimeout(8)
    _install_proxy_tunnel()
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
