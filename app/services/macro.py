from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import akshare as ak
import pandas as pd
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.models.macro_data import MacroIndicatorValue, MacroSnapshot
from app.schemas.macro import MacroIndicatorRead, MacroOverviewResponse, MacroSnapshotRead
from app.services.akshare_utils import quiet_akshare_output

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MacroSpec:
    region: str
    category: str
    indicator_key: str
    name: str
    fetcher: str
    value_col: int
    unit: str
    frequency: str
    scoring: str
    previous_col: int | None = None
    band_low: float | None = None
    band_high: float | None = None
    neutral: float | None = None
    scale: float = 1.0


SPECS: list[MacroSpec] = [
    MacroSpec("cn", "inflation", "cn_cpi_yoy", "China CPI YoY", "macro_china_cpi", 2, "%", "monthly", "band", band_low=0.5, band_high=3.0),
    MacroSpec("cn", "inflation", "cn_ppi_yoy", "China PPI YoY", "macro_china_ppi", 2, "%", "monthly", "band", band_low=-2.0, band_high=3.0),
    MacroSpec("cn", "growth", "cn_pmi", "China Manufacturing PMI", "macro_china_pmi", 1, "", "monthly", "above_50"),
    MacroSpec("cn", "liquidity", "cn_m2_yoy", "China M2 YoY", "macro_china_money_supply", 2, "%", "monthly", "band", band_low=6.0, band_high=10.5),
    MacroSpec("cn", "credit", "cn_social_financing", "China Social Financing", "macro_china_bank_financing", 1, "CNY 100M", "monthly", "higher", neutral=2600, scale=0.006),
    MacroSpec("cn", "credit", "cn_new_credit", "China New Credit", "macro_china_new_financial_credit", 1, "CNY 100M", "monthly", "higher", neutral=3000, scale=0.004),
    MacroSpec("cn", "risk", "cn_10y_yield", "China 10Y Government Bond Yield", "bond_zh_us_rate", 3, "%", "daily", "lower", neutral=2.2, scale=8.0),
    MacroSpec("cn", "liquidity", "cn_lpr_1y", "China LPR 1Y", "macro_china_lpr", 1, "%", "monthly", "lower", neutral=3.4, scale=8.0),
    MacroSpec("cn", "liquidity", "cn_lpr_5y", "China LPR 5Y", "macro_china_lpr", 2, "%", "monthly", "lower", neutral=3.9, scale=7.0),
    MacroSpec("cn", "liquidity", "cn_margin_sh", "SSE Margin Balance", "macro_china_market_margin_sh", 6, "CNY", "daily", "change"),
    MacroSpec("cn", "liquidity", "cn_margin_sz", "SZSE Margin Balance", "macro_china_market_margin_sz", 6, "CNY", "daily", "change"),
    MacroSpec("us", "inflation", "us_cpi_yoy", "US CPI YoY", "macro_usa_cpi_yoy", 2, "%", "monthly", "band", previous_col=3, band_low=1.5, band_high=3.0),
    MacroSpec("us", "inflation", "us_core_cpi_mom", "US Core CPI MoM", "macro_usa_core_cpi_monthly", 2, "%", "monthly", "band", previous_col=3, band_low=0.1, band_high=0.3),
    MacroSpec("us", "inflation", "us_ppi", "US PPI YoY", "macro_usa_ppi", 2, "%", "monthly", "band", previous_col=3, band_low=0.0, band_high=3.2),
    MacroSpec("us", "growth", "us_industrial_production", "US Industrial Production YoY", "fred:INDPRO", 2, "%", "monthly", "higher", neutral=1.5, scale=4.0),
    MacroSpec("us", "growth", "us_non_farm", "US Nonfarm Payrolls", "macro_usa_non_farm", 2, "10k people", "monthly", "higher", previous_col=3, neutral=15, scale=0.7),
    MacroSpec("us", "risk", "us_unemployment", "US Unemployment Rate", "macro_usa_unemployment_rate", 2, "%", "monthly", "lower", previous_col=3, neutral=4.2, scale=7.0),
    MacroSpec("us", "risk", "us_10y_yield", "US 10Y Treasury Yield", "bond_zh_us_rate", 9, "%", "daily", "lower", neutral=4.2, scale=5.0),
]

