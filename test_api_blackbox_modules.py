#!/usr/bin/env python3
"""
Comprehensive Black-Box API Test Script
Tests: Discovery, Macro, News, AI, Notifications, Factors,
       Scheduled Tasks, Settings, External Data, and Linkage modules.
Uses only urllib (no curl, no requests).
"""

import urllib.request
import urllib.error
import json
import time
import sys
from datetime import datetime

BASE_URL = "http://127.0.0.1:8000/api/v1"
OUTPUT_FILE = r"D:\ai_project\dataAanlystNew\test_results_api_discovery.txt"

results = []
test_count = 0
pass_count = 0
fail_count = 0
error_count = 0


def log(msg):
    """Print and collect a log line."""
    print(msg)


def do_request(method, path, body=None, params=None, timeout=15):
    """
    Make an HTTP request and return (status_code, response_body_dict_or_str, raw_bytes).
    """
    url = BASE_URL + path
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url += "?" + qs

    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")

    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())

    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        status = resp.status
        raw = resp.read()
        try:
            body_parsed = json.loads(raw.decode("utf-8"))
        except Exception:
            body_parsed = raw.decode("utf-8", errors="replace")
        return status, body_parsed, raw
    except urllib.error.HTTPError as e:
        status = e.code
        raw = e.read()
        try:
            body_parsed = json.loads(raw.decode("utf-8"))
        except Exception:
            body_parsed = raw.decode("utf-8", errors="replace")
        return status, body_parsed, raw
    except urllib.error.URLError as e:
        return 0, {"error": str(e.reason)}, b""
    except Exception as e:
        return 0, {"error": str(e)}, b""


def summarize_response(body, max_len=500):
    """Create a compact summary of a response body."""
    if isinstance(body, dict):
        # Show keys and types
        parts = []
        for k, v in body.items():
            if isinstance(v, list):
                parts.append(f"{k}: list[{len(v)}]")
            elif isinstance(v, dict):
                parts.append(f"{k}: dict{{{','.join(list(v.keys())[:5])}}}")
            elif isinstance(v, str) and len(v) > 80:
                parts.append(f"{k}: '{v[:80]}...'")
            else:
                parts.append(f"{k}: {v}")
        summary = ", ".join(parts)
        if len(summary) > max_len:
            summary = summary[:max_len] + "..."
        return summary
    elif isinstance(body, str):
        if len(body) > max_len:
            return body[:max_len] + "..."
        return body
    else:
        return str(body)[:max_len]


def record_test(module, method, endpoint, params_or_body, status, body, notes=""):
    """Record a single test result."""
    global test_count, pass_count, fail_count, error_count
    test_count += 1

    is_success = 200 <= status < 300
    is_404 = status == 404
    is_422 = status == 422
    is_5xx = status >= 500
    is_conn_err = status == 0

    if is_success:
        pass_count += 1
        verdict = "PASS"
    elif is_404:
        fail_count += 1
        verdict = "FAIL (404 Not Found)"
    elif is_422:
        fail_count += 1
        verdict = "FAIL (422 Validation Error)"
    elif is_5xx:
        error_count += 1
        verdict = "ERROR (5xx Server Error)"
    elif is_conn_err:
        error_count += 1
        verdict = "ERROR (Connection Failed)"
    else:
        fail_count += 1
        verdict = f"FAIL (HTTP {status})"

    summary = summarize_response(body)

    entry = {
        "module": module,
        "method": method,
        "endpoint": endpoint,
        "params_or_body": params_or_body,
        "status": status,
        "verdict": verdict,
        "summary": summary,
        "notes": notes,
    }
    results.append(entry)

    icon = "OK" if is_success else "XX"
    log(f"  [{icon}] {method} {endpoint} -> {status} | {verdict}")
    if not is_success:
        log(f"       Summary: {summary[:200]}")
    if notes:
        log(f"       Notes: {notes}")


def section(title):
    log(f"\n{'='*70}")
    log(f"  {title}")
    log(f"{'='*70}")


# =========================================================================
#  TEST DEFINITIONS
# =========================================================================

def test_health():
    """Pre-check: health endpoint."""
    section("0. HEALTH CHECK (Pre-requisite)")
    # Health is at root /health, not under /api/v1/
    try:
        r = urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=5)
        status = r.status
        body = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        status = e.code
        try:
            body = json.loads(e.read().decode())
        except Exception:
            body = {"error": f"HTTP {e.code}"}
    except Exception as e:
        status = 0
        body = {"error": str(e)}
    record_test("Health", "GET", "/health (root)", None, status, body)
    return 200 <= status < 300


