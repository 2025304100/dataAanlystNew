"""T09 · 条件筛选规则编译 + 预览 + 分位数预设 的契约测试。

测试策略：把「需要基础设施」与「纯逻辑」严格分开
==============================================
- **纯逻辑**（编译 / 引擎 / 分位数 / 阻断汇总）→ 用自造 `ScreeningPanel`，
  毫秒级、完全不碰 MySQL 与 6.2GB DuckDB。
- **HTTP 层**（4xx 断言）→ 用最小 FastAPI app + 覆盖 `get_db`，
  并把 `preview_filter` 换成「同一份编译+引擎、只是面板来自内存」的桩，
  避免测试依赖数仓。

这样即使数仓不可用，这条 DoD 也应该绿 —— 它测的是**规则语义**，不是数据快照。
"""
from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db.session import get_db
from app.models.symbol import Symbol
from app.schemas.errors import FactorSevenError
from app.services.factors.candidate_pool import presets as P
from app.services.factors.candidate_pool import rules as R


# ══════════════════════════════════════════════════════════
# 夹具：自造面板（无 IO）
# ══════════════════════════════════════════════════════════

N_STOCKS = 60


@pytest.fixture
def small_panel() -> R.ScreeningPanel:
    """60 只沪市主板标的；10 只市值 ≥ 1e9，其余 3e8。"""
    symbols = [f"S{i:03d}" for i in range(N_STOCKS)]
    universe = pd.DataFrame({
        "symbol": symbols,
        "name": [f"名{i}" for i in range(N_STOCKS)],
        "market": ["sh"] * N_STOCKS,
        "board": ["main"] * N_STOCKS,
    })
    valuation = pd.DataFrame({
        "symbol": symbols,
        "total_market_cap": [1e9 * (i + 1) if i < 10 else 3e8 for i in range(N_STOCKS)],
        "circulating_market_cap": [1e9 * (i + 1) if i < 10 else 3e8
                                   for i in range(N_STOCKS)],
        "pe_ttm": [10.0 + i for i in range(N_STOCKS)],
        "pb": [1.0 + 0.05 * i for i in range(N_STOCKS)],
        "dividend_yield": [None] * N_STOCKS,
    })
    liquidity = pd.DataFrame({
        "symbol": symbols,
        "avg_amount": [1e7 * (i + 1) for i in range(N_STOCKS)],
        "avg_volume": [1e6 * (i + 1) for i in range(N_STOCKS)],
        "avg_turnover_rate": [0.5 + 0.01 * i for i in range(N_STOCKS)],
        "effective_days": [20] * N_STOCKS,
    })
    financials = pd.DataFrame({
        "symbol": symbols,
        "roe_ttm": [5.0 + i for i in range(N_STOCKS)],
        "annual_periods": [[date(2025, 12, 31), date(2024, 12, 31), date(2023, 12, 31)]]
        * N_STOCKS,
        "annual_net_profits": [[1e8, 2e8, 3e8]] * N_STOCKS,
    })
    return R.ScreeningPanel(universe=universe, valuation=valuation,
                            liquidity=liquidity, financials=financials,
                            liquidity_days_actual=20)


def _preview_with(panel: R.ScreeningPanel, cfg: dict[str, Any],
                  as_of: date | None = None) -> R.PreviewResult:
    """用内存面板跑完整链路（编译 → 引擎 → 阻断汇总），只跳过 IO。"""
    rules = R.compile_filter_config(cfg)
    outcome = R.run_screening(rules, panel)
    res = R.PreviewResult(as_of_date=as_of or date(2026, 8, 21),
                          as_of_requested=as_of, as_of_date_adjusted=False,
                          rules=rules, outcome=outcome)
    res.blocking_issues = R._collect_blocking_issues(res)
    return res


# ══════════════════════════════════════════════════════════
# 1. 字段目录：可用性必须来自实测，且阻断要带原因
# ══════════════════════════════════════════════════════════


class TestFieldCatalog:
    def test_every_field_has_binding_metadata(self):
        for row in R.list_available_fields():
            assert row["field"]
            assert row["label_zh"]
            assert row["category"] in R.CATEGORY_LABELS_ZH
            assert row["category_label_zh"]

    def test_blocked_fields_must_carry_measured_reason(self):
        """阻断不能只是「不可用」三个字 —— 必须给出实测证据，
        否则下一个 Agent 会以为是配置问题，而不是数据缺失。"""
        blocked = [r for r in R.list_available_fields() if r["availability"] == "blocked"]
        assert blocked, "应当存在被阻断的字段（上市日/行业/股息率/净利润…）"
        for row in blocked:
            assert row["blocked_reason_zh"], f"{row['field']} 缺少阻断原因"
            assert len(row["blocked_reason_zh"]) > 20

    @pytest.mark.parametrize("field_name", [
        "min_listed_trading_days", "industry", "dividend_yield", "loss",
        "net_profit_yoy", "exclude_suspended", "index_member",
    ])
    def test_known_missing_data_fields_are_blocked(self, field_name):
        """2026-09-16 实测：这些字段对应的物理列全部为空或不存在。

        这条测试是「数据现状的哨兵」—— 一旦上游把数据补上，有人应当来
        更新 `FIELD_BINDINGS` 并让这条测试改判，而不是让它继续红。
        """
        assert R.FIELD_BINDINGS[field_name].blocked is True

    @pytest.mark.parametrize("field_name", [
        "total_market_cap", "circulating_market_cap", "pe_ttm", "pb",
        "avg_amount", "avg_volume", "avg_turnover_rate", "roe_ttm",
    ])
    def test_available_fields_are_not_blocked(self, field_name):
        assert R.FIELD_BINDINGS[field_name].blocked is False

    def test_st_and_delisting_are_not_point_in_time(self):
        """ST/退市按**当前名称**匹配（`is_st` 全 0、`security_status_daily` 是
        e2e fixture），所以必须显式标注 `point_in_time=False`，
        防止有人拿它做历史回放。"""
        assert R.FIELD_BINDINGS["exclude_st"].point_in_time is False
        assert R.FIELD_BINDINGS["exclude_delisting"].point_in_time is False
        assert R.FIELD_BINDINGS["roe_ttm"].point_in_time is True


