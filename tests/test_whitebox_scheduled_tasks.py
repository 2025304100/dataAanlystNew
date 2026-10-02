import json
from datetime import datetime
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.db.session import get_db
from app.models.scheduled_task import ScheduledTask, ScheduledTaskRun
from app.schemas.scheduled_task import ScheduledTaskCreate, ScheduledTaskUpdate
from app.services import scheduled_tasks


import pytest

pytestmark = pytest.mark.whitebox
def test_calculate_next_daily_and_weekly_run_in_configured_timezone():
    after = datetime(2026, 7, 13, 9, 0)  # 17:00 Asia/Shanghai
    daily = scheduled_tasks.calculate_next_run(
        frequency="daily",
        time_of_day="18:00",
        weekdays=[],
        interval_minutes=None,
        timezone_name="Asia/Shanghai",
        after_utc=after,
    )
    weekly = scheduled_tasks.calculate_next_run(
        frequency="weekly",
        time_of_day="18:30",
        weekdays=[4],
        interval_minutes=None,
        timezone_name="Asia/Shanghai",
        after_utc=after,
    )

    assert daily == datetime(2026, 7, 13, 10, 0)
    assert weekly == datetime(2026, 7, 17, 10, 30)


def test_seed_default_schedules_is_idempotent(db_session):
    assert scheduled_tasks.seed_default_schedules(db_session) == 14
    db_session.commit()
    assert scheduled_tasks.seed_default_schedules(db_session) == 0
    rows = db_session.query(ScheduledTask).all()
    assert len(rows) == 14
    enabled = [item for item in rows if item.enabled]
    # 默认启用：行情增量同步 + 组合净值快照 + 指数日线同步（自动交易默认关闭，需用户主动开启）
    assert {item.task_type for item in enabled} == {
        "universe_incremental_sync",
        "portfolio_equity_snapshot",
        "index_daily_sync",
        "external_data_sync",
    }
    assert all(item.next_run_at is not None for item in enabled)
    # P2-3：portfolio_auto_trade 应在默认调度中且默认关闭
    auto_trade = [r for r in rows if r.task_type == "portfolio_auto_trade"]
    assert len(auto_trade) == 1
    assert auto_trade[0].enabled == 0
    assert auto_trade[0].next_run_at is None


def test_deleted_or_renamed_defaults_are_not_reseeded(db_session):
    assert scheduled_tasks.seed_default_schedules(db_session) == 14
    db_session.commit()
    item = db_session.query(ScheduledTask).filter_by(name="每日宏观数据更新").one()
    scheduled_tasks.delete_schedule(db_session, item.id)

    assert scheduled_tasks.seed_default_schedules(db_session) == 0
    assert db_session.query(ScheduledTask).filter_by(name="每日宏观数据更新").count() == 0


def _seed_legacy_defaults_only(db_session) -> None:
    """Simulate a database already seeded by the previous default schedule set."""
    from app.models.scheduled_task import ScheduledTaskSeedState

    for data in scheduled_tasks.DEFAULT_SCHEDULES:
        if data["name"] == "每日因子资金流增量同步":
            continue
        payload = ScheduledTaskCreate(**data, timezone="Asia/Shanghai")
        db_session.add(ScheduledTask(
            name=payload.name,
            task_type=payload.task_type,
            frequency=payload.frequency,
            time_of_day=payload.time_of_day,
            weekdays_json=json.dumps(payload.weekdays),
            interval_minutes=payload.interval_minutes,
            timezone=payload.timezone,
            payload_json=json.dumps(payload.payload, ensure_ascii=False),
            enabled=int(payload.enabled),
        ))
    db_session.add(ScheduledTaskSeedState(key="default_schedules_v7"))
    db_session.commit()


def test_capital_flow_daily_schedule_is_seeded_once(db_session):
    assert scheduled_tasks.seed_default_schedules(db_session) == 14
    db_session.commit()

    item = db_session.query(ScheduledTask).filter_by(name="每日因子资金流增量同步").one()
    assert item.enabled == 1
    assert item.time_of_day == "19:10"
    assert json.loads(item.payload_json) == {
        "dataset": "capital_flow",
        "source": "all",
        "include_northbound": False,
        "mode": "incremental",
        "lookback_days": 1,
    }


