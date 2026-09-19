"""白盒测试 - WP-MSG.8 渠道配置/测试/掩码。

覆盖：
1.  渠道创建与配置（字段存储）
2.  config_mask_json 脱敏（app_token 脱敏 / uid 保留）
3.  渠道状态流转（can_transition 验证每步合法）
4.  渠道测试成功（in_app send_test + channel 字段更新）
5.  渠道测试失败（wxpusher 配置错误 + channel 字段更新）
6.  渠道掩码展示（adapter.mask_config）
7.  is_selectable_for_policy 状态组合
8.  get_selection_hint 提示
9.  敏感字段不暴露（加密 + 脱敏分离）
10. 渠道删除
11. 渠道列表
12. 适配器注册表（get_adapter / list_adapters）

project_memory 硬约束：
- 错误消息不暴露敏感信息（不出现完整 Token/Secret/密码）
- 测试覆盖关联场景与边界测试
- 不修改已稳定组件实现
"""
from __future__ import annotations

import json

import pytest

from app.models.notification import (
    CHANNEL_STATUS_DISABLED,
    CHANNEL_STATUS_ENABLED,
    CHANNEL_STATUS_PENDING_TEST,
    CHANNEL_STATUS_TEST_FAILED,
    CHANNEL_STATUS_TEST_SUCCESS,
    CHANNEL_STATUS_UNCONFIGURED,
    CHANNEL_TYPE_IN_APP,
    CHANNEL_TYPE_WXPUSHER,
    NotificationChannel,
)
from app.services.notifications.channel_state import (
    STATE_DISABLED,
    STATE_ENABLED,
    STATE_PENDING_TEST,
    STATE_TEST_FAILED,
    STATE_TEST_SUCCESS,
    STATE_UNCONFIGURED,
    can_transition,
    get_selection_hint,
    is_selectable_for_policy,
)
from app.services.notifications.dingtalk import DingTalkAdapter
from app.services.notifications.email import EmailAdapter
from app.services.notifications.in_app import InAppAdapter
from app.services.notifications.onebot import OneBotAdapter
from app.services.notifications.registry import get_adapter, list_adapters
from app.services.notifications.webhook import WebhookAdapter
from app.services.notifications.wxpusher import WxPusherAdapter
from app.services.notifications.outbox import _now_utc
from app.utils.secret_mask import (
    decrypt_config,
    encrypt_config,
    mask_token,
)


pytestmark = pytest.mark.whitebox


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------


def _make_channel(
    db_session,
    *,
    name: str = "ch",
    channel_type: str = CHANNEL_TYPE_IN_APP,
    enabled: bool = False,
    status: str = CHANNEL_STATUS_UNCONFIGURED,
    config_encrypted_json: str | None = None,
    config_mask_json: str | None = None,
) -> NotificationChannel:
    """创建并提交一个 channel，返回 refresh 后的实例。"""
    channel = NotificationChannel(
        name=name,
        channel_type=channel_type,
        enabled=enabled,
        status=status,
        config_encrypted_json=config_encrypted_json,
        config_mask_json=config_mask_json,
    )
    db_session.add(channel)
    db_session.commit()
    db_session.refresh(channel)
    return channel


# ----------------------------------------------------------------------------
# 1. 渠道创建与配置
# ----------------------------------------------------------------------------


def test_channel_create_and_fields_persist(db_session):
    """【WP-MSG.8】创建 NotificationChannel 后字段正确存储并可读回。"""
    channel = _make_channel(
        db_session,
        name="my-wxpusher",
        channel_type=CHANNEL_TYPE_WXPUSHER,
        enabled=False,
        status=CHANNEL_STATUS_UNCONFIGURED,
        config_encrypted_json="eyJ0b2tlbiI6ImFiYzEyMyJ9",
        config_mask_json='{"app_token":"abcd****1234"}',
    )

    # 重新查询以验证持久化
    fetched = db_session.query(NotificationChannel).filter_by(id=channel.id).one()
    assert fetched.id is not None
    assert fetched.name == "my-wxpusher"
    assert fetched.channel_type == CHANNEL_TYPE_WXPUSHER
    assert fetched.enabled is False
    assert fetched.status == CHANNEL_STATUS_UNCONFIGURED
    assert fetched.config_encrypted_json == "eyJ0b2tlbiI6ImFiYzEyMyJ9"
    assert fetched.config_mask_json == '{"app_token":"abcd****1234"}'
    assert fetched.created_at is not None


