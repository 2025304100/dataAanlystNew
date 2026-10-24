"""组合行动项聚合服务（P0，只读）。

把三套互不联通的状态源归并成一份按优先级排序、可执行的行动项：
  - readiness：自动交易五维门禁（app.services.auto_trade_readiness）
  - HG1 九状态机：portfolio_state_machine._get_status
  - 四维交易状态、实际执行来源和已应用策略快照；
  - G7 运维条件：g7_operational_readiness（只作灰度信息展示）。

设计约束：
  - 纯只读：不写库、不触发状态转移/对账/审计；
  - 同族去重：数据新鲜度问题在 readiness（MARKET_DATA_STALE）与 HG1
    （DATA_INCOMPLETE_PAUSED / SCORE_STALE）会重复出现，合并为一条；
  - 未知状态安全兜底，绝不向用户裸抛英文枚举（前端还有一层兜底）。
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
import hashlib
import json
from typing import Any

from sqlalchemy.orm import Session

from app.models.portfolio import AUTO_TRADE_SOURCE_MEMBERS_ONLY, AUTO_TRADE_SOURCE_PORTFOLIO, Portfolio
from app.services.auto_trade_readiness import (
    BLOCKER_DECISION_ENGINE_SOURCE_REQUIRED,
    BLOCKER_NO_TRADEABLE_RANGE,
    BLOCKER_STRATEGY_SNAPSHOT_REQUIRED,
    BLOCKER_STRATEGY_BINDING_INVALID,
    READINESS_FIX_REGISTRY,
    WARNING_SIGNAL_EMPTY_TODAY,
    WARNING_SOURCE_ENV_OVERRIDE,
    AutoTradeReadinessResult,
    ReadinessIssue,
    _blocker,
    get_auto_trade_readiness,
)
from app.services.g7_operational_readiness import evaluate_g7_operational_status
from app.services.portfolio_state_machine import _get_status as _psm_get_status
from app.services.portfolio_status import resolve_composite_status

logger = __import__("logging").getLogger(__name__)

SEVERITY_ORDER = {"blocker": 0, "warning": 1, "info": 2}
SOURCE_ORDER = {"readiness": 0, "hg1_status": 1, "g7_ops": 2}

# 跨源「同一根因」归并族：数据新鲜度问题三处都可能报，只给用户一条。
FAMILY_DATA_FRESHNESS = "data-freshness"
HEALTHY_AUTO_STATES = frozenset({"READY", "RUNNING_AUTO_SIMULATION"})
HEALTHY_DIMENSION_STATES = frozenset({"READY", "RUNNING"})

# HG1 状态 → 行动项；值为 None 表示健康/运行态，不产项。
HG1_ACTION_MAP: dict[str, dict[str, Any] | None] = {
    "READY": None,
    "RUNNING_AUTO_SIMULATION": None,
    "RUNNING_BACKTEST": {
        "severity": "blocker",
        "title": "回测任务运行中",
        "impact": "等待回测结束后再自动接管，避免同一组合并发执行",
        "action_type": "subtab",
        "target": "backtest",
        "family": "execution-busy",
    },
    "PENDING_INITIAL_REVIEW": {
        "severity": "blocker",
        "title": "等待本人首次确认",
        "impact": "完成本人首次审查后，组合才能进入自动接管",
        "action_type": "subtab",
        "target": "governance:state",
        "family": None,
    },
    "DATA_INCOMPLETE_PAUSED": {
        "severity": "blocker",
        "title": "数据缺口暂停",
        "impact": "数据不完整已暂停新买入，风险退出仍可执行",
        "action_type": "redirect",
        "target": "settings:data-center",
        "family": FAMILY_DATA_FRESHNESS,
    },
    "RECONCILIATION_BLOCKED": {
        "severity": "blocker",
        "title": "对账差异阻塞",
        "impact": "对账未平，已暂停新买入；风险退出仍可执行，恢复需本人确认",
        "action_type": "guided",
        "target": "governance:reconciliation",
        "family": "reconciliation",
    },
    "MODEL_INACTIVE": {
        "severity": "blocker",
        "title": "模型未激活",
        "impact": "模型绑定尚未确认，已暂停新买入；请核验当前模型后本人确认",
        "action_type": "open-modal",
        "target": "model-binding-review",
        "family": "model",
    },
    "SCORE_STALE": {
        "severity": "blocker",
        "title": "评分不新鲜",
        "impact": "评分覆盖率或新鲜度未达标，已暂停新买入",
        "action_type": "redirect",
        "target": "settings:data-center",
        "family": FAMILY_DATA_FRESHNESS,
    },
    "INTERRUPTED": {
        "severity": "blocker",
        "title": "任务异常中断",
        "impact": "执行心跳超时，恢复扫描器会尝试自动接续",
        "action_type": "subtab",
        "target": "governance:state",
        "family": "interrupted",
    },
    "ADMIN_PAUSED": {
        "severity": "blocker",
        "title": "本人已暂停接管",
        "impact": "紧急暂停中，恢复需本人填写审查备注",
        "action_type": "subtab",
        "target": "governance:state",
        "family": "admin-paused",
    },
}

HG1_FALLBACK = {
    "severity": "blocker",
    "title": "检测到一项待处理状态",
    "impact": "系统返回了未登记的组合状态，如需处理请联系支持",
    "action_type": "none",
    "target": None,
    "family": None,
}


def _primary_action(fix_link: dict[str, Any] | None) -> dict[str, Any] | None:
    if not fix_link:
        return None
    action_type = fix_link.get("action_type", "none")
    if action_type == "none":
        return None
    action = {"action_type": action_type}
    target = fix_link.get("target")
    if target:
        action["target"] = target
    return action


def _readiness_items(
    readiness: AutoTradeReadinessResult, *, waiting_for_signal: bool = False
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    # 基础级别由所在列表决定；注册表 display_severity 仅做展示降级（如今日无信号→info）。
    for issue in readiness.blockers:
        # Display-only distinction: no signal in a configured candidate range
        # is normal waiting. Keep the underlying execution readiness false.
        if waiting_for_signal and issue.code == BLOCKER_NO_TRADEABLE_RANGE:
            continue
        items.append(_issue_to_item("readiness", issue, base_severity="blocker"))
    for issue in readiness.warnings:
        meta = READINESS_FIX_REGISTRY.get(issue.code, {})
        severity = meta.get("display_severity") or "warning"
        items.append(_issue_to_item("readiness", issue, base_severity=severity))
    return items


def _issue_to_item(source: str, issue: Any, *, base_severity: str) -> dict[str, Any]:
    family = FAMILY_DATA_FRESHNESS if issue.code == "MARKET_DATA_STALE" else None
    if issue.code in {WARNING_SOURCE_ENV_OVERRIDE, BLOCKER_DECISION_ENGINE_SOURCE_REQUIRED}:
        family = "execution-source"
    return {
        "id": f"{source}:{issue.code}",
        "source": source,
        "code": issue.code,
        "severity": base_severity,
        # title 后端注册表已给；未知 code 缺失时退回 message，保证主文案不裸显枚举。
        "title": issue.title or issue.message,
        "impact": READINESS_FIX_REGISTRY.get(issue.code, {}).get("impact") or issue.message,
        "detail": "\n".join(part for part in (issue.message, issue.detail) if part),
        "family": family or f"{source}:{issue.code}",
        "primary_action": _primary_action(issue.fix_link),
        "sources": [source],
    }


def _hg1_item(state: str | None) -> dict[str, Any] | None:
    if not state:
        return None
    meta = HG1_ACTION_MAP.get(state, HG1_FALLBACK)
    if meta is None:
        return None
    action_type = meta["action_type"]
    primary = None
    if action_type != "none":
        primary = {"action_type": action_type}
        if meta.get("target"):
            primary["target"] = meta["target"]
    return {
        "id": f"hg1_status:{state}",
        "source": "hg1_status",
        "code": state,
        "severity": meta["severity"],
        "title": meta["title"],
        "impact": meta["impact"],
        "detail": None,
        "family": meta["family"] or f"hg1_status:{state}",
        "primary_action": primary,
        "sources": ["hg1_status"],
    }


def _g7_items(blockers: list[str]) -> list[dict[str, Any]]:
    """P0 兼容路径：纯文本阻断合并成一条 info 项。"""
    if not blockers:
        return []
    return [
        {
            "id": "g7_ops:operational",
            "source": "g7_ops",
            "code": "G7_OPERATIONAL_BLOCKERS",
            "severity": "info",
            "title": "灰度运行条件待确认",
            "impact": blockers[0],
            "detail": "\n".join(blockers),
            "family": "g7_ops:operational",
            "primary_action": None,
            "sources": ["g7_ops"],
        }
    ]


def _g7_structured_items(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """P1：G7 结构化阻断逐条转为行动项（统一 info 运维级）。"""
    items: list[dict[str, Any]] = []
    for entry in entries:
        code = str(entry.get("code") or "G7_OPERATIONAL_BLOCKERS")
        severity = entry.get("severity") if entry.get("severity") in SEVERITY_ORDER else "info"
        items.append(
            {
                "id": f"g7_ops:{code}",
                "source": "g7_ops",
                "code": code,
                "severity": severity,
                "title": entry.get("title") or "灰度运行条件待确认",
                "impact": entry.get("impact") or (entry.get("detail") or ""),
                "detail": entry.get("detail"),
                "family": f"g7_ops:{code}",
                "primary_action": _primary_action(entry.get("fix_link")),
                "sources": ["g7_ops"],
            }
        )
    return items


def _merge_family(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同族项合并：保留最高 severity，内容优先取可操作性最强的 readiness 项。"""
    groups: dict[str, list[dict[str, Any]]] = {}
    passthrough: list[dict[str, Any]] = []
    for item in items:
        groups.setdefault(item["family"], []).append(item) if item.get("family") else passthrough.append(item)

    merged: list[dict[str, Any]] = []
    for family, members in groups.items():
        if len(members) == 1:
            merged.append(members[0])
            continue
        # 同族取最高级别；同级 readiness > hg1_status > g7_ops。
        anchor = min(
            members,
            key=lambda m: (SEVERITY_ORDER[m["severity"]], SOURCE_ORDER.get(m["source"], 9)),
        )
        sources: list[str] = []
        for m in members:
            for s in m["sources"]:
                if s not in sources:
                    sources.append(s)
        keep = dict(anchor)
        keep["id"] = family
        keep["sources"] = sources
        details = list(dict.fromkeys(m["detail"] for m in members if m.get("detail")))
        keep["detail"] = "\n".join(details) or None
        merged.append(keep)
    return merged + passthrough


