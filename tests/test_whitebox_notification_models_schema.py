"""白盒测试 - WP-MSG.1 通知数据模型。

覆盖 6 张表的 schema、默认值、唯一约束、部分唯一索引、级联删除、
SQLite/MySQL schema 补丁幂等性、敏感字段脱敏等。

测试维度：
1.  6 张表存在性（init_db 后表存在）
2.  NotificationChannel 字段完整性
3.  NotificationChannel 默认值（status='unconfigured', enabled=False）
4.  NotificationChannel channel_type 枚举
5.  NotificationChannel status 枚举
6.  NotificationChannel 敏感字段（加密 + 脱敏）
7.  NotificationPolicy 字段完整性
8.  NotificationPolicy 默认值（enabled=False, min_severity='info', delivery_mode='instant'）
9.  NotificationPolicy source_types_json 读写
10. NotificationPolicyChannel 唯一约束 (policy_id, channel_id)
11. NotificationPolicyChannel 级联删除（删 policy 关联记录被删）
12. NotificationOutbox 字段完整性
13. NotificationOutbox 默认值（status='pending', attempt_count=0, max_attempts=5）
14. NotificationOutbox event_key + channel_id 部分唯一索引
15. NotificationDelivery 字段完整性
16. NotificationDelivery 级联删除（删 outbox 关联 delivery 被删）
17. NotificationTemplate 字段完整性
18. NotificationTemplate 默认值（version=1, is_active=True）
19. schema 补丁幂等（多次调用不报错）
20. MySQL 补丁函数存在且可调用
21. 索引存在性（idx_no_event_channel_pending）
22. 脱敏函数（mask_token / mask_webhook_secret / mask_smtp_password / mask_url）
23. 脱敏不暴露完整值（config_mask_json 中无完整 Token/Secret/密码）
"""
from __future__ import annotations

import json
import os
import tempfile

import pytest
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.init_db import (
    _ensure_mysql_notification_tables,
    _ensure_sqlite_notification_outbox_unique_index,
    _ensure_sqlite_notification_tables,
    init_db,
)
from app.models.notification import (
    CHANNEL_STATUSES,
    CHANNEL_STATUS_DISABLED,
    CHANNEL_STATUS_ENABLED,
    CHANNEL_STATUS_PENDING_TEST,
    CHANNEL_STATUS_TEST_FAILED,
    CHANNEL_STATUS_TEST_SUCCESS,
    CHANNEL_STATUS_UNCONFIGURED,
    CHANNEL_TYPES,
    CHANNEL_TYPE_DINGTALK,
    CHANNEL_TYPE_EMAIL,
    CHANNEL_TYPE_IN_APP,
    CHANNEL_TYPE_ONEBOT,
    CHANNEL_TYPE_WEBHOOK,
    CHANNEL_TYPE_WXPUSHER,
    DELIVERY_DIGEST,
    DELIVERY_INSTANT,
    DELIVERY_MODES,
    DELIVERY_STATUS_AUTH_FAILED,
    DELIVERY_STATUS_FAILED,
    DELIVERY_STATUS_RATE_LIMITED,
    DELIVERY_STATUS_SUCCESS,
    DELIVERY_STATUS_TIMEOUT,
    OUTBOX_STATUS_DEAD_LETTER,
    OUTBOX_STATUS_FAILED,
    OUTBOX_STATUS_PENDING,
    OUTBOX_STATUS_SENDING,
    OUTBOX_STATUS_SENT,
    OUTBOX_STATUSES,
    SCOPE_ALL,
    SCOPE_PORTFOLIO,
    SCOPE_SYMBOL,
    SCOPE_WATCHLIST,
    SEVERITIES,
    SEVERITY_CRITICAL,
    SEVERITY_ERROR,
    SEVERITY_INFO,
    SEVERITY_WARN,
    NotificationChannel,
    NotificationDelivery,
    NotificationOutbox,
    NotificationPolicy,
    NotificationPolicyChannel,
    NotificationTemplate,
)
from app.utils.secret_mask import (
    decrypt_config,
    encrypt_config,
    mask_smtp_password,
    mask_token,
    mask_url,
    mask_webhook_secret,
)


pytestmark = pytest.mark.whitebox


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------


def _make_channel(
    *,
    name: str = "test-channel",
    channel_type: str = CHANNEL_TYPE_IN_APP,
    **kwargs,
) -> NotificationChannel:
    """构造一个 NotificationChannel（未传字段使用模型默认值）。"""
    return NotificationChannel(name=name, channel_type=channel_type, **kwargs)


def _make_policy(
    *,
    name: str = "test-policy",
    **kwargs,
) -> NotificationPolicy:
    """构造一个 NotificationPolicy（未传字段使用模型默认值）。"""
    return NotificationPolicy(name=name, **kwargs)


def _make_outbox(
    *,
    event_key: str = "evt-001",
    source_type: str = "alert",
    event_type: str = "price_alert_triggered",
    channel_id: int = 1,
    **kwargs,
) -> NotificationOutbox:
    """构造一个 NotificationOutbox（未传字段使用模型默认值）。

    注意：调用方需先确保 channel_id 指向的 NotificationChannel 已存在
    （SQLite 默认不强制外键，但 MySQL 会强制）。
    """
    return NotificationOutbox(
        event_key=event_key,
        source_type=source_type,
        event_type=event_type,
        channel_id=channel_id,
        **kwargs,
    )


def _make_channel_in_db(db_session, **kwargs) -> NotificationChannel:
    """在数据库中创建并提交一个渠道，返回已 refresh 的实例。"""
    channel = _make_channel(**kwargs)
    db_session.add(channel)
    db_session.commit()
    db_session.refresh(channel)
    return channel


# ----------------------------------------------------------------------------
# 1. 6 张表存在性
# ----------------------------------------------------------------------------


def test_all_tables_exist_after_init_db(tmp_sqlite_url):
    """【WP-MSG.1】init_db() 后 6 张 notification_* 表都存在。"""
    from app.db.manager import DatabaseManager

    mgr = DatabaseManager.get()
    try:
        mgr.dispose()
    except Exception:
        pass
    mgr.initialize(tmp_sqlite_url, db_type="sqlite")

    try:
        init_db()

        engine = mgr.engine
        inspector = inspect(engine)
        table_names = set(inspector.get_table_names())
        expected = {
            "notification_channels",
            "notification_policies",
            "notification_policy_channels",
            "notification_outbox",
            "notification_deliveries",
            "notification_templates",
        }
        assert expected.issubset(table_names), (
            f"缺失表: {expected - table_names}; 实际: {table_names}"
        )
    finally:
        try:
            mgr.dispose()
        except Exception:
            pass