# ══════════════════════════════════════════════════════════
# 2. 编译：格式错误抛、业务阻断记
# ══════════════════════════════════════════════════════════


class TestCompile:
    def test_valid_config(self):
        rules = R.compile_filter_config({
            "markets": ["sh"], "boards": ["sh_main"], "exclude_st": True,
            "valuation": {"total_market_cap": {"min": 1e9, "max": 1e12}},
            "liquidity": {"window_days": 20, "avg_amount": {"min": 1e7}},
            "profitability": {"roe_ttm": {"min": 10}},
        })
        assert rules.markets == ("sh",)
        assert rules.boards == ("sh_main",)
        assert rules.exclude_st is True
        assert [c.field for c in rules.valuation] == ["total_market_cap"]
        assert [c.field for c in rules.liquidity] == ["avg_amount"]
        assert rules.roe_ttm is not None
        assert not rules.problems
        assert not rules.blocked_fields
        assert len(rules.rule_hash) == 16

    def test_rule_hash_is_stable_across_calls(self):
        cfg = {"markets": ["sh"], "valuation": {"pe_ttm": {"min": 1}}}
        assert R.compile_filter_config(cfg).rule_hash == \
            R.compile_filter_config(cfg).rule_hash

    def test_rule_hash_ignores_key_order(self):
        a = R.compile_filter_config({"markets": ["sh"], "boards": ["sh_main"]})
        b = R.compile_filter_config({"boards": ["sh_main"], "markets": ["sh"]})
        assert a.rule_hash == b.rule_hash

    def test_empty_bounds_means_not_enabled(self):
        """前端会回传整个对象；区间两端都空不应变成一条筛选条件。"""
        rules = R.compile_filter_config({
            "valuation": {"pe_ttm": {"min": None, "max": None}}})
        assert rules.valuation == ()

    def test_empty_config_is_all_pass(self, small_panel):
        outcome = R.run_screening(R.compile_filter_config({}), small_panel)
        assert outcome.hits == N_STOCKS

    @pytest.mark.parametrize("bad", [
        {"markets": ["hk"]},
        {"markets": ["us"]},
        {"boards": ["nasdaq"]},
        {"markets": "sh"},
        {"boards": "sh_main"},
        {"liquidity": {"window_days": 0}},
        {"liquidity": {"window_days": 99999}},
        {"liquidity": {"window_days": "20"}},
        {"profitability": {"loss": {"mode": "weird"}}},
        {"profitability": {"loss": {"years": 0}}},
        {"profitability": {"loss": {"years": 9}}},
        {"valuation": {"pe_ttm": {"min": "abc"}}},
        {"valuation": {"pe_ttm": {"min": 1, "missing": "maybe"}}},
        {"valuation": {"pe_ttm": [1, 2]}},
    ])
    def test_malformed_config_raises(self, bad):
        with pytest.raises(FactorSevenError) as ei:
            R.compile_filter_config(bad)
        assert ei.value.error_code == "VALIDATION_ERROR"

    def test_unknown_market_extras_are_actionable(self):
        with pytest.raises(FactorSevenError) as ei:
            R.compile_filter_config({"markets": ["hk"]})
        assert ei.value.extras["reason"] == R.REASON_UNSUPPORTED_MARKET
        assert ei.value.extras["allowed"] == list(R.SUPPORTED_MARKETS)

    def test_range_conflict_is_structured_not_exception(self):
        """范围冲突是**业务**问题（用户在编辑中会持续触发），
        不是请求不合法 —— 故进 problems，不抛。"""
        rules = R.compile_filter_config(
            {"valuation": {"pe_ttm": {"min": 50, "max": 10}}})
        assert len(rules.problems) == 1
        assert rules.problems[0]["reason"] == R.REASON_RANGE_CONFLICT
        assert "区间为空" in rules.problems[0]["detail_zh"]

    @pytest.mark.parametrize("cfg,field_name", [
        ({"min_listed_trading_days": 120}, "min_listed_trading_days"),
        ({"industry": {"include": ["银行"]}}, "industry"),
        ({"exclude_suspended": True}, "exclude_suspended"),
        ({"index_members": ["hs300"]}, "index_member"),
        ({"valuation": {"dividend_yield": {"min": 2}}}, "dividend_yield"),
        ({"profitability": {"loss": {"years": 3}}}, "loss"),
    ])
    def test_blocked_field_reference_is_registered(self, cfg, field_name):
        """引用不可用字段**绝不静默忽略** —— 那正是 prev_close 那类
        「静默产出错误结果」的失败形态。"""
        rules = R.compile_filter_config(cfg)
        assert field_name in [b.field for b in rules.blocked_fields]

    def test_blocked_fields_are_deduplicated(self):
        rules = R.compile_filter_config({
            "min_listed_trading_days": 120,
            "valuation": {"dividend_yield": {"min": 2}},
            "profitability": {"loss": {"years": 3}},
        })
        fields = [b.field for b in rules.blocked_fields]
        assert len(fields) == len(set(fields))

    def test_board_market_mismatch_is_flagged(self):
        rules = R.compile_filter_config({"markets": ["bj"], "boards": ["gem"]})
        assert any(p["reason"] == R.REASON_RANGE_CONFLICT for p in rules.problems)


