"""Task 24: 30 天 BFG baseline 回放对账（pytest）。

加载 tests/fixtures/backtest_filter_30d_baseline.json，
逐日构造 SecurityStatusDTO → 调用 apply_daily_filters + process_delisting_liquidations
(无价格时跳过清算或用注入 resolver 模拟) → 逐日对账：
  - sha256_signature（trades_count + holdings_count + exclude_count_by_rule）100% 匹配
  - effective_count 与 fixture 记录一致
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from app.services.backtest_filters.config import BacktestFilterConfig, compute_config_hash
from app.services.backtest_filters.engine import apply_daily_filters
from app.services.backtest_filters.liquidation import (
    HoldingInfo,
    process_delisting_liquidations,
)
from app.services.backtest_filters.rules import (
    ACTION_INCLUDE,
    DelistingPriceMissingError,
    RULE_INCLUDE_NORMAL,
)
from app.services.security_status.pit_service import SecurityStatusDTO


FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "backtest_filter_30d_baseline.json"


# =====================================================================
# Fixture loader
# =====================================================================
def _load_baseline():
    raw = FIXTURE_PATH.read_text(encoding="utf-8")
    return json.loads(raw)


BASELINE = _load_baseline()


def _status_from_dict(sid: int, td: date, sd: dict) -> SecurityStatusDTO:
    ld = date.fromisoformat(sd["listing_date"]) if sd["listing_date"] else None
    dd = date.fromisoformat(sd["delisting_date"]) if sd["delisting_date"] else None
    return SecurityStatusDTO(
        symbol_id=sid,
        trade_date=td,
        status=sd["status"],  # type: ignore[arg-type]
        listing_age_calendar_days=sd["listing_age_calendar_days"],
        delisting_days_ago=(sd.get("delisting_days_ago")
                            if sd.get("delisting_days_ago") is not None
                            else ((td - dd).days if dd is not None else None)),
        raw_source="replay_fixture",
        listing_date=ld,
        delisting_date=dd,
        is_st=bool(sd["is_st"]),
        is_suspended=bool(sd["is_suspended"]),
        is_delisting_period=bool(sd["is_delisting_period"]),
        is_listed=bool(sd["is_listed"]),
    )


# =====================================================================
# Replay: verify each day
# =====================================================================
class TestBaseline30DReplay:
    def test_meta_symbols_count(self):
        """Fixture 中 exactly 10 symbols。"""
        assert BASELINE["meta"]["symbols"] == list(range(1, 11))

    def test_meta_30_days(self):
        assert len(BASELINE["days"]) == 30, (
            f"期望 30 天，实际 {len(BASELINE['days'])} 天"
        )

    def test_meta_config_hash_matches(self):
        """meta.config_hash compatible: schema_version=1 (old fixture) or schema_version=2 (new canonical) either passes.

        Strategy (Plan A dual-pass):
          - fixture hash_schema_version default => schema=2; but also accept schema=1 match.
          - Old fixture (declared 4e232e...) matches schema=1;
          - New fixture (declared 5eab1f...) matches schema=2 (default).
        """
        cfg = BacktestFilterConfig()
        fixture_hash = BASELINE["meta"]["config_hash"]
        schema_from_meta = BASELINE["meta"].get("hash_schema_version", 2)
        expected_by_meta = compute_config_hash(cfg, schema_version=schema_from_meta)
        expected_v1 = compute_config_hash(cfg, schema_version=1)
        expected_v2 = compute_config_hash(cfg, schema_version=2)
        assert fixture_hash in {expected_by_meta, expected_v1, expected_v2}, (
            "config_hash mismatch (schema=1/2 dual-pass):\n"
            f"  fixture_declared: {fixture_hash}\n"
            f"  hash_schema_version={schema_from_meta} => {expected_by_meta}\n"
            f"  schema=1 (legacy) => {expected_v1}\n"
            f"  schema=2 (canonical) => {expected_v2}"
        )
    @pytest.mark.parametrize("day_idx", list(range(30)))
    def test_day_sha256_signature(self, day_idx):
        """逐日对账：trades_count/holdings_count/exclude_count_by_rule 的 SHA256 与 baseline 一致。"""
        day = BASELINE["days"][day_idx]
        td = date.fromisoformat(day["trade_date"])
        all_sids = list(range(1, 11))
        pit = {
            sid: _status_from_dict(sid, td, day["statuses"][str(sid)])
            for sid in all_sids
        }
        positions = {3: 50.0, 2: 80.0, 5: 100.0}
        cfg = BacktestFilterConfig()

        outcome = apply_daily_filters(
            td, all_sids, positions, pit, cfg,
            run_id=1001, data_batch_id="baseline-30d",
        )

        trades_count = len([
            e for e in outcome.filter_events
            if e.action == ACTION_INCLUDE and e.rule_code == RULE_INCLUDE_NORMAL
        ])
        holdings_count = len(positions)
        exclude = {}
        for e in outcome.filter_events:
            if e.action == "exclude_candidate":
                exclude[e.rule_code] = exclude.get(e.rule_code, 0) + 1

        payload = json.dumps({
            "td": td.isoformat(),
            "trades_count": trades_count,
            "holdings_count": holdings_count,
            "exclude_count_by_rule": exclude,
        }, sort_keys=True, ensure_ascii=False)
        actual_sig = hashlib.sha256(payload.encode("utf-8")).hexdigest()

        assert actual_sig == day["sha256_signature"], (
            f"Day {day_idx} {td.isoformat()} SHA256 mismatch.\n"
            f"  Expected: {day['sha256_signature']}\n"
            f"  Actual:   {actual_sig}\n"
            f"  Payload:  {payload}"
        )

    @pytest.mark.parametrize("day_idx", list(range(30)))
    def test_day_effective_count(self, day_idx):
        """逐日对账 effective_count 与 baseline 一致。"""
        day = BASELINE["days"][day_idx]
        td = date.fromisoformat(day["trade_date"])
        all_sids = list(range(1, 11))
        pit = {
            sid: _status_from_dict(sid, td, day["statuses"][str(sid)])
            for sid in all_sids
        }
        positions = {3: 50.0, 2: 80.0, 5: 100.0}
        outcome = apply_daily_filters(
            td, all_sids, positions, pit, BacktestFilterConfig(),
            run_id=1001, data_batch_id="baseline-30d",
        )
        assert len(outcome.eligible_candidates) == day["effective_count"], (
            f"Day {day_idx} {td.isoformat()} effective_count mismatch: "
            f"baseline={day['effective_count']} replay={len(outcome.eligible_candidates)}"
        )

    def test_total_effective_matches_meta(self):
        """30 天 effective 总和 == meta.total_effective_count。"""
        total = sum(day["effective_count"] for day in BASELINE["days"])
        assert total == BASELINE["meta"]["total_effective_count"]

    def test_process_delisting_liquidations_injected(self):
        """symbol=5 摘牌后注入 last_valid_close=10.5，process_delisting_liquidations 成功。

        验证：摘牌日之后的 2024-09-01 起，注入 resolver 返回 10.5，
        LiquidationResult.exit_price == 10.5，pnl 计算正确。
        """
        # Find delisting days
        delisting_day = None
        for d in BASELINE["days"]:
            if d["delisting_candidate_count"] > 0:
                delisting_day = d
                break
        assert delisting_day is not None, "fixture 中至少有 1 个摘牌日"

        td = date.fromisoformat(delisting_day["trade_date"])
        all_sids = list(range(1, 11))
        pit = {
            sid: _status_from_dict(sid, td, delisting_day["statuses"][str(sid)])
            for sid in all_sids
        }
        positions_holding = {5: HoldingInfo(quantity=100.0, avg_cost_price=9.0)}
        cfg = BacktestFilterConfig()

        positions_qty = {3: 50.0, 2: 80.0, 5: 100.0}
        outcome = apply_daily_filters(td, all_sids, positions_qty, pit, cfg,
                                       run_id=1001, data_batch_id="baseline-30d")
        cands = outcome.delisting_candidates
        assert len(cands) >= 1

        def fixed_resolver(db, sid_, td_):
            return 10.5  # 前一日收盘 10.5

        results, extra = process_delisting_liquidations(
            db=None, run_id=1001, trade_date=td, positions=positions_holding,
            pit_status_map=pit, delisting_candidates=cands,
            price_resolver=fixed_resolver,
        )
        assert len(results) == 1
        r = results[0]
        assert r.symbol_id == 5
        assert r.exit_price == 10.5
        assert r.quantity == 100.0
        expected_pnl = (10.5 - 9.0) * 100.0
        assert abs(r.pnl - expected_pnl) < 1e-9

    def test_process_delisting_missing_price_blocks(self):
        """摘牌日 resolver 返回 None → DelistingPriceMissingError。"""
        for d in BASELINE["days"]:
            if d["delisting_candidate_count"] > 0:
                delisting_day = d
                break
        td = date.fromisoformat(delisting_day["trade_date"])
        all_sids = list(range(1, 11))
        pit = {
            sid: _status_from_dict(sid, td, delisting_day["statuses"][str(sid)])
            for sid in all_sids
        }
        positions_qty = {3: 50.0, 2: 80.0, 5: 100.0}
        positions_holding = {5: HoldingInfo(quantity=100.0, avg_cost_price=9.0)}
        outcome = apply_daily_filters(
            td, all_sids, positions_qty, pit, BacktestFilterConfig(),
            run_id=1001, data_batch_id="baseline-30d",
        )

        def none_resolver(db, sid_, td_):
            return None

        with pytest.raises(DelistingPriceMissingError):
            process_delisting_liquidations(
                db=None, run_id=1001, trade_date=td, positions=positions_holding,
                pit_status_map=pit, delisting_candidates=outcome.delisting_candidates,
                price_resolver=none_resolver,
            )