def test_all_tables_exist_after_metadata_create_all(db_session):
    """【WP-MSG.1】Base.metadata.create_all 后 6 张表都存在。"""
    engine = db_session.bind
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    expected = {
        "notification_channels",
        "notification_policies",
        "notification_policy_channels",
        "notification_outbox",
        "notification_deliveries",
        "notification_templates",
    }
    assert expected.issubset(table_names), (
        f"缺失表: {expected - table_names}; 实际: {table_names}"
    )


# ----------------------------------------------------------------------------
# 2. NotificationChannel 字段完整性
# ----------------------------------------------------------------------------


def test_channel_all_fields_writable(db_session):
    """【WP-MSG.1】NotificationChannel 所有字段可写入指定值并读回。"""
    channel = _make_channel(
        name="full-channel",
        channel_type=CHANNEL_TYPE_WXPUSHER,
        enabled=True,
        status=CHANNEL_STATUS_ENABLED,
        config_encrypted_json="eyJ0b2tlbiI6ImFiYzEyMyJ9",
        config_mask_json='{"token":"abcd****1234"}',
        last_error_code="AUTH_FAILED",
        last_error_message="Token 无效",
    )
    db_session.add(channel)
    db_session.commit()
    db_session.refresh(channel)

    assert channel.id is not None
    assert channel.name == "full-channel"
    assert channel.channel_type == CHANNEL_TYPE_WXPUSHER
    assert channel.enabled is True
    assert channel.status == CHANNEL_STATUS_ENABLED
    assert channel.config_encrypted_json == "eyJ0b2tlbiI6ImFiYzEyMyJ9"
    assert channel.config_mask_json == '{"token":"abcd****1234"}'
    assert channel.last_error_code == "AUTH_FAILED"
    assert channel.last_error_message == "Token 无效"
    assert channel.created_at is not None


def test_channel_all_columns_present(db_session):
    """【WP-MSG.1】notification_channels 表中所有声明字段都存在。"""
    engine = db_session.bind
    inspector = inspect(engine)
    columns = {col["name"] for col in inspector.get_columns("notification_channels")}
    expected = {
        "id",
        "name",
        "channel_type",
        "enabled",
        "status",
        "config_encrypted_json",
        "config_mask_json",
        "verified_at",
        "last_test_at",
        "last_test_success",
        "last_error_code",
        "last_error_message",
        "created_at",
        "updated_at",
    }
    assert expected.issubset(columns), f"缺失字段: {expected - columns}"


# ----------------------------------------------------------------------------
# 3. NotificationChannel 默认值
# ----------------------------------------------------------------------------


def test_channel_defaults(db_session):
    """【WP-MSG.1】不传 enabled / status 时默认值正确。"""
    channel = _make_channel(name="default-channel")
    db_session.add(channel)
    db_session.commit()
    db_session.refresh(channel)

    assert channel.enabled is False
    assert channel.status == CHANNEL_STATUS_UNCONFIGURED
    assert channel.config_encrypted_json is None
    assert channel.config_mask_json is None
    assert channel.verified_at is None
    assert channel.last_test_at is None
    assert channel.last_test_success is None
    assert channel.last_error_code is None
    assert channel.last_error_message is None
    assert channel.created_at is not None
    assert channel.updated_at is None


# ----------------------------------------------------------------------------
# 4. NotificationChannel channel_type 枚举
# ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "channel_type,idx",
    [
        (CHANNEL_TYPE_IN_APP, 1),
        (CHANNEL_TYPE_WXPUSHER, 2),
        (CHANNEL_TYPE_DINGTALK, 3),
        (CHANNEL_TYPE_ONEBOT, 4),
        (CHANNEL_TYPE_EMAIL, 5),
        (CHANNEL_TYPE_WEBHOOK, 6),
    ],
)
def test_channel_type_values(db_session, channel_type, idx):
    """【WP-MSG.1】channel_type 支持所有合法取值。"""
    channel = _make_channel(name=f"ch-type-{idx}", channel_type=channel_type)
    db_session.add(channel)
    db_session.commit()
    db_session.refresh(channel)

    assert channel.channel_type == channel_type


# ----------------------------------------------------------------------------
# 5. NotificationChannel status 枚举
# ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,idx",
    [
        (CHANNEL_STATUS_UNCONFIGURED, 1),
        (CHANNEL_STATUS_PENDING_TEST, 2),
        (CHANNEL_STATUS_TEST_SUCCESS, 3),
        (CHANNEL_STATUS_TEST_FAILED, 4),
        (CHANNEL_STATUS_ENABLED, 5),
        (CHANNEL_STATUS_DISABLED, 6),
    ],
)
def test_channel_status_values(db_session, status, idx):
    """【WP-MSG.1】status 支持所有合法取值。"""
    channel = _make_channel(name=f"ch-status-{idx}", status=status)
    db_session.add(channel)
    db_session.commit()
    db_session.refresh(channel)

    assert channel.status == status


# ----------------------------------------------------------------------------
# 6. NotificationChannel 敏感字段（加密 + 脱敏）
# ----------------------------------------------------------------------------


def test_channel_sensitive_fields_encrypted_and_masked(db_session):
    """【WP-MSG.1】config_encrypted_json 存储加密值，config_mask_json 存储脱敏值。

    验证：
    - 加密后的值不等于原始明文
    - 脱敏后的值不包含完整 Token
    - 解密后能还原原始配置
    """
    original_config = {"token": "sk-abcdef1234567890", "uid": "12345"}
    config_json = json.dumps(original_config)

    encrypted = encrypt_config(config_json)
    assert encrypted != config_json, "加密后的值不应等于明文"

    masked_config = {"token": mask_token(original_config["token"]), "uid": "****"}
    mask_json = json.dumps(masked_config)

    channel = _make_channel(
        name="sensitive-channel",
        channel_type=CHANNEL_TYPE_WXPUSHER,
        config_encrypted_json=encrypted,
        config_mask_json=mask_json,
    )
    db_session.add(channel)
    db_session.commit()
    db_session.refresh(channel)

    # 加密值不等于明文
    assert channel.config_encrypted_json != config_json
    # 脱敏值不包含完整 Token
    assert "sk-abcdef1234567890" not in channel.config_mask_json
    # 解密能还原
    decrypted = decrypt_config(channel.config_encrypted_json)
    assert json.loads(decrypted) == original_config