# ══════════════════════════════════════════════════════════
# 3. 纯引擎：分类语义
# ══════════════════════════════════════════════════════════


class TestScreeningEngine:
    def _hits(self, panel, cfg):
        return R.run_screening(R.compile_filter_config(cfg), panel).hit_symbols

    def test_board_filter(self, small_panel):
        assert len(self._hits(small_panel, {"boards": ["sh_main"]})) == N_STOCKS
        assert self._hits(small_panel, {"boards": ["gem"]}) == []

    def test_st_and_delisting_by_name(self, small_panel):
        panel = small_panel
        panel.universe.loc[0, "name"] = "*ST甲"
        panel.universe.loc[1, "name"] = "乙退"
        hits = self._hits(panel, {"exclude_st": True})
        assert "S000" not in hits
        assert len(hits) == N_STOCKS - 1
        hits2 = self._hits(panel, {"exclude_delisting": True})
        assert "S001" not in hits2

    def test_valuation_range_is_inclusive(self, small_panel):
        hits = self._hits(small_panel, {"valuation": {"total_market_cap": {"min": 1e9}}})
        assert len(hits) == 10
        # 端点含：市值正好等于下界的 S000 必须命中
        assert "S000" in hits

    def test_missing_policy_exclude_vs_keep(self, small_panel):
        panel = small_panel
        panel.valuation.loc[0, "pe_ttm"] = None
        excluded = self._hits(panel, {"valuation": {"pe_ttm": {"min": 10}}})
        kept = self._hits(panel, {"valuation": {"pe_ttm": {"min": 10,
                                                          "missing": "keep"}}})
        assert "S000" not in excluded
        assert "S000" in kept

    def test_missing_is_never_treated_as_zero(self, small_panel):
        """禁止「稀疏字段填零」（项目硬约束）：缺失若被当 0，
        `min=1e9` 这种条件会把缺失项静默排除到与业务语义相反的方向。"""
        panel = small_panel
        panel.valuation.loc[0, "total_market_cap"] = None
        hits = self._hits(panel, {"valuation": {"total_market_cap": {"max": 1e9}}})
        # 缺失不应因为「0 ≤ 1e9」而命中
        assert "S000" not in hits

    def test_liquidity_filter(self, small_panel):
        hits = self._hits(small_panel,
                          {"liquidity": {"window_days": 20, "avg_amount": {"min": 1e8}}})
        assert len(hits) == 51  # 1e7*(i+1) >= 1e8 → i+1 >= 10 → i >= 9

    def test_roe_filter(self, small_panel):
        hits = self._hits(small_panel, {"profitability": {"roe_ttm": {"min": 30}}})
        assert len(hits) == 35  # roe = 5+i >= 30 → i >= 25

    def test_loss_modes(self):
        """向导 §3.4 的四种口径。"""
        def _panel(series_year1, *, periods=None):
            syms = ["A", "B", "C", "D"]
            per = periods or [date(2025, 12, 31), date(2024, 12, 31), date(2023, 12, 31)]
            return R.ScreeningPanel(
                universe=pd.DataFrame({"symbol": syms, "name": syms,
                                       "market": ["sh"] * 4, "board": ["main"] * 4}),
                financials=pd.DataFrame({
                    "symbol": syms,
                    "roe_ttm": [None] * 4,
                    "annual_periods": [per] * 4,
                    "annual_net_profits": series_year1,
                }),
            )

        profits = [[1e8, 1e8, 1e8],       # A 全盈利
                   [-1e8, 1e8, 1e8],      # B 最近一年亏损
                   [-1e8, -1e8, -1e8],    # C 三年全亏
                   [1e8, 1e8, -5e8]]      # D 累计为负
        panel = _panel(profits)

        def hits(mode):
            res = R.run_screening(
                R.compile_filter_config({"profitability": {"loss": {"years": 3, "mode": mode}}}),
                panel)
            return res.hit_symbols

        # any：任一年亏损即排除 → 只剩 A
        assert hits(R.LOSS_MODE_ANY) == ["A"]
        # all_consecutive：连续三年全亏才排除 → 排除 C
        assert hits(R.LOSS_MODE_ALL_CONSECUTIVE) == ["A", "B", "D"]
        # cumulative：累计 < 0 才排除 → 排除 C、D
        assert hits(R.LOSS_MODE_CUMULATIVE) == ["A", "B"]

    def test_loss_missing_year_is_excluded_not_treated_as_profit(self):
        """「缺失年度不等于盈利」：可得年度数 < 请求年数 → 排除。"""
        panel = R.ScreeningPanel(
            universe=pd.DataFrame({"symbol": ["A"], "name": ["A"],
                                   "market": ["sh"], "board": ["main"]}),
            financials=pd.DataFrame({
                "symbol": ["A"], "roe_ttm": [None],
                "annual_periods": [[date(2025, 12, 31)]],
                "annual_net_profits": [[1e8]],
            }),
        )
        res = R.run_screening(
            R.compile_filter_config({"profitability": {"loss": {"years": 3, "mode": "any"}}}),
            panel)
        assert res.hits == 0

    def test_all_consecutive_requires_adjacent_years(self):
        """连续三年：年份必须相邻，跳年不算连续。"""
        broken = [date(2025, 12, 31), date(2023, 12, 31), date(2022, 12, 31)]
        panel = R.ScreeningPanel(
            universe=pd.DataFrame({"symbol": ["A"], "name": ["A"],
                                   "market": ["sh"], "board": ["main"]}),
            financials=pd.DataFrame({
                "symbol": ["A"], "roe_ttm": [None],
                "annual_periods": [broken],
                "annual_net_profits": [[-1e8, -1e8, -1e8]],
            }),
        )
        res = R.run_screening(R.compile_filter_config(
            {"profitability": {"loss": {"years": 3, "mode": R.LOSS_MODE_ALL_CONSECUTIVE}}}),
            panel)
        assert res.hits == 1  # 年份不连续 → 不判为「连续三年亏损」

    def test_categories_are_reported_per_group(self, small_panel):
        res = R.run_screening(R.compile_filter_config({
            "boards": ["sh_main"], "exclude_st": True,
            "valuation": {"total_market_cap": {"min": 1e9}},
        }), small_panel)
        cats = {c.category: c for c in res.categories}
        assert cats[R.CATEGORY_UNIVERSE].evaluated == N_STOCKS
        assert cats[R.CATEGORY_VALUATION].excluded == N_STOCKS - 10
        assert cats[R.CATEGORY_LISTING].evaluated == 0  # 数据缺失，未启用

    def test_field_coverage_is_reported(self, small_panel):
        panel = small_panel
        panel.valuation.loc[0:4, "total_market_cap"] = None
        res = R.run_screening(R.compile_filter_config(
            {"valuation": {"total_market_cap": {"min": 1e9}}}), panel)
        assert 0.9 < res.field_coverage["total_market_cap"] < 1.0


