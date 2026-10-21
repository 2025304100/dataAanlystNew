"""Legacy HTTP guards: invalid formulas and labels cannot manufacture model evidence.

Genuine training, explicit activation and inferred scores are exercised in
test_turnover_v3_public_warehouse_lifecycle.py. These former label-only tests
now verify rejection instead of pretending rule calculations are Ridge output.

URL 前缀：/api/v1/*（对齐 router.prefix）
DB：通过 conftest.db_session 覆盖 app.main.app 的 get_db，避免 lifespan 连真实 MySQL。
"""
from __future__ import annotations

import json
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.db.session import get_db
from app.models.backtest import BacktestRun
from app.models.factor import Factor
from app.models.factor_model import FactorModelRun
from app.models.factor_evaluation import FactorSet
from app.models.portfolio import Portfolio, PortfolioRule
from app.models.portfolio_candidate import PortfolioCandidate
from app.models.score import Score
from app.models.security_status import SecurityStatusDaily
from app.models.symbol import Symbol
from app.services.sim_accounts import ensure_sim_account_seed


pytestmark = pytest.mark.e2e


API_PREFIX = "/api/v1"


@pytest.fixture()
def client(db_session):
    """黑盒 TestClient：把 app.main.get_db 替换为测试级 db_session。"""
    from app.main import app

    def _override():
        yield db_session

    app.dependency_overrides[get_db] = _override
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        app.dependency_overrides.pop(get_db, None)


# ---------------------------------------------------------------------------
# 辅助：DB 造数（E2E 链路只对 HTTP 契约断言，内部 ORM 提前补齐前置）
# ---------------------------------------------------------------------------


def _make_factor(db, code: str, category: str = "value") -> tuple[Factor, int]:
    """ORM 直接造一个 DSL 因子 + 单版本，省掉 POST /factors 的公式参数。"""
    from app.models.factor_model import FactorVersion

    f = Factor(
        code=code,
        name=f"WP07-{code}",
        category=category,
        direction="higher_better",
        status="active",
        source_type="manual",
        is_active=1,
        description="",
    )
    db.add(f); db.flush()
    fv = FactorVersion(
        factor_id=f.id, version=1, formula_expr="", params_json="{}", created_by="wp07",
        validation_status="valid",
    )
    db.add(fv); db.flush()
    # 必须 commit：SQLite 下单连接写锁会阻挡后续 HTTP 用例的内部第二连接，
    # 只 flush 不提交会让紧接的 POST /factor-sets 拖到 30s 后报 503 DB_LOCK_TIMEOUT。
    # （本文件其他 _make_* 都是 commit 口径，这里保持一致）
    db.commit()
    db.refresh(f); db.refresh(fv)
    return f, fv.id, fv.version


def _make_symbol(db, symbol: str, name="WP07标的", asset_type="stock", market="SH") -> Symbol:
    s = Symbol(symbol=symbol, name=name, asset_type=asset_type, market=market)
    db.add(s); db.commit(); db.refresh(s)
    return s


def _make_portfolio(db, name="WP07-E2E-PF", total_capital=200000.0) -> Portfolio:
    p = Portfolio(
        name=name, account_type="simulated", total_capital=total_capital,
        investable_ratio=0.9, cash_reserve_ratio=0.1, currency="CNY",
        is_default=0, auto_trade_enabled=1,
    )
    db.add(p); db.commit(); db.refresh(p)
    ensure_sim_account_seed(db, p); db.commit()
    return p


def _make_bars_for_window(db, symbol_id: int, end_date: date, days: int = 40, base=10.0):
    """造一串连续日 K，让回测有行情可用（避免行情缺失抛错阻断链路）。"""
    from app.models.daily_bar import DailyBar
    for i in range(days):
        d = end_date - timedelta(days=days - i - 1)
        close = base + i * 0.05
        db.add(DailyBar(
            symbol_id=symbol_id, trade_date=d,
            open=close, high=close * 1.02, low=close * 0.98, close=close,
            volume=1_000_000.0,
        ))
    db.commit()


def _seed_quality_daily(db, codes: list[str], *, days: int = 35) -> None:
    """给训练门禁喂数据：近 `days` 天每天的覆盖率 / IC。

    `POST /factor-models/train` 现在先跑 3 道准入 gate（coverage≥0.70 /
    IC∈[0.01,0.10] / lookback≥30 天），数据源是 `factor_quality_daily`。
    旧的 E2E seed 根本不会写这张表，训练会被 TRAIN_GATE_COVERAGE_LOW 直接
    挡在门外——那是 fixture 缺口，不是产品回归。
    日期全部相对“今天”生成，不留挂钟炸弹。
    """
    from datetime import datetime, timedelta, timezone

    from app.models.factor_governance import FactorQualityDaily

    today = datetime.now(timezone.utc).date()
    rows = [
        FactorQualityDaily(
            trade_date=today - timedelta(days=offset),
            factor_code=code,
            coverage=0.95,
            ic_mean=0.05,
            ir_20d=0.6,
            turnover_20d=0.1,
            autocorr_20d=0.9,
            n_stocks=5000,
            detail_json="{}",
        )
        for code in codes
        for offset in range(1, days + 1)
    ]
    db.add_all(rows)
    db.commit()