def test_channel_name_unique(db_session):
    """【WP-MSG.8】渠道名称唯一约束：重名插入报错。"""
    from sqlalchemy.exc import IntegrityError

    _make_channel(db_session, name="unique-ch")
    with pytest.raises(IntegrityError):
        _make_channel(db_session, name="unique-ch")


# ----------------------------------------------------------------------------
# 2. config_mask_json 脱敏
# ----------------------------------------------------------------------------


def test_config_mask_json_masks_app_token_and_keeps_uid(db_session):
    """【WP-MSG.8】config_mask_json 中 app_token 脱敏，uid 保留。

    模拟 API 层存储流程：
    1. 业务侧构造 config dict（含敏感 app_token + 非敏感 uid）
    2. 加密后写入 config_encrypted_json
    3. 脱敏后写入 config_mask_json（用于前端展示）
    """
    config = {
        "app_token": "AT_abcdef1234567890",
        "uid": "user123",
    }
    encrypted = encrypt_config(json.dumps(config, ensure_ascii=False))
    masked = {
        "app_token": mask_token(config["app_token"]),
        "uid": config["uid"],
    }
    mask_json = json.dumps(masked, ensure_ascii=False)

    channel = _make_channel(
        db_session,
        name="mask-demo",
        channel_type=CHANNEL_TYPE_WXPUSHER,
        config_encrypted_json=encrypted,
        config_mask_json=mask_json,
    )

    fetched = db_session.query(NotificationChannel).filter_by(id=channel.id).one()
    # app_token 脱敏：保留前 4 + 后 4，中间 ****
    assert "AT_a****7890" in fetched.config_mask_json
    # 完整 token 不出现在 mask 字段
    assert "AT_abcdef1234567890" not in fetched.config_mask_json
    # uid 保留
    assert "user123" in fetched.config_mask_json
    # 加密字段不含明文 token
    assert "AT_abcdef1234567890" not in (fetched.config_encrypted_json or "")
    # 解密可还原原始 config
    decrypted = decrypt_config(fetched.config_encrypted_json)
    assert json.loads(decrypted)["app_token"] == "AT_abcdef1234567890"


# ----------------------------------------------------------------------------
# 3. 渠道状态流转
# ----------------------------------------------------------------------------


def test_channel_status_lifecycle_transitions(db_session):
    """【WP-MSG.8】渠道状态完整生命周期流转，每步用 can_transition 验证合法。

    unconfigured → pending_test → test_success → enabled → disabled
    以及 pending_test → test_failed → pending_test（重新测试）
    """
    channel = _make_channel(
        db_session, name="lifecycle-ch", channel_type=CHANNEL_TYPE_WXPUSHER,
    )
    assert channel.status == CHANNEL_STATUS_UNCONFIGURED

    # 1. unconfigured → pending_test（配置完成进入待测试）
    assert can_transition(channel.status, CHANNEL_STATUS_PENDING_TEST) is True
    channel.status = CHANNEL_STATUS_PENDING_TEST
    db_session.commit()

    # 2. pending_test → test_failed（测试失败为独立状态）
    assert can_transition(channel.status, CHANNEL_STATUS_TEST_FAILED) is True
    channel.status = CHANNEL_STATUS_TEST_FAILED
    db_session.commit()

    # 3. test_failed → pending_test（重新测试）
    assert can_transition(channel.status, CHANNEL_STATUS_PENDING_TEST) is True
    channel.status = CHANNEL_STATUS_PENDING_TEST
    db_session.commit()

    # 4. pending_test → test_success（测试成功）
    assert can_transition(channel.status, CHANNEL_STATUS_TEST_SUCCESS) is True
    channel.status = CHANNEL_STATUS_TEST_SUCCESS
    db_session.commit()

    # 5. test_success → enabled（启用）
    assert can_transition(channel.status, CHANNEL_STATUS_ENABLED) is True
    channel.status = CHANNEL_STATUS_ENABLED
    channel.enabled = True
    db_session.commit()

    # 6. enabled → disabled（禁用）
    assert can_transition(channel.status, CHANNEL_STATUS_DISABLED) is True
    channel.status = CHANNEL_STATUS_DISABLED
    channel.enabled = False
    db_session.commit()

    # 7. disabled → enabled（恢复）
    assert can_transition(channel.status, CHANNEL_STATUS_ENABLED) is True
    channel.status = CHANNEL_STATUS_ENABLED
    channel.enabled = True
    db_session.commit()

    # 持久化检查
    fetched = db_session.query(NotificationChannel).filter_by(id=channel.id).one()
    assert fetched.status == CHANNEL_STATUS_ENABLED
    assert fetched.enabled is True