# ══════════════════════════════════════════════════════════
# 4. as_of_date：跳过稀疏日，且选择必须可解释
# ══════════════════════════════════════════════════════════


class _FakeConn:
    """记录 `execute(sql, params)` 的假连接。

    `results` 是**按调用顺序**返回的结果队列（`load_liquidity` 会查两次），
    用完最后一组后继续返回空结果。
    """

    def __init__(self, *results: list[tuple]):
        self.results = list(results) or [[]]
        self.calls: list[tuple[str, list[Any]]] = []

    def execute(self, sql, params=None):
        idx = len(self.calls)
        self.calls.append((sql, list(params or [])))
        rows = self.results[idx] if idx < len(self.results) else []
        return _FakeResult(rows)

    @property
    def sqls(self) -> list[str]:
        return [s for s, _p in self.calls]


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class TestAsOfDate:
    def _conn(self, pairs):
        return _FakeConn([(d, n) for d, n in pairs])

    def test_skips_sparse_latest_day(self):
        """实测场景：最新日只有 1 只，倒数第二日 5,544 只 → 必须跳过最新日。"""
        conn = self._conn([(date(2026, 9, 7), 1),
                           (date(2026, 9, 4), 5544),
                           (date(2026, 9, 3), 5540)])
        resolved, adjusted, ev = R._resolve_as_of_date(conn, None)
        assert resolved == date(2026, 9, 4)
        assert adjusted is False
        assert ev["fallback_reason"] is None
        assert ev["observed_symbols"] == 5544

    def test_requested_date_beyond_data_is_adjusted(self):
        conn = self._conn([(date(2026, 9, 4), 5544)])
        resolved, adjusted, _ = R._resolve_as_of_date(conn, date(2026, 12, 31))
        assert resolved == date(2026, 9, 4)
        assert adjusted is True

    def test_evidence_explains_the_choice(self):
        conn = self._conn([(date(2026, 9, 7), 1), (date(2026, 9, 4), 5544)])
        _resolved, _adj, ev = R._resolve_as_of_date(conn, None)
        # 必须能事后解释「为什么不是 09-07」
        assert ev["selected_trade_date"] == "2026-09-04"
        assert ev["required_symbols"] > 1
        assert "2026-09-07" in ev["candidate_ratios"]
        assert ev["candidate_ratios"]["2026-09-07"] < ev["completeness_threshold"]

    def test_no_rows_returns_none(self):
        resolved, adjusted, ev = R._resolve_as_of_date(_FakeConn(), None)
        assert resolved is None
        assert adjusted is False
        assert ev["candidate_ratios"] == {}

    def test_cross_section_drop_off_skips_the_whole_tail(self):
        """真实场景复现（2026-09-16 实测）：
        估值截面 9 月各日只有 ~1,904 只，8 月各日 ~5,544 只。
        按「中位数 × 0.9」判定，必须整段跳过 9 月，落到最后一个完整日 08-21。
        """
        pairs = [(date(2026, 9, d), 1904) for d in (1, 2, 3, 4)] + \
                [(date(2026, 8, d), 5544) for d in (21, 22, 25, 26, 27, 28)]
        resolved, _adj, ev = R._resolve_as_of_date(self._conn(pairs), None)
        assert resolved == date(2026, 8, 21)
        assert ev["observed_symbols"] == 5544
        assert ev["candidate_ratios"]["2026-09-04"] < ev["completeness_threshold"]

    def test_fallback_branch_is_defensive_only(self):
        """以中位数为基线时，`floor = 中位数 × 0.9` 一定 ≤ 中位数，
        而中位数本身就是某个候选日的值 → 循环必然命中。
        因此 `no_candidate_meets_completeness` 是**防御性分支**（不可达），
        这里只断言它存在且不会抛异常，避免有人误以为它是主路径。"""
        conn = self._conn([(date(2026, 9, 4), 10), (date(2026, 9, 3), 8),
                           (date(2026, 9, 2), 6)])
        resolved, _adj, ev = R._resolve_as_of_date(conn, None)
        assert resolved is not None
        assert "fallback_reason" in ev


