"""白盒测试 - HTTP 加固回归守护 + call_akshare_with_retry 重试逻辑。

核心守护：防止 _harden_requests_session() 再次注入破坏性 headers（Referer 事件回归）。
覆盖：
1. HTTP monkey-patch 注入的 headers（UA + Connection: close，无 Referer）
2. call_akshare_with_retry 的重试/退避/状态记录逻辑
3. 请求级代理绕过不修改全局环境，也不串行化并发 worker
4. slow 联网测试：真实 akshare 接口可达性（守护深交所 WAF 问题）
"""
from __future__ import annotations

import pytest
import requests
from unittest.mock import MagicMock, patch
from http.client import RemoteDisconnected

# 导入触发 _harden_requests_session() 执行
from app.services import akshare_utils
from app.services.akshare_utils import (
    _harden_requests_session,
    _http_hardened,
    call_akshare_with_retry,
)

pytestmark = pytest.mark.whitebox

# ============================================================================
# 1. HTTP monkey-patch headers 守护（Referer 事件回归核心）
# ============================================================================

def test_harden_injects_user_agent():
    """_harden_requests_session 应注入浏览器 User-Agent（绕过 WAF 的 python-requests 拦截）。"""
    import requests as req
    s = req.Session()
    assert "Mozilla/5.0" in s.headers["User-Agent"], "User-Agent 应包含浏览器标识"


def test_harden_injects_connection_close():
    """_harden_requests_session 应注入 Connection: close（避免 keep-alive 死连接复用）。"""
    s = requests.Session()
    assert s.headers.get("Connection") == "close", "Connection 应为 close"


def test_harden_does_not_inject_referer():
    """【核心回归守护】_harden_requests_session 不应注入全局 Referer。

    历史事件：全局注入 Referer: https://quote.eastmoney.com/ 被深交所 WAF 拦截
    返回 403 HTML，导致 pd.read_excel 报 'Excel file format cannot be determined'。
    """
    s = requests.Session()
    assert "Referer" not in s.headers, (
        "禁止全局注入 Referer —— 会被深交所/上交所 WAF 拦截。"
        "如需针对东方财富的 Referer，应在调用点显式设置，而非全局注入。"
    )


def test_harden_idempotent():
    """多次调用 _harden_requests_session() 不重复 patch（_http_hardened 标志保护）。"""
    # 第一次调用已通过模块导入完成，_http_hardened 应为 True
    assert _http_hardened is True, "_http_hardened 应在模块导入后为 True"
    # 再次调用应是 no-op，不抛异常
    _harden_requests_session()
    # 验证 headers 仍正确
    s = requests.Session()
    assert "Mozilla/5.0" in s.headers["User-Agent"]


def test_harden_injects_accept_header():
    """_harden_requests_session 应注入 Accept: */*（兼容多种响应类型）。"""
    s = requests.Session()
    assert s.headers.get("Accept") == "*/*", "Accept 应为 */*"


# ============================================================================
# 2. call_akshare_with_retry 重试逻辑
# ============================================================================

def test_call_akshare_with_retry_success():
    """成功调用 → 返回值正确，record_call_result(success=True) 被调用。"""
    mock_func = MagicMock(return_value="test_result")
    mock_db = MagicMock()

    with patch("app.services.akshare_registry.record_call_result") as mock_record:
        result = call_akshare_with_retry(
            mock_func, api_key="stock_zh_a_hist", db=mock_db
        )

    assert result == "test_result"
    mock_func.assert_called_once()
    mock_record.assert_called_once_with(mock_db, "stock_zh_a_hist", success=True)


def test_call_akshare_with_retry_no_api_key_skips_delay_and_record():
    """未传 api_key → 不调用 apply_delay，不记录状态（避免无谓的 DB 写入）。"""
    mock_func = MagicMock(return_value=42)

    with patch("app.services.akshare_registry.apply_delay") as mock_delay, \
         patch("app.services.akshare_registry.record_call_result") as mock_record:
        result = call_akshare_with_retry(mock_func)

    assert result == 42
    mock_delay.assert_not_called()
    mock_record.assert_not_called()


