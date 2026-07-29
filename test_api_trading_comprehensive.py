"""
Comprehensive Black-Box API Test Script (v2 - fixed)
Modules: Trading, Backtest, Alerts, Auto-Trade, Signal Rules, Custom Indicators, Sim Accounts
Uses only urllib (no curl, no requests).
"""

import urllib.request
import urllib.error
import json
import time
import traceback

BASE = "http://127.0.0.1:8000/api/v1"
OUTPUT_FILE = r"D:\ai_project\dataAanlystNew\test_results_api_trading.txt"

results = []
test_count = 0
pass_count = 0
fail_count = 0
error_count = 0

# Known test data (verified existing)
PORTFOLIO_ID = 1
SYMBOL_ID = 3276       # 贵州茅台
SYMBOL_ID_2 = 36

# Valid alert types: task_failed, indicator_trigger, data_stale, score_drop, watchlist_signal
# Valid value_type for custom indicators: boolean, number


def api_call(method, path, body=None, params=None):
    """Make an HTTP request and return (status_code, parsed_body, raw_text)."""
    url = BASE + path
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url += "?" + qs

    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")

    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")

    try:
        resp = urllib.request.urlopen(req, timeout=60)
        raw = resp.read().decode("utf-8", errors="replace")
        status = resp.status
        try:
            body_parsed = json.loads(raw)
        except:
            body_parsed = raw
        return status, body_parsed, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace") if e.fp else ""
        try:
            body_parsed = json.loads(raw)
        except:
            body_parsed = raw
        return e.code, body_parsed, raw
    except Exception as e:
        return 0, {"error": str(e)}, str(e)


def record(module, test_name, method, path, request_info, status, response, notes=""):
    """Record a single test result."""
    global test_count, pass_count, fail_count, error_count
    test_count += 1

    # Determine verdict
    if status == 0:
        verdict = "ERROR"
        error_count += 1
    elif 200 <= status < 300:
        verdict = "PASS"
        pass_count += 1
    elif 400 <= status < 500:
        verdict = "PASS (expected client error)"
        pass_count += 1
    elif status >= 500:
        verdict = "FAIL (server error)"
        fail_count += 1
    else:
        verdict = "UNKNOWN"

    # Truncate response for readability
    resp_str = json.dumps(response, ensure_ascii=False, default=str) if not isinstance(response, str) else response
    if len(resp_str) > 600:
        resp_str = resp_str[:600] + "... [TRUNCATED]"

    req_str = json.dumps(request_info, ensure_ascii=False, default=str) if not isinstance(request_info, str) else request_info

    results.append({
        "module": module,
        "test": test_name,
        "method": method,
        "path": path,
        "request": req_str,
        "status": status,
        "response": resp_str,
        "verdict": verdict,
        "notes": notes,
    })


def do(module, test_name, method, path, body=None, params=None, request_info=None):
    """Call API and record result in one step."""
    status, resp, raw = api_call(method, path, body=body, params=params)
    if request_info is None:
        request_info = {}
        if body:
            request_info["body"] = body
        if params:
            request_info["params"] = params
    record(module, test_name, method, path, request_info, status, resp)
    return resp, status


# ============================================================================
# MODULE 1: TRADE SETUPS
# ============================================================================
def test_trade_setups():
    mod = "1. Trade Setups"

    # 1a. GET latest trade setup for a valid symbol
    do(mod, "GET latest trade setup (valid symbol & portfolio)",
       "GET", f"/trade-setups/latest/{SYMBOL_ID}",
       params={"portfolio_id": str(PORTFOLIO_ID)},
       request_info={"symbol_id": SYMBOL_ID, "portfolio_id": PORTFOLIO_ID})

    # 1b. GET latest trade setup - missing required query param
    do(mod, "GET latest trade setup (missing portfolio_id)",
       "GET", f"/trade-setups/latest/{SYMBOL_ID}",
       request_info={"symbol_id": SYMBOL_ID, "portfolio_id": "<missing>"})

    # 1c. GET latest trade setup - nonexistent symbol
    do(mod, "GET latest trade setup (nonexistent symbol_id=999999)",
       "GET", "/trade-setups/latest/999999",
       params={"portfolio_id": str(PORTFOLIO_ID)},
       request_info={"symbol_id": 999999, "portfolio_id": PORTFOLIO_ID})

    # 1d. POST generate trade setup - valid
    do(mod, "POST generate trade setup (valid)",
       "POST", "/trade-setups/generate",
       body={"portfolio_id": PORTFOLIO_ID, "symbol_id": SYMBOL_ID})

    # 1e. POST generate trade setup - missing required fields
    do(mod, "POST generate trade setup (empty body)",
       "POST", "/trade-setups/generate",
       body={})

    # 1f. POST generate trade setup - invalid symbol
    do(mod, "POST generate trade setup (invalid symbol_id=999999)",
       "POST", "/trade-setups/generate",
       body={"portfolio_id": PORTFOLIO_ID, "symbol_id": 999999})

    # 1g. PATCH tranches - nonexistent setup_id
    do(mod, "PATCH tranches (nonexistent setup_id=999999)",
       "PATCH", "/trade-setups/999999/tranches",
       body={"tranche_plan": []})


