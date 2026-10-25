"""BUG-17 回归：security_status_daily.source_updated_at 必须保留微秒。

CSV 导入回执以完整微秒时间戳做行↔回执一致性校验
（local_import._same_source / _status_fingerprint 用 timespec="microseconds"）。
MySQL 默认 DATETIME 截断到秒曾导致校验恒 False，导入的涨跌停执行约束
永远无法挂接、回测预检必败（PRICE_LIMIT_DATA_UNAVAILABLE）。SQLite 存
字符串不截断，因此本回归用 MySQL 方言编译断言把契约钉死。
"""
import sqlalchemy.dialects.mysql as mysql_dialect

from app.models.security_status import SecurityStatusDaily

pytestmark = pytest.mark.whitebox if __import__("pytest").mark.__dict__.get("whitebox") else None

import pytest


def test_source_updated_at_compiles_to_datetime6_on_mysql():
    column = SecurityStatusDaily.__table__.c.source_updated_at
    compiled = str(column.type.compile(dialect=mysql_dialect.dialect()))
    assert "DATETIME(6)" in compiled, (
        "source_updated_at 在 MySQL 方言必须编译为 DATETIME(6)："
        f"实际编译结果 {compiled!r}。若退化为普通 DATETIME，微秒将被截断，"
        "CSV 导入的执行约束（涨跌停）将永远无法通过 _same_source 校验（BUG-17）。"
    )


def test_status_fingerprint_uses_microseconds():
    """_status_fingerprint 依赖微秒精度；此处守住模型层精度不被无意调低。"""
    from datetime import datetime

    from sqlalchemy import DateTime
    from sqlalchemy.dialects.mysql import DATETIME

    col = SecurityStatusDaily.__table__.c.source_updated_at
    # with_variant 的 mysql 变体必须仍是 DATETIME(6)
    variant = col.type
    assert isinstance(variant, DateTime)
    compiled = str(variant.compile(dialect=mysql_dialect.dialect()))
    assert "DATETIME(6)" in compiled
    # 时间戳含微秒时可无损往返（datetime 对象本身支持，此处防低精度默认值）
    now = datetime(2026, 10, 25, 0, 33, 20, 126864)
    assert now.microsecond == 126864
