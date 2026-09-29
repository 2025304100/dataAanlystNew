"""白盒 - 回测预检阈值单一来源 + 阻断信封完整性（PT-DEF-25 回归）。

覆盖四件事：
1. 阈值 240 只有一个来源（schema 常量），且**近 1 年窗口不再被交易日门禁阻断**
   （旧值 300 ≈ 1.2 自然年，而回测中心自带「近1年」快捷区间，等于必然失败）；
2. `_build_structured_error` 在调用方没给 next_actions 时，从
   `blocking_reasons[].fix_link` 派生（旧行为是恒为空，用户只看到"被阻断"）；
3. 显式传入的 next_actions 不被派生逻辑覆盖；
4. 全局 HTTPException 处理器不再把整个 7 要素 dict 的 repr 塞进
   `technical_details.error_message`（PT-DEF-25b：长度不可控且外泄内部结构）。

日历数据直接写裸表 `trade_calendar`，顺带把 PT-DEF-24 的 SQLite 类型归一放在
真实调用链上验一次（预检能读到交易日 = 归一生效）。
"""
from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from app.api.routes.backtest import _build_structured_error
from app.main import http_exception_handler
from app.schemas.bfg_precheck import (
    DEFAULT_BACKTEST_MINIMUM_TRADE_DAYS,
    BacktestPrecheckRequest,
)
from app.schemas.errors import NextAction
from app.services.bfg_precheck_service import run_backtest_precheck

pytestmark = pytest.mark.whitebox

# 近 1 自然年：工作日约 261 天，介于 240 与 300 之间 —— 正是本次调整的分界样本
LAST_YEAR_START = date(2025, 9, 29)
LAST_YEAR_END = date(2026, 9, 28)

_SAMPLE_BLOCKERS = [
    {
        "code": "INSUFFICIENT_TRADE_DAYS",
        "title_zh": "回测区间交易日不足（阻断）",
        "detail_zh": "当前可用交易日 261 天，低于最低要求 300 天。",
        "evidence": {"actual_usable": 261, "minimum_required": 300, "gap_days": 39},
        "fix_link": {"tab": "portfolio-backtest", "label_zh": "调整回测区间参数"},
    },
]


def _seed_weekday_calendar(db, start: date, end: date) -> int:
    """填 trade_calendar（工作日当交易日），返回填入的天数。"""
    db.execute(text(
        "CREATE TABLE IF NOT EXISTS trade_calendar ("
        "  date DATE NOT NULL,"
        "  is_trading_day INTEGER NOT NULL"
        ")"
    ))
    db.execute(text("DELETE FROM trade_calendar"))
    rows = []
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5:
            rows.append({"d": cursor.isoformat(), "f": 1})
        cursor += timedelta(days=1)
    db.execute(
        text("INSERT INTO trade_calendar (date, is_trading_day) VALUES (:d, :f)"),
        rows,
    )
    db.commit()
    return len(rows)


def _blocker_codes(resp) -> set[str]:
    return {str(b.code) for b in resp.blocking_reasons}


# ---------------------------------------------------------------------------
# 1. 阈值单一来源
# ---------------------------------------------------------------------------


def test_threshold_constant_is_240_and_is_the_schema_default():
    assert DEFAULT_BACKTEST_MINIMUM_TRADE_DAYS == 240, (
        "组合回测预检阈值被改动了；它必须等于 240（≈1 自然年），"
        "否则回测中心的「近1年」区间会重新变成必然被阻断"
    )
    req = BacktestPrecheckRequest(
        portfolio_id=1, symbol_ids=[],
        start_date=LAST_YEAR_START, end_date=LAST_YEAR_END,
    )
    assert req.minimum_trade_days == DEFAULT_BACKTEST_MINIMUM_TRADE_DAYS


