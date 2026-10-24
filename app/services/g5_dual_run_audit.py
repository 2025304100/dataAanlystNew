"""Persistence and lookup for auditable G5 dual-run summaries."""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
import math
from typing import Any
from contextlib import contextmanager

from sqlalchemy import inspect, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.g5_dual_run import G5DualRunReport

_CURRENT_READ_KEY = "g5_report_current_read"


@contextmanager
def _read_mode(db, current_read):
    previous = db.info.get(_CURRENT_READ_KEY)
    db.info[_CURRENT_READ_KEY] = bool(current_read or previous)
    try:
        yield
    finally:
        if previous is None:
            db.info.pop(_CURRENT_READ_KEY, None)
        else:
            db.info[_CURRENT_READ_KEY] = previous


def _execute_select(db, statement):
    if db.info.get(_CURRENT_READ_KEY):
        statement = statement.with_for_update().execution_options(populate_existing=True)
    return db.execute(statement)


def _get(db, model, key):
    current = bool(db.info.get(_CURRENT_READ_KEY))
    return db.get(model, key, with_for_update=current, populate_existing=True)


def _canonical_json(summary: dict[str, Any]) -> str:
    return json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)


def _fingerprint(value: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _row_values(row) -> dict[str, Any]:
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


def _utc(value) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed


def _scope_errors(db: Session, summary: dict, candidate) -> list[str]:
    from app.models.decision_engine import StrategyExecutionSnapshot
    from app.services.portfolio_binding_review import _last_accepted_binding
    scope = summary.get("applicable_scope")
    if not isinstance(scope, dict) or scope.get("comparison_kind") != "engine_source":
        return ["缺少明确的引擎链验收适用范围"]
    accepted = _last_accepted_binding(db, summary["portfolio_id"], current_read=bool(db.info.get(_CURRENT_READ_KEY)))
    if not accepted or accepted.get("snapshot_id") != scope.get("baseline_snapshot_id"):
        return ["基线不是本组合最后明确接受的运行快照"]
    baseline = _get(db, StrategyExecutionSnapshot, scope["baseline_snapshot_id"])
    if baseline is None or baseline.portfolio_id != summary["portfolio_id"] or scope.get("candidate_snapshot_id") != candidate.id:
        return ["双侧运行快照不属于当前组合或候选已变化"]
    if scope.get("baseline_snapshot_sha256") != _fingerprint(_row_values(baseline)) or scope.get("candidate_snapshot_sha256") != _fingerprint(_row_values(candidate)):
        return ["双侧方案内容与报告冻结的指纹不一致"]
    for snapshot, side, runner in ((baseline, "baseline", "legacy_scan"), (candidate, "candidate", "unified_decision")):
        versions = json.loads(snapshot.versions_json or "{}")
        if (not isinstance(versions, dict) or versions.get("runner_ref") != runner
            or not isinstance(versions.get("engine_build_id"), str) or not versions["engine_build_id"].strip()
            or scope.get(side + "_engine_build_id") != versions["engine_build_id"]
            or scope.get(side + "_runner_ref") != runner):
            return ["运行快照没有可解析的实际执行器与构建来源"]
        required_source = scope.get("from_source_mode") if side == "baseline" else "members_only"
        if versions.get("auto_trade_source_mode") != required_source:
            return ["来源切换范围与双侧运行快照不一致"]
        if not isinstance(scope.get("scoring_config_sha256"), str) or versions.get("scoring_config_sha256") != scope["scoring_config_sha256"]:
            return ["双侧评分配置未固定为相同的实际输入"]
    if scope.get("from_source_mode") not in {"legacy_scan", "portfolio"} or scope.get("to_source_mode") != "members_only":
        return ["报告不适用于当前支持的成员来源切换"]
    for key in ("factor_model_run_id", "factor_set_id", "rule_id", "rule_version", "cost_config_json"):
        if getattr(baseline, key) != getattr(candidate, key) or key not in scope or scope[key] != getattr(candidate, key):
            return ["引擎链验收必须固定相同模型、因子集、策略和费用输入；本报告不能用于其他方案"]
    return []


def _input_manifest_errors(db: Session, day: dict, old_filters: dict, new, old_rows: list, new_rows: list, scope: dict) -> list[str]:
    from app.models.daily_bar import DailyBar
    from app.models.score import Score
    manifest = day.get("input_manifest")
    if not isinstance(manifest, list) or not manifest:
        return ["缺少真实行情与评分输入清单，输出记录哈希不能替代输入证据"]
    models = {"daily_bars": DailyBar, "scores": Score}
    seen, covered, actual = set(), {name: set() for name in models}, []
    cutoff = _utc(day["chain_a_meta"]["data_cutoff_at"])
    td = date.fromisoformat(day["trade_date"])
    new_scores = {row.symbol_id: row.score_id for row in new_rows}
    for ref in manifest:
        if not isinstance(ref, dict) or ref.get("source") not in models or type(ref.get("record_id")) is not int:
            return ["输入清单包含未支持或无法解析的来源"]
        key = (ref["source"], ref["record_id"])
        if key in seen:
            return ["输入清单包含重复来源"]
        seen.add(key)
        row = _get(db, models[key[0]], key[1])
        if row is None or row.trade_date != td or _utc(row.created_at) > cutoff:
            return ["输入记录不存在、日期不符或在截止时点后才可用"]
        if key[0] == "scores" and (
            new_scores.get(row.symbol_id) != row.id or row.pit_safety != "PIT_VERIFIED"
            or _utc(row.published_at) > cutoff
            or any(stamp is not None and _utc(stamp) > cutoff for stamp in (row.first_published_at, row.revision_at, row.factor_data_cutoff_at))
            or row.factor_model_run_id != scope["factor_model_run_id"] or row.factor_set_id != scope["factor_set_id"]):
            return ["评分来源未通过历史可见性检查，或不属于冻结模型与因子集"]
        if key[0] == "scores":
            config = json.loads(row.scoring_config_snapshot_json or "null")
            if not isinstance(config, dict) or not config or _fingerprint(config) != scope["scoring_config_sha256"]:
                return ["实际评分配置快照与双侧冻结输入不一致"]
            evidence = next((ev for ev in new_rows if ev.symbol_id == row.symbol_id), None)
            old_decision = next((ev for ev in old_rows if ev.symbol_id == row.symbol_id), None)
            if evidence is None or evidence.pit_safe_flag != "PIT_SAFE" or evidence.score_published_at is None or _utc(evidence.score_published_at) != _utc(row.published_at) or old_decision is None or old_decision.quality_score != row.quality_score:
                return ["逐标的决策未引用同一份可见评分输入"]
        fingerprint = _fingerprint(_row_values(row))
        if ref.get("sha256") != fingerprint:
            return ["真实输入内容已修订，原报告必须重新验收"]
        covered[key[0]].add(row.symbol_id)
        actual.append({"source": key[0], "record_id": key[1], "sha256": fingerprint})
    required_symbols = {row.symbol_id for row in old_rows} | {row.symbol_id for row in new_rows}
    if any(symbols != required_symbols for symbols in covered.values()):
        return ["输入清单未完整覆盖双侧实际标的"]
    input_hash = _fingerprint({"rows": actual})
    versions = json.loads(new.versions_json or "{}")
    if (old_filters.get("input_manifest_sha256") != input_hash or versions.get("input_manifest_sha256") != input_hash
        or any(day[name].get("input_manifest_sha256") != input_hash for name in ("chain_a_meta", "chain_b_meta"))):
        return ["两条真实运行记录未绑定同一份实际输入清单"]
    return []


def _task_errors(db: Session, summary: dict) -> list[str]:
    from app.models.async_task import AsyncTaskRecord
    mode = summary.get("execution_mode")
    if mode == "synchronous_export":
        return [] if summary.get("async_task_id") is None else ["同步报告不能冒用异步任务编号"]
    if mode != "async" or not isinstance(summary.get("async_task_id"), str):
        return ["报告缺少明确执行模式或异步任务引用"]
    task = _get(db, AsyncTaskRecord, summary["async_task_id"])
    if task is None or task.task_type != "g5_dual_run" or task.status != "done" or task.cancel_requested:
        return ["真实双跑任务尚未完成、失败、已取消或不存在"]
    payload = json.loads(task.payload_json or "{}")
    result = json.loads(task.result_json or "{}")
    if payload.get("portfolio_id") != summary["portfolio_id"] or result.get("replay_id") != summary.get("replay_id"):
        return ["异步任务不属于本组合或当前报告"]
    return []


def _execution_evidence_errors(db: Session, summary: dict[str, Any]) -> list[str]:
    """Resolve original inputs in the existing local database, without running engines."""
    from app.models.decision_engine import DecisionEvidence, DecisionRun, StrategyExecutionSnapshot
    from app.models.scan import ScanResult, ScanRun
    from app.services.g5_dual_run_replay import ACTION_TYPES, DailyChainResult, SecurityDecision, compare_one_day

    provenance = summary["provenance"]
    if provenance.get("trading_calendar") != "trade_calendar":
        return ["没有使用本机已核验的真实交易日历"]
    if not inspect(db.connection()).has_table("trade_calendar"):
        return ["本机缺少真实交易日历，无法核对报告日期"]
    from app.services.portfolio_inspection_diagnostics import resolve_inspection_calendar
    trusted = resolve_inspection_calendar(db, current_read=bool(db.info.get(_CURRENT_READ_KEY)))
    if trusted["status"] != "PASSED" or provenance.get("calendar_version") != trusted.get("version"):
        return ["交易所日历来源未核验或日历版本已变化，必须重新预检"]
    start, end = date.fromisoformat(summary["start_date"]), date.fromisoformat(summary["end_date"])
    calendar = db.execute(text("SELECT date, is_trading_day FROM trade_calendar WHERE date >= :start AND date <= :end ORDER BY date" +
        (" FOR UPDATE" if db.info.get(_CURRENT_READ_KEY) and db.get_bind().dialect.name != "sqlite" else "")),
                          {"start": start, "end": end}).all()
    dates = [str(row[0])[:10] for row in calendar if row[1] == 1]
    if len(calendar) != (end - start).days + 1 or dates != provenance["expected_trade_dates"]:
        return ["报告日期与真实交易日历不一致，或日历覆盖不完整"]
    latest_snapshot = _execute_select(db, select(StrategyExecutionSnapshot).where(
        StrategyExecutionSnapshot.portfolio_id == summary["portfolio_id"],
        StrategyExecutionSnapshot.snapshot_type == "save_and_apply",
    ).order_by(StrategyExecutionSnapshot.snapshot_no.desc(), StrategyExecutionSnapshot.created_at.desc(), StrategyExecutionSnapshot.id.desc()).limit(1)).scalar_one_or_none()
    if latest_snapshot is None or provenance.get("strategy_snapshot_id") != latest_snapshot.id:
        return ["报告没有对应当前组合已保存的执行方案"]
    if _utc(latest_snapshot.created_at) > _utc(provenance["exported_at"]):
        return ["候选方案在报告导出后才产生，不能作为原始执行证据"]
    scope_errors = _scope_errors(db, summary, latest_snapshot)
    if scope_errors:
        return scope_errors

    manifest = []
    for day in summary["daily_reports"]:
        td = date.fromisoformat(day["trade_date"])
        a_meta, b_meta = day["chain_a_meta"], day["chain_b_meta"]
        scan_id = str(a_meta["source_run_id"])
        old = _get(db, ScanRun, int(scan_id)) if scan_id.isdigit() else None
        new = _get(db, DecisionRun, b_meta["source_run_id"])
        if old is None or new is None:
            return [f"{td} 的执行编号无法对应真实扫描或决策记录"]
        if old.portfolio_id != summary["portfolio_id"] or new.portfolio_id != summary["portfolio_id"] or new.trade_date != td:
            return [f"{td} 的原始执行记录不属于当前组合或日期"]
        if any((_utc(meta["data_cutoff_at"]) + timedelta(hours=8)).date() > td for meta in (a_meta, b_meta)):
            return [f"{td} 的执行使用了后续日期的数据，不能作为历史证据"]
        if _utc(a_meta["data_cutoff_at"]) != _utc(b_meta["data_cutoff_at"]):
            return [f"{td} 双侧数据截止时点不一致，不能使用不同输入验收"]
        if any(finished is None or _utc(finished) > _utc(provenance["exported_at"]) for finished in (old.finished_at, new.finished_at)):
            return [f"{td} 的原始执行在报告导出时尚未完成"]
        if any(_utc(run.created_at) > _utc(provenance["exported_at"]) for run in (old, new)):
            return [f"{td} 的原始运行在报告导出后才产生"]
        old_filters = json.loads(old.filters_snapshot or "null")
        if not isinstance(old_filters, dict) or old_filters.get("trade_date") != td.isoformat() or _utc(old_filters.get("data_cutoff_at")) != _utc(a_meta["data_cutoff_at"]):
            return [f"{td} 的旧方案没有可核验的数据截止时间"]
        required_run_type = "backtest" if provenance["capture_mode"] == "historical_replay" else "research_preflight"
        if (old.status not in {"completed", "success"} or new.status != "SUCCEEDED"
            or not old.finished_at or not new.finished_at or new.run_type != required_run_type
            or new.blocking_status != "READY" or new.is_result_production_eligible != 1
            or new.strategy_snapshot_id != latest_snapshot.id or _utc(new.data_cutoff_at) != _utc(b_meta["data_cutoff_at"])):
            return [f"{td} 的执行未成功完成，或不对应本次方案及数据时间"]
        old_rows = _execute_select(db, select(ScanResult).where(ScanResult.scan_run_id == old.id).order_by(ScanResult.id)).scalars().all()
        new_rows = _execute_select(db, select(DecisionEvidence).where(DecisionEvidence.decision_run_id == new.id).order_by(DecisionEvidence.id)).scalars().all()
        if any(_utc(row.created_at) > _utc(provenance["exported_at"]) for row in old_rows + new_rows):
            return [f"{td} 的逐标的来源在报告导出后才写入"]
        old_ids, new_ids = [row.symbol_id for row in old_rows], [row.symbol_id for row in new_rows]
        if (not old_rows or not new_rows or len(set(old_ids)) != len(old_rows) or len(set(new_ids)) != len(new_rows)
            or old.total_in_snapshot != len(old_rows) or new.universe_count != len(new_rows)
            or not isinstance(new.score_coverage_pct, (int, float)) or not math.isfinite(new.score_coverage_pct) or not 0 < new.score_coverage_pct <= 100
            or any(row.quality_score is None or row.action not in ACTION_TYPES for row in old_rows)
            or any(row.portfolio_id != summary["portfolio_id"] or row.trade_date != td
                   or row.strategy_snapshot_id != latest_snapshot.id or row.action not in ACTION_TYPES
                   or _utc(row.data_cutoff_at) != _utc(new.data_cutoff_at) for row in new_rows)):
            return [f"{td} 缺少完整的实际标的和决策记录，空样本不能算通过"]
        expected_a_coverage = 100 * sum(row.quality_score is not None for row in old_rows) / old.total_in_snapshot
        if (day.get("chain_a_coverage_pct") != expected_a_coverage or day.get("chain_b_coverage_pct") != new.score_coverage_pct
            or not 0 < day["chain_a_coverage_pct"] <= 100 or not 0 < day["chain_b_coverage_pct"] <= 100):
            return [f"{td} 双侧实际覆盖率不足或与来源不一致"]
        contract = summary["applicable_scope"]
        old_contract, new_contract = old_filters.get("execution_contract"), json.loads(new.versions_json or "{}").get("execution_contract")
        if old_contract != {"snapshot_id": contract["baseline_snapshot_id"], "runner_ref": contract["baseline_runner_ref"], "engine_build_id": contract["baseline_engine_build_id"]} or new_contract != {"snapshot_id": latest_snapshot.id, "runner_ref": contract["candidate_runner_ref"], "engine_build_id": contract["candidate_engine_build_id"]}:
            return [f"{td} 原始运行没有固定报告声明的双侧执行器与构建"]
        input_errors = _input_manifest_errors(db, day, old_filters, new, old_rows, new_rows, contract)
        if input_errors:
            return [f"{td}：{reason}" for reason in input_errors]
        from app.models.async_task import AsyncTaskRecord
        if new.task_id:
            source_task = _get(db, AsyncTaskRecord, new.task_id)
            if source_task is None or source_task.status != "done" or source_task.cancel_requested:
                return [f"{td} 的来源任务未成功完成或已取消"]
        a_hash = _fingerprint({"run": _row_values(old), "rows": [_row_values(row) for row in old_rows]})
        b_hash = _fingerprint({"run": _row_values(new), "rows": [_row_values(row) for row in new_rows], "strategy": _row_values(latest_snapshot)})
        if a_meta.get("source_manifest_sha256") != a_hash or b_meta.get("source_manifest_sha256") != b_hash:
            return [f"{td} 的来源校验值与原始执行记录不一致"]
        actual = compare_one_day(
            DailyChainResult(chain="A", trade_date=td, universe_symbol_ids=old_ids, coverage_pct=expected_a_coverage,
                decisions={row.symbol_id: SecurityDecision(symbol_id=row.symbol_id, action=row.action) for row in old_rows}),
            DailyChainResult(chain="B", trade_date=td, universe_symbol_ids=new_ids, coverage_pct=new.score_coverage_pct,
                decisions={row.symbol_id: SecurityDecision(symbol_id=row.symbol_id, action=row.action,
                    reject_reason_code=row.rejection_reason) for row in new_rows}),
            portfolio_id=summary["portfolio_id"],
        )
        matrix = day.get("action_confusion", day.get("action_confusion_matrix"))
        if (not isinstance(matrix, dict) or set(matrix) != set(ACTION_TYPES)
            or any(not isinstance(row, dict) or set(row) != set(ACTION_TYPES) or any(type(count) is not int or count < 0 for count in row.values()) for row in matrix.values())
            or matrix != actual.action_confusion
            or day.get("action_match_rate") != actual.action_match_rate or day.get("universe_jaccard") != actual.universe_jaccard
            or day.get("p0_unexplained_count") != actual.p0_unexplained_count or day.get("p1_hold_noaction_flip") != actual.p1_hold_noaction_flip):
            return [f"{td} 的对比统计与原始执行记录不一致"]
        count_fields = ("universe_union_count", "universe_intersection_count", "chain_a_checked_count", "chain_b_checked_count",
                        "valid_symbol_days", "action_denominator", "action_match_count", "sample_status", "metric_status")
        if any(day.get(key) != getattr(actual, key) for key in count_fields) or actual.sample_status != "VALID":
            return [f"{td} 分母、有效样本或检查完整性不足/不一致"]
        if summary["g5_eligible_for_g6"] and (sum(matrix[a][b] for a in ACTION_TYPES for b in ACTION_TYPES if a != b)
                or actual.universe_added or actual.universe_removed):
            return [f"{td} 仍有待审核的实际差异，当前策略不允许仅凭归因标签通过"]
        manifest.append({"trade_date": td.isoformat(), "chain_a_sha256": a_hash, "chain_b_sha256": b_hash,
                         "input_manifest_sha256": a_meta["input_manifest_sha256"]})
    expected_hash = _fingerprint({"portfolio_id": summary["portfolio_id"], "strategy_snapshot_id": latest_snapshot.id,
                                  "applicable_scope": summary["applicable_scope"], "days": manifest})
    if provenance.get("source_manifest_sha256") != expected_hash:
        return ["输入清单的校验值与真实执行记录不一致"]
    daily = summary["daily_reports"]
    if (summary["avg_action_match_rate"] != sum(row["action_match_rate"] for row in daily) / len(daily)
        or summary["avg_universe_jaccard"] != sum(row["universe_jaccard"] for row in daily) / len(daily)
        or summary["total_p0_unexplained"] != sum(row["p0_unexplained_count"] for row in daily)
        or summary["total_p1_hold_noaction_flip"] != sum(row["p1_hold_noaction_flip"] for row in daily)):
        return ["报告汇总与逐日实际执行结果不一致"]
    from app.services.g5_dual_run_replay import get_g5_acceptance_policy
    if summary.get("valid_symbol_days") != sum(day["valid_symbol_days"] for day in daily) or summary["valid_symbol_days"] < get_g5_acceptance_policy()["min_symbol_days"]:
        return ["实际有效标的日样本不足，或汇总与逐日分母不一致"]
    return []


def _g5_provenance_errors(summary: dict[str, Any]) -> list[str]:
    """Return audit gaps that prevent a G5 export from unlocking G6."""
    errors: list[str] = []
    provenance = summary.get("provenance")
    if not isinstance(provenance, dict):
        return ["缺少 G5 来源证明"]
    capture_mode = provenance.get("capture_mode")
    if capture_mode not in {"real_dry_run_export", "historical_replay"}:
        errors.append("G5 来源必须是 real_dry_run_export 或 historical_replay")
    if provenance.get("trading_calendar") == "G5_VIRTUAL_WEEKDAY":
        errors.append("报告使用近似工作日日历，不能作为真实执行证据")
    if capture_mode == "historical_replay":
        if provenance.get("simulated") is not True:
            errors.append("historical_replay 必须标记 simulated=true")
        if provenance.get("real_dry_run") is not False:
            errors.append("historical_replay 必须标记 real_dry_run=false")
    for field in ("exported_at", "trading_calendar", "source_manifest_sha256"):
        if not isinstance(provenance.get(field), str) or not provenance[field].strip():
            errors.append(f"G5 来源证明缺少 {field}")
    exported_at = provenance.get("exported_at")
    if isinstance(exported_at, str) and exported_at.strip():
        try:
            datetime.fromisoformat(exported_at.replace("Z", "+00:00"))
        except ValueError:
            errors.append("G5 来源证明 exported_at 不是 ISO 时间")

    expected_dates = provenance.get("expected_trade_dates")
    daily_reports = summary.get("daily_reports")
    expected_set: set[str] = set()
    if not isinstance(expected_dates, list) or not expected_dates:
        errors.append("G5 来源证明缺少 expected_trade_dates")
    else:
        expected_set = {str(value) for value in expected_dates}
        if len(expected_set) != len(expected_dates):
            errors.append("G5 expected_trade_dates 存在重复日期")
    if not isinstance(daily_reports, list):
        errors.append("G5 daily_reports 非法")
        daily_reports = []
    if not daily_reports:
        errors.append("报告没有逐日执行记录")
    actual_dates = {str(item.get("trade_date")) for item in daily_reports if isinstance(item, dict)}
    skipped_set = {str(value) for value in (summary.get("skipped_days") or [])}
    if expected_set:
        if actual_dates & skipped_set:
            errors.append("G5 已回放日与 skipped_days 重复")
        if actual_dates | skipped_set != expected_set:
            errors.append("G5 已回放日、跳过日与预期交易日清单不一致")
        if len(skipped_set) != len(summary.get("skipped_days") or []):
            errors.append("G5 skipped_days 存在重复日期")
    if int(summary.get("days_replayed") or 0) != len(actual_dates):
        errors.append("G5 days_replayed 与每日报告数量不一致")

    for item in daily_reports:
        if not isinstance(item, dict):
            errors.append("G5 每日报告包含非法记录")
            continue
        for name, expected_mode in (("chain_a_meta", "legacy_dry_run"), ("chain_b_meta", "unified_dry_run")):
            meta = item.get(name)
            if not isinstance(meta, dict):
                errors.append(f"G5 {item.get('trade_date')} 缺少 {name}")
                continue
            if meta.get("capture_mode") != expected_mode:
                errors.append(f"G5 {item.get('trade_date')} {name} 不是 {expected_mode}")
            for field in ("source_run_id", "data_cutoff_at"):
                if not isinstance(meta.get(field), str) or not meta[field].strip():
                    errors.append(f"G5 {item.get('trade_date')} {name} 缺少 {field}")
            if str(meta.get("source_run_id", "")).startswith("g5-virtual-"):
                errors.append("报告引用了虚拟执行编号")
    return errors


def validate_g5_report(db: Session, record: G5DualRunReport, *, portfolio_id: int,
                       check_invalidation: bool = True, expected_scope: dict | None = None,
                       current_read: bool = False) -> dict[str, Any]:
    with db.no_autoflush, _read_mode(db, current_read):
        if current_read and record.id is not None:
            refreshed = _get(db, G5DualRunReport, record.id)
            if refreshed is None:
                return {"evidence_verified": False, "g5_eligible_for_g6": False, "status": "INVALID",
                        "g5_validation_errors": ["当前报告记录已不存在"], "storage_errors": []}
            record = refreshed
        return _validate_g5_report(db, record, portfolio_id=portfolio_id,
                                  check_invalidation=check_invalidation, expected_scope=expected_scope)


def _validate_g5_report(db: Session, record: G5DualRunReport, *, portfolio_id: int,
                        check_invalidation: bool, expected_scope: dict | None) -> dict[str, Any]:
    """Recheck the immutable original; never repair or update a report on read."""
    errors: list[str] = []
    storage_errors: list[dict] = []
    try:
        payload = json.loads(record.report_json)
        if not isinstance(payload, dict):
            raise ValueError("report must be an object")
        errors.extend(_g5_provenance_errors(payload))
        if hashlib.sha256(_canonical_json(payload).encode()).hexdigest() != record.report_hash:
            errors.append("报告内容与保存时的校验值不一致")
        if record.portfolio_id != portfolio_id or payload.get("portfolio_id") != portfolio_id:
            errors.append("报告不属于当前组合")
        if payload.get("status") != "COMPLETED":
            errors.append("报告尚未完成，或本次执行未成功")
        from app.services.g5_dual_run_replay import get_g5_acceptance_policy
        policy = get_g5_acceptance_policy()
        if payload.get("acceptance_policy_version") != policy["version"] or payload.get("acceptance_policy") != policy:
            errors.append("验收策略缺失、已变化或阈值被修改，请重新预检并生成新报告")
        if payload.get("evidence_scope") != policy["evidence_scope"]:
            errors.append("报告未声明当前支持的证据范围，不能冒充完整双链验收")
        if payload.get("g5_eligible_for_g6") is True and not policy["admission_supported"]:
            errors.append("当前策略仅支持决策动作归档核验，不支持正式引擎链准入")
        if expected_scope is not None and payload.get("applicable_scope") != expected_scope:
            errors.append("报告不适用于本次申请的方案与切换范围")
        errors.extend(_task_errors(db, payload))
        fields = {"start_date": str(record.start_date), "end_date": str(record.end_date),
                  "total_days": record.total_days, "days_replayed": record.days_replayed,
                  "total_p0_unexplained": record.p0_unexplained_count,
                  "total_p1_hold_noaction_flip": record.p1_hold_noaction_flip_count}
        if any(payload.get(key) != value for key, value in fields.items()):
            errors.append("报告内容与保存的日期或统计不一致")
        if type(payload.get("g5_eligible_for_g6")) is not bool or record.eligible_for_g6 != int(payload.get("g5_eligible_for_g6") is True):
            errors.append("报告的通过标记不一致或格式不正确")
        if payload.get("g5_validation_errors"):
            errors.append("报告保存时已发现证据缺失")
        if record.days_replayed < policy["min_trade_days"] or record.skipped_day_count or payload.get("skipped_days"):
            errors.append("不足十个完整交易日，或存在未完成的日期")
        if (payload.get("total_days") != payload.get("days_replayed") or len(payload.get("daily_reports") or []) != record.days_replayed
            or record.skipped_day_count != len(payload.get("skipped_days") or [])):
            errors.append("报告的完整日期数量不一致")
        if not errors:
            errors.extend(_execution_evidence_errors(db, payload))
        if payload.get("g5_eligible_for_g6") is True and (record.p0_unexplained_count or record.p1_hold_noaction_flip_count):
            errors.append("仍有未解释差异，不能标记通过")
        if check_invalidation:
            try:
                from app.services.g5_admission_governance import report_revocation_reason
                invalidation = report_revocation_reason(db, record, current_read=bool(db.info.get(_CURRENT_READ_KEY)))
                if invalidation:
                    errors.append(str(invalidation))
                    if str(invalidation).startswith("G5_GOVERNANCE_STORAGE_UNAVAILABLE"):
                        storage_errors.append({"code": "G5_GOVERNANCE_STORAGE_UNAVAILABLE", "detail": "准入隔离存储不可读取"})
            except ImportError:
                errors.append("准入隔离存储不可用，尚不能确认报告有效性")
                storage_errors.append({"code": "G5_GOVERNANCE_STORAGE_UNAVAILABLE", "detail": "准入隔离能力尚未部署"})
    except SQLAlchemyError:
        payload = {}
        errors.append("真实来源或准入隔离存储读取失败，不能确认报告有效性")
        storage_errors.append({"code": "G5_SOURCE_STORAGE_UNAVAILABLE", "detail": "报告来源存储读取失败"})
    except (TypeError, ValueError, KeyError, AttributeError):
        payload = {}
        errors.append("报告内容不完整或已损坏")
    payload["audit_report_id"] = record.id
    payload["audit_report_hash"] = record.report_hash
    payload["evidence_verified"] = not errors
    payload["g5_validation_errors"] = list(dict.fromkeys(errors))
    payload["storage_errors"] = storage_errors
    payload["evidence_scope"] = "decision_actions_only"
    payload["acceptance_blocker_codes"] = ["DEPLOYMENT_RUNNER_PROOF", "OPENING_ACCOUNT_SNAPSHOT",
        "ISOLATED_ORDER_PLAN", "RISK_INPUT_SNAPSHOT", "FACTOR_DEPENDENCY_SNAPSHOT"]
    payload["acceptance_blockers"] = ["缺少当前部署产物与真实执行器的可解析证明",
        "缺少双侧相同且可核验的期初现金、持仓与未决义务快照",
        "缺少隔离订单计划及目标仓位的完整比较证据",
        "缺少双侧风控输入、约束与参数的冻结证明",
        "缺少模型权重与因子原始依赖的完整冻结证明"]
    payload["job_status"] = payload.get("status") if payload.get("status") in {"SUBMITTED", "RUNNING", "COMPLETED", "FAILED", "CANCELLED"} else "INVALID"
    payload["verdict"] = "INVALID" if errors else "EVIDENCE_VERIFIED_NOT_ADMISSIBLE"
    payload["g5_eligible_for_g6"] = False  # Current supported evidence is explicitly partial.
    if errors:
        payload["status"] = "INVALID"
    return payload


def persist_g5_summary(db: Session, summary: dict[str, Any]) -> G5DualRunReport:
    """Persist one immutable G5 summary, fail-closed for G6 eligibility."""
    summary = json.loads(json.dumps(summary, ensure_ascii=False, default=str))
    rendered = _canonical_json(summary)
    report_hash = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
    skipped_days = list(summary.get("skipped_days") or [])
    record = G5DualRunReport(
        portfolio_id=int(summary["portfolio_id"]),
        start_date=date.fromisoformat(str(summary["start_date"])),
        end_date=date.fromisoformat(str(summary["end_date"])),
        total_days=int(summary.get("total_days") or 0),
        days_replayed=int(summary.get("days_replayed") or 0),
        skipped_day_count=len(skipped_days),
        p0_unexplained_count=int(summary.get("total_p0_unexplained") or 0),
        p1_hold_noaction_flip_count=int(summary.get("total_p1_hold_noaction_flip") or 0),
        eligible_for_g6=1 if bool(summary.get("g5_eligible_for_g6")) else 0,
        report_hash=report_hash,
        report_json=rendered,
    )
    checked = validate_g5_report(db, record, portfolio_id=record.portfolio_id)
    if not checked["evidence_verified"]:
        summary["g5_eligible_for_g6"] = False
        summary["g5_validation_errors"] = checked["g5_validation_errors"]
        record.eligible_for_g6 = 0
        record.report_json = _canonical_json(summary)
        record.report_hash = _fingerprint(summary)
    existing = db.execute(select(G5DualRunReport).where(G5DualRunReport.report_hash == record.report_hash)).scalar_one_or_none()
    if existing is not None:
        return existing
    db.add(record)
    db.flush()
    return record


def latest_g5_summary(db: Session, *, portfolio_id: int, current_read: bool = False) -> dict[str, Any] | None:
    """Return the newest report payload for a portfolio, or ``None``."""
    with db.no_autoflush, _read_mode(db, current_read):
        record = _execute_select(db,
        select(G5DualRunReport)
        .where(G5DualRunReport.portfolio_id == int(portfolio_id))
        .order_by(G5DualRunReport.created_at.desc(), G5DualRunReport.id.desc())
        .limit(1)
        ).scalar_one_or_none()
    if record is None:
        return None
    return validate_g5_report(db, record, portfolio_id=int(portfolio_id), current_read=current_read)
