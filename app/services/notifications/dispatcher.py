"""异步 dispatcher（WP-MSG.3）。

后台轮询 Outbox，调用适配器发送。
一渠道失败不影响其他渠道。
dispatcher 重启后继续处理未完成 Outbox。

project_memory 硬约束：
- 一渠道失败不影响其他渠道（单条 try/except）
- dispatcher 重启后继续处理未完成 Outbox（状态在 DB 中持久化）
- 鉴权失败暂停渠道 + 站内告警
- 指数退避重试
- dead-letter
- 错误消息不暴露敏感信息（sanitize_message）
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models.notification import (
    NotificationChannel,
    NotificationDelivery,
    NotificationOutbox,
)
from app.schemas.error_sanitizer import sanitize_message
from app.services.notifications.outbox import (
    _create_in_app_system_alert,
    _is_auth_error,
    _is_rate_limited,
    get_pending_outbox,
    mark_failed,
    mark_sending,
    mark_sent,
)
from app.services.notifications.registry import get_adapter
from app.utils.secret_mask import decrypt_config


logger = logging.getLogger(__name__)


def _now_utc_naive() -> datetime:
    """返回无时区信息的 UTC 当前时间。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Dispatcher:
    """Outbox 异步 dispatcher。

    周期性轮询 Outbox，调用适配器发送。支持：
    - 重启恢复：启动时处理所有 pending 记录（状态在 DB 持久化）
    - 故障隔离：一渠道失败不影响其他渠道（单条 try/except + rollback）
    - 鉴权失败暂停渠道 + 站内告警
    - 指数退避重试
    - dead-letter
    """

    def __init__(self, poll_interval: float = 5.0, batch_size: int = 50):
        self.poll_interval = poll_interval
        self.batch_size = batch_size
        self._running = False
        self._stop_requested = False

    def start(self) -> None:
        """启动 dispatcher（前台运行，测试用）。

        生产环境应通过 APScheduler 或后台线程调用 run_once。
        """
        self._running = True
        self._stop_requested = False
        logger.info("Notification dispatcher 已启动")

        while not self._stop_requested:
            try:
                self.run_once()
            except Exception as e:
                logger.exception(f"Dispatcher 轮询异常：{sanitize_message(str(e))}")
            time.sleep(self.poll_interval)

        self._running = False
        logger.info("Notification dispatcher 已停止")

    def stop(self) -> None:
        """请求停止 dispatcher。"""
        self._stop_requested = True

    def run_once(self) -> int:
        """执行一次轮询，返回处理的记录数。

        供 APScheduler 调用，或测试中手动调用。
        """
        db: Session = SessionLocal()
        try:
            pending = get_pending_outbox(db, limit=self.batch_size)
            count = 0
            for outbox in pending:
                try:
                    self._process_one(db, outbox)
                    count += 1
                except Exception as e:
                    # 单条失败不影响其他渠道
                    logger.exception(
                        f"处理 Outbox id={outbox.id} 异常：{sanitize_message(str(e))}"
                    )
                    try:
                        db.rollback()
                    except Exception:
                        pass
            return count
        finally:
            db.close()

    def _process_one(self, db: Session, outbox: NotificationOutbox) -> None:
        """处理单条 Outbox 记录。"""
        # 1. 标记为 sending
        marked = mark_sending(db, outbox_id=outbox.id)
        if marked is None:
            return

        # 2. 获取渠道
        channel = db.get(NotificationChannel, outbox.channel_id)
        if channel is None:
            mark_failed(
                db,
                outbox_id=outbox.id,
                error_code="channel_not_found",
                error_message=f"渠道 ID={outbox.channel_id} 不存在",
            )
            return

        # 3. 检查渠道是否启用
        if not channel.enabled or channel.status in ("disabled", "unconfigured"):
            mark_failed(
                db,
                outbox_id=outbox.id,
                error_code="channel_disabled",
                error_message=f"渠道 {channel.name} 未启用",
            )
            return

        # 4. 获取适配器
        adapter = get_adapter(channel.channel_type)
        if adapter is None:
            mark_failed(
                db,
                outbox_id=outbox.id,
                error_code="adapter_not_found",
                error_message=f"无 {channel.channel_type} 适配器",
            )
            return

        # 5. 解密配置（容错：解密失败用空配置）
        try:
            config = (
                json.loads(decrypt_config(channel.config_encrypted_json))
                if channel.config_encrypted_json
                else {}
            )
        except Exception:
            config = {}

        # 6. 解析 payload（容错：解析失败用空字典）
        try:
            payload = (
                json.loads(outbox.payload_json) if outbox.payload_json else {}
            )
        except Exception:
            payload = {}

        title = payload.get("title", "")
        body = payload.get("body", "")
        body_text = payload.get("body_text")

        # 7. 发送
        start_time = time.time()
        try:
            result = adapter.send_message(
                config=config,
                title=title,
                body=body,
                body_text=body_text,
                payload=payload,
            )
            duration_ms = int((time.time() - start_time) * 1000)

            if result.success:
                # 8. 成功：记录 delivery + 标记 sent
                delivery = NotificationDelivery(
                    outbox_id=outbox.id,
                    channel_id=outbox.channel_id,
                    attempt_number=outbox.attempt_count + 1,
                    status="success",
                    status_code=result.status_code,
                    response_summary=(
                        sanitize_message(result.response_summary)[:500]
                        if result.response_summary
                        else None
                    ),
                    error_code=None,
                    error_message=None,
                    duration_ms=duration_ms,
                    sent_at=_now_utc_naive(),
                    created_at=_now_utc_naive(),
                )
                mark_sent(db, outbox_id=outbox.id, delivery=delivery)
                logger.debug(f"Outbox id={outbox.id} 发送成功")
            else:
                # 9. 失败：判断错误类型（鉴权失败 / 临时错误）
                is_auth = _is_auth_error(result.error_code, result.status_code)
                mark_failed(
                    db,
                    outbox_id=outbox.id,
                    error_code=result.error_code,
                    error_message=result.error_message,
                    status_code=result.status_code,
                    duration_ms=duration_ms,
                    response_summary=result.response_summary,
                    is_auth_fail=is_auth,
                )
                logger.warning(
                    f"Outbox id={outbox.id} 发送失败："
                    f"{sanitize_message(result.error_message or '')}"
                )
        except Exception as exc:
            # 适配器异常：归一化错误并标记失败
            duration_ms = int((time.time() - start_time) * 1000)
            error_code, error_message = adapter.normalize_error(exc)
            mark_failed(
                db,
                outbox_id=outbox.id,
                error_code=error_code,
                error_message=error_message,
                duration_ms=duration_ms,
            )
            logger.exception(f"Outbox id={outbox.id} 适配器异常")


# 单例
_dispatcher: Dispatcher | None = None


def get_dispatcher() -> Dispatcher:
    """获取 dispatcher 单例。"""
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = Dispatcher()
    return _dispatcher


__all__ = ["Dispatcher", "get_dispatcher"]
