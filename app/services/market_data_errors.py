"""Business-safe source failures; raw exceptions stay in the log chain."""
from __future__ import annotations


class MarketHistorySourceError(ValueError):
    def __init__(self, code: str, reason: str, *, source_label: str,
                 next_action: str, retryable: bool = True):
        super().__init__(reason)
        self.code = code
        self.source_label = source_label
        self.next_action = next_action
        self.retryable = retryable


def etf_qfq_source_error(cause: Exception, *, backup_attempted: bool = False) -> MarketHistorySourceError:
    types, seen = set(), set()
    current = cause
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        types.add(type(current).__name__)
        current = current.__cause__ or current.__context__
    if types & {"Timeout", "TimeoutError", "ConnectTimeout", "ReadTimeout"}:
        detail = "行情服务响应超时"
    elif types & {"ConnectionError", "RemoteDisconnected", "ProtocolError", "ProxyError"}:
        detail = "行情服务连接中断"
    elif "AkshareApiDisabledError" in types:
        detail = "该行情接口当前已禁用"
    else:
        detail = "来源未返回可核验的前复权行情"
    disabled = "AkshareApiDisabledError" in types
    return MarketHistorySourceError(
        "ETF_QFQ_SOURCE_UNAVAILABLE",
        f"ETF前复权行情获取失败：{detail}，本次未写入该标的行情；未使用不复权数据替代。",
        source_label="东方财富 / 腾讯财经 · ETF前复权日K" if backup_attempted else "东方财富 · ETF前复权日K",
        next_action="到接口管理核实并启用对应行情接口，再仅重试失败步骤。" if disabled else
                    "稍后仅重试本标的失败步骤。若持续失败，可核查有明确前复权口径的历史资料后按正式流程导入；已成功的其他标的不必重跑。",
        retryable=not disabled,
    )