def aggregate_action_items(
    *,
    readiness: AutoTradeReadinessResult | None,
    hg1_state: str | None,
    g7_blockers: list[str] | None = None,
    g7_structured: list[dict[str, Any]] | None = None,
    dimension_states: dict[str, str] | None = None,
    execution_blockers: list[ReadinessIssue] | None = None,
    binding_review: dict[str, Any] | None = None,
    for_schedule: bool = True,
    generated_at: str,
) -> dict[str, Any]:
    """纯函数聚合：三源 → 排序去重后的行动项 + 计数摘要。供 API 与单测共用。"""
    items: list[dict[str, Any]] = []
    dimensions = dict(dimension_states or {})
    invalid_binding = bool(
        binding_review
        and binding_review.get("snapshot_id")
        and binding_review.get("blockers")
        and not binding_review.get("requires_confirmation")
    )
    governance_ready = (
        hg1_state in HEALTHY_AUTO_STATES
        and all(state in HEALTHY_DIMENSION_STATES for state in dimensions.values())
        and not bool((binding_review or {}).get("requires_confirmation"))
        and not invalid_binding
    )
    execution_issues = list(execution_blockers or [])
    if invalid_binding:
        execution_issues.append(_blocker(
            BLOCKER_STRATEGY_BINDING_INVALID,
            "；".join(str(reason) for reason in binding_review["blockers"]),
        ))
    signal_scope_available = bool(
        readiness is not None
        and (
            (readiness.source_mode == AUTO_TRADE_SOURCE_MEMBERS_ONLY and readiness.auto_member_count > 0)
            or (
                readiness.source_mode == AUTO_TRADE_SOURCE_PORTFOLIO
                and (readiness.candidate_count > 0 or readiness.auto_member_count > 0)
            )
        )
        and readiness.details.get("data_scope_symbol_count", 1) > 0
    )
    waiting_for_signal = bool(
        readiness is not None
        and readiness.enabled
        and readiness.account_ready
        and readiness.data_ready
        and readiness.source_ready
        and readiness.rule_configured
        and (readiness.schedule_ready or not for_schedule)
        and signal_scope_available
        and readiness.position_count == 0
        and readiness.eligible_buy_count == 0
        and any(issue.code == WARNING_SIGNAL_EMPTY_TODAY for issue in readiness.warnings)
        and all(issue.code == BLOCKER_NO_TRADEABLE_RANGE for issue in readiness.blockers)
        and governance_ready
        and not execution_issues
    )
    if readiness is not None:
        items.extend(_readiness_items(readiness, waiting_for_signal=waiting_for_signal))
    items.extend(_issue_to_item("readiness", issue, base_severity="blocker") for issue in execution_issues)
    hg1 = _hg1_item(hg1_state)
    if hg1:
        items.append(hg1)
    for dimension, state in dimensions.items():
        if state in HEALTHY_DIMENSION_STATES:
            continue
        item = _hg1_item(state)
        if item:
            item["detail"] = f"{dimension}={state}"
            items.append(item)
    if (binding_review or {}).get("requires_confirmation"):
        model_item = _hg1_item("MODEL_INACTIVE")
        model_item["detail"] = "模型绑定变更待本人核验确认"
        items.append(model_item)
    # 优先结构化逐条；无结构化时回退 P0 文本合并。
    if g7_structured:
        items.extend(_g7_structured_items(g7_structured))
    else:
        items.extend(_g7_items(list(g7_blockers or [])))

    items = _merge_family(items)
    items.sort(
        key=lambda m: (
            SEVERITY_ORDER[m["severity"]],
            SOURCE_ORDER.get(m["source"], 9),
            m["code"],
        )
    )

    blocker_count = sum(1 for m in items if m["severity"] == "blocker")
    daily_blocker_count = sum(1 for m in items if m["severity"] == "blocker" and m["source"] != "g7_ops")
    engine_blocker_count = sum(1 for m in items if m["source"] == "g7_ops")
    ready = bool(
        governance_ready
        and not daily_blocker_count
        and (readiness.ready if readiness is not None else True)
    )
    if readiness is not None and not readiness.enabled:
        execution_state = "disabled"
    elif daily_blocker_count or not governance_ready:
        execution_state = "blocked"
    elif hg1_state == "RUNNING_AUTO_SIMULATION":
        execution_state = "running"
    elif waiting_for_signal:
        execution_state = "waiting_signal"
    elif ready:
        execution_state = "ready"
    else:
        execution_state = "blocked"
    summary = {
        "blockers": blocker_count,
        "warnings": sum(1 for m in items if m["severity"] == "warning"),
        "info": sum(1 for m in items if m["severity"] == "info"),
        "ready": ready,
        "waiting_for_signal": waiting_for_signal,
        "execution_state": execution_state,
        "daily_blocked": bool(daily_blocker_count or not governance_ready),
        "daily_blocker_count": daily_blocker_count,
        "engine_admission_blocked": bool(engine_blocker_count),
        "engine_admission_blocker_count": engine_blocker_count,
    }
    # 五维指示灯只做展示；非模拟账户不跑就绪检查，给 null。
    checks = (
        {
            "enabled": readiness.enabled,
            "account_ready": readiness.account_ready,
            "data_ready": readiness.data_ready and all(
                dimensions.get(dimension, "READY") in HEALTHY_DIMENSION_STATES
                for dimension in ("status_score", "status_data")
            ),
            "source_ready": readiness.source_ready and not any(
                issue.code == BLOCKER_DECISION_ENGINE_SOURCE_REQUIRED for issue in execution_issues
            ),
            "schedule_ready": readiness.schedule_ready,
        }
        if readiness is not None
        else None
    )
    for item in items:
        _add_action_contract(item, detected_at=generated_at)
    return {
        "items": items, "summary": summary, "checks": checks,
        "generated_at": generated_at, "binding_review": binding_review,
    }


