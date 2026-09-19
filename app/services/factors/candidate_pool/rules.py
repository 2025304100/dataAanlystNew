"""候选池 · 条件筛选规则编译与预览（SD-v2.0 §6.3 / 向导 §3.2~§3.5；任务 T09）。

职责边界
========
✅ 条件规则**编译**：JSON → 结构化条件对象（`CompiledRules`），含范围冲突/未知值校验
✅ 条件规则**执行**：纯函数 `run_screening(rules, panel)`，pandas 布尔掩码
✅ **预览**：`preview_filter(...)` → 命中/排除统计 + 示例 + 结构化阻断项（**不落库**）
✅ **生成前硬校验**：`assert_pool_generatable(...)` → 命中 <50 抛 `MINING_POOL_TOO_SMALL`
✅ 数据装载：`load_screening_panel(...)`（MySQL `universe_symbols` + DuckDB 数仓）

❌ 不做：分位数预设 → `presets.py`；成员写入 → `service.py`（T08）；快照/看板 → T11

「禁止拼接 SQL」怎么落地
======================
规则编译的输出是 **dataclass**，执行阶段是 **pandas 掩码**。用户值永远不作为
SQL 文本的一部分：DuckDB 查询只用常量标识符 + `?` 参数绑定。
`test_rules_presets.py` 里有注入测试断言「恶意字符串不改变执行的 SQL 结构」。

数据现状基线（2026-09-16 实测，**不是猜测**）
==========================================
本模块的可用性结论全部来自对真实库的实测。踩过的坑（`prev_close` / `dividend_yield`）
说明「列存在」≠「有数据」，因此这里对每个字段都标注实测覆盖率并给出阻断理由。

| 筛选项 | 来源 | 实测 | 结论 |
|---|---|---|---|
| A 股 universe | `universe_symbols`（asset_type=stock, region=cn, is_synced=1） | 5,554 | ✅ |
| 板块（沪主板/深主板/创业板/科创板/北交所） | `universe_symbols.market`+`.board` | sh_main 1700 / sz_main 1498 / gem 1403 / star 615 / bj 339 | ✅ |
| ST / 退市风险 | `universe_symbols.name` 模式匹配 | `is_st` 列**全为 0**；`security_status_daily` 是 **e2e fixture 残留**（`status_source` 只有 `T26_FIXTURE`/`T30_FIXTURE`） | ⚠️ 仅「当前名称」语义 |
| 停牌 / 指数成分 | — | 无字段、无事件表 | ❌ 阻断 |
| 上市满 N 交易日 | `symbols.listed_at` / `universe_symbols.listed_at` | **两边均 0 行非空** | ❌ 阻断 |
| 总市值 / 流通市值 | DuckDB `raw_valuation_snapshots` | 100% 非空、100% 正值；但**每日仅 ~1,904 只** | ✅（覆盖面需报告） |
| PE_TTM | 同上 | 100% 非空，**80.9% 正值** | ✅ |
| PB | 同上 | 100% 非空，99.6% 正值 | ✅ |
| 股息率 | 同上 | **0 行非空（全 NULL）** | ❌ 阻断 |
| 流动性（日均成交额/量/换手率） | DuckDB `raw_daily_bars` | 100% 非空；**仅 401 个交易日**（2025-01-13 起） | ✅（窗口需回退并报告实际天数） |
| ROE_TTM | DuckDB `raw_financial_reports`（PIT: `announcement_date <= as_of`） | 83.2% 非空 | ✅ |
| 净利润 / 净利润同比（近三年亏损） | 同上 | **均 0 行非空** | ❌ 阻断 |
| 行业 | `symbols.industry` / `universe_symbols.industry` | **两边均 0 行非空** | ❌ 阻断 |

为什么 `as_of_date` 不能取 `MAX(trade_date)`
==========================================
实测：`raw_daily_bars` 最新日 **2026-09-07 只有 1 个标的**，而 `raw_valuation_snapshots`
最新日是 **2026-09-04（1,904 只）**。若按行情取最新日，估值类条件会在一个「只有 1 只股票」
的日子上求值 → 命中 1 只 → 被误判为「条件太严」，用户完全无从下手。

故本模块用 `_resolve_as_of_date()`：在**估值快照**上取「标的数 ≥ 近 20 个日期中位数 × 0.9」
的最新日期（0.9 阈值与项目既有 `trade_calendar.latest_complete_trade_date` 一致），
并在结果里显式回报 `as_of_date_adjusted` / `as_of_requested`。
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Mapping, Sequence

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.hash_utils import canonical_json
from app.models.symbol import Symbol
from app.models.universe import UniverseSymbol
from app.schemas.errors import FactorSevenError

# ══════════════════════════════════════════════════════════════
# 常量
# ══════════════════════════════════════════════════════════════

#: 候选池有效标的**硬下限**（需求 §3.6 / 向导 §3.5：50 为系统硬下限，前端不可降低）。
#: **单一事实源在 ** —— 这里只是转出口，保持既有导入路径
#: `rules.MIN_POOL_SIZE` 不变（SD-v2.0 §3.1 冻结契约引用的就是这个名字）。
from app.services.factors.candidate_pool.service import MIN_POOL_SIZE  # noqa: E402

#: 首期仅支持 A 股市场（向导 §3.3：出现港股/美股/未知市场代码时逐条拒绝）
SUPPORTED_MARKETS: tuple[str, ...] = ("sh", "sz", "bj", "cn")

#: 板块代码 → (market, board) 物理映射。
#: ⚠️ **必须用 `universe_symbols` 而非 `symbols`**：实测 `symbols.board` 的
#:    gem 仅 4 只、star 仅 6 只（脏数据），而 `universe_symbols.board` 是
#:    gem 1,403 / star 615（与真实市场结构一致）。
BOARD_TO_PHYSICAL: dict[str, tuple[str, str]] = {
    "sh_main": ("sh", "main"),   # 上证主板
    "sz_main": ("sz", "main"),   # 深证主板
    "gem": ("sz", "gem"),        # 创业板
    "star": ("sh", "star"),      # 科创板
    "bj": ("bj", "bj"),          # 北交所
}
BOARD_LABELS_ZH: dict[str, str] = {
    "sh_main": "上证主板",
    "sz_main": "深证主板",
    "gem": "创业板",
    "star": "科创板",
    "bj": "北交所",
}
ALL_BOARD_CODES: tuple[str, ...] = tuple(BOARD_TO_PHYSICAL)

#: 缺失值处理
MISSING_EXCLUDE = "exclude"
MISSING_KEEP = "keep"
VALID_MISSING_POLICIES: frozenset[str] = frozenset({MISSING_EXCLUDE, MISSING_KEEP})

#: 近三年亏损判定模式（向导 §3.4）
LOSS_MODE_ANY = "any"                        # 任一年亏损即排除
LOSS_MODE_ALL_CONSECUTIVE = "all_consecutive"  # 连续 N 年亏损才排除
LOSS_MODE_CUMULATIVE = "cumulative"          # 近 N 年累计净利润 < 0 才排除
VALID_LOSS_MODES: tuple[str, ...] = (
    LOSS_MODE_ANY,
    LOSS_MODE_ALL_CONSECUTIVE,
    LOSS_MODE_CUMULATIVE,
)
DEFAULT_LOSS_YEARS = 3

#: 流动性窗口缺省与上限（向导 §3.2：5/10/20/60 或自定义整数）
DEFAULT_LIQUIDITY_WINDOW = 20
MAX_LIQUIDITY_WINDOW = 250

#: 被引用字段在**基础 universe** 上可用率低于此值 → 阻断（向导 §3.5「关键字段覆盖率不足」）
#: 取 0.20：既能拦住「全 NULL」字段（0%），又不会拦住在用的估值字段（~34%）。
MIN_REFERENCED_FIELD_COVERAGE = 0.20

#: 可用率低于此值但高于阻断线 → **告警不阻断**。
#: 理由：ROE_TTM 在 2026-08-21 截面上只有 20~31% 可得（财报公告日 PIT 决定），
#: 它是向导的一等条件，直接阻断会让功能整体不可用；但 80% 标的被静默排除
#: 是系统性偏差，必须让用户看见。故「低覆盖告警 + 高覆盖静默」。
LOW_COVERAGE_WARNING = 0.50

#: `as_of_date` 的横截面完整度阈值（与 `trade_calendar.latest_complete_trade_date` 一致）
CROSS_SECTION_COMPLETENESS = 0.9
CROSS_SECTION_BASELINE_DAYS = 20

#: 预览返回的示例标的条数（向导 §3.5「示例代码」）
PREVIEW_SAMPLE_SIZE = 10

#: 筛选分类（顺序与向导 §3.2 的 Accordion 一致）
CATEGORY_UNIVERSE = "market_status"
CATEGORY_RISK = "risk"
CATEGORY_VALUATION = "valuation"
CATEGORY_PROFITABILITY = "profitability"
CATEGORY_LIQUIDITY = "liquidity"
CATEGORY_LISTING = "listing"
CATEGORY_INDUSTRY = "industry"

CATEGORY_LABELS_ZH: dict[str, str] = {
    CATEGORY_UNIVERSE: "市场与交易状态",
    CATEGORY_RISK: "风险标记",
    CATEGORY_VALUATION: "估值与市值",
    CATEGORY_PROFITABILITY: "盈利质量",
    CATEGORY_LIQUIDITY: "流动性",
    CATEGORY_LISTING: "上市时间",
    CATEGORY_INDUSTRY: "行业",
}
CATEGORY_ORDER: tuple[str, ...] = (
    CATEGORY_UNIVERSE,
    CATEGORY_RISK,
    CATEGORY_VALUATION,
    CATEGORY_PROFITABILITY,
    CATEGORY_LIQUIDITY,
    CATEGORY_LISTING,
    CATEGORY_INDUSTRY,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ══════════════════════════════════════════════════════════════
# 阻断原因码（放进 `extras.reason`；不新增错误码 —— T09 无 errors.py 写权限）
# ══════════════════════════════════════════════════════════════

REASON_EMPTY_RESULT = "EMPTY_RESULT"
REASON_POOL_TOO_SMALL = "POOL_TOO_SMALL"
REASON_FIELD_UNAVAILABLE = "FIELD_UNAVAILABLE"
REASON_FIELD_COVERAGE_INSUFFICIENT = "FIELD_COVERAGE_INSUFFICIENT"
REASON_RANGE_CONFLICT = "RANGE_CONFLICT"
REASON_UNSUPPORTED_MARKET = "UNSUPPORTED_MARKET"
REASON_UNSUPPORTED_BOARD = "UNSUPPORTED_BOARD"
REASON_CROSS_MARKET_INCOMPLETE = "CROSS_MARKET_INCOMPLETE"
REASON_WAREHOUSE_UNAVAILABLE = "WAREHOUSE_UNAVAILABLE"
REASON_INVALID_RULE = "INVALID_RULE"


# ══════════════════════════════════════════════════════════════
# 字段绑定表（可用性结论来自实测，见模块 docstring 的表）
# ══════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class FieldBinding:
    """筛选字段 → 物理落点 + 实测可用性。"""

    field: str
    label_zh: str
    category: str
    source: str                    # universe | valuation | liquidity | financial
    physical_table: str
    physical_column: str | None
    blocked: bool = False
    blocked_reason_zh: str | None = None
    #: 该字段的语义是否为「当前快照」而非 point-in-time（必须在 UI 与快照里标注）
    point_in_time: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "label_zh": self.label_zh,
            "category": self.category,
            "category_label_zh": CATEGORY_LABELS_ZH.get(self.category, self.category),
            "source": self.source,
            "physical_table": self.physical_table,
            "physical_column": self.physical_column,
            "point_in_time": self.point_in_time,
            "availability": "blocked" if self.blocked else "available",
            "blocked_reason_zh": self.blocked_reason_zh,
        }


_BLOCKED_LISTED_AT = (
    "实测 `symbols.listed_at` 与 `universe_symbols.listed_at` **均为 0 行非空**（全 NULL）。"
    "向导 §3.3 明确要求「上市日使用状态快照中的上市日；状态来源不足时**不得以当前主表"
    "无提示替代**」，故该条件不可用。需先补采上市日（或由首个交易日回推并显式标注口径）。"
)
_BLOCKED_INDUSTRY = (
    "实测 `symbols.industry` 与 `universe_symbols.industry` **均为 0 行非空**（全 NULL）。"
    "行业分类尚未建设（向导 §3.2 也声明「当前行业不具备历史分类版本」）。"
)
_BLOCKED_DIVIDEND_YIELD = (
    "实测 `raw_valuation_snapshots.dividend_yield` **3,079,528 行全部为 NULL**（列存在但从未采集）。"
)
_BLOCKED_NET_PROFIT = (
    "实测 `raw_financial_reports.net_profit` **54,789 行全部为 NULL**。"
    "缺了它「近 N 年亏损」无法判定 —— 而缺失年度不等于盈利（向导 §3.4），"
    "故不支持降级为「用其它字段推断亏损」。"
)
_BLOCKED_NET_PROFIT_YOY = (
    "实测 `raw_financial_reports.net_profit_yoy` **54,789 行全部为 NULL**。"
)
_BLOCKED_SUSPENDED = (
    "停牌状态无字段、无事件表。`security_status_daily` 不可用（见 `_BLOCKED_STATUS_TABLE`）。"
)
_BLOCKED_INDEX_MEMBER = "指数成分表尚未建设（`index_constituents` 类表不存在）。"

#: `security_status_daily` 为什么不能用（这是个需要上报的数据卫生问题）
_BLOCKED_STATUS_TABLE = (
    "`security_status_daily` 仅有 4,132 行且**全部来自 e2e 测试 fixture**："
    "`status_source` 只有 `T26_FIXTURE` / `T30_FIXTURE`，`as_of_batch_id` 为 "
    "`T26_BATCH` / `T30_BATCH_v5`，`listing_date` 只有 2 个不同取值，"
    "且 `is_st` / `is_suspended` / `is_delisting_period` 三者取值分布完全相同（3332/800）。"
    "把它当真实数据源会让「排除 ST/停牌」变成随机命中，故显式不用。"
)

FIELD_BINDINGS: dict[str, FieldBinding] = {
    # ── 1. 市场与交易状态 ──
    "board": FieldBinding("board", "板块", CATEGORY_UNIVERSE, "universe",
                          "universe_symbols", "board"),
    "market": FieldBinding("market", "市场", CATEGORY_UNIVERSE, "universe",
                           "universe_symbols", "market"),
    "index_member": FieldBinding("index_member", "指数成分", CATEGORY_UNIVERSE, "universe",
                                 None, None, blocked=True,
                                 blocked_reason_zh=_BLOCKED_INDEX_MEMBER),
    "exclude_suspended": FieldBinding("exclude_suspended", "排除停牌", CATEGORY_UNIVERSE,
                                      "universe", None, None, blocked=True,
                                      blocked_reason_zh=_BLOCKED_SUSPENDED),
    "exclude_delisting": FieldBinding("exclude_delisting", "排除退市整理/已退市",
                                      CATEGORY_UNIVERSE, "universe", "universe_symbols",
                                      "name", point_in_time=False),
    # ── 2. 风险标记 ──
    "exclude_st": FieldBinding("exclude_st", "排除 ST/*ST", CATEGORY_RISK, "universe",
                               "universe_symbols", "name", point_in_time=False),
    # ── 3. 估值与市值 ──
    "total_market_cap": FieldBinding("total_market_cap", "总市值", CATEGORY_VALUATION,
                                     "valuation", "raw_valuation_snapshots",
                                     "total_market_cap"),
    "circulating_market_cap": FieldBinding("circulating_market_cap", "流通市值",
                                           CATEGORY_VALUATION, "valuation",
                                           "raw_valuation_snapshots",
                                           "circulating_market_cap"),
    "pe_ttm": FieldBinding("pe_ttm", "PE_TTM", CATEGORY_VALUATION, "valuation",
                           "raw_valuation_snapshots", "pe_ttm"),
    "pb": FieldBinding("pb", "PB", CATEGORY_VALUATION, "valuation",
                       "raw_valuation_snapshots", "pb"),
    "dividend_yield": FieldBinding("dividend_yield", "股息率", CATEGORY_VALUATION,
                                   "valuation", "raw_valuation_snapshots",
                                   "dividend_yield", blocked=True,
                                   blocked_reason_zh=_BLOCKED_DIVIDEND_YIELD),
    # ── 4. 盈利质量 ──
    "loss": FieldBinding("loss", "近 N 年是否亏损", CATEGORY_PROFITABILITY, "financial",
                         "raw_financial_reports", "net_profit", blocked=True,
                         blocked_reason_zh=_BLOCKED_NET_PROFIT),
    "net_profit_yoy": FieldBinding("net_profit_yoy", "净利润同比", CATEGORY_PROFITABILITY,
                                   "financial", "raw_financial_reports",
                                   "net_profit_yoy", blocked=True,
                                   blocked_reason_zh=_BLOCKED_NET_PROFIT_YOY),
    "roe_ttm": FieldBinding("roe_ttm", "ROE_TTM", CATEGORY_PROFITABILITY, "financial",
                            "raw_financial_reports", "roe_ttm"),
    # ── 5. 流动性 ──
    "avg_amount": FieldBinding("avg_amount", "近 N 日日均成交额", CATEGORY_LIQUIDITY,
                               "liquidity", "raw_daily_bars", "amount"),
    "avg_volume": FieldBinding("avg_volume", "近 N 日日均成交量", CATEGORY_LIQUIDITY,
                               "liquidity", "raw_daily_bars", "volume"),
    "avg_turnover_rate": FieldBinding("avg_turnover_rate", "近 N 日日均换手率",
                                      CATEGORY_LIQUIDITY, "liquidity",
                                      "raw_daily_bars", "turnover_rate"),
    # ── 6. 上市时间 ──
    "min_listed_trading_days": FieldBinding("min_listed_trading_days", "上市满 N 个交易日",
                                            CATEGORY_LISTING, "universe",
                                            "universe_symbols", "listed_at", blocked=True,
                                            blocked_reason_zh=_BLOCKED_LISTED_AT),
    # ── 7. 行业 ──
    "industry": FieldBinding("industry", "行业包含/排除", CATEGORY_INDUSTRY, "universe",
                             "universe_symbols", "industry", blocked=True,
                             blocked_reason_zh=_BLOCKED_INDUSTRY),
}


def list_available_fields() -> list[dict[str, Any]]:
    """返回全部筛选字段及其可用性（前端 Accordion 用它渲染「已选条件数」与阻断提示）。"""
    return [FIELD_BINDINGS[k].to_dict() for k in sorted(FIELD_BINDINGS)]


# ══════════════════════════════════════════════════════════════
# 结构化输出类型
# ══════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class RangeCondition:
    """区间条件（端点均含；未填一端表示该侧无界）。"""

    field: str
    min_value: float | None = None
    max_value: float | None = None
    missing: str = MISSING_EXCLUDE

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "min_value": self.min_value,
            "max_value": self.max_value,
            "missing": self.missing,
            "inclusive": True,
        }


@dataclass(frozen=True)
class BlockedField:
    """用户引用了但数据不可用的字段（**不静默忽略**）。"""

    field: str
    label_zh: str
    category: str
    reason_zh: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "label_zh": self.label_zh,
            "category": self.category,
            "category_label_zh": CATEGORY_LABELS_ZH.get(self.category, self.category),
            "reason_zh": self.reason_zh,
        }


@dataclass
class CompiledRules:
    """规则编译产物：**结构化条件对象**（不是 SQL）。

    `to_dict()` 可安全落库（`training_candidate_pools.filter_config_json`）。
    """

    markets: tuple[str, ...] = tuple()
    boards: tuple[str, ...] = tuple()
    exclude_st: bool = False
    exclude_delisting: bool = False
    valuation: tuple[RangeCondition, ...] = tuple()
    liquidity: tuple[RangeCondition, ...] = tuple()
    liquidity_window: int = DEFAULT_LIQUIDITY_WINDOW
    roe_ttm: RangeCondition | None = None
    loss_years: int | None = None
    loss_mode: str | None = None
    blocked_fields: tuple[BlockedField, ...] = tuple()
    problems: tuple[dict[str, Any], ...] = tuple()
    warnings: tuple[str, ...] = tuple()

    # ── 便捷视图 ──

    @property
    def referenced_fields(self) -> tuple[str, ...]:
        """本次规则引用到的全部字段（用于覆盖率门禁）。"""
        out: list[str] = []
        if self.boards:
            out.append("board")
        if self.markets:
            out.append("market")
        if self.exclude_st:
            out.append("exclude_st")
        if self.exclude_delisting:
            out.append("exclude_delisting")
        out.extend(c.field for c in self.valuation)
        out.extend(c.field for c in self.liquidity)
        if self.roe_ttm is not None:
            out.append("roe_ttm")
        if self.loss_years is not None:
            out.append("loss")
        out.extend(b.field for b in self.blocked_fields)
        seen: set[str] = set()
        return tuple(f for f in out if not (f in seen or seen.add(f)))

    @property
    def missing_policies(self) -> dict[str, str]:
        policies: dict[str, str] = {}
        for cond in (*self.valuation, *self.liquidity):
            policies[cond.field] = cond.missing
        if self.roe_ttm is not None:
            policies[self.roe_ttm.field] = self.roe_ttm.missing
        return policies

    @property
    def rule_hash(self) -> str:
        """规则内容的稳定哈希（写入 `training_candidate_pools.rule_hash`）。"""
        payload = canonical_json(self.to_dict())
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        return {
            "markets": list(self.markets),
            "boards": list(self.boards),
            "exclude_st": self.exclude_st,
            "exclude_delisting": self.exclude_delisting,
            "valuation": [c.to_dict() for c in self.valuation],
            "liquidity": [c.to_dict() for c in self.liquidity],
            "liquidity_window": self.liquidity_window,
            "roe_ttm": self.roe_ttm.to_dict() if self.roe_ttm else None,
            "loss_years": self.loss_years,
            "loss_mode": self.loss_mode,
            "blocked_fields": [b.to_dict() for b in self.blocked_fields],
        }


@dataclass
class CategoryStat:
    """单个筛选分类的命中/排除统计（向导 §3.2「预览区按分类展示」）。"""

    category: str
    label_zh: str
    evaluated: int
    excluded: int
    detail_zh: str = ""

    @property
    def passed(self) -> int:
        return max(0, self.evaluated - self.excluded)

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "label_zh": self.label_zh,
            "evaluated": self.evaluated,
            "excluded": self.excluded,
            "passed": self.passed,
            "detail_zh": self.detail_zh,
        }


@dataclass
class ScreeningOutcome:
    """纯函数 `run_screening` 的输出。"""

    universe_size: int
    hits: int
    categories: list[CategoryStat] = field(default_factory=list)
    hit_symbols: list[str] = field(default_factory=list)
    unmapped_symbols: int = 0
    #: 被引用字段 → 在基础 universe（或可得子集）上的可用率
    field_coverage: dict[str, float] = field(default_factory=dict)

    @property
    def excluded_total(self) -> int:
        return max(0, self.universe_size - self.hits)


@dataclass
class PreviewResult:
    """预览结果（**不落库**；向导 §3.2「防抖预览结果不能直接作为挖掘快照」）。"""

    as_of_date: date | None
    as_of_requested: date | None
    as_of_date_adjusted: bool
    rules: CompiledRules
    outcome: ScreeningOutcome | None
    blocking_issues: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    samples: list[dict[str, Any]] = field(default_factory=list)
    data_version: dict[str, Any] = field(default_factory=dict)
    liquidity_actual_days: int = 0
    #: 「为什么选这个数据截止日」的证据（候选日完整度、中位数基线、阈值）
    as_of_evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def hits(self) -> int:
        return self.outcome.hits if self.outcome else 0

    @property
    def can_generate(self) -> bool:
        """能否据此生成候选池（向导 §3.5 的四类硬阻断全部通过）。"""
        return not self.blocking_issues

    @property
    def rule_hash(self) -> str:
        return self.rules.rule_hash

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of_date": self.as_of_date.isoformat() if self.as_of_date else None,
            "as_of_requested": self.as_of_requested.isoformat() if self.as_of_requested else None,
            "as_of_date_adjusted": self.as_of_date_adjusted,
            "universe_size": self.outcome.universe_size if self.outcome else 0,
            "hits": self.hits,
            "excluded_total": self.outcome.excluded_total if self.outcome else 0,
            "min_pool_size": MIN_POOL_SIZE,
            "can_generate": self.can_generate,
            "rule_hash": self.rule_hash,
            "rules": self.rules.to_dict(),
            "categories": [c.to_dict() for c in (self.outcome.categories if self.outcome else [])],
            "samples": self.samples,
            "blocking_issues": self.blocking_issues,
            "warnings": self.warnings,
            "field_coverage": {k: round(v, 4) for k, v in
                               (self.outcome.field_coverage if self.outcome else {}).items()},
            "liquidity_window_requested": self.rules.liquidity_window,
            "liquidity_window_actual_days": self.liquidity_actual_days,
            "data_version": self.data_version,
            "as_of_evidence": self.as_of_evidence,
            "unmapped_symbols": self.outcome.unmapped_symbols if self.outcome else 0,
        }


# ══════════════════════════════════════════════════════════════
# 错误工厂
# ══════════════════════════════════════════════════════════════


def _validation_error(detail: str, **extras: Any) -> FactorSevenError:
    return FactorSevenError(
        "VALIDATION_ERROR",
        detail_zh=detail,
        impact="本次操作未执行",
        fix_link="/settings/factor-mining?step=1",
        retryable=False,
        extras=extras or None,
    )


def pool_too_small_error(hits: int, *, universe_size: int) -> FactorSevenError:
    """命中 <50 → `MINING_POOL_TOO_SMALL`（复用既有错误码，不新增）。"""
    return FactorSevenError(
        "MINING_POOL_TOO_SMALL",
        detail_zh=(
            f"当前条件命中 {hits} 只，低于系统硬下限 {MIN_POOL_SIZE} 只"
            f"（基础股票池 {universe_size} 只）。"
            "50 是后端硬校验、不可通过前端参数降低 —— 样本过少会让横截面统计"
            "（IC / 分组收益）不可信。请放宽条件后重试。"
        ),
        fix_link="/settings/factor-mining?step=1",
        retryable=True,
        extras={
            "reason": REASON_POOL_TOO_SMALL,
            "hits": hits,
            "min_pool_size": MIN_POOL_SIZE,
            "universe_size": universe_size,
        },
    )


def empty_result_error(*, universe_size: int, detail_zh: str) -> FactorSevenError:
    """命中 0 → 与「不足 50」区分开，便于前端给出不同引导。"""
    return FactorSevenError(
        "VALIDATION_ERROR",
        detail_zh=detail_zh,
        impact="本次操作未执行",
        fix_link="/settings/factor-mining?step=1",
        retryable=True,
        extras={
            "reason": REASON_EMPTY_RESULT,
            "hits": 0,
            "universe_size": universe_size,
        },
    )


def _blocking(code: str, reason: str, detail_zh: str, **extra: Any) -> dict[str, Any]:
    return {
        "code": code,
        "reason": reason,
        "detail_zh": detail_zh,
        **extra,
    }


# ══════════════════════════════════════════════════════════════
# 规则编译
# ══════════════════════════════════════════════════════════════


def _coerce_number(raw: Any, *, field_name: str) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise _validation_error(
            f"字段 {field_name} 的区间端点必须是数字或 null，收到 {raw!r}。",
            reason=REASON_INVALID_RULE,
            field=field_name,
        )
    value = float(raw)
    if not math.isfinite(value):
        raise _validation_error(
            f"字段 {field_name} 的区间端点必须是有限数字，收到 {raw!r}。",
            reason=REASON_INVALID_RULE,
            field=field_name,
        )
    return value


def _compile_range(raw: Mapping[str, Any], *, field_name: str,
                   problems: list[dict[str, Any]]) -> RangeCondition:
    """编译单个区间条件；范围冲突**不抛异常**，而是记成结构化问题。"""
    min_value = _coerce_number(raw.get("min", raw.get("min_value")), field_name=field_name)
    max_value = _coerce_number(raw.get("max", raw.get("max_value")), field_name=field_name)
    missing = raw.get("missing", raw.get("missing_policy", MISSING_EXCLUDE))
    if missing not in VALID_MISSING_POLICIES:
        raise _validation_error(
            f"字段 {field_name} 的缺失处理必须是 {sorted(VALID_MISSING_POLICIES)} 之一，"
            f"收到 {missing!r}。",
            reason=REASON_INVALID_RULE,
            field=field_name,
        )
    if min_value is not None and max_value is not None and min_value > max_value:
        binding = FIELD_BINDINGS.get(field_name)
        label = binding.label_zh if binding else field_name
        problems.append(_blocking(
            "VALIDATION_ERROR",
            REASON_RANGE_CONFLICT,
            f"{label} 的最小值 {min_value} 大于最大值 {max_value}，区间为空。"
            "请修正为「最小值 ≤ 最大值」（区间**包含端点**）。",
            field=field_name,
            min_value=min_value,
            max_value=max_value,
        ))
    return RangeCondition(field=field_name, min_value=min_value, max_value=max_value,
                          missing=missing)


def _is_condition_active(raw: Mapping[str, Any]) -> bool:
    """区间是否「真的在筛」——两端都空视为未启用（前端会回传整个对象）。"""
    return raw.get("min", raw.get("min_value")) is not None or \
        raw.get("max", raw.get("max_value")) is not None


def _truthy(raw: Any) -> bool:
    return bool(raw)


def compile_filter_config(raw_config: Mapping[str, Any] | None) -> CompiledRules:
    """把前端规则 JSON 编译为结构化条件对象。

    设计取舍
    --------
    - **格式类错误**（未知市场/板块、类型不对、缺失策略非法）→ 直接抛 `VALIDATION_ERROR`
      （400）。这些是「请求本身不合法」，前端不该把它们当正常状态渲染。
    - **业务类阻断**（范围冲突、字段无数据、命中不足）→ 记进 `problems` / `blocked_fields`，
      由 `preview_filter` 汇总为 `blocking_issues`（向导 §3.3：这些「在预览时以结构化错误
      阻断快照生成」）。这样用户在编辑过程中能一直看到「还差多少」，而不是撞一屏 400。
    """
    cfg: Mapping[str, Any] = raw_config or {}
    if not isinstance(cfg, Mapping):
        raise _validation_error(
            f"filter_config 必须是对象，收到 {type(cfg).__name__}。",
            reason=REASON_INVALID_RULE,
        )

    problems: list[dict[str, Any]] = []
    warnings: list[str] = []
    blocked: list[BlockedField] = []

    # ── 市场与板块 ──
    raw_markets = cfg.get("markets")
    markets: tuple[str, ...] = tuple()
    if raw_markets:
        if not isinstance(raw_markets, (list, tuple)):
            raise _validation_error("markets 必须是数组。", reason=REASON_INVALID_RULE)
        seen: list[str] = []
        for m in raw_markets:
            key = str(m).strip().lower()
            if key not in SUPPORTED_MARKETS:
                raise _validation_error(
                    f"市场代码 {m!r} 不是首期支持的 A 股市场。"
                    f"首期仅支持 {list(SUPPORTED_MARKETS)}（沪/深/北交所与 A 股聚合）。"
                    "港股、美股等非首期资产请先通过数据门禁评审后再开放。",
                    reason=REASON_UNSUPPORTED_MARKET,
                    value=str(m),
                    allowed=list(SUPPORTED_MARKETS),
                )
            if key not in seen:
                seen.append(key)
        markets = tuple(seen)

    raw_boards = cfg.get("boards")
    boards: tuple[str, ...] = tuple()
    if raw_boards:
        if not isinstance(raw_boards, (list, tuple)):
            raise _validation_error("boards 必须是数组。", reason=REASON_INVALID_RULE)
        seen_b: list[str] = []
        for b in raw_boards:
            key = str(b).strip().lower()
            if key not in BOARD_TO_PHYSICAL:
                raise _validation_error(
                    f"板块代码 {b!r} 不受支持。可选：{list(ALL_BOARD_CODES)}"
                    f"（对应 {list(BOARD_LABELS_ZH.values())}）。",
                    reason=REASON_UNSUPPORTED_BOARD,
                    value=str(b),
                    allowed=list(ALL_BOARD_CODES),
                )
            if key not in seen_b:
                seen_b.append(key)
        boards = tuple(seen_b)

    # 板块与市场的一致性：选了 board 但 market 不含其所属市场 → 结果必为空
    if boards and markets:
        need = {BOARD_TO_PHYSICAL[b][0] for b in boards}
        allowed = set(markets) | ({"sh", "sz", "bj"} if "cn" in markets else set())
        missing = sorted(need - allowed)
        if missing:
            labels = "、".join(BOARD_LABELS_ZH[b] for b in boards
                              if BOARD_TO_PHYSICAL[b][0] in missing)
            problems.append(_blocking(
                "VALIDATION_ERROR",
                REASON_RANGE_CONFLICT,
                f"所选板块（{labels}）所属市场不在所选市场 {list(markets)} 内，结果必然为空。"
                "请让两者一致，或清空市场选择。",
                boards=list(boards),
                markets=list(markets),
            ))

    # ── 布尔开关 ──
    exclude_st = _truthy(cfg.get("exclude_st"))
    exclude_delisting = _truthy(cfg.get("exclude_delisting"))
    if exclude_st:
        warnings.append(
            "「排除 ST/*ST」按 **当前名称** 匹配（`universe_symbols.name` 含 `ST`）。"
            "实测 `universe_symbols.is_st` 列全为 0、`security_status_daily` 只有 e2e "
            "fixture 数据，故无 point-in-time 的 ST 历史。该条件仅表达「此刻的 ST 状态」，"
            "**不可**用于历史时点的 ST 回放。"
        )
    if exclude_delisting:
        warnings.append(
            "「排除退市整理/已退市」按 **当前名称** 含 `退` 匹配，语义同 ST：仅表达此刻状态。"
        )

    # ── 被引用但不可用的字段（显式登记 + 阻断，不静默忽略）──
    for field_name, active in (
        ("exclude_suspended", _truthy(cfg.get("exclude_suspended"))),
        ("index_member", _truthy(cfg.get("index_members")) or _truthy(cfg.get("index_member"))),
        ("min_listed_trading_days", cfg.get("min_listed_trading_days") is not None),
        ("industry", bool(cfg.get("industry") or cfg.get("industries"))),
    ):
        if active:
            binding = FIELD_BINDINGS[field_name]
            blocked.append(BlockedField(field_name, binding.label_zh, binding.category,
                                        binding.blocked_reason_zh or ""))

    # ── 估值区间 ──
    valuation: list[RangeCondition] = []
    raw_val = cfg.get("valuation") or {}
    if not isinstance(raw_val, Mapping):
        raise _validation_error("valuation 必须是对象。", reason=REASON_INVALID_RULE)
    for field_name in ("total_market_cap", "circulating_market_cap", "pe_ttm", "pb",
                       "dividend_yield"):
        raw_cond = raw_val.get(field_name)
        if raw_cond is None:
            continue
        if not isinstance(raw_cond, Mapping):
            raise _validation_error(f"valuation.{field_name} 必须是对象。",
                                    reason=REASON_INVALID_RULE)
        cond = _compile_range(raw_cond, field_name=field_name, problems=problems)
        if not _is_condition_active(raw_cond) and cond.missing == MISSING_EXCLUDE:
            # 区间两端都空且无特殊缺失处理 → 视为未启用（前端会回传整个对象，
            # 不应因为「对象在但没填值」就把它当成一条筛选条件）
            continue
        valuation.append(cond)
        if FIELD_BINDINGS[field_name].blocked:
            b = FIELD_BINDINGS[field_name]
            blocked.append(BlockedField(field_name, b.label_zh, b.category,
                                        b.blocked_reason_zh or ""))

    # ── 流动性区间 ──
    raw_liq = cfg.get("liquidity") or {}
    if not isinstance(raw_liq, Mapping):
        raise _validation_error("liquidity 必须是对象。", reason=REASON_INVALID_RULE)
    window = raw_liq.get("window_days", DEFAULT_LIQUIDITY_WINDOW)
    if isinstance(window, bool) or not isinstance(window, (int, float)):
        raise _validation_error(
            f"liquidity.window_days 必须是整数，收到 {window!r}。",
            reason=REASON_INVALID_RULE,
        )
    window_i = int(window)
    if window_i < 1 or window_i > MAX_LIQUIDITY_WINDOW:
        raise _validation_error(
            f"liquidity.window_days 必须在 1~{MAX_LIQUIDITY_WINDOW} 之间，收到 {window_i}。",
            reason=REASON_INVALID_RULE,
        )
    liquidity: list[RangeCondition] = []
    for field_name in ("avg_amount", "avg_volume", "avg_turnover_rate"):
        raw_cond = raw_liq.get(field_name)
        if raw_cond is None or not isinstance(raw_cond, Mapping):
            if raw_cond is not None:
                raise _validation_error(f"liquidity.{field_name} 必须是对象。",
                                        reason=REASON_INVALID_RULE)
            continue
        if not _is_condition_active(raw_cond):
            continue
        liquidity.append(_compile_range(raw_cond, field_name=field_name, problems=problems))

    # ── 盈利质量 ──
    raw_prof = cfg.get("profitability") or {}
    if not isinstance(raw_prof, Mapping):
        raise _validation_error("profitability 必须是对象。", reason=REASON_INVALID_RULE)
    roe_ttm: RangeCondition | None = None
    if isinstance(raw_prof.get("roe_ttm"), Mapping):
        raw_roe = raw_prof["roe_ttm"]
        if _is_condition_active(raw_roe) or raw_roe.get("missing") == MISSING_KEEP:
            roe_ttm = _compile_range(raw_roe, field_name="roe_ttm", problems=problems)

    loss_years: int | None = None
    loss_mode: str | None = None
    raw_loss = raw_prof.get("loss")
    if isinstance(raw_loss, Mapping) and _truthy(raw_loss.get("enabled", True)):
        years = raw_loss.get("years", DEFAULT_LOSS_YEARS)
        if isinstance(years, bool) or not isinstance(years, (int, float)):
            raise _validation_error(f"profitability.loss.years 必须是整数，收到 {years!r}。",
                                    reason=REASON_INVALID_RULE)
        years_i = int(years)
        if years_i < 1 or years_i > 5:
            raise _validation_error(
                f"profitability.loss.years 必须在 1~5 之间（回看已公告年度数），收到 {years_i}。",
                reason=REASON_INVALID_RULE,
            )
        mode = raw_loss.get("mode", LOSS_MODE_ANY)
        if mode not in VALID_LOSS_MODES:
            raise _validation_error(
                f"profitability.loss.mode 必须是 {list(VALID_LOSS_MODES)} 之一，收到 {mode!r}。",
                reason=REASON_INVALID_RULE,
            )
        loss_years, loss_mode = years_i, mode
        b = FIELD_BINDINGS["loss"]
        blocked.append(BlockedField("loss", b.label_zh, b.category, b.blocked_reason_zh or ""))

    # ── 收尾：跨市场完整性与去重 ──
    dedup_blocked: list[BlockedField] = []
    seen_fields: set[str] = set()
    for b in blocked:
        if b.field in seen_fields:
            continue
        seen_fields.add(b.field)
        dedup_blocked.append(b)

    if markets and "cn" not in markets and len(set(markets)) > 1 and not boards:
        warnings.append(
            f"已选多个市场 {list(markets)} 但未限制板块；若某些市场的行情覆盖不完整，"
            "预览会以 CROSS_MARKET_INCOMPLETE 阻断（向导 §3.5）。"
        )

    return CompiledRules(
        markets=markets,
        boards=boards,
        exclude_st=exclude_st,
        exclude_delisting=exclude_delisting,
        valuation=tuple(valuation),
        liquidity=tuple(liquidity),
        liquidity_window=window_i,
        roe_ttm=roe_ttm,
        loss_years=loss_years,
        loss_mode=loss_mode,
        blocked_fields=tuple(dedup_blocked),
        problems=tuple(problems),
        warnings=tuple(warnings),
    )


# ══════════════════════════════════════════════════════════════
# 筛选面板（纯数据输入，便于无基础设施地测试）
# ══════════════════════════════════════════════════════════════


@dataclass
class ScreeningPanel:
    """规则引擎的输入面板。

    **纯数据**：不持有任何 DB 连接，因此 `run_screening()` 可以完全不碰 MySQL/DuckDB
    地被测试覆盖，也天然满足「禁止拼接 SQL」（引擎里根本没有 SQL）。

    列约定
    ------
    - `universe`: `symbol, name, market, board`
    - `valuation`: `symbol, total_market_cap, circulating_market_cap, pe_ttm, pb,
      dividend_yield`
    - `liquidity`: `symbol, avg_amount, avg_volume, avg_turnover_rate, effective_days`
    - `financials`: `symbol, roe_ttm, annual_periods, annual_net_profits`
      （`annual_*` 为**倒序**列表：最近年度在前，长度 ≤ 回看年数）
    """

    universe: pd.DataFrame
    valuation: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(
        columns=["symbol", "total_market_cap", "circulating_market_cap", "pe_ttm", "pb",
                 "dividend_yield"]))
    liquidity: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(
        columns=["symbol", "avg_amount", "avg_volume", "avg_turnover_rate", "effective_days"]))
    financials: pd.DataFrame = field(default_factory=lambda: pd.DataFrame(
        columns=["symbol", "roe_ttm", "annual_periods", "annual_net_profits"]))
    liquidity_days_actual: int = 0

    @property
    def is_empty(self) -> bool:
        return self.universe is None or self.universe.empty


def _empty_panel() -> ScreeningPanel:
    return ScreeningPanel(universe=pd.DataFrame(columns=["symbol", "name", "market", "board"]))


# ══════════════════════════════════════════════════════════════
# 纯筛选引擎
# ══════════════════════════════════════════════════════════════


def _st_mask(names: pd.Series) -> pd.Series:
    """ST / *ST 识别（按**当前名称**）。"""
    return names.fillna("").str.upper().str.contains("ST", regex=False)


def _delisting_mask(names: pd.Series) -> pd.Series:
    """退市整理 / 已退市识别（按**当前名称**含「退」）。"""
    return names.fillna("").str.contains("退", regex=False)


def _range_mask(values: pd.Series, cond: RangeCondition) -> tuple[pd.Series, pd.Series]:
    """区间掩码（含端点）。

    返回 `(是否缺失, 是否通过)`。缺失的含义由 `cond.missing` 决定：
    - `exclude`：缺失 → 不通过（**不把缺失当 0**，见 MEMORY 的「禁止稀疏字段填零」）
    - `keep`：缺失 → 通过（显式保留，用户已知情）
    """
    numeric = pd.to_numeric(values, errors="coerce")
    missing = numeric.isna()
    ok = pd.Series(True, index=values.index)
    if cond.min_value is not None:
        ok &= numeric >= cond.min_value
    if cond.max_value is not None:
        ok &= numeric <= cond.max_value
    ok = ok & ~missing if cond.missing == MISSING_EXCLUDE else ok | missing
    return missing, ok


def _should_exclude_by_loss(net_profits: pd.Series, periods: pd.Series, *,
                            years: int, mode: str) -> pd.Series:
    """近 N 年亏损判定（向导 §3.4）→ **是否应被排除**。

    `net_profits` / `periods` 是**倒序**列表（最近年度在前）。

    ⚠️ 返回值的语义是「**该排除**」而不是「**是亏损**」——
    因为向导明确：「缺失年度**不等于盈利**，默认按『数据不足』排除并提示」。
    若把「数据不足」判成 False（=不亏损=不排除），就等于把缺失当成了正常，
    这正是 MEMORY 里「禁止稀疏字段填零」要防的失败形态。
    """
    def _judge(row_np: Any, row_pd_: Any) -> bool:
        np_list = list(row_np) if isinstance(row_np, (list, tuple)) else []
        pd_list = list(row_pd_) if isinstance(row_pd_, (list, tuple)) else []
        usable = [(p, v) for p, v in zip(pd_list, np_list)
                  if v is not None and not (isinstance(v, float) and math.isnan(v))]
        usable = usable[:years]
        if len(usable) < years:
            return True  # 数据不足 → **排除**（不等于盈利）
        values = [float(v) for _, v in usable]
        if mode == LOSS_MODE_ANY:
            return any(v < 0 for v in values)
        if mode == LOSS_MODE_ALL_CONSECUTIVE:
            # 「连续 N 年亏损才排除」：最近 N 个**已公告年度**必须年份连续且全部亏损
            years_seq = [p.year for p, _ in usable]
            expected = list(range(years_seq[0], years_seq[0] - years, -1))
            return years_seq == expected and all(v < 0 for v in values)
        if mode == LOSS_MODE_CUMULATIVE:
            return sum(values) < 0
        return False

    if net_profits.empty:
        return pd.Series(False, index=net_profits.index)
    return pd.Series(
        [_judge(a, b) for a, b in zip(net_profits, periods)],
        index=net_profits.index,
    )


def run_screening(rules: CompiledRules, panel: ScreeningPanel) -> ScreeningOutcome:
    """执行筛选：**纯函数**，pandas 布尔掩码，无 SQL、无 IO。

    逐分类统计排除数（向导 §3.2 要求「预览区按分类展示命中/排除数量」）。
    分类之间是**交集**关系：每个分类的 `excluded` 都是「相对上一分类剩余集合」的排除数，
    因此 `Σ excluded` 不等于 `universe_size - hits`（有重叠），在 `detail_zh` 里说明。
    """
    if panel.is_empty:
        return ScreeningOutcome(universe_size=0, hits=0)

    u = panel.universe.copy()
    u = u.reset_index(drop=True)
    base_n = len(u)
    names = u.get("name", pd.Series([""] * base_n))
    remaining = pd.Series(True, index=u.index)
    categories: list[CategoryStat] = []
    coverage: dict[str, float] = {}

    def _apply(category: str, mask: pd.Series, detail: str) -> None:
        nonlocal remaining
        evaluated = int(remaining.sum())
        before = remaining.copy()
        remaining = remaining & mask.fillna(False)
        excluded = int((before & ~remaining).sum())
        categories.append(CategoryStat(
            category=category,
            label_zh=CATEGORY_LABELS_ZH[category],
            evaluated=evaluated,
            excluded=excluded,
            detail_zh=detail,
        ))

    # ── 1. 市场与交易状态 ──
    market_mask = pd.Series(True, index=u.index)
    details: list[str] = []
    if rules.markets:
        market_mask &= u["market"].isin(list(rules.markets))
        details.append(f"市场 ∈ {list(rules.markets)}")
    if rules.boards:
        pairs = {BOARD_TO_PHYSICAL[b] for b in rules.boards}
        market_mask &= pd.Series(
            [(str(m), str(b)) in pairs for m, b in zip(u["market"], u.get("board", [None] * base_n))],
            index=u.index,
        )
        details.append("板块 ∈ " + "、".join(BOARD_LABELS_ZH[b] for b in rules.boards))
    if rules.exclude_delisting:
        market_mask &= ~_delisting_mask(names)
        details.append("排除名称含「退」")
    _apply(CATEGORY_UNIVERSE, market_mask, "；".join(details) or "未限制市场/板块")

    # ── 2. 风险标记 ──
    risk_mask = pd.Series(True, index=u.index)
    if rules.exclude_st:
        risk_mask &= ~_st_mask(names)
    _apply(CATEGORY_RISK, risk_mask,
           "排除当前名称含 ST/*ST" if rules.exclude_st else "未启用风险标记过滤")

    # ── 3. 估值与市值 ──
    val_detail: list[str] = []
    val_mask = pd.Series(True, index=u.index)
    joined = u[["symbol"]].merge(panel.valuation, on="symbol", how="left") if not \
        panel.valuation.empty else u[["symbol"]].assign(
            **{c: pd.NA for c in ("total_market_cap", "circulating_market_cap", "pe_ttm",
                                  "pb", "dividend_yield")})
    joined.index = u.index
    for cond in rules.valuation:
        if cond.field not in joined.columns:
            continue
        missing, ok = _range_mask(joined[cond.field], cond)
        val_mask &= ok
        binding = FIELD_BINDINGS.get(cond.field)
        avail = 1.0 - float(missing.mean()) if len(missing) else 0.0
        coverage[cond.field] = avail
        label = binding.label_zh if binding else cond.field
        bounds = []
        if cond.min_value is not None:
            bounds.append(f"≥{cond.min_value:g}")
        if cond.max_value is not None:
            bounds.append(f"≤{cond.max_value:g}")
        val_detail.append(f"{label} {' 且 '.join(bounds) or '无界'}"
                          f"（缺失{'排除' if cond.missing == MISSING_EXCLUDE else '保留'}）")
    if rules.valuation:
        _apply(CATEGORY_VALUATION, val_mask, "；".join(val_detail))
    else:
        categories.append(CategoryStat(CATEGORY_VALUATION, CATEGORY_LABELS_ZH[CATEGORY_VALUATION],
                                       0, 0, "未启用估值/市值条件"))

    # ── 4. 流动性 ──
    liq_mask = pd.Series(True, index=u.index)
    liq_detail: list[str] = []
    if rules.liquidity:
        lj = u[["symbol"]].merge(panel.liquidity, on="symbol", how="left") if not \
            panel.liquidity.empty else u[["symbol"]].assign(
                **{c: pd.NA for c in ("avg_amount", "avg_volume", "avg_turnover_rate",
                                      "effective_days")})
        lj.index = u.index
        for cond in rules.liquidity:
            if cond.field not in lj.columns:
                continue
            missing, ok = _range_mask(lj[cond.field], cond)
            liq_mask &= ok
            coverage[cond.field] = 1.0 - float(missing.mean()) if len(missing) else 0.0
            binding = FIELD_BINDINGS.get(cond.field)
            label = binding.label_zh if binding else cond.field
            bounds = []
            if cond.min_value is not None:
                bounds.append(f"≥{cond.min_value:g}")
            if cond.max_value is not None:
                bounds.append(f"≤{cond.max_value:g}")
            liq_detail.append(f"{label} {' 且 '.join(bounds) or '无界'}（窗口 {rules.liquidity_window} 日）")
        _apply(CATEGORY_LIQUIDITY, liq_mask, "；".join(liq_detail))
    else:
        categories.append(CategoryStat(CATEGORY_LIQUIDITY, CATEGORY_LABELS_ZH[CATEGORY_LIQUIDITY],
                                       0, 0, "未启用流动性条件"))

    # ── 5. 盈利质量 ──
    prof_mask = pd.Series(True, index=u.index)
    prof_detail: list[str] = []
    if rules.roe_ttm is not None or rules.loss_years is not None:
        fj = u[["symbol"]].merge(panel.financials, on="symbol", how="left") if not \
            panel.financials.empty else u[["symbol"]].assign(
                roe_ttm=pd.NA, annual_periods=pd.NA, annual_net_profits=pd.NA)
        fj.index = u.index
        if rules.roe_ttm is not None:
            missing, ok = _range_mask(fj.get("roe_ttm", pd.Series(pd.NA, index=u.index)),
                                      rules.roe_ttm)
            prof_mask &= ok
            coverage["roe_ttm"] = 1.0 - float(missing.mean()) if len(missing) else 0.0
            bounds = []
            if rules.roe_ttm.min_value is not None:
                bounds.append(f"≥{rules.roe_ttm.min_value:g}")
            if rules.roe_ttm.max_value is not None:
                bounds.append(f"≤{rules.roe_ttm.max_value:g}")
            prof_detail.append(f"ROE_TTM {' 且 '.join(bounds)}")
        if rules.loss_years is not None:
            periods = fj["annual_periods"] if "annual_periods" in fj.columns else \
                pd.Series([None] * len(u), index=u.index)
            profits = fj["annual_net_profits"] if "annual_net_profits" in fj.columns else \
                pd.Series([None] * len(u), index=u.index)
            prof_mask &= ~_should_exclude_by_loss(
                profits, periods, years=rules.loss_years,
                mode=rules.loss_mode or LOSS_MODE_ANY)
            prof_detail.append(f"排除近 {rules.loss_years} 年亏损（mode={rules.loss_mode}）")
        _apply(CATEGORY_PROFITABILITY, prof_mask, "；".join(prof_detail))
    else:
        categories.append(CategoryStat(CATEGORY_PROFITABILITY,
                                       CATEGORY_LABELS_ZH[CATEGORY_PROFITABILITY],
                                       0, 0, "未启用盈利质量条件"))

    # ── 6/7. 上市时间、行业（T09 全部阻断，仅登记空统计）──
    for cat, note in (
        (CATEGORY_LISTING, "未启用（数据缺失，见阻断项）"),
        (CATEGORY_INDUSTRY, "未启用（数据缺失，见阻断项）"),
    ):
        categories.append(CategoryStat(cat, CATEGORY_LABELS_ZH[cat], 0, 0, note))

    hit_symbols = u.loc[remaining, "symbol"].astype(str).tolist()
    mapped = int(remaining.sum())

    # 未映射标的：universe 里的 symbol 在估值/流动性面板里找不到（用于诊断覆盖率）
    unmapped = 0
    if not panel.valuation.empty:
        known = set(panel.valuation["symbol"].astype(str))
        unmapped = int((~u["symbol"].astype(str).isin(known)).sum())

    return ScreeningOutcome(
        universe_size=base_n,
        hits=len(hit_symbols),
        categories=categories,
        hit_symbols=hit_symbols,
        unmapped_symbols=unmapped,
        field_coverage=coverage,
    )


# ══════════════════════════════════════════════════════════════
# as_of_date 解析
# ══════════════════════════════════════════════════════════════


def _resolve_as_of_date(conn, requested: date | None, *,
                        baseline_days: int = CROSS_SECTION_BASELINE_DAYS,
                        completeness: float = CROSS_SECTION_COMPLETENESS,
                        ) -> tuple[date | None, bool, dict[str, Any]]:
    """在**估值快照**上解析「最近完整交易日」。

    为什么不取 `MAX(trade_date)`：实测 `raw_daily_bars` 最新日 2026-09-07 **只有 1 个标的**；
    而 `raw_valuation_snapshots` 的 9 月各日只有 ~1,904 只（对比 8 月的 ~5,544 只），
    按最新日求值会让「市值 ≥ X」这类条件在一个只剩三分之一标的的截面上算，
    结果完全不可比。

    判定规则（与项目既有 `trade_calendar.latest_complete_trade_date` 的 0.9 阈值一致）：
    在最近 `baseline_days` 个日期上取标的数**中位数**，要求候选日的标的数
    ≥ 中位数 × `completeness`。

    返回 `(as_of_date, adjusted, evidence)`。
    `evidence` 里带 `candidate_ratios`，**让「为什么选这天」可被事后解释** ——
    实测本规则会跳过 9 月的稀疏日、落到 2026-08-21，这个落差必须由证据说话。
    """
    cutoff = requested
    rows = conn.execute(
        "SELECT trade_date, COUNT(DISTINCT symbol) AS n FROM raw_valuation_snapshots "
        "WHERE (? IS NULL OR trade_date <= ?) "
        "GROUP BY trade_date ORDER BY trade_date DESC LIMIT ?",
        [cutoff, cutoff, int(baseline_days)],
    ).fetchall()
    if not rows:
        return None, False, {"candidate_ratios": {}, "median_baseline": 0,
                             "completeness_threshold": completeness}

    counts = sorted(int(r[1] or 0) for r in rows)
    median = counts[len(counts) // 2] if counts else 0
    floor = max(1, int(median * completeness)) if median else 1

    ratios = {str(r[0]): round(int(r[1] or 0) / median, 6) if median else 0.0
              for r in rows}
    evidence: dict[str, Any] = {
        "candidate_ratios": ratios,
        "median_baseline": median,
        "completeness_threshold": completeness,
        "required_symbols": floor,
    }

    for trade_date, n in rows:
        if int(n or 0) >= floor:
            resolved = trade_date
            adjusted = requested is not None and resolved != requested
            evidence.update({
                "selected_trade_date": str(resolved),
                "observed_symbols": int(n or 0),
                "completeness_ratio": round(int(n or 0) / median, 6) if median else 0.0,
                "fallback_reason": None,
                "evaluated_candidate_dates": [str(r[0]) for r in rows],
            })
            return resolved, adjusted, evidence

    # 全部不达标 → 取最新的（并标记，让调用方去报警）
    resolved = rows[0][0]
    evidence.update({
        "selected_trade_date": str(resolved),
        "observed_symbols": int(rows[0][1] or 0),
        "completeness_ratio": round(int(rows[0][1] or 0) / median, 6) if median else 0.0,
        "fallback_reason": "no_candidate_meets_completeness",
        "evaluated_candidate_dates": [str(r[0]) for r in rows],
    })
    return resolved, requested is not None and resolved != requested, evidence


# ══════════════════════════════════════════════════════════════
# 数据装载（唯一的 SQL 出现地：常量标识符 + `?` 参数绑定）
# ══════════════════════════════════════════════════════════════

_UNIVERSE_LIMIT = 20_000


def load_universe(db: Session, rules: CompiledRules) -> pd.DataFrame:
    """从 MySQL `universe_symbols` 读候选 universe（**只读，不回写主数据**）。

    为什么是 `universe_symbols` 而不是 `symbols`：
    - `universe_symbols` 的 docstring 明确「挖掘任务只读此表」
    - 实测 `symbols.board` 是脏数据（gem 仅 4 只 / star 仅 6 只），
      `universe_symbols.board` 才是真实结构（gem 1,403 / star 615）
    """
    stmt = select(
        UniverseSymbol.symbol,
        UniverseSymbol.name,
        UniverseSymbol.market,
        UniverseSymbol.board,
        UniverseSymbol.asset_type,
    ).where(
        UniverseSymbol.asset_type == "stock",
        UniverseSymbol.region == "cn",
        UniverseSymbol.is_synced == 1,
    ).limit(_UNIVERSE_LIMIT)
    if rules.markets:
        stmt = stmt.where(UniverseSymbol.market.in_(list(rules.markets)))
    if rules.boards:
        pairs = sorted({BOARD_TO_PHYSICAL[b] for b in rules.boards})
        markets_in = [m for m, _ in pairs]
        boards_in = [b for _, b in pairs]
        stmt = stmt.where(
            UniverseSymbol.market.in_(markets_in),
            UniverseSymbol.board.in_(boards_in),
        )
    rows = db.execute(stmt).all()
    frame = pd.DataFrame(rows, columns=["symbol", "name", "market", "board", "asset_type"])
    if frame.empty:
        return frame
    # 板块对偶精确过滤（避免 market/board 的笛卡尔组合误入）
    if rules.boards:
        pairs = {BOARD_TO_PHYSICAL[b] for b in rules.boards}
        frame = frame[[(str(m), str(b)) in pairs
                       for m, b in zip(frame["market"], frame["board"])]].reset_index(drop=True)
    return frame


_VALUATION_COLUMNS = ("total_market_cap", "circulating_market_cap", "pe_ttm", "pb",
                      "dividend_yield")


def load_valuation(conn, as_of_date: date, symbols: Sequence[str] | None = None) -> pd.DataFrame:
    """读指定交易日的估值快照。

    ⚠️ 实测：每日仅约 **1,904 只**标的（覆盖率 ~34%，全库 5,549 只）。这是数据源现状，
    不是查询 bug；`preview_filter` 会把该覆盖率作为 `field_coverage` 回报。
    """
    sql = (
        "SELECT symbol, total_market_cap, circulating_market_cap, pe_ttm, pb, dividend_yield "
        "FROM raw_valuation_snapshots WHERE trade_date = ?"
    )
    rows = conn.execute(sql, [as_of_date]).fetchall()
    frame = pd.DataFrame(rows, columns=("symbol",) + _VALUATION_COLUMNS)
    if not frame.empty and symbols is not None:
        frame = frame[frame["symbol"].isin(list(symbols))].reset_index(drop=True)
    return frame


def load_liquidity(conn, as_of_date: date, window: int,
                   symbols: Sequence[str] | None = None) -> tuple[pd.DataFrame, int]:
    """读「近 N 个**已完成交易日**」的日均成交额/量/换手率。

    返回 `(frame, actual_days)`。`actual_days` 可能小于 `window`——
    实测 `raw_daily_bars` 只有 401 个交易日（2025-01-13 起），
    向导 §3.3 要求「返回实际有效天数」，故必须回报而不是静默按可得天数算。

    `effective_days` 列 = 窗口内该标的**非空**成交额行数（停牌日无行 → 不计入）。
    """
    dates = conn.execute(
        "SELECT DISTINCT trade_date FROM raw_daily_bars "
        "WHERE trade_date <= ? ORDER BY trade_date DESC LIMIT ?",
        [as_of_date, int(window)],
    ).fetchall()
    trade_dates = [r[0] for r in dates]
    if not trade_dates:
        return pd.DataFrame(columns=["symbol", "avg_amount", "avg_volume",
                                     "avg_turnover_rate", "effective_days"]), 0
    placeholders = ", ".join("?" for _ in trade_dates)
    sql = (
        "SELECT symbol, "
        "AVG(amount) AS avg_amount, "
        "AVG(volume) AS avg_volume, "
        "AVG(turnover_rate) AS avg_turnover_rate, "
        "COUNT(amount) AS effective_days "
        "FROM raw_daily_bars "
        f"WHERE trade_date IN ({placeholders}) "
        "GROUP BY symbol"
    )
    rows = conn.execute(sql, list(trade_dates)).fetchall()
    frame = pd.DataFrame(rows, columns=["symbol", "avg_amount", "avg_volume",
                                        "avg_turnover_rate", "effective_days"])
    if not frame.empty and symbols is not None:
        frame = frame[frame["symbol"].isin(list(symbols))].reset_index(drop=True)
    return frame, len(trade_dates)


def load_financials_pit(conn, as_of_date: date, *, years: int,
                        symbols: Sequence[str] | None = None) -> pd.DataFrame:
    """按**公告日 PIT** 读财务数据。

    - `roe_ttm`：取 `announcement_date <= as_of_date` 的**最新一条**报告
      （`ROW_NUMBER()` 按 `announcement_date DESC, report_period DESC` 排序）
    - `annual_periods` / `annual_net_profits`：取年度报告（`report_period` 为 12-31）
      按 `report_period DESC` 取前 `years` 条，**倒序列表**（最近年度在前）

    向导 §3.4：「按公告日可见的年度财报，向每个挖掘日期回看最近三个**已公告**年度」——
    所以过滤条件是 `announcement_date <= as_of_date` 而**不是** `report_period <=`。
    实测：as_of 2026-09-04 时最新可见报告期是 2026-06-30（2,033 只）。
    """
    years = max(1, int(years))
    latest_sql = (
        "WITH visible AS ("
        "  SELECT symbol, report_period, roe_ttm, announcement_date,"
        "         ROW_NUMBER() OVER (PARTITION BY symbol"
        "                            ORDER BY announcement_date DESC, report_period DESC) AS rn"
        "  FROM raw_financial_reports WHERE announcement_date <= ?"
        ") SELECT symbol, roe_ttm FROM visible WHERE rn = 1"
    )
    latest_rows = conn.execute(latest_sql, [as_of_date]).fetchall()
    latest = pd.DataFrame(latest_rows, columns=["symbol", "roe_ttm"])

    annual_sql = (
        "WITH visible AS ("
        "  SELECT symbol, report_period, net_profit,"
        "         ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY report_period DESC) AS rn"
        "  FROM raw_financial_reports"
        "  WHERE announcement_date <= ? AND EXTRACT(MONTH FROM report_period) = 12"
        f") SELECT symbol, report_period, net_profit FROM visible WHERE rn <= {years}"
    )
    annual_rows = conn.execute(annual_sql, [as_of_date]).fetchall()
    grouped: dict[str, dict[str, list[Any]]] = {}
    for symbol, period, profit in annual_rows:
        slot = grouped.setdefault(str(symbol), {"periods": [], "profits": []})
        slot["periods"].append(period)
        slot["profits"].append(None if profit is None else float(profit))
    annual = pd.DataFrame(
        [{"symbol": s, "annual_periods": v["periods"], "annual_net_profits": v["profits"]}
         for s, v in grouped.items()]
    )

    if latest.empty and annual.empty:
        return pd.DataFrame(columns=["symbol", "roe_ttm", "annual_periods",
                                     "annual_net_profits"])
    if latest.empty:
        merged = annual
    elif annual.empty:
        merged = latest.assign(annual_periods=None, annual_net_profits=None)
    else:
        merged = latest.merge(annual, on="symbol", how="outer")
    if symbols is not None and not merged.empty:
        merged = merged[merged["symbol"].isin(list(symbols))].reset_index(drop=True)
    return merged


def load_screening_panel(db: Session, rules: CompiledRules, *, as_of_date: date,
                         warehouse: Any | None = None) -> ScreeningPanel:
    """装配筛选面板（MySQL universe + DuckDB 估值/流动性/财务）。"""
    universe = load_universe(db, rules)
    if universe.empty:
        return ScreeningPanel(universe=universe, liquidity_days_actual=0)
    symbols = universe["symbol"].astype(str).tolist()

    wh = warehouse if warehouse is not None else _default_warehouse(db)
    with wh.connection(read_only=True) as conn:
        valuation = load_valuation(conn, as_of_date, symbols)
        liquidity, days = load_liquidity(conn, as_of_date, rules.liquidity_window, symbols)
        financials = load_financials_pit(conn, as_of_date,
                                        years=rules.loss_years or DEFAULT_LOSS_YEARS,
                                        symbols=symbols)
    return ScreeningPanel(
        universe=universe,
        valuation=valuation,
        liquidity=liquidity,
        financials=financials,
        liquidity_days_actual=days,
    )


def resolve_hit_symbol_ids(db: Session, symbols: Sequence[str],
                           ) -> tuple[list[int], list[str]]:
    """把筛选命中的**代码**映射为 `symbols.id`（成员表用的 ID 空间）。

    为什么需要映射
    --------------
    - 筛选 universe 来自 `universe_symbols`（基础数据层）
    - 成员表 `training_candidate_pool_members.symbol_id` 由 T08 校验为 **`symbols.id`**
      （业务层）—— 两张表是**两套自增主键**，不能混用。

    实测（2026-09-16）
    ------------------
    - 两表 `symbol` 列都唯一 → 映射无歧义
    - universe A 股 5,554 只 → **100%** 能映到 `symbols`（未映射 0 只）
    - 反向有 8 只 stock 只在 `symbols` 里（不在 universe），不影响本方向

    返回 `(symbol_ids, unmapped_codes)`。未映射的代码**不静默丢弃**，由调用方决定。
    """
    codes = [str(s) for s in symbols]
    if not codes:
        return [], []
    rows = db.execute(
        select(Symbol.id, Symbol.symbol).where(Symbol.symbol.in_(codes))
    ).all()
    by_code = {str(code): int(sid) for sid, code in rows}
    ids: list[int] = []
    unmapped: list[str] = []
    for code in codes:
        sid = by_code.get(code)
        if sid is None:
            unmapped.append(code)
        else:
            ids.append(sid)
    seen: set[int] = set()
    ordered = [i for i in ids if not (i in seen or seen.add(i))]
    return ordered, unmapped


def _default_warehouse(db: Session) -> Any:
    """按项目配置构造 `FactorWarehouse`（与 `routes/factors.py` 同一范式）。"""
    from app.services.factors.config import get_factor_system_config
    from app.services.factors.store import FactorWarehouse

    config = get_factor_system_config(db)
    return FactorWarehouse(config.warehouse_path)


# ══════════════════════════════════════════════════════════════
# 阻断项汇总
# ══════════════════════════════════════════════════════════════


def _collect_blocking_issues(result: PreviewResult) -> list[dict[str, Any]]:
    """汇总向导 §3.5 要求的四类硬阻断 + §3.3 的结构化错误。

    顺序刻意稳定：**格式/数据类问题在前**（用户必须先修这些），命中量问题在后。
    """
    issues: list[dict[str, Any]] = []

    # ① 仓库不可用
    if result.outcome is None and result.as_of_date is None:
        issues.append(_blocking(
            "VALIDATION_ERROR", REASON_WAREHOUSE_UNAVAILABLE,
            "无法确定数据截止日：估值快照表为空或数仓不可用。"
            "候选池必须绑定一个明确的数据截止日，故本次预览被阻断。",
        ))
        return issues

    # ② 规则自身格式问题（范围冲突等）
    issues.extend(dict(p) for p in result.rules.problems)

    # ③ 被引用字段无数据（**不静默忽略**）
    for b in result.rules.blocked_fields:
        issues.append(_blocking(
            "VALIDATION_ERROR", REASON_FIELD_UNAVAILABLE,
            f"「{b.label_zh}」条件当前不可用：{b.reason_zh}",
            field=b.field,
            category=b.category,
            category_label_zh=CATEGORY_LABELS_ZH.get(b.category, b.category),
        ))

    # ④ 被引用字段覆盖率不足（向导 §3.5「关键字段覆盖率不足」）
    coverage = result.outcome.field_coverage if result.outcome else {}
    universe_size = result.outcome.universe_size if result.outcome else 0
    for field_name, ratio in sorted(coverage.items()):
        if ratio < MIN_REFERENCED_FIELD_COVERAGE:
            binding = FIELD_BINDINGS.get(field_name)
            label = binding.label_zh if binding else field_name
            issues.append(_blocking(
                "VALIDATION_ERROR", REASON_FIELD_COVERAGE_INSUFFICIENT,
                f"「{label}」在基础股票池上的可用率仅 {ratio:.1%}，"
                f"低于门禁 {MIN_REFERENCED_FIELD_COVERAGE:.0%}。"
                "按此条件筛选会得到系统性偏差的池子，故阻断。",
                field=field_name,
                coverage=round(ratio, 4),
                threshold=MIN_REFERENCED_FIELD_COVERAGE,
            ))

    if result.outcome is None:
        return issues

    # ⑤ 命中为 0
    if result.outcome.hits == 0:
        issues.append(_blocking(
            "VALIDATION_ERROR", REASON_EMPTY_RESULT,
            f"当前条件在 {universe_size} 只基础股票池中命中 **0** 只。"
            "请放宽区间、减少「排除」类条件，或确认数据截止日。",
            hits=0,
            universe_size=universe_size,
        ))
        return issues

    # ⑥ 命中 <50（硬下限）
    if result.outcome.hits < MIN_POOL_SIZE:
        issues.append(_blocking(
            "MINING_POOL_TOO_SMALL", REASON_POOL_TOO_SMALL,
            f"当前条件命中 {result.outcome.hits} 只，低于系统硬下限 {MIN_POOL_SIZE} 只。"
            "50 为后端硬校验，不能通过前端参数降低。",
            hits=result.outcome.hits,
            min_pool_size=MIN_POOL_SIZE,
            universe_size=universe_size,
        ))

    # ⑦ 跨市场但行情不完整
    if result.rules.markets and len(set(result.rules.markets)) > 1 and \
            result.outcome.unmapped_symbols > 0:
        ratio = result.outcome.unmapped_symbols / max(1, universe_size)
        if ratio > 0.5:
            issues.append(_blocking(
                "VALIDATION_ERROR", REASON_CROSS_MARKET_INCOMPLETE,
                f"所选市场 {list(result.rules.markets)} 中，有 "
                f"{result.outcome.unmapped_symbols} 只（{ratio:.0%}）在数据截止日缺少"
                "行情/估值数据。跨市场筛选要求各市场数据完整，故阻断。",
                unmapped=result.outcome.unmapped_symbols,
                universe_size=universe_size,
            ))

    return issues


def _build_samples(panel: ScreeningPanel, outcome: ScreeningOutcome) -> list[dict[str, Any]]:
    """构造示例标的（向导 §3.5「示例代码」）。

    ⚠️ 示例只暴露**展示用**字段，不回传整行 —— 预览不落库，也不该成为数据导出通道。
    """
    if outcome.hits == 0 or panel.universe.empty:
        return []
    head = outcome.hit_symbols[:PREVIEW_SAMPLE_SIZE]
    u = panel.universe.set_index("symbol")
    val = panel.valuation.set_index("symbol") if not panel.valuation.empty else None
    out: list[dict[str, Any]] = []
    for sym in head:
        item: dict[str, Any] = {"symbol": sym}
        if sym in u.index:
            row = u.loc[sym]
            item["name"] = None if pd.isna(row.get("name")) else str(row.get("name"))
            item["market"] = None if pd.isna(row.get("market")) else str(row.get("market"))
            item["board"] = None if pd.isna(row.get("board")) else str(row.get("board"))
            item["board_label_zh"] = BOARD_LABELS_ZH.get(
                next((code for code, pair in BOARD_TO_PHYSICAL.items()
                      if pair == (str(row.get("market")), str(row.get("board")))), ""),
                None,
            )
        if val is not None and sym in val.index:
            vrow = val.loc[sym]
            for col in ("total_market_cap", "pe_ttm", "pb"):
                if col in val.columns:
                    raw = vrow.get(col)
                    item[col] = None if raw is None or pd.isna(raw) else float(raw)
        out.append(item)
    return out


def _data_version(conn) -> dict[str, Any]:
    """数据版本（向导 §3.2：预设与快照都要带「数据版本」以便复现）。"""
    version: dict[str, Any] = {"captured_at": _utcnow().isoformat()}
    try:
        row = conn.execute(
            "SELECT key, value FROM warehouse_metadata WHERE key = 'schema_version'"
        ).fetchone()
        version["warehouse_schema_version"] = row[1] if row else None
    except Exception:  # noqa: BLE001 - 元数据缺失不该让预览失败
        version["warehouse_schema_version"] = None
    try:
        rows = conn.execute(
            "SELECT source_key, batch_id, finished_at FROM ingestion_batches "
            "WHERE status = 'committed' ORDER BY finished_at DESC LIMIT 20"
        ).fetchall()
        latest: dict[str, dict[str, Any]] = {}
        for source_key, batch_id, finished_at in rows:
            key = str(source_key or "")
            if key in latest:
                continue
            latest[key] = {
                "batch_id": str(batch_id),
                "finished_at": finished_at.isoformat() if finished_at else None,
            }
        version["sources"] = latest
    except Exception:  # noqa: BLE001
        version["sources"] = {}
    return version


# ══════════════════════════════════════════════════════════════
# 对外主入口
# ══════════════════════════════════════════════════════════════


def preview_filter(db: Session, *, filter_config: Mapping[str, Any] | None,
                   as_of_date: date | None = None,
                   warehouse: Any | None = None,
                   panel: ScreeningPanel | None = None) -> PreviewResult:
    """编译规则 + 执行筛选 + 汇总阻断项。**不落库**。

    `panel` 参数用于测试与复用：传入现成面板则跳过全部 IO。
    `warehouse` 允许调用方复用已打开的 `FactorWarehouse`。

    永远返回 `PreviewResult`（不抛业务异常）；用户是否被阻断由 `can_generate`
    与 `blocking_issues` 表达。编辑过程中的中间态（命中 12 只）必须能正常渲染，
    否则向导 §3.2 的「底部实时更新命中数量」无法实现。
    """
    rules = compile_filter_config(filter_config)

    requested = as_of_date
    resolved: date | None = None
    adjusted = False
    as_of_evidence: dict[str, Any] = {}
    data_version: dict[str, Any] = {}
    outcome: ScreeningOutcome | None = None
    samples: list[dict[str, Any]] = []
    actual_days = 0

    if panel is None:
        wh = warehouse if warehouse is not None else _default_warehouse(db)
        with wh.connection(read_only=True) as conn:
            resolved, adjusted, as_of_evidence = _resolve_as_of_date(conn, requested)
            data_version = _data_version(conn)
        if resolved is not None:
            panel = load_screening_panel(db, rules, as_of_date=resolved, warehouse=wh)
            actual_days = panel.liquidity_days_actual

    if panel is not None:
        if resolved is None:
            resolved = requested or datetime.now(timezone.utc).date()
        outcome = run_screening(rules, panel)
        actual_days = actual_days or panel.liquidity_days_actual
        samples = _build_samples(panel, outcome)

    result = PreviewResult(
        as_of_date=resolved,
        as_of_requested=requested,
        as_of_date_adjusted=adjusted,
        rules=rules,
        outcome=outcome,
        warnings=list(rules.warnings),
        samples=samples,
        data_version=data_version,
        liquidity_actual_days=actual_days,
        as_of_evidence=as_of_evidence,
    )

    if adjusted and requested is not None and resolved is not None:
        result.warnings.append(
            f"请求的数据截止日 {requested.isoformat()} 不可用（该日无估值截面或数据不完整），"
            f"已回退到最近完整交易日 {resolved.isoformat()}。"
        )
    if rules.liquidity and actual_days and actual_days < rules.liquidity_window:
        result.warnings.append(
            f"流动性窗口请求 {rules.liquidity_window} 个交易日，实际只有 {actual_days} 个"
            "（数仓日线起点 2025-01-13）。日均值按实际天数计算，"
            "回看期短于请求值的含义差异会在快照里记录。"
        )
    if outcome is not None and outcome.unmapped_symbols:
        result.warnings.append(
            f"{outcome.unmapped_symbols} 只在数据截止日缺少估值/行情数据"
            f"（估值截面按日覆盖波动很大，实测 8 月约 5,544 只、9 月约 1,904 只）——"
            "引用估值类条件时这些标的会被判为缺失并排除。"
        )

    # 覆盖率偏低（未到阻断线）→ 告警，不阻断
    for field_name, ratio in sorted((outcome.field_coverage if outcome else {}).items()):
        if MIN_REFERENCED_FIELD_COVERAGE <= ratio < LOW_COVERAGE_WARNING:
            binding = FIELD_BINDINGS.get(field_name)
            label = binding.label_zh if binding else field_name
            result.warnings.append(
                f"「{label}」在基础股票池上的可用率只有 {ratio:.1%}，"
                "筛选结果会有系统性偏差（未覆盖的标的被当作缺失排除）。"
                "如不需要该条件请取消，或接受该偏差后继续。"
            )

    result.blocking_issues = _collect_blocking_issues(result)
    return result


def assert_pool_generatable(result: PreviewResult) -> None:
    """生成候选池前的硬校验（向导 §3.5「必须阻断」）。

    由调用方（路由层）在**写成员之前**执行，避免先写后校验。

    优先级 = `blocking_issues` 的既有顺序（`_collect_blocking_issues` 已排好）：
    **根因类在前**（数仓不可用 / 范围冲突 / 字段无数据 / 覆盖率不足），
    **命中量类在后**（0 只 / <50 只）。

    为什么不能用「按 reason 查表」的写法：那会让「用户引用了无数据字段」这种情况
    报成「命中太少」—— 用户会去放宽区间，而真正的问题（那个条件根本不可用）
    被永远掩盖。**报根因，不报症状。**
    """
    if not result.blocking_issues:
        return
    universe_size = result.outcome.universe_size if result.outcome else 0
    first = result.blocking_issues[0]
    reason = first.get("reason")

    if reason == REASON_POOL_TOO_SMALL:
        raise pool_too_small_error(result.hits, universe_size=universe_size)

    if reason == REASON_EMPTY_RESULT:
        raise empty_result_error(universe_size=universe_size,
                                 detail_zh=str(first.get("detail_zh")))

    # 其余（字段不可用 / 覆盖率不足 / 范围冲突 / 跨市场不完整 / 数仓不可用）
    # 统一为 VALIDATION_ERROR，带上 reason 便于前端定位到具体 Accordion。
    raise _validation_error(
        str(first.get("detail_zh")),
        reason=reason,
        code=first.get("code"),
        field=first.get("field"),
    )


__all__ = [
    "MIN_POOL_SIZE",
    "MIN_REFERENCED_FIELD_COVERAGE",
    "SUPPORTED_MARKETS",
    "BOARD_TO_PHYSICAL",
    "BOARD_LABELS_ZH",
    "ALL_BOARD_CODES",
    "CATEGORY_LABELS_ZH",
    "CATEGORY_ORDER",
    "VALID_LOSS_MODES",
    "LOSS_MODE_ANY",
    "LOSS_MODE_ALL_CONSECUTIVE",
    "LOSS_MODE_CUMULATIVE",
    "DEFAULT_LIQUIDITY_WINDOW",
    "MAX_LIQUIDITY_WINDOW",
    "MISSING_EXCLUDE",
    "MISSING_KEEP",
    "FIELD_BINDINGS",
    "FieldBinding",
    "RangeCondition",
    "BlockedField",
    "CompiledRules",
    "CategoryStat",
    "ScreeningOutcome",
    "ScreeningPanel",
    "PreviewResult",
    "compile_filter_config",
    "run_screening",
    "preview_filter",
    "assert_pool_generatable",
    "load_universe",
    "load_valuation",
    "load_liquidity",
    "load_financials_pit",
    "load_screening_panel",
    "list_available_fields",
    "pool_too_small_error",
    "empty_result_error",
    "REASON_EMPTY_RESULT",
    "REASON_POOL_TOO_SMALL",
    "REASON_FIELD_UNAVAILABLE",
    "REASON_FIELD_COVERAGE_INSUFFICIENT",
    "REASON_RANGE_CONFLICT",
    "REASON_UNSUPPORTED_MARKET",
    "REASON_UNSUPPORTED_BOARD",
    "REASON_CROSS_MARKET_INCOMPLETE",
    "REASON_WAREHOUSE_UNAVAILABLE",
    "REASON_INVALID_RULE",
]