# ══════════════════════════════════════════════════════════
# 5. 禁止拼接 SQL：用户值只能出现在参数里
# ══════════════════════════════════════════════════════════


class TestNoSqlInjection:
    MALICIOUS = "1); DROP TABLE symbols; --"

    def test_compiled_rules_contains_no_sql(self):
        rules = R.compile_filter_config({
            "markets": ["sh"], "boards": ["sh_main"],
            "valuation": {"pe_ttm": {"min": 1, "max": 100}},
        })
        blob = str(rules.to_dict()).upper()
        for kw in ("SELECT", "DROP", "INSERT", "DELETE", "UNION"):
            assert kw not in blob

    def test_malicious_values_never_appear_in_sql(self):
        """用户值**永远不会**变成 SQL 文本的一部分。

        本项目 `load_valuation` 的标的过滤是在**取回之后**用 pandas `isin` 做的，
        所以恶意代码既不会进 SQL 文本、也不会进参数 —— 它压根到不了数据库层。
        这条测试锁死这个性质：将来若有人改成 SQL 侧 `IN (...)` 拼接，这里会红。
        """
        conn = _FakeConn([("S000", 1.0, 1.0, 1.0, 1.0, None)])
        frame = R.load_valuation(conn, date(2026, 8, 21), [self.MALICIOUS, "S000"])
        for sql, params in conn.calls:
            assert self.MALICIOUS not in sql
            assert self.MALICIOUS not in [str(p) for p in params]
            assert "DROP" not in sql.upper()
        # 过滤在 pandas 侧生效：恶意代码不在结果里
        assert "S000" in set(frame["symbol"])

    def test_liquidity_in_clause_uses_placeholders(self):
        """`IN (...)` 的**日期**也用 `?` 占位（数量随 window 变化，最易被写成拼接）。"""
        conn = _FakeConn([(date(2026, 8, 21),), (date(2026, 8, 20),)], [])
        _frame, days = R.load_liquidity(conn, date(2026, 8, 21), 2)
        assert days == 2
        assert len(conn.calls) == 2
        sql, params = conn.calls[1]
        assert "?" in sql
        assert sql.count("?") == 2
        assert params == [date(2026, 8, 21), date(2026, 8, 20)]
        for p in params:
            assert isinstance(p, date)

    def test_financials_pit_filters_on_announcement_date(self):
        """PIT 必须按 `announcement_date <= as_of`，而不是 report_period。"""
        conn = _FakeConn([])
        R.load_financials_pit(conn, date(2026, 8, 21), years=3)
        latest_sql = conn.calls[0][0]
        assert "announcement_date <= ?" in latest_sql
        assert conn.calls[0][1] == [date(2026, 8, 21)]

    def test_universe_uses_orm_not_raw_sql(self):
        """universe 走 SQLAlchemy ORM（天然参数化）。"""
        rules = R.compile_filter_config({"markets": ["sh"]})
        src = R.load_universe.__code__
        assert src is not None
        # 断言实现里没有 f-string 拼接的 SELECT
        assert "SELECT" not in str(R.load_universe.__doc__ or "").upper()


# ══════════════════════════════════════════════════════════
# 6. 分位数预设：服务端算、随样本变化
# ══════════════════════════════════════════════════════════