def _install_calendar_utils(monkeypatch) -> None:
    """注册产品明文支持的第二个日历扩展点 `calendar_utils.get_trading_days`。

    回测预检的交易日历 Adapter（fail-closed）只认两个数据源：
    1. 裸表 `trade_calendar`（列 date / is_trading_day）；
    2. 顶层 `calendar_utils` 模块的 `get_trading_days(start, end)`。

    为什么不再依赖它作为主路径：旧说明写“SQLite 下裸 SQL 取回的 date 列是字符串，
    会被判成未知类型而放弃该数据源（PT-DEF-24）”——那是 **修之前** 的实测结论，现在
    已不成立：tests/test_whitebox_trade_calendar_adapter.py
    ::test_sqlite_string_date_column_is_readable 钉住了“字符串日期可读”。
    本用例因此改为种生产主数据源 trade_calendar 表
    （见 _make_trade_calendar_for_window）：适配器 Priority 1 只要该表存在就采用其
    结果（含空表），不种表就会拿到 trade_days=0，被误报成“区间交易日不足”。

    这里仍注册一个内存 calendar_utils 模块，只作为 Priority 2 的多一重保险
    （产品文档明确支持该扩展点），用 monkeypatch 自动回收，不泄漏给其它用例。
    窗口仍须 ≥ 预检阈值（DEFAULT_BACKTEST_MINIMUM_TRADE_DAYS=240）个交易日。
    """
    import sys
    import types
    from datetime import timedelta

    module = types.ModuleType("calendar_utils")

    def get_trading_days(start, end):
        days = []
        cursor_date = start
        while cursor_date <= end:
            if cursor_date.weekday() < 5:  # Mon~Fri
                days.append(cursor_date)
            cursor_date += timedelta(days=1)
        return days

    module.get_trading_days = get_trading_days
    monkeypatch.setitem(sys.modules, "calendar_utils", module)


def _make_ssd_for_window(db, symbol_id: int, start_date: date, end_date: date) -> int:
    """为窗口内每个工作日补一行「正常上市」的 security_status_daily。

    为什么必须种：回测预检有 SSD fail-closed 门（覆盖交易日比例 < 70% 直接
    报 STATUS_DATA_UNAVAILABLE），产品注释明写“保真回测不允许使用固定 0 基线
    伪装通过”。本用例要证的是 FactorSet→Model→Score→Rule→Backtest 全链路，
    所以按产品口径把状态数据补齐，而不是去关预检门（那等于把被测对象拆掉）。

    工作日集合与 _install_calendar_utils 的日历扩展点同一口径（Mon~Fri），
    否则覆盖率分母与 SSD 行对不上。listing_date 给一个早于窗口的日期，
    避免把标的算成次新排除。
    """
    days = 0
    cursor = start_date
    while cursor <= end_date:
        if cursor.weekday() < 5:
            db.add(
                SecurityStatusDaily(
                    symbol_id=symbol_id,
                    trade_date=cursor,
                    is_st=False,
                    is_suspended=False,
                    is_delisting_period=False,
                    is_listed=True,
                    listing_date=date(2015, 1, 5),
                    delisting_date=None,
                    status_source="raw_universe",
                    as_of_batch_id="wp07-e2e-ssd",
                )
            )
            days += 1
        cursor += timedelta(days=1)
    db.commit()
    return days


def _make_trade_calendar_for_window(db, start_date: date, end_date: date) -> int:
    """种生产主数据源 trade_calendar：窗口内工作日标为交易日，周末标为非交易日。

    为什么必须种表而不是只靠 calendar_utils 扩展点：适配器 Priority 1 只要
    `trade_calendar` 表存在就采用它的结果（空表也算“可用”，这是被
    tests/test_whitebox_trade_calendar_adapter.py::test_empty_table_is_not_reported_unavailable
    显式钉住的语义）。因此空表会让 Priority 2 的扩展点永远走不到，预检拿到
    trade_days=0 后报 INSUFFICIENT_TRADE_DAYS（“缺口 240 天，请扩大区间”），
    并把 SSD 检查拖成“trade_days 为空”——病因被误报成参数问题。

    建表语句与那份白盒测试保持同一形状（date / is_trading_day 两列）。
    工作日口径与 _install_calendar_utils 一致（Mon~Fri）。
    """
    from sqlalchemy import text

    db.execute(text(
        "CREATE TABLE IF NOT EXISTS trade_calendar ("
        "  date DATE NOT NULL,"
        "  is_trading_day INTEGER NOT NULL"
        ")"
    ))
    db.execute(text("DELETE FROM trade_calendar"))
    rows = []
    cursor = start_date - timedelta(days=7)
    while cursor <= end_date + timedelta(days=7):
        rows.append({"d": cursor.isoformat(), "f": 1 if cursor.weekday() < 5 else 0})
        cursor += timedelta(days=1)
    db.execute(
        text("INSERT INTO trade_calendar (date, is_trading_day) VALUES (:d, :f)"),
        rows,
    )
    db.commit()
    return sum(1 for r in rows if r["f"] == 1)


