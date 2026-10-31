"""Read-only source facts for interface management; declarations are not admission."""
from __future__ import annotations
from functools import lru_cache
from pathlib import Path
from datetime import datetime, timezone
import os

from app.services.data_source_contracts import contract_catalog


@lru_cache(maxsize=1)
def declarations():
    return {item["endpoint"]: item for item in contract_catalog()}


def context():
    from app.services.data_source_control import RequestController, PolicyDenied
    path = os.environ.get("DATA_SOURCE_CONTROL_DB")
    if not path:
        return None
    if not Path(path).is_file():
        raise PolicyDenied("CONTROL_STORE_UNAVAILABLE")
    return RequestController(path).management_snapshot()


def metadata(key, *, registered=False, snapshot=None):
    item = declarations().get(key)
    if item is None:
        raise ValueError("unknown endpoint")
    implemented = item["implementation_status"] == "implemented"
    in_release = item["in_current_release"]
    allowed = in_release and implemented
    result = {
        "access_status": item["access_status"], "in_current_release": in_release,
        "implementation_status": "implemented" if implemented else "not_implemented",
        "auth_required": item["auth_required"], "auth_status": item["auth_status"],
        "admission_status": "not_reviewed", "production_enabled": False,
        "production_request_allowed": False, "permission_proof_id": None,
        "supplier_quota_status": None if key == "TDX:day" else "unknown", "supplier_quota_remaining": None,
        "network_metrics_applicable": key != "TDX:day",
        "contract_status": item["contract_status"], "profile_ids": item["profile_ids"],
        "unit_fields": [{k: field.get(k) for k in ("wire_field", "canonical_target", "native_unit_claim", "native_unit_verified")}
                        for field in item["field_mappings"]],
        "can_edit": allowed and registered, "can_probe": allowed and registered,
        "control_mode": "legacy_process_cache", "health_status": "unobserved",
        "block_reasons": ["IMPLEMENTATION_NOT_VALIDATED", "ADMISSION_NOT_REVIEWED"] + ([] if in_release else ["OUT_OF_RELEASE"]),
    }
    if snapshot is not None:
        ep = snapshot["policies"].get("endpoint:" + key)
        provider = snapshot["policies"].get("provider:" + ep["provider"]) if ep else None
        configured = bool(ep and provider)
        binding_reasons = []
        if ep:
            from app.services.data_source_control import validate_upstream_scope, PolicyDenied
            try:
                validate_upstream_scope(ep['allowed_hosts'])
            except PolicyDenied as exc:
                binding_reasons.append(str(exc))
        result['block_reasons'] += binding_reasons
        enabled = bool(configured and allowed and not binding_reasons and ep["policy"]["enabled"] and provider["policy"]["enabled"])
        result.update(control_mode="shared", can_edit=allowed and configured, can_probe=allowed and registered and enabled,
                      effective_enabled=enabled, config_version={"endpoint": ep["version"], "provider": provider["version"]} if configured else None,
                      control={"provider_id": ep["provider"], "endpoint_policy": ep["policy"], "provider_policy": provider["policy"],
                               "transport": ep["transport"]} if configured else None,
                      statistics=snapshot["statistics"].get(key, {"by_kind": {"call": {}, "probe": {}}, "latest": {}}))
        runtime = [snapshot["state"].get(k, {}) for k in ("endpoint:" + key, "provider:" + ep["provider"] if ep else "")]
        until = max((s.get("open_until", 0) for s in runtime), default=0)
        result["cooldown_remaining_s"] = max(0, until - snapshot["observed_at"])
        if until > snapshot["observed_at"]:
            result["health_status"] = "cooldown"
        elif any(s.get("lease") and s.get("lease_until", 0) > snapshot["observed_at"] for s in runtime):
            result["health_status"] = "half_open"
        else:
            latest = result["statistics"]["latest"]
            records = [r for r in latest.values() if r["status"] in {"success", "empty_result", "supplier_error"}]
            if records:
                last = max(records, key=lambda r: r["finished_at"] or r["started_at"])
                result["health_status"] = "observed_failure" if last["status"] == "supplier_error" else "observed_success"
        if not configured:
            result["block_reasons"].append("CONTROL_NOT_REGISTERED")
        publication = snapshot.get('admissions', {}).get(key)
        if publication:
            from app.services.data_source_admission import decision
            record = publication['record']
            reasons = decision(key, publication, snapshot['observed_at'])
            result.update(implementation_status=record['implementation_status'] if not reasons else result['implementation_status'],
                          admission_status=record['admission_status'], production_enabled=record['production_enabled'],
                          permission_proof_id=record['permission_proof_id'], admission_version=publication['version'],
                          production_request_allowed=not reasons and enabled and result['health_status'] not in {'cooldown','half_open'},
                          block_reasons=reasons + binding_reasons + ([] if enabled else ['DISABLED_OR_UNCONFIGURED']))
    return result


def project_status(status, cfg):
    g = status.governance
    if not g["network_metrics_applicable"]:
        status.last_probe_latency_ms = None
    g["legacy_statistics"] = {"available": cfg is not None, "total_calls": cfg.total_calls or 0 if cfg else None,
                              "total_failures": cfg.total_failures or 0 if cfg else None}
    if g["control_mode"] != "shared":
        g["can_probe"] = g["can_probe"] and status.enabled
        return status
    status.enabled = g["effective_enabled"]
    control = g.get("control")
    if control:
        status.anti_risk_strategy = "shared"
        status.delay_min_ms = status.delay_max_ms = int(max(control["endpoint_policy"]["min_interval_s"], control["provider_policy"]["min_interval_s"]) * 1000)
    stats = g["statistics"]
    status.total_calls = stats["by_kind"]["call"].get("logical_calls", 0)
    status.total_failures = stats["by_kind"]["call"].get("supplier_error", 0)
    for kind in ("call", "probe"):
        record = stats["latest"].get(kind)
        setattr(status, "last_" + kind + "_at", datetime.fromtimestamp(record["started_at"], timezone.utc) if record else None)
        setattr(status, "last_" + kind + "_success", record["status"] == "success" if record and record["status"] != "running" else None)
        setattr(status, "last_" + kind + "_error", record.get("error_code") if record else None)
        if kind == "probe":
            status.last_probe_latency_ms = max(0, int((record["finished_at"] - record["started_at"]) * 1000)) if record and record["finished_at"] is not None else None
    return status


def catalog_entry(item):
    key = item["endpoint"]
    names = {"PLAN:Fuyao": "同花顺 Fuyao REST", "PLAN:iFinD": "同花顺 iFinD QuantAPI",
             "PLAN:BSE": "北交所官网", "TDX:day": "TDX 本地日线"}
    name = names.get(key, f"{item['provider_claim']} · {item['dataset']}")
    return {"key": key, "name_zh": name, "name_en": name, "category_zh": item["dataset"],
            "category_en": item["dataset"], "module": item["provider_claim"],
            "desc_zh": "来源目录登记；实现、认证与生产准入独立核验。", "desc_en": "Catalog declaration; implementation, authentication and admission are independent.",
            "default_strategy": "standard"}