def test_channel_name_unique(db_session):
    """【WP-MSG.1】channel name 唯一约束生效。"""
    ch1 = _make_channel(name="unique-name-channel")
    db_session.add(ch1)
    db_session.commit()

    ch2 = _make_channel(name="unique-name-channel")
    db_session.add(ch2)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


# ----------------------------------------------------------------------------
# 7. NotificationPolicy 字段完整性
# ----------------------------------------------------------------------------


def test_policy_all_fields_writable(db_session):
    """【WP-MSG.1】NotificationPolicy 所有字段可写入指定值并读回。"""
    policy = _make_policy(
        name="full-policy",
        enabled=True,
        source_types_json='["alert","task_done"]',
        min_severity=SEVERITY_ERROR,
        scope_type=SCOPE_PORTFOLIO,
        scope_ids_json="[1,2,3]",
        delivery_mode=DELIVERY_DIGEST,
        digest_schedule="0 9 * * *",
        quiet_hours_json='{"start":"22:00","end":"08:00","timezone":"Asia/Shanghai","bypass_for_critical":false}',
        cooldown_minutes=30,
        dedup_window_minutes=60,
        template_id=1,
    )
    db_session.add(policy)
    db_session.commit()
    db_session.refresh(policy)

    assert policy.id is not None
    assert policy.name == "full-policy"
    assert policy.enabled is True
    assert policy.source_types_json == '["alert","task_done"]'
    assert policy.min_severity == SEVERITY_ERROR
    assert policy.scope_type == SCOPE_PORTFOLIO
    assert policy.scope_ids_json == "[1,2,3]"
    assert policy.delivery_mode == DELIVERY_DIGEST
    assert policy.digest_schedule == "0 9 * * *"
    assert policy.cooldown_minutes == 30
    assert policy.dedup_window_minutes == 60
    assert policy.template_id == 1
    assert policy.created_at is not None


def test_policy_all_columns_present(db_session):
    """【WP-MSG.1】notification_policies 表中所有声明字段都存在。"""
    engine = db_session.bind
    inspector = inspect(engine)
    columns = {col["name"] for col in inspector.get_columns("notification_policies")}
    expected = {
        "id",
        "name",
        "enabled",
        "source_types_json",
        "min_severity",
        "scope_type",
        "scope_ids_json",
        "delivery_mode",
        "digest_schedule",
        "quiet_hours_json",
        "cooldown_minutes",
        "dedup_window_minutes",
        "template_id",
        "created_at",
        "updated_at",
    }
    assert expected.issubset(columns), f"缺失字段: {expected - columns}"


# ----------------------------------------------------------------------------
# 8. NotificationPolicy 默认值
# ----------------------------------------------------------------------------


def test_policy_defaults(db_session):
    """【WP-MSG.1】NotificationPolicy 默认值正确。"""
    policy = _make_policy(name="default-policy")
    db_session.add(policy)
    db_session.commit()
    db_session.refresh(policy)

    assert policy.enabled is False
    assert policy.min_severity == SEVERITY_INFO
    assert policy.scope_type == SCOPE_ALL
    assert policy.delivery_mode == DELIVERY_INSTANT
    assert policy.cooldown_minutes == 0
    assert policy.dedup_window_minutes == 0
    assert policy.template_id is None
    assert policy.source_types_json is None
    assert policy.scope_ids_json is None
    assert policy.digest_schedule is None
    assert policy.quiet_hours_json is None
    assert policy.created_at is not None
    assert policy.updated_at is None


# ----------------------------------------------------------------------------
# 9. NotificationPolicy source_types_json 读写
# ----------------------------------------------------------------------------


def test_policy_source_types_json_read_write(db_session):
    """【WP-MSG.1】source_types_json 可写入 JSON 字符串并读回解析为列表。"""
    source_types = ["alert", "task_done", "trade", "data_expired", "discovery", "signal", "auto_block", "drawdown"]
    policy = _make_policy(
        name="source-types-policy",
        source_types_json=json.dumps(source_types),
    )
    db_session.add(policy)
    db_session.commit()
    db_session.refresh(policy)

    assert policy.source_types_json is not None
    parsed = json.loads(policy.source_types_json)
    assert isinstance(parsed, list)
    assert "alert" in parsed
    assert "trade" in parsed
    assert "drawdown" in parsed
    assert len(parsed) == 8


def test_policy_min_severity_values(db_session):
    """【WP-MSG.1】min_severity 支持所有合法取值。"""
    for idx, severity in enumerate(SEVERITIES, start=1):
        policy = _make_policy(name=f"sev-policy-{idx}", min_severity=severity)
        db_session.add(policy)
        db_session.commit()
        db_session.refresh(policy)
        assert policy.min_severity == severity


def test_policy_scope_type_values(db_session):
    """【WP-MSG.1】scope_type 支持所有合法取值。"""
    for idx, scope in enumerate([SCOPE_ALL, SCOPE_PORTFOLIO, SCOPE_WATCHLIST, SCOPE_SYMBOL], start=1):
        policy = _make_policy(name=f"scope-policy-{idx}", scope_type=scope)
        db_session.add(policy)
        db_session.commit()
        db_session.refresh(policy)
        assert policy.scope_type == scope


# ----------------------------------------------------------------------------
# 10. NotificationPolicyChannel 唯一约束
# ----------------------------------------------------------------------------