# ============================================================================
# MODULE 2: SIM ACCOUNTS
# ============================================================================
def test_sim_accounts():
    mod = "2. Sim Accounts"

    # 2a. GET sim account for valid portfolio
    do(mod, "GET sim account (valid portfolio)",
       "GET", f"/portfolios/{PORTFOLIO_ID}/sim-account")

    # 2b. GET sim account with query params
    do(mod, "GET sim account (with trade_limit & ledger_limit)",
       "GET", f"/portfolios/{PORTFOLIO_ID}/sim-account",
       params={"trade_limit": "10", "ledger_limit": "20"},
       request_info={"portfolio_id": PORTFOLIO_ID, "trade_limit": 10, "ledger_limit": 20})

    # 2c. GET sim account - nonexistent portfolio
    do(mod, "GET sim account (nonexistent portfolio_id=999999)",
       "GET", "/portfolios/999999/sim-account")

    # 2d. POST sim order - valid buy
    do(mod, "POST sim order (valid buy)",
       "POST", f"/portfolios/{PORTFOLIO_ID}/sim-orders",
       body={"symbol_id": SYMBOL_ID, "side": "buy", "quantity": 100})

    # 2e. POST sim order - missing required fields
    do(mod, "POST sim order (empty body)",
       "POST", f"/portfolios/{PORTFOLIO_ID}/sim-orders",
       body={})

    # 2f. POST sim order - invalid side
    do(mod, "POST sim order (invalid side='fly')",
       "POST", f"/portfolios/{PORTFOLIO_ID}/sim-orders",
       body={"symbol_id": SYMBOL_ID, "side": "fly", "quantity": 100})

    # 2g. POST sim order - zero quantity
    do(mod, "POST sim order (zero quantity)",
       "POST", f"/portfolios/{PORTFOLIO_ID}/sim-orders",
       body={"symbol_id": SYMBOL_ID, "side": "buy", "quantity": 0})

    # 2h. POST sim order - negative quantity
    do(mod, "POST sim order (negative quantity)",
       "POST", f"/portfolios/{PORTFOLIO_ID}/sim-orders",
       body={"symbol_id": SYMBOL_ID, "side": "buy", "quantity": -10})

    # 2i. POST sim order - nonexistent portfolio
    do(mod, "POST sim order (nonexistent portfolio_id=999999)",
       "POST", "/portfolios/999999/sim-orders",
       body={"symbol_id": SYMBOL_ID, "side": "buy", "quantity": 100})


