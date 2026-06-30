import json
import os
from pathlib import Path

from fastapi.testclient import TestClient


def main() -> None:
    db_path = Path("tmp/qa_backend_smoke.db")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    os.environ["DATABASE_URL"] = "sqlite:///./tmp/qa_backend_smoke.db"

    from app.main import app

    results: dict[str, object] = {}
    with TestClient(app) as client:
        health = client.get("/health")
        openapi = client.get("/openapi.json")
        root = client.get("/")
        workbench = client.get("/workbench")
        api_health = client.get("/api/v1/system/data-health")

        results["health"] = {
            "status_code": health.status_code,
            "json": health.json(),
        }
        openapi_json = openapi.json()
        results["openapi"] = {
            "status_code": openapi.status_code,
            "path_count": len(openapi_json.get("paths", {})),
        }
        results["root"] = {
            "status_code": root.status_code,
            "content_type": root.headers.get("content-type"),
        }
        results["workbench"] = {
            "status_code": workbench.status_code,
            "content_type": workbench.headers.get("content-type"),
        }
        results["system_data_health"] = {
            "status_code": api_health.status_code,
            "json": api_health.json(),
        }

    print(json.dumps(results, ensure_ascii=False, default=str, indent=2))


if __name__ == "__main__":
    main()