def test_seed_key_bump_adds_capital_flow_to_a_legacy_seeded_db(db_session):
    _seed_legacy_defaults_only(db_session)

    created = scheduled_tasks.seed_default_schedules(db_session)
    db_session.commit()

    assert created == 1
    assert db_session.query(ScheduledTask).filter_by(
        name="每日因子资金流增量同步"
    ).count() == 1
    assert db_session.query(ScheduledTask).count() == 14


def test_capital_flow_schedule_validates_and_dispatches(monkeypatch):
    payload = scheduled_tasks.validate_task_payload(
        "external_data_sync",
        {"dataset": "capital_flow", "source": "all", "include_northbound": False},
    )
    assert payload == {
        "dataset": "capital_flow",
        "source": "all",
        "include_northbound": False,
        "mode": "incremental",
        "lookback_days": 1,
    }

    captured = {}
    monkeypatch.setattr(
        "app.services.external_data_sync_task.start_external_data_sync",
        lambda dataset, task_payload: captured.update(dataset=dataset, payload=task_payload)
        or SimpleNamespace(id="external-2", status="queued", message="queued"),
    )
    item = SimpleNamespace(
        task_type="external_data_sync",
        payload_json=json.dumps(payload),
    )

    source, task = scheduled_tasks._dispatch_task(item)

    assert source == "async"
    assert task.id == "external-2"
    assert captured == {"dataset": "capital_flow", "payload": payload}


def test_scheduled_external_sync_still_rejects_unschedulable_datasets():
    with pytest.raises(ValueError, match="fundamental and capital_flow"):
        scheduled_tasks.validate_task_payload(
            "external_data_sync", {"dataset": "financial", "source": "all"}
        )


def test_external_data_schedule_validates_and_dispatches_incremental_sync(monkeypatch):
    payload = scheduled_tasks.validate_task_payload(
        "external_data_sync",
        {"dataset": "fundamental", "source": "all", "include_northbound": False},
    )
    assert payload == {
        "dataset": "fundamental",
        "source": "all",
        "include_northbound": False,
        "mode": "incremental",
        "lookback_days": 1,
    }

    captured = {}
    monkeypatch.setattr(
        "app.services.external_data_sync_task.start_external_data_sync",
        lambda dataset, task_payload: captured.update(dataset=dataset, payload=task_payload)
        or SimpleNamespace(id="external-1", status="queued", message="queued"),
    )
    item = SimpleNamespace(
        task_type="external_data_sync",
        payload_json=__import__("json").dumps(payload),
    )

    source, task = scheduled_tasks._dispatch_task(item)

    assert source == "async"
    assert task.id == "external-1"
    assert captured == {"dataset": "fundamental", "payload": payload}


def test_portfolio_auto_trade_payload_validation(db_session):
    """P2-3：portfolio_auto_trade payload 校验。"""
    # 合法 payload
    result = scheduled_tasks.validate_task_payload(
        "portfolio_auto_trade", {"dry_run": False, "buy_candidate_limit": 10}
    )
    assert result == {"dry_run": False, "buy_candidate_limit": 10}
    # 默认值
    result = scheduled_tasks.validate_task_payload("portfolio_auto_trade", {})
    assert result == {"dry_run": False, "buy_candidate_limit": 10}
    # buy_candidate_limit 越界
    try:
        scheduled_tasks.validate_task_payload(
            "portfolio_auto_trade", {"buy_candidate_limit": 0}
        )
    except ValueError as exc:
        assert "buy_candidate_limit" in str(exc)
    else:
        raise AssertionError("buy_candidate_limit=0 must be rejected")
    try:
        scheduled_tasks.validate_task_payload(
            "portfolio_auto_trade", {"buy_candidate_limit": 51}
        )
    except ValueError as exc:
        assert "buy_candidate_limit" in str(exc)
    else:
        raise AssertionError("buy_candidate_limit=51 must be rejected")


def test_index_daily_sync_payload_validation(db_session):
    """P3+：index_daily_sync payload 校验。"""
    # 合法 payload
    result = scheduled_tasks.validate_task_payload(
        "index_daily_sync", {"symbol": "000300", "lookback_days": 5}
    )
    assert result == {"symbol": "000300", "lookback_days": 5}
    # 默认值
    result = scheduled_tasks.validate_task_payload("index_daily_sync", {})
    assert result == {"symbol": "000300", "lookback_days": 5}
    # symbol 空串
    try:
        scheduled_tasks.validate_task_payload("index_daily_sync", {"symbol": ""})
    except ValueError as exc:
        assert "symbol" in str(exc)
    else:
        raise AssertionError("symbol='' must be rejected")
    # lookback_days 越界
    try:
        scheduled_tasks.validate_task_payload("index_daily_sync", {"lookback_days": 0})
    except ValueError as exc:
        assert "lookback_days" in str(exc)
    else:
        raise AssertionError("lookback_days=0 must be rejected")
    try:
        scheduled_tasks.validate_task_payload("index_daily_sync", {"lookback_days": 366})
    except ValueError as exc:
        assert "lookback_days" in str(exc)
    else:
        raise AssertionError("lookback_days=366 must be rejected")