def test_channel_status_illegal_transitions_rejected(db_session):
    """【WP-MSG.8】非法状态流转被 can_transition 拒绝（边界测试）。"""
    # 未配置不能直接启用（必须先测试）
    assert can_transition(STATE_UNCONFIGURED, STATE_ENABLED) is False
    # 未配置不能直接测试成功
    assert can_transition(STATE_UNCONFIGURED, STATE_TEST_SUCCESS) is False
    # 测试失败不能直接启用（必须重新测试）
    assert can_transition(STATE_TEST_FAILED, STATE_ENABLED) is False
    # 已启用不能回退到测试成功
    assert can_transition(STATE_ENABLED, STATE_TEST_SUCCESS) is False
    # 未知状态无任何流转
    assert can_transition("unknown_state", STATE_ENABLED) is False


# ----------------------------------------------------------------------------
# 4. 渠道测试成功（in_app）
# ----------------------------------------------------------------------------


def test_channel_test_success_in_app(db_session):
    """【WP-MSG.8】in_app 渠道调用 adapter.send_test 成功，channel 字段更新。

    模拟 API 层"测试渠道"工作流：
    1. 调用 adapter.send_test(config)
    2. 根据 SendResult 更新 channel.last_test_at / last_test_success / status
    """
    channel = _make_channel(
        db_session,
        name="in-app-test",
        channel_type=CHANNEL_TYPE_IN_APP,
        enabled=True,
        status=CHANNEL_STATUS_ENABLED,
    )

    adapter = get_adapter(CHANNEL_TYPE_IN_APP)
    assert adapter is not None
    result = adapter.send_test({})

    # 适配器返回成功
    assert result.success is True
    assert result.status_code == 200

    # 模拟 API 层根据结果更新 channel
    channel.last_test_at = _now_utc()
    channel.last_test_success = True
    channel.last_error_code = None
    channel.last_error_message = None
    db_session.commit()

    fetched = db_session.query(NotificationChannel).filter_by(id=channel.id).one()
    assert fetched.last_test_at is not None
    assert fetched.last_test_success is True
    assert fetched.last_error_code is None


# ----------------------------------------------------------------------------
# 5. 渠道测试失败（wxpusher 配置错误）
# ----------------------------------------------------------------------------


def test_channel_test_failure_wxpusher_invalid_config(db_session):
    """【WP-MSG.8】wxpusher 渠道配置错误 → send_test 失败，channel 字段更新。

    使用缺 app_token 的配置：validate_config 在网络调用前直接返回失败，
    无需 mock httpx，验证错误码与脱敏错误消息写入 channel。
    """
    channel = _make_channel(
        db_session,
        name="wx-fail",
        channel_type=CHANNEL_TYPE_WXPUSHER,
        enabled=False,
        status=CHANNEL_STATUS_PENDING_TEST,
        # 加密存储的错误配置（缺 app_token）
        config_encrypted_json=encrypt_config(
            json.dumps({"uids": ["uid1"]}, ensure_ascii=False)
        ),
    )

    adapter = get_adapter(CHANNEL_TYPE_WXPUSHER)
    assert adapter is not None
    # 配置缺 app_token
    result = adapter.send_test({"uids": ["uid1"]})

    # 适配器返回失败
    assert result.success is False
    assert result.error_code is not None and result.error_code != ""
    assert result.error_code == "INVALID_CONFIG"

    # 模拟 API 层根据结果更新 channel
    channel.last_test_at = _now_utc()
    channel.last_test_success = False
    channel.last_error_code = result.error_code
    channel.last_error_message = result.error_message
    # 测试失败 → 独立状态
    assert can_transition(channel.status, CHANNEL_STATUS_TEST_FAILED) is True
    channel.status = CHANNEL_STATUS_TEST_FAILED
    db_session.commit()

    fetched = db_session.query(NotificationChannel).filter_by(id=channel.id).one()
    assert fetched.last_test_at is not None
    assert fetched.last_test_success is False
    assert fetched.last_error_code == "INVALID_CONFIG"
    assert fetched.status == CHANNEL_STATUS_TEST_FAILED