def test_discovery():
    """Module 1: Discovery / Opportunity"""
    section("1. DISCOVERY / OPPORTUNITY")

    # 1a. GET discovery tasks
    status, body, _ = do_request("GET", "/discovery/tasks")
    record_test("Discovery", "GET", "/discovery/tasks", None, status, body)

    # 1b. GET discovery candidates
    status, body, _ = do_request("GET", "/discovery/candidates")
    record_test("Discovery", "GET", "/discovery/candidates", None, status, body)

    # 1c. GET discovery latest-candidates
    status, body, _ = do_request("GET", "/discovery/latest-candidates")
    record_test("Discovery", "GET", "/discovery/latest-candidates", None, status, body)

    # 1d. GET discovery excluded
    status, body, _ = do_request("GET", "/discovery/excluded")
    record_test("Discovery", "GET", "/discovery/excluded", None, status, body)

    # 1e. GET discovery scan-runs
    status, body, _ = do_request("GET", "/discovery/scan-runs")
    record_test("Discovery", "GET", "/discovery/scan-runs", None, status, body)

    # 1f. GET discovery snapshot/status
    status, body, _ = do_request("GET", "/discovery/snapshot/status")
    record_test("Discovery", "GET", "/discovery/snapshot/status", None, status, body)

    # 1g. POST discovery/tasks (start a task) - with minimal/empty body
    status, body, _ = do_request("POST", "/discovery/tasks", body={})
    record_test("Discovery", "POST", "/discovery/tasks", "{}", status, body,
                "Attempted to start discovery task with empty body")

    # 1h. POST discovery/fast-scan
    status, body, _ = do_request("POST", "/discovery/fast-scan", body={})
    record_test("Discovery", "POST", "/discovery/fast-scan", "{}", status, body)

    # 1i. POST discovery/data-prep
    status, body, _ = do_request("POST", "/discovery/data-prep", body={})
    record_test("Discovery", "POST", "/discovery/data-prep", "{}", status, body)

    # 1j. GET discovery-plans (under settings)
    status, body, _ = do_request("GET", "/settings/discovery-plans")
    record_test("Discovery", "GET", "/settings/discovery-plans", None, status, body,
                "Discovery plans are under /settings/")

    # 1k. GET scans/runs (under /scans/)
    status, body, _ = do_request("GET", "/scans/runs")
    record_test("Discovery", "GET", "/scans/runs", None, status, body)

    # 1l. GET scans/latest/executable
    status, body, _ = do_request("GET", "/scans/latest/executable")
    record_test("Discovery", "GET", "/scans/latest/executable", None, status, body)

    # 1m. GET discovery scope stats (with a sample scope)
    status, body, _ = do_request("GET", "/discovery/scopes/default/stats")
    record_test("Discovery", "GET", "/discovery/scopes/default/stats", None, status, body,
                "Testing with 'default' scope")

    # 1n. POST discovery/indicators/evaluate
    status, body, _ = do_request("POST", "/discovery/indicators/evaluate", body={})
    record_test("Discovery", "POST", "/discovery/indicators/evaluate", "{}", status, body)


def test_macro():
    """Module 2: Macro Data"""
    section("2. MACRO DATA")

    # 2a. GET macro overview
    status, body, _ = do_request("GET", "/macro/overview")
    record_test("Macro", "GET", "/macro/overview", None, status, body)

    # 2b. GET macro update-tasks/latest
    status, body, _ = do_request("GET", "/macro/update-tasks/latest")
    record_test("Macro", "GET", "/macro/update-tasks/latest", None, status, body)

    # 2c. POST macro/update
    status, body, _ = do_request("POST", "/macro/update", body={})
    record_test("Macro", "POST", "/macro/update", "{}", status, body,
                "Attempted macro data update")

    # 2d. POST macro/update-tasks
    status, body, _ = do_request("POST", "/macro/update-tasks", body={})
    record_test("Macro", "POST", "/macro/update-tasks", "{}", status, body)

    # 2e. GET macro indicator history (sample key)
    status, body, _ = do_request("GET", "/macro/indicators/gdp/history")
    record_test("Macro", "GET", "/macro/indicators/gdp/history", None, status, body,
                "Testing with 'gdp' indicator key")


