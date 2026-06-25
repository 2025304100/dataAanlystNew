import json
import os
import sys
import uuid
from pathlib import Path

from fastapi.testclient import TestClient


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> None:
    db_name = f"qa_backend_{uuid.uuid4().hex}.db"
    db_path = Path("tmp") / db_name
    db_path.parent.mkdir(parents=True, exist_ok=True)
    os.environ["DATABASE_URL"] = f"sqlite:///./{db_path.as_posix()}"

    from app.main import app

    results: dict[str, object] = {"database_url": os.environ["DATABASE_URL"]}
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