def test_channel_test_failure_error_message_no_sensitive(db_session):
    """【WP-MSG.8】测试失败错误消息不暴露敏感信息（边界测试）。

    即使配置中包含疑似 token 字符串，错误消息也不应回显完整 token。
    """
    channel = _make_channel(
        db_session,
        name="wx-fail-mask",
        channel_type=CHANNEL_TYPE_WXPUSHER,
        status=CHANNEL_STATUS_PENDING_TEST,
    )

    adapter = get_adapter(CHANNEL_TYPE_WXPUSHER)
    # 配置含敏感 token 但 uids 为空（触发 uids 校验错误）
    sensitive_token = "AT_super_secret_token_value_1234567890"
    result = adapter.send_test({"app_token": sensitive_token})

    assert result.success is False
    assert result.error_code is not None
    # 错误消息中不出现完整 token
    assert sensitive_token not in (result.error_message or "")

    channel.last_error_message = result.error_message
    db_session.commit()

    fetched = db_session.query(NotificationChannel).filter_by(id=channel.id).one()
    assert sensitive_token not in (fetched.last_error_message or "")


# ----------------------------------------------------------------------------
# 6. 渠道掩码展示（adapter.mask_config）
# ----------------------------------------------------------------------------


def test_channel_mask_config_wxpusher(db_session):
    """【WP-MSG.8】wxpusher adapter.mask_config：app_token 脱敏，uids 保留。"""
    _make_channel(
        db_session,
        name="wx-mask",
        channel_type=CHANNEL_TYPE_WXPUSHER,
        status=CHANNEL_STATUS_ENABLED,
        enabled=True,
    )
    adapter = get_adapter(CHANNEL_TYPE_WXPUSHER)
    raw_config = {
        "app_token": "AT_abcdef1234567890",
        "uids": ["uid_abc", "uid_def"],
        "topic_ids": [123, 456],
        "content_type": 3,
        "timeout": 10,
    }
    masked = adapter.mask_config(raw_config)

    # app_token 脱敏（前 4 + 后 4）
    assert masked["app_token"] == "AT_a****7890"
    assert "AT_abcdef1234567890" not in str(masked)
    # uids / topic_ids 保留原值
    assert masked["uids"] == ["uid_abc", "uid_def"]
    assert masked["topic_ids"] == [123, 456]
    # 非敏感配置项保留
    assert masked["content_type"] == 3
    assert masked["timeout"] == 10


def test_channel_mask_config_in_app_empty():
    """【WP-MSG.8】in_app adapter.mask_config 返回空 dict（无敏感字段）。"""
    adapter = get_adapter(CHANNEL_TYPE_IN_APP)
    masked = adapter.mask_config({"any": "thing"})
    assert masked == {}


# ----------------------------------------------------------------------------
# 7. is_selectable_for_policy 状态组合
# ----------------------------------------------------------------------------


def _channel(
    *,
    channel_type: str = CHANNEL_TYPE_WXPUSHER,
    enabled: bool = True,
    status: str = CHANNEL_STATUS_ENABLED,
) -> NotificationChannel:
    """构造一个未持久化的 channel 用于状态组合判断。

    默认使用 wxpusher（第三方渠道），因为 is_selectable_for_policy /
    get_selection_hint 的状态分支主要针对第三方渠道；in_app 走单独分支。
    """
    return NotificationChannel(
        name="t", channel_type=channel_type, enabled=enabled, status=status,
    )