def test_policy_channel_unique_constraint(db_session):
    """【WP-MSG.1】(policy_id, channel_id) 唯一约束生效。"""
    policy = _make_policy(name="unique-pc-policy")
    channel = _make_channel(name="unique-pc-channel")
    db_session.add_all([policy, channel])
    db_session.commit()
    db_session.refresh(policy)
    db_session.refresh(channel)

    pc1 = NotificationPolicyChannel(policy_id=policy.id, channel_id=channel.id)
    db_session.add(pc1)
    db_session.commit()

    pc2 = NotificationPolicyChannel(policy_id=policy.id, channel_id=channel.id)
    db_session.add(pc2)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_policy_channel_allows_different_channels(db_session):
    """【WP-MSG.1】同一策略可关联多个不同渠道。"""
    policy = _make_policy(name="multi-channel-policy")
    ch1 = _make_channel(name="multi-ch-1")
    ch2 = _make_channel(name="multi-ch-2")
    db_session.add_all([policy, ch1, ch2])
    db_session.commit()
    db_session.refresh(policy)
    db_session.refresh(ch1)
    db_session.refresh(ch2)

    pc1 = NotificationPolicyChannel(policy_id=policy.id, channel_id=ch1.id)
    pc2 = NotificationPolicyChannel(policy_id=policy.id, channel_id=ch2.id)
    db_session.add_all([pc1, pc2])
    db_session.commit()

    assert pc1.id is not None
    assert pc2.id is not None
    assert pc1.id != pc2.id


# ----------------------------------------------------------------------------
# 11. NotificationPolicyChannel 级联删除
# ----------------------------------------------------------------------------


def test_policy_channel_cascade_delete_on_policy(tmp_sqlite_url):
    """【WP-MSG.1】删除 policy 时关联 policy_channel 记录被级联删除。

    SQLite 默认不启用外键级联，需通过 PRAGMA foreign_keys=ON 开启。
    使用独立 engine + connect 事件监听器确保外键约束生效。
    """
    engine = create_engine(tmp_sqlite_url)

    @event.listens_for(engine, "connect")
    def _enable_fk(dbapi_conn, _):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()

    try:
        policy = _make_policy(name="cascade-policy")
        channel = _make_channel(name="cascade-channel")
        session.add_all([policy, channel])
        session.commit()
        session.refresh(policy)
        session.refresh(channel)

        pc = NotificationPolicyChannel(policy_id=policy.id, channel_id=channel.id)
        session.add(pc)
        session.commit()
        session.refresh(pc)
        pc_id = pc.id

        # 删除 policy
        session.delete(policy)
        session.commit()

        # 关联记录应被级联删除
        found = session.query(NotificationPolicyChannel).filter_by(id=pc_id).first()
        assert found is None
    finally:
        session.close()
        engine.dispose()


def test_policy_channel_cascade_delete_on_channel(tmp_sqlite_url):
    """【WP-MSG.1】删除 channel 时关联 policy_channel 记录被级联删除。"""
    engine = create_engine(tmp_sqlite_url)

    @event.listens_for(engine, "connect")
    def _enable_fk(dbapi_conn, _):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()

    try:
        policy = _make_policy(name="cascade-ch-policy")
        channel = _make_channel(name="cascade-ch-channel")
        session.add_all([policy, channel])
        session.commit()
        session.refresh(policy)
        session.refresh(channel)

        pc = NotificationPolicyChannel(policy_id=policy.id, channel_id=channel.id)
        session.add(pc)
        session.commit()
        session.refresh(pc)
        pc_id = pc.id

        # 删除 channel
        session.delete(channel)
        session.commit()

        # 关联记录应被级联删除
        found = session.query(NotificationPolicyChannel).filter_by(id=pc_id).first()
        assert found is None
    finally:
        session.close()
        engine.dispose()


# ----------------------------------------------------------------------------
# 12. NotificationOutbox 字段完整性
# ----------------------------------------------------------------------------


def test_outbox_all_fields_writable(db_session):
    """【WP-MSG.1】NotificationOutbox 所有字段可写入指定值并读回。"""
    # 先创建一个 channel 以满足外键（SQLite 默认不强制，但保留好习惯）
    channel = _make_channel_in_db(db_session, name="outbox-test-channel")

    outbox = _make_outbox(
        event_key="evt-full-001",
        source_type="trade",
        source_id=100,
        event_type="trade_executed",
        severity=SEVERITY_CRITICAL,
        payload_json='{"title":"买入成交","body":"600000 100股","symbol_id":1}',
        channel_id=channel.id,
        policy_id=1,
        status=OUTBOX_STATUS_SENT,
        attempt_count=2,
        max_attempts=5,
        last_error_code="TIMEOUT",
        last_error_message="请求超时",
    )
    db_session.add(outbox)
    db_session.commit()
    db_session.refresh(outbox)

    assert outbox.id is not None
    assert outbox.event_key == "evt-full-001"
    assert outbox.source_type == "trade"
    assert outbox.source_id == 100
    assert outbox.event_type == "trade_executed"
    assert outbox.severity == SEVERITY_CRITICAL
    assert outbox.payload_json == '{"title":"买入成交","body":"600000 100股","symbol_id":1}'
    assert outbox.channel_id == channel.id
    assert outbox.policy_id == 1
    assert outbox.status == OUTBOX_STATUS_SENT
    assert outbox.attempt_count == 2
    assert outbox.max_attempts == 5
    assert outbox.last_error_code == "TIMEOUT"
    assert outbox.last_error_message == "请求超时"
    assert outbox.created_at is not None


def test_outbox_all_columns_present(db_session):
    """【WP-MSG.1】notification_outbox 表中所有声明字段都存在。"""
    engine = db_session.bind
    inspector = inspect(engine)
    columns = {col["name"] for col in inspector.get_columns("notification_outbox")}
    expected = {
        "id",
        "event_key",
        "source_type",
        "source_id",
        "event_type",
        "severity",
        "payload_json",
        "channel_id",
        "policy_id",
        "status",
        "attempt_count",
        "max_attempts",
        "next_retry_at",
        "last_error_code",
        "last_error_message",
        "sent_at",
        "created_at",
        "updated_at",
    }
    assert expected.issubset(columns), f"缺失字段: {expected - columns}"


# ----------------------------------------------------------------------------
# 13. NotificationOutbox 默认值
# ----------------------------------------------------------------------------


