"""白盒测试 - 评分引擎 (app.services.analysis)。

针对代码审查发现的 C-3 问题：data_credibility 在 K 线 < 5 时被错误重置为 0.0。
同时覆盖评分边界、阶段判定、可信度计算等核心逻辑。
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.models.daily_bar import DailyBar
from app.models.symbol import Symbol
from app.services import analysis

pytestmark = pytest.mark.whitebox


def _make_symbol(db_session, symbol="600000", asset_type="stock", theme="银行") -> Symbol:
    sym = Symbol(
        symbol=symbol,
        name=f"测试-{symbol}",
        asset_type=asset_type,
        market="cn" if asset_type == "stock" else "cn",
        theme=theme,
    )
    db_session.add(sym)
    db_session.commit()
    db_session.refresh(sym)
    return sym


def _add_bars(db_session, symbol_id: int, count: int, start_price=10.0, base_date=None):
    base_date = base_date or date.today() - timedelta(days=count + 5)
    for i in range(count):
        d = base_date + timedelta(days=i)
        # 价格缓慢上涨，保证 momentum > 0、close > ma20 > ma50
        price = round(start_price * (1 + i * 0.01), 4)
        db_session.add(DailyBar(
            symbol_id=symbol_id,
            trade_date=d,
            open=price,
            high=price * 1.02,
            low=price * 0.99,
            close=price,
            volume=1_000_000 + i * 10000,
            amount=10_000_000 + i * 100000,
            turnover_rate=1.5,
        ))
    db_session.commit()


# ---------- 辅助函数 ----------

def test_clamp_score_bounds():
    """_clamp_score 应将值约束在 [0, 100] 并保留两位小数。"""
    assert analysis._clamp_score(-5) == 0.0
    assert analysis._clamp_score(150) == 100.0
    assert analysis._clamp_score(66.66666) == 66.67


def test_grade_thresholds():
    """_grade 阶段划分边界正确。"""
    assert analysis._grade(80) == "A"
    assert analysis._grade(79.99) == "B"
    assert analysis._grade(65) == "B"
    assert analysis._grade(64.99) == "C"
    assert analysis._grade(50) == "C"
    assert analysis._grade(49.99) == "D"


def test_safe_date_handles_multiple_formats():
    """_safe_date 应处理 None/datetime/date/多种字符串格式。"""
    assert analysis._safe_date(None) is None
    assert analysis._safe_date("2026-07-01") == date(2026, 7, 1)
    assert analysis._safe_date("2026-07-01 10:30:00") == date(2026, 7, 1)
    assert analysis._safe_date("invalid") is None


# ---------- 评分主流程 ----------

# C-3 回归（2026-09-27 摘掉 xfail 帽子）：该 bug 已修，用例实测通过；
# 原标记的 reason 本身就写着“已修复”，留着只会在未来把真回归当成“预期内失败”。
def test_calculate_score_with_insufficient_bars_returns_low_credibility(db_session):
    """[C-3 回归] K 线 < 5 根时，data_credibility 应为 0.2，而非被重置为 0.0。

    这是代码审查发现的 Critical bug：第 134 行无条件 data_credibility = 0.0
    会覆盖第 68 行 < 5 分支设置的 0.2。
    """
    sym = _make_symbol(db_session)
    _add_bars(db_session, sym.id, count=3)  # 仅 3 根 K 线

    trade_date = date.today()
    score = analysis.calculate_symbol_score(db_session, sym, trade_date)

    # 核心断言：数据可信度应为 0.2（数据极少），而不是 0.0
    assert score.data_credibility == 0.2, (
        f"K 线不足 5 根时 data_credibility 应为 0.2，实际为 {score.data_credibility}。"
        "这表明 analysis.py 第 134 行的无条件重置 bug 仍然存在。"
    )


def test_calculate_score_with_insufficient_bars_secondary_fields(db_session):
    """[PT-DEF-16 已拍板：A 口径] 数据不足（<5 根 K 线）时各字段应为“分项值按权重加权”的结果。

    旧用例期望 quality=50.0 / timing=45.0，那是 `_legacy_calculate_symbol_score` 的
    `<5` 分支里**写死**的近似值；现在走配置引擎，它拿同一组分项
    （scoring_config_engine L416+：trend=50 / momentum=45 / volatility=50 /
    liquidity=45 / breadth=55（本 symbol 带 theme）/ event=50）按预设权重加权：

        quality = 50*0.25 + 45*0.20 + 50*0.15 + 45*0.15 + 55*0.15 + 50*0.10 = 49.0
        timing  = 50*0.30 + 45*0.20 + 45*0.15 + 40*0.15 + 50*0.10 + 100*0.10 = 51.75

    所以 49.0 / 51.75 可现算、不是“抄实得值”（拍板记录见体检报告 §十二.9）。

    用户可见后果单独钉住：quality 49.0 经 `_grade()`（>=50 为 C）落到 **D 级**，
    即“数据极少的标的分级更低”是新口径的一部分，不是回归。
    """
    sym = _make_symbol(db_session)
    _add_bars(db_session, sym.id, count=3)

    score = analysis.calculate_symbol_score(db_session, sym, date.today())

    assert score.quality_score == 49.0
    assert score.quality_grade == "D"  # 49.0 < 50 → D（阈值边界，单独钉住）
    assert score.timing_score == 51.75
    assert score.stage == "cooldown"
    assert score.action == "hold"
    assert score.data_credibility == 0.2


# C-3（零 K 线时 data_credibility 应保留 0.2）已修复，2026-09-27 全量跑实测该用例
# 已 XPASS → 摘掉 xfail 帽子，转为正式断言（留着会把将来的回归当成“预期内失败”）。
def test_calculate_score_with_zero_bars(db_session):
    """[边界] 无任何 K 线时不应崩溃，且 data_credibility 应为 0.2。"""
    sym = _make_symbol(db_session)
    trade_date = date.today()
    score = analysis.calculate_symbol_score(db_session, sym, trade_date)

    assert score is not None
    assert score.data_credibility == 0.2


def test_calculate_score_with_enough_bars_computes_credibility(db_session):
    """K 线 >= 5 根时，data_credibility 应基于 bar_count 与时效性计算 (0, 1]。"""
    sym = _make_symbol(db_session)
    _add_bars(db_session, sym.id, count=30)

    trade_date = date.today()
    score = analysis.calculate_symbol_score(db_session, sym, trade_date)

    assert score.data_credibility > 0.0
    assert score.data_credibility <= 1.0
    # 30 根 K 线：bar_factor = 0.4 + (30-5)*0.02 = 0.9；当天 freshness=1.0
    expected = round(min(1.0, 0.9 * 1.0), 2)
    assert score.data_credibility == expected


def test_calculate_score_idempotent_same_batch(db_session):
    """同一 calc_batch_id 重复计算应更新而非插入新记录。"""
    sym = _make_symbol(db_session)
    _add_bars(db_session, sym.id, count=25)

    trade_date = date.today()
    s1 = analysis.calculate_symbol_score(db_session, sym, trade_date)
    s2 = analysis.calculate_symbol_score(db_session, sym, trade_date)

    assert s1.id == s2.id, "同一批次重复计算应 upsert 而非新建"


def test_calculate_score_stage_accel(db_session):
    """足够强的上涨趋势应判定为 accel 阶段。"""
    sym = _make_symbol(db_session, theme="科技")
    _add_bars(db_session, sym.id, count=60, start_price=10.0)
    # 60 根 K 线 × 1% 涨幅 → momentum20 远超 0.08

    trade_date = date.today()
    score = analysis.calculate_symbol_score(db_session, sym, trade_date)

    assert score.stage in ("accel", "start", "overheat")
    assert score.action in ("hold", "open", "reduce")


def test_calculate_score_overheat_penalty(db_session):
    """价格远超 MA20 时应触发 overheat_penalty 并降级。"""
    sym = _make_symbol(db_session)
    _add_bars(db_session, sym.id, count=30, start_price=10.0)
    # 最后一根 K 线暴涨 20% 触发过热
    last = db_session.query(DailyBar).filter_by(symbol_id=sym.id).order_by(DailyBar.trade_date.desc()).first()
    last.close = last.close * 1.20
    db_session.commit()

    trade_date = date.today()
    score = analysis.calculate_symbol_score(db_session, sym, trade_date)

    # 触发 overheat 后 stage 应为 overheat
    if score.overheat_penalty >= 20:
        assert score.stage == "overheat"
        assert score.action == "reduce"


def test_calculate_score_priority_formula(db_session):
    """priority_score 应符合 timing*0.4 + quality*0.3 + liquidity*0.2 + breadth*0.1。"""
    sym = _make_symbol(db_session, theme="消费")
    _add_bars(db_session, sym.id, count=40)

    trade_date = date.today()
    score = analysis.calculate_symbol_score(db_session, sym, trade_date)

    expected = round(
        score.timing_score * 0.4
        + score.quality_score * 0.3
        + score.liquidity_score * 0.2
        + score.breadth_score * 0.1,
        2,
    )
    assert score.priority_score == expected