def test_is_selectable_in_app_enabled():
    """【WP-MSG.8】in_app + enabled → 可选。"""
    assert is_selectable_for_policy(
        _channel(channel_type=CHANNEL_TYPE_IN_APP, enabled=True, status=CHANNEL_STATUS_ENABLED)
    ) is True


def test_is_selectable_in_app_disabled():
    """【WP-MSG.8】in_app + disabled → 不可选。"""
    assert is_selectable_for_policy(
        _channel(channel_type=CHANNEL_TYPE_IN_APP, enabled=False, status=CHANNEL_STATUS_ENABLED)
    ) is False


def test_is_selectable_wxpusher_enabled_status_enabled():
    """【WP-MSG.8】wxpusher + enabled + status=enabled → 可选。"""
    assert is_selectable_for_policy(
        _channel(
            channel_type=CHANNEL_TYPE_WXPUSHER,
            enabled=True,
            status=CHANNEL_STATUS_ENABLED,
        )
    ) is True


def test_is_selectable_wxpusher_test_success_not_enabled_status():
    """【WP-MSG.8】wxpusher + enabled=True + status=test_success → 不可选（需 enabled 状态）。"""
    assert is_selectable_for_policy(
        _channel(
            channel_type=CHANNEL_TYPE_WXPUSHER,
            enabled=True,
            status=CHANNEL_STATUS_TEST_SUCCESS,
        )
    ) is False


def test_is_selectable_wxpusher_test_failed():
    """【WP-MSG.8】wxpusher + status=test_failed → 不可选。"""
    assert is_selectable_for_policy(
        _channel(
            channel_type=CHANNEL_TYPE_WXPUSHER,
            enabled=True,
            status=CHANNEL_STATUS_TEST_FAILED,
        )
    ) is False


def test_is_selectable_wxpusher_unconfigured():
    """【WP-MSG.8】wxpusher + status=unconfigured → 不可选。"""
    assert is_selectable_for_policy(
        _channel(
            channel_type=CHANNEL_TYPE_WXPUSHER,
            enabled=False,
            status=CHANNEL_STATUS_UNCONFIGURED,
        )
    ) is False


# ----------------------------------------------------------------------------
# 8. get_selection_hint 提示
# ----------------------------------------------------------------------------


def test_selection_hint_unconfigured():
    """【WP-MSG.8】unconfigured → 提示去配置。"""
    hint = get_selection_hint(_channel(status=CHANNEL_STATUS_UNCONFIGURED, enabled=False))
    assert "未配置" in hint
    assert "请先完成配置" in hint


def test_selection_hint_pending_test():
    """【WP-MSG.8】pending_test → 提示立即测试。"""
    hint = get_selection_hint(_channel(status=CHANNEL_STATUS_PENDING_TEST, enabled=False))
    assert "已配置待测试" in hint
    assert "请立即测试" in hint


def test_selection_hint_test_failed():
    """【WP-MSG.8】test_failed → 提示重新测试。"""
    hint = get_selection_hint(_channel(status=CHANNEL_STATUS_TEST_FAILED, enabled=False))
    assert "测试失败" in hint
    assert "重新测试" in hint


def test_selection_hint_test_success_not_enabled():
    """【WP-MSG.8】test_success + 未启用 → 提示启用。"""
    hint = get_selection_hint(
        _channel(status=CHANNEL_STATUS_TEST_SUCCESS, enabled=False)
    )
    assert "测试成功" in hint
    assert "请启用" in hint


def test_selection_hint_enabled_empty():
    """【WP-MSG.8】enabled → 无提示（空串）。"""
    hint = get_selection_hint(
        _channel(status=CHANNEL_STATUS_ENABLED, enabled=True)
    )
    assert hint == ""


# ----------------------------------------------------------------------------
# 9. 敏感字段不暴露
# ----------------------------------------------------------------------------


