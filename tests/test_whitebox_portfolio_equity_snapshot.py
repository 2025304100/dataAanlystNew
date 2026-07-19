"""白盒测试 - 组合净值快照（P0-8 / P0-9 / P0-10）。

守护本次新增的快照逻辑关键点，防止重构/优化时退化：
1. upsert_snapshot 首次写入 daily_return=0（无前一日）
2. upsert_snapshot 次日写入 daily_return 计算正确
3. upsert_snapshot 同日重复 → 更新而非插入（保持唯一约束）
4. upsert_snapshot 前一日 total_equity=0 兜底为 0
5. snapshot_all_simulated_portfolios 只处理 simulated 组合
6. snapshot_all_simulated_portfolios 单组合失败不影响其他组合
7. list_portfolio_equity_snapshots 按日期升序 + start_date/end_date/limit 过滤
8. snapshot_to_dict 字段完整、类型正确
9. create_portfolio_equity_snapshot_task 去重（已有 queued 时返回现有任务）
10. scheduled_tasks 新增 portfolio_equity_snapshot 类型可校验与分发
"""
from __future__ import annotations

import math
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from app.models.portfolio import Portfolio
from app.models.portfolio_equity_snapshot import PortfolioEquitySnapshot
from app.services.portfolio_equity_snapshot import (
    TASK_TYPE,
    create_portfolio_equity_snapshot_task,
    list_portfolio_equity_snapshots,
    snapshot_all_simulated_portfolios,
    snapshot_to_dict,
    upsert_snapshot,
)
from app.services.sim_accounts import ensure_sim_account_seed

pytestmark = pytest.mark.whitebox


# ============================================================================
# 测试辅助
# ============================================================================