def _add_action_contract(item: dict, *, detected_at: str, portfolio_id: int | None = None) -> None:
    """Add controlled navigation and evidence without inventing a write API."""
    engine_only = item["source"] == "g7_ops"
    item.setdefault("item_id", item["id"])
    item.setdefault("root_cause_id", item.get("family") or item["id"])
    item.setdefault("reason_code", item["code"])
    item.setdefault("verdict", "BLOCKED" if item["severity"] == "blocker" else "REVIEW_REQUIRED")
    item.setdefault("trade_date", None)
    item.setdefault("scope", {"kind": "engine_admission" if engine_only else "portfolio", "portfolio_id": portfolio_id})
    item.setdefault("affected_capabilities", ["engine_switch", "scope_expansion"] if engine_only else
        ["new_buy", "automatic_recovery"] if item["severity"] == "blocker" else [])
    item.setdefault("portfolio_wide", not engine_only and item["severity"] == "blocker")
    item.setdefault("bypassable", False)
    item.setdefault("preconditions", [])
    item.setdefault("audit_refs", [])
    item.setdefault("report_refs", [])
    item.setdefault("task_refs", [])
    item.setdefault("issue_ids", [])
    item.setdefault("depends_on", [])
    item.setdefault("related_item_ids", [])
    item.setdefault("resolution_status", "unresolved")
    item.setdefault("completion_criteria", ["实际来源证据满足当前门禁后重新检查；点击或备注不视为完成"])
    item.setdefault("detected_at", detected_at)
    item.setdefault("version", 1)
    label = {"g7_ops": "新方案运行条件", "hg1_status": "组合运行状态", "readiness": "自动接管就绪检查"}.get(item["source"], "未登记来源")
    item.setdefault("evidence", [{"evidence_id": item["id"], "check_code": item["code"], "label": item["title"],
        "value": item.get("detail"), "expected": "对应门禁条件满足", "status": item["verdict"],
        "reason": item.get("detail") or item["impact"], "source_code": item["source"], "source_label": label,
        "observed_at": detected_at}])
    primary = item.get("primary_action")
    item.setdefault("actions", [{"action_id": f"inspect:{item['id']}", "label": "查看证据并处理", "kind": "inspect",
        "is_primary": True, "enabled": bool(primary and primary.get("target")),
        "disabled_reason": None if primary and primary.get("target") else "此来源暂无可自动执行的处理器，请核查所列证据与来源",
        "target": primary.get("target") if primary else None, "required_role": None,
        "expected_effect": "打开真实处理入口并在完成后重新检查", "confirmation_required": False,
        "idempotency_scope": None, "preconditions": []}])
    item.setdefault("snapshot_id", hashlib.sha256(json.dumps({key: item.get(key) for key in (
        "id", "code", "verdict", "scope", "issue_ids", "report_refs")}, sort_keys=True, default=str).encode()).hexdigest())


