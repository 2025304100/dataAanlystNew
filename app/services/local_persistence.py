"""本地入库稳定性独立模块（WP-S.4b）。

职责：
封装 `app/services/external_data_gateway.py` 中的入库相关工具，提供独立、稳定、
易导入的公共 API，便于业务方在网络请求后建立短事务、按契约批量 UPSERT 入库。

设计原则（来自 project_memory 硬约束）：
- 不重写 `external_data_gateway.py` 已稳定的能力，仅做薄包装
- 网络请求后建立短事务，单事务提交/回滚，自建 session 自动关闭
- 字段契约校验失败时不写入业务表
- 错误消息脱敏（不暴露明文密码、Token、Webhook 等）
- 大批量写入按块提交，失败块可重试
- 关键数据操作 commit 后再继续后续处理（防止数据丢失）

公共 API：
- `validate_record_contract(rows, *, required_fields, date_fields=(), numeric_fields=(),
    date_range=None, numeric_range=None) -> None`
- `batch_upsert(table, rows, conflict_keys, *, db=None, batch_size=None, max_retries=None) -> int`
- `with_short_transaction(db: Session | None = None)` 上下文管理器

使用示例：
    from app.services.local_persistence import (
        batch_upsert, validate_record_contract, with_short_transaction,
    )

    # 网络请求获取数据（释放 DB 事务）
    rows = await fetch_rows_from_remote()

    # 网络后建立短事务批量写入
    with with_short_transaction() as db:
        validate_record_contract(
            rows,
            required_fields=["symbol", "trade_date", "close"],
            date_fields=["trade_date"],
            numeric_fields=["close"],
        )
        batch_upsert(
            MyModel.__table__, rows,
            conflict_keys=["symbol", "trade_date"],
            db=db,
        )
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Mapping, Sequence

from sqlalchemy.orm import Session

# 复用 external_data_gateway 已稳定的工具（薄包装，不重写）
from app.services.external_data_gateway import _upsert_batch, validate_rows
from app.schemas.external_data import DataValidationError
from app.db.session import get_session_local

logger = logging.getLogger(__name__)


__all__ = [
    "validate_record_contract",
    "batch_upsert",
    "with_short_transaction",
    # 复用异常类型，便于业务方按统一方式捕获
    "DataValidationError",
]


def validate_record_contract(
    rows: Sequence[Mapping[str, Any]],
    *,
    required_fields: Sequence[str],
    date_fields: Sequence[str] = (),
    numeric_fields: Sequence[str] = (),
    date_range: tuple[Any, Any] | None = None,
    numeric_range: tuple[float, float] | None = None,
) -> None:
    """入库前字段契约校验（WP-S.4b）。

    包装 `external_data_gateway.validate_rows`，对外提供稳定的命名入口。
    校验失败抛 `DataValidationError`，不写入业务表。

    Args:
        rows: 待校验的行列表（每行为 dict-like）
        required_fields: 必填字段名列表（值为 None 或空串视为缺失）
        date_fields: 日期字段名列表（校验可解析为日期）
        numeric_fields: 数值字段名列表（校验可转为 float）
        date_range: (start, end) 闭区间，date_fields 必须在此范围内
        numeric_range: (min, max) 闭区间，numeric_fields 必须在此范围内

    Raises:
        DataValidationError: 任一行任一字段校验失败

    Example:
        >>> validate_record_contract(
        ...     rows=[{"symbol": "000001", "trade_date": "2024-06-01", "close": 10.0}],
        ...     required_fields=["symbol", "trade_date", "close"],
        ...     date_fields=["trade_date"],
        ...     numeric_fields=["close"],
        ...     numeric_range=(0.0, 100000.0),
        ... )
    """
    validate_rows(
        rows,
        required_fields=required_fields,
        date_fields=date_fields,
        numeric_fields=numeric_fields,
        date_range=date_range,
        numeric_range=numeric_range,
    )


def batch_upsert(
    table,
    rows: Sequence[Mapping[str, Any]],
    conflict_keys: Sequence[str],
    *,
    db: Session | None = None,
    batch_size: int | None = None,
    max_retries: int | None = None,
) -> int:
    """批量 UPSERT 入库工具（WP-S.4b）。

    包装 `external_data_gateway._upsert_batch`，对外提供稳定的命名入口。
    支持自动创建/关闭 session 与复用调用方传入的 session。

    特性：
    - SQLite 用 `ON CONFLICT DO UPDATE`；MySQL 用 `ON DUPLICATE KEY UPDATE`
    - conflict_keys 必须匹配目标表的实际唯一约束
    - 大批量写入按 `batch_size` 分块提交，失败块按 `max_retries` 重试
    - 自建 session 模式（db=None）在函数结束时自动关闭
    - 传入 session 模式由调用方管理生命周期（不自动关闭）

    Args:
        table: SQLAlchemy Table 对象（通过 `ORM.__table__` 获取）
        rows: 待写入的行列表（每行为 dict-like）
        conflict_keys: 冲突判定列名列表
        db: 可选 Session（不传则自动创建并关闭）
        batch_size: 分块大小（None 用 settings.EXTERNAL_DATA_UPSERT_BATCH_SIZE）
        max_retries: 失败块最大重试次数（None 用 settings.EXTERNAL_DATA_UPSERT_MAX_RETRIES）

    Returns:
        成功写入的行数（注意：UPSERT 更新已存在行也计为 1）

    Example:
        >>> written = batch_upsert(
        ...     IndexPrice.__table__,
        ...     [{"symbol": "000300", "trade_date": date(2024, 6, 1), "close": 4000.0}],
        ...     conflict_keys=["symbol", "trade_date"],
        ... )
    """
    return _upsert_batch(
        table,
        rows,
        conflict_keys,
        db=db,
        batch_size=batch_size,
        max_retries=max_retries,
    )


@contextmanager
def with_short_transaction(db: Session | None = None):
    """短事务上下文管理器（WP-S.4b）。

    在网络请求后建立短事务，单事务提交/回滚。
    自建 session（db=None）在 with 退出时自动关闭；
    传入 session（db=Session）由调用方管理生命周期，with 不关闭。

    设计原则（来自 project_memory 硬约束）：
    - "网络请求前释放长期数据库事务，网络后建立短事务"
    - "关键数据操作必须 commit 后再继续后续处理（防止数据丢失）"
    - 异常时 rollback 未提交的写入，原异常向上抛（不被 rollback 失败掩盖）

    Args:
        db: 可选 Session；None 时自动创建新 session 并在退出时关闭

    Yields:
        Session: 可用于该事务内的读写操作

    Example:
        >>> # 自建 session 模式
        >>> with with_short_transaction() as db:
        ...     db.add(MyModel(field="value"))
        ...     # with 正常退出时自动 commit
        >>> # 传入 session 模式（不关闭 session）
        >>> with with_short_transaction(db=existing_session) as tx_db:
        ...     tx_db.add(MyModel(field="value"))
    """
    own_session = db is None
    if own_session:
        SessionLocal = get_session_local()
        db = SessionLocal()

    assert db is not None  # mypy hint

    try:
        yield db
        # 正常退出：commit 提交事务
        db.commit()
    except Exception:
        # 异常退出：rollback 未提交的写入
        # 注意：rollback 失败不应掩盖原异常
        try:
            db.rollback()
        except Exception as rollback_exc:
            logger.warning(
                "with_short_transaction rollback failed: %s (original exception preserved)",
                rollback_exc,
            )
            # 不替换原异常，原异常会向上抛
        raise
    finally:
        if own_session:
            try:
                db.close()
            except Exception as close_exc:
                logger.warning(
                    "with_short_transaction session close failed: %s",
                    close_exc,
                )
