from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import Settings

pytestmark = pytest.mark.whitebox


def test_factor_feature_defaults_are_safe(monkeypatch):
    for key in (
        "FACTOR_FEATURE_ENABLED",
        "FACTOR_WEIGHT_MODE",
        "FACTOR_WAREHOUSE_PATH",
        "WXPUSHER_ENABLED",
        "WXPUSHER_APP_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)

    cfg = Settings()

    assert cfg.factor_feature_enabled is False
    assert cfg.factor_weight_mode == "manual"
    assert cfg.factor_warehouse_path.name == "factor_warehouse.duckdb"
    assert cfg.wxpusher_enabled is False
    assert cfg.wxpusher_app_token == ""


def test_factor_feature_environment_overrides(monkeypatch, tmp_path):
    warehouse_path = tmp_path / "custom.duckdb"
    monkeypatch.setenv("FACTOR_FEATURE_ENABLED", "true")
    monkeypatch.setenv("FACTOR_WEIGHT_MODE", "shadow")
    monkeypatch.setenv("FACTOR_WAREHOUSE_PATH", str(warehouse_path))
    monkeypatch.setenv("WXPUSHER_ENABLED", "1")
    monkeypatch.setenv("WXPUSHER_APP_TOKEN", "secret")

    cfg = Settings()

    assert cfg.factor_feature_enabled is True
    assert cfg.factor_weight_mode == "shadow"
    assert cfg.factor_warehouse_path == Path(warehouse_path)
    assert cfg.wxpusher_enabled is True
    assert cfg.wxpusher_app_token == "secret"


def test_invalid_factor_weight_mode_falls_back_to_manual(monkeypatch):
    monkeypatch.setenv("FACTOR_WEIGHT_MODE", "unknown")
    assert Settings().factor_weight_mode == "manual"