# ============================================================================
# MODULE 3: BACKTEST
# ============================================================================
def test_backtest():
    mod = "3. Backtest"

    # 3a. GET backtest runs (valid)
    do(mod, "GET backtest runs (valid portfolio)",
       "GET", "/backtest/runs",
       params={"portfolio_id": str(PORTFOLIO_ID)},
       request_info={"portfolio_id": PORTFOLIO_ID})

    # 3b. GET backtest runs - missing required param
    do(mod, "GET backtest runs (missing portfolio_id)",
       "GET", "/backtest/runs",
       request_info={"portfolio_id": "<missing>"})

    # 3c. GET backtest runs - nonexistent portfolio
    do(mod, "GET backtest runs (nonexistent portfolio_id=999999)",
       "GET", "/backtest/runs",
       params={"portfolio_id": "999999"},
       request_info={"portfolio_id": 999999})

    # 3d. POST backtest run - valid config
    do(mod, "POST backtest run (valid config)",
       "POST", "/backtest/run",
       body={
           "portfolio_id": PORTFOLIO_ID,
           "symbol_ids": [SYMBOL_ID],
           "start_date": "2024-01-01",
           "end_date": "2024-12-31",
           "rule_config": {"buy_conditions": {"quality_score_min": 60}, "sell_conditions": {"take_profit_pct": 0.1}}
       })

    # 3e. POST backtest run - empty body
    do(mod, "POST backtest run (empty body)",
       "POST", "/backtest/run",
       body={})

    # 3f. POST backtest run - missing symbol_ids
    do(mod, "POST backtest run (missing symbol_ids)",
       "POST", "/backtest/run",
       body={
           "portfolio_id": PORTFOLIO_ID,
           "start_date": "2024-01-01",
           "end_date": "2024-12-31",
           "rule_config": {}
       })

    # 3g. POST backtest run - invalid dates
    do(mod, "POST backtest run (invalid date format)",
       "POST", "/backtest/run",
       body={
           "portfolio_id": PORTFOLIO_ID,
           "symbol_ids": [SYMBOL_ID],
           "start_date": "not-a-date",
           "end_date": "also-not",
           "rule_config": {}
       })

    # 3h. POST backtest run - reversed dates
    do(mod, "POST backtest run (end_date before start_date)",
       "POST", "/backtest/run",
       body={
           "portfolio_id": PORTFOLIO_ID,
           "symbol_ids": [SYMBOL_ID],
           "start_date": "2025-12-31",
           "end_date": "2024-01-01",
           "rule_config": {}
       })

    # 3i. GET backtest templates
    do(mod, "GET backtest templates",
       "GET", "/backtest/templates")

    # 3j. POST backtest template - valid
    do(mod, "POST backtest template (valid)",
       "POST", "/backtest/templates",
       body={"name": "API Test Template", "description": "Test template", "rule_config": {"buy_conditions": {}}})

    # 3k. POST backtest template - missing name
    do(mod, "POST backtest template (missing name)",
       "POST", "/backtest/templates",
       body={"description": "No name provided", "rule_config": {}})

    # 3l. GET specific backtest run (nonexistent)
    do(mod, "GET backtest run (nonexistent run_id=999999)",
       "GET", "/backtest/runs/999999")

    # 3m. POST portfolio backtest run - valid
    do(mod, "POST portfolio backtest run (valid)",
       "POST", "/backtest/portfolio/run",
       body={
           "portfolio_id": PORTFOLIO_ID,
           "start_date": "2024-06-01",
           "end_date": "2024-12-31"
       })

    # 3n. POST portfolio backtest run - missing fields
    do(mod, "POST portfolio backtest run (empty body)",
       "POST", "/backtest/portfolio/run",
       body={})


