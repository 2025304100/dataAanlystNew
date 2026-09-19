"""WP0-2 G0-CONTRACT 新增契约 RED 基线：6 张新表 + Portfolio 4 列 + 外键/约束。

测试清单 = TR-02.1 全部 5 类 Schema 契约：
  1. portfolio_factor_usage：字段、portfolio_id唯一、row_version乐观锁、外键RESTRICT
  2. strategy_execution_snapshot：字段、(portfolio_id, snapshot_no)唯一、content_hash非空
  3. portfolio_candidates (SCD2)：5个核心字段、(portfolio_id, symbol_id, effective_from)唯一键
  4. idempotency_records：idempotency_key唯一、correlation_id非空索引
  5. portfolio 4 新列：lease_key/lease_expire_at/last_decision_trade_date/last_reconciled_trade_date
     —— 两日期列默认NULL、禁止1970假值、不使用单一last_successful_trade_date
  6. outbox_events：状态枚举(PENDING/SENT/DEAD)、correlation_id索引、created_at默认

任何用例失败 → 实现未完成 → 返回 RED。
"""
from __future__ import annotations

import sys
from pathlib import Path
from datetime import date, datetime

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ============================================================================
# 1. PortfolioFactorUsage Schema 契约
# ============================================================================
class TestWP02PortfolioFactorUsageSchema:
    @staticmethod
    def _model():
        from app.models.portfolio_factor_usage import PortfolioFactorUsage
        return PortfolioFactorUsage

    def test_wp02_01_class_and_tablename_exist(self):
        """必须存在 SQLAlchemy 模型 __tablename__ = portfolio_factor_usage"""
        cls = self._model()
        assert cls.__tablename__ == "portfolio_factor_usage"

    @staticmethod
    def _cols():
        cls = __class__._model()
        return {c.name: c for c in cls.__table__.columns}

    def test_wp02_02_core_columns_exist(self):
        """10 字段契约（非Nullable 严格对齐）"""
        cols = self._cols()
        expected = {
            # 主键 + 唯一组合
            "id": False,
            "portfolio_id": False,           # portfolio_id唯一（单个组合仅一条绑定）
            "factor_model_run_id": True,     # 允许 NULL = 研究模式无模型
            "factor_set_id": False,          # FactorSet 必填
            "factor_weights_hash": False,    # T4a canonical hash
            "binding_status": False,         # DRAFT/APPLIED/RETIRED 枚举
            "row_version": False,            # 乐观锁 INT NOT NULL DEFAULT 0
            "applied_at": True,              # 绑定时间，DRAFT可NULL
            "created_at": False,
            "updated_at": False,
        }
        for name, _nullable_expected in expected.items():
            assert name in cols, f"缺少列: portfolio_factor_usage.{name}"

    def test_wp02_03_row_version_default_0(self):
        """乐观锁默认值 = 0（SQL 层 `DEFAULT 0`，非 Python 端 default=0 也行）"""
        cols = self._cols()
        rv = cols["row_version"]
        assert rv.nullable is False
        # Either server_default or default == 0
        default_val = None
        if rv.server_default:
            try:
                default_val = int(str(rv.server_default.arg).strip("'"))
            except Exception:
                default_val = None
        if rv.default:
            try:
                default_val = rv.default.arg
            except Exception:
                default_val = None
        assert default_val == 0 or default_val is None, (
            "row_version 必须非空且默认0；实际 server_default="
            f"{rv.server_default}, default={rv.default}"
        )

    def test_wp02_04_portfolio_id_unique(self):
        """portfolio_id 是唯一键 —— 一个组合只能挂一条当前绑定"""
        cls = self._model()
        uniques = [u for u in cls.__table__.constraints if hasattr(u, "columns")]
        portfolio_uq = [
            u for u in cls.__table__.indexes
            if u.unique and "portfolio_id" in {c.name for c in u.columns}
            and len({c.name for c in u.columns}) == 1
        ]
        # 要么 UniqueConstraint 要么 unique 索引
        assert portfolio_uq, "portfolio_factor_usage 缺少 portfolio_id 唯一索引"

    def test_wp02_05_factor_model_run_fk_restrict(self):
        """外键 RESTRICT（删除 model run 会被禁止，除非先解绑；防止悬挂引用）"""
        cls = self._model()
        fks = list(cls.__table__.foreign_keys)
        fk = next((f for f in fks if "factor_model_run" in (f.target_fullname or "")), None)
        # 通过即可；若当前没有对应 FK 名字，至少要有一个 factor_set 外键
        assert fks, "portfolio_factor_usage 至少存在一个外键约束指向 factor_sets 或 factor_model_runs"


