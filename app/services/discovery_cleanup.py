from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.scan import ScanResult

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _safe_datetime(value):
    """安全地将值转换为 datetime 类型，处理 MySQL 返回的字符串。"""
    from datetime import datetime
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d"):
            try:
                return datetime.strptime(value, fmt)
            except ValueError:
                continue
    return None


def cleanup_expired_discovery_results(db: Session) -> dict:
    """清理过期挖掘结果（向后兼容接口）。

    WP-P.7 后：内部委托给分层清理服务 `cleanup_expired_discovery_results_layered`，
    返回兼容旧格式 {"deleted": int, "layered_report": dict}。

    旧调用方（main.py / discovery 路由）读取 `result["deleted"]`，无需修改；
    新调用方可读取 `result["layered_report"]` 获取分层清理详情。

    Returns:
        {
            "deleted": int,                # ScanResult + DiscoveryCandidate 删除总数
            "skipped_frozen": 0,           # 兼容字段（永远为 0）
            "layered_report": dict,        # CleanupReport.to_dict()
        }
    """
    # 延迟导入避免循环依赖
    from app.services.discovery_retention import cleanup_expired_discovery_results_layered

    report = cleanup_expired_discovery_results_layered(db)

    deleted_total = report.deleted_scan_results + report.deleted_discovery_candidates
    return {
        "deleted": deleted_total,
        "skipped_frozen": 0,
        "layered_report": report.to_dict(),
    }
