"""渠道适配器基类（WP-MSG.2）。

所有渠道适配器统一实现：
- validate_config：校验配置
- send_test：发送测试消息
- send_message：发送实际消息
- normalize_error：归一化错误
- mask_config：脱敏配置

project_memory 硬约束（spec line 261）：
- 适配器统一使用有限超时、重试、熔断
- 不允许第三方 SDK 建立无法取消的永久线程

实现策略：
- 超时：默认 10 秒，可由 config["timeout"] 覆盖
- 重试：默认 3 次，指数退避（base * 2^attempt），最大 30 秒
- 熔断：连续失败 N 次后短期熔断（默认 5 次失败 → 熔断 60 秒）
- 全部同步调用 httpx/smtplib，不建立后台线程
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable

from app.schemas.error_sanitizer import sanitize_message


@dataclass
class SendResult:
    """发送结果。

    response_summary / error_message 均已脱敏，不暴露完整 Token/Secret/密码。
    """
    success: bool
    status_code: int | None = None
    response_summary: str | None = None  # 脱敏后的响应摘要（最多 500 字符）
    error_code: str | None = None
    error_message: str | None = None  # 脱敏后的错误消息
    duration_ms: int = 0


@dataclass
class CircuitBreakerState:
    """熔断器状态（每个适配器实例独立）。

    - closed：正常调用
    - open：连续失败达 threshold 后熔断，拒绝请求 reset_after 秒
    - half_open：reset_after 到期后允许单次试探，成功则 closed，失败则继续 open
    """
    failure_count: int = 0
    threshold: int = 5
    reset_after_seconds: float = 60.0
    opened_at: float = 0.0  # time.monotonic() 时间戳
    state: str = "closed"  # closed / open / half_open

    def record_success(self) -> None:
        self.failure_count = 0
        self.state = "closed"
        self.opened_at = 0.0

    def record_failure(self) -> None:
        self.failure_count += 1
        if self.failure_count >= self.threshold:
            self.state = "open"
            self.opened_at = time.monotonic()

    def allow_request(self) -> bool:
        """是否允许请求通过（True=允许，False=熔断中）。"""
        if self.state == "closed":
            return True
        if self.state == "open":
            if time.monotonic() - self.opened_at >= self.reset_after_seconds:
                # 进入 half_open，允许单次试探
                self.state = "half_open"
                return True
            return False
        # half_open：上次 allow_request 已经放过一次，这里仍允许试探
        return True


class CircuitBreakerOpenError(Exception):
    """熔断器开启时抛出。"""


class ChannelAdapter(ABC):
    """渠道适配器基类。

    所有渠道适配器必须实现：
    - validate_config(config: dict) -> list[str]：返回校验错误列表（空表示通过）
    - send_test(config: dict) -> SendResult
    - send_message(config, title, body, body_text=None, payload=None) -> SendResult
    - mask_config(config: dict) -> dict：返回脱敏后的配置

    统一约束：
    - 超时：默认 10 秒，可由 config["timeout"] 覆盖（最大 60 秒）
    - 重试：默认 3 次，指数退避
    - 熔断：连续失败 N 次后短期熔断
    - 不建立永久线程（同步调用第三方接口）
    """

    channel_type: str = "base"
    default_timeout: float = 10.0
    max_timeout: float = 60.0
    max_retries: int = 3
    retry_base_delay: float = 0.5  # 第一段重试基础延迟（秒）

    def __init__(self) -> None:
        # 每个适配器实例独立的熔断器
        self._breaker = CircuitBreakerState()

    # ------------------------------------------------------------------
    # 子类必须实现的接口
    # ------------------------------------------------------------------

    @abstractmethod
    def validate_config(self, config: dict) -> list[str]:
        """校验配置，返回错误列表（空表示通过）。"""
        ...

    @abstractmethod
    def send_test(self, config: dict) -> SendResult:
        """发送测试消息。"""
        ...

    @abstractmethod
    def send_message(
        self,
        config: dict,
        title: str,
        body: str,
        body_text: str | None = None,
        payload: dict | None = None,
    ) -> SendResult:
        """发送实际消息。"""
        ...

    @abstractmethod
    def mask_config(self, config: dict) -> dict:
        """返回脱敏后的配置（用于前端展示）。"""
        ...

    # ------------------------------------------------------------------
    # 通用工具方法
    # ------------------------------------------------------------------

    def normalize_error(self, exc: Exception) -> tuple[str, str]:
        """归一化错误为 (error_code, error_message)。

        默认实现：error_code 取异常类名，error_message 走 sanitize_message 脱敏。
        子类可覆盖以提供更具体的错误码。
        """
        code = type(exc).__name__
        # 错误消息必须脱敏，不暴露完整 Token/Webhook Secret/SMTP 密码
        msg = sanitize_message(str(exc))
        # 进一步：若消息中仍疑似包含 token/secret，统一替换为通用提示
        lower_msg = msg.lower()
        if any(kw in lower_msg for kw in ("token", "secret", "password", "apikey", "api_key")):
            # 仅当原始消息明显包含完整敏感字段时降级为通用提示
            # sanitize_message 已将 password=xxx / token=xxx 等替换为 ***，
            # 若仍出现这些关键词但已被替换为 ***，保留替换后的消息即可
            if "***" in msg:
                # 已被 sanitize_message 处理过，保留
                pass
            else:
                msg = "认证失败（已脱敏）"
        return code, msg[:500]

    def _truncate_response(self, response: str, max_len: int = 500) -> str:
        """截断响应摘要，最多保留 max_len 字符。"""
        if response is None:
            return ""
        if not isinstance(response, str):
            response = str(response)
        if len(response) > max_len:
            return response[:max_len] + "..."
        return response

    def _resolve_timeout(self, config: dict) -> float:
        """从 config 解析超时秒数。

        优先使用 config["timeout"]，否则使用 default_timeout。
        上限为 max_timeout，避免配置错误导致永久阻塞。
        """
        try:
            t = float(config.get("timeout", self.default_timeout))
        except (TypeError, ValueError):
            t = self.default_timeout
        # 限定在 (0, max_timeout] 区间
        if t <= 0:
            t = self.default_timeout
        return min(t, self.max_timeout)

    def _check_breaker(self) -> None:
        """发送前检查熔断器，熔断中抛 CircuitBreakerOpenError。"""
        if not self._breaker.allow_request():
            raise CircuitBreakerOpenError(
                f"渠道 {self.channel_type} 熔断中（连续失败 "
                f"{self._breaker.failure_count} 次），稍后重试"
            )

    def _record_send_result(self, result: SendResult) -> None:
        """根据发送结果更新熔断器状态。"""
        if result.success:
            self._breaker.record_success()
        else:
            self._breaker.record_failure()

    def _retry_send(
        self,
        sender: Callable[[], SendResult],
        config: dict,
    ) -> SendResult:
        """通用重试封装。

        - 检查熔断器（熔断中直接返回失败结果）
        - 调用 sender 发送，失败时按指数退避重试 max_retries 次
        - 成功/失败都更新熔断器状态
        - 不建立后台线程，重试在当前线程同步进行
        """
        # 熔断器检查
        try:
            self._check_breaker()
        except CircuitBreakerOpenError as exc:
            code, msg = self.normalize_error(exc)
            return SendResult(
                success=False,
                error_code=code,
                error_message=msg,
                duration_ms=0,
            )

        start = time.monotonic()
        last_result: SendResult | None = None
        retries = int(config.get("max_retries", self.max_retries))
        # 限制最大重试次数，防止恶意配置导致长时间阻塞
        retries = max(0, min(retries, 5))

        for attempt in range(retries + 1):
            try:
                result = sender()
            except Exception as exc:
                code, msg = self.normalize_error(exc)
                result = SendResult(
                    success=False,
                    error_code=code,
                    error_message=msg,
                    duration_ms=int((time.monotonic() - start) * 1000),
                )

            if result.success:
                result.duration_ms = int((time.monotonic() - start) * 1000)
                self._breaker.record_success()
                return result

            last_result = result
            # 失败也记录熔断器
            self._breaker.record_failure()

            # 最后一次不再 sleep
            if attempt >= retries:
                break
            # 指数退避：base * 2^attempt，最大 30 秒
            delay = min(self.retry_base_delay * (2 ** attempt), 30.0)
            time.sleep(delay)

        # 全部重试失败
        if last_result is None:
            last_result = SendResult(
                success=False,
                error_code="UNKNOWN",
                error_message="发送失败（无详细错误）",
                duration_ms=int((time.monotonic() - start) * 1000),
            )
        else:
            last_result.duration_ms = int((time.monotonic() - start) * 1000)
        return last_result


__all__ = [
    "ChannelAdapter",
    "SendResult",
    "CircuitBreakerState",
    "CircuitBreakerOpenError",
]