def test_news():
    """Module 3: News & Market Events"""
    section("3. NEWS & MARKET EVENTS")

    # 3a. GET news/latest
    status, body, _ = do_request("GET", "/news/latest")
    record_test("News", "GET", "/news/latest", None, status, body)

    # 3b. POST news/update
    status, body, _ = do_request("POST", "/news/update", body={})
    record_test("News", "POST", "/news/update", "{}", status, body)

    # 3c. GET market-events
    status, body, _ = do_request("GET", "/market-events")
    record_test("MarketEvents", "GET", "/market-events", None, status, body)

    # 3d. GET market-events/scopes
    status, body, _ = do_request("GET", "/market-events/scopes")
    record_test("MarketEvents", "GET", "/market-events/scopes", None, status, body)

    # 3e. POST market-events (create)
    status, body, _ = do_request("POST", "/market-events", body={
        "title": "API Test Event",
        "event_type": "test",
        "impact": "neutral"
    })
    record_test("MarketEvents", "POST", "/market-events",
                '{"title":"API Test Event",...}', status, body,
                "Attempted to create a test market event")

    # 3f. POST market-events/collect
    status, body, _ = do_request("POST", "/market-events/collect", body={})
    record_test("MarketEvents", "POST", "/market-events/collect", "{}", status, body)


def test_ai():
    """Module 4: AI Config & Sessions"""
    section("4. AI CONFIG & SESSIONS")

    # 4a. GET settings/ai-config
    status, body, _ = do_request("GET", "/settings/ai-config")
    record_test("AI", "GET", "/settings/ai-config", None, status, body)

    # 4b. GET settings/ai-config/models
    status, body, _ = do_request("GET", "/settings/ai-config/models")
    record_test("AI", "GET", "/settings/ai-config/models", None, status, body)

    # 4c. GET ai/profiles
    status, body, _ = do_request("GET", "/ai/profiles")
    record_test("AI", "GET", "/ai/profiles", None, status, body)

    # 4d. GET ai/sessions
    status, body, _ = do_request("GET", "/ai/sessions")
    record_test("AI", "GET", "/ai/sessions", None, status, body)

    # 4e. GET ai/health
    status, body, _ = do_request("GET", "/ai/health")
    record_test("AI", "GET", "/ai/health", None, status, body)

    # 4f. POST ai/sessions (create a session)
    status, body, _ = do_request("POST", "/ai/sessions", body={
        "profile_id": "test-profile",
        "title": "API Test Session"
    })
    record_test("AI", "POST", "/ai/sessions",
                '{"profile_id":"test-profile",...}', status, body,
                "Attempted to create AI session")

    # 4g. POST settings/ai-config/test
    status, body, _ = do_request("POST", "/settings/ai-config/test", body={})
    record_test("AI", "POST", "/settings/ai-config/test", "{}", status, body)


def test_notifications():
    """Module 5: Notifications"""
    section("5. NOTIFICATIONS")

    # 5a. GET notifications/channels
    status, body, _ = do_request("GET", "/notifications/channels")
    record_test("Notifications", "GET", "/notifications/channels", None, status, body)

    # 5b. GET notifications/policies
    status, body, _ = do_request("GET", "/notifications/policies")
    record_test("Notifications", "GET", "/notifications/policies", None, status, body)

    # 5c. GET notifications/templates
    status, body, _ = do_request("GET", "/notifications/templates")
    record_test("Notifications", "GET", "/notifications/templates", None, status, body)

    # 5d. GET notifications/deliveries
    status, body, _ = do_request("GET", "/notifications/deliveries")
    record_test("Notifications", "GET", "/notifications/deliveries", None, status, body)

    # 5e. GET notifications/outbox
    status, body, _ = do_request("GET", "/notifications/outbox")
    record_test("Notifications", "GET", "/notifications/outbox", None, status, body)


def test_factors():
    """Module 6: Factors"""
    section("6. FACTORS")

    # 6a. GET factors
    status, body, _ = do_request("GET", "/factors")
    record_test("Factors", "GET", "/factors", None, status, body)

    # 6b. GET factor-models
    status, body, _ = do_request("GET", "/factor-models")
    record_test("Factors", "GET", "/factor-models", None, status, body)

    # 6c. GET factors/overview
    status, body, _ = do_request("GET", "/factors/overview")
    record_test("Factors", "GET", "/factors/overview", None, status, body)

    # 6d. GET factors/config
    status, body, _ = do_request("GET", "/factors/config")
    record_test("Factors", "GET", "/factors/config", None, status, body)

    # 6e. GET factor-models/latest
    status, body, _ = do_request("GET", "/factor-models/latest")
    record_test("Factors", "GET", "/factor-models/latest", None, status, body)

    # 6f. GET factor-models/runtime
    status, body, _ = do_request("GET", "/factor-models/runtime")
    record_test("Factors", "GET", "/factor-models/runtime", None, status, body)

    # 6g. GET factor-pipeline/tasks
    status, body, _ = do_request("GET", "/factor-pipeline/tasks")
    record_test("Factors", "GET", "/factor-pipeline/tasks", None, status, body)

    # 6h. GET factor-pipeline/eta
    status, body, _ = do_request("GET", "/factor-pipeline/eta")
    record_test("Factors", "GET", "/factor-pipeline/eta", None, status, body)


