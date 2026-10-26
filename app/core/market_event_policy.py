"""Shared retention contract for API, scheduler and direct service calls."""

MARKET_EVENT_MIN_KEEP_DAYS = 90
MARKET_EVENT_MAX_KEEP_DAYS = 3650
MARKET_EVENT_HISTORY_DAYS = 365
MARKET_EVENT_KEEP_DAYS_DEFAULT = MARKET_EVENT_HISTORY_DAYS


def effective_keep_days(keep_days: int) -> int:
    if type(keep_days) is not int or not MARKET_EVENT_MIN_KEEP_DAYS <= keep_days <= MARKET_EVENT_MAX_KEEP_DAYS:
        raise ValueError("keep_days must be an integer between 90 and 3650")
    # Legacy schedules requesting 90 days remain runnable without destroying
    # the history needed by the longest supported hotspot window.
    return max(keep_days, MARKET_EVENT_HISTORY_DAYS)