def _enrich_inspection_items(db: Session, portfolio_id: int, payload: dict) -> dict:
    from app.services.portfolio_inspection_diagnostics import BAD_STATUSES, build_portfolio_inspection
    from app.services.portfolio_inspection_runtime import get_portfolio_runtime_inspection
    from app.services.portfolio_inspection_repair import list_inspection_repair_tasks
    from app.services.portfolio_resume_service import get_reconciliation_queue
    inspection = build_portfolio_inspection(db, portfolio_id)
    runtime = get_portfolio_runtime_inspection(db, portfolio_id)
    queue = get_reconciliation_queue(db, portfolio_id)
    tasks = list_inspection_repair_tasks(db, portfolio_id, limit=20)["items"]
    all_evidence = [ev for ev in inspection["evidence"] + [ev for row in inspection["symbols"] for ev in row["evidence"]]
                    if ev["check_code"] not in {"STRATEGY_BINDING", "RULE_BINDING", "MEMBER_RULE_VERSION"}]
    bad = [ev for ev in all_evidence if ev["status"] in BAD_STATUSES]
    data_checks = next(check for check in runtime["recovery_summary"]["checks"] if check["key"] == "data")
    data_item = next((item for item in payload["items"] if item.get("family") == FAMILY_DATA_FRESHNESS), None)
    if bad and data_item is None:
        data_item = {"id": FAMILY_DATA_FRESHNESS, "family": FAMILY_DATA_FRESHNESS, "source": "readiness",
            "sources": ["data_diagnostics"], "code": "DATA_DIAGNOSTIC_GAPS", "title": "当前依赖的数据仍有缺口",
            "impact": data_checks["reason"], "detail": data_checks["reason"],
            "severity": "blocker" if data_checks["passed"] is not True else "warning",
            "primary_action": {"action_type": "guided", "target": "governance:data"}}
        payload["items"].append(data_item)
    if data_item:
        ids = {ev["evidence_id"] for ev in bad}
        related_tasks = [task for task in tasks if ids.intersection(task["issue_ids"])]
        data_item.update(evidence=all_evidence, issue_ids=sorted(ids), snapshot_id=inspection["snapshot_id"],
            verdict="UNKNOWN" if data_checks["passed"] is None else "BLOCKED" if data_checks["passed"] is False else "SYMBOL_RESTRICTED",
            scope={"kind": "portfolio" if data_checks["passed"] is not True else "symbols", "portfolio_id": portfolio_id,
                   "symbol_ids": sorted({ev["symbol_id"] for ev in bad if ev.get("symbol_id") is not None})},
            portfolio_wide=data_checks["passed"] is not True,
            affected_capabilities=sorted({cap for ev in bad for cap in ev.get("affected_capabilities", [])}),
            task_refs=[f"async_task:{task['task_id']}" for task in related_tasks],
            audit_refs=sorted({ref for task in related_tasks for ref in task["audit_refs"]}),
            report_refs=sorted({ref for task in related_tasks for ref in task["report_refs"]}),
            resolution_status="in_progress" if any(task["status"] not in {"done", "failed", "cancelled", "partial"} for task in related_tasks) else "unresolved",
            depends_on=[], repair_dependencies=[{"operation_id": op["operation_id"], "depends_on": op["depends_on"]}
                for op in inspection["repair_plan"]["operations"]],
            completion_criteria=inspection["repair_plan"]["completion_criteria"],
            actions=[{"action_id": "inspect_data_repair_plan", "label": "查看缺口并确认修复范围", "kind": "inspect", "is_primary": True,
                "enabled": True, "disabled_reason": None, "target": "governance:data", "required_role": None,
                "expected_effect": "查看当前组合的逐对象修复预览，确认后提交真实作业并重检", "confirmation_required": False,
                "idempotency_scope": None, "preconditions": []}],
            coverage=data_checks.get("coverage"), restricted_symbol_ids=data_checks.get("restricted_symbol_ids", []))
    pending = [day for day in queue["days"] if day["status"] != "PASSED" and not day.get("history_only")
               and day.get("work_state") != "waiting_execution"]
    if pending:
        payload["items"] = [item for item in payload["items"] if item.get("family") != "reconciliation"]
        for day in pending:
            item_id = f"recon:{portfolio_id}:{queue.get('baseline_id', 'legacy')}:{queue.get('generation', 0)}:{day['trade_date']}"
            payload["items"].append({"id": item_id, "family": item_id, "source": "reconciliation", "sources": ["reconciliation"],
                "code": day["status"], "title": f"{day['trade_date']} 对账尚未完成", "impact": day["reason"], "detail": day["reason"],
                "severity": "blocker", "verdict": day["status"], "trade_date": day["trade_date"],
                "scope": {"kind": "account_date", "portfolio_id": portfolio_id, "baseline_id": queue.get("baseline_id"),
                          "generation": queue.get("generation"), "trade_date": day["trade_date"]},
                "affected_capabilities": ["reconciliation_progression", "new_buy", "automatic_recovery"],
                "audit_refs": [f"data_governance_audit_event:{day['audit_event_id']}"] if day.get("audit_event_id") else [],
                "report_refs": [day["report_id"]] if day.get("report_id") else [],
                "snapshot_id": day.get("report_version"), "primary_action": {"action_type": "guided", "target": "governance:reconciliation"}})
    for item in payload["items"]:
        if item.get("code") == "RECONCILIATION_BLOCKED":
            item["title"], item["impact"] = "对账尚未完成，需核查证据", "缺证据与真实差异分别核实；当前状态本身不能证明账差"
        _add_action_contract(item, detected_at=payload["generated_at"], portfolio_id=portfolio_id)
        item["scope"]["portfolio_id"] = portfolio_id
    payload["items"].sort(key=lambda item: (SEVERITY_ORDER[item["severity"]], 0 if item.get("depends_on") else 1,
                                           item.get("trade_date") or "", item["id"]))
    summary = payload["summary"]
    summary.update(blockers=sum(item["severity"] == "blocker" for item in payload["items"]),
        warnings=sum(item["severity"] == "warning" for item in payload["items"]), info=sum(item["severity"] == "info" for item in payload["items"]),
        daily_blocked=runtime["permissions"]["allow_new_buys"] is not True,
        daily_blocker_count=sum(item["severity"] == "blocker" and item.get("portfolio_wide", True) and item["source"] != "g7_ops" for item in payload["items"]),
        engine_admission_blocked=any(item["source"] == "g7_ops" for item in payload["items"]),
        engine_admission_blocker_count=sum(item["source"] == "g7_ops" for item in payload["items"]),
        symbol_restriction_count=len(data_checks.get("restricted_symbol_ids", [])))
    payload.update(inspection_snapshot_id=inspection["snapshot_id"], runtime=runtime,
        reconciliation_queue=queue, repair_tasks=tasks,
        snapshot_id=hashlib.sha256(json.dumps({"diagnostics": inspection["snapshot_id"], "dimensions": runtime["dimensions"],
            "state": runtime["current_state"], "reports": [(day["trade_date"], day.get("report_version")) for day in queue["days"]]}, sort_keys=True).encode()).hexdigest())
    return payload