def test_schedule_create_update_and_delete(db_session):
    item = scheduled_tasks.create_schedule(
        db_session,
        ScheduledTaskCreate(
            name="Macro every hour",
            task_type="macro_update",
            frequency="interval",
            time_of_day=None,
            interval_minutes=60,
            payload={"region": "all"},
        ),
    )
    assert item.next_run_at is not None

    item = scheduled_tasks.update_schedule(
        db_session,
        item.id,
        ScheduledTaskUpdate(enabled=False, name="Paused macro"),
    )
    assert item.name == "Paused macro"
    assert item.enabled == 0
    assert item.next_run_at is None

    scheduled_tasks.delete_schedule(db_session, item.id)
    assert db_session.get(ScheduledTask, item.id) is None


def test_manual_execute_records_auditable_run(db_session, monkeypatch):
    item = scheduled_tasks.create_schedule(
        db_session,
        ScheduledTaskCreate(
            name="Manual macro",
            task_type="macro_update",
            frequency="daily",
            time_of_day="17:30",
            payload={"region": "all"},
            enabled=False,
        ),
    )
    monkeypatch.setattr(
        scheduled_tasks,
        "_dispatch_task",
        lambda schedule: (
            "async",
            SimpleNamespace(id="task-123", status="queued", message="queued"),
        ),
    )

    run = scheduled_tasks.execute_schedule(
        db_session,
        item.id,
        trigger_source="manual",
    )
    db_session.refresh(item)

    assert run.task_id == "task-123"
    assert run.trigger_source == "manual"
    assert item.last_task_id == "task-123"
    assert item.last_error is None
    assert db_session.query(ScheduledTaskRun).count() == 1


def test_manual_execute_accepts_dict_task_result(db_session, monkeypatch):
    item = scheduled_tasks.create_schedule(
        db_session,
        ScheduledTaskCreate(
            name="Factor task",
            task_type="factor_pipeline",
            frequency="daily",
            time_of_day="18:10",
            payload={
                "train_model": False,
                "materialize_scores": True,
                "window_days": 250,
                "validation_days": 50,
            },
            enabled=False,
        ),
    )
    monkeypatch.setattr(
        scheduled_tasks,
        "_dispatch_task",
        lambda schedule: (
            "async",
            {"id": "factor-123", "status": "queued", "message": "queued"},
        ),
    )

    run = scheduled_tasks.execute_schedule(
        db_session,
        item.id,
        trigger_source="manual",
    )

    assert run.task_id == "factor-123"
    assert run.status == "queued"


def test_due_schedule_is_claimed_once(db_session, monkeypatch):
    item = scheduled_tasks.create_schedule(
        db_session,
        ScheduledTaskCreate(
            name="Due macro",
            task_type="macro_update",
            frequency="daily",
            time_of_day="17:30",
            payload={"region": "all"},
        ),
    )
    item.next_run_at = datetime(2020, 1, 1)
    db_session.commit()
    calls = []
    monkeypatch.setattr(
        scheduled_tasks,
        "execute_schedule",
        lambda db, schedule_id, trigger_source: calls.append(
            (schedule_id, trigger_source)
        ),
    )

    assert scheduled_tasks.run_due_schedules() == 1
    assert scheduled_tasks.run_due_schedules() == 0
    assert calls == [(item.id, "scheduled")]


def test_invalid_task_payload_is_rejected_when_schedule_is_saved(db_session):
    try:
        scheduled_tasks.create_schedule(
            db_session,
            ScheduledTaskCreate(
                name="Invalid factor task",
                task_type="factor_pipeline",
                frequency="daily",
                time_of_day="18:10",
                payload={"window_days": 60, "validation_days": 60},
            ),
        )
    except ValueError as exc:
        assert "validation_days" in str(exc)
    else:
        raise AssertionError("invalid factor payload must be rejected")


def test_scheduled_task_routes_are_registered():
    from app.main import app

    paths = {route.path for route in app.routes}
    assert "/api/v1/scheduled-tasks" in paths
    assert "/api/v1/scheduled-tasks/{schedule_id}/run" in paths
    assert "/api/v1/scheduled-tasks/runs" in paths


