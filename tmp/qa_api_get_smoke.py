import json
import os
from pathlib import Path

from fastapi.testclient import TestClient


def has_required_parameters(operation: dict) -> bool:
    for parameter in operation.get("parameters", []):
        if parameter.get("required"):
            return True
    request_body = operation.get("requestBody") or {}
    if request_body.get("required"):
        return True
    return False


def main() -> None:
    db_path = Path("tmp/qa_api_get_smoke.db")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    os.environ["DATABASE_URL"] = "sqlite:///./tmp/qa_api_get_smoke.db"

    from app.main import app

    report: list[dict[str, object]] = []
    with TestClient(app) as client:
        openapi = client.get("/openapi.json").json()
        for path, path_item in sorted(openapi.get("paths", {}).items()):
            get_op = path_item.get("get")
            if not get_op:
                continue
            if "{" in path:
                continue
            if has_required_parameters(get_op):
                continue
            response = client.get(path)
            report.append(
                {
                    "path": path,
                    "status_code": response.status_code,
                    "content_type": response.headers.get("content-type"),
                }
            )

    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