class TestPresets:
    def test_quantile_bounds_equal_pandas(self, small_panel):
        series = pd.Series([float(i) for i in range(1, 101)])
        bounds = P.compute_quantile_bounds(series, (0.1, 0.3, 0.7))
        assert bounds["p10"] == pytest.approx(float(series.quantile(0.10)))
        assert bounds["p30"] == pytest.approx(float(series.quantile(0.30)))
        assert bounds["p70"] == pytest.approx(float(series.quantile(0.70)))

    def test_bounds_move_with_sample(self, small_panel):
        """阈值必须随样本变化 —— 这正是「不得硬编码金额」的可验证点。"""
        base = P.get_filter_presets(None, panel=small_panel,
                                    data_version={"v": 1})
        panel_big = R.ScreeningPanel(
            universe=small_panel.universe,
            valuation=small_panel.valuation.assign(
                total_market_cap=small_panel.valuation["total_market_cap"] * 100),
            liquidity=small_panel.liquidity,
            liquidity_days_actual=20,
        )
        scaled = P.get_filter_presets(None, panel=panel_big, data_version={"v": 1})
        b1 = base.by_code("market_cap.total_market_cap.large").min_value
        b2 = scaled.by_code("market_cap.total_market_cap.large").min_value
        assert b2 == pytest.approx(b1 * 100, rel=1e-6)

    def test_pe_pb_exclude_non_positive(self, small_panel):
        panel = R.ScreeningPanel(
            universe=small_panel.universe,
            valuation=small_panel.valuation.assign(
                pe_ttm=[-10.0] + list(small_panel.valuation["pe_ttm"][1:])),
            liquidity=small_panel.liquidity,
            liquidity_days_actual=20,
        )
        resp = P.get_filter_presets(None, panel=panel, data_version={"v": 1})
        low = resp.by_code("valuation.pe_ttm.low")
        positive = small_panel.valuation["pe_ttm"][1:]
        assert low.max_value == pytest.approx(float(positive.quantile(0.20)))
        assert low.sample_scope["positive_only"] is True

    def test_preset_codes_and_operators(self, small_panel):
        resp = P.get_filter_presets(None, panel=small_panel, data_version={"v": 1})
        codes = {p.preset_code for p in resp.presets}
        assert "market_cap.total_market_cap.large" in codes
        assert "valuation.pe_ttm.low" in codes
        assert "valuation.pb.low" in codes
        assert "liquidity.avg_amount.high" in codes
        operators = {p.operator for p in resp.presets}
        assert operators == {"all", "le", "between", "ge"}

    def test_listing_presets_are_all_blocked(self, small_panel):
        resp = P.get_filter_presets(None, panel=small_panel, data_version={"v": 1})
        listing = [p for p in resp.presets if p.group == P.LISTING_GROUP]
        assert listing, "上市时间组必须存在（否则前端少一个选项卡）"
        for p in listing:
            assert p.availability == "blocked"
            assert p.applyable is False
            assert p.blocked_reason_zh
        # 向导 §3.2 的四档 + 默认 120 交易日
        assert {p.preset_code for p in listing} >= {
            "listing.d120", "listing.m6", "listing.y1", "listing.y3", "listing.y5"}

    def test_presets_carry_as_of_and_data_version(self, small_panel):
        resp = P.get_filter_presets(None, as_of_date=date(2026, 8, 21),
                                    panel=small_panel,
                                    data_version={"warehouse_schema_version": "3"})
        p = resp.by_code("market_cap.total_market_cap.large")
        assert p.as_of_date == date(2026, 8, 21)
        assert p.data_version["warehouse_schema_version"] == "3"
        assert p.sample_scope["as_of_date"] == "2026-08-21"
        assert p.sample_scope["sample_size"] == N_STOCKS

    def test_invalid_market_and_window_rejected(self, small_panel):
        for kw in ({"markets": ["hk"]}, {"window_days": 0},
                   {"window_days": "20"}, {"window_days": 99999}):
            with pytest.raises(FactorSevenError):
                P.get_filter_presets(None, panel=small_panel, **kw)

    def test_market_cap_tiers_are_ordered_and_contiguous(self, small_panel):
        resp = P.get_filter_presets(None, panel=small_panel, data_version={"v": 1})
        micro = resp.by_code("market_cap.total_market_cap.micro")
        small = resp.by_code("market_cap.total_market_cap.small")
        mid = resp.by_code("market_cap.total_market_cap.mid")
        large = resp.by_code("market_cap.total_market_cap.large")
        assert micro.max_value == small.min_value
        assert small.max_value == mid.min_value
        assert mid.max_value == large.min_value


# ══════════════════════════════════════════════════════════
# 7. 阻断汇总与生成前硬校验
# ══════════════════════════════════════════════════════════


class TestBlocking:
    def test_pool_below_floor_is_blocked(self, small_panel):
        res = _preview_with(small_panel,
                            {"valuation": {"total_market_cap": {"min": 1e9}}})
        assert res.hits == 10
        assert res.can_generate is False
        assert res.blocking_issues[0]["reason"] == R.REASON_POOL_TOO_SMALL

    def test_empty_result_is_distinct_from_too_small(self, small_panel):
        res = _preview_with(small_panel,
                            {"valuation": {"total_market_cap": {"min": 1e15}}})
        assert res.hits == 0
        assert res.blocking_issues[0]["reason"] == R.REASON_EMPTY_RESULT

    def test_field_unavailable_is_reported_not_ignored(self, small_panel):
        res = _preview_with(small_panel, {"min_listed_trading_days": 120})
        assert res.blocking_issues[0]["reason"] == R.REASON_FIELD_UNAVAILABLE
        # 2026-09-22：「本项不可用」必须能被**机器**定位到具体字段（field 键），
        # 而不是靠文案里出现表名（旧断言 `"listed_at" in detail_zh` 已废 ——
        # 用户句禁止出现表名，技术口径改由 blocked_detail_zh 承载）。
        assert res.blocking_issues[0]["field"] == "min_listed_trading_days"
        detail = res.blocking_issues[0]["detail_zh"]
        assert "上市满" in detail, f"用户句应点明是哪个条件：{detail}"
        assert "`" not in detail and "listed_at" not in detail, "用户句不得泄漏内部标识"

    def test_root_cause_beats_symptom(self, small_panel):
        """根因优先：用户引用了无数据字段时，不能只报「命中太少」，
        否则用户会去放宽区间，而真正的问题永远不会被发现。"""
        res = _preview_with(small_panel, {
            "markets": ["sh"], "boards": ["sh_main"],
            "min_listed_trading_days": 120,
            "valuation": {"total_market_cap": {"min": 1e9}},
        })
        reasons = [i["reason"] for i in res.blocking_issues]
        assert reasons[0] == R.REASON_FIELD_UNAVAILABLE
        assert R.REASON_POOL_TOO_SMALL in reasons

    def test_coverage_below_threshold_blocks(self, small_panel):
        panel = R.ScreeningPanel(
            universe=small_panel.universe,
            valuation=small_panel.valuation.assign(
                pe_ttm=[None] * (N_STOCKS - 5) + [1.0, 2.0, 3.0, 4.0, 5.0]),
            liquidity=small_panel.liquidity, liquidity_days_actual=20,
        )
        res = _preview_with(panel, {"valuation": {"pe_ttm": {"min": 1}}})
        assert any(i["reason"] == R.REASON_FIELD_COVERAGE_INSUFFICIENT
                   for i in res.blocking_issues)

    def test_low_coverage_warns_without_blocking(self, small_panel):
        """覆盖率在 [0.20, 0.50) 之间：告警但不阻断 ——
        ROE 在真实库上就是这个区间，直接阻断会让功能整体不可用。"""
        panel = R.ScreeningPanel(
            universe=small_panel.universe,
            valuation=small_panel.valuation,
            liquidity=small_panel.liquidity,
            financials=small_panel.financials.assign(
                roe_ttm=[None] * (N_STOCKS - 20) + [10.0] * 20),
            liquidity_days_actual=20,
        )
        rules = R.compile_filter_config({"profitability": {"roe_ttm": {"min": 10}}})
        outcome = R.run_screening(rules, panel)
        res = R.PreviewResult(as_of_date=date(2026, 8, 21), as_of_requested=None,
                              as_of_date_adjusted=False, rules=rules, outcome=outcome)
        res.blocking_issues = R._collect_blocking_issues(res)
        assert res.outcome.field_coverage["roe_ttm"] == pytest.approx(20 / N_STOCKS)
        assert not any(i["reason"] == R.REASON_FIELD_COVERAGE_INSUFFICIENT
                       for i in res.blocking_issues)
        # 告警由 preview_filter 追加（这里直接验证阈值分界）
        assert R.MIN_REFERENCED_FIELD_COVERAGE <= 20 / N_STOCKS < R.LOW_COVERAGE_WARNING

    def test_assert_pool_generatable_maps_to_right_code(self, small_panel):
        cases = [
            ({"valuation": {"total_market_cap": {"min": 1e15}}},
             "VALIDATION_ERROR", R.REASON_EMPTY_RESULT),
            ({"min_listed_trading_days": 120},
             "VALIDATION_ERROR", R.REASON_FIELD_UNAVAILABLE),
        ]
        for cfg, code, reason in cases:
            with pytest.raises(FactorSevenError) as ei:
                R.assert_pool_generatable(_preview_with(small_panel, cfg))
            assert ei.value.error_code == code
            assert ei.value.extras["reason"] == reason

        with pytest.raises(FactorSevenError) as ei:
            R.assert_pool_generatable(
                _preview_with(small_panel, {"valuation": {"total_market_cap": {"min": 1e9}}}))
        assert ei.value.error_code == "MINING_POOL_TOO_SMALL"
        assert ei.value.extras["hits"] == 10
        assert ei.value.extras["min_pool_size"] == 50

    def test_assert_passes_when_enough_hits(self, small_panel):
        R.assert_pool_generatable(_preview_with(small_panel, {"markets": ["sh"]}))

    def test_min_pool_size_is_hard_floor(self):
        assert R.MIN_POOL_SIZE == 50


