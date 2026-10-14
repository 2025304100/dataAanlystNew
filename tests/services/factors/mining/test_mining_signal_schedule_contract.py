"""Published L3 mining split/signal-schedule contracts; all inputs are isolated."""
from datetime import date, timedelta

import pytest

from app.services.factors.mining import evaluation_adapter as EVA


pytestmark = pytest.mark.whitebox


def _weekly_points(count):
    return [date(2020, 1, 3) + timedelta(weeks=index) for index in range(count)]


def test_twenty_day_weekly_budget_does_not_claim_104_points_are_ready():
    budget = EVA.compute_split_budget(
        all_dates=_weekly_points(104), frequency="weekly",
        purge_points=4, embargo_points=4, tail_loss=0,
    )
    assert budget.train_points == 62
    assert budget.val_points == 17
    assert budget.test_points == 17
    assert budget.meets_floor is False


@pytest.mark.parametrize("horizon, count, expected", [(1, 104, (62, 20, 20)), (5, 104, (62, 20, 20)), (20, 130, (78, 22, 22))])
def test_mature_weekly_labels_do_not_lose_another_target_tail(horizon, count, expected):
    split, budget = EVA.build_split(all_dates=_weekly_points(count), frequency="weekly",
        target_horizon=horizon, tail_loss_points=0)
    assert (budget.train_points, budget.val_points, budget.test_points) == expected
    assert budget.meets_floor and budget.tail_loss == 0
    assert split.target_horizon == horizon


@pytest.mark.parametrize("horizon", [1, 5, 20])
def test_mature_monthly_labels_keep_the_actual_horizon_and_statistics_downgrade(horizon):
    import pandas as pd
    points = list(pd.date_range("2020-01-31", periods=41, freq="ME").date)
    split, budget = EVA.build_split(all_dates=points, frequency="monthly",
        target_horizon=horizon, tail_loss_points=0)
    assert (budget.train_points, budget.val_points, budget.test_points) == (24, 7, 8)
    assert budget.meets_floor and budget.statistically_degraded
    assert split.target_horizon == horizon


def test_signal_schedule_deduplicates_symbols_and_uses_real_period_ends():
    days = [date(2020, 12, 30), date(2020, 12, 31), date(2021, 1, 4), date(2021, 1, 8), date(2021, 1, 29), date(2021, 2, 1)]
    assert EVA.select_signal_dates(days + days, "weekly") == (date(2020, 12, 31), date(2021, 1, 8), date(2021, 1, 29), date(2021, 2, 1))
    assert EVA.select_signal_dates(days + days, "monthly") == (date(2020, 12, 31), date(2021, 1, 29), date(2021, 2, 1))