def test_scheduled_tasks():
    """Module 7: Scheduled Tasks"""
    section("7. SCHEDULED TASKS")

    # 7a. GET scheduled-tasks
    status, body, _ = do_request("GET", "/scheduled-tasks")
    record_test("ScheduledTasks", "GET", "/scheduled-tasks", None, status, body)

    # 7b. GET scheduled-tasks/definitions
    status, body, _ = do_request("GET", "/scheduled-tasks/definitions")
    record_test("ScheduledTasks", "GET", "/scheduled-tasks/definitions", None, status, body)

    # 7c. GET scheduled-tasks/runs
    status, body, _ = do_request("GET", "/scheduled-tasks/runs")
    record_test("ScheduledTasks", "GET", "/scheduled-tasks/runs", None, status, body)

    # 7d. POST scheduled-tasks (create)
    status, body, _ = do_request("POST", "/scheduled-tasks", body={
        "name": "api-test-task",
        "task_type": "test",
        "cron": "0 0 * * *"
    })
    record_test("ScheduledTasks", "POST", "/scheduled-tasks",
                '{"name":"api-test-task",...}', status, body,
                "Attempted to create scheduled task")


def test_settings():
    """Module 8: DB Config / Settings"""
    section("8. DB CONFIG / SETTINGS")

    # 8a. GET settings/db-config
    status, body, _ = do_request("GET", "/settings/db-config")
    record_test("Settings", "GET", "/settings/db-config", None, status, body)

    # 8b. POST settings/db-config/test
    status, body, _ = do_request("POST", "/settings/db-config/test", body={})
    record_test("Settings", "POST", "/settings/db-config/test", "{}", status, body)

    # 8c. GET settings/db-config/migration-status
    status, body, _ = do_request("GET", "/settings/db-config/migration-status")
    record_test("Settings", "GET", "/settings/db-config/migration-status", None, status, body)

    # 8d. GET settings/custom-indicators
    status, body, _ = do_request("GET", "/settings/custom-indicators")
    record_test("Settings", "GET", "/settings/custom-indicators", None, status, body)

    # 8e. GET settings/scoring-configs
    status, body, _ = do_request("GET", "/settings/scoring-configs")
    record_test("Settings", "GET", "/settings/scoring-configs", None, status, body)

    # 8f. GET settings/scoring-configs/active
    status, body, _ = do_request("GET", "/settings/scoring-configs/active")
    record_test("Settings", "GET", "/settings/scoring-configs/active", None, status, body)


def test_external_data():
    """Module 9: External Data"""
    section("9. EXTERNAL DATA")

    # 9a. GET external-data/apis
    status, body, _ = do_request("GET", "/external-data/apis")
    record_test("ExternalData", "GET", "/external-data/apis", None, status, body)

    # 9b. GET external-data/apis/strategies
    status, body, _ = do_request("GET", "/external-data/apis/strategies")
    record_test("ExternalData", "GET", "/external-data/apis/strategies", None, status, body)


def test_linkage_and_extras():
    """Module 10: Linkage (and related endpoints like alerts, journals, system)"""
    section("10. LINKAGE / ALERTS / SYSTEM / EXTRAS")

    # No dedicated /linkage endpoint exists. Test related modules.

    # 10a. Alerts
    status, body, _ = do_request("GET", "/alerts/rules")
    record_test("Alerts", "GET", "/alerts/rules", None, status, body)

    status, body, _ = do_request("GET", "/alerts/events")
    record_test("Alerts", "GET", "/alerts/events", None, status, body)

    status, body, _ = do_request("GET", "/alerts/active")
    record_test("Alerts", "GET", "/alerts/active", None, status, body)

    # 10b. Journals (trading journals / logs)
    status, body, _ = do_request("GET", "/journals")
    record_test("Journals", "GET", "/journals", None, status, body)

    # 10c. System
    status, body, _ = do_request("GET", "/system/capabilities")
    record_test("System", "GET", "/system/capabilities", None, status, body)

    status, body, _ = do_request("GET", "/system/data-health")
    record_test("System", "GET", "/system/data-health", None, status, body)

    status, body, _ = do_request("GET", "/system/tasks")
    record_test("System", "GET", "/system/tasks", None, status, body)

    # 10d. Dashboard overview (bonus)
    status, body, _ = do_request("GET", "/dashboard/overview")
    record_test("Dashboard", "GET", "/dashboard/overview", None, status, body)

    # 10e. Symbols (bonus)
    status, body, _ = do_request("GET", "/symbols")
    record_test("Symbols", "GET", "/symbols", None, status, body,
                "List symbols - basic connectivity check")

    # 10f. Watchlists (bonus)
    status, body, _ = do_request("GET", "/watchlists")
    record_test("Watchlists", "GET", "/watchlists", None, status, body)

    # 10g. Portfolios (bonus)
    status, body, _ = do_request("GET", "/portfolios")
    record_test("Portfolios", "GET", "/portfolios", None, status, body)


