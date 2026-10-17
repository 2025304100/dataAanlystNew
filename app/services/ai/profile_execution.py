"""Shared per-profile admission for all in-process AI HTTP requests."""
from __future__ import annotations

import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from sqlalchemy import case, or_, update
from sqlalchemy.exc import SQLAlchemyError

from app.models.ai_profile import AIProfile
from app.services.ai.profile_capabilities import profile_protocol_error

_guard = threading.Lock()
_active: dict[tuple[str, int], int] = {}


class ProfileExecutionRejected(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def has_active_profile_requests() -> bool:
    with _guard:
        return bool(_active)


@contextmanager
def profile_request_slot(db, profile, *, count_daily=True):
    protocol_error = profile_protocol_error(profile.provider, profile.base_url)
    if protocol_error:
        raise ProfileExecutionRejected("unsupported_protocol", protocol_error)
    key = (db.get_bind().url.render_as_string(hide_password=True), profile.id)
    with _guard:
        if _active.get(key, 0) >= profile.max_concurrent:
            raise ProfileExecutionRejected("concurrency", "该 AI 配置已达到最大并发，请等待当前请求完成后重试")
        _active[key] = _active.get(key, 0) + 1
    try:
        if count_daily:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            new_window = or_(AIProfile.daily_request_reset_at.is_(None), AIProfile.daily_request_reset_at <= now - timedelta(days=1))
            result = db.execute(update(AIProfile).where(
                AIProfile.id == profile.id,
                AIProfile.daily_request_limit > 0,
                or_(new_window, AIProfile.daily_request_count < AIProfile.daily_request_limit),
            ).values(
                daily_request_count=case((new_window, 1), else_=AIProfile.daily_request_count + 1),
                daily_request_reset_at=case((new_window, now), else_=AIProfile.daily_request_reset_at),
            ).execution_options(synchronize_session=False))
            if result.rowcount != 1:
                db.rollback()
                raise ProfileExecutionRejected("quota", "该 AI 配置的当日请求额度已用完")
            db.commit()
            db.refresh(profile)
        yield
    except SQLAlchemyError:
        db.rollback()
        raise ProfileExecutionRejected("storage", "AI 额度检查暂不可用，请稍后重试") from None
    finally:
        with _guard:
            remaining = _active.get(key, 1) - 1
            if remaining:
                _active[key] = remaining
            else:
                _active.pop(key, None)
