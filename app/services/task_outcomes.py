"""Additive result semantics without changing persisted task/retry states."""
from __future__ import annotations


def external_sync_result(task, result: dict | None) -> dict | None:
    if not task.task_type.startswith("external_sync_"):
        return result
    if task.status not in {"done", "failed", "cancelled"}:
        return result
    summary = dict(result or {})
    success = int(summary.get("success", task.ok_count) or 0)
    failed = int(summary.get("failed", task.failed_count) or 0)
    # A restart can leave a durable checkpoint without a result_json.
    skipped = int(summary.get("skipped", max(0, (task.processed or 0) - success - failed)) or 0)
    total = int(summary.get("total", task.total) or 0)
    mirror_warning = (summary.get("warehouse_mirror") or {}).get("status") == "warning"
    quality_warning = (summary.get("quality_snapshot") or {}).get("status") == "warning"
    no_source_work = not success and not int(summary.get("records", 0) or 0)
    skip_reason = summary.get("skip_reason")
    if task.status == "cancelled":
        outcome = "cancelled"
    elif failed or mirror_warning or quality_warning or task.status == "failed":
        outcome = "partial_success" if success or int(summary.get("records", 0) or 0) else "failed"
    elif no_source_work and not summary.get("mirror_retry") and skip_reason == "data_unavailable":
        outcome = "failed"
    elif no_source_work and not summary.get("mirror_retry") and skip_reason in {
        "market_closed", "already_current", "no_new_data", "no_events",
    }:
        # Preflight skips have no work units, so total/skipped can both be zero.
        outcome = "skipped"
    elif skipped and not success:
        outcome = "skipped"
    else:
        outcome = "success"
    summary.update(outcome=outcome, success=success, skipped=skipped, failed=failed,
                   skip_ratio=skipped / total if total > 0 else 0.0)
    return summary