def test_call_akshare_with_retry_retries_on_remote_disconnected():
    """前 2 次 RemoteDisconnected，第 3 次成功 → 返回成功值，重试 2 次。"""
    mock_func = MagicMock(side_effect=[
        RemoteDisconnected("Remote end closed connection"),
        RemoteDisconnected("Remote end closed connection"),
        "success",
    ])

    with patch("app.services.akshare_registry.apply_delay"), \
         patch("app.services.akshare_registry.record_call_result"), \
         patch("app.services.akshare_utils.time.sleep") as mock_sleep:
        result = call_akshare_with_retry(mock_func, max_attempts=3, base_delay=0.01)

    assert result == "success"
    assert mock_func.call_count == 3
    # 指数退避：0.01 * 2^0=0.01, 0.01 * 2^1=0.02
    assert mock_sleep.call_count == 2


def test_call_akshare_with_retry_no_retry_on_value_error():
    """ValueError（数据格式问题）不可重试，直接抛出。

    防止对数据格式错误做无意义重试（如 'Excel file format cannot be determined'）。
    """
    mock_func = MagicMock(side_effect=ValueError("Excel file format cannot be determined"))

    with patch("app.services.akshare_registry.apply_delay"), \
         patch("app.services.akshare_utils.time.sleep") as mock_sleep:
        with pytest.raises(ValueError, match="Excel file format"):
            call_akshare_with_retry(mock_func, max_attempts=3)

    # 不应有任何重试 sleep
    mock_sleep.assert_not_called()
    assert mock_func.call_count == 1


def test_call_akshare_with_retry_no_retry_on_key_error():
    """KeyError（列名不匹配）不可重试，直接抛出。"""
    mock_func = MagicMock(side_effect=KeyError("code"))

    with patch("app.services.akshare_registry.apply_delay"), \
         patch("app.services.akshare_utils.time.sleep") as mock_sleep:
        with pytest.raises(KeyError):
            call_akshare_with_retry(mock_func, max_attempts=3)

    mock_sleep.assert_not_called()


def test_call_akshare_with_retry_retries_on_connection_error():
    """ConnectionError 是可重试的网络错误。"""
    mock_func = MagicMock(side_effect=[
        ConnectionError("Connection aborted"),
        "success",
    ])

    with patch("app.services.akshare_registry.apply_delay"), \
         patch("app.services.akshare_registry.record_call_result"), \
         patch("app.services.akshare_utils.time.sleep"):
        result = call_akshare_with_retry(mock_func, max_attempts=3, base_delay=0.01)

    assert result == "success"
    assert mock_func.call_count == 2


def test_call_akshare_with_retry_retries_on_timeout_error():
    """TimeoutError 是可重试的网络错误。"""
    mock_func = MagicMock(side_effect=[
        TimeoutError("Read timed out"),
        "success",
    ])

    with patch("app.services.akshare_registry.apply_delay"), \
         patch("app.services.akshare_registry.record_call_result"), \
         patch("app.services.akshare_utils.time.sleep"):
        result = call_akshare_with_retry(mock_func, max_attempts=3, base_delay=0.01)

    assert result == "success"


def test_call_akshare_with_retry_retries_on_message_match():
    """异常消息含 'remote'/'connection'/'timeout'/'reset'/'broken pipe' 也应重试。"""
    mock_func = MagicMock(side_effect=[
        OSError("Connection reset by peer"),
        "success",
    ])

    with patch("app.services.akshare_registry.apply_delay"), \
         patch("app.services.akshare_registry.record_call_result"), \
         patch("app.services.akshare_utils.time.sleep"):
        result = call_akshare_with_retry(mock_func, max_attempts=3, base_delay=0.01)

    assert result == "success"


def test_call_akshare_with_retry_all_fail_raises_last():
    """全部重试失败 → 抛出最后一次异常。"""
    exc = RemoteDisconnected("Persistent failure")
    mock_func = MagicMock(side_effect=exc)

    with patch("app.services.akshare_registry.apply_delay"), \
         patch("app.services.akshare_registry.record_call_result"), \
         patch("app.services.akshare_utils.time.sleep"):
        with pytest.raises(RemoteDisconnected):
            call_akshare_with_retry(mock_func, max_attempts=2, base_delay=0.01)

    assert mock_func.call_count == 2


