"""API 废弃期访问日志中间件（WP9.6）。

功能：
1. 对已标记为 deprecated 的 API 端点添加 HTTP 废弃头：
   - Deprecation: true
   - Sunset: 2026-12-31（计划下线日期）
   - Link: </api/v1/new-endpoint>; rel="successor-version"
2. 记录访问日志到 api_deprecation_logs 表：
   - 访问时间、端点、方法、客户端 IP、User-Agent
   - 日志写入失败不阻断请求（best-effort）

设计要点：
- 废弃端点注册表（DEPRECATED_ENDPOINTS）集中管理，便于扩展
- 路径匹配支持精确匹配与通配符（{param} 占位符）
- 中间件对所有请求生效，但仅对注册表中的端点添加头和记录日志
- 旧 API 在废弃期仍可正常访问（仅添加头和日志，不拒绝请求）

废弃期合规：
- 旧 API 在 Sunset 日期前保持可用
- 通过访问日志监控使用情况，评估安全下线时机
- Sunset 日期后可通过运维手段（如反代规则）正式下线
"""
from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.models.api_deprecation_log import ApiDeprecationLog


logger = logging.getLogger(__name__)


# ── 废弃端点注册表 ──────────────────────────────────────────
#
# 每个条目定义：
# - pattern: 路径匹配模式（支持 {param} 占位符，会转为正则）
# - methods: 匹配的 HTTP 方法列表（None 表示所有方法）
# - sunset: 计划下线日期（ISO 8601）
# - successor: 后继端点路径（用于 Link 头）
# - reason: 废弃原因（用于日志上下文）
#
# WP9.5 停止读取最新扫描作为默认来源后，以下端点进入废弃期：
# - /api/v1/scans/latest/executable：旧版"最新可执行候选"端点，
#   后继为组合成员管理端点（/api/v1/portfolios/{portfolio_id}/members）
#   注意：该端点在废弃期仍可访问，仅添加废弃头和访问日志。

DEPRECATED_ENDPOINTS: list[dict[str, Any]] = [
    {
        "pattern": "/api/v1/scans/latest/executable",
        "methods": None,  # 所有方法
        "sunset": "2026-12-31",
        "successor": "/api/v1/portfolios/{portfolio_id}/members",
        "reason": "WP9.5: 最新扫描候选不再作为自动交易/组合回测的默认来源，"
                  "请改用组合成员管理 API 获取标的列表",
    },
]


def _compile_pattern(pattern: str) -> re.Pattern[str]:
    """将路径模式编译为正则表达式。

    支持的占位符：
    - {param} → 匹配任意非斜杠字符（[^/]+）
    - 其余字符按字面量匹配

    返回的正则会匹配完整路径（^...$）。
    """
    # 转义正则特殊字符，然后替换 {param} 占位符
    escaped = re.escape(pattern)
    # re.escape 会将 { } 转义为 \{ \}，这里还原为正则捕获组
    compiled = re.sub(r"\\\{[^}]+\}", r"[^/]+", escaped)
    return re.compile(f"^{compiled}$")


def _match_deprecated(path: str, method: str) -> dict[str, Any] | None:
    """检查请求路径是否匹配某个废弃端点。

    Returns:
        匹配的废弃端点配置 dict，未匹配返回 None。
    """
    for entry in DEPRECATED_ENDPOINTS:
        pattern = _compile_pattern(entry["pattern"])
        if pattern.match(path):
            if entry["methods"] is None or method.upper() in entry["methods"]:
                return entry
    return None


def _get_client_ip(request: Any) -> str | None:
    """从请求中提取客户端 IP（best-effort）。

    优先级：
    1. X-Forwarded-For 头（第一个 IP）
    2. X-Real-IP 头
    3. request.client.host
    """
    try:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()[:64]
        real_ip = request.headers.get("x-real-ip")
        if real_ip:
            return real_ip.strip()[:64]
        if request.client:
            return str(request.client.host)[:64]
    except Exception:
        pass
    return None


def _get_user_agent(request: Any) -> str | None:
    """从请求中提取 User-Agent（截断到 256 字符）。"""
    try:
        ua = request.headers.get("user-agent")
        if ua:
            return ua[:256]
    except Exception:
        pass
    return None