# ============================================================================
# MODULE 4: ALERTS
# ============================================================================
def test_alerts():
    mod = "4. Alerts"
    created_rule_id = None

    # 4a. GET alert events
    do(mod, "GET alert events",
       "GET", "/alerts/events")

    # 4b. GET alert events with params
    do(mod, "GET alert events (limit=5, include_acknowledged=true)",
       "GET", "/alerts/events",
       params={"limit": "5", "include_acknowledged": "true"},
       request_info={"limit": 5, "include_acknowledged": True})

    # 4c. GET active alerts
    do(mod, "GET active alerts",
       "GET", "/alerts/active")

    # 4d. GET active alerts with limit
    do(mod, "GET active alerts (limit=3)",
       "GET", "/alerts/active",
       params={"limit": "3"},
       request_info={"limit": 3})

    # 4e. GET alert rules
    do(mod, "GET alert rules",
       "GET", "/alerts/rules")

    # 4f. POST alert rule - valid (use valid alert_type: score_drop)
    resp, status = do(mod, "POST alert rule (valid - score_drop)",
       "POST", "/alerts/rules",
       body={
           "name": "API Test Alert Rule",
           "alert_type": "score_drop",
           "enabled": True,
           "severity": "warn",
           "config": {"threshold": 40},
           "cooldown_minutes": 60
       })

    if isinstance(resp, dict) and "id" in resp:
        created_rule_id = resp["id"]

    # 4g. POST alert rule - missing required fields
    do(mod, "POST alert rule (missing name & alert_type)",
       "POST", "/alerts/rules",
       body={"enabled": True})

    # 4h. POST alert rule - empty body
    do(mod, "POST alert rule (empty body)",
       "POST", "/alerts/rules",
       body={})

    # 4i. POST alert rule - invalid alert_type
    do(mod, "POST alert rule (invalid alert_type='price_change')",
       "POST", "/alerts/rules",
       body={"name": "Bad Type Alert", "alert_type": "price_change"})

    # 4j. POST alert rule - valid with watchlist_signal type
    resp2, status2 = do(mod, "POST alert rule (valid - watchlist_signal)",
       "POST", "/alerts/rules",
       body={
           "name": "API Test Watchlist Signal",
           "alert_type": "watchlist_signal",
           "enabled": False,
           "severity": "info",
           "config": {},
           "cooldown_minutes": 30
       })

    rule_id_2 = None
    if isinstance(resp2, dict) and "id" in resp2:
        rule_id_2 = resp2["id"]

    # 4k. PATCH alert rule - valid update
    if created_rule_id:
        do(mod, f"PATCH alert rule (valid update, id={created_rule_id})",
           "PATCH", f"/alerts/rules/{created_rule_id}",
           body={"name": "Updated API Test Rule", "severity": "error"})
    else:
        do(mod, "PATCH alert rule (guessed id=1)",
           "PATCH", "/alerts/rules/1",
           body={"name": "Updated API Test Rule"})

    # 4l. PATCH alert rule - nonexistent
    do(mod, "PATCH alert rule (nonexistent id=999999)",
       "PATCH", "/alerts/rules/999999",
       body={"name": "Ghost Rule"})

    # 4m. POST evaluate alerts
    do(mod, "POST evaluate alerts",
       "POST", "/alerts/evaluate",
       body={})

    # 4n. POST acknowledge all
    do(mod, "POST acknowledge all alerts",
       "POST", "/alerts/acknowledge-all",
       body={})

    # 4o. POST acknowledge specific event (nonexistent)
    do(mod, "POST acknowledge event (nonexistent event_id=999999)",
       "POST", "/alerts/acknowledge/999999",
       body={})

    # 4p. GET alert context (nonexistent)
    do(mod, "GET alert context (nonexistent alert_id=999999)",
       "GET", "/alerts/999999/context")

    # 4q. DELETE alert rule (cleanup created rules)
    if created_rule_id:
        do(mod, f"DELETE alert rule (cleanup, id={created_rule_id})",
           "DELETE", f"/alerts/rules/{created_rule_id}")
    if rule_id_2:
        do(mod, f"DELETE alert rule (cleanup, id={rule_id_2})",
           "DELETE", f"/alerts/rules/{rule_id_2}")

    # 4r. DELETE alert rule - nonexistent
    do(mod, "DELETE alert rule (nonexistent id=999999)",
       "DELETE", "/alerts/rules/999999")


# ============================================================================
# MODULE 5: AUTO TRADE
# ============================================================================
def test_auto_trade():
    mod = "5. Auto Trade"

    # 5a. GET member status
    do(mod, "GET auto-trade member status (valid portfolio)",
       "GET", f"/portfolios/{PORTFOLIO_ID}/auto-trade/member-status")

    # 5b. GET member status - nonexistent portfolio
    do(mod, "GET auto-trade member status (nonexistent portfolio)",
       "GET", "/portfolios/999999/auto-trade/member-status")

    # 5c. GET member source status
    do(mod, "GET auto-trade member source status (valid portfolio)",
       "GET", f"/portfolios/{PORTFOLIO_ID}/auto-trade/member-source-status")

    # 5d. GET dry-run diff
    do(mod, "GET auto-trade dry-run diff (valid portfolio)",
       "GET", f"/portfolios/{PORTFOLIO_ID}/auto-trade/dry-run-diff")

    # 5e. POST execute auto-trade (dry_run=true)
    do(mod, "POST auto-trade execute (dry_run=true)",
       "POST", f"/portfolios/{PORTFOLIO_ID}/auto-trade/execute",
       body={"dry_run": True, "buy_candidate_limit": 5})

    # 5f. POST execute auto-trade (empty body)
    do(mod, "POST auto-trade execute (empty body)",
       "POST", f"/portfolios/{PORTFOLIO_ID}/auto-trade/execute",
       body={})

    # 5g. POST execute auto-trade - nonexistent portfolio
    do(mod, "POST auto-trade execute (nonexistent portfolio)",
       "POST", "/portfolios/999999/auto-trade/execute",
       body={"dry_run": True})

    # 5h. GET backtest source status
    do(mod, "GET backtest source status (valid portfolio)",
       "GET", f"/portfolios/{PORTFOLIO_ID}/backtest/source-status")

    # 5i. POST rollback
    do(mod, "POST auto-trade rollback (valid portfolio)",
       "POST", f"/portfolios/{PORTFOLIO_ID}/auto-trade/rollback-to-old-source",
       body={})


