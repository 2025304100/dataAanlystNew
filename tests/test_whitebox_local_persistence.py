"""白盒测试 - 本地入库稳定性模块（WP-S.4b）。

覆盖 `app/services/local_persistence.py` 的三个公开 API：
- `validate_record_contract`：字段契约/日期/数值范围校验
- `batch_upsert`：批量 UPSERT 分块提交
- `with_short_transaction`：短事务上下文管理器（commit/rollback/auto-close）

测试在 SQLite 内存库上运行，不依赖 MySQL。
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.models.index_price import IndexPrice
from app.schemas.external_data import DataValidationError
from app.services.local_persistence import (
    DataValidationError as ReExportedDataValidationError,
    batch_upsert,
    validate_record_contract,
    with_short_transaction,
)

pytestmark = pytest.mark.whitebox


# ============================================================================
# 1. validate_record_contract 测试
# ============================================================================

class TestValidateRecordContract:
    """字段契约校验测试。"""

    def test_valid_rows_pass(self):
        """符合契约的行应正常通过校验，不抛异常。"""
        rows = [
            {"symbol": "000300", "trade_date": "2024-06-01", "close": 4000.0},
            {"symbol": "000300", "trade_date": "2024-06-02", "close": 4050.0},
        ]
        # 不抛异常即通过
        validate_record_contract(
            rows,
            required_fields=["symbol", "trade_date", "close"],
            date_fields=["trade_date"],
            numeric_fields=["close"],
        )

    def test_empty_rows_pass(self):
        """空行列表应直接通过（无可校验内容）。"""
        validate_record_contract(
            [],
            required_fields=["symbol"],
        )

    def test_missing_required_field_raises(self):
        """缺失必填字段应抛 DataValidationError。"""
        rows = [{"trade_date": "2024-06-01", "close": 4000.0}]  # 缺 symbol
        with pytest.raises(DataValidationError) as exc_info:
            validate_record_contract(
                rows,
                required_fields=["symbol", "trade_date"],
            )
        assert "symbol" in str(exc_info.value)
        assert exc_info.value.field == "symbol"

    def test_none_required_field_raises(self):
        """必填字段为 None 应抛 DataValidationError。"""
        rows = [{"symbol": None, "trade_date": "2024-06-01"}]
        with pytest.raises(DataValidationError):
            validate_record_contract(
                rows,
                required_fields=["symbol"],
            )

    def test_invalid_date_raises(self):
        """不可解析的日期字段应抛 DataValidationError。"""
        rows = [{"symbol": "000300", "trade_date": "not-a-date"}]
        with pytest.raises(DataValidationError) as exc_info:
            validate_record_contract(
                rows,
                required_fields=["symbol"],
                date_fields=["trade_date"],
            )
        assert exc_info.value.field == "trade_date"

    def test_invalid_numeric_raises(self):
        """不可转为 float 的数值字段应抛 DataValidationError。"""
        rows = [{"symbol": "000300", "close": "not-a-number"}]
        with pytest.raises(DataValidationError):
            validate_record_contract(
                rows,
                required_fields=["symbol"],
                numeric_fields=["close"],
            )

    def test_numeric_out_of_range_raises(self):
        """数值字段超出范围应抛 DataValidationError。"""
        rows = [{"symbol": "000300", "close": 99999.0}]
        with pytest.raises(DataValidationError):
            validate_record_contract(
                rows,
                required_fields=["symbol"],
                numeric_fields=["close"],
                numeric_range=(0.0, 10000.0),
            )

    def test_date_out_of_range_raises(self):
        """日期字段超出范围应抛 DataValidationError。"""
        rows = [{"symbol": "000300", "trade_date": "2020-01-01"}]
        with pytest.raises(DataValidationError):
            validate_record_contract(
                rows,
                required_fields=["symbol"],
                date_fields=["trade_date"],
                date_range=("2024-01-01", "2024-12-31"),
            )

    def test_re_exported_exception_is_same_class(self):
        """local_persistence 导出的 DataValidationError 应与 schemas 中的是同一个类。"""
        assert ReExportedDataValidationError is DataValidationError


# ============================================================================
# 2. batch_upsert 测试
# ============================================================================

class TestBatchUpsert:
    """批量 UPSERT 测试。"""

    def test_insert_new_rows(self, db_session):
        """插入新行应返回写入数量，DB 中有对应记录。"""
        rows = [
            {"symbol": "000300", "trade_date": date(2024, 6, 1),
             "open": 4000.0, "high": 4050.0, "low": 3990.0, "close": 4020.0,
             "source": "test"},
            {"symbol": "000300", "trade_date": date(2024, 6, 2),
             "open": 4020.0, "high": 4080.0, "low": 4010.0, "close": 4060.0,
             "source": "test"},
        ]
        written = batch_upsert(
            IndexPrice.__table__, rows,
            conflict_keys=["symbol", "trade_date"],
            db=db_session,
        )
        assert written == 2
        assert db_session.query(IndexPrice).filter_by(symbol="000300").count() == 2

    def test_upsert_updates_existing_rows(self, db_session):
        """UPSERT 应更新已存在的行（按 conflict_keys 判定冲突）。"""
        # 第一次插入
        rows_v1 = [
            {"symbol": "000300", "trade_date": date(2024, 6, 1),
             "open": 4000.0, "high": 4050.0, "low": 3990.0, "close": 4020.0,
             "source": "v1"},
        ]
        batch_upsert(
            IndexPrice.__table__, rows_v1,
            conflict_keys=["symbol", "trade_date"],
            db=db_session,
        )

        # 第二次 UPSERT（相同主键，不同 close/source）
        rows_v2 = [
            {"symbol": "000300", "trade_date": date(2024, 6, 1),
             "open": 4000.0, "high": 4050.0, "low": 3990.0, "close": 4500.0,
             "source": "v2"},
        ]
        batch_upsert(
            IndexPrice.__table__, rows_v2,
            conflict_keys=["symbol", "trade_date"],
            db=db_session,
        )

        # 应只有 1 行，且 close 已被更新为 v2 的值
        records = db_session.query(IndexPrice).filter_by(symbol="000300").all()
        assert len(records) == 1
        assert records[0].close == 4500.0
        assert records[0].source == "v2"

    def test_empty_rows_returns_zero(self, db_session):
        """空行列表应返回 0，不抛异常。"""
        written = batch_upsert(
            IndexPrice.__table__, [],
            conflict_keys=["symbol", "trade_date"],
            db=db_session,
        )
        assert written == 0
        assert db_session.query(IndexPrice).count() == 0

    def test_auto_create_session_when_db_none(self, db_session):
        """db=None 时应自动创建 session 并关闭。"""
        # db_session fixture 已初始化 DatabaseManager，可直接通过 get_session_local 创建
        rows = [
            {"symbol": "000300", "trade_date": date(2024, 6, 1),
             "open": 4000.0, "high": 4050.0, "low": 3990.0, "close": 4020.0,
             "source": "auto_session"},
        ]
        written = batch_upsert(
            IndexPrice.__table__, rows,
            conflict_keys=["symbol", "trade_date"],
            db=None,  # 自动创建
        )
        assert written == 1
        # 验证写入确实生效（通过 db_session 查询）
        assert db_session.query(IndexPrice).filter_by(symbol="000300").count() == 1


# ============================================================================
# 3. with_short_transaction 测试
# ============================================================================

class TestWithShortTransaction:
    """短事务上下文管理器测试。"""

    def test_commit_on_success_with_own_session(self, db_session):
        """自建 session 模式：正常退出应 commit。"""
        rows = [
            {"symbol": "000300", "trade_date": date(2024, 6, 1),
             "open": 4000.0, "high": 4050.0, "low": 3990.0, "close": 4020.0,
             "source": "own_session_commit"},
        ]
        with with_short_transaction() as tx_db:
            batch_upsert(
                IndexPrice.__table__, rows,
                conflict_keys=["symbol", "trade_date"],
                db=tx_db,
            )
        # 退出 with 后通过 db_session 验证数据已持久化
        assert db_session.query(IndexPrice).filter_by(symbol="000300").count() == 1

    def test_rollback_on_exception_with_own_session(self, db_session):
        """自建 session 模式：异常退出应 rollback 未提交的写入，原异常向上抛。

        注意：`batch_upsert` 内部已按块 `commit()`，其写入不受外层 rollback 影响。
        此测试用 `db.add()` 直接写入（不 commit），验证 with_short_transaction
        的 rollback 语义对未提交事务有效。
        """
        with pytest.raises(RuntimeError, match="intentional failure"):
            with with_short_transaction() as tx_db:
                # 直接 add 不 commit，模拟"短事务内的中间写入"
                tx_db.add(IndexPrice(
                    symbol="000300",
                    trade_date=date(2024, 6, 1),
                    open=4000.0, high=4050.0, low=3990.0, close=4020.0,
                    source="should_rollback",
                ))
                # 模拟后续业务异常（commit 之前）
                raise RuntimeError("intentional failure")
        # rollback 后未提交的 add 不应持久化
        assert db_session.query(IndexPrice).filter_by(symbol="000300").count() == 0

    def test_passed_session_not_closed(self, db_session):
        """传入 session 模式：with 退出后 session 不应被关闭。"""
        with with_short_transaction(db=db_session) as tx_db:
            assert tx_db is db_session
            # 简单写入验证可用
            rows = [
                {"symbol": "000300", "trade_date": date(2024, 6, 1),
                 "open": 4000.0, "high": 4050.0, "low": 3990.0, "close": 4020.0,
                 "source": "passed_session"},
            ]
            batch_upsert(
                IndexPrice.__table__, rows,
                conflict_keys=["symbol", "trade_date"],
                db=tx_db,
            )
        # with 退出后 session 仍可用（未关闭）
        assert db_session.query(IndexPrice).filter_by(symbol="000300").count() == 1
        # 再次查询验证 session 活着
        assert db_session.query(IndexPrice).count() == 1

    def test_passed_session_rollback_on_exception(self, db_session):
        """传入 session 模式：异常退出应 rollback 但不关闭 session。"""
        with pytest.raises(ValueError, match="biz error"):
            with with_short_transaction(db=db_session) as tx_db:
                rows = [
                    {"symbol": "000300", "trade_date": date(2024, 6, 1),
                     "open": 4000.0, "close": 4020.0, "source": "rollback_test"},
                ]
                batch_upsert(
                    IndexPrice.__table__, rows,
                    conflict_keys=["symbol", "trade_date"],
                    db=tx_db,
                )
                raise ValueError("biz error")
        # rollback 后数据未持久化
        assert db_session.query(IndexPrice).filter_by(symbol="000300").count() == 0
        # session 仍可用
        assert db_session.query(IndexPrice).count() == 0

    def test_exception_not_masked_by_rollback_failure(self, db_session):
        """rollback 本身失败时，原异常应保留并向上抛。"""
        from unittest.mock import MagicMock

        original_exc = ValueError("original business error")
        with pytest.raises(ValueError, match="original business error"):
            with with_short_transaction(db=db_session) as tx_db:
                # 让 rollback 抛异常（不应掩盖原异常）
                db_session.rollback = MagicMock(side_effect=RuntimeError("rollback failed"))
                raise original_exc

    def test_can_be_used_without_writes(self, db_session):
        """空 with 块（无写入）正常 commit 不报错。"""
        with with_short_transaction(db=db_session):
            pass  # 无操作
        # 验证 session 仍可用
        assert db_session.query(IndexPrice).count() == 0

    def test_commit_then_subsequent_operation_sees_data(self, db_session):
        """验证 project_memory 硬约束：commit 后后续操作可见数据。

        场景模拟：网络请求获取数据 → with_short_transaction 写入 → 后续读取验证。
        """
        # 模拟"网络请求"获得的数据
        fetched_rows = [
            {"symbol": "000300", "trade_date": date(2024, 6, 1),
             "open": 4000.0, "high": 4050.0, "low": 3990.0, "close": 4020.0,
             "source": "fetched"},
        ]

        # 网络后建立短事务写入
        with with_short_transaction() as tx_db:
            batch_upsert(
                IndexPrice.__table__, fetched_rows,
                conflict_keys=["symbol", "trade_date"],
                db=tx_db,
            )

        # 后续操作（新事务）应能读到刚 commit 的数据
        subsequent_count = db_session.query(IndexPrice).filter_by(symbol="000300").count()
        assert subsequent_count == 1