CATEGORY_WEIGHTS = {
    "growth": 0.30,
    "inflation": 0.25,
    "liquidity": 0.25,
    "credit": 0.15,
    "risk": 0.05,
}

FRED_FALLBACKS = {
    "us_cpi_yoy": {"series": "CPIAUCSL", "transform": "yoy"},
    "us_core_cpi_mom": {"series": "CPILFESL", "transform": "mom"},
    "us_ppi": {"series": "PPIACO", "transform": "yoy"},
    "us_unemployment": {"series": "UNRATE", "transform": "level"},
    "us_non_farm": {"series": "PAYEMS", "transform": "monthly_change_10k"},
    "us_industrial_production": {"series": "INDPRO", "transform": "yoy"},
}


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _period_key(value: Any) -> tuple[int, int, int]:
    text = str(value)
    parsed = pd.to_datetime(text, errors="coerce")
    if not pd.isna(parsed):
        return (int(parsed.year), int(parsed.month), int(parsed.day))
    match = re.search(r"(\d{4}).*?(\d{1,2})", text)
    if match:
        return (int(match.group(1)), int(match.group(2)), 1)
    match = re.search(r"(\d{4})", text)
    if match:
        return (int(match.group(1)), 1, 1)
    return (0, 0, 0)


def _to_float(value: Any) -> float | None:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, (int, float)):
        if math.isnan(float(value)):
            return None
        return float(value)
    text = str(value).strip().replace(",", "").replace("%", "")
    if not text or text.lower() in {"nan", "none", "--"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _status(score: float) -> str:
    if score >= 6:
        return "positive"
    if score <= -6:
        return "negative"
    return "neutral"


def _score_band(value: float, low: float, high: float) -> float:
    if low <= value <= high:
        return 12.0
    if value < low:
        distance = low - value
        return _clamp(4 - distance * 8, -20, 8)
    distance = value - high
    return _clamp(4 - distance * 7, -20, 8)


def _score_indicator(spec: MacroSpec, value: float | None, previous: float | None) -> float:
    if value is None:
        return 0.0
    if spec.scoring == "above_50":
        return round(_clamp((value - 50) * 4.5, -20, 20), 2)
    if spec.scoring == "band" and spec.band_low is not None and spec.band_high is not None:
        return round(_score_band(value, spec.band_low, spec.band_high), 2)
    if spec.scoring == "higher":
        neutral = spec.neutral if spec.neutral is not None else 0.0
        return round(_clamp((value - neutral) * spec.scale, -20, 20), 2)
    if spec.scoring == "lower":
        neutral = spec.neutral if spec.neutral is not None else 0.0
        return round(_clamp((neutral - value) * spec.scale, -20, 20), 2)
    if spec.scoring == "change" and previous not in (None, 0):
        pct = (value - previous) / abs(previous) * 100
        return round(_clamp(pct * 7, -20, 20), 2)
    return 0.0


def _payload_from_values(
    spec: MacroSpec,
    *,
    period: str,
    value: float | None,
    previous_value: float | None,
    source: str,
    raw_payload: dict,
) -> dict:
    delta = None if value is None or previous_value is None else round(value - previous_value, 4)
    score = _score_indicator(spec, value, previous_value)
    return {
        "region": spec.region,
        "category": spec.category,
        "indicator_key": spec.indicator_key,
        "name": spec.name,
        "period": period,
        "value": value,
        "previous_value": previous_value,
        "delta": delta,
        "unit": spec.unit,
        "frequency": spec.frequency,
        "source": source,
        "score": score,
        "status": _status(score),
        "raw_payload": json.dumps(raw_payload, ensure_ascii=False, default=str),
    }


def _extract_fred(spec: MacroSpec) -> tuple[dict, list[dict]]:
    fallback = FRED_FALLBACKS.get(spec.indicator_key)
    if fallback is None:
        raise ValueError("no fred fallback configured")
    series = fallback["series"]
    frame = pd.read_csv(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}")
    if frame is None or frame.empty:
        raise ValueError("empty fred dataframe")
    value_col = series
    frame[value_col] = pd.to_numeric(frame[value_col].replace(".", pd.NA), errors="coerce")
    frame = frame.dropna(subset=[value_col]).copy()
    if frame.empty:
        raise ValueError("no numeric fred value found")
    transform = fallback["transform"]
    if transform == "yoy":
        frame["macro_value"] = frame[value_col].pct_change(12) * 100
        frame["macro_previous"] = frame["macro_value"].shift(1)
    elif transform == "mom":
        frame["macro_value"] = frame[value_col].pct_change(1) * 100
        frame["macro_previous"] = frame["macro_value"].shift(1)
    elif transform == "monthly_change_10k":
        frame["macro_value"] = frame[value_col].diff(1) / 10
        frame["macro_previous"] = frame["macro_value"].shift(1)
    else:
        frame["macro_value"] = frame[value_col]
        frame["macro_previous"] = frame[value_col].shift(1)
    frame = frame.dropna(subset=["macro_value"]).copy()
    if frame.empty:
        raise ValueError("fred transform produced no values")
    payloads: list[dict] = []
    for row in frame.tail(60).to_dict("records"):
        value = _to_float(row.get("macro_value"))
        if value is None:
            continue
        previous_value = _to_float(row.get("macro_previous"))
        payloads.append(
            _payload_from_values(
                spec,
                period=str(row.get("observation_date", "unknown")),
                value=value,
                previous_value=previous_value,
                source=f"fred:{series}",
                raw_payload=row,
            )
        )
    if not payloads:
        raise ValueError("fred history produced no values")
    return payloads[-1], payloads


def _extract_latest(spec: MacroSpec) -> tuple[dict, list[dict]]:
    if spec.fetcher.startswith("fred:"):
        return _extract_fred(spec)
    fetcher = getattr(ak, spec.fetcher)
    with quiet_akshare_output():
        frame = fetcher()
    if frame is None or frame.empty:
        raise ValueError("empty macro dataframe")
    rows = frame.to_dict("records")
    columns = list(frame.columns)
    if spec.value_col >= len(columns):
        raise ValueError(f"value column {spec.value_col} out of range")
    rows = sorted(rows, key=lambda row: _period_key(row.get(columns[0])))
    payloads: list[dict] = []
    for index, row in enumerate(rows[-60:]):
        value = _to_float(row.get(columns[spec.value_col]))
        if value is None:
            continue
        previous_value = None
        if spec.previous_col is not None and spec.previous_col < len(columns):
            previous_value = _to_float(row.get(columns[spec.previous_col]))
        if previous_value is None:
            source_index = rows.index(row)
            for prev in reversed(rows[:source_index]):
                previous_value = _to_float(prev.get(columns[spec.value_col]))
                if previous_value is not None:
                    break
        payloads.append(
            _payload_from_values(
                spec,
                period=str(row.get(columns[0], "unknown")),
                value=value,
                previous_value=previous_value,
                source=f"akshare:{spec.fetcher}",
                raw_payload=row,
            )
        )
    if not payloads:
        raise ValueError("no numeric value found")
    return payloads[-1], payloads


def _upsert_indicator(db: Session, payload: dict) -> MacroIndicatorValue:
    existing = (
        db.execute(
            select(MacroIndicatorValue).where(
                MacroIndicatorValue.region == payload["region"],
                MacroIndicatorValue.indicator_key == payload["indicator_key"],
                MacroIndicatorValue.period == payload["period"],
            )
        )
        .scalars()
        .first()
    )
    if existing is None:
        existing = MacroIndicatorValue()
        db.add(existing)
    for key, value in payload.items():
        setattr(existing, key, value)
    existing.updated_at = datetime.now(timezone.utc)
    db.flush()
    return existing


def _indicator_read(row: MacroIndicatorValue) -> MacroIndicatorRead:
    return MacroIndicatorRead(
        id=row.id,
        region=row.region,
        category=row.category,
        indicator_key=row.indicator_key,
        name=row.name,
        period=row.period,
        value=row.value,
        previous_value=row.previous_value,
        delta=row.delta,
        unit=row.unit,
        frequency=row.frequency,
        source=row.source,
        score=row.score,
        status=row.status,
        updated_at=row.updated_at,
    )


def _snapshot_read(row: MacroSnapshot) -> MacroSnapshotRead:
    return MacroSnapshotRead(
        id=row.id,
        region=row.region,
        market_score=row.market_score,
        stance=row.stance,
        summary=row.summary,
        growth_score=row.growth_score,
        inflation_score=row.inflation_score,
        liquidity_score=row.liquidity_score,
        credit_score=row.credit_score,
        risk_score=row.risk_score,
        indicators_total=row.indicators_total,
        failed_total=row.failed_total,
        created_at=row.created_at,
    )


def _average(values: list[float]) -> float:
    return round(sum(values) / len(values), 2) if values else 0.0


def _stance(score: float) -> str:
    if score >= 68:
        return "risk_on"
    if score <= 42:
        return "defensive"
    if score <= 52:
        return "cautious"
    return "neutral"


def _brief(region: str, category_scores: dict[str, float], market_score: float, failed_total: int) -> str:
    region_label = {"cn": "中国大陆", "us": "美国", "all": "综合市场"}.get(region, region)
    best = max(category_scores.items(), key=lambda item: item[1], default=("growth", 0))
    worst = min(category_scores.items(), key=lambda item: item[1], default=("risk", 0))
    category_label = {
        "growth": "增长",
        "inflation": "通胀",
        "liquidity": "流动性",
        "credit": "信用/杠杆",
        "risk": "风险",
    }
    lines = [
        f"{region_label}宏观环境分为 {market_score:.1f}，当前偏{ {'risk_on':'积极','neutral':'中性','cautious':'谨慎','defensive':'防守'}[_stance(market_score)] }。",
        f"贡献最强的是{category_label.get(best[0], best[0])}维度，拖累最大的是{category_label.get(worst[0], worst[0])}维度。",
    ]
    if category_scores.get("inflation", 0) < -5:
        lines.append("通胀维度偏弱，仓位扩张前要先确认利率和估值压力是否缓和。")
    if category_scores.get("liquidity", 0) > 6:
        lines.append("流动性边际改善，短线机会更容易扩散，但仍要配合个股买点。")
    if category_scores.get("growth", 0) < -6:
        lines.append("增长信号偏弱，优先降低周期和高弹性仓位的激进程度。")
    if failed_total:
        lines.append(f"有 {failed_total} 个数据源暂时不可用，本次评分会自动降低参考权重。")
    return "\n".join(lines)


def _create_snapshot(db: Session, region: str, indicators: list[MacroIndicatorValue], failed_total: int) -> MacroSnapshot:
    category_scores = {
        category: _average([row.score for row in indicators if row.category == category])
        for category in CATEGORY_WEIGHTS
    }
    weighted = sum(category_scores[key] * weight for key, weight in CATEGORY_WEIGHTS.items())
    market_score = round(_clamp(50 + weighted * 2.25, 0, 100), 2)
    snapshot = MacroSnapshot(
        region=region,
        market_score=market_score,
        stance=_stance(market_score),
        summary=_brief(region, category_scores, market_score, failed_total),
        growth_score=category_scores["growth"],
        inflation_score=category_scores["inflation"],
        liquidity_score=category_scores["liquidity"],
        credit_score=category_scores["credit"],
        risk_score=category_scores["risk"],
        indicators_total=len(indicators),
        failed_total=failed_total,
    )
    db.add(snapshot)
    db.flush()
    return snapshot


def _latest_indicators(db: Session, region: str) -> list[MacroIndicatorValue]:
    target_regions = ["cn", "us"] if region == "all" else [region]
    latest: list[MacroIndicatorValue] = []
    keys = (
        db.execute(
            select(MacroIndicatorValue.region, MacroIndicatorValue.indicator_key)
            .where(MacroIndicatorValue.region.in_(target_regions))
            .distinct()
        )
        .all()
    )
    for item_region, key in keys:
        row = (
            db.execute(
                select(MacroIndicatorValue)
                .where(MacroIndicatorValue.region == item_region, MacroIndicatorValue.indicator_key == key)
                .order_by(desc(MacroIndicatorValue.updated_at), desc(MacroIndicatorValue.id))
            )
            .scalars()
            .first()
        )
        if row is not None:
            latest.append(row)
    return sorted(latest, key=lambda item: (item.region, item.category, item.indicator_key))


def _latest_snapshot(db: Session, region: str) -> MacroSnapshot | None:
    return (
        db.execute(
            select(MacroSnapshot)
            .where(MacroSnapshot.region == region)
            .order_by(desc(MacroSnapshot.created_at), desc(MacroSnapshot.id))
        )
        .scalars()
        .first()
    )


def update_macro_data(db: Session, region: str = "all") -> MacroOverviewResponse:
    target_regions = ["cn", "us"] if region == "all" else [region]
    failed: list[dict] = []
    refreshed: list[MacroIndicatorValue] = []
    for spec in [item for item in SPECS if item.region in target_regions]:
        try:
            try:
                payload, history = _extract_latest(spec)
            except Exception as ak_exc:
                if spec.indicator_key not in FRED_FALLBACKS:
                    raise
                logger.debug("AKShare failed for %s, trying FRED fallback", spec.indicator_key, exc_info=True)
                try:
                    payload, history = _extract_fred(spec)
                except Exception as fred_exc:
                    raise RuntimeError(f"AKShare failed: {ak_exc}; FRED failed: {fred_exc}") from fred_exc
            for item in history:
                _upsert_indicator(db, item)
            refreshed.append(_upsert_indicator(db, payload))
        except Exception as exc:
            logger.debug("Failed to fetch macro indicator %s", spec.indicator_key, exc_info=True)
            failed.append({"indicator_key": spec.indicator_key, "name": spec.name, "error": str(exc)})

    snapshots: list[MacroSnapshot] = []
    for item_region in target_regions:
        rows = [row for row in _latest_indicators(db, item_region) if row.region == item_region]
        snapshots.append(_create_snapshot(db, item_region, rows, len([f for f in failed if f["indicator_key"].startswith(f"{item_region}_")])))
    if region == "all":
        rows = _latest_indicators(db, "all")
        snapshots.append(_create_snapshot(db, "all", rows, len(failed)))

    db.commit()
    return get_macro_overview(db, region=region, failed=failed)


def get_macro_overview(db: Session, region: str = "all", failed: list[dict] | None = None) -> MacroOverviewResponse:
    snapshot = _latest_snapshot(db, region)
    indicators = _latest_indicators(db, region)
    brief = snapshot.summary.split("\n") if snapshot and snapshot.summary else []
    return MacroOverviewResponse(
        region=region,
        snapshot=_snapshot_read(snapshot) if snapshot else None,
        indicators=[_indicator_read(row) for row in indicators],
        brief=brief,
        failed=failed or [],
    )



def get_macro_indicator_history(db: Session, region: str, indicator_key: str, limit: int = 60) -> list[MacroIndicatorRead]:
    rows = (
        db.execute(
            select(MacroIndicatorValue)
            .where(MacroIndicatorValue.region == region, MacroIndicatorValue.indicator_key == indicator_key)
            .order_by(desc(MacroIndicatorValue.updated_at), desc(MacroIndicatorValue.id))
            .limit(limit)
        )
        .scalars()
        .all()
    )
    ordered = sorted(rows, key=lambda item: _period_key(item.period))
    return [_indicator_read(row) for row in ordered]