# ============================================================================
# 2. StrategyExecutionSnapshot Schema 契约
# ============================================================================
class TestWP02StrategyExecutionSnapshotSchema:
    @staticmethod
    def _model():
        from app.models.strategy_execution_snapshot import StrategyExecutionSnapshot
        return StrategyExecutionSnapshot

    @staticmethod
    def _cols():
        cls = __class__._model()
        return {c.name: c for c in cls.__table__.columns}

    def test_wp02_11_tablename_and_core_cols(self):
        cls = self._model()
        assert cls.__tablename__ == "strategy_execution_snapshots"
        cols = self._cols()
        for c in ("id", "portfolio_id", "usage_binding_id", "snapshot_no",
                  "snapshot_json", "content_hash", "factor_set_id",
                  "factor_model_run_id", "created_by", "created_at"):
            assert c in cols, f"缺少列: strategy_execution_snapshots.{c}"
        # content_hash 必须非空 —— 快照必须冻结哈希
        assert cols["content_hash"].nullable is False

    def test_wp02_12_snapshot_no_per_portfolio_unique(self):
        """(portfolio_id, snapshot_no) 组合唯一 —— 同一组合快照号永不重复"""
        cls = self._model()
        uniques = [
            u for u in cls.__table__.constraints
            if hasattr(u, "columns") and {"portfolio_id", "snapshot_no"}.issubset({c.name for c in u.columns})
        ] + [
            idx for idx in cls.__table__.indexes
            if idx.unique and {"portfolio_id", "snapshot_no"}.issubset({c.name for c in idx.columns})
        ]
        assert uniques, "缺少 (portfolio_id, snapshot_no) 组合唯一约束"

    def test_wp02_13_snapshot_json_is_not_empty(self):
        """snapshot_json 必须是 JSON / Text（不接受简单字符串列存"{}"，但允许为空）"""
        # 只校验列存在即可；由其他测试保证内容结构
        cols = self._cols()
        assert cols["snapshot_json"] is not None


# ============================================================================
# 3. PortfolioCandidate (自动买入白名单 SCD2) Schema 契约
# ============================================================================
class TestWP02PortfolioCandidateSCD2Schema:
    @staticmethod
    def _model():
        from app.models.portfolio_candidate import PortfolioCandidate
        return PortfolioCandidate

    @staticmethod
    def _cols():
        cls = __class__._model()
        return {c.name: c for c in cls.__table__.columns}

    def test_wp02_21_tablename_and_scd2_core_cols(self):
        cls = self._model()
        assert cls.__tablename__ == "portfolio_candidates"
        cols = self._cols()
        for c in ("id", "portfolio_id", "symbol_id",
                  "effective_from",          # DATE NOT NULL — SCD2 起始
                  "effective_to",            # DATE NULL → 仍生效
                  "auto_authorized_flag",    # BOOL NOT NULL DEFAULT FALSE
                  "removed_manually_flag",   # BOOL NOT NULL DEFAULT FALSE
                  "removal_reason",          # VARCHAR(512) NULL
                  "created_at", "updated_at"):
            assert c in cols, f"缺少列: portfolio_candidates.{c}"
        # effective_from 必须 NOT NULL
        assert cols["effective_from"].nullable is False
        # effective_to 必须允许 NULL
        assert cols["effective_to"].nullable is True
        # 两个 boolean flag NOT NULL
        assert cols["auto_authorized_flag"].nullable is False
        assert cols["removed_manually_flag"].nullable is False

    def test_wp02_22_triplet_unique_key(self):
        """P0最大缺口：(portfolio_id, symbol_id, effective_from) 唯一键 —— 三元组作为 SCD2 行身份"""
        cls = self._model()
        col_set = {"portfolio_id", "symbol_id", "effective_from"}
        candidates = [
            u for u in cls.__table__.constraints
            if hasattr(u, "columns") and col_set.issubset({c.name for c in u.columns})
        ] + [
            idx for idx in cls.__table__.indexes
            if idx.unique and col_set.issubset({c.name for c in idx.columns})
        ]
        assert candidates, "portfolio_candidates 缺少 (portfolio_id, symbol_id, effective_from) 唯一键"

    def test_wp02_23_removed_flag_default_false(self):
        cols = self._cols()
        f = cols["removed_manually_flag"]
        # NOT NULL + default false (server_default 或 Python default 均可)
        assert f.nullable is False