def test_outbox_defaults(db_session):
    """【WP-MSG.1】NotificationOutbox 默认值正确。"""
    channel = _make_channel_in_db(db_session, name="outbox-default-channel")

    outbox = _make_outbox(channel_id=channel.id)
    db_session.add(outbox)
    db_session.commit()
    db_session.refresh(outbox)

    assert outbox.status == OUTBOX_STATUS_PENDING
    assert outbox.attempt_count == 0
    assert outbox.max_attempts == 5
    assert outbox.severity == SEVERITY_INFO
    assert outbox.source_id is None
    assert outbox.policy_id is None
    assert outbox.payload_json is None
    assert outbox.next_retry_at is None
    assert outbox.last_error_code is None
    assert outbox.last_error_message is None
    assert outbox.sent_at is None
    assert outbox.created_at is not None
    assert outbox.updated_at is None


# ----------------------------------------------------------------------------
# 14. NotificationOutbox event_key + channel_id 部分唯一索引
# ----------------------------------------------------------------------------


def test_outbox_partial_unique_index_blocks_duplicate_pending(db_session):
    """【WP-MSG.1】同 event_key + channel_id 的第二条 pending 记录抛 IntegrityError。"""
    channel = _make_channel_in_db(db_session, name="idx-test-channel")

    outbox1 = _make_outbox(
        event_key="evt-idx-001",
        channel_id=channel.id,
        status=OUTBOX_STATUS_PENDING,
    )
    db_session.add(outbox1)
    db_session.commit()

    outbox2 = _make_outbox(
        event_key="evt-idx-001",
        channel_id=channel.id,
        status=OUTBOX_STATUS_PENDING,
    )
    db_session.add(outbox2)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_outbox_partial_unique_index_allows_sent_duplicate(db_session):
    """【WP-MSG.1】同 event_key + channel_id 的 sent 记录不与 pending 冲突。

    部分唯一索引仅对 status != 'sent' 生效，已发送记录不参与唯一约束。
    """
    channel = _make_channel_in_db(db_session, name="idx-sent-channel")

    # 先创建一条 pending 记录
    outbox_pending = _make_outbox(
        event_key="evt-sent-001",
        channel_id=channel.id,
        status=OUTBOX_STATUS_PENDING,
    )
    db_session.add(outbox_pending)
    db_session.commit()

    # 再创建一条 sent 记录（相同 event_key + channel_id）应成功
    outbox_sent = _make_outbox(
        event_key="evt-sent-001",
        channel_id=channel.id,
        status=OUTBOX_STATUS_SENT,
    )
    db_session.add(outbox_sent)
    db_session.commit()

    assert outbox_pending.id is not None
    assert outbox_sent.id is not None
    assert outbox_pending.id != outbox_sent.id


def test_outbox_partial_unique_index_allows_different_channels(db_session):
    """【WP-MSG.1】同 event_key 不同 channel_id 的 pending 记录可共存。"""
    ch1 = _make_channel_in_db(db_session, name="idx-multi-ch-1")
    ch2 = _make_channel_in_db(db_session, name="idx-multi-ch-2")

    outbox1 = _make_outbox(
        event_key="evt-multi-ch-001",
        channel_id=ch1.id,
        status=OUTBOX_STATUS_PENDING,
    )
    outbox2 = _make_outbox(
        event_key="evt-multi-ch-001",
        channel_id=ch2.id,
        status=OUTBOX_STATUS_PENDING,
    )
    db_session.add_all([outbox1, outbox2])
    db_session.commit()

    assert outbox1.id is not None
    assert outbox2.id is not None
    assert outbox1.id != outbox2.id


# ----------------------------------------------------------------------------
# 15. NotificationDelivery 字段完整性
# ----------------------------------------------------------------------------


def test_delivery_all_fields_writable(db_session):
    """【WP-MSG.1】NotificationDelivery 所有字段可写入指定值并读回。"""
    channel = _make_channel_in_db(db_session, name="delivery-test-channel")
    outbox = _make_outbox(channel_id=channel.id)
    db_session.add(outbox)
    db_session.commit()
    db_session.refresh(outbox)

    delivery = NotificationDelivery(
        outbox_id=outbox.id,
        channel_id=channel.id,
        attempt_number=1,
        status=DELIVERY_STATUS_SUCCESS,
        status_code=200,
        response_summary='{"code":0,"msg":"ok"}',
        error_code=None,
        error_message=None,
        duration_ms=150,
    )
    db_session.add(delivery)
    db_session.commit()
    db_session.refresh(delivery)

    assert delivery.id is not None
    assert delivery.outbox_id == outbox.id
    assert delivery.channel_id == channel.id
    assert delivery.attempt_number == 1
    assert delivery.status == DELIVERY_STATUS_SUCCESS
    assert delivery.status_code == 200
    assert delivery.response_summary == '{"code":0,"msg":"ok"}'
    assert delivery.error_code is None
    assert delivery.error_message is None
    assert delivery.duration_ms == 150
    assert delivery.created_at is not None


def test_delivery_all_columns_present(db_session):
    """【WP-MSG.1】notification_deliveries 表中所有声明字段都存在。"""
    engine = db_session.bind
    inspector = inspect(engine)
    columns = {col["name"] for col in inspector.get_columns("notification_deliveries")}
    expected = {
        "id",
        "outbox_id",
        "channel_id",
        "attempt_number",
        "status",
        "status_code",
        "response_summary",
        "error_code",
        "error_message",
        "duration_ms",
        "sent_at",
        "created_at",
    }
    assert expected.issubset(columns), f"缺失字段: {expected - columns}"


def test_delivery_status_values(db_session):
    """【WP-MSG.1】NotificationDelivery status 支持所有合法取值。"""
    channel = _make_channel_in_db(db_session, name="delivery-status-channel")

    for idx, status in enumerate(
        [DELIVERY_STATUS_SUCCESS, DELIVERY_STATUS_FAILED, DELIVERY_STATUS_TIMEOUT,
         DELIVERY_STATUS_AUTH_FAILED, DELIVERY_STATUS_RATE_LIMITED],
        start=1,
    ):
        outbox = _make_outbox(
            event_key=f"evt-delivery-status-{idx}",
            channel_id=channel.id,
        )
        db_session.add(outbox)
        db_session.commit()
        db_session.refresh(outbox)

        delivery = NotificationDelivery(
            outbox_id=outbox.id,
            channel_id=channel.id,
            attempt_number=1,
            status=status,
        )
        db_session.add(delivery)
        db_session.commit()
        db_session.refresh(delivery)
        assert delivery.status == status


