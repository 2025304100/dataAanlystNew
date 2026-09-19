from __future__ import annotations

import json
from datetime import date

from scripts.g5_dual_run_report import load_input


def test_g5_report_loader_preserves_both_chain_snapshots(tmp_path):
    source = tmp_path / "g5.json"
    source.write_text(json.dumps({
        "portfolio_id": 12,
        "days": [{
            "trade_date": "2026-08-20",
            "chain_a": {
                "universe_symbol_ids": [1],
                "decisions": [{"symbol_id": 1, "action": "BUY"}],
            },
            "chain_b": {
                "universe_symbol_ids": [1],
                "decisions": [{"symbol_id": 1, "action": "BUY"}],
            },
        }],
    }), encoding="utf-8")
    portfolio_id, days, provenance = load_input(source)
    assert portfolio_id == 12
    assert days[date(2026, 8, 20)][0].decisions[1].action == "BUY"
    assert days[date(2026, 8, 20)][1].chain == "B"
    assert provenance == {}