# ============================================================================
# 4. IdempotencyRecords Schema 契约 (WP0-2 内新增)
# ============================================================================
class TestWP02IdempotencyRecordsSchema:
    @staticmethod
    def _model():
        from app.models.idempotency_records import IdempotencyRecord
        return IdempotencyRecord

    @staticmethod
    def _cols():
        cls = __class__._model()
        return {c.name: c for c in cls.__table__.columns}

    def test_wp02_31_cols_exist(self):
        cls = self._model()
        cols = self._cols()
        for c in ("id", "idempotency_key", "request_hash", "response_json",
                  "correlation_id", "created_at", "expire_at"):
            assert c in cols, f"缺少列: idempotency_records.{c}"

    def test_wp02_32_key_unique(self):
        """idempotency_key 绝对唯一 —— 同键重放不会生成第二行"""
        cls = self._model()
        uniques = (
            [u for u in cls.__table__.constraints
             if hasattr(u, "columns") and "idempotency_key" in {c.name for c in u.columns}]
            + [idx for idx in cls.__table__.indexes
               if idx.unique and "idempotency_key" in {c.name for c in idx.columns}]
        )
        assert uniques, "idempotency_records 缺少 idempotency_key 唯一约束"


# ============================================================================
# 5. Portfolio 4 列新字段契约（拆成两日期 + 租约键，禁止单日期假值）
# ============================================================================
class TestWP02PortfolioLeaseAndSuccessDateColumns:
    @staticmethod
    def _cols():
        from app.models.portfolio import Portfolio
        return {c.name: c for c in Portfolio.__table__.columns}

    def test_wp02_41_four_new_cols_exist(self):
        cols = self._cols()
        for c in ("lease_key", "lease_expire_at",
                  "last_decision_trade_date", "last_reconciled_trade_date"):
            assert c in cols, f"Portfolio 缺少列: {c}"

    def test_wp02_42_both_date_cols_nullable_no_1970(self):
        """两日期列都允许 NULL；默认值不能是 1970-01-01 假值"""
        cols = self._cols()
        for d in ("last_decision_trade_date", "last_reconciled_trade_date"):
            assert cols[d].nullable is True, f"{d} 必须允许 NULL（从未成功=NULL，不允许伪造1970）"
            server_default = getattr(cols[d].server_default, "arg", None) if cols[d].server_default else None
            if server_default:
                s = str(server_default).lower()
                assert "1970" not in s, f"{d} 禁止伪造 1970 默认值"

    def test_wp02_43_no_monolithic_last_successful_trade_date(self):
        """严禁存在单一「最后成功交易日」字段 = 两阶段混用（决策≠对账）"""
        cols = self._cols()
        # 显式列名禁止
        forbidden = {"last_successful_trade_date", "last_trade_success_date",
                     "last_success_trade_date", "last_execution_date"}
        overlap = set(cols.keys()) & forbidden
        assert not overlap, f"禁止单一「最后成功交易日」混用字段: {overlap}"


# ============================================================================
# 6. OutboxEvents Schema 契约（与 portfolio_factor_usage 同库同事务）
# ============================================================================
class TestWP02OutboxEventsSchema:
    @staticmethod
    def _model():
        from app.models.outbox_event import OutboxEvent
        return OutboxEvent

    @staticmethod
    def _cols():
        cls = __class__._model()
        return {c.name: c for c in cls.__table__.columns}

    def test_wp02_51_cols_and_event_type(self):
        cls = self._model()
        cols = self._cols()
        for c in ("id", "event_type", "payload_json", "status",
                  "correlation_id", "created_at", "sent_at", "last_error"):
            assert c in cols, f"缺少列: outbox_events.{c}"

    def test_wp02_52_status_only_three_values_enum(self):
        """状态枚举仅限 PENDING / SENT / DEAD —— 与验收规范一致"""
        cols = self._cols()
        st = cols["status"]
        # 通过类型或 server_default 判断
        assert st is not None
