"""候选池 · 条件筛选分位数预设（SD-v2.0 §6.3 / 向导 §3.2；任务 T09）。

为什么分位数必须**服务端**算
==========================
向导 §3.2 对左栏四个预设选项卡都写死了同一条约束：

> 阈值必须由后端按**当日目标市场可用样本**分位数计算并连同版本写入快照，
> **不能把固定金额硬编码成跨时期标准**。

理由很实在：「大盘/小盘」是**相对**概念。2018 年 A 股 50 亿市值算中盘，
2026 年可能只能算微盘；把「50 亿」硬编码进前端，同一套规则在不同年份会筛出
完全不同的池子，而快照里记的却是同一个数字 —— 复现性直接失效。

所以本模块：
1. 拉取 `as_of_date` 当日的目标市场样本，用 pandas 在服务端算分位数；
2. 把 `quantile_bounds`（各分位点绝对值）、`sample_scope`（样本范围与样本量）、
   `as_of_date`、`data_version` 一并返回；
3. **不返回任何写死的金额** —— 前端只能拿服务端返回的边界去填输入框。

DoD 里「分位数必须服务端算，前端不得自行计算或缓存筛选」对应的可验证点：
`test_rules_presets.py` 断言同一份规则在两个不同 `as_of_date` 下得到**不同**边界值，
且边界严格等于当日样本的 `pandas.quantile`（逐条比对）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Mapping, Sequence

import pandas as pd
from sqlalchemy.orm import Session

from app.services.factors.candidate_pool import rules as R

# ══════════════════════════════════════════════════════════════
# 常量
# ══════════════════════════════════════════════════════════════

#: 流动性的默认统计窗口（向导 §3.2：N 默认 20，允许调整）
DEFAULT_PRESET_WINDOW = 20

MARKET_CAP_GROUP = "market_cap"
VALUATION_GROUP = "valuation"
LIQUIDITY_GROUP = "liquidity"
LISTING_GROUP = "listing"

GROUP_LABELS_ZH: dict[str, str] = {
    MARKET_CAP_GROUP: "市值",
    VALUATION_GROUP: "估值",
    LIQUIDITY_GROUP: "流动性",
    LISTING_GROUP: "上市时间",
}
GROUP_ORDER: tuple[str, ...] = (MARKET_CAP_GROUP, VALUATION_GROUP, LIQUIDITY_GROUP,
                                LISTING_GROUP)

#: 每个预设组的**分位数切点**。
#: 集中在一处便于评审与复现；改动会改变所有预设边界，属契约变更。
PRESET_QUANTILES: dict[str, tuple[float, ...]] = {
    # 市值：微盘 <p10 ≤ 小盘 <p30 ≤ 中盘 <p70 ≤ 大盘
    MARKET_CAP_GROUP: (0.10, 0.30, 0.70),
    # 估值：低 ≤p20 < 中低 ≤p40 < 中等 ≤p60 < 高
    VALUATION_GROUP: (0.20, 0.40, 0.60),
    # 流动性：低 <p30 ≤ 中等 <p70 ≤ 高
    LIQUIDITY_GROUP: (0.30, 0.70),
    LISTING_GROUP: (),
}

#: 需要「非正值排除」的字段（向导 §3.2：PE/PB 非正值排除）
POSITIVE_ONLY_FIELDS: frozenset[str] = frozenset({"pe_ttm", "pb"})

#: 每个预设组主字段（多字段时逐个生成一套预设）
GROUP_FIELDS: dict[str, tuple[str, ...]] = {
    MARKET_CAP_GROUP: ("total_market_cap",),
    VALUATION_GROUP: ("pe_ttm", "pb"),
    LIQUIDITY_GROUP: ("avg_amount",),
    LISTING_GROUP: ("min_listed_trading_days",),
}

_OPERATOR_ALL = "all"
_OPERATOR_GE = "ge"
_OPERATOR_LE = "le"
_OPERATOR_BETWEEN = "between"

#: 「全量/不限」预设的标签（按组区分，避免出现「全总市值」这类读不通的词）
ALL_PRESET_LABEL_ZH: dict[str, str] = {
    MARKET_CAP_GROUP: "全市场",
    VALUATION_GROUP: "不限",
    LIQUIDITY_GROUP: "不限",
    LISTING_GROUP: "不限",
}

#: 相邻预设**共享边界值**（双向含端点）—— 与向导 §3.3「所有区间均包含端点」一致。
#: 共享点只是单个数值（零测度），不会造成实质重复；换来的是「每个预设 = {min, max}」
#: 的统一形态，前端只需填两个输入框，不需要维护开闭区间表。
SHARED_ENDPOINT_NOTE_ZH = "相邻档位共享边界值（区间均含端点）"


# ══════════════════════════════════════════════════════════════
# 输出类型
# ══════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class Preset:
    """单个预设项（向导 §3.2：「预设接口返回 preset_code、字段、运算符、计算口径、
    样本范围、分位数边界、as_of_date 和数据版本」）。"""

    preset_code: str
    group: str
    group_label_zh: str
    label_zh: str
    field: str | None
    operator: str
    min_value: float | None
    max_value: float | None
    basis_zh: str
    sample_scope: dict[str, Any] = field(default_factory=dict)
    quantile_bounds: dict[str, float] = field(default_factory=dict)
    as_of_date: date | None = None
    data_version: dict[str, Any] = field(default_factory=dict)
    availability: str = "available"
    blocked_reason_zh: str | None = None
    note_zh: str | None = None

    @property
    def applyable(self) -> bool:
        return self.availability == "available"

    def to_dict(self) -> dict[str, Any]:
        return {
            "preset_code": self.preset_code,
            "group": self.group,
            "group_label_zh": self.group_label_zh,
            "label_zh": self.label_zh,
            "field": self.field,
            "field_label_zh": (R.FIELD_BINDINGS[self.field].label_zh
                               if self.field in R.FIELD_BINDINGS else None),
            "operator": self.operator,
            "min_value": self.min_value,
            "max_value": self.max_value,
            "inclusive": True,
            "basis_zh": self.basis_zh,
            "sample_scope": self.sample_scope,
            "quantile_bounds": self.quantile_bounds,
            "as_of_date": self.as_of_date.isoformat() if self.as_of_date else None,
            "data_version": self.data_version,
            "availability": self.availability,
            "blocked_reason_zh": self.blocked_reason_zh,
            "note_zh": self.note_zh,
            "applyable": self.applyable,
        }


@dataclass
class PresetResponse:
    """预设接口响应。"""

    as_of_date: date | None
    as_of_requested: date | None
    as_of_date_adjusted: bool
    markets: tuple[str, ...]
    window_days: int
    window_actual_days: int
    presets: list[Preset] = field(default_factory=list)
    data_version: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    as_of_evidence: dict[str, Any] = field(default_factory=dict)

    def by_code(self, preset_code: str) -> Preset | None:
        for p in self.presets:
            if p.preset_code == preset_code:
                return p
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of_date": self.as_of_date.isoformat() if self.as_of_date else None,
            "as_of_requested": self.as_of_requested.isoformat() if self.as_of_requested else None,
            "as_of_date_adjusted": self.as_of_date_adjusted,
            "markets": list(self.markets),
            "window_days": self.window_days,
            "window_actual_days": self.window_actual_days,
            "quantile_spec": {k: list(v) for k, v in PRESET_QUANTILES.items()},
            "groups": [{"group": g, "label_zh": GROUP_LABELS_ZH[g]} for g in GROUP_ORDER],
            "presets": [p.to_dict() for p in self.presets],
            "data_version": self.data_version,
            "as_of_evidence": self.as_of_evidence,
            "warnings": self.warnings,
        }


# ══════════════════════════════════════════════════════════════
# 分位数计算（服务端唯一实现）
# ══════════════════════════════════════════════════════════════


def compute_quantile_bounds(values: pd.Series,
                            quantiles: Sequence[float]) -> dict[str, float]:
    """按分位点计算边界值。**服务端计算的唯一入口**。

    使用 `pandas.Series.quantile`（线性插值），与前端任何近似实现都会不同 ——
    这正是「前端不得自行计算」要防的：两套插值口径会给出不同阈值，
    而快照里只记数字，事后无法判断是谁算的。
    """
    numeric = pd.to_numeric(values, errors="coerce").dropna()
    out: dict[str, float] = {}
    for q in quantiles:
        if numeric.empty:
            continue
        out[f"p{int(round(q * 100)):02d}"] = float(numeric.quantile(q))
    return out


def _sample_series(panel: R.ScreeningPanel, field_name: str, *,
                   positive_only: bool, universe_symbols: Sequence[str] | None,
                   window_days: int | None = None) -> pd.Series:
    """取出某个字段在「目标市场可用样本」上的取值序列。

    目标市场 = 已按 `markets` 筛过的 universe（见 `get_filter_presets`）。
    """
    if field_name in R._VALUATION_COLUMNS:
        frame = panel.valuation
        column = field_name
    elif field_name in ("avg_amount", "avg_volume", "avg_turnover_rate"):
        frame = panel.liquidity
        column = field_name
    else:
        return pd.Series(dtype="float64")

    if frame is None or frame.empty or column not in frame.columns:
        return pd.Series(dtype="float64")
    sub = frame
    if universe_symbols is not None:
        sub = sub[sub["symbol"].isin(list(universe_symbols))]
    series = pd.to_numeric(sub[column], errors="coerce")
    if positive_only:
        series = series[series > 0]
    return series


# ══════════════════════════════════════════════════════════════
# 预设构造
# ══════════════════════════════════════════════════════════════


def _basis_zh(group: str, field_name: str, window_days: int,
              positive_only: bool) -> str:
    binding = R.FIELD_BINDINGS.get(field_name)
    label = binding.label_zh if binding else field_name
    if group == MARKET_CAP_GROUP:
        return (f"{label}：按数据截止日可见快照取值，"
                "阈值 = 当日目标市场**正值**样本的分位数（非硬编码金额）")
    if group == VALUATION_GROUP:
        extra = "，**非正值样本已排除**（向导 §3.2）" if positive_only else ""
        return (f"{label}：按数据截止日可见快照取值，"
                f"阈值 = 当日目标市场**正值**样本的分位数{extra}")
    if group == LIQUIDITY_GROUP:
        return (f"{label}：最近 {window_days} 个有效交易日的日均值"
                "（停牌日不计入窗口），阈值按目标市场样本分位数计算，不使用单一绝对金额")
    if group == LISTING_GROUP:
        return f"{label}：上市日至数据截止日的实际交易日数（不以自然日替代）"
    return label


def _make_aligned_presets(*, group: str, field_name: str, bounds: dict[str, float],
                          scope: dict[str, Any], window_days: int,
                          as_of_date: date | None, data_version: Mapping[str, Any],
                          positive_only: bool, note_zh: str | None = None) -> list[Preset]:
    """按分位切点生成「低 → 高」的预设序列。

    区间语义统一为**两端含端点**（向导 §3.3），`operator` 只取
    `all` / `le` / `between` / `ge` 四种 —— 相邻档位共享边界值，
    前端因此只需把 `{min_value, max_value}` 填进两个输入框即可。
    """
    label = R.FIELD_BINDINGS[field_name].label_zh if field_name in R.FIELD_BINDINGS \
        else field_name
    basis = _basis_zh(group, field_name, window_days, positive_only)
    common = {
        "group": group,
        "group_label_zh": GROUP_LABELS_ZH[group],
        "sample_scope": scope,
        "quantile_bounds": dict(bounds),
        "as_of_date": as_of_date,
        "data_version": dict(data_version),
        "field": field_name,
        "basis_zh": basis,
    }
    items: list[Preset] = []

    # ① 不限（无边界）
    items.append(Preset(preset_code=f"{group}.{field_name}.all",
                        label_zh=ALL_PRESET_LABEL_ZH.get(group, "不限"),
                        operator=_OPERATOR_ALL, min_value=None, max_value=None,
                        note_zh=None, **common))

    qs = sorted(bounds.items(), key=lambda kv: kv[0])
    if len(qs) < 2:
        return items

    def _mk(code: str, label_zh: str, lo: float | None, hi: float | None,
            extra_note: str | None = None) -> Preset:
        if lo is None:
            operator = _OPERATOR_LE
        elif hi is None:
            operator = _OPERATOR_GE
        else:
            operator = _OPERATOR_BETWEEN
        note = extra_note or note_zh
        return Preset(preset_code=f"{group}.{field_name}.{code}", label_zh=label_zh,
                      operator=operator, min_value=lo, max_value=hi,
                      note_zh=note, **common)

    if group == MARKET_CAP_GROUP and len(qs) == 3:
        p10, p30, p70 = qs[0][1], qs[1][1], qs[2][1]
        items += [
            _mk("micro", "微盘", None, p10),
            _mk("small", "小盘", p10, p30),
            _mk("mid", "中盘", p30, p70),
            _mk("large", "大盘", p70, None),
        ]
        return items

    if group == VALUATION_GROUP and len(qs) == 3:
        p20, p40, p60 = qs[0][1], qs[1][1], qs[2][1]
        items += [
            _mk("low", f"低{label}", None, p20),
            _mk("mid_low", f"中低{label}", p20, p40),
            _mk("mid", f"中等{label}", p40, p60),
            _mk("high", f"高{label}", p60, None),
        ]
        return items

    if group == LIQUIDITY_GROUP and len(qs) == 2:
        p30, p70 = qs[0][1], qs[1][1]
        items += [
            _mk("low", "低流动性", None, p30),
            _mk("mid", "中等流动性", p30, p70),
            _mk("high", "高流动性", p70, None),
        ]
        return items

    return items


def _listing_presets(*, as_of_date: date | None, data_version: Mapping[str, Any],
                     scope: dict[str, Any]) -> list[Preset]:
    """上市时间预设 —— **全部阻断**（实测 `listed_at` 两表均全空）。

    向导 §3.3 明令：「状态来源不足时**不得以当前主表无提示替代**」。
    因此这里既不猜、也不用 `last_bar_date` / `bar_count` 之类代替品 ——
    只如实报告阻断与原因，让上游去补数据。
    """
    binding = R.FIELD_BINDINGS["min_listed_trading_days"]
    common = {
        "group": LISTING_GROUP,
        "group_label_zh": GROUP_LABELS_ZH[LISTING_GROUP],
        "field": "min_listed_trading_days",
        "sample_scope": scope,
        "quantile_bounds": {},
        "as_of_date": as_of_date,
        "data_version": dict(data_version),
        "availability": "blocked",
        "blocked_reason_zh": binding.blocked_reason_zh,
        "basis_zh": _basis_zh(LISTING_GROUP, "min_listed_trading_days", 0, False),
        "operator": _OPERATOR_GE,
        "min_value": None,
        "max_value": None,
    }
    # 向导 §3.2 的四个档位 + 默认 120 交易日
    return [
        Preset(preset_code=f"{LISTING_GROUP}.d120", label_zh="已上市满 120 个交易日（默认）",
               **common),
        Preset(preset_code=f"{LISTING_GROUP}.m6", label_zh="已上市满 6 个月", **common),
        Preset(preset_code=f"{LISTING_GROUP}.y1", label_zh="已上市满 1 年", **common),
        Preset(preset_code=f"{LISTING_GROUP}.y3", label_zh="已上市满 3 年", **common),
        Preset(preset_code=f"{LISTING_GROUP}.y5", label_zh="已上市满 5 年", **common),
    ]


# ══════════════════════════════════════════════════════════════
# 对外主入口
# ══════════════════════════════════════════════════════════════


def get_filter_presets(db: Session, *, as_of_date: date | None = None,
                       markets: Sequence[str] | None = None,
                       window_days: int = DEFAULT_PRESET_WINDOW,
                       warehouse: Any | None = None,
                       panel: R.ScreeningPanel | None = None,
                       data_version: Mapping[str, Any] | None = None,
                       ) -> PresetResponse:
    """返回左侧预设栏的全部预设（SD-v2.0 §6.3 关键符号 `get_filter_presets`）。

    `panel` / `data_version` 参数用于测试与复用：传入现成面板则跳过全部 IO，
    这样分位数逻辑可以在**毫秒级**被覆盖，且能精确断言「边界 == 当日样本分位数」。
    """
    market_tuple: tuple[str, ...] = tuple()
    if markets:
        for m in markets:
            key = str(m).strip().lower()
            if key not in R.SUPPORTED_MARKETS:
                raise R._validation_error(
                    f"市场代码 {m!r} 不是首期支持的 A 股市场。"
                    f"首期仅支持 {list(R.SUPPORTED_MARKETS)}。",
                    reason=R.REASON_UNSUPPORTED_MARKET,
                    value=str(m),
                    allowed=list(R.SUPPORTED_MARKETS),
                )
            if key not in market_tuple:
                market_tuple = (*market_tuple, key)

    if not isinstance(window_days, int) or isinstance(window_days, bool) or \
            window_days < 1 or window_days > R.MAX_LIQUIDITY_WINDOW:
        raise R._validation_error(
            f"window_days 必须在 1~{R.MAX_LIQUIDITY_WINDOW} 之间，收到 {window_days!r}。",
            reason=R.REASON_INVALID_RULE,
        )

    warnings: list[str] = []
    resolved: date | None = None
    adjusted = False
    as_of_evidence: dict[str, Any] = {}
    version: dict[str, Any] = dict(data_version or {})
    actual_days = 0
    universe_symbols: list[str] | None = None

    if panel is None:
        wh = warehouse if warehouse is not None else R._default_warehouse(db)
        with wh.connection(read_only=True) as conn:
            resolved, adjusted, as_of_evidence = R._resolve_as_of_date(conn, as_of_date)
            if not version:
                version = R._data_version(conn)
        if resolved is not None:
            universe_rules = R.CompiledRules(markets=market_tuple)
            panel = R.load_screening_panel(db, universe_rules, as_of_date=resolved,
                                           warehouse=wh)
            actual_days = panel.liquidity_days_actual
    if panel is not None:
        if resolved is None:
            resolved = as_of_date or date.today()
        universe_symbols = panel.universe["symbol"].astype(str).tolist()
        actual_days = actual_days or panel.liquidity_days_actual

    if adjusted and as_of_date is not None and resolved is not None:
        warnings.append(
            f"请求的数据截止日 {as_of_date.isoformat()} 不可用或不完整，"
            f"已回退到最近完整交易日 {resolved.isoformat()}。"
        )
    if actual_days and actual_days < window_days:
        warnings.append(
            f"流动性预设请求 {window_days} 日窗口，数仓实际只有 {actual_days} 个交易日"
            "（日线起点 2025-01-13）；日均值按实际天数计算。"
        )

    scope_base: dict[str, Any] = {
        "markets": list(market_tuple) or ["cn"],
        "as_of_date": resolved.isoformat() if resolved else None,
        "universe_size": len(universe_symbols) if universe_symbols is not None else 0,
        "window_days": window_days,
        "window_actual_days": actual_days,
    }

    presets: list[Preset] = []

    # ① 市值
    for field_name in GROUP_FIELDS[MARKET_CAP_GROUP]:
        series = _sample_series(panel, field_name, positive_only=True,
                                universe_symbols=universe_symbols)
        bounds = compute_quantile_bounds(series, PRESET_QUANTILES[MARKET_CAP_GROUP])
        scope = {**scope_base, "sample_size": int(series.notna().sum())}
        presets += _make_aligned_presets(
            group=MARKET_CAP_GROUP, field_name=field_name, bounds=bounds,
            scope=scope, window_days=window_days,
            as_of_date=resolved, data_version=version, positive_only=True,
            note_zh=SHARED_ENDPOINT_NOTE_ZH,
        )

    # ② 估值（PE_TTM / PB 各一套，均排除非正值）
    for field_name in GROUP_FIELDS[VALUATION_GROUP]:
        series = _sample_series(panel, field_name, positive_only=True,
                                universe_symbols=universe_symbols)
        bounds = compute_quantile_bounds(series, PRESET_QUANTILES[VALUATION_GROUP])
        scope = {**scope_base, "sample_size": int(series.notna().sum()),
                 "positive_only": True}
        note = ("已排除非正值样本（PE/PB ≤ 0 不参与分位与筛选）；"
                + SHARED_ENDPOINT_NOTE_ZH)
        presets += _make_aligned_presets(
            group=VALUATION_GROUP, field_name=field_name, bounds=bounds,
            scope=scope, window_days=window_days,
            as_of_date=resolved, data_version=version, positive_only=True,
            note_zh=note,
        )

    # ③ 流动性
    for field_name in GROUP_FIELDS[LIQUIDITY_GROUP]:
        series = _sample_series(panel, field_name, positive_only=False,
                                universe_symbols=universe_symbols)
        bounds = compute_quantile_bounds(series, PRESET_QUANTILES[LIQUIDITY_GROUP])
        scope = {**scope_base, "sample_size": int(series.notna().sum())}
        presets += _make_aligned_presets(
            group=LIQUIDITY_GROUP, field_name=field_name, bounds=bounds,
            scope=scope, window_days=window_days,
            as_of_date=resolved, data_version=version, positive_only=False,
            note_zh=SHARED_ENDPOINT_NOTE_ZH,
        )

    # ④ 上市时间（全部阻断）
    presets += _listing_presets(as_of_date=resolved, data_version=version,
                                scope=scope_base)

    if panel is None or panel.universe.empty:
        warnings.append(
            "未取到任何 universe 样本（`universe_symbols` 中 asset_type=stock / "
            "region=cn / is_synced=1 为空，或数仓不可用）；分位数预设全部为 N/A。"
        )

    return PresetResponse(
        as_of_date=resolved,
        as_of_requested=as_of_date,
        as_of_date_adjusted=adjusted,
        markets=market_tuple,
        window_days=window_days,
        window_actual_days=actual_days,
        presets=presets,
        data_version=version,
        warnings=warnings,
        as_of_evidence=as_of_evidence,
    )


__all__ = [
    "DEFAULT_PRESET_WINDOW",
    "MARKET_CAP_GROUP",
    "VALUATION_GROUP",
    "LIQUIDITY_GROUP",
    "LISTING_GROUP",
    "GROUP_LABELS_ZH",
    "GROUP_ORDER",
    "PRESET_QUANTILES",
    "POSITIVE_ONLY_FIELDS",
    "ALL_PRESET_LABEL_ZH",
    "SHARED_ENDPOINT_NOTE_ZH",
    "Preset",
    "PresetResponse",
    "compute_quantile_bounds",
    "get_filter_presets",
]