def build_portfolio_action_items(
    db: Session,
    portfolio_id: int,
    *,
    for_schedule: bool = True,
) -> dict[str, Any]:
    """DB 编排：只读取数后聚合，不产生任何写入或状态转移。"""
    # Suppress ORM autoflush as well as explicit writes: a caller may have
    # unrelated dirty objects in the same session while requesting this view.
    with db.no_autoflush:
        return _build_portfolio_action_items(db, portfolio_id, for_schedule=for_schedule)


def _execution_prerequisites(db: Session, portfolio_id: int) -> list[ReadinessIssue]:
    """Read the same source/snapshot conditions as run_dual_trade.

    This is display enrichment only; it never calls the executor or changes
    get_auto_trade_readiness's existing admission boolean.
    """
    from app.services.auto_trade_dual_run import (
        is_member_source_enabled,
        resolve_applied_snapshot_id,
    )

    issues: list[ReadinessIssue] = []
    if not is_member_source_enabled(portfolio_id):
        issues.append(_blocker(
            BLOCKER_DECISION_ENGINE_SOURCE_REQUIRED,
            "自动模拟交易只能通过已启用的成员来源与决策引擎订单计划执行",
        ))
    if resolve_applied_snapshot_id(db, portfolio_id=portfolio_id) is None:
        issues.append(_blocker(
            BLOCKER_STRATEGY_SNAPSHOT_REQUIRED,
            "自动模拟交易需要已应用的策略执行快照",
        ))
    return issues


