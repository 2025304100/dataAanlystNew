"""白盒守护 - 自动交易就绪检查行动项注册表（P0）。

钉住三条不变量，防止再出现「指引与 UI 脱节 / 新 code 漏登记」：
1. readiness 模块里每个真实 BLOCKER_*/WARNING_* 常量都必须在 READINESS_FIX_REGISTRY 登记；
2. 每条登记字段合法：title 非空且 ≤12 字、impact 非空、action_type 合法，
   且 redirect/subtab/open-modal 必须带非空 target；
3. _blocker/_warning 工厂按「进入哪个列表」给基础 severity，并补齐 title/fix_link；
   展示降级（如今日无信号→info）只写在注册表 display_severity，不改 readiness 基础级别。
"""
from __future__ import annotations

import pytest

from app.services import auto_trade_readiness as rt

pytestmark = pytest.mark.whitebox


def _registered_codes() -> set[str]:
    """收集模块内全部 BLOCKER_*/WARNING_* 常量的真实取值（去重）。"""
    names = [
        n
        for n in dir(rt)
        if n.startswith("BLOCKER_") or n.startswith("WARNING_")
    ]
    return {getattr(rt, n) for n in names}


def test_every_emitted_code_is_registered():
    registry = rt.READINESS_FIX_REGISTRY
    missing = [code for code in _registered_codes() if code not in registry]
    assert missing == [], f"以下 readiness code 未登记修复动作：{missing}"


def test_registry_entries_are_well_formed():
    for code, meta in rt.READINESS_FIX_REGISTRY.items():
        title = meta.get("title")
        impact = meta.get("impact")
        action_type = meta.get("action_type")

        assert isinstance(title, str) and title.strip(), f"{code} 缺 title"
        assert len(title) <= 12, f"{code} title 超过 12 字：{title}"
        assert isinstance(impact, str) and impact.strip(), f"{code} 缺 impact"
        assert action_type in rt.READINESS_ACTION_TYPES, f"{code} action_type 非法"

        if action_type in {"redirect", "subtab", "open-modal"}:
            assert meta.get("target"), f"{code} 的 {action_type} 必须带 target"

        display = meta.get("display_severity")
        if display is not None:
            assert display in {"blocker", "warning", "info"}, f"{code} display_severity 非法"


def test_blocker_and_warning_factories_set_base_severity():
    blocker = rt._blocker(rt.BLOCKER_MARKET_DATA_STALE, "m")
    warning = rt._warning(rt.WARNING_NO_AUTO_MEMBERS, "m")

    assert blocker.severity == "blocker"
    assert warning.severity == "warning"
    assert blocker.title == "行情或评分过期"
    assert blocker.fix_link == {"action_type": "redirect", "target": "settings:data-center"}
    assert warning.fix_link == {"action_type": "subtab", "target": "members"}


def test_schedule_disabled_severity_follows_list_not_code():
    # 同一 code：调度入口进 blockers 是 blocker，普通入口进 warnings 是 warning。
    assert rt._blocker(rt.BLOCKER_SCHEDULE_DISABLED, "m").severity == "blocker"
    assert rt._warning(rt.WARNING_SCHEDULE_DISABLED, "m").severity == "warning"
    # 两条都指向任务中心。
    assert rt._blocker(rt.BLOCKER_SCHEDULE_DISABLED, "m").fix_link == {
        "action_type": "redirect",
        "target": "settings:tasks",
    }


def test_today_no_signal_keeps_warning_in_readiness_but_display_info():
    issue = rt._warning(rt.WARNING_SIGNAL_EMPTY_TODAY, "m")
    assert issue.severity == "warning"
    assert rt.READINESS_FIX_REGISTRY[rt.WARNING_SIGNAL_EMPTY_TODAY]["display_severity"] == "info"


def test_unknown_code_falls_back_safely():
    issue = rt._blocker("SOME_FUTURE_CODE", "m")
    assert issue.title is None
    assert issue.fix_link == {"action_type": "none"}
    assert issue.severity == "blocker"