# ----------------------------------------------------------------------------
# 16. NotificationDelivery 级联删除
# ----------------------------------------------------------------------------


def test_delivery_cascade_delete_on_outbox(tmp_sqlite_url):
    """【WP-MSG.1】删除 outbox 时关联 delivery 记录被级联删除。

    SQLite 默认不启用外键级联，需通过 PRAGMA foreign_keys=ON 开启。
    """
    engine = create_engine(tmp_sqlite_url)

    @event.listens_for(engine, "connect")
    def _enable_fk(dbapi_conn, _):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()

    try:
        channel = _make_channel(name="cascade-delivery-channel")
        outbox = _make_outbox(channel_id=1)
        session.add_all([channel, outbox])
        session.commit()
        session.refresh(outbox)

        delivery = NotificationDelivery(
            outbox_id=outbox.id,
            channel_id=1,
            attempt_number=1,
            status=DELIVERY_STATUS_SUCCESS,
        )
        session.add(delivery)
        session.commit()
        session.refresh(delivery)
        delivery_id = delivery.id

        # 删除 outbox
        session.delete(outbox)
        session.commit()

        # delivery 应被级联删除
        found = session.query(NotificationDelivery).filter_by(id=delivery_id).first()
        assert found is None
    finally:
        session.close()
        engine.dispose()


# ----------------------------------------------------------------------------
# 17. NotificationTemplate 字段完整性
# ----------------------------------------------------------------------------


def test_template_all_fields_writable(db_session):
    """【WP-MSG.1】NotificationTemplate 所有字段可写入指定值并读回。"""
    template = NotificationTemplate(
        name="price-alert-template",
        title_template="价格告警: {{symbol}} {{direction}} {{price}}",
        body_template="## 价格告警\n\n标的: **{{symbol}}**\n方向: {{direction}}\n价格: {{price}}",
        body_text_template="价格告警: 标的 {{symbol}}, 方向 {{direction}}, 价格 {{price}}",
        variables_json='[{"name":"symbol","description":"标的代码"},{"name":"price","description":"触发价格"}]',
        version=2,
        is_active=False,
    )
    db_session.add(template)
    db_session.commit()
    db_session.refresh(template)

    assert template.id is not None
    assert template.name == "price-alert-template"
    assert template.title_template == "价格告警: {{symbol}} {{direction}} {{price}}"
    assert "## 价格告警" in template.body_template
    assert template.body_text_template is not None
    assert "{{symbol}}" in template.body_text_template
    assert template.variables_json is not None
    parsed = json.loads(template.variables_json)
    assert isinstance(parsed, list)
    assert parsed[0]["name"] == "symbol"
    assert template.version == 2
    assert template.is_active is False
    assert template.created_at is not None


def test_template_all_columns_present(db_session):
    """【WP-MSG.1】notification_templates 表中所有声明字段都存在。"""
    engine = db_session.bind
    inspector = inspect(engine)
    columns = {col["name"] for col in inspector.get_columns("notification_templates")}
    expected = {
        "id",
        "name",
        "title_template",
        "body_template",
        "body_text_template",
        "variables_json",
        "version",
        "is_active",
        "created_at",
        "updated_at",
    }
    assert expected.issubset(columns), f"缺失字段: {expected - columns}"


# ----------------------------------------------------------------------------
# 18. NotificationTemplate 默认值
# ----------------------------------------------------------------------------


def test_template_defaults(db_session):
    """【WP-MSG.1】NotificationTemplate 默认值正确。"""
    template = NotificationTemplate(
        name="default-template",
        title_template="默认标题",
        body_template="默认正文",
    )
    db_session.add(template)
    db_session.commit()
    db_session.refresh(template)

    assert template.version == 1
    assert template.is_active is True
    assert template.body_text_template is None
    assert template.variables_json is None
    assert template.created_at is not None
    assert template.updated_at is None


def test_template_name_unique(db_session):
    """【WP-MSG.1】template name 唯一约束生效。"""
    t1 = NotificationTemplate(name="unique-template", title_template="t", body_template="b")
    db_session.add(t1)
    db_session.commit()

    t2 = NotificationTemplate(name="unique-template", title_template="t", body_template="b")
    db_session.add(t2)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


# ----------------------------------------------------------------------------
# 19. schema 补丁幂等
# ----------------------------------------------------------------------------


def test_ensure_sqlite_patch_idempotent_when_tables_exist(db_session):
    """【WP-MSG.1】表已存在时多次调用 SQLite 补丁不报错。"""
    engine = db_session.bind
    # 表已由 create_all 创建
    _ensure_sqlite_notification_tables(engine)
    # 第二次调用不应报错（幂等）
    _ensure_sqlite_notification_tables(engine)

    inspector = inspect(engine)
    expected_tables = {
        "notification_channels",
        "notification_policies",
        "notification_policy_channels",
        "notification_outbox",
        "notification_deliveries",
        "notification_templates",
    }
    actual_tables = set(inspector.get_table_names())
    assert expected_tables.issubset(actual_tables)


def test_ensure_sqlite_patch_creates_tables_when_missing():
    """【WP-MSG.1】表不存在时 SQLite 补丁创建表。"""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="qa_notif_")
    os.close(fd)
    try:
        engine = create_engine(f"sqlite:///{path}")
        # 先 create_all，再 drop 目标表，模拟"表不存在"场景
        Base.metadata.create_all(engine)
        with engine.begin() as conn:
            for tbl in [
                "notification_deliveries",
                "notification_outbox",
                "notification_policy_channels",
                "notification_policies",
                "notification_channels",
                "notification_templates",
            ]:
                conn.execute(text(f"DROP TABLE IF EXISTS {tbl}"))

        inspector = inspect(engine)
        for tbl in [
            "notification_channels",
            "notification_policies",
            "notification_policy_channels",
            "notification_outbox",
            "notification_deliveries",
            "notification_templates",
        ]:
            assert tbl not in inspector.get_table_names()

        # 调用补丁函数
        _ensure_sqlite_notification_tables(engine)

        # 表应已创建
        inspector = inspect(engine)
        for tbl in [
            "notification_channels",
            "notification_policies",
            "notification_policy_channels",
            "notification_outbox",
            "notification_deliveries",
            "notification_templates",
        ]:
            assert tbl in inspector.get_table_names(), f"表 {tbl} 未创建"

        # 再次调用（幂等）
        _ensure_sqlite_notification_tables(engine)
    finally:
        engine.dispose()
        try:
            os.remove(path)
        except OSError:
            pass