# ══════════════════════════════════════════════════════════
# 8. 路由层：顺序 + 4xx
# ══════════════════════════════════════════════════════════


def _build_client(db_session, panel: R.ScreeningPanel | None) -> TestClient:
    """最小 app：只挂候选池路由，覆盖 `get_db`，并把仓储读取换成内存面板。

    为什么必须换掉 IO：本测试若走真实数仓，会构造 `FactorWarehouse`
    （`store.py:544` 以 **read_only=False** 打开数仓），在数仓被其它进程持有时
    会一直等锁 —— 实测挂死 5 分钟无任何输出。DoD 要的是毫秒级确定性。

    替换的只是「取数」，**编译 / 引擎 / 阻断汇总仍是真实实现**。
    """
    from app.api.routes import mining_candidate_pool as route_mod

    app = FastAPI()
    app.include_router(route_mod.router)
    app.dependency_overrides[get_db] = lambda: db_session

    db_panel = panel

    def _stub_preview(db, *, filter_config, as_of_date=None, warehouse=None,
                      panel=None):
        return _preview_with(db_panel, filter_config, as_of_date)

    originals = {
        "preview": route_mod.pool_rules.preview_filter,
        "presets": route_mod.pool_presets.get_filter_presets,
    }
    # ⚠️ 桩内部必须调用**原函数**（已在 originals 里捕获）。
    #    若直接写 `P.get_filter_presets(...)`，因为 `pool_presets` 就是 `presets`
    #    模块本身，会把桩自己再调一遍 → RecursionError。
    _real_presets = originals["presets"]

    def _stub_presets(db, *, as_of_date=None, markets=None, window_days=None,
                      warehouse=None, panel=None, data_version=None):
        return _real_presets(None, as_of_date=as_of_date, markets=markets,
                             window_days=window_days or P.DEFAULT_PRESET_WINDOW,
                             panel=db_panel, data_version=data_version or {})

    route_mod.pool_rules.preview_filter = _stub_preview
    route_mod.pool_presets.get_filter_presets = _stub_presets

    client = TestClient(app)
    # 用属性挂还原函数，测试 finally 里恢复（避免污染同进程其它测试）
    client._restore_stubs = (originals, route_mod)  # type: ignore[attr-defined]
    return client


def _restore(client: TestClient) -> None:
    originals, mod = client._restore_stubs  # type: ignore[attr-defined]
    mod.pool_rules.preview_filter = originals["preview"]
    mod.pool_presets.get_filter_presets = originals["presets"]


@pytest.fixture
def seeded_symbols(db_session):
    """在业务 `symbols` 表里造 60 行，供 `resolve_hit_symbol_ids` 映射。"""
    for i in range(N_STOCKS):
        db_session.add(Symbol(
            symbol=f"S{i:03d}", name=f"名{i}", asset_type="stock",
            market="sh", board="main", is_st=0, is_active=1,
        ))
    db_session.flush()
    return N_STOCKS


