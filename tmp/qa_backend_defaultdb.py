import json
import sys
from pathlib import Path

from fastapi.testclient import TestClient


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> None:
    from app.main import app

    results: dict[str, object] = {}
    with TestClient(app) as client:
        health = client.get("/health")
        openapi = client.get("/openapi.json")
        api_health = client.get("/api/v1/system/data-health")
        results["health"] = {"status_code": health.status_code, "json": health.json()}
        results["openapi"] = {
            "status_code": openapi.status_code,
            "path_count": len(openapi.json().get("paths", {})),
        }
        results["system_data_health"] = {
            "status_code": api_health.status_code,
            "json": api_health.json(),
        }

    print(json.dumps(results, ensure_ascii=False, default=str, indent=2))


if __name__ == "__main__":
    main()