# ---------------------------------------------------------------------------
# WP07_E2E_01：空公式不能进入训练流程
# ---------------------------------------------------------------------------


def test_wp07_e2e_01_untrainable_legacy_factor_cannot_enter_model_flow(client, db_session):
    """An empty legacy formula must not be promoted into purported model evidence.

    Genuine training, activation and inferred scores are covered through the
    public warehouse lifecycle in test_turnover_v3_public_warehouse_lifecycle.
    """
    factor, version_id, version = _make_factor(db_session, "WP07_EMPTY_FORMULA", "value")
    created = client.post(f"{API_PREFIX}/factor-sets", json={
        "name": "Reject untrainable legacy formula", "created_by": "wp07",
    })
    assert created.status_code == 200, created.text
    set_id = created.json()["id"]
    response = client.post(f"{API_PREFIX}/factor-sets/{set_id}/members", json={
        "factor_id": factor.id, "factor_version_id": version_id,
        "factor_code": factor.code, "factor_version": version, "role": "feature",
    })
    assert response.status_code == 200, response.text
    response = client.post(f"{API_PREFIX}/factor-sets/{set_id}/freeze", json={
        "actor": "wp07", "reason": "Check the executable formula before training",
    })
    assert response.status_code == 422, response.text
    assert "member_version_not_trainable" in response.text


# ---------------------------------------------------------------------------
# WP07_E2E_02: 规则评分入口不支持模型计算
# ---------------------------------------------------------------------------


def test_wp07_e2e_02_score_ridge_mode_without_traceability_rejected(client, db_session):
    sym = _make_symbol(db_session, "600008")
    # weight_mode=ridge 但无 factor_set_id / factor_model_run_id
    resp = client.post(
        f"{API_PREFIX}/scores/calculate",
        json={"symbol_ids": [sym.id],
              "trade_date": date(2026, 8, 19).isoformat(),
              "weight_mode": "ridge"},
    )
    assert resp.status_code == 422, (resp.status_code, resp.text)
    body = resp.json()
    assert body["error_code"] == "MODEL_SCORING_PIPELINE_REQUIRED"
    assert "模型评分" in body["user_message"]


# ---------------------------------------------------------------------------
# WP07_E2E_03: 训练路由缺 FactorSet → 404
# ---------------------------------------------------------------------------


def test_wp07_e2e_03_train_missing_factor_set_rejected(client, db_session):
    """引用不存在的因子集去训练：按统一的 7 要素门禁错误返回 400。

    旧断言要求 404。现在 `POST /factor-models/train` 先进 `assert_factor_set_ready`，
    “请求体里引用的因子集不存在”属于业务前置未通过，统一以
    400 + error_code=FACTOR_SET_NOT_FOUND 的结构化信封返回（该 URI 本身存在，
    404 反而不准确）。断言不再写“404 或 NOT_FOUND”这种恒真式。
    """
    resp = client.post(
        f"{API_PREFIX}/factor-models/train",
        json={"factor_set_id": "fs-does-not-exist-wp07-03", "mode": "offline_minimal"},
    )
    assert resp.status_code == 400, (resp.status_code, resp.text)
    body = resp.json()
    assert isinstance(body, dict), body
    assert body.get("error_code") == "FACTOR_SET_NOT_FOUND", body
    for field in ("user_message", "impact", "retryable"):
        assert field in body, f"错误信封缺字段 {field}：{body}"


def test_wp07_e2e_04_traceability_labels_do_not_substitute_for_model_inference(client, db_session):
    """Both lineage IDs still cannot make the rule calculator a model scorer."""
    symbol = _make_symbol(db_session, "600009")
    response = client.post(f"{API_PREFIX}/scores/calculate", json={
        "symbol_ids": [symbol.id], "trade_date": date(2026, 8, 19).isoformat(),
        "weight_mode": "ridge", "factor_model_run_id": "claimed-model",
        "factor_set_id": "claimed-factor-set",
    })
    assert response.status_code == 422, response.text
    assert response.json()["error_code"] == "MODEL_SCORING_PIPELINE_REQUIRED"
    assert client.get(f"{API_PREFIX}/scores/history/{symbol.id}").json() == []
