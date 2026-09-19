from dataclasses import FrozenInstanceError

import pytest

from app.services.backtest_filters.config import (
    BacktestFilterConfig,
    compute_config_hash,
    validate_production_fidelity,
)


def test_default_values():
    cfg = BacktestFilterConfig()
    assert cfg.filter_new_listing is True
    assert cfg.min_listing_age_calendar_days == 120
    assert cfg.filter_st is True
    assert cfg.filter_suspended is True
    assert cfg.force_delisting_liquidation is True
    assert cfg.delisting_period_excluded is True
    assert cfg.min_history_days == 20
    assert cfg.production_fidelity is True
    assert cfg.diagnostic_only is False


def test_frozen_immutable():
    cfg = BacktestFilterConfig()
    with pytest.raises(FrozenInstanceError):
        cfg.filter_st = False


def test_validate_st_disabled():
    cfg = BacktestFilterConfig(filter_st=False)
    ok, reason = validate_production_fidelity(cfg)
    assert ok is False
    assert reason == "filter_st disabled"


def test_validate_multiple_disabled():
    cfg = BacktestFilterConfig(filter_st=False, filter_new_listing=False)
    ok, reason = validate_production_fidelity(cfg)
    assert ok is False
    # 顺序按 CORE_RULE_FIELDS：filter_new_listing -> filter_st
    assert reason == "filter_new_listing disabled; filter_st disabled"


def test_validate_passed_default():
    cfg = BacktestFilterConfig()
    ok, reason = validate_production_fidelity(cfg)
    assert ok is True
    assert reason == ""


def test_hash_identical_for_same_values():
    cfg1 = BacktestFilterConfig()
    cfg2 = BacktestFilterConfig()
    h1 = compute_config_hash(cfg1)
    h2 = compute_config_hash(cfg2)
    assert h1 == h2
    assert len(h1) == 64
    # hex chars
    int(h1, 16)


def test_hash_changes_on_value_change():
    cfg1 = BacktestFilterConfig(min_listing_age_calendar_days=120)
    cfg2 = BacktestFilterConfig(min_listing_age_calendar_days=121)
    h1 = compute_config_hash(cfg1)
    h2 = compute_config_hash(cfg2)
    assert h1 != h2


def test_hash_excludes_diagnostic_only():
    cfg_a = BacktestFilterConfig(diagnostic_only=False)
    cfg_b = BacktestFilterConfig(diagnostic_only=True)
    ha = compute_config_hash(cfg_a)
    hb = compute_config_hash(cfg_b)
    assert ha == hb