# ============================================================================
# MODULE 6: SIGNAL RULES
# ============================================================================
def test_signal_rules():
    mod = "6. Signal Rules"

    # 6a. GET signal rule presets
    do(mod, "GET signal rule presets",
       "GET", "/signal-rules/presets")

    # 6b. GET signal rule stats for valid symbol
    do(mod, f"GET signal rule stats (valid symbol_id={SYMBOL_ID})",
       "GET", f"/signal-rules/stats/{SYMBOL_ID}")

    # 6c. GET signal rule stats - nonexistent symbol
    do(mod, "GET signal rule stats (nonexistent symbol_id=999999)",
       "GET", "/signal-rules/stats/999999")

    # 6d. GET signal rule stats with portfolio_id
    do(mod, f"GET signal rule stats (with portfolio_id={PORTFOLIO_ID})",
       "GET", f"/signal-rules/stats/{SYMBOL_ID}",
       params={"portfolio_id": str(PORTFOLIO_ID)},
       request_info={"symbol_id": SYMBOL_ID, "portfolio_id": PORTFOLIO_ID})

    # 6e. GET portfolio signal rule
    do(mod, "GET portfolio signal rule (valid portfolio)",
       "GET", f"/portfolios/{PORTFOLIO_ID}/signal-rule")

    # 6f. POST portfolio signal rule - valid upsert
    do(mod, "POST portfolio signal rule (valid upsert)",
       "POST", f"/portfolios/{PORTFOLIO_ID}/signal-rule",
       body={
           "rule_name": "API Test Signal Rule",
           "mode": "conservative",
           "quality_tolerance": 0.8,
           "timing_tolerance": 24.0,
           "min_sample_count": 10,
           "max_samples": 100
       })

    # 6g. POST portfolio signal rule - empty body
    do(mod, "POST portfolio signal rule (empty body)",
       "POST", f"/portfolios/{PORTFOLIO_ID}/signal-rule",
       body={})

    # 6h. POST portfolio signal rule - invalid portfolio
    do(mod, "POST portfolio signal rule (nonexistent portfolio)",
       "POST", "/portfolios/999999/signal-rule",
       body={"rule_name": "Ghost Rule", "mode": "aggressive"})

    # 6i. POST signal rule preview
    do(mod, "POST signal rule preview (valid)",
       "POST", f"/portfolios/{PORTFOLIO_ID}/signal-rule/preview",
       body={
           "rule_name": "Preview Test",
           "mode": "conservative",
           "quality_tolerance": 0.5,
           "symbol_id": SYMBOL_ID
       })

    # 6j. POST signal rule preview - empty body
    do(mod, "POST signal rule preview (empty body)",
       "POST", f"/portfolios/{PORTFOLIO_ID}/signal-rule/preview",
       body={})


