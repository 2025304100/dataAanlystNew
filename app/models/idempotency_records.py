"""复数模块名兼容入口；实际实现见单数模块（idempotency_record.py）。

允许两种导入：
    from app.models.idempotency_records import IdempotencyRecord  # 复数
    from app.models.idempotency_record  import IdempotencyRecord  # 单数
两者指向同一个 Mapper 类。注意 SQLAlchemy 不能重复注册相同 __tablename__，
所以这里仅 re-export，避免二次 declare。
"""
from app.models.idempotency_record import IdempotencyRecord  # noqa: F401

__all__ = ["IdempotencyRecord"]