def test_sensitive_fields_not_exposed_in_mask(db_session):
    """【WP-MSG.8】敏感字段加密存储，展示用 config_mask_json 不含完整 Token/Secret/密码。

    验证：
    - config_encrypted_json 不等于明文 config
    - config_mask_json 不含完整敏感值
    - 解密可还原（加密可逆，用于实际发送）
    """
    raw_config = {
        "app_token": "AT_abcdef1234567890",
        "uids": ["uid1"],
    }
    encrypted = encrypt_config(json.dumps(raw_config, ensure_ascii=False))
    masked = {
        "app_token": mask_token(raw_config["app_token"]),
        "uids": raw_config["uids"],
    }
    mask_json = json.dumps(masked, ensure_ascii=False)

    channel = _make_channel(
        db_session,
        name="sensitive-ch",
        channel_type=CHANNEL_TYPE_WXPUSHER,
        config_encrypted_json=encrypted,
        config_mask_json=mask_json,
    )
    fetched = db_session.query(NotificationChannel).filter_by(id=channel.id).one()

    # 加密字段非明文
    assert fetched.config_encrypted_json != json.dumps(raw_config, ensure_ascii=False)
    # mask 字段不含完整 token
    assert "AT_abcdef1234567890" not in (fetched.config_mask_json or "")
    # 解密还原
    decrypted_config = json.loads(decrypt_config(fetched.config_encrypted_json))
    assert decrypted_config["app_token"] == "AT_abcdef1234567890"


def test_sensitive_fields_mask_used_for_display_not_encrypted(db_session):
    """【WP-MSG.8】展示场景使用 config_mask_json 而非 config_encrypted_json。

    模拟前端读取渠道：应使用 mask 版本，encrypted 版本仅供后端发送使用。
    """
    raw_config = {"app_token": "AT_secret_token_1234567890abcdef", "uids": ["u1"]}
    encrypted = encrypt_config(json.dumps(raw_config, ensure_ascii=False))
    mask_json = json.dumps(
        {"app_token": mask_token(raw_config["app_token"]), "uids": ["u1"]},
        ensure_ascii=False,
    )
    channel = _make_channel(
        db_session,
        name="display-ch",
        channel_type=CHANNEL_TYPE_WXPUSHER,
        config_encrypted_json=encrypted,
        config_mask_json=mask_json,
    )

    fetched = db_session.query(NotificationChannel).filter_by(id=channel.id).one()
    # 展示字段
    display = fetched.config_mask_json
    assert display is not None
    # 脱敏后保留前 4 + 后 4：AT_s + cdef
    assert "AT_s****cdef" in display
    assert "AT_secret_token_1234567890abcdef" not in display
    # 加密字段仅供后端使用，含 base64 编码
    assert fetched.config_encrypted_json is not None
    assert "AT_secret_token_1234567890abcdef" not in (fetched.config_encrypted_json or "")


# ----------------------------------------------------------------------------
# 10. 渠道删除
# ----------------------------------------------------------------------------


def test_channel_delete(db_session):
    """【WP-MSG.8】删除渠道后查询不到。

    注：级联删除策略-渠道关联（ondelete=CASCADE）已在 WP-MSG.1
    test_whitebox_notification_models_schema.py::test_policy_channel_cascade_delete_on_channel
    中通过 PRAGMA foreign_keys=ON 覆盖，此处不重复。
    """
    channel = _make_channel(db_session, name="to-delete")
    channel_id = channel.id

    db_session.delete(channel)
    db_session.commit()

    found = db_session.query(NotificationChannel).filter_by(id=channel_id).first()
    assert found is None


# ----------------------------------------------------------------------------
# 11. 渠道列表
# ----------------------------------------------------------------------------


def test_channel_list_multiple(db_session):
    """【WP-MSG.8】创建 3 个渠道后列表查询返回 3 个。"""
    _make_channel(db_session, name="list-ch-1", channel_type=CHANNEL_TYPE_IN_APP)
    _make_channel(db_session, name="list-ch-2", channel_type=CHANNEL_TYPE_WXPUSHER)
    _make_channel(db_session, name="list-ch-3", channel_type=CHANNEL_TYPE_IN_APP)

    channels = db_session.query(NotificationChannel).order_by(NotificationChannel.id).all()
    assert len(channels) == 3
    names = {c.name for c in channels}
    assert names == {"list-ch-1", "list-ch-2", "list-ch-3"}