def test_last_year_window_is_no_longer_blocked(db_session):
    """近 1 年窗口：默认阈值放行；显式要 300 才该被挡（证明是阈值而非判定逻辑坏了）。"""
    seeded = _seed_weekday_calendar(db_session, LAST_YEAR_START, LAST_YEAR_END)
    assert 240 <= seeded < 300, f"测试前提不成立：样本交易日 {seeded} 不在 [240,300)"

    default_resp = run_backtest_precheck(db_session, BacktestPrecheckRequest(
        portfolio_id=1, symbol_ids=[],
        start_date=LAST_YEAR_START, end_date=LAST_YEAR_END,
    ))
    assert default_resp.usable_trade_days == seeded, (
        f"预检读到的交易日不对：{default_resp.usable_trade_days} vs 填入 {seeded}"
        "（PT-DEF-24 的日历类型归一可能又退了）"
    )
    assert default_resp.minimum_trade_days == DEFAULT_BACKTEST_MINIMUM_TRADE_DAYS
    assert "INSUFFICIENT_TRADE_DAYS" not in _blocker_codes(default_resp), (
        f"近 1 年窗口仍被交易日门禁阻断：{default_resp.blocking_reasons}"
    )

    strict_resp = run_backtest_precheck(db_session, BacktestPrecheckRequest(
        portfolio_id=1, symbol_ids=[],
        start_date=LAST_YEAR_START, end_date=LAST_YEAR_END,
        minimum_trade_days=300,
    ))
    assert "INSUFFICIENT_TRADE_DAYS" in _blocker_codes(strict_resp), (
        "显式要求 300 交易日时应当阻断；没阻断说明门禁失效了"
    )


# ---------------------------------------------------------------------------
# 2/3. next_actions 派生
# ---------------------------------------------------------------------------


def test_next_actions_derived_from_blocker_fix_link():
    payload = _build_structured_error(
        error_code="PRECHECK_BLOCKED",
        title_zh="回测预检未通过（阻断）",
        detail_zh="最低交易日不足，缺口 39 天",
        correlation_id="cid-1",
        blocking_reasons=_SAMPLE_BLOCKERS,
    )
    actions = payload.get("next_actions")
    assert isinstance(actions, list) and len(actions) == 1, (
        f"阻断项自带 fix_link 却没派生出下一步动作：{payload}"
    )
    assert actions[0]["label"] == "调整回测区间参数"
    assert actions[0]["action_type"] == "configure"
    assert actions[0]["target"] == "portfolio-backtest"
    # 必须能被 7 要素协议模型接受（action_type 是 Literal，写错就炸前端）
    NextAction(**actions[0])


def test_explicit_next_actions_are_not_overwritten():
    explicit = [{"label": "重新同步行情", "action_type": "sync"}]
    payload = _build_structured_error(
        error_code="PRECHECK_BLOCKED",
        title_zh="回测预检未通过（阻断）",
        detail_zh="行情缺失",
        correlation_id="cid-2",
        blocking_reasons=_SAMPLE_BLOCKERS,
        next_actions=explicit,
    )
    assert payload["next_actions"] == explicit


def test_no_next_actions_when_blockers_have_no_fix_link():
    payload = _build_structured_error(
        error_code="PRECHECK_BLOCKED",
        title_zh="回测预检未通过（阻断）",
        detail_zh="没有指引可用",
        correlation_id="cid-3",
        blocking_reasons=[{"code": "X", "title_zh": "无 fix_link"}],
    )
    assert "next_actions" not in payload, (
        f"没有 fix_link 时不应编造下一步动作：{payload.get('next_actions')}"
    )


# ---------------------------------------------------------------------------
# 4. 技术细节瘦身（PT-DEF-25b）
# ---------------------------------------------------------------------------


def test_handler_does_not_dump_dict_repr_into_technical_details():
    request = SimpleNamespace(
        url=SimpleNamespace(path="/api/v1/backtest/portfolio/run"), method="POST",
    )
    exc = HTTPException(
        status_code=400,
        detail=_build_structured_error(
            error_code="PRECHECK_BLOCKED",
            title_zh="回测预检未通过（阻断）",
            detail_zh="最低交易日不足，缺口 39 天",
            correlation_id="cid-4",
            blocking_reasons=_SAMPLE_BLOCKERS,
        ),
    )

    response = asyncio.run(http_exception_handler(request, exc))
    body = json.loads(response.body.decode("utf-8"))

    assert body["error_code"] == "PRECHECK_BLOCKED", (
        f"顶层错误码被全局处理器改写：{body.get('error_code')}"
    )
    tech = body["technical_details"]["error_message"]
    assert "PRECHECK_BLOCKED" in tech
    for leaked in ("blocking_reasons", "actual_usable", "{", "'title_zh'"):
        assert leaked not in tech, f"技术细节里仍混入了信封结构 {leaked!r}：{tech}"
    # 25c 端到端：派生的下一步动作要能穿过全局处理器
    assert any(
        a.get("label") == "调整回测区间参数" for a in (body.get("next_actions") or [])
    ), f"next_actions 没进入最终响应：{body.get('next_actions')}"
