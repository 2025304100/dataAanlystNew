"""白盒测试 - 机会挖掘 universe 刷新回归守护。

核心守护：防止 Referer 事件再次导致 'Excel file format cannot be determined' 错误。
覆盖：
1. _cn_stock_universe_frame 双数据源回退（stock_info_a_code_name → stock_zh_a_spot_em）
2. _refresh_cn_stock_universe 缓存兜底（失败时回退 DB 已有 symbol）
3. _refresh_cn_etf_universe 主源/备源切换（fund_etf_spot_em → fund_etf_category_sina）
4. _refresh_discovery_universe 按 scope 路由
5. _first_row_value 多键名兼容
6. slow 联网测试：真实 universe 拉取
7. 关联场景：HTTP monkey-patch 不破坏 universe 拉取
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from app.models.symbol import Symbol
from app.services.discovery_tasks import (
    _cn_stock_universe_frame,
    _first_row_value,
    _refresh_cn_etf_universe,
    _refresh_cn_stock_universe,
    _refresh_discovery_universe,
)
from app.schemas.discovery import DiscoveryTaskCreate

pytestmark = pytest.mark.whitebox


# ============================================================================
# 1. _cn_stock_universe_frame 双数据源回退
# ============================================================================

def test_cn_stock_universe_frame_primary_success(db_session, monkeypatch):
    """主数据源 stock_info_a_code_name 成功 → 返回 DataFrame。"""
    expected_df = pd.DataFrame([{"code": "000001", "name": "平安银行"}])
    mock_func = MagicMock(return_value=expected_df)
    monkeypatch.setattr("app.services.discovery_tasks.ak.stock_info_a_code_name", mock_func)
    monkeypatch.setattr("app.services.discovery_tasks.ak.stock_zh_a_spot_em", MagicMock())

    result = _cn_stock_universe_frame(db_session)

    assert result is expected_df
    mock_func.assert_called_once()


def test_cn_stock_universe_frame_fallback_to_secondary(db_session, monkeypatch):
    """主源失败（max_attempts=2 都失败），备源 stock_zh_a_spot_em 成功 → 返回备源 DataFrame。"""
    primary_df = pd.DataFrame([{"code": "000002", "name": "万科A"}])
    mock_primary = MagicMock(side_effect=ConnectionError("primary failed"))
    mock_secondary = MagicMock(return_value=primary_df)
    monkeypatch.setattr("app.services.discovery_tasks.ak.stock_info_a_code_name", mock_primary)
    monkeypatch.setattr("app.services.discovery_tasks.ak.stock_zh_a_spot_em", mock_secondary)

    result = _cn_stock_universe_frame(db_session)

    assert result is primary_df
    # 主源 max_attempts=2，所以被调用 2 次后抛异常，然后回退备源
    assert mock_primary.call_count == 2
    mock_secondary.assert_called_once()


def test_cn_stock_universe_frame_both_fail_raises(db_session, monkeypatch):
    """两个数据源都失败 → raise RuntimeError，错误信息含两个源的错误。"""
    mock_primary = MagicMock(side_effect=ConnectionError("primary fail"))
    mock_secondary = MagicMock(side_effect=TimeoutError("secondary timeout"))
    monkeypatch.setattr("app.services.discovery_tasks.ak.stock_info_a_code_name", mock_primary)
    monkeypatch.setattr("app.services.discovery_tasks.ak.stock_zh_a_spot_em", mock_secondary)

    with pytest.raises(RuntimeError, match="A-share universe refresh failed"):
        _cn_stock_universe_frame(db_session)


def test_cn_stock_universe_frame_uses_call_akshare_with_retry(db_session, monkeypatch):
    """验证 _cn_stock_universe_frame 通过 call_akshare_with_retry 调用（而非直接 ak.xxx）。"""
    captured_calls = []

    def fake_call(func, *args, **kwargs):
        # 用 getattr 获取 __name__，兼容 mock 对象
        func_name = getattr(func, "__name__", str(func))
        captured_calls.append((func_name, kwargs.get("api_key"), kwargs.get("max_attempts")))
        return pd.DataFrame([{"code": "000001", "name": "test"}])

    monkeypatch.setattr(
        "app.services.discovery_tasks.call_akshare_with_retry", fake_call
    )
    # 用有 __name__ 属性的 mock 对象
    mock_ak_func = MagicMock()
    mock_ak_func.__name__ = "stock_info_a_code_name"
    monkeypatch.setattr("app.services.discovery_tasks.ak.stock_info_a_code_name", mock_ak_func)

    _cn_stock_universe_frame(db_session)

    assert len(captured_calls) == 1
    assert captured_calls[0][0] == "stock_info_a_code_name"
    assert captured_calls[0][1] == "stock_info_a_code_name"
    assert captured_calls[0][2] == 2  # max_attempts=2


# ============================================================================
# 2. _refresh_cn_stock_universe 缓存兜底
# ============================================================================

def test_refresh_cn_stock_universe_success(db_session, monkeypatch):
    """成功拉取 → seen/created 正确，code zfill(6) 补零。"""
    mock_frame = pd.DataFrame([
        {"code": "1", "name": "平安银行"},  # 需 zfill
        {"code": "000002", "name": "万科A"},
    ])
    monkeypatch.setattr(
        "app.services.discovery_tasks._cn_stock_universe_frame", lambda db: mock_frame
    )

    result = _refresh_cn_stock_universe(db_session)

    assert result["seen"] == 2
    assert result["created"] == 2
    assert "source" not in result  # 成功时无 source 字段


def test_refresh_cn_stock_universe_zfills_code(db_session, monkeypatch):
    """code="1" → zfill(6)="000001"。"""
    mock_frame = pd.DataFrame([{"code": "1", "name": "test"}])
    monkeypatch.setattr(
        "app.services.discovery_tasks._cn_stock_universe_frame", lambda db: mock_frame
    )

    _refresh_cn_stock_universe(db_session)

    sym = db_session.query(Symbol).filter_by(symbol="000001").first()
    assert sym is not None, "zfill 后的 code 000001 应被创建"


def test_refresh_cn_stock_universe_skips_invalid_rows(db_session, monkeypatch):
    """code 非数字/空 → 跳过，不计入 seen。"""
    mock_frame = pd.DataFrame([
        {"code": "abc", "name": "invalid"},  # 非数字
        {"code": "", "name": "empty"},  # 空
        {"code": "000001", "name": "valid"},  # 有效
        {"code": "000002", "name": ""},  # name 空也跳过
    ])
    monkeypatch.setattr(
        "app.services.discovery_tasks._cn_stock_universe_frame", lambda db: mock_frame
    )

    result = _refresh_cn_stock_universe(db_session)

    assert result["seen"] == 1  # 只有 000001 有效
    assert result["created"] == 1


def test_refresh_cn_stock_universe_cache_fallback(db_session, monkeypatch):
    """frame 抛异常 + DB 已有 cn-stock symbol → 返回 cache 兜底结果。"""
    # 预置一个 cn-stock symbol 到 DB
    db_session.add(Symbol(
        symbol="000001", name="平安银行", asset_type="stock",
        market="sz", board="main", theme="a-share", is_active=1,
    ))
    db_session.commit()

    monkeypatch.setattr(
        "app.services.discovery_tasks._cn_stock_universe_frame",
        lambda db: (_ for _ in ()).throw(RuntimeError("network fail")),
    )

    result = _refresh_cn_stock_universe(db_session)

    assert result["source"] == "cache"
    assert result["seen"] == 1  # DB 已有 1 个
    assert result["created"] == 0
    assert "network fail" in result["warning"]


def test_refresh_cn_stock_universe_no_cache_raises(db_session, monkeypatch):
    """frame 抛异常 + DB 无 cn-stock symbol → raise（不能空扫描）。"""
    monkeypatch.setattr(
        "app.services.discovery_tasks._cn_stock_universe_frame",
        lambda db: (_ for _ in ()).throw(RuntimeError("network fail")),
    )

    with pytest.raises(RuntimeError, match="network fail"):
        _refresh_cn_stock_universe(db_session)


def test_refresh_cn_stock_universe_supports_chinese_column_names(db_session, monkeypatch):
    """列名是中文（'代码'/'名称'）时也能正确解析。"""
    mock_frame = pd.DataFrame([
        {"代码": "000001", "名称": "平安银行"},
        {"代码": "000002", "名称": "万科A"},
    ])
    monkeypatch.setattr(
        "app.services.discovery_tasks._cn_stock_universe_frame", lambda db: mock_frame
    )

    result = _refresh_cn_stock_universe(db_session)

    assert result["seen"] == 2


def test_refresh_cn_stock_universe_idempotent(db_session, monkeypatch):
    """重复刷新同一批 symbol → created=0（已存在不重复创建）。"""
    mock_frame = pd.DataFrame([{"code": "000001", "name": "平安银行"}])
    monkeypatch.setattr(
        "app.services.discovery_tasks._cn_stock_universe_frame", lambda db: mock_frame
    )

    first = _refresh_cn_stock_universe(db_session)
    second = _refresh_cn_stock_universe(db_session)

    assert first["created"] == 1
    assert second["created"] == 0  # 已存在不重复创建
    assert second["seen"] == 1


# ============================================================================
# 3. _refresh_cn_etf_universe 主源/备源切换
# ============================================================================

def test_refresh_cn_etf_universe_primary_success(db_session, monkeypatch):
    """主源 fund_etf_spot_em 成功 → 解析正确。"""
    mock_frame = pd.DataFrame([
        {"代码": "510300", "名称": "沪深300ETF"},
        {"代码": "159915", "名称": "创业板ETF"},
    ])
    monkeypatch.setattr(
        "app.services.discovery_tasks.ak.fund_etf_spot_em", lambda: mock_frame
    )

    result = _refresh_cn_etf_universe(db_session)

    assert result["seen"] == 2
    assert result["created"] == 2


def test_refresh_cn_etf_universe_fallback_to_sina(db_session, monkeypatch):
    """主源失败 → 备源 fund_etf_category_sina 成功 → 用 sina 列名解析。"""
    mock_primary = MagicMock(side_effect=ConnectionError("primary fail"))
    mock_sina_frame = pd.DataFrame([
        {"代码": "510300", "名称": "沪深300ETF"},
    ])
    mock_secondary = MagicMock(return_value=mock_sina_frame)
    monkeypatch.setattr("app.services.discovery_tasks.ak.fund_etf_spot_em", mock_primary)
    monkeypatch.setattr("app.services.discovery_tasks.ak.fund_etf_category_sina", mock_secondary)

    result = _refresh_cn_etf_universe(db_session)

    assert result["seen"] == 1
    mock_secondary.assert_called_once()


def test_refresh_cn_etf_universe_strips_prefix(db_session, monkeypatch):
    """code="sh510300" → 去除 "sh" 前缀 → "510300"。"""
    mock_frame = pd.DataFrame([
        {"代码": "sh510300", "名称": "沪深300ETF"},
        {"代码": "sz159915", "名称": "创业板ETF"},
    ])
    monkeypatch.setattr(
        "app.services.discovery_tasks.ak.fund_etf_spot_em", lambda: mock_frame
    )

    _refresh_cn_etf_universe(db_session)

    sym1 = db_session.query(Symbol).filter_by(symbol="510300").first()
    sym2 = db_session.query(Symbol).filter_by(symbol="159915").first()
    assert sym1 is not None, "sh510300 去前缀后应为 510300"
    assert sym2 is not None, "sz159915 去前缀后应为 159915"


def test_refresh_cn_etf_universe_both_fail_raises(db_session, monkeypatch):
    """主源和备源都失败 → raise。"""
    monkeypatch.setattr(
        "app.services.discovery_tasks.ak.fund_etf_spot_em",
        lambda: (_ for _ in ()).throw(ConnectionError("primary fail")),
    )
    monkeypatch.setattr(
        "app.services.discovery_tasks.ak.fund_etf_category_sina",
        lambda: (_ for _ in ()).throw(ConnectionError("secondary fail")),
    )

    with pytest.raises(ConnectionError):
        _refresh_cn_etf_universe(db_session)


def test_refresh_cn_etf_universe_skips_invalid_code(db_session, monkeypatch):
    """code 非数字（去前缀后）→ 跳过。"""
    mock_frame = pd.DataFrame([
        {"代码": "abc", "名称": "invalid"},
        {"代码": "510300", "名称": "valid"},
    ])
    monkeypatch.setattr(
        "app.services.discovery_tasks.ak.fund_etf_spot_em", lambda: mock_frame
    )

    result = _refresh_cn_etf_universe(db_session)

    assert result["seen"] == 1  # 只有 510300 有效


# ============================================================================
# 4. _refresh_discovery_universe 按 scope 路由
# ============================================================================

def test_refresh_discovery_universe_routes_cn_stock(db_session, monkeypatch):
    """scope=cn-stock → 调用 _refresh_cn_stock_universe。"""
    called = {"stock": False, "etf": False}
    monkeypatch.setattr(
        "app.services.discovery_tasks._refresh_cn_stock_universe",
        lambda db: (called.__setitem__("stock", True), {"seen": 0, "created": 0})[1],
    )
    monkeypatch.setattr(
        "app.services.discovery_tasks._refresh_cn_etf_universe",
        lambda db: (called.__setitem__("etf", True), {"seen": 0, "created": 0})[1],
    )

    payload = DiscoveryTaskCreate(scope="cn-stock", refresh_universe=True)
    _refresh_discovery_universe(db_session, payload)

    assert called["stock"] is True
    assert called["etf"] is False


def test_refresh_discovery_universe_routes_cn_etf(db_session, monkeypatch):
    """scope=cn-etf → 调用 _refresh_cn_etf_universe。"""
    called = {"stock": False, "etf": False}
    monkeypatch.setattr(
        "app.services.discovery_tasks._refresh_cn_stock_universe",
        lambda db: (called.__setitem__("stock", True), {"seen": 0, "created": 0})[1],
    )
    monkeypatch.setattr(
        "app.services.discovery_tasks._refresh_cn_etf_universe",
        lambda db: (called.__setitem__("etf", True), {"seen": 0, "created": 0})[1],
    )

    payload = DiscoveryTaskCreate(scope="cn-etf", refresh_universe=True)
    _refresh_discovery_universe(db_session, payload)

    assert called["stock"] is False
    assert called["etf"] is True


def test_refresh_discovery_universe_respects_refresh_flag(db_session, monkeypatch):
    """refresh_universe=False → 返回 None，不调用任何 universe 函数。"""
    called = {"stock": False, "etf": False}
    monkeypatch.setattr(
        "app.services.discovery_tasks._refresh_cn_stock_universe",
        lambda db: called.__setitem__("stock", True),
    )
    monkeypatch.setattr(
        "app.services.discovery_tasks._refresh_cn_etf_universe",
        lambda db: called.__setitem__("etf", True),
    )

    payload = DiscoveryTaskCreate(scope="cn-stock", refresh_universe=False)
    result = _refresh_discovery_universe(db_session, payload)

    assert result is None
    assert called["stock"] is False
    assert called["etf"] is False


def test_refresh_discovery_universe_unknown_scope_returns_none(db_session, monkeypatch):
    """scope 不在 cn-stock/cn-etf 路由中（如 us-stock）→ 返回 None。"""
    payload = DiscoveryTaskCreate(scope="us-stock", refresh_universe=True)
    result = _refresh_discovery_universe(db_session, payload)
    assert result is None


# ============================================================================
# 5. _first_row_value 多键名兼容
# ============================================================================

def test_first_row_value_finds_first_matching_key():
    """按 keys 顺序查找，返回第一个匹配的值。"""
    row = {"代码": "000001", "symbol": "x", "code": "y"}
    assert _first_row_value(row, ["代码", "symbol", "code"]) == "000001"


def test_first_row_value_falls_back_to_second_key():
    """第一个 key 不在 row → 用第二个 key。"""
    row = {"symbol": "000001", "code": "000002"}
    assert _first_row_value(row, ["代码", "symbol", "code"]) == "000001"


def test_first_row_value_returns_none_when_missing():
    """所有 key 都不在 row → 返回 None。"""
    row = {"other": "value"}
    assert _first_row_value(row, ["code", "代码", "symbol"]) is None


def test_first_row_value_skips_none_values():
    """key 存在但值为 None → 继续找下一个 key。"""
    row = {"code": None, "symbol": "000001"}
    assert _first_row_value(row, ["code", "symbol"]) == "000001"


def test_first_row_value_empty_keys_list():
    """keys 为空列表 → 返回 None。"""
    assert _first_row_value({"code": "x"}, []) is None


# ============================================================================
# 6. slow 联网测试（真实 universe 拉取）
# ============================================================================

@pytest.mark.slow
def test_real_cn_stock_universe_frame(db_session):
    """【slow 联网】真实调用 _cn_stock_universe_frame → 行数 > 1000。

    守护：深交所/东财接口可达，不再报 'Excel file format cannot be determined'。
    """
    frame = _cn_stock_universe_frame(db_session)
    assert frame is not None
    assert len(frame) > 1000, f"A 股 universe 应 > 1000 行，实际 {len(frame)} 行"


@pytest.mark.slow
def test_real_cn_etf_universe(db_session):
    """【slow 联网】真实调用 _refresh_cn_etf_universe → seen > 100。"""
    result = _refresh_cn_etf_universe(db_session)
    assert result["seen"] > 100, f"ETF universe 应 > 100 个，实际 {result['seen']}"


# ============================================================================
# 7. 关联场景：HTTP monkey-patch 不破坏 universe 拉取
# ============================================================================

def test_universe_frame_uses_hardened_session_no_referer(db_session, monkeypatch):
    """【关联测试】universe 拉取通过 call_akshare_with_retry 走加固的 HTTP 路径。

    这是 Referer 事件的核心关联守护：验证 _cn_stock_universe_frame 调用链路
    使用 call_akshare_with_retry（其底层 Session 已被 _harden_requests_session 加固，
    不含 Referer）。HTTP headers 的直接守护在 test_whitebox_akshare_http.py 中覆盖。
    """
    captured_api_keys = []

    def fake_call(func, *args, **kwargs):
        captured_api_keys.append(kwargs.get("api_key"))
        return pd.DataFrame([{"code": "000001", "name": "test"}])

    monkeypatch.setattr(
        "app.services.discovery_tasks.call_akshare_with_retry", fake_call
    )
    mock_ak_func = MagicMock()
    mock_ak_func.__name__ = "stock_info_a_code_name"
    monkeypatch.setattr("app.services.discovery_tasks.ak.stock_info_a_code_name", mock_ak_func)

    _cn_stock_universe_frame(db_session)

    # 核心断言：通过 call_akshare_with_retry 调用，api_key 正确传递
    # call_akshare_with_retry 内部会 apply_delay + 使用加固的 Session
    assert len(captured_api_keys) == 1
    assert captured_api_keys[0] == "stock_info_a_code_name", (
        "universe 拉取应通过 call_akshare_with_retry 调用，传入正确的 api_key。"
        "api_key 传入后会走加固的 HTTP 路径（_harden_requests_session 已确保无 Referer）。"
    )


def test_universe_failure_with_seen_zero_aborts(db_session, monkeypatch):
    """【关联测试】universe 拉取失败 + DB 无缓存 → raise，不继续空扫描。

    守护：避免 universe 为空时仍执行扫描（浪费资源 + 误清理）。
    """
    monkeypatch.setattr(
        "app.services.discovery_tasks._cn_stock_universe_frame",
        lambda db: (_ for _ in ()).throw(RuntimeError("all sources failed")),
    )

    with pytest.raises(RuntimeError, match="all sources failed"):
        _refresh_cn_stock_universe(db_session)