# =========================================================================
#  REPORT GENERATION
# =========================================================================

def write_report():
    """Write structured results to file."""
    lines = []
    lines.append("=" * 80)
    lines.append("  COMPREHENSIVE BLACK-BOX API TEST RESULTS")
    lines.append(f"  Target: {BASE_URL}")
    lines.append(f"  Date:   {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("=" * 80)
    lines.append("")
    lines.append(f"  TOTAL TESTS:  {test_count}")
    lines.append(f"  PASSED:       {pass_count}")
    lines.append(f"  FAILED:       {fail_count}")
    lines.append(f"  ERRORS:       {error_count}")
    lines.append(f"  PASS RATE:    {pass_count/test_count*100:.1f}%" if test_count > 0 else "  PASS RATE:    N/A")
    lines.append("")

    # Group by module
    modules_seen = []
    for r in results:
        if r["module"] not in modules_seen:
            modules_seen.append(r["module"])

    for mod in modules_seen:
        mod_results = [r for r in results if r["module"] == mod]
        mod_pass = sum(1 for r in mod_results if 200 <= r["status"] < 300)
        mod_total = len(mod_results)
        lines.append("-" * 70)
        lines.append(f"  MODULE: {mod}  ({mod_pass}/{mod_total} passed)")
        lines.append("-" * 70)

        for r in mod_results:
            lines.append(f"")
            lines.append(f"  Endpoint:    {r['method']} {r['endpoint']}")
            if r['params_or_body']:
                lines.append(f"  Request:     {r['params_or_body']}")
            lines.append(f"  HTTP Status: {r['status']}")
            lines.append(f"  Verdict:     {r['verdict']}")
            lines.append(f"  Response:    {r['summary'][:300]}")
            if r['notes']:
                lines.append(f"  Notes:       {r['notes']}")

    lines.append("")
    lines.append("=" * 80)
    lines.append("  ANOMALIES AND ISSUES")
    lines.append("=" * 80)

    anomalies = [r for r in results if not (200 <= r["status"] < 300)]
    if anomalies:
        for r in anomalies:
            lines.append(f"  - [{r['verdict']}] {r['method']} {r['endpoint']}")
            lines.append(f"    Status: {r['status']} | Summary: {r['summary'][:200]}")
            if r['notes']:
                lines.append(f"    Notes: {r['notes']}")
            lines.append("")
    else:
        lines.append("  No anomalies found - all endpoints returned 2xx.")

    lines.append("")
    lines.append("=" * 80)
    lines.append("  END OF REPORT")
    lines.append("=" * 80)

    report_text = "\n".join(lines)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(report_text)

    log(f"\n\nReport written to: {OUTPUT_FILE}")
    log(f"Total: {test_count} tests, {pass_count} passed, {fail_count} failed, {error_count} errors")
    return report_text


# =========================================================================
#  MAIN
# =========================================================================

def main():
    log("=" * 60)
    log("  Starting Comprehensive Black-Box API Tests")
    log(f"  Target: {BASE_URL}")
    log(f"  Time:   {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log("=" * 60)

    # Health check first
    alive = test_health()
    if not alive:
        log("\nFATAL: Server is not reachable. Aborting tests.")
        # Still write a report
        write_report()
        sys.exit(1)

    # Run all test modules
    test_discovery()
    test_macro()
    test_news()
    test_ai()
    test_notifications()
    test_factors()
    test_scheduled_tasks()
    test_settings()
    test_external_data()
    test_linkage_and_extras()

    # Write report
    write_report()


if __name__ == "__main__":
    main()
