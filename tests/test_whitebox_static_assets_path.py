"""静态资源路由的路径穿越防护。

回归点（生产准入 §4.4 组合交易 R1）：`/assets/{asset_path:path}` 曾把
`asset_path` 直接拼到 `dist_root/assets` 并 `exists()`/`FileResponse`，
`..%2f..` 一类请求可越界读取 assets 目录外的文件。

先红后绿：先复现越界可读，再验证修复后拒绝。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.requests import Request

import app.main as main_mod

pytestmark = pytest.mark.whitebox


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/assets/probe",
            "headers": [],
            "query_string": b"",
        }
    )


@pytest.fixture
def dist_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """临时 dist 目录；assets 子目录存在，并在其外放一个哨兵文件。"""
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "assets" / "app.js").write_text("ok", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("SECRET", encoding="utf-8")
    monkeypatch.setattr(main_mod, "dist_root", dist)
    # 测试环境不做 dev 代理
    monkeypatch.setattr(main_mod, "_proxy_frontend_dev", lambda _request: None)
    return dist


def test_asset_inside_assets_root_is_served(dist_root: Path):
    response = main_mod.frontend_assets("app.js", _request())
    assert response.status_code == 200


@pytest.mark.parametrize(
    "asset_path",
    ["../secret.txt", "..%2fsecret.txt", "../../secret.txt", "sub/../../secret.txt"],
)
def test_asset_traversal_outside_assets_root_is_rejected(dist_root: Path, asset_path: str):
    with pytest.raises(HTTPException) as exc:
        main_mod.frontend_assets(asset_path, _request())
    assert exc.value.status_code == 404