def test_ensure_sqlite_patch_creates_partial_index_when_missing():
    """【WP-MSG.1】表存在但缺部分唯一索引时，补丁补建索引（幂等）。"""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="qa_notif_idx_")
    os.close(fd)
    try:
        engine = create_engine(f"sqlite:///{path}")
        # 手动创建表（不含部分唯一索引）模拟"表已存在但缺索引"
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE TABLE notification_channels ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "name VARCHAR(64) NOT NULL UNIQUE, "
                "channel_type VARCHAR(16) NOT NULL, "
                "enabled BOOLEAN NOT NULL DEFAULT 0, "
                "status VARCHAR(16) NOT NULL DEFAULT 'unconfigured', "
                "config_encrypted_json TEXT, "
                "config_mask_json TEXT, "
                "verified_at DATETIME, "
                "last_test_at DATETIME, "
                "last_test_success BOOLEAN, "
                "last_error_code VARCHAR(64), "
                "last_error_message VARCHAR(500), "
                "created_at DATETIME NOT NULL, "
                "updated_at DATETIME)"
            ))
            conn.execute(text(
                "CREATE TABLE notification_outbox ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "event_key VARCHAR(128) NOT NULL, "
                "source_type VARCHAR(32) NOT NULL, "
                "source_id INTEGER, "
                "event_type VARCHAR(64) NOT NULL, "
                "severity VARCHAR(16) NOT NULL DEFAULT 'info', "
                "payload_json TEXT, "
                "channel_id INTEGER NOT NULL, "
                "policy_id INTEGER, "
                "status VARCHAR(16) NOT NULL DEFAULT 'pending', "
                "attempt_count INTEGER NOT NULL DEFAULT 0, "
                "max_attempts INTEGER NOT NULL DEFAULT 5, "
                "next_retry_at DATETIME, "
                "last_error_code VARCHAR(64), "
                "last_error_message VARCHAR(500), "
                "sent_at DATETIME, "
                "created_at DATETIME NOT NULL, "
                "updated_at DATETIME)"
            ))

        # 补丁前：索引不存在
        inspector = inspect(engine)
        before_idx = {idx["name"] for idx in inspector.get_indexes("notification_outbox")}
        assert "idx_no_event_channel_pending" not in before_idx

        # 调用补丁（仅补索引函数）
        _ensure_sqlite_notification_outbox_unique_index(engine)

        # 补丁后：索引存在
        inspector = inspect(engine)
        after_idx = {idx["name"] for idx in inspector.get_indexes("notification_outbox")}
        assert "idx_no_event_channel_pending" in after_idx

        # 再次调用（幂等）
        _ensure_sqlite_notification_outbox_unique_index(engine)
        inspector = inspect(engine)
        after_idx2 = {idx["name"] for idx in inspector.get_indexes("notification_outbox")}
        assert "idx_no_event_channel_pending" in after_idx2
    finally:
        engine.dispose()
        try:
            os.remove(path)
        except OSError:
            pass


# ----------------------------------------------------------------------------
# 20. MySQL 补丁函数存在且可调用
# ----------------------------------------------------------------------------


def test_mysql_patch_function_exists_and_callable():
    """【WP-MSG.1】_ensure_mysql_notification_tables 函数存在且可调用。

    不需要真实 MySQL 实例，仅验证函数定义存在且可被调用。
    """
    assert callable(_ensure_mysql_notification_tables)
    import inspect as _inspect
    sig = _inspect.signature(_ensure_mysql_notification_tables)
    params = list(sig.parameters.keys())
    assert len(params) == 1
    assert params[0] == "engine"


def test_sqlite_patch_function_exists_and_callable():
    """【WP-MSG.1】_ensure_sqlite_notification_tables 函数存在且可调用。"""
    assert callable(_ensure_sqlite_notification_tables)
    import inspect as _inspect
    sig = _inspect.signature(_ensure_sqlite_notification_tables)
    params = list(sig.parameters.keys())
    assert len(params) == 1
    assert params[0] == "engine"


# ----------------------------------------------------------------------------
# 21. 索引存在性
# ----------------------------------------------------------------------------


def test_outbox_partial_unique_index_exists(db_session):
    """【WP-MSG.1】notification_outbox 的 idx_no_event_channel_pending 索引存在。"""
    engine = db_session.bind
    inspector = inspect(engine)
    indexes = inspector.get_indexes("notification_outbox")
    index_names = {idx["name"] for idx in indexes}
    assert "idx_no_event_channel_pending" in index_names, (
        f"缺失索引 idx_no_event_channel_pending; 实际: {index_names}"
    )


def test_outbox_partial_unique_index_is_unique(db_session):
    """【WP-MSG.1】idx_no_event_channel_pending 为唯一索引。"""
    engine = db_session.bind
    inspector = inspect(engine)
    indexes = inspector.get_indexes("notification_outbox")
    target_idx = [idx for idx in indexes if idx["name"] == "idx_no_event_channel_pending"]
    assert len(target_idx) == 1
    assert target_idx[0].get("unique"), f"索引非唯一: {target_idx[0]}"


def test_channel_indexes_exist(db_session):
    """【WP-MSG.1】notification_channels 字段级索引存在。"""
    engine = db_session.bind
    inspector = inspect(engine)
    indexes = inspector.get_indexes("notification_channels")

    indexed_columns = set()
    for idx in indexes:
        for col in idx.get("column_names", []) or []:
            indexed_columns.add(col)

    # name (unique+index), channel_type (index), status (index), created_at (index)
    expected_indexed_columns = {"name", "channel_type", "status", "created_at"}
    assert expected_indexed_columns.issubset(indexed_columns), (
        f"缺失列索引: {expected_indexed_columns - indexed_columns}; "
        f"实际索引列: {indexed_columns}"
    )


# ----------------------------------------------------------------------------
# 22. 脱敏函数
# ----------------------------------------------------------------------------


