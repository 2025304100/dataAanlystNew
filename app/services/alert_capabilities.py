"""Executable alert capabilities, shared by HTTP and AI draft validation."""

SUPPORTED_ALERT_TYPES = frozenset({"score_drop", "data_stale", "task_failed"})
ALERT_EVALUATION_MODE = "manual"
UNSUPPORTED_ALERT_REASON = "该告警类型尚未接入执行，暂不可启用；请选择评分下降、数据过期或任务失败。"