def test_scheduled_task_api_crud_and_manual_run(db_session, monkeypatch):
    from app.main import app

    def override_get_db():
        yield db_session

    monkeypatch.setattr(
        scheduled_tasks,
        "_dispatch_task",
        lambda schedule: (
            "async",
            SimpleNamespace(id="api-task-123", status="queued", message="queued"),
        ),
    )
    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)
    try:
        definitions = client.get("/api/v1/scheduled-tasks/definitions")
        assert definitions.status_code == 200
        assert {item["task_type"] for item in definitions.json()} >= {
            "universe_incremental_sync",
            "macro_update",
            "factor_pipeline",
            "hot_rank_snapshot",
            "tail_proxy_snapshot",
            "lhb_institution_sync",
            "financial_report_sync",
            "discovery_mining",
        }

        created = client.post(
            "/api/v1/scheduled-tasks",
            json={
                "name": "API macro task",
                "task_type": "macro_update",
                "frequency": "daily",
                "time_of_day": "17:30",
                "timezone": "Asia/Shanghai",
                "payload": {"region": "all"},
                "enabled": True,
            },
        )
        assert created.status_code == 200
        schedule_id = created.json()["id"]
        assert created.json()["next_run_at"] is not None

        listed = client.get("/api/v1/scheduled-tasks")
        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()] == [schedule_id]

        disabled = client.patch(
            f"/api/v1/scheduled-tasks/{schedule_id}",
            json={"enabled": False},
        )
        assert disabled.status_code == 200
        assert disabled.json()["enabled"] is False
        assert disabled.json()["next_run_at"] is None

        executed = client.post(f"/api/v1/scheduled-tasks/{schedule_id}/run")
        assert executed.status_code == 200
        assert executed.json()["task_id"] == "api-task-123"
        assert executed.json()["trigger_source"] == "manual"

        runs = client.get("/api/v1/scheduled-tasks/runs?limit=10")
        assert runs.status_code == 200
        assert runs.json()[0]["schedule_id"] == schedule_id

        deleted = client.delete(f"/api/v1/scheduled-tasks/{schedule_id}")
        assert deleted.status_code == 200
        assert deleted.json() == {"status": "deleted", "id": schedule_id}
        assert client.get(f"/api/v1/scheduled-tasks/{schedule_id}").status_code == 404
    finally:
        app.dependency_overrides.pop(get_db, None)


def _due_schedule(db_session, name: str, task_type: str = "hot_rank_snapshot"):
    from datetime import datetime, timedelta, timezone

    item = scheduled_tasks.ScheduledTask(
        name=name,
        task_type=task_type,
        frequency="daily",
        time_of_day="19:10",
        weekdays_json="[]",
        timezone="Asia/Shanghai",
        payload_json=json.dumps({}),
        enabled=1,
        next_run_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=5),
    )
    db_session.add(item)
    db_session.commit()
    return item


def test_dispatch_failure_retries_within_the_day(db_session, monkeypatch):
    """先推进 next_run_at 再派发，所以派发失败必须回拨，否则这一天被无声吃掉。"""
    from datetime import datetime, timedelta, timezone

    item = _due_schedule(db_session, "资金流日更撞前台同步")
    monkeypatch.setattr(
        scheduled_tasks, "_dispatch_task",
        lambda *_a: (_ for _ in ()).throw(RuntimeError("前台行情同步进行中，拒绝创建")),
    )

    scheduled_tasks.run_due_schedules()

    db_session.expire_all()
    reloaded = db_session.query(scheduled_tasks.ScheduledTask).filter_by(id=item.id).one()
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    assert reloaded.last_status == "failed"
    assert "前台行情同步进行中" in (reloaded.last_error or "")
    delta = (reloaded.next_run_at - now_utc).total_seconds()
    assert 10 * 60 <= delta <= 20 * 60, reloaded.next_run_at
    assert reloaded.next_run_at.date() == now_utc.date()


