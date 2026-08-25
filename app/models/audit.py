"""审计模型模块（兼容入口）。

实际 DataGovernanceAuditEvent ORM 类定义在 services 层（因为服务函数一起）。
本模块只做 re-export，方便测试和路由的 `from app.models.audit import DataGovernanceAuditEvent`。
"""
from app.services.data_governance_audit import DataGovernanceAuditEvent  # noqa: F401

__all__ = ["DataGovernanceAuditEvent"]
