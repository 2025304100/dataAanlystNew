"""Portfolio-scoped, bounded facts for analysis; never accepts client snapshots or tools."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.services.ai.context_pack import ContextPack, sanitize_context


def build_inspection_context(db: Session, portfolio_id: int, *, symbol_ids: list[int] | None = None,
                             trade_date: str | None = None, step: str | None = None):
    from app.services.portfolio_inspection_diagnostics import build_portfolio_inspection
    from app.services.portfolio_inspection_runtime import get_portfolio_runtime_inspection
    from app.services.portfolio_reconciliation_job import get_portfolio_reconciliation_jobs
    from app.services.portfolio_resume_service import get_reconciliation_queue

    # Read failures remain explicit. Never substitute an empty passing result.
    unknowns, facts = [], {}
    for key, reader in (("data", build_portfolio_inspection), ("runtime", get_portfolio_runtime_inspection),
                        ("queue", get_reconciliation_queue), ("jobs", get_portfolio_reconciliation_jobs)):
        try:
            value = reader(db, portfolio_id)
            facts[key] = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
        except Exception:
            facts[key] = {"status": "READ_ERROR"}
            unknowns.append(f"{key}读取失败，不能据此判定通过")
    data = facts.get("data", {})
    all_symbols = data.get("symbols", [])
    requested = set(symbol_ids or [])
    if data.get("status") != "READ_ERROR" and requested - {s["symbol_id"] for s in all_symbols}:
        raise ValueError("所选标的不属于本次组合检查范围")
    selected = [s for s in all_symbols if not requested or s["symbol_id"] in requested]
    symbols = []
    evidence_refs = []
    for symbol in selected[:40]:
        evidence = symbol.get("evidence", [])
        evidence_refs.extend(e["evidence_id"] for e in evidence if e.get("evidence_id"))
        symbols.append({k: symbol.get(k) for k in ("symbol_id", "symbol", "name", "market", "expected_trade_date", "scoring")}
                       | {"evidence": [{k: e.get(k) for k in ("evidence_id", "label", "status", "reason", "value", "expected", "source_label", "source_record_ref", "actual_trade_date", "expected_trade_date", "available_at", "depends_on")} for e in evidence[:24]]})
    if len(selected) > len(symbols):
        unknowns.append(f"本次仅提供前{len(symbols)}个标的，另{len(selected)-len(symbols)}个须分批分析")
    facts["data"] = {k: data.get(k) for k in ("snapshot_id", "generated_at", "binding", "calendars", "evidence", "limitations")}
    facts["data"]["symbols"] = symbols
    scope = {"portfolio_id": portfolio_id, "symbol_ids": sorted(requested), "trade_date": trade_date, "step": step}
    # Dates are explanatory scope, never used to overwrite observed evidence dates.
    if trade_date and any(s.get("expected_trade_date") != trade_date for s in symbols):
        unknowns.append("所选交易日与当前数据应到日不完全一致；历史材料需从处理记录查看，不能混作同一时点")
    actions = [{"action_type": key, "label": label} for key, label in (
        ("VIEW_EXECUTION", "核查执行与资金持仓"), ("PREVIEW_DATA_REPAIR", "预览批量数据修复"),
        ("REVIEW_BINDING", "核实实际运行绑定"), ("VIEW_JOB", "查看自动对账作业"),
        ("VIEW_HISTORY", "查看相关处理记录"), ("REVIEW_COVERAGE", "核查历史未知与新覆盖起点"))]
    runtime = facts.get("runtime", {})
    unknowns.extend(runtime.get("recovery_summary", {}).get("blockers", []))
    encoded = json.dumps({"scope": scope, "facts": facts}, ensure_ascii=False, sort_keys=True, default=str)
    snapshot_id = hashlib.sha256(encoded.encode()).hexdigest()
    # Limit before serializing into the AI context. Always disclose truncation.
    if len(encoded) > 42000:
        facts = {"runtime": runtime, "data_snapshot_id": data.get("snapshot_id"), "partial_material": encoded[:30000]}
        unknowns.append("材料超出本次上下文预算，部分明细被截断，须回正式证据页核实")
    source_as_of = [{"symbol_id": s.get("symbol_id"), "bar_trade_date": s.get("actual_bar_trade_date"),
        "score_trade_date": s.get("actual_score_trade_date"), "expected_trade_date": s.get("expected_trade_date")} for s in selected[:40]]
    observed_dates = {value for row in source_as_of for key, value in row.items() if key in {"bar_trade_date", "score_trade_date"} and value}
    same_cutoff = bool(source_as_of) and len(observed_dates) == 1 and all(row["bar_trade_date"] and row["score_trade_date"] for row in source_as_of)
    if not same_cutoff:
        unknowns.append("各来源截止时点不同或尚未提供，不以本次检查时间代替数据截止时间")
    metadata = {"inspection_snapshot_id": snapshot_id, "generated_at": datetime.now(timezone.utc).isoformat(),
                "data_as_of": next(iter(observed_dates)) if same_cutoff else None,
                "source_as_of": source_as_of, "scope": scope, "unknowns": list(dict.fromkeys(unknowns)),
                "objects": [{"symbol_id": s["symbol_id"], "symbol": s.get("symbol"), "market": s.get("market"),
                    "trade_date": s.get("expected_trade_date")} for s in selected[:40]],
                "available_actions": actions, "evidence_refs": evidence_refs[:200]}
    pack = ContextPack(user_question="解释当前阻断，区分事实、推断与未知，按依赖给出最短修复路径与完成条件。",
        source_page="portfolio-inspection", references={"portfolio_id": portfolio_id},
        data_status={"credibility": "requires_evidence", "missing_items": metadata["unknowns"]},
        capabilities={"execute_tools": False, "human_confirmation_required": True},
        untrusted_content=[{"is_untrusted": True, "kind": "inspection_evidence", "facts": facts}], metadata=metadata)
    pack = sanitize_context(pack)
    pack.metadata["version"] = 1
    pack.metadata["inspection_snapshot_id"] = hashlib.sha256(json.dumps({"scope": pack.metadata["scope"],
        "material": pack.untrusted_content}, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
    return pack