def _make_portfolio(db_session, name="QA-Snap", account_type="simulated", total_capital=100000.0, is_default=False) -> Portfolio:
    """造一个组合并初始化 CashLedger（与 sim_accounts 流程一致）。"""
    p = Portfolio(
        name=name,
        account_type=account_type,
        total_capital=total_capital,
        investable_ratio=0.9,
        cash_reserve_ratio=0.1,
        currency="CNY",
        is_default=int(is_default),
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    if account_type == "simulated":
        ensure_sim_account_seed(db_session, p)
        db_session.commit()
    return p


# ============================================================================
# 1-4. upsert_snapshot 行为
# ============================================================================

class TestUpsertSnapshot:
    """守护 upsert_snapshot 的 daily_return 计算与 Upsert 语义。"""

    def test_first_snapshot_daily_return_is_zero(self, db_session):
        """【P0-8/9 测试】首次写入（无前一日）daily_return 应为 0。"""
        p = _make_portfolio(db_session, name="QA-First")
        snap = upsert_snapshot(
            db_session,
            portfolio_id=p.id,
            snapshot_date=date(2026, 7, 18),
            cash_balance=100000.0,
            market_value=0.0,
            total_equity=100000.0,
            realized_pnl=0.0,
            unrealized_pnl=0.0,
            position_count=0,
        )
        db_session.commit()
        assert snap.daily_return == 0.0
        assert snap.total_equity == 100000.0
        assert snap.id is not None

    def test_second_snapshot_daily_return_computed(self, db_session):
        """【P0-9 测试】次日写入 daily_return = (今日 - 昨日) / 昨日。"""
        p = _make_portfolio(db_session, name="QA-Second")
        # Day1: 100000
        upsert_snapshot(
            db_session,
            portfolio_id=p.id,
            snapshot_date=date(2026, 7, 18),
            cash_balance=100000.0,
            market_value=0.0,
            total_equity=100000.0,
            realized_pnl=0.0,
            unrealized_pnl=0.0,
            position_count=0,
        )
        db_session.commit()
        # Day2: 105000（+5%）
        snap2 = upsert_snapshot(
            db_session,
            portfolio_id=p.id,
            snapshot_date=date(2026, 7, 19),
            cash_balance=50000.0,
            market_value=55000.0,
            total_equity=105000.0,
            realized_pnl=500.0,
            unrealized_pnl=4500.0,
            position_count=2,
        )
        db_session.commit()
        # 5% 收益率
        assert snap2.daily_return == pytest.approx(0.05, abs=1e-4)
        assert snap2.position_count == 2

    def test_same_day_repeat_upsert_updates_existing(self, db_session):
        """【P0-9 测试】同日重复调用应更新已有记录（不抛唯一约束错误）。"""
        p = _make_portfolio(db_session, name="QA-Upsert")
        snap1 = upsert_snapshot(
            db_session,
            portfolio_id=p.id,
            snapshot_date=date(2026, 7, 18),
            cash_balance=100000.0,
            market_value=0.0,
            total_equity=100000.0,
            realized_pnl=0.0,
            unrealized_pnl=0.0,
            position_count=0,
        )
        db_session.commit()
        original_id = snap1.id

        # 同日重写，total_equity 变化
        snap2 = upsert_snapshot(
            db_session,
            portfolio_id=p.id,
            snapshot_date=date(2026, 7, 18),
            cash_balance=95000.0,
            market_value=6000.0,
            total_equity=101000.0,
            realized_pnl=100.0,
            unrealized_pnl=900.0,
            position_count=1,
        )
        db_session.commit()

        # id 应保持不变（更新而非新增）
        assert snap2.id == original_id
        # DB 中只该有一条
        rows = db_session.query(PortfolioEquitySnapshot).filter_by(portfolio_id=p.id).all()
        assert len(rows) == 1
        # values 应被更新
        assert rows[0].total_equity == 101000.0
        assert rows[0].position_count == 1

    def test_prev_zero_equity_daily_return_is_zero(self, db_session):
        """【P0-9 测试】前一日 total_equity=0 时 daily_return 应兜底为 0（避免除零）。"""
        p = _make_portfolio(db_session, name="QA-Zero")
        upsert_snapshot(
            db_session,
            portfolio_id=p.id,
            snapshot_date=date(2026, 7, 18),
            cash_balance=0.0,
            market_value=0.0,
            total_equity=0.0,
            realized_pnl=0.0,
            unrealized_pnl=0.0,
            position_count=0,
        )
        db_session.commit()
        # Day2: 突然有 100，不应除零
        snap2 = upsert_snapshot(
            db_session,
            portfolio_id=p.id,
            snapshot_date=date(2026, 7, 19),
            cash_balance=100.0,
            market_value=0.0,
            total_equity=100.0,
            realized_pnl=0.0,
            unrealized_pnl=0.0,
            position_count=0,
        )
        db_session.commit()
        assert snap2.daily_return == 0.0  # 兜底


# ============================================================================
# 5-6. snapshot_all_simulated_portfolios
# ============================================================================

class TestSnapshotAllSimulated:
    """守护批量快照的隔离与过滤行为。"""

    def test_only_simulated_portfolios_are_snapshotted(self, db_session):
        """【P0-9 测试】account_type != 'simulated' 的组合不应被写入快照。"""
        sim_p = _make_portfolio(db_session, name="QA-Sim", account_type="simulated")
        real_p = _make_portfolio(db_session, name="QA-Real", account_type="real")

        result = snapshot_all_simulated_portfolios(db_session)

        assert result["total"] == 1  # 只有 1 个 simulated
        assert result["written"] == 1
        assert result["skipped"] == 0
        # 验证 DB 中只有 sim_p 的快照
        all_snaps = db_session.query(PortfolioEquitySnapshot).all()
        assert len(all_snaps) == 1
        assert all_snaps[0].portfolio_id == sim_p.id
        # real_p 不应有快照
        real_snaps = db_session.query(PortfolioEquitySnapshot).filter_by(portfolio_id=real_p.id).all()
        assert len(real_snaps) == 0

    def test_single_portfolio_failure_does_not_block_others(self, db_session):
        """【P0-9 测试】单个组合 build_sim_account_summary 失败不应影响其他组合。"""
        p1 = _make_portfolio(db_session, name="QA-OK")
        p2 = _make_portfolio(db_session, name="QA-Fail")
        p3 = _make_portfolio(db_session, name="QA-OK2")

        # mock build_sim_account_summary：对 p2 抛异常，对其他正常返回
        original = __import__("app.services.portfolio_equity_snapshot", fromlist=["build_sim_account_summary"]).build_sim_account_summary

        def fake_summary(db, portfolio):
            if portfolio.id == p2.id:
                raise RuntimeError("Mocked failure for p2")
            return original(db, portfolio)

        with patch(
            "app.services.portfolio_equity_snapshot.build_sim_account_summary",
            side_effect=fake_summary,
        ):
            result = snapshot_all_simulated_portfolios(db_session)

        # 3 个 simulated，1 个失败
        assert result["total"] == 3
        assert result["written"] == 2  # p1, p3 成功
        assert result["skipped"] == 1
        assert len(result["errors"]) == 1
        assert result["errors"][0]["portfolio_id"] == p2.id
        # p1, p3 在 DB 中有快照，p2 没有
        p1_snaps = db_session.query(PortfolioEquitySnapshot).filter_by(portfolio_id=p1.id).all()
        p2_snaps = db_session.query(PortfolioEquitySnapshot).filter_by(portfolio_id=p2.id).all()
        p3_snaps = db_session.query(PortfolioEquitySnapshot).filter_by(portfolio_id=p3.id).all()
        assert len(p1_snaps) == 1
        assert len(p2_snaps) == 0
        assert len(p3_snaps) == 1


# ============================================================================
# 7. list_portfolio_equity_snapshots 过滤与排序
# ============================================================================

class TestListSnapshots:
    """守护查询接口的日期过滤、升序、limit。"""

    def _seed_snapshots(self, db_session, portfolio_id):
        """造 5 天快照：7-15 ~ 7-19。"""
        for i, day in enumerate([15, 16, 17, 18, 19]):
            upsert_snapshot(
                db_session,
                portfolio_id=portfolio_id,
                snapshot_date=date(2026, 7, day),
                cash_balance=100000.0 + i * 100,
                market_value=float(i * 1000),
                total_equity=100000.0 + i * 1100,
                realized_pnl=float(i * 50),
                unrealized_pnl=float(i * 500),
                position_count=i,
            )
        db_session.commit()

    def test_returns_ascending_by_date(self, db_session):
        """【P0-10 测试】默认按日期升序返回。"""
        p = _make_portfolio(db_session, name="QA-List")
        self._seed_snapshots(db_session, p.id)

        snaps = list_portfolio_equity_snapshots(db_session, p.id)

        assert len(snaps) == 5
        dates = [s.snapshot_date for s in snaps]
        assert dates == sorted(dates)
        assert dates[0] == date(2026, 7, 15)
        assert dates[-1] == date(2026, 7, 19)

    def test_start_date_filter(self, db_session):
        """【P0-10 测试】start_date 过滤包含当日。"""
        p = _make_portfolio(db_session, name="QA-ListStart")
        self._seed_snapshots(db_session, p.id)

        snaps = list_portfolio_equity_snapshots(db_session, p.id, start_date=date(2026, 7, 17))

        assert len(snaps) == 3  # 17, 18, 19
        assert snaps[0].snapshot_date == date(2026, 7, 17)
        assert snaps[-1].snapshot_date == date(2026, 7, 19)

    def test_end_date_filter(self, db_session):
        """【P0-10 测试】end_date 过滤包含当日。"""
        p = _make_portfolio(db_session, name="QA-ListEnd")
        self._seed_snapshots(db_session, p.id)

        snaps = list_portfolio_equity_snapshots(db_session, p.id, end_date=date(2026, 7, 17))

        assert len(snaps) == 3  # 15, 16, 17
        assert snaps[0].snapshot_date == date(2026, 7, 15)
        assert snaps[-1].snapshot_date == date(2026, 7, 17)

    def test_limit_truncates(self, db_session):
        """【P0-10 测试】limit 截断（按升序保留前 N 条）。"""
        p = _make_portfolio(db_session, name="QA-ListLimit")
        self._seed_snapshots(db_session, p.id)

        snaps = list_portfolio_equity_snapshots(db_session, p.id, limit=2)

        assert len(snaps) == 2
        assert snaps[0].snapshot_date == date(2026, 7, 15)
        assert snaps[1].snapshot_date == date(2026, 7, 16)

    def test_empty_portfolio_returns_empty_list(self, db_session):
        """【P0-10 测试】无快照组合返回空列表（不抛异常）。"""
        p = _make_portfolio(db_session, name="QA-Empty")
        snaps = list_portfolio_equity_snapshots(db_session, p.id)
        assert snaps == []


# ============================================================================
# 8. snapshot_to_dict 序列化
# ============================================================================

class TestSnapshotToDict:
    """守护序列化字段完整、类型正确。"""

    def test_dict_has_all_required_fields(self, db_session):
        """【P0-10 测试】序列化输出包含前端所需全部字段。"""
        p = _make_portfolio(db_session, name="QA-Dict")
        snap = upsert_snapshot(
            db_session,
            portfolio_id=p.id,
            snapshot_date=date(2026, 7, 18),
            cash_balance=100000.0,
            market_value=5000.0,
            total_equity=105000.0,
            realized_pnl=200.0,
            unrealized_pnl=4800.0,
            position_count=1,
        )
        db_session.commit()

        d = snapshot_to_dict(snap)

        required = {
            "id", "portfolio_id", "snapshot_date",
            "cash_balance", "market_value", "total_equity",
            "realized_pnl", "unrealized_pnl", "daily_return",
            "position_count", "created_at",
        }
        assert required.issubset(d.keys())
        # 类型正确性
        assert d["portfolio_id"] == p.id
        assert d["snapshot_date"] == "2026-07-18"
        assert isinstance(d["cash_balance"], float)
        assert isinstance(d["position_count"], int)
        assert d["total_equity"] == 105000.0

    def test_dict_handles_none_fields(self, db_session):
        """【P0-10 测试】None 字段应兜底为 0/None，不抛 TypeError。"""
        p = _make_portfolio(db_session, name="QA-DictNone")
        # 直接构造一个未 flush 的实例，部分字段为 None
        snap = PortfolioEquitySnapshot(
            portfolio_id=p.id,
            snapshot_date=date(2026, 7, 18),
            cash_balance=None,
            market_value=None,
            total_equity=None,
            realized_pnl=None,
            unrealized_pnl=None,
            daily_return=None,
            position_count=None,
        )
        d = snapshot_to_dict(snap)
        assert d["cash_balance"] == 0.0
        assert d["position_count"] == 0
        assert d["total_equity"] == 0.0
        assert math.isfinite(d["daily_return"])


# ============================================================================
# 9. create_portfolio_equity_snapshot_task 去重
# ============================================================================

class TestCreateSnapshotTask:
    """守护任务去重逻辑。"""

    def test_returns_existing_queued_task(self):
        """【P0-9 测试】已有 queued 任务时应返回现有任务，不创建新任务。"""
        existing_task = MagicMock()
        existing_task.status = "queued"
        with patch(
            "app.services.portfolio_equity_snapshot.list_async_tasks",
            return_value=[existing_task],
        ) as mock_list, patch(
            "app.services.portfolio_equity_snapshot.create_async_task"
        ) as mock_create, patch(
            "app.services.portfolio_equity_snapshot._start_worker"
        ) as mock_start:
            result = create_portfolio_equity_snapshot_task()

        assert result is existing_task
        mock_create.assert_not_called()
        mock_start.assert_not_called()

    def test_returns_existing_running_task(self):
        """【P0-9 测试】已有 running 任务时也应返回现有任务。"""
        existing_task = MagicMock()
        existing_task.status = "running"
        with patch(
            "app.services.portfolio_equity_snapshot.list_async_tasks",
            return_value=[existing_task],
        ), patch(
            "app.services.portfolio_equity_snapshot.create_async_task"
        ) as mock_create, patch(
            "app.services.portfolio_equity_snapshot._start_worker"
        ) as mock_start:
            result = create_portfolio_equity_snapshot_task()

        assert result is existing_task
        mock_create.assert_not_called()
        mock_start.assert_not_called()

    def test_creates_new_task_when_none_running(self):
        """【P0-9 测试】无 queued/running 任务时应创建新任务并启动 worker。"""
        new_task = MagicMock()
        new_task.id = "new-task-id"
        new_task.status = "queued"
        with patch(
            "app.services.portfolio_equity_snapshot.list_async_tasks",
            return_value=[],
        ), patch(
            "app.services.portfolio_equity_snapshot.create_async_task",
            return_value=new_task,
        ) as mock_create, patch(
            "app.services.portfolio_equity_snapshot._start_worker"
        ) as mock_start:
            result = create_portfolio_equity_snapshot_task()

        assert result is new_task
        mock_create.assert_called_once_with(TASK_TYPE, {})
        mock_start.assert_called_once()
        # worker 函数应为模块内的 _run_snapshot_task
        assert mock_start.call_args[0][1].__name__ == "_run_snapshot_task"


# ============================================================================
# 10. scheduled_tasks 接入
# ============================================================================

class TestScheduledTasksIntegration:
    """守护 portfolio_equity_snapshot 在调度框架内的注册与校验。"""

    def test_task_definition_registered(self):
        """【P0-9 测试】TASK_DEFINITIONS 应包含 portfolio_equity_snapshot。"""
        from app.services.scheduled_tasks import TASK_DEFINITIONS

        assert "portfolio_equity_snapshot" in TASK_DEFINITIONS
        definition = TASK_DEFINITIONS["portfolio_equity_snapshot"]
        assert "name" in definition
        assert "description" in definition
        assert definition["default_payload"] == {}

    def test_validate_payload_empty_dict(self):
        """【P0-9 测试】空 payload 应通过校验。"""
        from app.services.scheduled_tasks import validate_task_payload

        result = validate_task_payload("portfolio_equity_snapshot", {})
        assert result == {}

    def test_validate_payload_rejects_non_empty(self):
        """【P0-9 测试】非空 payload 应被拒绝（避免误传参数）。"""
        from app.services.scheduled_tasks import validate_task_payload

        with pytest.raises(ValueError, match="payload must be empty"):
            validate_task_payload("portfolio_equity_snapshot", {"unexpected": True})

    def test_default_schedules_contains_snapshot(self):
        """【P0-9 测试】DEFAULT_SCHEDULES 应包含每日组合净值快照条目。"""
        from app.services.scheduled_tasks import DEFAULT_SCHEDULES

        snapshot_schedules = [s for s in DEFAULT_SCHEDULES if s["task_type"] == "portfolio_equity_snapshot"]
        assert len(snapshot_schedules) == 1
        schedule = snapshot_schedules[0]
        assert schedule["frequency"] == "daily"
        assert schedule["time_of_day"] == "16:00"
        assert schedule["enabled"] is True

    def test_dispatch_routes_to_snapshot_task(self, db_session):
        """【P0-9 测试】_dispatch_task 应路由到 create_portfolio_equity_snapshot_task。"""
        from app.services.scheduled_tasks import _dispatch_task
        from app.models.scheduled_task import ScheduledTask

        item = ScheduledTask(
            name="每日组合净值快照",
            task_type="portfolio_equity_snapshot",
            frequency="daily",
            time_of_day="16:00",
            weekdays_json="[]",
            timezone="Asia/Shanghai",
            payload_json="{}",
            enabled=1,
        )

        mock_task = MagicMock()
        mock_task.id = "test-task-id"
        mock_task.status = "queued"
        mock_task.message = "queued"

        with patch(
            "app.services.portfolio_equity_snapshot.create_portfolio_equity_snapshot_task",
            return_value=mock_task,
        ) as mock_create:
            source, task = _dispatch_task(item)

        assert source == "async"
        assert task is mock_task
        mock_create.assert_called_once()
