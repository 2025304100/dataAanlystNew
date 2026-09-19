from __future__ import annotations

from app.schemas.macro import MacroUpdateRequest
from app.services import macro, macro_update_task


def test_update_macro_data_reports_progress(monkeypatch, db_session):
    specs = [
        macro.MacroSpec("cn", "growth", "cn_demo_growth", "CN Demo Growth", "demo_growth", 1, "%", "monthly", "higher", neutral=1.0, scale=2.0),
        macro.MacroSpec("cn", "inflation", "cn_demo_inflation", "CN Demo Inflation", "demo_inflation", 1, "%", "monthly", "band", band_low=1.0, band_high=3.0),
    ]
    monkeypatch.setattr(macro, "SPECS", specs)

    def fake_fetch(spec):
        payload = {
            "region": spec.region,
            "category": spec.category,
            "indicator_key": spec.indicator_key,
            "name": spec.name,
            "period": "2026-06",
            "value": 2.0,
            "previous_value": 1.5,
            "delta": 0.5,
            "unit": spec.unit,
            "frequency": spec.frequency,
            "source": "test",
            "score": 8.0,
            "status": "positive",
            "raw_payload": "{}",
        }
        return payload, [payload]

    monkeypatch.setattr(macro, "_fetch_macro_payloads", fake_fetch)
    progress = []
    overview = macro.update_macro_data(db_session, region="cn", progress_callback=progress.append)

    assert overview.region == "cn"
    assert len(overview.indicators) == 2
    assert progress[0]["processed"] == 0
    assert progress[-1]["stage"] == "done"
    assert progress[-1]["processed"] == 2


def test_create_macro_update_task_returns_existing_running_task(monkeypatch, db_session):
    monkeypatch.setattr(macro_update_task, "_start_worker", lambda *args, **kwargs: None)

    first = macro_update_task.create_macro_update_task(MacroUpdateRequest(region="cn"))
    second = macro_update_task.create_macro_update_task(MacroUpdateRequest(region="us"))

    assert first.id == second.id
    latest = macro_update_task.get_latest_macro_update_task()
    assert latest is not None
    assert latest.id == first.id
