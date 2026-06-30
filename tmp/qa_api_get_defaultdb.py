import json
import sys
from pathlib import Path

from fastapi.testclient import TestClient


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def has_required_parameters(operation: dict) -> bool:
    for parameter in operation.get("parameters", []):
        if parameter.get("required"):
            return True
    request_body = operation.get("requestBody") or {}
    return bool(request_body.get("required"))


def main() -> None:
    from app.main import app

    report: list[dict[str, object]] = []
    with TestClient(app) as client:
        openapi = client.get("/openapi.json").json()
        for path, path_item in sorted(openapi.get("paths", {}).items()):
            get_op = path_item.get("get")
            if not get_op or "{" in path or has_required_parameters(get_op):
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
