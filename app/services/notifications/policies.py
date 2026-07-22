"""推送策略匹配服务（WP-MSG.4 + WP-MSG.5）。

根据业务事件匹配策略，写入 Outbox。

project_memory 硬约束：
- 只有"测试成功+已启用"的第三方渠道才能在策略中勾选（is_selectable_for_policy）
- 业务事务只写 Outbox，不直接等待第三方
- event_key + channel_id 唯一防重（由 enqueue 保证）
- 错误消息不暴露敏感信息
- WP-MSG.5：防打扰（去重/冷却/免打扰）+ 内容脱敏
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.notification import (
    NotificationChannel,
    NotificationPolicy,
    NotificationPolicyChannel,
    NotificationTemplate,
)
from app.services.notifications.anti_disturb import should_send_now
from app.services.notifications.channel_state import is_selectable_for_policy
from app.services.notifications.content_sanitizer import (
    build_summary,
    sanitize_payload,
)
from app.services.notifications.outbox import enqueue
from app.services.notifications.template_renderer import render_template


logger = logging.getLogger(__name__)


# 严重级别排序：info < warn < error < critical
SEVERITY_ORDER: dict[str, int] = {
    "info": 0,
    "warn": 1,
    "error": 2,
    "critical": 3,
}


def match_policies(
    db: Session,
    *,
    source_type: str,
    event_type: str,
    severity: str,
    scope_type: str | None = None,
    scope_id: int | None = None,
) -> list[tuple[NotificationPolicy, list[NotificationChannel]]]:
    """匹配策略。

    返回 [(policy, [channel1, channel2, ...]), ...]

    匹配条件：
    - policy.enabled = True
    - source_type 在 policy.source_types_json 中
    - severity >= policy.min_severity
    - scope 匹配（policy.scope_type='all' 或匹配 scope_id）
    - 渠道可选（is_selectable_for_policy）
    """
    # 拉取所有启用的策略
    policies = db.execute(
        select(NotificationPolicy).where(NotificationPolicy.enabled == True)  # noqa: E712
    ).scalars().all()

    matched: list[tuple[NotificationPolicy, list[NotificationChannel]]] = []

    for policy in policies:
        # 1. 检查 source_type
        source_types = (
            json.loads(policy.source_types_json) if policy.source_types_json else []
        )
        if source_type not in source_types:
            continue

        # 2. 检查 severity
        if SEVERITY_ORDER.get(severity, 0) < SEVERITY_ORDER.get(policy.min_severity, 0):
            continue

        # 3. 检查 scope
        if policy.scope_type != "all":
            if scope_type != policy.scope_type:
                continue
            scope_ids = (
                json.loads(policy.scope_ids_json) if policy.scope_ids_json else []
            )
            if scope_id is not None and scope_id not in scope_ids:
                continue

        # 4. 获取关联渠道
        policy_channels = db.execute(
            select(NotificationChannel)
            .join(
                NotificationPolicyChannel,
                NotificationPolicyChannel.channel_id == NotificationChannel.id,
            )
            .where(NotificationPolicyChannel.policy_id == policy.id)
        ).scalars().all()

        # 5. 过滤可选渠道
        selectable_channels = [
            ch for ch in policy_channels if is_selectable_for_policy(ch)
        ]

        if selectable_channels:
            matched.append((policy, selectable_channels))

    return matched


def emit_event(
    db: Session,
    *,
    source_type: str,
    source_id: int | None,
    event_type: str,
    severity: str,
    title: str,
    body: str,
    body_text: str | None = None,
    scope_type: str | None = None,
    scope_id: int | None = None,
    template_variables: dict | None = None,
) -> list:
    """触发业务事件，匹配策略并写入 Outbox。

    返回创建的 Outbox 记录列表。

    防重：同一 source_type:event_type:source_id + channel_id 在 Outbox 中
    已存在未发送记录时，跳过（由 enqueue 保证）。

    WP-MSG.5：
    - 防打扰检查（去重窗口/冷却/免打扰）由 should_send_now 保证
    - error/critical 默认即时发送（不受去重/冷却限制），event_key 含时间戳确保唯一
    - 内容脱敏：站内消息保留完整内容，第三方渠道截断摘要
    """
    # 1. 匹配策略
    matched = match_policies(
        db,
        source_type=source_type,
        event_type=event_type,
        severity=severity,
        scope_type=scope_type,
        scope_id=scope_id,
    )

    # 2. 生成 event_key（防重键）
    # 格式：source_type:event_type:source_id（source_id 为空时用时间戳保证不防重）
    # error/critical 默认即时发送，event_key 含时间戳确保不被 enqueue 防重
    if severity in ("error", "critical"):
        event_key_base = (
            f"{source_type}:{event_type}:{datetime.now(timezone.utc).isoformat()}"
        )
    elif source_id is not None:
        event_key_base = f"{source_type}:{event_type}:{source_id}"
    else:
        event_key_base = (
            f"{source_type}:{event_type}:{datetime.now(timezone.utc).isoformat()}"
        )

    created = []
    for policy, channels in matched:
        # 3. 渲染模板（如策略指定了模板）
        rendered_title = title
        rendered_body = body
        rendered_body_text = body_text

        if policy.template_id:
            template = db.get(NotificationTemplate, policy.template_id)
            if template and template.is_active:
                vars_ = template_variables or {}
                rendered_title = render_template(template.title_template, vars_)
                rendered_body = render_template(template.body_template, vars_)
                if template.body_text_template:
                    rendered_body_text = render_template(
                        template.body_text_template, vars_
                    )

        # 4. 写入每个渠道的 Outbox（含防打扰检查 + 内容脱敏）
        for channel in channels:
            # 4.1 防打扰检查
            should_send, reason = should_send_now(
                db,
                policy=policy,
                source_type=source_type,
                source_id=source_id,
                event_type=event_type,
                channel_id=channel.id,
                severity=severity,
            )
            if not should_send:
                logger.debug(
                    f"防打扰跳过: event_key={event_key_base}, "
                    f"channel_id={channel.id}, reason={reason}"
                )
                continue

            # 4.2 内容脱敏 + 渠道适配
            # 站内消息保留完整内容（脱敏敏感字段），第三方渠道截断摘要
            if channel.channel_type == "in_app":
                channel_payload = sanitize_payload({
                    "title": rendered_title,
                    "body": rendered_body,
                    "body_text": rendered_body_text,
                })
            else:
                # 第三方渠道：截断内容到摘要长度
                summary = build_summary(
                    rendered_title,
                    rendered_body,
                    max_length=500,
                )
                channel_payload = sanitize_payload({
                    "title": rendered_title,
                    "body": summary,
                    "body_text": rendered_body_text,
                })

            # 4.3 写入 Outbox
            outbox = enqueue(
                db,
                event_key=event_key_base,
                source_type=source_type,
                source_id=source_id,
                event_type=event_type,
                severity=severity,
                payload=channel_payload,
                channel_id=channel.id,
                policy_id=policy.id,
            )
            if outbox is not None:
                created.append(outbox)

    return created


__all__ = ["match_policies", "emit_event", "SEVERITY_ORDER"]
