"""组合回测快照构建函数（WP7.2）。

为 BacktestRun 提供决策上下文快照构建能力，确保历史回测即使
成员/规则/模型改变仍按原快照可读。

快照内容覆盖 Spec WP7.2 要求保存的 12 类信息：
1. 成员 ID 列表 → member_snapshot_json[*].member_id
2. 标的 ID 列表 → symbol_ids_json
3. 成员有效日期 → member_snapshot_json[*].effective_from/effective_to
4. 执行模式快照 → member_snapshot_json[*].execution_mode
5. 买卖规则版本 → member_snapshot_json[*].entry_rule_version_id/exit_rule_version_id
6. 组合风控版本 → portfolio_rule_version_id
7. 成本配置 → cost_config_json（已存在字段，由本模块构建快照 dict）
8. 评分模式 → score_mode
9. 因子模型运行 ID → factor_model_run_id（已存在字段）
10. 数据截止时间 → data_cutoff_at
11. 引擎名称和版本 → engine_name / engine_version
12. 运行时排除标的及原因 → excluded_members_json

所有构建函数为纯函数，不依赖 DB 会话，方便单测与复用。
"""
from __future__ import annotations

from app.models.portfolio import Portfolio
from app.models.portfolio_member import PortfolioMember


def build_member_snapshot(members: list[PortfolioMember]) -> list[dict]:
    """构建成员快照 JSON 数据。

    保存每个成员在回测时的关键属性（成员 ID、标的 ID、有效日期、
    执行模式、买卖规则版本 ID），即使后续成员被归档或规则版本升级，
    历史回测仍可按原快照还原决策上下文。

    Args:
        members: 组合成员列表

    Returns:
        成员快照 dict 列表，可直接 json.dumps 序列化
    """
    return [
        {
            "member_id": m.id,
            "symbol_id": m.symbol_id,
            "effective_from": m.effective_from.isoformat() if m.effective_from else None,
            "effective_to": m.effective_to.isoformat() if m.effective_to else None,
            "execution_mode": m.execution_mode,
            "entry_rule_version_id": m.entry_rule_version_id,
            "exit_rule_version_id": m.exit_rule_version_id,
        }
        for m in members
    ]


def build_excluded_members_snapshot(
    excluded: list[tuple[PortfolioMember, "str | dict"]],
) -> list[dict]:
    """构建“不具备自动下单资格成员”审计快照。

    语义提醒（PT-DEF-20）：这里记的成员**仍然参与本次回测**（回测标的集统一
    取快照内全部有效成员），只是不会被当作自动成员执行。字段名 `excluded_*`
    是历史遗留命名，含义以本注释为准。

    Args:
        excluded: (成员, 原因) 元组列表。原因可以是字符串（旧写法，存为 `reason`），
            也可以是 dict（带 `reason` / `reason_code` / `reason_zh`，新写法）。

    Returns:
        审计快照 dict 列表；`member_id` / `symbol_id` / `reason` 始终存在，
        `reason_code` / `reason_zh` 仅在能拿到时写入（不编造）。
    """
    rows: list[dict] = []
    for m, reason in excluded:
        item = dict(reason) if isinstance(reason, dict) else {}
        raw_reason = item.get("reason") if isinstance(reason, dict) else reason
        row: dict = {
            "member_id": m.id,
            "symbol_id": m.symbol_id,
            "reason": raw_reason or "unknown",
        }
        if item.get("reason_code"):
            row["reason_code"] = str(item["reason_code"])
        if item.get("reason_zh"):
            row["reason_zh"] = str(item["reason_zh"])
        rows.append(row)
    return rows


def build_cost_config_snapshot(portfolio: Portfolio) -> dict:
    """构建成本配置快照。

    保存回测时的成本配置（佣金费率、印花税、滑点、最低佣金），
    确保历史回测的成本假设可追溯。

    注：当前 Portfolio 模型未直接持有成本配置字段（成本配置由
    DEFAULT_COST_CONFIG 或回测入参 cost_config 提供，存于
    BacktestRun.cost_config_json）。本函数使用 getattr 兜底 None，
    向前兼容后续 WP 扩展 Portfolio 字段后的直接读取场景。

    Args:
        portfolio: 组合实例

    Returns:
        成本配置 dict，字段缺失时值为 None
    """
    return {
        "commission_rate": getattr(portfolio, "commission_rate", None),
        "stamp_duty_rate": getattr(portfolio, "stamp_duty_rate", None),
        "slippage": getattr(portfolio, "slippage", None),
        "min_commission": getattr(portfolio, "min_commission", None),
    }


__all__ = [
    "build_member_snapshot",
    "build_excluded_members_snapshot",
    "build_cost_config_snapshot",
]