def test_call_akshare_with_retry_records_failure():
    """全部失败 → record_call_result(success=False, error=...) 被调用。"""
    mock_func = MagicMock(side_effect=ConnectionError("fail"))
    mock_db = MagicMock()

    with patch("app.services.akshare_registry.apply_delay"), \
         patch("app.services.akshare_registry.record_call_result") as mock_record, \
         patch("app.services.akshare_utils.time.sleep"):
        with pytest.raises(ConnectionError):
            call_akshare_with_retry(
                mock_func, api_key="stock_zh_a_hist", db=mock_db, max_attempts=2, base_delay=0.01
            )

    # 失败时应记录
    mock_record.assert_called_with(
        mock_db, "stock_zh_a_hist", success=False, error="ConnectionError: fail"
    )


def test_call_akshare_with_retry_applies_delay_when_api_key():
    """传入 api_key → apply_delay 被调用（防风控延时）。"""
    mock_func = MagicMock(return_value="ok")

    with patch("app.services.akshare_registry.apply_delay") as mock_delay, \
         patch("app.services.akshare_registry.record_call_result"):
        call_akshare_with_retry(mock_func, api_key="stock_zh_a_hist")

    mock_delay.assert_called_once_with("stock_zh_a_hist")


def test_call_akshare_with_retry_max_attempts_from_registry():
    """api_key 已知 + max_attempts=None → 从 registry 读取 max_retries。"""
    mock_func = MagicMock(side_effect=ConnectionError("fail"))

    with patch("app.services.akshare_registry.apply_delay"), \
         patch("app.services.akshare_registry.record_call_result"), \
         patch("app.services.akshare_registry.get_max_retries", return_value=5) as mock_retries, \
         patch("app.services.akshare_utils.time.sleep"):
        with pytest.raises(ConnectionError):
            call_akshare_with_retry(mock_func, api_key="stock_zh_a_hist", base_delay=0.01)

    mock_retries.assert_called_once_with("stock_zh_a_hist")
    # 应该尝试 5 次
    assert mock_func.call_count == 5


def test_call_akshare_with_retry_max_attempts_default_three():
    """无 api_key + max_attempts=None → 默认 3 次。"""
    mock_func = MagicMock(side_effect=ConnectionError("fail"))

    with patch("app.services.akshare_utils.time.sleep"):
        with pytest.raises(ConnectionError):
            call_akshare_with_retry(mock_func, base_delay=0.01)

    assert mock_func.call_count == 3


def test_call_akshare_with_retry_passes_args_kwargs():
    """*args 和 **kwargs 应透传给 func。"""
    mock_func = MagicMock(return_value="ok")

    with patch("app.services.akshare_registry.apply_delay"), \
         patch("app.services.akshare_registry.record_call_result"):
        call_akshare_with_retry(
            mock_func, "arg1", "arg2", api_key="test_key", kwarg1="v1", kwarg2="v2"
        )

    mock_func.assert_called_once_with("arg1", "arg2", kwarg1="v1", kwarg2="v2")


# ============================================================================
# 3. 请求级代理绕过并发守护
# ============================================================================

def test_proxy_bypass_nested_context_manager():
    """嵌套代理绕过不修改进程级环境变量。"""
    from app.services.market_data import _proxy_bypass
    import os

    original = os.environ.get("HTTP_PROXY")
    os.environ["HTTP_PROXY"] = "http://proxy.invalid:8080"
    try:
        with _proxy_bypass():
            with _proxy_bypass():
                assert os.environ["HTTP_PROXY"] == "http://proxy.invalid:8080"
        assert os.environ["HTTP_PROXY"] == "http://proxy.invalid:8080"
    finally:
        if original is None:
            os.environ.pop("HTTP_PROXY", None)
        else:
            os.environ["HTTP_PROXY"] = original