def record_deprecation_access(
    db: Session,
    *,
    endpoint: str,
    method: str,
    client_ip: str | None,
    user_agent: str | None,
    successor_endpoint: str | None,
    sunset_date: str | None,
    status_code: int | None = None,
    context_json: str | None = None,
) -> ApiDeprecationLog | None:
    """记录一次废弃 API 访问到数据库（WP9.6）。

    Args:
        db: 数据库会话
        endpoint: 被访问的废弃端点路径
        method: HTTP 方法
        client_ip: 客户端 IP
        user_agent: User-Agent
        successor_endpoint: 后继端点路径
        sunset_date: Sunset 日期
        status_code: HTTP 响应状态码
        context_json: 额外上下文 JSON

    Returns:
        创建的 ApiDeprecationLog 对象，失败返回 None
    """
    try:
        log = ApiDeprecationLog(
            endpoint=endpoint,
            method=method,
            client_ip=client_ip,
            user_agent=user_agent,
            successor_endpoint=successor_endpoint,
            sunset_date=sunset_date,
            status_code=status_code,
            context_json=context_json,
        )
        db.add(log)
        db.commit()
        db.refresh(log)
        return log
    except Exception as exc:
        # 日志写入失败不阻断请求
        logger.warning("WP9.6: 废弃 API 访问日志写入失败 endpoint=%s: %s", endpoint, exc)
        try:
            db.rollback()
        except Exception:
            pass
        return None


def query_deprecation_logs(
    db: Session,
    *,
    endpoint: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[ApiDeprecationLog]:
    """查询废弃 API 访问日志（WP9.6）。

    Args:
        db: 数据库会话
        endpoint: 按端点过滤（None 表示所有）
        limit: 返回条数上限
        offset: 偏移量

    Returns:
        ApiDeprecationLog 列表，按 created_at 降序排列
    """
    stmt = select(ApiDeprecationLog)
    if endpoint:
        stmt = stmt.where(ApiDeprecationLog.endpoint == endpoint)
    stmt = stmt.order_by(desc(ApiDeprecationLog.created_at)).limit(limit).offset(offset)
    return list(db.execute(stmt).scalars().all())


def count_deprecation_logs(
    db: Session,
    *,
    endpoint: str | None = None,
) -> int:
    """统计废弃 API 访问日志数量（WP9.6）。

    Args:
        db: 数据库会话
        endpoint: 按端点过滤（None 表示所有）

    Returns:
        日志总数
    """
    from sqlalchemy import func

    stmt = select(func.count(ApiDeprecationLog.id))
    if endpoint:
        stmt = stmt.where(ApiDeprecationLog.endpoint == endpoint)
    return int(db.execute(stmt).scalar() or 0)


def apply_deprecation_headers(response: Any, entry: dict[str, Any]) -> None:
    """为响应添加废弃相关的 HTTP 头（WP9.6）。

    添加的头：
    - Deprecation: true
    - Sunset: <sunset_date>
    - Link: <successor>; rel="successor-version"

    Args:
        response: FastAPI Response 对象
        entry: 废弃端点配置 dict
    """
    try:
        response.headers["Deprecation"] = "true"
        if entry.get("sunset"):
            response.headers["Sunset"] = entry["sunset"]
        if entry.get("successor"):
            response.headers["Link"] = f'<{entry["successor"]}>; rel="successor-version"'
    except Exception as exc:
        logger.warning("WP9.6: 添加废弃头失败: %s", exc)


def ensure_deprecation_log_table(engine: Any) -> None:
    """确保 api_deprecation_logs 表存在（幂等，WP9.6）。

    用于启动时和测试中确保表存在。Base.metadata.create_all 已覆盖新建场景，
    本函数用于防御性检查。
    """
    from sqlalchemy import inspect as sa_inspect

    inspector = sa_inspect(engine)
    if not inspector.has_table(ApiDeprecationLog.__tablename__):
        ApiDeprecationLog.__table__.create(engine, checkfirst=True)
        logger.info("WP9.6: 已创建 %s 表", ApiDeprecationLog.__tablename__)


__all__ = [
    "DEPRECATED_ENDPOINTS",
    "apply_deprecation_headers",
    "count_deprecation_logs",
    "ensure_deprecation_log_table",
    "query_deprecation_logs",
    "record_deprecation_access",
]
