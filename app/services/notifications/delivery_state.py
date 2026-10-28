"""Durable transport identity in the existing Outbox payload; no schema change."""
import hashlib
import json

from sqlalchemy import update

from app.models.notification import NotificationOutbox

STATE_KEY = "_notification_delivery_state"


def state_of(box):
    try:
        value = json.loads(box.payload_json or "{}").get(STATE_KEY, {})
        return value if isinstance(value, dict) and value.get("v") == 1 else {}
    except (ValueError, AttributeError):
        return {}


def may_have_been_sent(box):
    state = state_of(box)
    return bool(state.get("frozen")) or (box.attempt_count > 0 and not state)


def _hash(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()


def freeze_for_transport(db, *, outbox_id, expected_attempt_count, channel_type):
    """Commit immutable body/key before I/O; uncertain legacy records stay owned."""
    from app.services.notifications.outbox import hold_for_review
    box = db.get(NotificationOutbox, outbox_id, populate_existing=True)
    if box is None or box.status != "sending" or box.attempt_count != expected_attempt_count:
        return None
    raw = box.payload_json
    try:
        try:
            payload = json.loads(raw or "{}")
        except ValueError:
            if box.attempt_count:
                raise
            payload = {}  # Preserve the established fresh-message fallback.
        if not isinstance(payload, dict):
            if box.attempt_count:
                raise ValueError("Notification payload must be an object")
            payload = {}
        state = state_of(box)
        payload.pop(STATE_KEY, None)
        if state.get("frozen"):
            if state.get("hash") != _hash(payload):
                raise ValueError("Frozen notification content changed")
            return payload
        if box.attempt_count and not state:
            raise ValueError("Legacy attempted notification requires receipt review")
        if channel_type == "webhook":
            identity = [db.get_bind().engine.url.database, box.id, box.channel_id,
                        box.event_key, box.created_at.isoformat()]
            digest = hashlib.sha256(json.dumps(identity, ensure_ascii=True,
                separators=(",", ":")).encode("utf-8")).hexdigest()
            payload["_notification_idempotency_key"] = f"notify-v1-{digest}"
        stored = {**payload, STATE_KEY: {"v": 1, "frozen": True, "hash": _hash(payload)}}
        result = db.execute(update(NotificationOutbox).where(
            NotificationOutbox.id == box.id, NotificationOutbox.status == "sending",
            NotificationOutbox.attempt_count == expected_attempt_count,
            NotificationOutbox.payload_json == raw if raw is not None else NotificationOutbox.payload_json.is_(None),
        ).values(payload_json=json.dumps(stored, ensure_ascii=False, separators=(",", ":")))
            .execution_options(synchronize_session=False))
        db.commit()
        return payload if result.rowcount == 1 else None
    except (ValueError, TypeError):
        db.rollback()
        hold_for_review(db, outbox_id=outbox_id, expected_attempt_count=expected_attempt_count,
            error_code="delivery_content_review_required",
            error_message="历史发送结果或冻结内容待核对，禁止自动重发")
        return None