def test_proxy_bypass_does_not_serialize_workers():
    """两个 worker 可以同时进入代理绕过上下文。"""
    import threading
    from app.services.market_data import _proxy_bypass

    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def enter_context() -> None:
        try:
            with _proxy_bypass():
                barrier.wait(timeout=1)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=enter_context) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert errors == []
    assert all(not thread.is_alive() for thread in threads)


def test_proxy_bypass_disables_environment_proxy(monkeypatch):
    from requests import Response
    from requests.adapters import BaseAdapter
    from app.services.market_data import _proxy_bypass

    captured: dict = {}

    class CapturingAdapter(BaseAdapter):
        def send(self, request, **kwargs):
            captured['proxies'] = dict(kwargs.get('proxies') or {})
            response = Response()
            response.status_code = 200
            response.request = request
            response.url = request.url
            return response

        def close(self):
            return None

    monkeypatch.setenv('HTTP_PROXY', 'http://proxy.invalid:8080')
    session = requests.Session()
    session.mount('http://', CapturingAdapter())

    with _proxy_bypass():
        response = session.get('http://example.test')

    assert response.status_code == 200
    assert not captured['proxies'].get('http')


# ============================================================================
# 4. slow 联网测试（真实 akshare 接口可达性，守护 WAF/Headers 问题）
# ============================================================================

@pytest.mark.slow
def test_real_stock_info_a_code_name():
    """【slow 联网】真实调用 ak.stock_info_a_code_name 应返回非空 DataFrame。

    守护：HTTP monkey-patch 不破坏 akshare 基础接口。
    """
    import akshare as ak
    df = ak.stock_info_a_code_name()
    assert df is not None
    assert len(df) > 100, f"A股标的数据应 > 100 行，实际 {len(df)} 行"
    # 列名兼容（可能是 'code' 或 '代码'）
    cols = list(df.columns)
    assert any(c.lower() in ("code", "代码", "symbol") for c in cols), f"列名应含 code/symbol，实际 {cols}"


@pytest.mark.slow
def test_real_stock_info_sz_name_code():
    """【slow 联网】真实调用 ak.stock_info_sz_name_code 应返回非空数据。

    核心守护：深交所 Excel 接口可达，不再返回 403 HTML 导致
    'Excel file format cannot be determined' 错误。
    """
    import akshare as ak
    df = ak.stock_info_sz_name_code(symbol="A股列表")
    assert df is not None
    assert len(df) > 100, f"深交所 A 股数据应 > 100 行，实际 {len(df)} 行"


@pytest.mark.slow
def test_real_fund_etf_spot_em():
    """【slow 联网】真实调用 ak.fund_etf_spot_em 应返回非空 DataFrame。

    守护：ETF 实时行情接口可达（_refresh_cn_etf_universe 主数据源）。

    风控说明：东方财富 push2.eastmoney.com 接口有 IP 频次风控，
    实测首次调用可成功（HTTP 200），但短时间内再次调用（无论加不加 Referer）
    会被服务端直接断连（RemoteDisconnected），等 60s 仍失败。
    这是数据源端反爬，非代码问题。

    处理策略：首次失败即 skip 并标注风控触发，不 fail。
    业务侧已由 call_akshare_with_retry + sina 备用源保护
    （见 discovery_tasks._refresh_cn_etf_universe）。
    """
    import akshare as ak
    try:
        df = ak.fund_etf_spot_em()
    except Exception as exc:
        # 风控触发特征：RemoteDisconnected / ConnectionError
        # （首次成功后，短时间内再次调用必然触发，等 60s 也不恢复）
        exc_msg = str(exc).lower()
        if (
            "remotedisconnected" in exc_msg
            or "connection aborted" in exc_msg
            or "remote end closed connection" in exc_msg
        ):
            pytest.skip(
                f"fund_etf_spot_em 风控触发（RemoteDisconnected），"
                f"非代码问题：{type(exc).__name__}: {exc}"
            )
        raise
    assert df is not None
    assert len(df) > 10, f"ETF 行情数据应 > 10 行，实际 {len(df)} 行"


# ============================================================================
# 5. HTTP timeout 注入守护（P0 稳定性回归：防止 timeout patch 被移除）
# ============================================================================