def test_channel_list_filter_by_type(db_session):
    """【WP-MSG.8】按 channel_type 过滤渠道列表（关联场景）。"""
    _make_channel(db_session, name="ia-1", channel_type=CHANNEL_TYPE_IN_APP)
    _make_channel(db_session, name="ia-2", channel_type=CHANNEL_TYPE_IN_APP)
    _make_channel(db_session, name="wx-1", channel_type=CHANNEL_TYPE_WXPUSHER)

    in_app_channels = (
        db_session.query(NotificationChannel)
        .filter(NotificationChannel.channel_type == CHANNEL_TYPE_IN_APP)
        .all()
    )
    assert len(in_app_channels) == 2

    wx_channels = (
        db_session.query(NotificationChannel)
        .filter(NotificationChannel.channel_type == CHANNEL_TYPE_WXPUSHER)
        .all()
    )
    assert len(wx_channels) == 1


def test_channel_list_filter_by_status(db_session):
    """【WP-MSG.8】按 status 过滤渠道列表（关联场景）。"""
    _make_channel(
        db_session, name="enabled-ch",
        channel_type=CHANNEL_TYPE_IN_APP, enabled=True, status=CHANNEL_STATUS_ENABLED,
    )
    _make_channel(
        db_session, name="unconfigured-ch",
        channel_type=CHANNEL_TYPE_WXPUSHER, status=CHANNEL_STATUS_UNCONFIGURED,
    )
    _make_channel(
        db_session, name="failed-ch",
        channel_type=CHANNEL_TYPE_WXPUSHER, status=CHANNEL_STATUS_TEST_FAILED,
    )

    enabled = (
        db_session.query(NotificationChannel)
        .filter(NotificationChannel.status == CHANNEL_STATUS_ENABLED)
        .all()
    )
    assert len(enabled) == 1
    assert enabled[0].name == "enabled-ch"

    selectable = (
        db_session.query(NotificationChannel)
        .filter(NotificationChannel.status == CHANNEL_STATUS_ENABLED)
        .filter(NotificationChannel.enabled.is_(True))
        .all()
    )
    assert len(selectable) == 1


# ----------------------------------------------------------------------------
# 12. 适配器注册表
# ----------------------------------------------------------------------------


def test_registry_get_adapter_in_app():
    """【WP-MSG.8】get_adapter('in_app') 返回 InAppAdapter 实例。"""
    adapter = get_adapter("in_app")
    assert adapter is not None
    assert isinstance(adapter, InAppAdapter)
    assert adapter.channel_type == "in_app"


def test_registry_get_adapter_wxpusher():
    """【WP-MSG.8】get_adapter('wxpusher') 返回 WxPusherAdapter 实例。"""
    adapter = get_adapter("wxpusher")
    assert adapter is not None
    assert isinstance(adapter, WxPusherAdapter)
    assert adapter.channel_type == "wxpusher"


def test_registry_get_adapter_dingtalk():
    """【WP-MSG.8】get_adapter('dingtalk') 返回 DingTalkAdapter 实例。"""
    adapter = get_adapter("dingtalk")
    assert adapter is not None
    assert isinstance(adapter, DingTalkAdapter)
    assert adapter.channel_type == "dingtalk"


def test_registry_get_adapter_onebot():
    """【WP-MSG.8】get_adapter('onebot') 返回 OneBotAdapter 实例。"""
    adapter = get_adapter("onebot")
    assert adapter is not None
    assert isinstance(adapter, OneBotAdapter)
    assert adapter.channel_type == "onebot"


def test_registry_get_adapter_email():
    """【WP-MSG.8】get_adapter('email') 返回 EmailAdapter 实例。"""
    adapter = get_adapter("email")
    assert adapter is not None
    assert isinstance(adapter, EmailAdapter)
    assert adapter.channel_type == "email"


def test_registry_get_adapter_webhook():
    """【WP-MSG.8】get_adapter('webhook') 返回 WebhookAdapter 实例。"""
    adapter = get_adapter("webhook")
    assert adapter is not None
    assert isinstance(adapter, WebhookAdapter)
    assert adapter.channel_type == "webhook"


