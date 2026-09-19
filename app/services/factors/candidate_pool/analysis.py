"""候选池 · 分析看板（向导 §3.7 / SD-v2.0 §6.3；任务 T11）。

这个模块产出 `snapshot.analysis_json` —— 它**同时**是：
1. 向导 §3.7.4 看板弹窗的渲染数据（只读，不可编辑）
2. T22 AI 因子生成的 **Prompt 输入变量**（§3.7.5 的数据闭环）

所以字段名是**契约**：T22 会按这里的 key 取值，改名字就是破坏下游。

设计要点
========
**为什么市值分档用固定金额、而 T09 预设用分位数**
向导内部有两处看似矛盾的口径：
- §3.7.4 看板：「大盘（>500亿）、中盘（100~500亿）、小盘（<100亿）」—— 固定金额
- §3.2 预设：「阈值必须由后端按当日目标市场可用样本分位数计算」—— 分位数

两者**用途不同，可共存**：
- 看板是**描述性统计**（"这个池子长什么样"），固定金额让人有直觉、可跨期比较；
- 预设是**筛选档位**（"点一下帮我选一批"），必须随市场水位变化，否则同一档位
  在不同年份会筛出完全不同的池子。
故：看板用 §3.7.4 的固定阈值（本模块），预设用 §3.2 的分位数（`presets.py`）。

**风格暴露为什么必须用全市场做基准**
池内 z-score 的均值恒为 0，毫无信息量。有意义的是「这个池子在全市场中的相对位置」
—— 所以每个维度都计算**池内成员的全市场分位数均值**（0~1），
再映射成雷达图分数。这需要装载全市场数据（~5,500 只 × 25 日 ≈ 14 万行，
pandas 毫秒级，符合 §3.7.6「2~15 秒」的同步计算预算）。

**不可用的维度如实标注，不造数**
成长维度依赖 `net_profit_yoy` —— 实测 **0 行非空**（T09 已确认），
所以 5 维雷达只报 4 维，成长标 `unavailable` 并写明原因。
把缺失填成 0 会给 AI 一个"这个池子毫无成长性"的假信号，直接污染 Prompt。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from app.services.factors.candidate_pool import rules as R

#: 风格暴露维度（向导 §3.7.4：成长/价值/质量/动量/波动率）
STYLE_DIMENSIONS: tuple[str, ...] = ("growth", "value", "quality", "momentum",
                                     "volatility")
STYLE_LABELS_ZH: dict[str, str] = {
    "growth": "成长",
    "value": "价值",
    "quality": "质量",
    "momentum": "动量",
    "volatility": "波动",
}

#: 市值分档（向导 §3.7.4 的**固定金额**口径，见模块 docstring 的裁决）
MARKET_CAP_TIERS: tuple[tuple[str, str, float | None, float | None], ...] = (
    # (key, label_zh, min, max) —— 单位：元；区间**含下界、不含上界**
    ("large", "大盘", 5e10, None),
    ("mid", "中盘", 1e10, 5e10),
    ("small", "小盘", None, 1e10),
)

#: 市场环境：波动率分档阈值（**年化波动率**，池内等权日收益标准差 × √244）
VOL_HIGH = 0.30
VOL_MID = 0.18

#: 行业集中度告警阈值（向导 §3.7.4：>40% 提示行业中性）
INDUSTRY_CONCENTRATION_ALERT = 0.40

#: 行业分布 Top N（超过归「其他」）
INDUSTRY_TOP_N = 8

#: 覆盖率低于此值在看板标红（向导 §3.7.4：覆盖率<80%的字段标红提示）
COVERAGE_ALERT = 0.80

#: 动量/波动率的默认回看窗口（交易日）
DEFAULT_LOOKBACK_DAYS = 20

#: 不足该天数则动量/波动率不可用（避免 2 天算出天文数字波动率）
MIN_LOOKBACK_DAYS = 10

#: 分析回看的交易日窗口（比 lookback 略宽，留出收益率首行 NaN）
ANALYSIS_WINDOW_DAYS = 60


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ══════════════════════════════════════════════════════════════
# 分析面板（纯数据输入）
# ══════════════════════════════════════════════════════════════


@dataclass
class AnalysisPanel:
    """分析引擎的输入面板。

    与 T09 的 `ScreeningPanel` 区别：这里的表覆盖**全市场**（因为风格暴露需要
    全市场基准），成员列表单独传入，由引擎自己做交集。
    """

    #: 全市场行情（窗口内）：symbol, trade_date, close
    daily: pd.DataFrame
    #: 全市场估值（as_of 截面）：symbol, pe_ttm, pb, total_market_cap, ...
    valuation: pd.DataFrame
    #: 全市场财报（公告日 PIT）：symbol, roe_ttm, net_profit_yoy
    financials: pd.DataFrame
    #: 成员标的代码
    members: list[str]
    #: 数据截止日
    as_of_date: date
    #: 动量/波动率回看交易日数
    lookback_days: int = DEFAULT_LOOKBACK_DAYS
    #: 窗口内实际交易日数（由装载层回报）
    trade_days_actual: int = 0
    #: 平均每日有效标的数
    avg_daily_symbols: float = 0.0

    def is_empty(self) -> bool:
        return not self.members or self.daily is None or self.daily.empty


# ══════════════════════════════════════════════════════════════
# 纯分析引擎
# ══════════════════════════════════════════════════════════════


def _momentum_volatility(daily: pd.DataFrame, lookback: int,
                         ) -> pd.DataFrame:
    """按标的算近 `lookback` 交易日动量与年化波动率。

    返回 index=symbol 的 DataFrame：`momentum`（区间收益率）、
    `volatility`（日收益标准差 × √244）。
    """
    if daily.empty:
        return pd.DataFrame(columns=["momentum", "volatility"])
    d = daily[["symbol", "trade_date", "close"]].copy()
    d["trade_date"] = pd.to_datetime(d["trade_date"])
    d = d.sort_values(["symbol", "trade_date"])
    # 只保留每标的最近 `lookback + 1` 行（省内存，结果等价）
    d = d.groupby("symbol", sort=False).tail(lookback + 1)

    def _calc(g: pd.DataFrame) -> pd.Series:
        closes = g["close"].astype(float).dropna()
        if len(closes) < MIN_LOOKBACK_DAYS:
            return pd.Series({"momentum": np.nan, "volatility": np.nan})
        ret = closes.pct_change().dropna()
        if len(ret) < MIN_LOOKBACK_DAYS - 1 or float(ret.std(ddof=0)) == 0.0:
            momentum = float(closes.iloc[-1] / closes.iloc[0] - 1.0) \
                if closes.iloc[0] else np.nan
            return pd.Series({"momentum": momentum, "volatility": np.nan})
        return pd.Series({
            "momentum": float(closes.iloc[-1] / closes.iloc[0] - 1.0),
            "volatility": float(ret.std(ddof=0)) * np.sqrt(244.0),
        })

    return d.groupby("symbol", sort=False).apply(_calc, include_groups=False)


def _style_scores(panel: AnalysisPanel) -> tuple[dict[str, Any], dict[str, float]]:
    """5 维风格：池内成员的全市场分位数均值（0~1）。

    分位数而非 z-score：分位数对分布形状稳健、天然落在 [0,1]、
    且「0.72」比「z=0.63」更容易被人和 AI 理解。
    """
    members = set(panel.members)
    daily_stats = _momentum_volatility(panel.daily, panel.lookback_days)

    # 每个维度的全市场取值
    universe: dict[str, pd.Series] = {}
    val = panel.valuation
    if not val.empty:
        if "pe_ttm" in val.columns:
            # 价值：1/PE（PE 越低越"价值"；PE≤0 视为无意义）
            pe = pd.to_numeric(val["pe_ttm"], errors="coerce")
            pe = pe.where(pe > 0)
            universe["value"] = pd.Series(
                (1.0 / pe).to_numpy(), index=val["symbol"].astype(str))
        if "pb" in val.columns:
            pb = pd.to_numeric(val["pb"], errors="coerce")
            pb = pb.where(pb > 0)
            # PB 已在 value 里表达，避免双计；这里作为质量的补充来源之一不单独入维
    if not panel.financials.empty and "roe_ttm" in panel.financials.columns:
        universe["quality"] = pd.Series(
            pd.to_numeric(panel.financials["roe_ttm"], errors="coerce").to_numpy(),
            index=panel.financials["symbol"].astype(str))
    if not daily_stats.empty:
        universe["momentum"] = daily_stats["momentum"]
        universe["volatility"] = daily_stats["volatility"]
    # 成长：net_profit_yoy 实测全 NULL → 不进 universe，由下方标 unavailable

    dims: dict[str, Any] = {}
    coverages: dict[str, float] = {}
    for dim in STYLE_DIMENSIONS:
        series = universe.get(dim)
        if series is None or series.dropna().empty:
            dims[dim] = {
                "available": False,
                "score": None,
                "reason_zh": _style_unavailable_reason(dim),
            }
            continue
        vals = series.dropna()
        ranks = vals.rank(pct=True)            # 全市场分位 0~1
        member_idx = [s for s in panel.members if s in ranks.index]
        if not member_idx:
            dims[dim] = {"available": False, "score": None,
                         "reason_zh": "候选池成员在该维度无可用取值。"}
            continue
        member_pct = ranks.loc[member_idx]
        score = float(member_pct.mean())
        coverages[dim] = len(member_idx) / max(1, len(panel.members))
        # 「波动率」是反向维度：分位高 = 波动大。雷达上保留原语义并在 label 说明，
        # 不做 1-x 翻转 —— 翻转会让人误以为"波动分高=稳健"。
        dims[dim] = {
            "available": True,
            "score": round(score, 4),
            "label_zh": STYLE_LABELS_ZH[dim],
            "coverage": round(coverages[dim], 4),
            "interpretation_zh": (
                f"池内成员在{'全市场' if dim != 'volatility' else '全市场'}的"
                f"{STYLE_LABELS_ZH[dim]}分位均值：{score:.0%}"
                + ("（分位越高＝波动越大）" if dim == "volatility" else "")
            ),
        }
    return dims, coverages


def _style_unavailable_reason(dim: str) -> str:
    if dim == "growth":
        return ("实测 `raw_financial_reports.net_profit_yoy` **0 行非空**（全 NULL），"
                "成长维度无数据。不填 0 —— 那会给 AI 一个「该池毫无成长性」的假信号，"
                "直接污染 Prompt。")
    return "该维度所需数据当前不可用。"


def _market_regime(returns: pd.Series, max_drawdown: float) -> dict[str, Any]:
    """市场阶段：牛市 / 熊市 / 震荡市（区间涨跌幅 + 最大回撤）。

    口径（**等权池内指数**，不是真实指数 —— 本项目没有指数表，须明示）：
    - 区间涨跌幅 = 首末日累计收益
    - 最大回撤 = 累计净值相对历史峰值的最大跌幅
    """
    if returns.empty:
        return {"stage": "unavailable", "label_zh": "不可用",
                "reason_zh": "窗口内无行情数据。"}
    cumulative = float((1.0 + returns).prod() - 1.0)
    # 阶段判定：幅度为主、回撤为辅
    if cumulative >= 0.15:
        stage, label = "bull", "牛市"
    elif cumulative <= -0.15:
        stage, label = "bear", "熊市"
    else:
        stage, label = "sideways", "震荡市"
    return {
        "stage": stage,
        "label_zh": label,
        "cumulative_return": round(cumulative, 6),
        "max_drawdown": round(max_drawdown, 6),
        "basis_zh": (
            "基于**候选池成员等权日收益**的累乘（本项目无指数表，"
            "用池内等权近似市场环境；口径已写入快照，不可与真实指数混用）。"
        ),
    }


def _factor_type_suggestions(regime: Mapping[str, Any],
                             vol_annual: float | None) -> list[dict[str, Any]]:
    """因子类型建议（向导 §3.7.4 星级规则）。

    震荡市 → 反转★★★ / 波动率★★☆；趋势市 → 动量★★★ / 趋势★★☆；
    高波动 → 波动率收缩★★★ / 突破★★☆。三条规则可叠加，星级取并集。
    """
    out: dict[str, dict[str, Any]] = {}

    def _add(name: str, stars: int) -> None:
        cur = out.get(name)
        if cur is None or stars > cur["stars"]:
            out[name] = {"factor_type": name, "stars": stars}

    stage = regime.get("stage")
    if stage == "sideways":
        _add("reversal", 3)
        _add("volatility", 2)
    elif stage in ("bull", "bear"):  # 趋势市
        _add("momentum", 3)
        _add("trend", 2)

    if vol_annual is not None and vol_annual >= VOL_HIGH:
        _add("vol_contraction", 3)
        _add("breakout", 2)

    ranked = sorted(out.values(), key=lambda d: -d["stars"])
    for item in ranked:
        item["stars_label_zh"] = "★" * item["stars"] + "☆" * (3 - item["stars"])
    return ranked


def _distribution(values: pd.Series, edges: Sequence[tuple[str, str, float | None, float | None]],
                  total: int) -> list[dict[str, Any]]:
    """按固定分档统计数量与占比。区间**含下界、不含上界**。"""
    numeric = pd.to_numeric(values, errors="coerce").dropna()
    out: list[dict[str, Any]] = []
    assigned = 0
    for key, label, lo, hi in edges:
        mask = pd.Series(True, index=numeric.index)
        if lo is not None:
            mask &= numeric >= lo
        if hi is not None:
            mask &= numeric < hi
        n = int(mask.sum())
        assigned += n
        out.append({"key": key, "label_zh": label, "count": n,
                    "ratio": round(n / total, 6) if total else 0.0})
    # NaN（无估值）单列，不让它消失
    others = total - assigned
    if others > 0:
        out.append({"key": "unknown", "label_zh": "无数据", "count": others,
                    "ratio": round(others / total, 6) if total else 0.0})
    return out


def build_analysis(panel: AnalysisPanel) -> dict[str, Any]:
    """分析引擎主入口：**纯函数**，产出 `analysis_json` 全量结构。

    顶层 key 是 T22（AI 生成）的 Prompt 变量契约，不得改名：
    `overview` / `market_cap_distribution` / `industry_distribution`
    / `style_exposure` / `market_environment` / `data_quality` / `warnings`。
    """
    warnings: list[str] = []
    member_set = set(panel.members)

    if not panel.members:
        return {
            "generated_at": _utcnow().isoformat(),
            "as_of_date": None,
            "overview": {},
            "warnings": ["候选池没有成员，无法分析。"],
        }

    # ── 1. 概览 ──
    val = panel.valuation
    member_val = val[val["symbol"].astype(str).isin(member_set)] if not val.empty \
        else pd.DataFrame(columns=["symbol", "total_market_cap", "pe_ttm", "pb"])
    caps = pd.to_numeric(member_val.get("total_market_cap"), errors="coerce").dropna() \
        if not member_val.empty else pd.Series(dtype="float64")
    avg_cap = float(caps.mean()) if not caps.empty else None

    coverage_alert_fields: list[str] = []

    # ── 2. 市值分布（固定金额档，见模块 docstring 裁决）──
    tiers = _distribution(
        member_val.get("total_market_cap", pd.Series(dtype="float64")),
        MARKET_CAP_TIERS, len(panel.members))
    if avg_cap is None:
        warnings.append("成员在数据截止日缺少估值数据，市值概览与分布不可用。")

    # ── 3. 行业分布（symbols.industry 实测全空 → 全部落「未知」）──
    industry_counts: dict[str, int] = {}
    for m in panel.members:
        industry_counts[m] = industry_counts.get(m, 0) + 1
    # 成员无行业数据 → 不造数
    industry_dist: list[dict[str, Any]] = [
        {"industry": "未知 / 未披露", "count": len(panel.members),
         "ratio": 1.0,
         "note_zh": "实测 `symbols.industry` 0 行非空，行业维度当前不可用。"}
    ]
    industry_alert = None
    warnings.append(
        "行业分布不可用：`symbols.industry` 实测 0 行非空（全 NULL）。"
        "看板按「未知 / 未披露」展示，**不造数**；行业中性化类建议暂缺。"
    )

    # ── 4. 风格暴露 ──
    style_dims, style_coverages = _style_scores(panel)
    available_styles = {k: v["score"] for k, v in style_dims.items()
                        if v.get("available") and v.get("score") is not None}
    dominant = max(available_styles.items(), key=lambda kv: kv[1]) \
        if available_styles else None
    if "growth" in style_dims and not style_dims["growth"].get("available"):
        warnings.append("风格暴露的「成长」维度不可用（net_profit_yoy 全 NULL），已如实标注。")

    # ── 5. 市场环境 ──
    d = panel.daily
    rets = pd.Series(dtype="float64")
    vol_annual: float | None = None
    if not d.empty:
        dd = d[["symbol", "trade_date", "close"]].copy()
        dd["trade_date"] = pd.to_datetime(dd["trade_date"])
        dd["close"] = pd.to_numeric(dd["close"], errors="coerce")
        wide = dd.pivot_table(index="trade_date", columns="symbol",
                              values="close", aggfunc="last")
        # 等权日收益：每日横截面均值的逐日变化（成员全集，非仅成员池 —— 与全市场基准一致）
        mean_close = wide.mean(axis=1).dropna()
        if len(mean_close) >= 2:
            rets = mean_close.pct_change().dropna()
            if len(rets) >= MIN_LOOKBACK_DAYS - 1:
                vol_annual = float(rets.std(ddof=0) * np.sqrt(244.0))
    cum = float((1.0 + rets).prod() - 1.0) if not rets.empty else 0.0
    running_max = (1.0 + rets).cummax() if not rets.empty else pd.Series(dtype="float64")
    drawdown = ((1.0 + rets) / running_max - 1.0) if not rets.empty \
        else pd.Series(dtype="float64")
    max_dd = float(drawdown.min()) if not drawdown.empty else 0.0

    regime = _market_regime(rets, max_dd)
    vol_band = ("unavailable", "不可用")
    if vol_annual is not None:
        vol_band = (("high", "高") if vol_annual >= VOL_HIGH
                    else ("mid", "中") if vol_annual >= VOL_MID
                    else ("low", "低"))
    suggestions = _factor_type_suggestions(regime, vol_annual)

    market_env = {
        "market_regime": regime,
        "volatility": {
            "band": vol_band[0],
            "label_zh": vol_band[1],
            "annualized": round(vol_annual, 6) if vol_annual is not None else None,
            "thresholds": {"high": VOL_HIGH, "mid": VOL_MID},
        },
        "trend_strength": _trend_strength(rets),
        "factor_type_suggestions": suggestions,
    }

    # ── 6. 数据质量 ──
    quality = _data_quality(panel, member_set, coverage_alert_fields)

    overview = {
        "stock_count": len(panel.members),
        "below_min_pool_size": len(panel.members) < R.MIN_POOL_SIZE,
        "avg_market_cap": None if avg_cap is None else round(avg_cap, 2),
        "avg_market_cap_yi": None if avg_cap is None else round(avg_cap / 1e8, 2),
        "market_cap_band_zh": _cap_band_zh(avg_cap),
        "time_range": "待配置",   # Step2 配置后回填（向导 §3.7.4）
        "data_completeness": quality.get("overall_coverage"),
    }

    return {
        "generated_at": _utcnow().isoformat(),
        "as_of_date": panel.as_of_date.isoformat() if panel.as_of_date else None,
        "lookback_days": panel.lookback_days,
        "trade_days_actual": panel.trade_days_actual,
        "avg_daily_symbols": round(panel.avg_daily_symbols, 1),
        "overview": overview,
        "market_cap_distribution": tiers,
        "industry_distribution": industry_dist,
        "industry_concentration": {
            "top_ratio": 1.0,
            "alert": True,
            "alert_zh": "行业数据不可用，无法评估集中度。",
            "threshold": INDUSTRY_CONCENTRATION_ALERT,
        },
        "style_exposure": {
            "dimensions": style_dims,
            "dominant_style": dominant[0] if dominant else None,
            "dominant_score": round(dominant[1], 4) if dominant else None,
            "complement_suggestion_zh": _complement_suggestion(dominant[0])
            if dominant else None,
        },
        "market_environment": market_env,
        "data_quality": quality,
        "warnings": warnings,
    }


def _trend_strength(returns: pd.Series) -> dict[str, Any]:
    """趋势强度：强 / 中 / 弱（区间累计收益的绝对值 + 同向天数占比）。"""
    if returns.empty:
        return {"band": "unavailable", "label_zh": "不可用"}
    cum = float((1.0 + returns).prod() - 1.0)
    up_ratio = float((returns > 0).mean()) if len(returns) else 0.0
    strength = abs(cum)
    band = ("strong" if strength >= 0.10 else "mid" if strength >= 0.04 else "weak")
    return {
        "band": band,
        "label_zh": {"strong": "强", "mid": "中", "weak": "弱"}[band],
        "cumulative_return": round(cum, 6),
        "up_day_ratio": round(up_ratio, 4),
        "basis_zh": "区间累计收益绝对值（≥10% 强 / ≥4% 中 / 其余弱）。",
    }


def _complement_suggestion(dominant: str | None) -> str | None:
    mapping = {
        "momentum": "主导风格是动量，可补充反转/波动率类因子做对冲。",
        "value": "主导风格是价值，可补充成长/动量类因子避免纯低估值陷阱。",
        "quality": "主导风格是质量，可补充价值/动量类因子提升区分度。",
        "volatility": "主导风格是波动（分位越高波动越大），可补充波动率收缩/质量类因子。",
        "growth": "主导风格是成长，可补充价值/质量类因子平衡。",
    }
    return mapping.get(dominant)


def _cap_band_zh(avg_cap: float | None) -> str | None:
    if avg_cap is None:
        return None
    if avg_cap > 5e10:
        return "大盘"
    if avg_cap > 1e10:
        return "中盘"
    return "小盘"


def _data_quality(panel: AnalysisPanel, member_set: set[str],
                  alert_fields: list[str]) -> dict[str, Any]:
    """字段覆盖率（向导 §3.7.4 数据质量区：<80% 标红）。"""
    fields: list[dict[str, Any]] = []

    def _add(name: str, ratio: float, source: str) -> None:
        entry = {"field": name, "coverage": round(ratio, 6),
                 "below_threshold": ratio < COVERAGE_ALERT, "source": source}
        fields.append(entry)
        if entry["below_threshold"]:
            alert_fields.append(name)

    # 行情字段（近窗口）
    if not panel.daily.empty:
        d = panel.daily
        syms = set(d["symbol"].astype(str))
        for col in ("close", "volume", "amount", "turnover_rate"):
            if col in d.columns:
                have = d.loc[d[col].notna(), "symbol"].astype(str).nunique()
                _add(col, have / max(1, len(syms)), "raw_daily_bars")
    else:
        for col in ("close", "volume", "amount", "turnover_rate"):
            _add(col, 0.0, "raw_daily_bars")

    # 估值字段（as_of 截面）
    if not panel.valuation.empty:
        v = panel.valuation
        total = max(1, len(v))
        for col in ("pe_ttm", "pb", "total_market_cap", "circulating_market_cap",
                    "dividend_yield"):
            if col in v.columns:
                _add(col, float(pd.to_numeric(v[col], errors="coerce").notna().mean()),
                     "raw_valuation_snapshots")
    else:
        for col in ("pe_ttm", "pb", "total_market_cap", "dividend_yield"):
            _add(col, 0.0, "raw_valuation_snapshots")

    # 财报字段（PIT）
    if not panel.financials.empty and "roe_ttm" in panel.financials.columns:
        f = panel.financials
        _add("roe_ttm", float(pd.to_numeric(f["roe_ttm"], errors="coerce").notna().mean()),
             "raw_financial_reports")
    else:
        _add("roe_ttm", 0.0, "raw_financial_reports")

    known = [f for f in fields if f["coverage"] > 0]
    overall = float(np.mean([f["coverage"] for f in known])) if known else 0.0
    return {
        "fields": fields,
        "overall_coverage": round(overall, 6),
        "coverage_threshold": COVERAGE_ALERT,
        "below_threshold_fields": alert_fields,
        "note_zh": "覆盖率的分母是**全市场**（风格暴露需要全市场基准）；"
                   "候选池成员的实际覆盖见各自字段。",
    }


# ══════════════════════════════════════════════════════════════
# 装载与对外入口
# ══════════════════════════════════════════════════════════════


def load_analysis_panel(db: Session, *, member_symbols: Sequence[str],
                        as_of_date: date, warehouse: Any | None = None,
                        lookback_days: int = DEFAULT_LOOKBACK_DAYS,
                        ) -> AnalysisPanel:
    """装载分析面板（**全市场**，供风格暴露做基准）。"""
    wh = warehouse if warehouse is not None else R._default_warehouse(db)
    with wh.connection(read_only=True) as conn:
        start = as_of_date - pd.Timedelta(days=int(ANALYSIS_WINDOW_DAYS * 1.6))
        daily = conn.execute(
            "SELECT symbol, trade_date, close, volume, amount, turnover_rate "
            "FROM raw_daily_bars WHERE trade_date >= ? AND trade_date <= ?",
            [start, as_of_date],
        ).fetchdf()
        valuation = R.load_valuation(conn, as_of_date)
        financials = R.load_financials_pit(conn, as_of_date, years=3)

    trade_days = sorted(daily["trade_date"].unique()) if not daily.empty else []
    avg_syms = float(len(daily) / len(trade_days)) if trade_days else 0.0
    return AnalysisPanel(
        daily=daily,
        valuation=valuation,
        financials=financials,
        members=[str(s) for s in member_symbols],
        as_of_date=as_of_date,
        lookback_days=lookback_days,
        trade_days_actual=len(trade_days),
        avg_daily_symbols=avg_syms,
    )


def analyze_pool(
    db: Session, *, pool_id: str, operator_id: str = "system",
    warehouse: Any | None = None, panel: AnalysisPanel | None = None,
    as_of_date: date | None = None,
) -> dict[str, Any]:
    """「生成挖掘物料」：冻结快照 → 分析 → 写看板 → 锁定（向导 §3.7.1 全流程）。

    一步完成（§3.7.6：轻量统计，**同步**计算，不引异步任务）。
    `panel` 参数供测试注入内存面板，避免依赖数仓。
    """
    from app.services.factors.candidate_pool import service as pool_service

    pool = pool_service.get_pool(db, pool_id)
    pool_service._assert_not_locked(db, pool_id, "生成挖掘物料")

    resolved = as_of_date
    if panel is None:
        wh = warehouse if warehouse is not None else R._default_warehouse(db)
        with wh.connection(read_only=True) as conn:
            resolved, _adjusted, _ev = R._resolve_as_of_date(conn, as_of_date)
    if resolved is None:
        # 解析不出 as_of（数仓空）→ 用今天，让分析自己报告"无数据"，而不是在这里崩
        resolved = _utcnow().date()

    # 幂等：若已有「未分析且未锁定」的快照则复用，否则新建。
    # 这让「生成挖掘物料」按钮重复点击不会堆出一堆空快照。
    reusable = next(
        (s for s in pool_service.list_snapshots(db, pool_id)
         if s.analysis_status == pool_service.SNAPSHOT_NOT_ANALYZED
         and not int(s.is_locked or 0)),
        None,
    )
    if reusable is not None:
        snapshot = reusable
        member_symbols = [m["symbol"] for m in json.loads(snapshot.members_json or "[]")]
    else:
        snapshot = pool_service.freeze_snapshot(
            db, pool_id=pool_id,
            data_cutoff_at=datetime.combine(resolved, datetime.min.time()),
            operator_id=operator_id,
        )
        member_symbols = [
            m["symbol"] for m in json.loads(snapshot.members_json or "[]")
        ]

    if panel is None:
        wh = warehouse if warehouse is not None else R._default_warehouse(db)
        panel = load_analysis_panel(db, member_symbols=member_symbols,
                                    as_of_date=resolved, warehouse=wh)
    analysis = build_analysis(panel)

    snapshot = pool_service.mark_analyzed(db, snapshot_id=snapshot.id,
                                          analysis=analysis, operator_id=operator_id)
    detail = pool_service.snapshot_to_dict(db, snapshot)
    return {
        "snapshot_id": snapshot.id,
        "pool_id": pool_id,
        "analysis_status": snapshot.analysis_status,
        "is_locked": bool(snapshot.is_locked),
        "data_cutoff_at": snapshot.data_cutoff_at,
        "analysis": analysis,
        "pool": detail,
    }


__all__ = [
    "STYLE_DIMENSIONS",
    "STYLE_LABELS_ZH",
    "MARKET_CAP_TIERS",
    "VOL_HIGH",
    "VOL_MID",
    "INDUSTRY_CONCENTRATION_ALERT",
    "INDUSTRY_TOP_N",
    "COVERAGE_ALERT",
    "DEFAULT_LOOKBACK_DAYS",
    "MIN_LOOKBACK_DAYS",
    "ANALYSIS_WINDOW_DAYS",
    "AnalysisPanel",
    "build_analysis",
    "load_analysis_panel",
    "analyze_pool",
]
