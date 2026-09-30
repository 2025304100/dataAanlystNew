"""task_type 的用户可读中文名（服务端文案用）。

为什么需要它：告警标题原先是 f"{task.task_type} 任务失败"，于是「告警中心」里
用户看到的是 `factor_pipeline 任务失败`、`hot_rank_snapshot 任务失败` —— 内部 code
直接进了成人可读文案（拟真走查 §四 检出）。项目规范要求 UI 不暴露内部常量。

与前端 frontend/src/utils/taskTypeLabel.ts 是**同一份取值集的两处实现**（跨语言无法
共享代码），所以由守护 test_task_type_label_maps_do_not_drift 锁住两边键集合一致：
新增 task_type 时只改一边就会红。
"""
from __future__ import annotations

TASK_TYPE_LABELS_ZH: dict[str, str] = {
    # 行情与股票池
    "market_data_sync": "行情同步",
    "universe_incremental_sync": "行情增量同步",
    "universe_sync": "股票池同步",
    "universe_backfill": "股票池补齐",
    "universe_industry_backfill": "股票池行业补齐",
    "universe_range_repair": "股票池区间修复",
    "universe_smart_sync": "股票池智能同步",
    "data_mirror": "数据镜像",
    "history_initialization": "历史初始化",
    "index_daily_sync": "指数日线同步",
    "index_prices_sync": "指数行情同步",
    # 因子与挖掘
    "factor_pipeline": "因子流水线",
    "factor_mining": "因子挖掘",
    "factor_mining_validation": "因子挖掘验证",
    # 注：这里列的必须与 frontend/src/utils/taskTypeLabel.ts 保持同集（守护
    # test_task_type_label_maps_do_not_drift 会比两边的键集合）。因子评估类只留
    # wp5_evaluation 这一个实际出现在 app/ 扫描口径里的取值。
    "wp5_evaluation": "因子评估",
    "discovery_mining": "机会挖掘",
    "discovery_fast_scan": "快速筛选",
    "discovery": "机会发现",
    "discovery_data_prep": "扫描数据准备",
    # 外部数据
    "macro_update": "宏观更新",
    "financial_report_sync": "财报同步",
    "hot_rank_snapshot": "热榜快照",
    "lhb_institution_sync": "龙虎榜机构同步",
    "tail_proxy_snapshot": "尾盘快照采集",
    "external_api_probe": "接口探测",
    "external_data_sync": "外部数据同步",
    # 组合交易
    "portfolio_auto_trade": "组合自动交易",
    "portfolio_equity_snapshot": "组合权益快照",
}

# external_sync_<dataset> 是运行时按数据集拼出来的前缀族，统一按一句中文呈现。
_EXTERNAL_SYNC_PREFIX = "external_sync_"


def task_type_label_zh(task_type: str | None) -> str:
    """返回可读中文名；未收录时保留原值（宁可暴露给运维，也不显示成空字符串）。

    未收录属于"守护该管住的事"：test_backend_task_types_have_frontend_labels 与
    test_task_type_label_maps_do_not_drift 会把它挑出来。
    """
    value = (task_type or "").strip()
    if not value:
        return "异步任务"
    if value in TASK_TYPE_LABELS_ZH:
        return TASK_TYPE_LABELS_ZH[value]
    if value.startswith(_EXTERNAL_SYNC_PREFIX):
        return "外部数据同步"
    return value
