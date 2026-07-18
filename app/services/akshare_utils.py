from __future__ import annotations

import logging
import time
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import os
from typing import Any, Callable

logger = logging.getLogger(__name__)


@contextmanager
def quiet_akshare_output():
    """抑制第三方库的进度输出，避免在 Windows 隐藏工作进程中出错。"""
    with open(os.devnull, "w", encoding="utf-8") as sink:
        with redirect_stdout(sink), redirect_stderr(sink):
            yield


# ----------------------------------------------------------------------------
# HTTP 层加固（P0-3）
# ----------------------------------------------------------------------------
# akshare 底层用 requests/urllib3，默认 User-Agent 为 python-requests/x.y，
# 易被东方财富/新浪等 WAF 拦截；且默认开启 keep-alive 连接池，空闲连接被
# 服务端关闭后复用会触发 RemoteDisconnected。
#
# 通过一次性 monkey-patch requests.Session 的默认 headers，达成：
# 1. 浏览器 UA 伪装 → 降低被 WAF 主动断连的概率
# 2. Connection: close → 每次请求新建 TCP，避免复用死连接（RemoteDisconnected 主因）
#
# 注意：不要注入全局 Referer。akshare 同时访问东方财富、深交所、上交所、新浪
# 等多个数据源，注入 Referer=https://quote.eastmoney.com/ 会被深交所 WAF 拦截
# 返回 403 HTML，导致 stock_info_sz_name_code 内部 pd.read_excel 报
# "Excel file format cannot be determined"（机会挖掘任务失败的根因）。
# 如需针对东方财富的 Referer，应在调用点显式设置，而非全局注入。
#
# 代价：略增 TCP 握手开销，但 akshare 调用频率低，可接受。
# ----------------------------------------------------------------------------

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# 默认 HTTP 超时：(连接超时 5s, 读取超时 15s)
# akshare 内部所有 requests 调用默认 timeout=None 即永久阻塞，当数据源
# (东财/新浪/腾讯/深交所) 接受 TCP 连接但不返回响应体时会导致永久卡死。
# 通过 patch requests.Session.request 注入默认 timeout，从根本上消除卡死。
# 可通过环境变量 AKSHARE_HTTP_TIMEOUT_CONNECT / AKSHARE_HTTP_TIMEOUT_READ 覆盖。
_DEFAULT_TIMEOUT: tuple[float, float] = (
    float(os.environ.get("AKSHARE_HTTP_TIMEOUT_CONNECT", "5")),
    float(os.environ.get("AKSHARE_HTTP_TIMEOUT_READ", "15")),
)

_http_hardened = False


# 东财 kline API 的 fields2 字段修复
# akshare 1.18.30 在 stock_zh_a_hist 的 fields2 中新增了 f116 字段，
# 但东财服务器（push2his.eastmoney.com）拒绝 f51-f61 + f116 的组合，
# 直接 RemoteDisconnected。去掉 f116 后请求正常返回数据。
# 此修复在 HTTP 加固层拦截，只影响该特定 API，未来升级 akshare 后
# 若新版去掉了 f116，此 patch 无副作用（fields2 不匹配条件即跳过）。
_EASTMONEY_KLINE_URL = "push2his.eastmoney.com/api/qt/stock/kline/get"


def _fix_eastmoney_kline_fields2(url: str, kwargs: dict) -> None:
    """修复东财 kline API 的 fields2：去掉被服务器拒绝的 f116 字段。"""
    if _EASTMONEY_KLINE_URL not in url:
        return
    params = kwargs.get("params")
    if not isinstance(params, dict):
        return
    fields2 = params.get("fields2")
    if not isinstance(fields2, str) or "f116" not in fields2:
        return
    # 仅当 fields2 包含其他字段时才去掉 f116（单独请求 f116 不受影响）
    parts = [p for p in fields2.split(",") if p and p != "f116"]
    if len(parts) == 0:
        return  # fields2 只有 f116，不处理
    new_fields2 = ",".join(parts)
    if new_fields2 != fields2:
        params["fields2"] = new_fields2
        logger.debug("eastmoney kline fields2 fixed: removed f116 (%r -> %r)", fields2, new_fields2)


def _harden_requests_session() -> None:
    """一次性给 requests.Session 注入浏览器 UA + Connection: close + 默认 timeout。

    刻意不注入 Referer：不同数据源对 Referer 的要求不同，全局注入 eastmoney
    的 Referer 会被深交所 WAF 拦截（返回 403 HTML），破坏 stock_info_sz_name_code
    等用 pd.read_excel 读取交易所 Excel 的接口。

    注入默认 timeout：akshare 内部 requests 调用默认 timeout=None 永久阻塞，
    当数据源接受连接不返回响应时会永久卡死，导致探测端点耗尽线程池、
    机会挖掘单 symbol 卡死阻塞整个任务。patch request 方法注入默认 (5, 15) 超时。
    """
    global _http_hardened
    if _http_hardened:
        return
    try:
        import requests

        _orig_init = requests.Session.__init__

        def _patched_init(self, *args: Any, **kwargs: Any) -> None:
            _orig_init(self, *args, **kwargs)
            self.headers.update(
                {
                    "User-Agent": _BROWSER_UA,
                    "Connection": "close",
                    "Accept": "*/*",
                }
            )

        requests.Session.__init__ = _patched_init  # type: ignore[assignment]

        # patch request 方法注入默认 timeout（仅当调用方未显式设置时）
        _orig_request = requests.Session.request

        def _patched_request(self, method: str, url: str, **kwargs: Any) -> Any:
            if "timeout" not in kwargs or kwargs["timeout"] is None:
                kwargs["timeout"] = _DEFAULT_TIMEOUT
            _fix_eastmoney_kline_fields2(url, kwargs)
            return _orig_request(self, method, url, **kwargs)

        requests.Session.request = _patched_request  # type: ignore[assignment]

        _http_hardened = True
        logger.info(
            "requests.Session hardened (UA + Connection: close + timeout=%s, no global Referer)",
            _DEFAULT_TIMEOUT,
        )
    except Exception as exc:
        # 风控加固：patch 失败意味着 UA/timeout/Connection:close 全部未生效，所有 akshare 调用将裸奔
        # 易被 WAF 拦截 + 无默认 timeout 永久阻塞。提升日志级别为 error 以便运维发现
        logger.error(
            "Failed to harden requests.Session (UA/timeout/Connection:close NOT applied): %s. "
            "All akshare calls will run without hardening — high risk of WAF block and hang.",
            exc,
        )


