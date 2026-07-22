"""外部数据网关的 Pydantic schema 与异常定义（WP-S）。

包含：
- GatewayResponse：统一返回结构（data/source/cache_hit/degraded_reason/correlation_id 等）
- DataValidationError：入库字段契约校验失败
- StaleDataError：allow_stale=False 时数据过期
- CircuitBreakerOpenError：熔断器打开，拒绝请求
"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

if TYPE_CHECKING:
    from app.schemas.errors import UserError


# 数据源标识常量（与 GatewayResponse.source 字段值一致）
SOURCE_L1_CACHE = "l1_cache"
SOURCE_L2_DB = "l2_db"
SOURCE_L3_DUCKDB = "l3_duckdb"
SOURCE_L4_REMOTE = "l4_remote"
SOURCE_STALE = "stale"  # L4 失败且本地有过期数据


class GatewayResponse(BaseModel):
    """网关统一返回结构。

    Attributes:
        data: 实际数据（pandas DataFrame / dict / list 等）
        source: 实际数据源：
            - "l1_cache"：进程缓存命中
            - "l2_db"：业务数据库命中
            - "l3_duckdb"：DuckDB 快照命中
            - "l4_remote"：第三方接口实时拉取
            - "stale"：远程失败且降级到本地过期数据
        cache_hit: 是否命中任何缓存层（L1/L2/L3）
        data_cutoff_at: 数据截止时间（缓存层为最新记录时间，L4 为拉取时间）
        degraded_reason: 降级原因（None 表示未降级，正常命中或远程拉取）
        correlation_id: 调用追踪 ID（uuid4.hex），用于日志关联
        source_detail: 远程拉取时的具体数据源标识（如 "em_stock"/"sina_stock"）
    """

    data: Any = None
    source: str
    cache_hit: bool = False
    data_cutoff_at: datetime | None = None
    degraded_reason: str | None = None
    correlation_id: str
    source_detail: str | None = None

    model_config = {"arbitrary_types_allowed": True}


class DataValidationError(Exception):
    """入库前字段契约/日期/数值范围校验失败。

    Attributes:
        field: 出错字段名
        reason: 详细原因（英文，用于日志）
    """

    def __init__(self, field: str, reason: str) -> None:
        self.field = field
        self.reason = reason
        super().__init__(f"DataValidation failed [{field}]: {reason}")

    def to_unified_error(self) -> "UserError":
        """转换为统一用户错误（WP-S.6）。

        映射到 `error_code="DATA_VALIDATION_FAILED"`，`status_code=422`。
        脱敏后的 `field`/`reason` 写入 `technical_details`。
        """
        from app.schemas.errors import build_user_error, TechnicalDetails
        from app.schemas.error_sanitizer import sanitize_message

        return build_user_error(
            "DATA_VALIDATION_FAILED",
            technical_details=TechnicalDetails(
                exception_type=type(self).__name__,
                status_code=422,
                error_message=sanitize_message(f"field={self.field}; reason={self.reason}")[:500],
            ),
        )


class StaleDataError(Exception):
    """数据过期且 allow_stale=False。

    当本地缓存数据不满足 freshness_requirement 且不允许降级时抛出。
    """

    def __init__(self, interface_key: str, cutoff_at: datetime | None, requirement: str) -> None:
        self.interface_key = interface_key
        self.cutoff_at = cutoff_at
        self.requirement = requirement
        super().__init__(
            f"Stale data for {interface_key}: cutoff={cutoff_at}, requirement={requirement}"
        )

    def to_unified_error(self) -> "UserError":
        """转换为统一用户错误（WP-S.6）。

        映射到 `error_code="STALE_DATA"`，`status_code=503`。
        """
        from app.schemas.errors import build_user_error, TechnicalDetails
        from app.schemas.error_sanitizer import sanitize_message

        cutoff_str = self.cutoff_at.isoformat() if self.cutoff_at else "None"
        return build_user_error(
            "STALE_DATA",
            technical_details=TechnicalDetails(
                exception_type=type(self).__name__,
                status_code=503,
                error_message=sanitize_message(
                    f"interface={self.interface_key}; cutoff={cutoff_str}; requirement={self.requirement}"
                )[:500],
            ),
        )


class CircuitBreakerOpenError(Exception):
    """熔断器打开，拒绝请求。

    当接口处于 open 状态且冷却未到期时抛出。allow_stale=True 时由网关捕获并
    降级到本地过期数据；allow_stale=False 时向上抛出。
    """

    def __init__(self, interface_key: str, cooldown_until: datetime) -> None:
        self.interface_key = interface_key
        self.cooldown_until = cooldown_until
        super().__init__(
            f"Circuit breaker open for {interface_key}, cooldown until {cooldown_until}"
        )

    def to_unified_error(self) -> "UserError":
        """转换为统一用户错误（WP-S.6）。

        映射到 `error_code="CIRCUIT_BREAKER_OPEN"`，`status_code=503`。
        """
        from app.schemas.errors import build_user_error, TechnicalDetails
        from app.schemas.error_sanitizer import sanitize_message

        cooldown_str = self.cooldown_until.isoformat() if self.cooldown_until else "None"
        return build_user_error(
            "CIRCUIT_BREAKER_OPEN",
            technical_details=TechnicalDetails(
                exception_type=type(self).__name__,
                status_code=503,
                error_message=sanitize_message(
                    f"interface={self.interface_key}; cooldown_until={cooldown_str}"
                )[:500],
            ),
        )


__all__ = [
    "GatewayResponse",
    "DataValidationError",
    "StaleDataError",
    "CircuitBreakerOpenError",
    "SOURCE_L1_CACHE",
    "SOURCE_L2_DB",
    "SOURCE_L3_DUCKDB",
    "SOURCE_L4_REMOTE",
    "SOURCE_STALE",
]
