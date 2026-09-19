"""T-C1 / T-C2 / T-D1 / T-B2 联合测试（全纯函数，零 DB / 零 Alembic）。

覆盖：
  - T-C1.2：trade_date=2025-01-06（周一）Asia/Shanghai → decision_at=T 15:05=UTC 07:05；data_cutoff=T 15:00=UTC 07:00；execution=T+1 09:30=2025-01-07 UTC 01:30
  - T-C1.1：5 个核心服务不出现 `datetime.now()`/`datetime.utcnow()`/`pendulum.now()` 裸调用
  - T-C2.1：resolve(portfolio_rule_stage='etf') == resolve(portfolio_rule_stage='stock') == resolve('index')（三资产类共用同一默认常量）
  - T-D1.1：四字段 status_score/status_data/status_model/status_reconciliation 独立赋值（PortfolioStatus 枚举 + 级别映射）
  - T-D2.1：16 组合覆盖 RECONCILIATION_BLOCKED(6) 出现时 = 最终最高；MODEL_INACTIVE(5)、DATA_INCOMPLETE_PAUSED(4)、SCORE_STALE(3)、RUNNING(2)、READY(1) 级别严格
  - T-B2.1：T=01-06，T+1=01-07 open 有效 → resolve_next_open_bar 返回 success=True / roll_forward_days=0 / rejections=[]
  - T-B2.2：T+1 涨跌停一字板（10% up）、T+2 停牌(volume=0)、T+3 有效 → 返回 T+3 open；rejections 累积 2 条（LIMIT_UP_DOWN + SUSPENDED）；intended_open=各自的 T+1/T+2 open
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

from app.services.decision_clock import (
    DEFAULT_EXECUTION_OFFSET_DAYS,
    DEFAULT_EXECUTION_TIME,
    DEFAULT_DATA_CUTOFF_TIME,
    DEFAULT_DECISION_TIME,
    DEFAULT_MARKET_CLOSE_TIME,
    resolve,
    PortfolioRuleStage,
)
from app.services.portfolio_status import (
    PortfolioStatus,
    ResolvedCompositeStatus,
    resolve_composite_status_from_row,
    PORTFOLIO_STATUS_BLOCK_LEVEL,
)
from app.services.match_price_resolver import (
    MatchPriceResult,
    REJECTION_REASON_LIMIT_UP_DOWN,
    REJECTION_REASON_SUSPENDED,
    resolve_next_open_bar,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
SERVICES_DIR = REPO_ROOT / "app" / "services"


# ═══════════════════════════════════════════════════════════════════════════════
# T-C1 + T-C2
# ═══════════════════════════════════════════════════════════════════════════════

class TestTCAndTC2Clock:
    """T-C1.2 三字段精确 UTC 值 + T-C2.1 资产类统一时点。"""

    def test_t_c1_2_2025_01_06_exact_utc_naive_values(self):
        trade = date(2025, 1, 6)  # Monday
        clock = resolve(trade)

        # T 15:05 Asia/Shanghai → UTC 07:05 naive
        assert clock.decision_at == datetime(2025, 1, 6, 7, 5, 0)
        # T 15:00 Asia/Shanghai → UTC 07:00 naive
        assert clock.data_cutoff_at == datetime(2025, 1, 6, 7, 0, 0)
        # T+1 09:30 Asia/Shanghai = 2025-01-07 09:30 +08 → UTC 01:30 naive
        assert clock.execution_at == datetime(2025, 1, 7, 1, 30, 0)

    def test_t_c2_1_etf_equals_stock_equals_index(self):
        trade = date(2025, 3, 4)  # 随便一个工作日（非 DST）
        cl_stock = resolve(trade, portfolio_rule_stage="stock")
        cl_etf = resolve(trade, portfolio_rule_stage="etf")
        cl_index = resolve(trade, portfolio_rule_stage="index")
        assert cl_stock.decision_at == cl_etf.decision_at == cl_index.decision_at
        assert cl_stock.data_cutoff_at == cl_etf.data_cutoff_at == cl_index.data_cutoff_at
        assert cl_stock.execution_at == cl_etf.execution_at == cl_index.execution_at

    def test_t_c2_constant_alignment_with_default_execution_dto(self):
        # T-C2：三字段默认值与集中常量严格一致（避免分散硬编码）
        assert DEFAULT_MARKET_CLOSE_TIME.hour == 15 and DEFAULT_MARKET_CLOSE_TIME.minute == 0
        assert DEFAULT_DECISION_TIME.hour == 15 and DEFAULT_DECISION_TIME.minute == 5
        assert DEFAULT_DATA_CUTOFF_TIME.hour == 15 and DEFAULT_DATA_CUTOFF_TIME.minute == 0
        assert DEFAULT_EXECUTION_TIME.hour == 9 and DEFAULT_EXECUTION_TIME.minute == 30
        assert DEFAULT_EXECUTION_OFFSET_DAYS == 1

    @pytest.mark.parametrize("filename", [
        "auto_trade_member_source.py",
        "portfolio_backtest.py",
        "signal_rules.py",
        "allocation.py",
        "decision_engine.py",
    ])
    def test_t_c1_1_no_raw_datetime_now_in_five_core_services(self, filename: str):
        fpath = SERVICES_DIR / filename
        assert fpath.exists(), f"Core service missing: {fpath}"
        text = fpath.read_text(encoding="utf-8")
        # 裸调用判定：要真的是 Python 语句，不是注释 / 不是字符串字面量里的"datetime.now"：
        #   1) 跳过整行注释（首字符 #）
        #   2) 去掉 # 之后的行尾注释
        #   3) 去掉 "..." 和 '...' 字符串字面量的内容，避免 docstring 里说明性的 "datetime.now(...)" 触发误判
        import re
        _str_re = re.compile(r"('''[\s\S]*?'''|\"\"\"[\s\S]*?\"\"\"|'[^'\n]*'|\"[^\"\n]*\")")
        bad = []
        for idx, raw_line in enumerate(text.splitlines(), start=1):
            line = raw_line
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            if "#" in line:
                # 行尾注释（简单处理：不以字符串内 # 为判定，因为去掉字符串内容后再 split 更安全）
                pass
            line_no_string = _str_re.sub("", line)
            if "#" in line_no_string:
                line_no_string = line_no_string.split("#", 1)[0]
            if (
                "datetime.now(" in line_no_string
                or "datetime.utcnow(" in line_no_string
                or "pendulum.now(" in line_no_string
            ):
                bad.append(f"L{idx}: {raw_line.strip()}")
        assert not bad, (
            f"T-C1.1 FAIL — {filename} contains raw datetime.now/datetime.utcnow/pendulum.now "
            f"calls (outside comments / strings). Replace with decision_clock.resolve() 三字段"
            f" or utcnow_naive():\n"
            + "\n".join(bad)
        )


# ═══════════════════════════════════════════════════════════════════════════════
# T-D1.1 / T-D2
# ═══════════════════════════════════════════════════════════════════════════════

class TestTD1AndTD2Status:
    def test_t_d1_1_four_dimensions_are_independent_fields(self):
        # 四维度允许独立赋值不同值：构造 READY(1) + RUNNING(2) + SCORE_STALE(3) + MODEL_INACTIVE(5)
        res: ResolvedCompositeStatus = resolve_composite_status_from_row(
            status_score=PortfolioStatus.READY.value,
            status_data=PortfolioStatus.RUNNING.value,
            status_model=PortfolioStatus.SCORE_STALE.value,
            status_reconciliation=PortfolioStatus.MODEL_INACTIVE.value,
        )
        # 每个维度返回的 enum 都正确对应原值（证明独立、未被总聚合"污染"）
        assert res.status_score == PortfolioStatus.READY
        assert res.status_data == PortfolioStatus.RUNNING
        assert res.status_model == PortfolioStatus.SCORE_STALE
        assert res.status_reconciliation == PortfolioStatus.MODEL_INACTIVE
        # 最高阻断级别取 max(1,2,3,5) = 5 → MODEL_INACTIVE
        assert res.highest_block_level == 5
        assert res.composite_status == PortfolioStatus.MODEL_INACTIVE

    @pytest.mark.parametrize(
        "one_dim_six",
        list(PortfolioStatus),
        ids=[e.value for e in PortfolioStatus],
    )
    def test_t_d2_1_each_dimension_dominates_symmetrically(self, one_dim_six: PortfolioStatus):
        """D2.1：把某一维置为 X，其余三维 READY(1) → composite 必须等于 X（16 组合对称覆盖的子集）。"""
        for target_dim_idx, target_dim in enumerate([
            "status_score", "status_data", "status_model", "status_reconciliation"
        ]):
            kwargs = {
                "status_score": PortfolioStatus.READY,
                "status_data": PortfolioStatus.READY,
                "status_model": PortfolioStatus.READY,
                "status_reconciliation": PortfolioStatus.READY,
            }
            kwargs[target_dim] = one_dim_six
            res = resolve_composite_status_from_row(**kwargs)
            assert res.composite_status == one_dim_six, (
                f"{target_dim} 置 {one_dim_six.value} 但 composite 是 {res.composite_status.value}"
            )
            assert res.highest_block_level == PORTFOLIO_STATUS_BLOCK_LEVEL[one_dim_six.value]

    def test_t_d2_reconciliation_blocked_6_is_always_highest(self):
        # 三维全顶级 MODEL_INACTIVE(5)，reconciliation=RECONCILIATION_BLOCKED(6) → 必须 6
        res = resolve_composite_status_from_row(
            status_score=PortfolioStatus.MODEL_INACTIVE,
            status_data=PortfolioStatus.MODEL_INACTIVE,
            status_model=PortfolioStatus.MODEL_INACTIVE,
            status_reconciliation=PortfolioStatus.RECONCILIATION_BLOCKED,
        )
        assert res.highest_block_level == 6
        assert res.composite_status == PortfolioStatus.RECONCILIATION_BLOCKED

    def test_t_d1_portfolio_status_enum_level_map_1_through_6(self):
        assert [PORTFOLIO_STATUS_BLOCK_LEVEL[e.value] for e in PortfolioStatus] == [1, 2, 3, 4, 5, 6]


# ═══════════════════════════════════════════════════════════════════════════════
# T-B2.1 / T-B2.2
# ═══════════════════════════════════════════════════════════════════════════════

class TestTB2NextOpenMatch:
    def test_t_b2_1_t_plus_1_open_valid_no_skip(self):
        trade = date(2025, 1, 6)
        t1 = date(2025, 1, 7)  # T+1
        # T-1 close = 10.0；T+1 open=10.5 < 11.0 (10% up) → 正常不跳过
        rows = [
            (t1, 10.5, 10.6, 10.4, 10.55, 1_000_000.0),
        ]
        res: MatchPriceResult = resolve_next_open_bar(rows, prev_close=10.0, max_roll_days=10)
        assert res.success is True
        assert res.final_trade_date == t1
        assert abs(float(res.final_open) - 10.5) <= 1e-9
        assert res.roll_forward_days == 0
        assert res.rejections == []

    def test_t_b2_2_t1_limit_up_t2_suspended_t3_valid_cumulates_two_rejections(self):
        trade = date(2025, 1, 6)
        t1 = date(2025, 1, 7)
        t2 = date(2025, 1, 8)
        t3 = date(2025, 1, 9)
        prev_close = 10.0
        limit_up_price = prev_close * 1.10  # 11.0 — 主板 10% 涨停
        rows = [
            # T+1：涨停一字板（open=high=low=close=11.0，volume>0）→ 跳过
            (t1, limit_up_price, limit_up_price, limit_up_price, limit_up_price, 2_000_000.0),
            # T+2：停牌（volume=0 且无成交；这里 open/high/low/close 即便都是 10.5 也视作停牌）→ 跳过
            (t2, 10.5, 10.5, 10.5, 10.5, 0.0),
            # T+3：有效（非涨跌停，volume>0，非一字板）
            (t3, 10.2, 10.5, 10.1, 10.4, 800_000.0),
        ]
        res = resolve_next_open_bar(rows, prev_close=prev_close, max_roll_days=10)
        assert res.success is True
        assert res.final_trade_date == t3
        assert abs(float(res.final_open) - 10.2) <= 1e-9
        assert res.roll_forward_days == 2  # 索引从 0 起：跳过 t1(0) 和 t2(1)，命中 t3(2)

        # rejection 累积 2 条
        assert len(res.rejections) == 2, f"rejections={[r.as_dict() for r in res.rejections]}"
        r0, r1 = res.rejections
        assert r0.trade_date == t1
        assert r0.reason == REJECTION_REASON_LIMIT_UP_DOWN
        assert abs(float(r0.intended_open) - limit_up_price) <= 1e-9
        assert r1.trade_date == t2
        assert r1.reason == REJECTION_REASON_SUSPENDED
        assert abs(float(r1.intended_open) - 10.5) <= 1e-9