# ============================================================================
# MODULE 7: CUSTOM INDICATORS
# ============================================================================
def test_custom_indicators():
    mod = "7. Custom Indicators"
    created_indicator_id = None

    # 7a. GET custom indicators (all)
    do(mod, "GET custom indicators (all)",
       "GET", "/settings/custom-indicators")

    # 7b. GET custom indicators with filter
    do(mod, "GET custom indicators (enabled=true)",
       "GET", "/settings/custom-indicators",
       params={"enabled": "true"},
       request_info={"enabled": True})

    # 7c. POST custom indicator - valid formula (value_type must be 'boolean' or 'number')
    resp, status = do(mod, "POST custom indicator (valid formula)",
       "POST", "/settings/custom-indicators",
       body={
           "name": "API Test Indicator",
           "key": "api_test_ind_v3",
           "description": "Test indicator created by API test",
           "formula": "close > open ? 1 : 0",
           "value_type": "number",
           "category": "test",
           "enabled": True,
           "change_note": "Initial creation by API test"
       })

    if isinstance(resp, dict) and "id" in resp:
        created_indicator_id = resp["id"]

    # 7d. POST custom indicator - missing required fields
    do(mod, "POST custom indicator (missing name, key, formula)",
       "POST", "/settings/custom-indicators",
       body={"description": "Missing required fields"})

    # 7e. POST custom indicator - empty body
    do(mod, "POST custom indicator (empty body)",
       "POST", "/settings/custom-indicators",
       body={})

    # 7f. POST custom indicator - invalid value_type
    do(mod, "POST custom indicator (invalid value_type='integer')",
       "POST", "/settings/custom-indicators",
       body={
           "name": "Bad Type Indicator",
           "key": "bad_type_ind",
           "formula": "close * 2",
           "value_type": "integer",
           "change_note": "Testing invalid value_type"
       })

    # 7g. POST custom indicator - duplicate key
    do(mod, "POST custom indicator (duplicate key)",
       "POST", "/settings/custom-indicators",
       body={
           "name": "Duplicate Key Indicator",
           "key": "api_test_ind_v3",
           "formula": "close * 2",
           "change_note": "Testing duplicate key"
       })

    # 7h. POST custom indicator preview - valid (requires symbol_id)
    do(mod, "POST custom indicator preview (valid)",
       "POST", "/settings/custom-indicators/preview",
       body={
           "symbol_id": SYMBOL_ID,
           "formula": "close > open ? 1 : 0",
           "value_type": "number",
           "recent_count": 30
       })

    # 7i. POST custom indicator preview - missing symbol_id
    do(mod, "POST custom indicator preview (missing symbol_id)",
       "POST", "/settings/custom-indicators/preview",
       body={
           "formula": "close > open ? 1 : 0"
       })

    # 7j. PUT custom indicator - valid update
    if created_indicator_id:
        do(mod, f"PUT custom indicator (valid update, id={created_indicator_id})",
           "PUT", f"/settings/custom-indicators/{created_indicator_id}",
           body={
               "name": "Updated API Test Indicator",
               "description": "Updated description",
               "change_note": "Updated by API test"
           })
    else:
        do(mod, "PUT custom indicator (guessed id=1)",
           "PUT", "/settings/custom-indicators/1",
           body={"name": "Updated", "change_note": "test"})

    # 7k. PUT custom indicator - nonexistent
    do(mod, "PUT custom indicator (nonexistent id=999999)",
       "PUT", "/settings/custom-indicators/999999",
       body={"name": "Ghost Indicator"})

    # 7l. GET custom indicator versions
    if created_indicator_id:
        do(mod, f"GET custom indicator versions (id={created_indicator_id})",
           "GET", f"/settings/custom-indicators/{created_indicator_id}/versions")

    # 7m. DELETE custom indicator (cleanup)
    if created_indicator_id:
        do(mod, f"DELETE custom indicator (cleanup, id={created_indicator_id})",
           "DELETE", f"/settings/custom-indicators/{created_indicator_id}")
    else:
        do(mod, "DELETE custom indicator (nonexistent id=999999)",
           "DELETE", "/settings/custom-indicators/999999")