def test_readiness_to_dict_carries_new_fields():
    result = rt.AutoTradeReadinessResult(
        ready=False,
        enabled=False,
        account_ready=True,
        data_ready=True,
        source_ready=True,
        schedule_ready=True,
        position_count=0,
        candidate_count=0,
        eligible_buy_count=0,
        auto_member_count=0,
        blockers=[rt._blocker(rt.BLOCKER_PORTFOLIO_DISABLED, "开关未开")],
        warnings=[],
    )
    payload = rt.readiness_to_dict(result)
    blk = payload["blockers"][0]
    assert blk["severity"] == "blocker"
    assert blk["title"] == "自动接管未开启"
    assert blk["fix_link"]["action_type"] == "toggle"


@pytest.mark.parametrize("for_schedule", [False, True])
def test_missing_schedule_has_a_blocker_only_for_scheduled_execution(db_session, for_schedule):
    from app.models.portfolio import Portfolio

    portfolio = Portfolio(
        name="schedule-missing", account_type="simulated",
        total_capital=100000, investable_ratio=1, cash_reserve_ratio=0,
    )
    db_session.add(portfolio)
    db_session.commit()
    result = rt.get_auto_trade_readiness(db_session, portfolio.id, for_schedule=for_schedule)
    issues = result.blockers if for_schedule else result.warnings
    assert any(issue.code == rt.BLOCKER_SCHEDULE_DISABLED for issue in issues)
    assert result.schedule_ready is False
    if for_schedule:
        assert result.ready is False


def test_execution_source_message_describes_blocking_instead_of_legacy_fallback():
    impact = rt.READINESS_FIX_REGISTRY[rt.WARNING_SOURCE_ENV_OVERRIDE]["impact"]
    assert "阻断" in impact
    assert "回退" not in impact


@pytest.mark.parametrize("plan_buys, expected", [([{"symbol_id":1,"action":"BUY"}],1),([],0)])
def test_applied_snapshot_uses_engine_buy_plans_instead_of_legacy_score_action(db_session,monkeypatch,plan_buys,expected):
    from types import SimpleNamespace
    from app.models.portfolio import Portfolio
    from app.models.symbol import Symbol
    from app.services import auto_trade_dual_run as dual
    portfolio=Portfolio(name="ridge-hold-is-engine-buy",account_type="simulated",asset_scope="stock",
        total_capital=100000,investable_ratio=1,cash_reserve_ratio=0,auto_trade_enabled=1)
    symbol=Symbol(id=1,symbol="600001",market="SH",asset_type="stock",name="fixture")
    db_session.add_all([portfolio,symbol]);db_session.commit()
    monkeypatch.setattr(rt,"_validate_rule_fields",lambda rule:(True,None))
    monkeypatch.setattr(rt,"_list_auto_member_ids",lambda *args:{1})
    monkeypatch.setattr(rt,"list_members",lambda *args,**kwargs:[])
    monkeypatch.setattr(rt,"check_data_health",lambda *args,**kwargs:SimpleNamespace(healthy=True))
    monkeypatch.setattr(dual,"resolve_applied_snapshot_id",lambda *args,**kwargs:"applied-real-contract")
    observed=[]
    def capture(*args,**kwargs):
        observed.append(kwargs)
        return dual.TradeSet(buys=plan_buys)
    monkeypatch.setattr(dual,"capture_trade_set_from_new_logic",capture)
    monkeypatch.setattr(rt,"_symbol_eligible_buy",lambda *args,**kwargs:False) # Genuine Ridge action is hold.
    result=rt.get_auto_trade_readiness(db_session,portfolio.id)
    assert result.eligible_buy_count==expected
    assert result.ready is bool(expected)
    assert len(observed)==1
    assert observed[0]["strategy_snapshot_id"]=="applied-real-contract"
