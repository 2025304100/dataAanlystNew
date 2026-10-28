"""Descriptive precision plus conservative, explicitly qualified sample gates."""
import json
import math
import random
from datetime import timedelta
from collections import defaultdict


def _wilson(hits, n):
    if not n:
        return None
    z, p = 1.959963984540054, hits/n
    denominator = 1+z*z/n
    centre = (p+z*z/(2*n))/denominator
    half = z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/denominator
    return [centre-half, centre+half]


def _quantile(values, probability):
    values = sorted(values)
    position = (len(values)-1)*probability
    index = int(position)
    return values[index]+(values[min(index+1, len(values)-1)]-values[index])*(position-index)


def summarize_predictions(rows):
    matured = [row for row in rows if row.outcome_status in ("hit", "miss", "unverifiable")]
    hits = sum(row.outcome_status == "hit" for row in matured)
    n, events = len(matured), defaultdict(list)
    for row in matured:
        events[row.event_key].append(row)
    p = hits/n if n else None
    baselines = [json.loads(row.snapshot_json).get("historical_baseline_rate") for row in matured]
    baseline = sum(baselines)/n if n and all(value is not None for value in baselines) else None
    event_interval = _wilson(sum(all(row.outcome_status == "hit" for row in group) for group in events.values()), len(events))
    by_day = defaultdict(list)
    for group in events.values():
        first_day = min(((row.sent_at or row.created_at)+timedelta(hours=8)).date() for row in group)
        by_day[first_day].extend(group)
    days = sorted(by_day)
    blocks = [[row for day in days[start:start+5] for row in by_day[day]] for start in range(0, len(days), 5)]
    block_interval = uplift_interval = None
    if len(blocks) >= 8:
        aggregates = [(len(block), sum(row.outcome_status == "hit" for row in block),
            sum(json.loads(row.snapshot_json).get("historical_baseline_rate") or 0 for row in block)) for block in blocks]
        rng, estimates, differences = random.Random(271027), [], []
        for _ in range(1000):
            sample = [rng.choice(aggregates) for _ in aggregates]
            size = sum(item[0] for item in sample)
            success = sum(item[1] for item in sample)
            estimates.append(success/size)
            differences.append((success-sum(item[2] for item in sample))/size)
        block_interval = [_quantile(estimates, 0.025), _quantile(estimates, 0.975)]
        if baseline is not None:
            uplift_interval = [_quantile(differences, 0.025), _quantile(differences, 0.975)]
    conservative_lower = min(event_interval[0], block_interval[0]) if event_interval and block_interval else None
    missing = sum(row.outcome_status == "unverifiable" for row in matured)
    status = "not_verified"
    if len(events) >= 200 and len(days) >= 40:
        if missing:
            status = "data_incomplete"
        elif p < 0.75:
            status = "below_target"
        elif conservative_lower is not None and conservative_lower >= 0.75 and uplift_interval and uplift_interval[0] > 0:
            status = "historical_target_met"
    return {"total": len(rows), "matured": n, "hits": hits,
        "misses": sum(row.outcome_status == "miss" for row in matured), "unverifiable": missing,
        "pending": sum(row.outcome_status == "pending" for row in rows), "precision": p,
        "wilson_interval": _wilson(hits, n), "event_clusters": len(events), "observation_days": len(days),
        "event_interval": event_interval, "block_interval": block_interval,
        "conservative_lower": conservative_lower, "historical_baseline_rate": baseline,
        "uplift": p-baseline if baseline is not None else None, "uplift_interval": uplift_interval,
        "validation_status": status, "minimum_event_clusters": 200, "minimum_observation_days": 40,
        "validation_note": "历史门槛判定：事件簇全目标成功率区间与5日块重采样下限均≥75%，且历史对照增益为正；不是因果或未来收益保证"}