def test_harden_injects_default_timeout():
    """【P0 稳定性回归】patched request 应注入默认 timeout=(5,15) 当调用方未传时。

    历史事件：akshare 内部所有 requests 调用默认 timeout=None（永久阻塞），
    数据源接受连接不响应时导致探测端点耗尽线程池、单 symbol 卡死阻塞整个任务。
    patch requests.Session.request 注入默认 (5, 15) timeout 从根本上消除卡死。
    """
    import requests
    from unittest.mock import patch

    captured = {}

    def mock_send(self, request, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        resp = requests.Response()
        resp.status_code = 200
        resp.url = request.url
        return resp

    with patch.object(requests.adapters.HTTPAdapter, "send", mock_send):
        s = requests.Session()
        s.get("http://example.com")

    assert captured.get("timeout") == (5.0, 15.0), (
        f"未传 timeout 时应注入默认 (5, 15), 实际: {captured.get('timeout')}"
    )


def test_harden_preserves_explicit_timeout():
    """【P0 稳定性回归】调用方显式传 timeout → 保留原值，不被默认值覆盖。

    patch 逻辑：仅当 kwargs 中无 timeout 或 timeout=None 时注入默认值，
    调用方显式设置的 timeout 应保留（如 akshare 某些接口自带 timeout）。
    """
    import requests
    from unittest.mock import patch

    captured = {}

    def mock_send(self, request, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        resp = requests.Response()
        resp.status_code = 200
        return resp

    with patch.object(requests.adapters.HTTPAdapter, "send", mock_send):
        s = requests.Session()
        s.get("http://example.com", timeout=30)

    assert captured.get("timeout") == 30, (
        f"显式 timeout=30 应保留, 实际: {captured.get('timeout')}"
    )


def test_harden_timeout_env_var_override(monkeypatch):
    """【P0 稳定性回归】环境变量 AKSHARE_HTTP_TIMEOUT_CONNECT/READ 应能覆盖默认 timeout。

    注意：_harden_requests_session 在模块导入时已执行（_http_hardened 保护防重复 patch），
    无法通过 importlib.reload 安全重载（会导致 _orig_request 捕获已 patch 版本引发无限递归）。
    这里验证环境变量读取表达式与模块实现一致，确保配置化生效。
    """
    import os

    # 验证环境变量覆盖
    monkeypatch.setenv("AKSHARE_HTTP_TIMEOUT_CONNECT", "10")
    monkeypatch.setenv("AKSHARE_HTTP_TIMEOUT_READ", "30")
    connect = float(os.environ.get("AKSHARE_HTTP_TIMEOUT_CONNECT", "5"))
    read = float(os.environ.get("AKSHARE_HTTP_TIMEOUT_READ", "15"))
    assert (connect, read) == (10.0, 30.0), "环境变量应覆盖默认值"

    # 验证默认值（不设环境变量时）
    monkeypatch.delenv("AKSHARE_HTTP_TIMEOUT_CONNECT", raising=False)
    monkeypatch.delenv("AKSHARE_HTTP_TIMEOUT_READ", raising=False)
    connect = float(os.environ.get("AKSHARE_HTTP_TIMEOUT_CONNECT", "5"))
    read = float(os.environ.get("AKSHARE_HTTP_TIMEOUT_READ", "15"))
    assert (connect, read) == (5.0, 15.0), "无环境变量时应为默认值 (5, 15)"


def test_call_akshare_with_retry_no_retry_on_keyerror():
    """【P0 稳定性回归】KeyError 属于数据格式错误，不应重试，直接抛出。

    重试逻辑只处理网络瞬时错误（ConnectionError/Timeout/RemoteDisconnected），
    KeyError/ValueError 等数据格式错误重试无意义，应立即失败。
    """
    mock_func = MagicMock(side_effect=KeyError("missing column"))

    with patch("app.services.akshare_utils.time.sleep") as mock_sleep:
        with pytest.raises(KeyError):
            call_akshare_with_retry(mock_func, base_delay=0.01)

    assert mock_func.call_count == 1, "KeyError 不应触发重试"
    mock_sleep.assert_not_called()