def test_registry_get_adapter_unknown_returns_none():
    """【WP-MSG.8】get_adapter('unknown') 返回 None（边界测试）。"""
    assert get_adapter("unknown") is None
    assert get_adapter("") is None


def test_registry_list_adapters_returns_six():
    """【WP-MSG.8】list_adapters 返回 6 个适配器。"""
    adapters = list_adapters()
    assert len(adapters) == 6
    expected_keys = {"in_app", "wxpusher", "dingtalk", "onebot", "email", "webhook"}
    assert set(adapters.keys()) == expected_keys


def test_registry_list_adapters_returns_copy():
    """【WP-MSG.8】list_adapters 返回副本，修改不影响内部注册表（边界测试）。"""
    adapters = list_adapters()
    adapters["injected"] = InAppAdapter()  # type: ignore
    del adapters["in_app"]

    # 内部注册表不受影响
    fresh = list_adapters()
    assert "in_app" in fresh
    assert "injected" not in fresh
    assert len(fresh) == 6


# ----------------------------------------------------------------------------
# 13. 端到端：渠道配置 → 测试 → 启用 → 可选（关联场景）
# ----------------------------------------------------------------------------


def test_channel_full_workflow_config_test_enable_selectable(db_session):
    """【WP-MSG.8】端到端关联场景：配置 → 测试 → 启用 → 可被策略选中。

    覆盖状态机 + adapter.send_test + is_selectable_for_policy 的联动。
    """
    # 1. 创建未配置的 wxpusher 渠道
    channel = _make_channel(
        db_session, name="e2e-wx", channel_type=CHANNEL_TYPE_WXPUSHER,
    )
    assert channel.status == CHANNEL_STATUS_UNCONFIGURED
    assert is_selectable_for_policy(channel) is False

    # 2. 配置完成 → pending_test
    assert can_transition(channel.status, CHANNEL_STATUS_PENDING_TEST) is True
    channel.status = CHANNEL_STATUS_PENDING_TEST
    db_session.commit()
    assert is_selectable_for_policy(channel) is False  # 仍不可选

    # 3. 调用 adapter.send_test（使用合法配置 + mock httpx 成功）
    from unittest.mock import MagicMock

    class _MockResponse:
        def __init__(self):
            self.status_code = 200
            self.text = ""

        def json(self):
            return {"code": 0, "msg": "ok", "data": {"messageId": "m-1"}}

    class _MockClient:
        def __init__(self, **kw):
            self._resp = _MockResponse()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, *args, **kwargs):
            return self._resp

    import app.services.notifications.wxpusher as wxpusher_mod

    original_client = wxpusher_mod.httpx.Client
    wxpusher_mod.httpx.Client = _MockClient  # type: ignore
    try:
        adapter = get_adapter(CHANNEL_TYPE_WXPUSHER)
        result = adapter.send_test({
            "app_token": "AT_test_token_1234567890abcdef",
            "uids": ["uid_e2e"],
        })
    finally:
        wxpusher_mod.httpx.Client = original_client  # type: ignore

    assert result.success is True
    # 响应摘要不含完整 token
    assert "AT_test_token_1234567890abcdef" not in (result.response_summary or "")

    # 4. 测试成功 → test_success
    assert can_transition(channel.status, CHANNEL_STATUS_TEST_SUCCESS) is True
    channel.status = CHANNEL_STATUS_TEST_SUCCESS
    channel.last_test_at = _now_utc()
    channel.last_test_success = True
    db_session.commit()
    # test_success 但未启用 → 仍不可选
    assert is_selectable_for_policy(channel) is False

    # 5. 启用 → enabled
    assert can_transition(channel.status, CHANNEL_STATUS_ENABLED) is True
    channel.status = CHANNEL_STATUS_ENABLED
    channel.enabled = True
    db_session.commit()

    # 6. 现在可被策略选中
    db_session.refresh(channel)
    assert is_selectable_for_policy(channel) is True
    assert get_selection_hint(channel) == ""
