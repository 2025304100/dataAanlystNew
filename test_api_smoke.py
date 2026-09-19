import urllib.request
import urllib.error
import json

BASE = "http://localhost:8000"
PORTFOLIO_ID = 2
SYMBOL_ID = 1

endpoints = [
    ("GET", f"/health", None),
    ("GET", f"/api/v1/portfolios", None),
    ("GET", f"/api/v1/dashboard/overview?portfolio_id={PORTFOLIO_ID}", None),
    ("GET", f"/api/v1/dashboard/workbench?portfolio_id={PORTFOLIO_ID}&market_group=all", None),
    ("GET", f"/api/v1/dashboard/symbol-detail?portfolio_id={PORTFOLIO_ID}&symbol_id={SYMBOL_ID}", None),
    ("GET", f"/api/v1/symbols?page=1&page_size=10", None),
    ("GET", f"/api/v1/watchlists", None),
    ("GET", f"/api/v1/discovery/tasks", None),
    ("GET", f"/api/v1/news/latest?portfolio_id={PORTFOLIO_ID}&limit=5", None),
    ("GET", f"/api/v1/macro/overview?region=cn", None),
    ("GET", f"/api/v1/macro/indicators/cpi/history?region=cn&limit=5", None),
]

print("API Smoke Test")
print("=" * 60)

failures = []
for method, path, body in endpoints:
    url = BASE + path
    try:
        req = urllib.request.Request(url, method=method)
        if body:
            req.add_header("Content-Type", "application/json")
            req.data = json.dumps(body).encode()
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read().decode()[:80]
            print(f"OK   {method} {path} -> {resp.status} {data}...")
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:200]
        print(f"FAIL {method} {path} -> {e.code} {detail}")
        failures.append((path, e.code, detail))
    except Exception as e:
        print(f"ERR  {method} {path} -> {type(e).__name__}: {e}")
        failures.append((path, None, str(e)))

print("=" * 60)
if failures:
    print(f"Failures: {len(failures)}")
    for path, code, detail in failures:
        print(f"  {path}: {code} {detail}")
else:
    print("All endpoints passed.")