def test_one_unexpected_failure_does_not_skip_other_due_schedules(db_session, monkeypatch):
    """一条调度抛非 ValueError 时，同一轮里其它到期任务还得被尝试。"""
    from sqlalchemy.exc import OperationalError

    first = _due_schedule(db_session, "先炸的那条")
    second = _due_schedule(db_session, "应该照跑的那条")
    attempted: list[int] = []

    def fake_execute(db, schedule_id, *, trigger_source):
        attempted.append(schedule_id)
        if schedule_id == first.id:
            raise OperationalError("SELECT 1", {}, Exception("connection reset"))
        return SimpleNamespace(id=schedule_id)

    monkeypatch.setattr(scheduled_tasks, "execute_schedule", fake_execute)
    count = scheduled_tasks.run_due_schedules()

    assert count == 2
    assert sorted(attempted) == sorted([first.id, second.id])


def test_dispatch_retry_gives_up_after_the_daily_limit(db_session, monkeypatch):
    """连续失败不能变成每 15 分钟一次的空转；到上限就让位给下一个正常时段。"""
    from datetime import datetime, timedelta, timezone

    item = _due_schedule(db_session, "一直失败的那条")
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    for _ in range(scheduled_tasks.DISPATCH_MAX_RETRIES_PER_DAY):
        db_session.add(scheduled_tasks.ScheduledTaskRun(
            schedule_id=item.id,
            trigger_source="scheduled",
            task_source="async",
            status="failed",
            message="前台行情同步进行中，拒绝创建",
            created_at=now_utc - timedelta(minutes=1),
        ))
    db_session.commit()
    monkeypatch.setattr(
        scheduled_tasks, "_dispatch_task",
        lambda *_a: (_ for _ in ()).throw(RuntimeError("前台行情同步进行中，拒绝创建")),
    )

    scheduled_tasks.run_due_schedules()

    db_session.expire_all()
    reloaded = db_session.query(scheduled_tasks.ScheduledTask).filter_by(id=item.id).one()
    delta = (reloaded.next_run_at - now_utc).total_seconds()
    assert delta > 20 * 60, reloaded.next_run_at


def test_failure_reason_is_not_masked_by_a_secondary_db_error(db_session, monkeypatch):
    """记账那次 commit 也炸时，抛出去的必须还是原始原因，不是数据库错误。"""
    from sqlalchemy.exc import OperationalError

    item = _due_schedule(db_session, "记账也失败的那条")
    monkeypatch.setattr(
        scheduled_tasks, "_dispatch_task",
        lambda *_a: (_ for _ in ()).throw(RuntimeError("原始原因：优先级冲突")),
    )
    real_commit = db_session.commit
    commits: list[int] = []

    def flaky_commit():
        commits.append(1)
        if len(commits) >= 2:
            raise OperationalError("UPDATE scheduled_tasks", {}, Exception("connection gone"))
        return real_commit()

    monkeypatch.setattr(db_session, "commit", flaky_commit)

    with pytest.raises(ValueError, match="原始原因：优先级冲突"):
        scheduled_tasks.execute_schedule(db_session, item.id, trigger_source="scheduled")

    assert len(commits) >= 2


def test_failure_reason_is_rewritten_from_a_clean_session(db_session, monkeypatch):
    """派发那个会话已被拖垮时，原因和 run 状态仍要落账（实测它们曾全部停在 dispatching）。"""
    from datetime import datetime, timedelta, timezone

    from app.models.scheduled_task import ScheduledTaskRun

    item = _due_schedule(db_session, "会话脏了的那条")
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    run = ScheduledTaskRun(
        schedule_id=item.id,
        trigger_source="scheduled",
        status="dispatching",
        message="Dispatching task",
        created_at=now_utc,
    )
    db_session.add(run)
    db_session.commit()
    run_id = run.id

    def poisoned_execute(db, schedule_id, *, trigger_source):
        raise scheduled_tasks.ScheduleDispatchError(
            "已有更高优先级任务在前台运行", schedule_id=schedule_id, run_id=run_id
        )

    monkeypatch.setattr(scheduled_tasks, "execute_schedule", poisoned_execute)

    scheduled_tasks.run_due_schedules()

    db_session.expire_all()
    reloaded = db_session.query(scheduled_tasks.ScheduledTask).filter_by(id=item.id).one()
    reloaded_run = db_session.query(ScheduledTaskRun).filter_by(id=run_id).one()
    assert reloaded.last_status == "failed"
    assert reloaded.last_error == "已有更高优先级任务在前台运行"
    assert reloaded.last_run_at is not None
    assert reloaded_run.status == "failed"
    assert reloaded_run.message == "已有更高优先级任务在前台运行"
    delta = (reloaded.next_run_at - datetime.now(timezone.utc).replace(tzinfo=None)).total_seconds()
    assert 10 * 60 <= delta <= 20 * 60