def _binding_review(db: Session, portfolio: Portfolio) -> dict[str, Any]:
    from app.services.portfolio_binding_review import build_binding_review

    return build_binding_review(db, portfolio)


def _build_portfolio_action_items(
    db: Session, portfolio_id: int, *, for_schedule: bool
) -> dict[str, Any]:
    portfolio = db.get(Portfolio, portfolio_id)
    if portfolio is None:
        raise ValueError(f"Portfolio {portfolio_id} not found")

    # 与前端门禁一致：仅模拟账户跑 readiness；非模拟账户以 HG1 状态为准。
    is_simulated = portfolio.account_type == "simulated"
    readiness = (
        get_auto_trade_readiness(db, portfolio_id, for_schedule=for_schedule)
        if is_simulated
        else None
    )
    hg1_state = _psm_get_status(db, portfolio)
    composite = resolve_composite_status(portfolio)
    dimensions = {
        "status_score": composite.status_score.value,
        "status_data": composite.status_data.value,
        "status_model": composite.status_model.value,
        "status_reconciliation": composite.status_reconciliation.value,
    }
    execution_blockers = _execution_prerequisites(db, portfolio_id) if is_simulated else []
    binding_review = _binding_review(db, portfolio) if is_simulated else None
    g7 = (
        evaluate_g7_operational_status(db, portfolio_id=portfolio_id)
        if is_simulated
        else None
    )

    payload = aggregate_action_items(
        readiness=readiness,
        hg1_state=hg1_state,
        g7_blockers=list(g7.operational_blockers) if g7 is not None else [],
        g7_structured=list(getattr(g7, "structured_blockers", []) or []) if g7 is not None else [],
        dimension_states=dimensions,
        execution_blockers=execution_blockers,
        binding_review=binding_review,
        for_schedule=for_schedule,
        generated_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    )
    return _enrich_inspection_items(db, portfolio_id, payload)
