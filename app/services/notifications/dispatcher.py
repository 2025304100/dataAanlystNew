"""异步 dispatcher（WP-MSG.3）。

后台轮询 Outbox，调用适配器发送。
一渠道失败不影响其他渠道。
dispatcher 重启后继续处理 pending Outbox；不自动抢回 sending 记录。

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
    hold_for_review,
)
from app.services.notifications.registry import get_adapter
from app.services.notifications.delivery_state import freeze_for_transport
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
        """执行一次轮询，返回实际领取并处理的记录数。

        供 APScheduler 调用，或测试中手动调用。
        """
        db: Session = SessionLocal()
        try:
            pending = get_pending_outbox(db, limit=self.batch_size)
            count = 0
            for outbox in pending:
                try:
                    if self._process_one(db, outbox):
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

    def _process_one(self, db: Session, outbox: NotificationOutbox) -> bool:
        """处理单条 Outbox 记录。"""
        # 1. 标记为 sending
        marked = mark_sending(db, outbox_id=outbox.id)
        if marked is None:
            return False
        outbox = marked
        claimed_id, generation, source_type = outbox.id, outbox.attempt_count, outbox.source_type

        def fail(**values):
            return mark_failed(db, outbox_id=claimed_id, expected_attempt_count=generation, **values)

        if outbox.source_type == "market_signal":
            from app.services.market_signal_delivery import prepare_notification, suppress_outbox
            from app.services.market_event_collection_lock import MarketEventCollectionBusy
            try:
                allowed = prepare_notification(db, outbox)
            except MarketEventCollectionBusy:
                fail(error_code="rate_limited",
                    error_message="行情提醒正在检查额度，请稍后重试")
                return True
            except Exception:
                db.rollback()
                logger.exception("行情提醒发送前检查失败，尚未调用运输适配器")
                fail(error_code="rate_limited",
                    error_message="发送前校验暂不可用，尚未发送，将重试")
                return True
            if not allowed:
                suppress_outbox(db, outbox, expected_attempt_count=generation)
                return True

        # 2. 获取渠道
        channel = db.get(NotificationChannel, outbox.channel_id)
        if channel is None:
            fail(
                error_code="channel_not_found",
                error_message=f"渠道 ID={outbox.channel_id} 不存在",
            )
            return True

        # 3. 检查渠道是否启用
        if not channel.enabled or channel.status in ("disabled", "unconfigured"):
            fail(
                error_code="channel_disabled",
                error_message=f"渠道 {channel.name} 未启用",
            )
            return True

        # 4. 获取适配器
        adapter = get_adapter(channel.channel_type)
        if adapter is None:
            fail(
                error_code="adapter_not_found",
                error_message=f"无 {channel.channel_type} 适配器",
            )
            return True

        # 5. 解密配置（容错：解密失败用空配置）
        try:
            config = (
                json.loads(decrypt_config(channel.config_encrypted_json))
                if channel.config_encrypted_json
                else {}
            )
        except Exception:
            config = {}

        # Persist exact transport content before the first possible side effect.
        payload = freeze_for_transport(db, outbox_id=claimed_id,
            expected_attempt_count=generation, channel_type=channel.channel_type)
        if payload is None:
            return True

        title = payload.get("title", "")
        body = payload.get("body", "")
        body_text = payload.get("body_text")

        # Only transport exceptions may enter transport retry. A successful
        # response followed by any storage error must remain owned for review.
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

        except Exception as exc:
            duration_ms = int((time.time() - start_time) * 1000)
            error_code, error_message = adapter.normalize_error(exc)
            fail(error_code=error_code, error_message=error_message, duration_ms=duration_ms, transport_started=True)
            logger.exception(f"Outbox id={claimed_id} 适配器异常")
            return True

        if result.success:
            try:
                delivery = NotificationDelivery(
                    outbox_id=claimed_id,
                    channel_id=outbox.channel_id,
                    attempt_number=generation + 1,
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
                completed = mark_sent(db, outbox_id=claimed_id, delivery=delivery)
            except Exception:
                db.rollback()
                logger.exception("通知已返回成功，回执记账异常；禁止自动重发")
                try:
                    hold_for_review(db, outbox_id=claimed_id, expected_attempt_count=generation,
                        error_code="delivery_receipt_review_required",
                        error_message="发送返回成功，回执记账待核对，禁止自动重发")
                except Exception:
                    db.rollback()
                    logger.exception("回执核对标记暂不可写；保留sending，不自动重发")
                return True
            if completed:
                if source_type == "market_signal":
                    # Notification success is final even if its analytics
                    # callback fails. Never resend after an analytics failure.
                    try:
                        from app.services.market_signal_delivery import record_delivery
                        record_delivery(db, outbox)
                    except Exception:
                        db.rollback()
                        logger.exception("行情提醒已发送，结果记录将在下轮对账恢复")
                logger.debug(f"Outbox id={claimed_id} 发送成功")
        else:
                # 9. 失败：判断错误类型（鉴权失败 / 临时错误）
                is_auth = _is_auth_error(result.error_code, result.status_code)
                fail(
                    transport_started=True,
                    error_code=result.error_code,
                    error_message=result.error_message,
                    status_code=result.status_code,
                    duration_ms=duration_ms,
                    response_summary=result.response_summary,
                    is_auth_fail=is_auth,
                )
                logger.warning(
                    f"Outbox id={claimed_id} 发送失败："
                    f"{sanitize_message(result.error_message or '')}"
                )
        return True


# 单例
_dispatcher: Dispatcher | None = None


def get_dispatcher() -> Dispatcher:
    """获取 dispatcher 单例。"""
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = Dispatcher()
    return _dispatcher


__all__ = ["Dispatcher", "get_dispatcher"]
