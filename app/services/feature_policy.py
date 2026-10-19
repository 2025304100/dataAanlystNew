"""Retired features remain identifiable for historical records only."""

TAIL_PROXY_DISABLED_REASON = "尾盘代理已停用，不再采集、导入或参与因子计算。"
DISABLED_FACTOR_CODES = frozenset({"tail_accumulation_proxy"})
DISABLED_FACTOR_FIELDS = frozenset({"proxy_score"})


def reject_tail_proxy() -> None:
    raise ValueError(TAIL_PROXY_DISABLED_REASON)


def is_tail_proxy_schedule(task_type: str, payload: dict) -> bool:
    return task_type == "tail_proxy_snapshot" or (
        task_type == "external_data_sync" and payload.get("dataset") == "tail_proxy"
    )