# 模块导入时即加固，确保后续所有 akshare 调用都走加固后的 session
_harden_requests_session()


# ----------------------------------------------------------------------------
# 带重试的 akshare 调用封装（P0-1 + 接口管理）
# ----------------------------------------------------------------------------

def call_akshare_with_retry(
    func: Callable[..., Any],
    *args: Any,
    api_key: str | None = None,
    max_attempts: int | None = None,
    base_delay: float = 1.0,
    db: Any = None,
    **kwargs: Any,
) -> Any:
    """调用 akshare 接口，失败时按指数退避重试，并按 api_key 配置应用防风控延时。

    专门处理 RemoteDisconnected / ConnectionError / Timeout 等网络瞬时错误。
    其他异常（如 KeyError、ValueError 表示数据格式问题）不重试，直接抛出。

    Args:
        func: akshare 函数对象（如 ak.stock_zh_a_spot_em）
        *args: 透传给 func 的位置参数
        api_key: 接口标识（与 akshare_registry 中的 key 一致）。
            传入后会：1) 调用前 apply_delay 规避风控；2) 用 registry 配置覆盖 max_attempts；
            3) 记录运行时状态到 DB（若 db 不为 None）。
        max_attempts: 最大尝试次数（含首次）。若为 None 且 api_key 已知，从 registry 读取；否则默认 3。
        base_delay: 首次重试退避基数（秒），后续按 2^n 退避，默认 1.0（→ 1s, 2s, 4s）
        db: 可选的 SQLAlchemy Session，用于记录调用结果到 akshare_api_config 表
        **kwargs: 透传给 func 的关键字参数

    Returns:
        func 的返回值（通常是 DataFrame）

    Raises:
        最后一次重试仍失败时抛出原始异常
    """
    # 解析重试次数：显式参数 > api_key 配置 > 默认 3
    if max_attempts is None:
        if api_key:
            try:
                from app.services.akshare_registry import get_max_retries
                max_attempts = get_max_retries(api_key)
            except Exception:
                # 风控加固：registry 加载失败时静默回退到 3 会让用户在 UI 上配置的"5 次"失效
                # 必须记录日志便于排查（registry 模块缺失/DB 异常）
                logger.warning(
                    "akshare %s: get_max_retries failed, fallback to default 3 attempts",
                    api_key, exc_info=True,
                )
                max_attempts = 3
        else:
            max_attempts = 3

    # 调用前应用防风控延时
    if api_key:
        try:
            from app.services.akshare_registry import apply_delay
            apply_delay(api_key)
        except Exception:
            # 风控加固：apply_delay 失败意味着防风控延时被跳过，akshare 调用立即发出
            # 极易触发东财/新浪 WAF 限流，导致后续大批量调用失败。必须 warning
            logger.warning(
                "akshare %s: apply_delay failed, rate-limit delay SKIPPED — high WAF risk",
                api_key, exc_info=True,
            )

    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            result = func(*args, **kwargs)
            # 记录成功
            if api_key and db is not None:
                try:
                    from app.services.akshare_registry import record_call_result
                    record_call_result(db, api_key, success=True)
                except Exception:
                    # 风控加固：record_call_result 失败会让接口运行时状态面板显示旧数据
                    # 但不应阻塞主调用流程，仅 warning
                    logger.warning(
                        "akshare %s: record_call_result (success) failed, runtime stats will be stale",
                        api_key, exc_info=True,
                    )
            return result
        except Exception as exc:
            last_exc = exc
            # 判断是否为可重试的网络瞬时错误
            exc_name = type(exc).__name__
            exc_msg = str(exc).lower()
            retryable = (
                exc_name in ("ConnectionError", "RemoteDisconnected", "TimeoutError", "ConnectTimeout", "ReadTimeout")
                or "remote" in exc_msg
                or "connection" in exc_msg
                or "timeout" in exc_msg
                or "reset" in exc_msg
                or "broken pipe" in exc_msg
            )
            if not retryable or attempt == max_attempts - 1:
                # 记录失败
                if api_key and db is not None:
                    try:
                        from app.services.akshare_registry import record_call_result
                        record_call_result(db, api_key, success=False, error=f"{type(exc).__name__}: {exc}")
                    except Exception:
                        # 风控加固：失败调用不被记录会让接口健康度永远显示"良好"
                        # 必须记录日志（warning，因为不影响主流程但影响可观测性）
                        logger.warning(
                            "akshare %s: record_call_result (failure) failed, failure stats will be inaccurate",
                            api_key, exc_info=True,
                        )
                raise
            delay = base_delay * (2 ** attempt)
            logger.info(
                "akshare %s failed (attempt %d/%d): %s, retrying in %.1fs",
                getattr(func, "__name__", "unknown"),
                attempt + 1,
                max_attempts,
                exc,
                delay,
            )
            time.sleep(delay)
    # 理论不可达，保险兜底
    if last_exc is not None:
        raise last_exc