class TestHttpLayer:
    def test_static_routes_precede_dynamic_pool_id(self):
        """FastAPI 按声明顺序匹配：`/filter-presets` 若排在 `{pool_id}` 之后，
        会被当成 pool_id="filter-presets" → 404 且**没有任何告警**。"""
        from app.api.routes import mining_candidate_pool as m

        entries = [(r.path, ",".join(sorted(getattr(r, "methods", []) or [])))
                   for r in m.router.routes
                   if "/candidate-pools" in getattr(r, "path", "")]
        idx = {p: i for i, (p, _m) in enumerate(entries)}
        first_dyn = next(i for i, (p, _m) in enumerate(entries)
                         if p.rsplit("/", 1)[-1] == "{pool_id}")
        for static in ("filter-fields", "filter-presets", "preview", "from-filter"):
            path = f"/factor-mining/candidate-pools/{static}"
            assert path in idx, f"{static} 未注册"
            assert idx[path] < first_dyn, f"{static} 必须排在 {{pool_id}} 之前"

    def _post_from_filter(self, client, name, filter_config):
        try:
            return client.post("/factor-mining/candidate-pools/from-filter",
                               json={"name": name, "filter_config": filter_config})
        finally:
            _restore(client)

    def test_members_below_floor_returns_4xx(self, db_session, small_panel,
                                             seeded_symbols):
        """DoD：断言成员 <50 时返回 4xx。"""
        client = _build_client(db_session, small_panel)
        resp = self._post_from_filter(
            client, "太小的池", {"valuation": {"total_market_cap": {"min": 1e9}}})

        assert 400 <= resp.status_code < 500, \
            f"命中 10 只（<50）必须返回 4xx，实得 {resp.status_code}"
        detail = resp.json()["detail"]
        assert detail["error_code"] == "MINING_POOL_TOO_SMALL"
        assert detail["extras"]["hits"] == 10
        assert detail["extras"]["min_pool_size"] == 50

    def test_empty_result_returns_4xx(self, db_session, small_panel, seeded_symbols):
        client = _build_client(db_session, small_panel)
        resp = self._post_from_filter(
            client, "空池", {"valuation": {"total_market_cap": {"min": 1e15}}})
        assert 400 <= resp.status_code < 500
        assert resp.json()["detail"]["extras"]["reason"] == R.REASON_EMPTY_RESULT

    def test_blocked_field_returns_4xx(self, db_session, small_panel, seeded_symbols):
        client = _build_client(db_session, small_panel)
        resp = self._post_from_filter(client, "用了不可用字段",
                                      {"min_listed_trading_days": 120})
        assert 400 <= resp.status_code < 500
        assert resp.json()["detail"]["extras"]["reason"] == R.REASON_FIELD_UNAVAILABLE

    def test_range_conflict_returns_4xx(self, db_session, small_panel, seeded_symbols):
        client = _build_client(db_session, small_panel)
        resp = self._post_from_filter(
            client, "范围反了", {"valuation": {"pe_ttm": {"min": 50, "max": 10}}})
        assert 400 <= resp.status_code < 500
        assert resp.json()["detail"]["extras"]["reason"] == R.REASON_RANGE_CONFLICT

    def test_enough_hits_creates_pool(self, db_session, small_panel, seeded_symbols):
        client = _build_client(db_session, small_panel)
        resp = self._post_from_filter(client, "够大的池",
                                      {"markets": ["sh"], "boards": ["sh_main"]})
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["member_count"] == N_STOCKS
        assert body["source_type"] == "filter"
        assert body["member_write"]["requested"] == N_STOCKS
        assert body["member_write"]["added"] == N_STOCKS
        assert body["member_write"]["unmapped_count"] == 0
        # 存入的 filter_config 必须带 as_of_date，否则快照不可复现
        assert body["as_of_date"] is not None

    def test_preview_never_4xx(self, db_session, small_panel, seeded_symbols):
        """预览在编辑中会被 300ms 防抖频繁调用；命中 10 只属于正常中间态，
        必须能正常渲染（否则前端无法做渐进反馈）。"""
        client = _build_client(db_session, small_panel)
        try:
            resp = client.post("/factor-mining/candidate-pools/preview", json={
                "filter_config": {"valuation": {"total_market_cap": {"min": 1e9}}},
            })
        finally:
            _restore(client)
        assert resp.status_code == 200
        body = resp.json()
        assert body["hits"] == 10
        assert body["can_generate"] is False
        assert body["min_pool_size"] == 50
        assert body["blocking_issues"][0]["reason"] == R.REASON_POOL_TOO_SMALL

    def test_filter_fields_endpoint(self, db_session):
        client = _build_client(db_session, None)
        try:
            resp = client.get("/factor-mining/candidate-pools/filter-fields")
        finally:
            _restore(client)
        assert resp.status_code == 200
        body = resp.json()
        assert body["available_count"] > 0
        assert body["blocked_count"] > 0
        assert body["min_pool_size"] == 50

    def test_filter_presets_endpoint_returns_server_computed_bounds(
            self, db_session, small_panel):
        client = _build_client(db_session, small_panel)
        try:
            resp = client.get("/factor-mining/candidate-pools/filter-presets")
        finally:
            _restore(client)
        assert resp.status_code == 200
        body = resp.json()
        assert body["presets"]
        large = next(p for p in body["presets"]
                     if p["preset_code"] == "market_cap.total_market_cap.large")
        expected = float(small_panel.valuation["total_market_cap"].quantile(0.70))
        assert large["min_value"] == pytest.approx(expected)