# ============================================================================
# OUTPUT
# ============================================================================
def write_results():
    lines = []
    lines.append("=" * 100)
    lines.append("COMPREHENSIVE BLACK-BOX API TEST RESULTS")
    lines.append(f"Target: {BASE}")
    lines.append(f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("=" * 100)
    lines.append("")

    # Summary
    lines.append("-" * 60)
    lines.append("SUMMARY")
    lines.append("-" * 60)
    lines.append(f"Total tests:  {test_count}")
    lines.append(f"Passed (2xx success):       {sum(1 for r in results if 200 <= r['status'] < 300)}")
    lines.append(f"Passed (4xx expected err):  {sum(1 for r in results if 400 <= r['status'] < 500)}")
    lines.append(f"Failed (5xx server error):  {sum(1 for r in results if r['status'] >= 500)}")
    lines.append(f"Errors (connection fail):   {sum(1 for r in results if r['status'] == 0)}")
    lines.append(f"Pass rate:    {(test_count - fail_count - error_count)/test_count*100:.1f}%" if test_count else "N/A")
    lines.append("")

    # Group by module
    current_module = None
    for r in results:
        if r["module"] != current_module:
            current_module = r["module"]
            lines.append("")
            lines.append("=" * 100)
            lines.append(f"MODULE: {current_module}")
            lines.append("=" * 100)

        lines.append("")
        lines.append(f"  Test:       {r['test']}")
        lines.append(f"  Method:     {r['method']}")
        lines.append(f"  Endpoint:   {r['path']}")
        lines.append(f"  Request:    {r['request']}")
        lines.append(f"  Status:     {r['status']}")
        lines.append(f"  Verdict:    {r['verdict']}")

        resp_display = r["response"]
        if len(resp_display) > 400:
            resp_display = resp_display[:400] + "... [TRUNCATED]"
        lines.append(f"  Response:   {resp_display}")

        if r["notes"]:
            lines.append(f"  Notes:      {r['notes']}")

        lines.append(f"  {'-' * 80}")

    # Anomalies section
    lines.append("")
    lines.append("=" * 100)
    lines.append("ANOMALIES & ISSUES")
    lines.append("=" * 100)

    # 5xx errors
    server_errors = [r for r in results if r["status"] >= 500]
    if server_errors:
        lines.append("")
        lines.append("SERVER ERRORS (5xx):")
        for a in server_errors:
            lines.append(f"  [{a['method']} {a['path']}] Status={a['status']} - {a['test']}")
            resp_short = a['response'][:200]
            lines.append(f"    Response: {resp_short}")
    else:
        lines.append("")
        lines.append("  No 5xx server errors detected.")

    # Unexpected behaviors
    lines.append("")
    lines.append("UNEXPECTED BEHAVIORS:")
    unexpected = []
    for r in results:
        # GET requests returning 500
        if r["method"] == "GET" and r["status"] >= 500:
            unexpected.append(f"  GET {r['path']} returned {r['status']} (server error on read operation)")
        # Missing param tests returning 200 (should be 422/400)
        if "missing" in r["test"].lower() and r["status"] == 200:
            unexpected.append(f"  {r['method']} {r['path']} returned 200 despite missing required params (should be 422)")
        # Empty body POST returning 200
        if "empty body" in r["test"].lower() and r["status"] == 200:
            unexpected.append(f"  POST {r['path']} returned 200 with empty body (should validate required fields)")
        # Nonexistent resource returning 200 instead of 404
        if "nonexistent" in r["test"].lower() and r["method"] in ("GET", "DELETE") and r["status"] == 200:
            unexpected.append(f"  {r['method']} {r['path']} returned 200 for nonexistent resource (should be 404)")

    if unexpected:
        for u in unexpected:
            lines.append(u)
    else:
        lines.append("  No unexpected behaviors detected.")

    # Status code distribution
    lines.append("")
    lines.append("STATUS CODE DISTRIBUTION:")
    status_counts = {}
    for r in results:
        s = r["status"]
        status_counts[s] = status_counts.get(s, 0) + 1
    for code in sorted(status_counts.keys()):
        lines.append(f"  {code}: {status_counts[code]} tests")

    lines.append("")
    lines.append("=" * 100)
    lines.append("END OF REPORT")
    lines.append("=" * 100)

    report = "\n".join(lines)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(report)

    print(report)
    print(f"\n\nResults written to: {OUTPUT_FILE}")


# ============================================================================
# MAIN
# ============================================================================
if __name__ == "__main__":
    print("=" * 60)
    print("Comprehensive Black-Box API Tests v2")
    print(f"Target: {BASE}")
    print(f"Output: {OUTPUT_FILE}")
    print("=" * 60)
    print()

    modules = [
        ("1/7", "Trade Setups", test_trade_setups),
        ("2/7", "Sim Accounts", test_sim_accounts),
        ("3/7", "Backtest", test_backtest),
        ("4/7", "Alerts", test_alerts),
        ("5/7", "Auto Trade", test_auto_trade),
        ("6/7", "Signal Rules", test_signal_rules),
        ("7/7", "Custom Indicators", test_custom_indicators),
    ]

    for num, name, func in modules:
        print(f"[{num}] Testing {name}...")
        try:
            func()
        except Exception as e:
            print(f"  FATAL ERROR in {name}: {e}")
            traceback.print_exc()
            record(name, f"FATAL ERROR in {name}", "N/A", "N/A", str(e), 0, str(e))

    print("\nWriting results...")
    write_results()
    print("Done!")
