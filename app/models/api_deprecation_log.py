"""API 废弃访问日志模型（WP9.6）。

记录对已废弃 API 端点的访问，用于：
- 监控旧 API 的使用情况，评估是否可以安全下线
- 识别仍在使用旧 API 的客户端，便于通知迁移
- 满足废弃期合规要求：旧 API 在 Sunset 日期前仍可访问，但需记录访问日志

设计要点：
- 仅记录被标记为 deprecated 的端点访问，不影响正常 API
- 日志写入失败不阻断请求（best-effort）
- 支持按端点、时间范围查询
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ApiDeprecationLog(Base):
    """API 废弃访问日志。

    每次访问被标记为 deprecated 的端点时写入一条记录。
    """

    __tablename__ = "api_deprecation_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 被访问的废弃端点路径（含 /api/v1 前缀）
    endpoint: Mapped[str] = mapped_column(String(256), index=True)
    # HTTP 方法（GET/POST/PUT/DELETE/PATCH）
    method: Mapped[str] = mapped_column(String(8))
    # 客户端 IP（best-effort，可能为 None）
    client_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # User-Agent（best-effort，截断到 256 字符）
    user_agent: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # 后继端点（successor-version，从 deprecation 注册表读取）
    successor_endpoint: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # Sunset 日期（ISO 8601 字符串，如 "2026-12-31"）
    sunset_date: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # HTTP 响应状态码
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 额外上下文（JSON 字符串，预留扩展）
    context_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None),
        index=True,
    )