def test_mask_token_long():
    """【WP-MSG.1】mask_token 保留前 4 + 后 4，中间 ****。"""
    token = "sk-abcdef1234567890"
    masked = mask_token(token)
    assert masked == "sk-a****7890"
    assert "abcdef123456" not in masked


def test_mask_token_short():
    """【WP-MSG.1】mask_token 长度 < 8 时全部 ****。"""
    assert mask_token("abc") == "****"
    assert mask_token("1234567") == "****"


def test_mask_token_medium():
    """【WP-MSG.1】mask_token 长度 8-12 时保留前 2 + 后 2。"""
    assert mask_token("abcdefgh") == "ab****gh"
    assert mask_token("abcdefghij") == "ab****ij"


def test_mask_token_empty():
    """【WP-MSG.1】mask_token 空值返回空字符串。"""
    assert mask_token("") == ""
    assert mask_token(None) == ""


def test_mask_webhook_secret():
    """【WP-MSG.1】mask_webhook_secret 与 mask_token 策略相同。"""
    secret = "SEC1234567890abcdef"
    masked = mask_webhook_secret(secret)
    assert masked == "SEC1****cdef"
    assert "1234567890ab" not in masked


def test_mask_smtp_password():
    """【WP-MSG.1】mask_smtp_password 全部 ****（不保留任何字符）。"""
    assert mask_smtp_password("my-secret-password-123") == "****"
    assert mask_smtp_password("short") == "****"
    assert mask_smtp_password("") == ""
    assert mask_smtp_password(None) == ""


def test_mask_url_with_query_params():
    """【WP-MSG.1】mask_url 脱敏查询参数 value，保留 scheme/host/path/key。"""
    url = "https://example.com/webhook?token=abc123&uid=456"
    masked = mask_url(url)
    assert "https://example.com/webhook" in masked
    assert "token=****" in masked
    assert "uid=****" in masked
    assert "abc123" not in masked
    assert "456" not in masked


def test_mask_url_without_query():
    """【WP-MSG.1】mask_url 无查询参数时原样返回。"""
    url = "https://example.com/webhook"
    assert mask_url(url) == url


def test_mask_url_empty():
    """【WP-MSG.1】mask_url 空值返回空字符串。"""
    assert mask_url("") == ""
    assert mask_url(None) == ""


def test_encrypt_decrypt_config_roundtrip():
    """【WP-MSG.1】加密后解密能还原原始配置。"""
    original = '{"token":"sk-1234567890abcdef","smtp_password":"secret"}'
    encrypted = encrypt_config(original)
    decrypted = decrypt_config(encrypted)
    assert decrypted == original
    assert encrypted != original


def test_encrypt_config_empty():
    """【WP-MSG.1】encrypt_config 空值返回空字符串。"""
    assert encrypt_config("") == ""
    assert encrypt_config(None) == ""


# ----------------------------------------------------------------------------
# 23. 脱敏不暴露完整值
# ----------------------------------------------------------------------------


def test_config_mask_json_does_not_expose_full_token():
    """【WP-MSG.1】config_mask_json 中不出现完整 Token。

    验证场景：WxPusher 渠道配置含 AppToken，脱敏后不应暴露完整值。
    """
    original_token = "AT_xxxxxxxxxxxxxxxx1234567890"
    masked_token = mask_token(original_token)

    # 脱敏值不包含完整 Token
    assert original_token not in masked_token
    assert original_token[:4] in masked_token  # 仅保留前 4
    assert original_token[-4:] in masked_token  # 仅保留后 4

    # 模拟 config_mask_json 存储
    config_mask = {"app_token": masked_token, "uids": "****"}
    mask_json = json.dumps(config_mask)

    # 完整 Token 不应出现在脱敏 JSON 中
    assert original_token not in mask_json


def test_config_mask_json_does_not_expose_full_smtp_password():
    """【WP-MSG.1】config_mask_json 中不出现完整 SMTP 密码。"""
    original_pwd = "my-smtp-password-123"
    masked_pwd = mask_smtp_password(original_pwd)

    # 脱敏值不包含任何密码字符
    assert masked_pwd == "****"
    assert original_pwd not in masked_pwd
    assert "my-smtp" not in masked_pwd

    config_mask = {"smtp_password": masked_pwd}
    mask_json = json.dumps(config_mask)
    assert original_pwd not in mask_json


def test_config_mask_json_does_not_expose_full_webhook_secret():
    """【WP-MSG.1】config_mask_json 中不出现完整 Webhook Secret。"""
    original_secret = "SECabcdefghijklmnopqrstuvwxyz123456"
    masked_secret = mask_webhook_secret(original_secret)

    assert original_secret not in masked_secret

    config_mask = {"webhook_secret": masked_secret}
    mask_json = json.dumps(config_mask)
    assert original_secret not in mask_json


def test_channel_last_error_message_does_not_expose_token():
    """【WP-MSG.1】channel.last_error_message 不应包含完整 Token。

    验证场景：渠道测试失败时，错误消息可能包含配置中的 Token，
    应使用 sanitize_message 脱敏后再存储。
    使用 sanitize_message 能识别的 sk- 前缀 Token 格式。
    """
    from app.schemas.error_sanitizer import sanitize_message

    # sk- 前缀 + 20+ 字母数字字符（sanitize_message 能识别的格式）
    full_token = "sk-abcdef1234567890abcdef12"
    raw_error = f"WxPusher error: invalid token {full_token}"
    sanitized = sanitize_message(raw_error)

    # 完整 Token 不应出现在脱敏后的消息中
    assert full_token not in sanitized
    # 应包含 *** 标记
    assert "***" in sanitized


def test_channel_store_sanitized_error_message(db_session):
    """【WP-MSG.1】channel.last_error_message 存储脱敏后的消息。"""
    from app.schemas.error_sanitizer import sanitize_message

    full_token = "sk-abcdef1234567890abcdef12"
    raw_error = f"WxPusher error: invalid token {full_token}"
    sanitized = sanitize_message(raw_error)

    channel = _make_channel(
        name="error-channel",
        last_error_code="AUTH_FAILED",
        last_error_message=sanitized,
    )
    db_session.add(channel)
    db_session.commit()
    db_session.refresh(channel)

    # 存储的错误消息不应包含完整 Token
    assert full_token not in channel.last_error_message
    assert "***" in channel.last_error_message
