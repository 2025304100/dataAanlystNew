"""通知消息管理 HTTP API（WP-MSG.6 / UAT-P0.1）。

路由前缀 `/api/v1/notifications`，端点：
- 渠道：GET/POST /channels、PATCH/DELETE /channels/{id}、POST /channels/{id}/test
- 策略：GET/POST /policies、PATCH/DELETE /policies/{id}
- 模板：GET/POST /templates、PATCH/DELETE /templates/{id}、POST /templates/{id}/preview
- 发送记录：GET /deliveries、GET /outbox、GET /outbox/{id}/deliveries、POST /outbox/{id}/retry

实现要求：
- 复用 services/notifications 服务层，不在路由层重复业务逻辑
- 渠道 config 返回时走 mask_config 脱敏，永不出 config_encrypted_json
- 错误使用统一错误协议（UnifiedErrorException），中文 user_message
- 策略渠道勾选受状态机约束（is_selectable_for_policy）
- 模板预览变量转义防止 Markdown/Webhook 注入
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.notification import (
    CHANNEL_STATUS_DISABLED,
    CHANNEL_STATUS_ENABLED,
    CHANNEL_STATUS_PENDING_TEST,
    CHANNEL_STATUS_TEST_FAILED,
    CHANNEL_STATUS_TEST_SUCCESS,
    CHANNEL_STATUS_UNCONFIGURED,
    CHANNEL_TYPES,
    CHANNEL_TYPE_IN_APP,
    NotificationChannel,
    NotificationDelivery,
    NotificationOutbox,
    NotificationPolicy,
    NotificationPolicyChannel,
    NotificationTemplate,
)
from app.schemas.error_sanitizer import sanitize_message
from app.schemas.errors import NextAction, TechnicalDetails, UnifiedErrorException
from app.schemas.notification import (
    ChannelCreate,
    ChannelRead,
    ChannelTestResult,
    ChannelUpdate,
    DeliveryRead,
    OutboxRead,
    PolicyCreate,
    PolicyRead,
    PolicyUpdate,
    TemplateCreate,
    TemplatePreviewRequest,
    TemplatePreviewResponse,
    TemplateRead,
    TemplateUpdate,
)
from app.services.notifications.base import SendResult
from app.services.notifications.channel_state import (
    can_transition,
    get_selection_hint,
    is_selectable_for_policy,
)
from app.services.notifications.registry import get_adapter
from app.services.notifications.template_renderer import render_template
from app.utils.secret_mask import decrypt_config, encrypt_config

logger = logging.getLogger(__name__)

router = APIRouter()


def _now_utc_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _raise(
    error_code: str,
    *,
    status_code: int,
    user_message: str,
    impact: str | None = None,
    extra_next_actions: list[NextAction] | None = None,
    technical_message: str | None = None,
) -> None:
    """构造并抛出 UnifiedErrorException（统一错误协议）。"""
    technical_details = None
    if technical_message:
        technical_details = TechnicalDetails(
            exception_type="NotificationAPIError",
            status_code=status_code,
            error_message=sanitize_message(technical_message)[:500],
        )
    raise UnifiedErrorException(
        error_code,
        status_code=status_code,
        override_user_message=user_message,
        override_impact=impact,
        extra_next_actions=extra_next_actions,
        technical_details=technical_details,
    )


def _serialize_channel(channel: NotificationChannel) -> dict[str, Any]:
    """序列化渠道为响应字典（脱敏：仅 config_mask_json）。"""
    return {
        "id": channel.id,
        "name": channel.name,
        "channel_type": channel.channel_type,
        "enabled": channel.enabled,
        "status": channel.status,
        "config_mask_json": channel.config_mask_json,
        "verified_at": channel.verified_at.isoformat() if channel.verified_at else None,
        "last_test_at": channel.last_test_at.isoformat() if channel.last_test_at else None,
        "last_test_success": channel.last_test_success,
        "last_error_code": channel.last_error_code,
        "last_error_message": channel.last_error_message,
        "created_at": channel.created_at.isoformat() if channel.created_at else None,
        "updated_at": channel.updated_at.isoformat() if channel.updated_at else None,
    }


def _serialize_policy(policy: NotificationPolicy, channel_ids: list[int]) -> dict[str, Any]:
    """序列化策略为响应字典（附带 channel_ids）。"""
    return {
        "id": policy.id,
        "name": policy.name,
        "enabled": policy.enabled,
        "source_types_json": policy.source_types_json,
        "min_severity": policy.min_severity,
        "scope_type": policy.scope_type,
        "scope_ids_json": policy.scope_ids_json,
        "delivery_mode": policy.delivery_mode,
        "digest_schedule": policy.digest_schedule,
        "quiet_hours_json": policy.quiet_hours_json,
        "cooldown_minutes": policy.cooldown_minutes,
        "dedup_window_minutes": policy.dedup_window_minutes,
        "template_id": policy.template_id,
        "channel_ids": channel_ids,
        "created_at": policy.created_at.isoformat() if policy.created_at else None,
        "updated_at": policy.updated_at.isoformat() if policy.updated_at else None,
    }


def _serialize_template(template: NotificationTemplate) -> dict[str, Any]:
    """序列化模板为响应字典。"""
    return {
        "id": template.id,
        "name": template.name,
        "title_template": template.title_template,
        "body_template": template.body_template,
        "body_text_template": template.body_text_template,
        "variables_json": template.variables_json,
        "version": template.version,
        "is_active": template.is_active,
        "created_at": template.created_at.isoformat() if template.created_at else None,
        "updated_at": template.updated_at.isoformat() if template.updated_at else None,
    }


def _serialize_delivery(delivery: NotificationDelivery) -> dict[str, Any]:
    """序列化发送记录为响应字典。"""
    return {
        "id": delivery.id,
        "outbox_id": delivery.outbox_id,
        "channel_id": delivery.channel_id,
        "attempt_number": delivery.attempt_number,
        "status": delivery.status,
        "status_code": delivery.status_code,
        "response_summary": delivery.response_summary,
        "error_code": delivery.error_code,
        "error_message": delivery.error_message,
        "duration_ms": delivery.duration_ms,
        "sent_at": delivery.sent_at.isoformat() if delivery.sent_at else None,
        "created_at": delivery.created_at.isoformat() if delivery.created_at else None,
    }


def _serialize_outbox(outbox: NotificationOutbox) -> dict[str, Any]:
    """序列化发件箱记录为响应字典。"""
    return {
        "id": outbox.id,
        "event_key": outbox.event_key,
        "source_type": outbox.source_type,
        "source_id": outbox.source_id,
        "event_type": outbox.event_type,
        "severity": outbox.severity,
        "payload_json": outbox.payload_json,
        "channel_id": outbox.channel_id,
        "policy_id": outbox.policy_id,
        "status": outbox.status,
        "attempt_count": outbox.attempt_count,
        "max_attempts": outbox.max_attempts,
        "next_retry_at": outbox.next_retry_at.isoformat() if outbox.next_retry_at else None,
        "last_error_code": outbox.last_error_code,
        "last_error_message": outbox.last_error_message,
        "sent_at": outbox.sent_at.isoformat() if outbox.sent_at else None,
        "created_at": outbox.created_at.isoformat() if outbox.created_at else None,
        "updated_at": outbox.updated_at.isoformat() if outbox.updated_at else None,
    }


def _get_policy_channel_ids(db: Session, policy_id: int) -> list[int]:
    rows = db.execute(
        select(NotificationPolicyChannel.channel_id).where(
            NotificationPolicyChannel.policy_id == policy_id
        )
    ).scalars().all()
    return list(rows)


def _decrypt_channel_config(channel: NotificationChannel) -> dict[str, Any]:
    """解密渠道配置（容错：解密失败返回空字典）。"""
    try:
        return (
            json.loads(decrypt_config(channel.config_encrypted_json))
            if channel.config_encrypted_json
            else {}
        )
    except Exception:
        return {}


# ============================================================================
# 渠道 Channels
# ============================================================================


@router.get("/notifications/channels", response_model=list[ChannelRead])
def list_channels(
    enabled: bool | None = Query(None, description="按启用状态筛选"),
    status: str | None = Query(None, description="按渠道状态筛选"),
    channel_type: str | None = Query(None, description="按渠道类型筛选"),
    db: Session = Depends(get_db),
):
    """列出渠道（支持 enabled/status/channel_type 筛选），按 id 倒序。"""
    stmt = select(NotificationChannel)
    if enabled is not None:
        stmt = stmt.where(NotificationChannel.enabled == enabled)
    if status is not None:
        stmt = stmt.where(NotificationChannel.status == status)
    if channel_type is not None:
        stmt = stmt.where(NotificationChannel.channel_type == channel_type)
    stmt = stmt.order_by(NotificationChannel.id.desc())
    channels = db.execute(stmt).scalars().all()
    return [_serialize_channel(ch) for ch in channels]


@router.post("/notifications/channels", response_model=ChannelRead)
def create_channel(payload: ChannelCreate, db: Session = Depends(get_db)):
    """创建渠道。

    config 加密存储于 config_encrypted_json，脱敏版本存于 config_mask_json。
    - in_app：无需配置，初始状态 test_success（无需测试即可启用）
    - 第三方：配置后初始状态 pending_test（待测试）
    """
    if payload.channel_type not in CHANNEL_TYPES:
        _raise(
            "VALIDATION_ERROR",
            status_code=400,
            user_message=f"不支持的渠道类型：{payload.channel_type}",
            impact="请选择 in_app/wxpusher/dingtalk/onebot/email/webhook 之一",
        )

    existing = db.execute(
        select(NotificationChannel).where(NotificationChannel.name == payload.name)
    ).scalars().first()
    if existing is not None:
        _raise(
            "DB_INTEGRITY_VIOLATION",
            status_code=409,
            user_message=f"渠道名称已存在：{payload.name}",
            impact="请更换名称后重试",
        )

    adapter = get_adapter(payload.channel_type)
    if adapter is None:
        _raise(
            "VALIDATION_ERROR",
            status_code=400,
            user_message=f"未找到 {payload.channel_type} 渠道适配器",
        )

    # 配置校验
    errors = adapter.validate_config(payload.config)
    if errors:
        _raise(
            "VALIDATION_ERROR",
            status_code=400,
            user_message="渠道配置校验失败：" + "；".join(errors),
        )

    config_json = json.dumps(payload.config, ensure_ascii=False)
    encrypted = encrypt_config(config_json)
    masked = json.dumps(adapter.mask_config(payload.config), ensure_ascii=False)

    # in_app 无需测试，直接进入 test_success；第三方需测试，进入 pending_test
    initial_status = (
        CHANNEL_STATUS_TEST_SUCCESS
        if payload.channel_type == CHANNEL_TYPE_IN_APP
        else CHANNEL_STATUS_PENDING_TEST
    )

    channel = NotificationChannel(
        name=payload.name,
        channel_type=payload.channel_type,
        enabled=False,
        status=initial_status,
        config_encrypted_json=encrypted,
        config_mask_json=masked,
        created_at=_now_utc_naive(),
    )
    db.add(channel)
    db.commit()
    db.refresh(channel)
    return _serialize_channel(channel)


@router.patch("/notifications/channels/{channel_id}", response_model=ChannelRead)
def update_channel(
    channel_id: int, payload: ChannelUpdate, db: Session = Depends(get_db)
):
    """更新渠道（名称/启用状态/配置）。

    - 更新 config：重新加密 + 脱敏，第三方渠道状态回退 pending_test（需重新测试）
    - 启用/禁用：受状态机约束（test_success/disabled → enabled；enabled → disabled）
    """
    channel = db.get(NotificationChannel, channel_id)
    if channel is None:
        _raise(
            "NOT_FOUND",
            status_code=404,
            user_message=f"渠道不存在：id={channel_id}",
        )

    # 名称更新（含唯一性校验）
    if payload.name is not None and payload.name != channel.name:
        dup = db.execute(
            select(NotificationChannel).where(NotificationChannel.name == payload.name)
        ).scalars().first()
        if dup is not None:
            _raise(
                "DB_INTEGRITY_VIOLATION",
                status_code=409,
                user_message=f"渠道名称已存在：{payload.name}",
            )
        channel.name = payload.name

    # 配置更新
    if payload.config is not None:
        adapter = get_adapter(channel.channel_type)
        errors = adapter.validate_config(payload.config) if adapter else []
        if errors:
            _raise(
                "VALIDATION_ERROR",
                status_code=400,
                user_message="渠道配置校验失败：" + "；".join(errors),
            )
        config_json = json.dumps(payload.config, ensure_ascii=False)
        channel.config_encrypted_json = encrypt_config(config_json)
        channel.config_mask_json = json.dumps(
            adapter.mask_config(payload.config) if adapter else {}, ensure_ascii=False
        )
        # 第三方渠道改配置需重新测试
        if channel.channel_type != CHANNEL_TYPE_IN_APP:
            channel.status = CHANNEL_STATUS_PENDING_TEST
            channel.verified_at = None
            # 改配置后若处于 enabled，回退到 test_success 等待重新测试/启用
            if channel.enabled:
                channel.enabled = False

    # 启用/禁用（状态机校验）
    if payload.enabled is not None and payload.enabled != channel.enabled:
        if payload.enabled:  # 启用
            if channel.channel_type == CHANNEL_TYPE_IN_APP:
                # in_app 仅需 enabled=True，不强制状态流转
                channel.enabled = True
            elif channel.status in (CHANNEL_STATUS_TEST_SUCCESS, CHANNEL_STATUS_DISABLED):
                channel.enabled = True
                channel.status = CHANNEL_STATUS_ENABLED
            else:
                hint = get_selection_hint(channel) or "请先完成测试"
                _raise(
                    "PORTFOLIO_RULE_INVALID",
                    status_code=400,
                    user_message=f"渠道 {channel.name} 当前不可启用：{hint}",
                    impact="该渠道尚未通过测试，无法启用",
                    extra_next_actions=[NextAction(
                        label="立即测试", action_type="configure",
                        reason="测试成功后即可启用",
                    )],
                )
        else:  # 禁用
            channel.enabled = False
            if channel.status == CHANNEL_STATUS_ENABLED:
                channel.status = CHANNEL_STATUS_DISABLED

    channel.updated_at = _now_utc_naive()
    db.commit()
    db.refresh(channel)
    return _serialize_channel(channel)


@router.delete("/notifications/channels/{channel_id}")
def delete_channel(
    channel_id: int,
    hard: bool = Query(False, description="True 物理删除，默认软删除（禁用）"),
    db: Session = Depends(get_db),
):
    """删除渠道（默认软删除：禁用并标记 disabled）。"""
    channel = db.get(NotificationChannel, channel_id)
    if channel is None:
        _raise(
            "NOT_FOUND",
            status_code=404,
            user_message=f"渠道不存在：id={channel_id}",
        )

    if hard:
        db.delete(channel)
    else:
        channel.enabled = False
        channel.status = CHANNEL_STATUS_DISABLED
        channel.updated_at = _now_utc_naive()
    db.commit()
    return {"ok": True, "id": channel_id}


@router.post("/notifications/channels/{channel_id}/test", response_model=ChannelTestResult)
def test_channel(channel_id: int, db: Session = Depends(get_db)):
    """测试发送渠道消息。

    - 状态机：进入 pending_test，发送后转 test_success/test_failed
    - 可追踪：写入 notification_outbox + notification_deliveries
    - 返回脱敏后的发送结果与响应摘要
    """
    channel = db.get(NotificationChannel, channel_id)
    if channel is None:
        _raise(
            "NOT_FOUND",
            status_code=404,
            user_message=f"渠道不存在：id={channel_id}",
        )

    adapter = get_adapter(channel.channel_type)
    if adapter is None:
        _raise(
            "VALIDATION_ERROR",
            status_code=400,
            user_message=f"未找到 {channel.channel_type} 渠道适配器",
        )

    config = _decrypt_channel_config(channel)

    # 状态机：进入 pending_test（仅当当前状态允许）
    if channel.status != CHANNEL_STATUS_PENDING_TEST:
        if can_transition(channel.status, CHANNEL_STATUS_PENDING_TEST) or (
            channel.status in (CHANNEL_STATUS_TEST_SUCCESS, CHANNEL_STATUS_TEST_FAILED)
        ):
            channel.status = CHANNEL_STATUS_PENDING_TEST
        # 其它状态（enabled/disabled）测试时不改前置状态，发送后按结果设置

    now = _now_utc_naive()
    channel.last_test_at = now

    # 创建可追踪的 outbox 记录（测试事件，event_key 含时间戳不防重）
    test_event_key = f"channel_test:{channel.id}:{now.isoformat()}"
    outbox = NotificationOutbox(
        event_key=test_event_key,
        source_type="system",
        source_id=None,
        event_type="channel_test",
        severity="info",
        payload_json=json.dumps(
            {"title": "渠道测试", "body": f"测试渠道 {channel.name} 连通性"},
            ensure_ascii=False,
        ),
        channel_id=channel.id,
        policy_id=None,
        status="sending",
        attempt_count=0,
        max_attempts=1,
        next_retry_at=now,
        created_at=now,
    )
    db.add(outbox)
    db.flush()  # 取 outbox.id

    # 调用适配器 send_test；异常归一化为 SendResult（统一后续处理路径）
    try:
        sr = adapter.send_test(config)
    except Exception as exc:
        code, msg = adapter.normalize_error(exc)
        sr = SendResult(success=False, error_code=code, error_message=msg, duration_ms=0)

    response_summary = (
        sanitize_message(sr.response_summary)[:500] if sr.response_summary else None
    )
    err_msg = (
        sanitize_message(sr.error_message)[:500]
        if not sr.success and sr.error_message
        else None
    )

    # 更新 outbox（可追踪）
    outbox.status = "sent" if sr.success else "failed"
    outbox.last_error_code = sr.error_code if not sr.success else None
    outbox.last_error_message = err_msg
    outbox.sent_at = now if sr.success else None
    outbox.updated_at = now

    # 写入 delivery（可追踪：测试发送写入 notification_deliveries）
    delivery = NotificationDelivery(
        outbox_id=outbox.id,
        channel_id=channel.id,
        attempt_number=1,
        status="success" if sr.success else "failed",
        status_code=sr.status_code,
        response_summary=response_summary,
        error_code=sr.error_code if not sr.success else None,
        error_message=err_msg,
        duration_ms=sr.duration_ms,
        sent_at=now,
        created_at=now,
    )
    db.add(delivery)
    db.flush()

    # 更新渠道状态机
    channel.last_test_success = sr.success
    if sr.success:
        channel.status = CHANNEL_STATUS_TEST_SUCCESS
        channel.verified_at = now
        channel.last_error_code = None
        channel.last_error_message = None
    else:
        channel.status = CHANNEL_STATUS_TEST_FAILED
        channel.last_error_code = sr.error_code
        channel.last_error_message = err_msg
    channel.updated_at = now
    db.commit()

    return ChannelTestResult(
        success=sr.success,
        status_code=sr.status_code,
        error_code=sr.error_code if not sr.success else None,
        error_message=err_msg,
        response_summary=response_summary,
        duration_ms=sr.duration_ms,
        delivery_id=delivery.id,
    )


# ============================================================================
# 策略 Policies
# ============================================================================


@router.get("/notifications/policies", response_model=list[PolicyRead])
def list_policies(db: Session = Depends(get_db)):
    """列出策略，按 id 倒序。"""
    policies = db.execute(
        select(NotificationPolicy).order_by(NotificationPolicy.id.desc())
    ).scalars().all()
    return [
        _serialize_policy(p, _get_policy_channel_ids(db, p.id)) for p in policies
    ]


@router.post("/notifications/policies", response_model=PolicyRead)
def create_policy(payload: PolicyCreate, db: Session = Depends(get_db)):
    """创建策略。

    渠道勾选受状态机约束：未配置/测试失败的第三方渠道不能被选中（WP-MSG.4）。
    """
    # 校验渠道可被选中
    _validate_channel_ids_selectable(db, payload.channel_ids)

    policy = NotificationPolicy(
        name=payload.name,
        enabled=payload.enabled,
        source_types_json=json.dumps(payload.source_types, ensure_ascii=False)
        if payload.source_types
        else None,
        min_severity=payload.min_severity,
        scope_type=payload.scope_type,
        scope_ids_json=json.dumps(payload.scope_ids, ensure_ascii=False)
        if payload.scope_ids
        else None,
        delivery_mode=payload.delivery_mode,
        digest_schedule=payload.digest_schedule,
        quiet_hours_json=json.dumps(payload.quiet_hours, ensure_ascii=False)
        if payload.quiet_hours
        else None,
        cooldown_minutes=payload.cooldown_minutes,
        dedup_window_minutes=payload.dedup_window_minutes,
        template_id=payload.template_id,
        created_at=_now_utc_naive(),
    )
    db.add(policy)
    db.commit()
    db.refresh(policy)

    _sync_policy_channels(db, policy.id, payload.channel_ids)
    db.commit()
    db.refresh(policy)
    return _serialize_policy(policy, _get_policy_channel_ids(db, policy.id))


@router.patch("/notifications/policies/{policy_id}", response_model=PolicyRead)
def update_policy(
    policy_id: int, payload: PolicyUpdate, db: Session = Depends(get_db)
):
    """更新策略。"""
    policy = db.get(NotificationPolicy, policy_id)
    if policy is None:
        _raise(
            "NOT_FOUND",
            status_code=404,
            user_message=f"策略不存在：id={policy_id}",
        )

    if payload.name is not None:
        policy.name = payload.name
    if payload.enabled is not None:
        policy.enabled = payload.enabled
    if payload.source_types is not None:
        policy.source_types_json = (
            json.dumps(payload.source_types, ensure_ascii=False)
            if payload.source_types
            else None
        )
    if payload.min_severity is not None:
        policy.min_severity = payload.min_severity
    if payload.scope_type is not None:
        policy.scope_type = payload.scope_type
    if payload.scope_ids is not None:
        policy.scope_ids_json = (
            json.dumps(payload.scope_ids, ensure_ascii=False)
            if payload.scope_ids
            else None
        )
    if payload.delivery_mode is not None:
        policy.delivery_mode = payload.delivery_mode
    if payload.digest_schedule is not None:
        policy.digest_schedule = payload.digest_schedule
    if payload.quiet_hours is not None:
        policy.quiet_hours_json = (
            json.dumps(payload.quiet_hours, ensure_ascii=False)
            if payload.quiet_hours
            else None
        )
    if payload.cooldown_minutes is not None:
        policy.cooldown_minutes = payload.cooldown_minutes
    if payload.dedup_window_minutes is not None:
        policy.dedup_window_minutes = payload.dedup_window_minutes
    if payload.template_id is not None:
        policy.template_id = payload.template_id

    if payload.channel_ids is not None:
        _validate_channel_ids_selectable(db, payload.channel_ids)
        _sync_policy_channels(db, policy.id, payload.channel_ids)

    policy.updated_at = _now_utc_naive()
    db.commit()
    db.refresh(policy)
    return _serialize_policy(policy, _get_policy_channel_ids(db, policy.id))


@router.delete("/notifications/policies/{policy_id}")
def delete_policy(policy_id: int, db: Session = Depends(get_db)):
    """删除策略（物理删除，级联删除关联记录）。"""
    policy = db.get(NotificationPolicy, policy_id)
    if policy is None:
        _raise(
            "NOT_FOUND",
            status_code=404,
            user_message=f"策略不存在：id={policy_id}",
        )
    db.delete(policy)
    db.commit()
    return {"ok": True, "id": policy_id}


def _validate_channel_ids_selectable(db: Session, channel_ids: list[int]) -> None:
    """校验所选渠道均可被策略选中（状态机约束）。"""
    if not channel_ids:
        return
    channels = db.execute(
        select(NotificationChannel).where(NotificationChannel.id.in_(channel_ids))
    ).scalars().all()
    found_ids = {ch.id for ch in channels}
    missing = set(channel_ids) - found_ids
    if missing:
        _raise(
            "NOT_FOUND",
            status_code=404,
            user_message=f"渠道不存在：{sorted(missing)}",
        )
    for ch in channels:
        if not is_selectable_for_policy(ch):
            hint = get_selection_hint(ch) or "渠道不可用"
            _raise(
                "PORTFOLIO_RULE_INVALID",
                status_code=400,
                user_message=f"渠道 {ch.name} 不可被策略选中：{hint}",
                impact="未配置或测试失败的第三方渠道不能被策略选中",
                extra_next_actions=[NextAction(
                    label="去配置渠道", action_type="redirect",
                    target="/notifications/channels",
                    reason="完成配置与测试后即可选中",
                )],
            )


def _sync_policy_channels(db: Session, policy_id: int, channel_ids: list[int]) -> None:
    """同步策略-渠道关联（全量替换）。"""
    db.execute(
        NotificationPolicyChannel.__table__.delete().where(
            NotificationPolicyChannel.policy_id == policy_id
        )
    )
    for cid in channel_ids:
        db.add(
            NotificationPolicyChannel(
                policy_id=policy_id, channel_id=cid, created_at=_now_utc_naive()
            )
        )


# ============================================================================
# 模板 Templates
# ============================================================================


@router.get("/notifications/templates", response_model=list[TemplateRead])
def list_templates(
    is_active: bool | None = Query(None, description="按激活状态筛选"),
    db: Session = Depends(get_db),
):
    """列出模板，按 id 倒序。"""
    stmt = select(NotificationTemplate)
    if is_active is not None:
        stmt = stmt.where(NotificationTemplate.is_active == is_active)
    stmt = stmt.order_by(NotificationTemplate.id.desc())
    templates = db.execute(stmt).scalars().all()
    return [_serialize_template(t) for t in templates]


@router.post("/notifications/templates", response_model=TemplateRead)
def create_template(payload: TemplateCreate, db: Session = Depends(get_db)):
    """创建模板。"""
    existing = db.execute(
        select(NotificationTemplate).where(NotificationTemplate.name == payload.name)
    ).scalars().first()
    if existing is not None:
        _raise(
            "DB_INTEGRITY_VIOLATION",
            status_code=409,
            user_message=f"模板名称已存在：{payload.name}",
        )

    template = NotificationTemplate(
        name=payload.name,
        title_template=payload.title_template,
        body_template=payload.body_template,
        body_text_template=payload.body_text_template,
        variables_json=payload.variables_json,
        version=1,
        is_active=payload.is_active,
        created_at=_now_utc_naive(),
    )
    db.add(template)
    db.commit()
    db.refresh(template)
    return _serialize_template(template)


@router.patch("/notifications/templates/{template_id}", response_model=TemplateRead)
def update_template(
    template_id: int, payload: TemplateUpdate, db: Session = Depends(get_db)
):
    """更新模板。"""
    template = db.get(NotificationTemplate, template_id)
    if template is None:
        _raise(
            "NOT_FOUND",
            status_code=404,
            user_message=f"模板不存在：id={template_id}",
        )

    if payload.name is not None and payload.name != template.name:
        dup = db.execute(
            select(NotificationTemplate).where(NotificationTemplate.name == payload.name)
        ).scalars().first()
        if dup is not None:
            _raise(
                "DB_INTEGRITY_VIOLATION",
                status_code=409,
                user_message=f"模板名称已存在：{payload.name}",
            )
        template.name = payload.name
    if payload.title_template is not None:
        template.title_template = payload.title_template
    if payload.body_template is not None:
        template.body_template = payload.body_template
    # Nullable fields need to distinguish omitted from an explicit null,
    # otherwise clients cannot clear an existing value.
    if "body_text_template" in payload.model_fields_set:
        template.body_text_template = payload.body_text_template
    if "variables_json" in payload.model_fields_set:
        template.variables_json = payload.variables_json
    if payload.is_active is not None:
        template.is_active = payload.is_active

    template.version += 1
    template.updated_at = _now_utc_naive()
    db.commit()
    db.refresh(template)
    return _serialize_template(template)


@router.delete("/notifications/templates/{template_id}")
def delete_template(template_id: int, db: Session = Depends(get_db)):
    """删除模板（物理删除）。"""
    template = db.get(NotificationTemplate, template_id)
    if template is None:
        _raise(
            "NOT_FOUND",
            status_code=404,
            user_message=f"模板不存在：id={template_id}",
        )
    db.delete(template)
    db.commit()
    return {"ok": True, "id": template_id}


@router.post(
    "/notifications/templates/{template_id}/preview",
    response_model=TemplatePreviewResponse,
)
def preview_template(
    template_id: int,
    payload: TemplatePreviewRequest,
    db: Session = Depends(get_db),
):
    """模板预览（变量替换，变量转义防止 Markdown/Webhook 注入）。"""
    template = db.get(NotificationTemplate, template_id)
    if template is None:
        _raise(
            "NOT_FOUND",
            status_code=404,
            user_message=f"模板不存在：id={template_id}",
        )

    # 变量通过 render_template 内部 escape_value 转义，防止注入
    rendered_title = render_template(template.title_template, payload.variables)
    rendered_body = render_template(template.body_template, payload.variables)
    rendered_body_text = (
        render_template(template.body_text_template, payload.variables)
        if template.body_text_template
        else None
    )
    return TemplatePreviewResponse(
        title=rendered_title,
        body=rendered_body,
        body_text=rendered_body_text,
    )


# ============================================================================
# 发送记录 Deliveries / Outbox
# ============================================================================


@router.get("/notifications/deliveries", response_model=list[DeliveryRead])
def list_deliveries(
    source_type: str | None = Query(None, description="按来源类型筛选（关联 outbox）"),
    channel_id: int | None = Query(None, description="按渠道筛选"),
    status: str | None = Query(None, description="按发送状态筛选"),
    date_from: str | None = Query(None, description="起始日期 YYYY-MM-DD"),
    date_to: str | None = Query(None, description="结束日期 YYYY-MM-DD"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """列出发送记录（分页 + source_type/channel_id/status/时间区间筛选）。

    source_type 来自关联的 outbox，需 join。
    """
    from datetime import datetime as _dt

    stmt = select(NotificationDelivery)
    if source_type is not None:
        stmt = stmt.join(
            NotificationOutbox,
            NotificationOutbox.id == NotificationDelivery.outbox_id,
        ).where(NotificationOutbox.source_type == source_type)
    if channel_id is not None:
        stmt = stmt.where(NotificationDelivery.channel_id == channel_id)
    if status is not None:
        stmt = stmt.where(NotificationDelivery.status == status)
    if date_from is not None:
        try:
            start = _dt.fromisoformat(date_from)
            stmt = stmt.where(NotificationDelivery.created_at >= start)
        except ValueError:
            _raise(
                "VALIDATION_ERROR",
                status_code=400,
                user_message=f"date_from 格式无效：{date_from}（应为 YYYY-MM-DD）",
            )
    if date_to is not None:
        try:
            from datetime import timedelta

            end = _dt.fromisoformat(date_to) + timedelta(days=1)
            stmt = stmt.where(NotificationDelivery.created_at < end)
        except ValueError:
            _raise(
                "VALIDATION_ERROR",
                status_code=400,
                user_message=f"date_to 格式无效：{date_to}（应为 YYYY-MM-DD）",
            )
    stmt = stmt.order_by(NotificationDelivery.id.desc()).limit(limit).offset(offset)
    deliveries = db.execute(stmt).scalars().all()
    return [_serialize_delivery(d) for d in deliveries]


@router.get("/notifications/outbox", response_model=list[OutboxRead])
def list_outbox(
    status: str | None = Query(None, description="按状态筛选"),
    channel_id: int | None = Query(None, description="按渠道筛选"),
    source_type: str | None = Query(None, description="按来源类型筛选"),
    event_type: str | None = Query(None, description="按事件类型筛选"),
    date_from: str | None = Query(None, description="起始日期 YYYY-MM-DD"),
    date_to: str | None = Query(None, description="结束日期 YYYY-MM-DD"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """列出发件箱记录（分页 + 筛选），按 id 倒序。"""
    from datetime import datetime as _dt

    stmt = select(NotificationOutbox)
    if status is not None:
        stmt = stmt.where(NotificationOutbox.status == status)
    if channel_id is not None:
        stmt = stmt.where(NotificationOutbox.channel_id == channel_id)
    if source_type is not None:
        stmt = stmt.where(NotificationOutbox.source_type == source_type)
    if event_type is not None:
        stmt = stmt.where(NotificationOutbox.event_type == event_type)
    if date_from is not None:
        try:
            stmt = stmt.where(NotificationOutbox.created_at >= _dt.fromisoformat(date_from))
        except ValueError:
            _raise(
                "VALIDATION_ERROR",
                status_code=400,
                user_message=f"date_from 格式无效：{date_from}",
            )
    if date_to is not None:
        try:
            from datetime import timedelta

            end = _dt.fromisoformat(date_to) + timedelta(days=1)
            stmt = stmt.where(NotificationOutbox.created_at < end)
        except ValueError:
            _raise(
                "VALIDATION_ERROR",
                status_code=400,
                user_message=f"date_to 格式无效：{date_to}",
            )
    stmt = stmt.order_by(NotificationOutbox.id.desc()).limit(limit).offset(offset)
    outboxes = db.execute(stmt).scalars().all()
    return [_serialize_outbox(o) for o in outboxes]


@router.get(
    "/notifications/outbox/{outbox_id}/deliveries",
    response_model=list[DeliveryRead],
)
def list_outbox_deliveries(outbox_id: int, db: Session = Depends(get_db)):
    """列出某发件箱记录的所有发送尝试。"""
    outbox = db.get(NotificationOutbox, outbox_id)
    if outbox is None:
        _raise(
            "NOT_FOUND",
            status_code=404,
            user_message=f"发件箱记录不存在：id={outbox_id}",
        )
    deliveries = db.execute(
        select(NotificationDelivery)
        .where(NotificationDelivery.outbox_id == outbox_id)
        .order_by(NotificationDelivery.attempt_number.asc())
    ).scalars().all()
    return [_serialize_delivery(d) for d in deliveries]


@router.post("/notifications/outbox/{outbox_id}/retry")
def retry_outbox(outbox_id: int, db: Session = Depends(get_db)):
    """手动重发失败/死信记录（重置为 pending，等待 dispatcher 处理）。"""
    from app.services.notifications.outbox import manual_retry

    outbox = db.get(NotificationOutbox, outbox_id)
    if outbox is None:
        _raise(
            "NOT_FOUND",
            status_code=404,
            user_message=f"发件箱记录不存在：id={outbox_id}",
        )
    if outbox.status not in ("failed", "dead_letter"):
        _raise(
            "VALIDATION_ERROR",
            status_code=400,
            user_message=f"当前状态 {outbox.status} 不可重发，仅 failed/dead_letter 可重发",
        )
    updated = manual_retry(db, outbox_id=outbox_id)
    if updated is None:
        _raise(
            "UNKNOWN_ERROR",
            status_code=500,
            user_message="重发失败，请稍后重试",
        )
    return {"ok": True, "id": outbox_id, "status": updated.status}


# ============================================================================
# Real inbox API for the top navigation notification dropdown.
# The data source is the same persisted Outbox populated by Settings policies;
# only in_app channel rows are exposed, and an empty database returns [].
# ============================================================================

_SEVERITY_TO_TYPE: dict[str, str] = {
    "error": "error",
    "critical": "error",
    "alert": "error",
    "warning": "warning",
    "warn": "warning",
    "success": "success",
    "info": "info",
    "notice": "info",
}

_EVENT_TYPE_TO_TITLE_PREFIX: dict[str, str] = {
    "portfolio_rebalance": "调仓执行",
    "portfolio_backtest": "回测完成",
    "risk_alert": "风险预警",
    "auto_trade": "自动交易",
    "system": "系统通知",
    "strategy_update": "策略更新",
    "channel_test": "渠道测试",
}


def _parse_payload_title_body(outbox: NotificationOutbox) -> tuple[str, str]:
    try:
        payload = json.loads(outbox.payload_json) if outbox.payload_json else {}
    except Exception:
        payload = {}
    title = ""
    body = ""
    if isinstance(payload, dict):
        title = str(payload.get("title") or payload.get("subject") or "")
        body = str(payload.get("body") or payload.get("content") or payload.get("message") or "")
    if not title:
        prefix = _EVENT_TYPE_TO_TITLE_PREFIX.get(outbox.event_type or "", outbox.event_type or "通知")
        title = f"{prefix}：{outbox.event_key or '#' + str(outbox.id)}"
    if not body:
        body = f"event_key={outbox.event_key or outbox.id}"
    return title, body


def _relative_time(created_at: datetime | None) -> str:
    if created_at is None:
        return "刚刚"
    try:
        now = _now_utc_naive()
        delta = now - created_at
        secs = max(int(delta.total_seconds()), 0)
    except Exception:
        return "刚刚"
    if secs < 60:
        return f"{secs} 秒前"
    mins = secs // 60
    if mins < 60:
        return f"{mins} 分钟前"
    hours = mins // 60
    if hours < 24:
        return f"{hours} 小时前"
    days = hours // 24
    if days < 7:
        return f"{days} 天前"
    return created_at.strftime("%Y-%m-%d %H:%M")


def _outbox_to_inbox_item(outbox: NotificationOutbox) -> dict[str, Any]:
    title, description = _parse_payload_title_body(outbox)
    ntype = _SEVERITY_TO_TYPE.get((outbox.severity or "info").lower(), "info")
    return {
        "id": outbox.id,
        "type": ntype,
        "title": title,
        "description": description,
        "time": _relative_time(outbox.created_at),
        "created_at": outbox.created_at.isoformat() if outbox.created_at else None,
        "read": outbox.read_at is not None,
        "status": outbox.status,
        "source_type": outbox.source_type,
        "event_type": outbox.event_type,
        "channel_id": outbox.channel_id,
    }


@router.get("/notifications/inbox")
def list_inbox(
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """Return persisted Outbox messages routed through the in-app channel."""
    rows = db.execute(
        select(NotificationOutbox)
        .join(
            NotificationChannel,
            NotificationChannel.id == NotificationOutbox.channel_id,
        )
        .where(NotificationChannel.channel_type == CHANNEL_TYPE_IN_APP)
        .order_by(NotificationOutbox.id.desc())
        .limit(limit)
    ).scalars().all()
    return [_outbox_to_inbox_item(row) for row in rows]


@router.put("/notifications/inbox/{item_id:int}/read")
def mark_inbox_item_read(item_id: int, db: Session = Depends(get_db)):
    """Persist the read receipt for one real in-app outbox item."""
    outbox = db.execute(
        select(NotificationOutbox)
        .join(
            NotificationChannel,
            NotificationChannel.id == NotificationOutbox.channel_id,
        )
        .where(
            NotificationOutbox.id == item_id,
            NotificationChannel.channel_type == CHANNEL_TYPE_IN_APP,
        )
    ).scalars().first()
    if outbox is None:
        _raise(
            "NOT_FOUND",
            status_code=404,
            user_message=f"站内通知不存在：id={item_id}",
        )
    outbox.read_at = outbox.read_at or _now_utc_naive()
    db.commit()
    return {"ok": True, "id": item_id, "read": True}


@router.put("/notifications/inbox/read-all")
def mark_inbox_all_read(
    limit: int = Query(500, ge=1, le=5000),
    db: Session = Depends(get_db),
):
    """Persist read receipts for the latest unread in-app messages."""
    rows = db.execute(
        select(NotificationOutbox)
        .join(
            NotificationChannel,
            NotificationChannel.id == NotificationOutbox.channel_id,
        )
        .where(
            NotificationChannel.channel_type == CHANNEL_TYPE_IN_APP,
            NotificationOutbox.read_at.is_(None),
        )
        .order_by(NotificationOutbox.id.desc())
        .limit(limit)
    ).scalars().all()
    now = _now_utc_naive()
    for row in rows:
        row.read_at = now
    db.commit()
    return {"ok": True, "count": len(rows)}


@router.post("/notifications/inbox/view-all")
def inbox_view_all(payload: dict[str, Any] | None = None, db: Session = Depends(get_db)):
    """Return the real in-app total before the UI opens delivery history."""
    total = db.execute(
        select(func.count())
        .select_from(NotificationOutbox)
        .join(
            NotificationChannel,
            NotificationChannel.id == NotificationOutbox.channel_id,
        )
        .where(NotificationChannel.channel_type == CHANNEL_TYPE_IN_APP)
    ).scalar() or 0
    return {
        "ok": True,
        "total": total,
        "hint": "已打开设置中的消息投递记录。",
    }


__all__ = ["router"]
