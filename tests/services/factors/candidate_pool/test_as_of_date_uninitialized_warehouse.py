"""白盒：数仓未初始化（表不存在）时，基准日期解析必须降级而不是 500。

起因（拟真/CI 实测）：`tests/services/factors/candidate_pool/test_e2e.py::TestHttpLayer`
两条用例撞 `_duckdb.CatalogException: Table with name raw_valuation_snapshots does not exist`。
根因是建表只发生在 `POST /factors/warehouse/initialize` 与各 job 里，读路径
`wh.connection()` 不惰性建表；而 `analysis.py` 的既有意图写得很清楚：
“解析不出 as_of（数仓空）→ 用今天，让分析自己报告无数据，**而不是在这里崩**”。
实现只覆盖了“表里没数据”，漏了“表根本不存在”。本文件把两件事分开钉住。
"""
from __future__ import annotations

import datetime as dt

import duckdb
import pytest

pytestmark = pytest.mark.whitebox

from app.services.factors.candidate_pool import rules as R  # noqa: E402
from app.services.factors.candidate_pool import analysis as A  # noqa: E402


@pytest.fixture()
def empty_conn():
    con = duckdb.connect(":memory:")
    try:
        yield con
    finally:
        con.close()


def test_missing_table_degrades_instead_of_raising(empty_conn):
    """表不存在 → 返回“解析不出 as_of”，并在证据里留下可解释的原因。"""
    resolved, adjusted, evidence = R._resolve_as_of_date(
        empty_conn, dt.date(2026, 9, 30), baseline_days=30, completeness=0.9
    )

    assert resolved is None, "数仓未初始化时不该给出 as_of"
    assert adjusted is False
    assert evidence.get("warehouse_uninitialized") is True, (
        f"证据里必须写明是“数仓未建表”，否则调用方无从解释：{evidence}"
    )
    # 与既有“无数据”分支保持同样的键形状，避免上层按键取值时 KeyError
    assert {"candidate_ratios", "median_baseline", "completeness_threshold"} <= set(evidence)


def test_existing_empty_table_branch_unchanged(empty_conn):
    """表存在但没有任何行：仍走原来的优雅分支（不能被新代码改变语义）。"""
    empty_conn.execute(
        "CREATE TABLE raw_valuation_snapshots (trade_date DATE, symbol VARCHAR)"
    )

    resolved, adjusted, evidence = R._resolve_as_of_date(
        empty_conn, dt.date(2026, 9, 30), baseline_days=30, completeness=0.9
    )

    assert resolved is None
    assert adjusted is False
    assert "warehouse_uninitialized" not in evidence, "空表不是“未初始化”，别混为一谈"


def test_other_database_errors_are_not_swallowed(empty_conn):
    """只放行“表不存在”；别的数据库错误必须继续抛（否则真故障会被抹成无数据）。"""
    empty_conn.execute("CREATE TABLE raw_valuation_snapshots (trade_date DATE, symbol VARCHAR)")
    # 造一个必然失败的调用：baseline_days 传非数字 → 不是 “does not exist” 类错误
    with pytest.raises(Exception) as exc:
        R._resolve_as_of_date(empty_conn, None, baseline_days="not-a-number", completeness=0.9)
    assert "does not exist" not in str(exc.value)


def test_uninitialized_warehouse_is_reported_as_actionable_hint():
    """fail-soft 不等于沉默：文案必须告诉用户下一步做什么。

    只报“无数据”会让人去查一个并不存在的问题；而“数仓未初始化”是可直接执行的动作。
    """
    msg = A.warehouse_warning_for({"warehouse_uninitialized": True})

    assert msg is not None
    assert "初始化" in msg, f"文案必须点名真正原因：{msg}"
    assert "无数据" not in msg, "不能把“没建表”含糊地说成“没数据”"
    # 前端 PoolAnalysisModal 直接渲染 analysis.warnings，文案得能独立读通
    assert A.WAREHOUSE_UNINITIALIZED_WARNING == msg


def test_no_hint_when_warehouse_is_merely_empty():
    """表存在但无数据 ≠ 未初始化，不该拿这条文案误报。"""
    assert A.warehouse_warning_for({"warehouse_uninitialized": False}) is None
    assert A.warehouse_warning_for({}) is None
    assert A.warehouse_warning_for(None) is None


def test_analyze_pool_writes_the_hint_into_persisted_warnings(monkeypatch):
    """接线验证：提示必须进入会被持久化、且前端会渲染的 analysis.warnings。

    只测 helper 不够：如果 analyze_pool 里那句追加被删了，用户依旧只能看到“无数据”。
    这里把仓库/建池/入库等依赖全部桩掉，只留下“证据 → warnings”这条链路。
    """
    import contextlib
    import datetime as dt

    from app.services.factors.candidate_pool import service as pool_service

    captured: dict[str, object] = {}

    class _StubWarehouse:
        @contextlib.contextmanager
        def connection(self, read_only: bool = False):
            con = duckdb.connect(":memory:")   # 空仓库：raw_valuation_snapshots 不存在
            try:
                yield con
            finally:
                con.close()

    class _Snap:
        id = "snap-1"
        analysis_status = "analyzed"
        is_locked = 0
        data_cutoff_at = dt.datetime(2026, 9, 30)
        members_json = "[]"

    monkeypatch.setattr(pool_service, "get_pool", lambda db, pid: object())
    monkeypatch.setattr(pool_service, "_assert_not_locked", lambda *a, **kw: None)
    monkeypatch.setattr(pool_service, "list_snapshots", lambda db, pid: [])
    monkeypatch.setattr(pool_service, "freeze_snapshot", lambda db, **kw: _Snap())
    monkeypatch.setattr(
        pool_service, "snapshot_to_dict",
        lambda db, snap: {"pool_id": "p1"},
    )

    def _capture_mark_analyzed(db, *, snapshot_id, analysis, operator_id):
        captured["analysis"] = analysis
        return _Snap()

    monkeypatch.setattr(pool_service, "mark_analyzed", _capture_mark_analyzed)
    # 面板加载与指标计算不是本测试要校的东西，桩成空结果
    monkeypatch.setattr(
        A, "load_analysis_panel", lambda *a, **kw: A.AnalysisPanel.empty_for_test()
        if hasattr(A.AnalysisPanel, "empty_for_test") else _StubPanel(),
    )
    monkeypatch.setattr(A, "build_analysis", lambda panel: {"warnings": []})

    A.analyze_pool(None, pool_id="p1", warehouse=_StubWarehouse())

    warnings = captured["analysis"]["warnings"]
    assert any("初始化" in w for w in warnings), (
        f"数仓未初始化时，持久化的 analysis.warnings 里必须有一条可行动提示：{warnings}"
    )


class _StubPanel:
    """占位面板：build_analysis 已被桩掉，这里只需是个对象。"""
    members: list = []
