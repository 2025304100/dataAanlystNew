"""白盒 - latest-candidates 两条数据源分支的契约一致性（PT-DEF-19）。

背景：`GET /discovery/latest-candidates` 有两条分支 —— 优先查 P1 新表
`discovery_candidates`，若最新任务没有候选记录（改造前的历史任务）就回退查 `ScanResult`。
问题是两条分支返回的 dict **键集合不一样**：新表分支缺 `is_frozen`。前端拿这个字段做
排序（冻结置顶）与新鲜度样式，缺键时只能靠 `undefined` 被当成 falsy 蒙过去 ——
同一接口两种形状，早晚出事。

为什么在这里测而不是只靠黑盒：黑盒那条 `test_discovery_latest_candidates_endpoint`
是 `if data:` 才校验字段，**库里没候选就静默通过**，等于没测。这里用 db_session
把两条分支各自造出数据，保证一定真校验到。
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.models.discovery import DiscoveryTaskRecord
from app.models.discovery_candidate import DiscoveryCandidate
from app.models.scan import ScanResult, ScanRun
from app.models.symbol import Symbol
from app.models.universe import UniverseSymbol
from app.services.candidate_promote import get_latest_scan_run_candidates

pytestmark = pytest.mark.whitebox

# 前端真实依赖的键（Discovery.tsx 排序/新鲜度、format.discoveryFreshness、行高亮）
FRONTEND_CRITICAL_KEYS = {
    "symbol_id", "symbol", "name",
    "quality_score", "timing_score", "priority_score",
    "stage", "action",
    "warning_days", "valid_days", "created_at",
    "is_frozen",
}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _make_run(db_session) -> ScanRun:
    run = ScanRun(run_name="QA-parity-scan", scope_snapshot="cn_stock", status="done")
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)
    return run


def _make_task(db_session, scan_run_id: int) -> DiscoveryTaskRecord:
    task = DiscoveryTaskRecord(
        id=f"qa-parity-{scan_run_id}",
        status="done",
        stage="done",
        percent=100.0,
        scope="cn-stock",
        scan_run_id=scan_run_id,
        finished_at=_now(),
        updated_at=_now(),
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)
    return task


def _make_symbol_pair(db_session, code: str) -> tuple[Symbol, UniverseSymbol]:
    sym = Symbol(symbol=code, name=f"测试-{code}", asset_type="stock", market="cn", theme="测试")
    universe = UniverseSymbol(
        symbol=code, name=f"基础-{code}", asset_type="stock", market="sh", region="cn",
    )
    db_session.add_all([sym, universe])
    db_session.commit()
    db_session.refresh(sym)
    db_session.refresh(universe)
    return sym, universe


# ---------------------------------------------------------------------------


def test_fallback_branch_scan_result_returns_frontend_keys(db_session):
    """回退分支（ScanResult）：前端依赖的键必须都在。"""
    run = _make_run(db_session)
    sym, _universe = _make_symbol_pair(db_session, "600900")
    _make_task(db_session, run.id)
    db_session.add(ScanResult(
        scan_run_id=run.id, symbol_id=sym.id,
        result_type="quality", rank_no=1,
        quality_score=80.0, timing_score=70.0, priority_score=75.0,
        stage="hold", action="buy", is_frozen=0,
        warning_days=3, valid_days=5, created_at=_now(),
    ))
    db_session.commit()

    rows = get_latest_scan_run_candidates(db_session, min_score=0, limit=50)
    assert rows, "回退分支没返回候选，测试前提不成立"
    missing = FRONTEND_CRITICAL_KEYS - set(rows[0])
    assert not missing, f"ScanResult 分支缺前端依赖键：{sorted(missing)}"


def test_new_table_branch_returns_the_same_frontend_keys(db_session):
    """新表分支（DiscoveryCandidate）：键集合不得比回退分支少（PT-DEF-19 的核心）。"""
    run = _make_run(db_session)
    _sym, universe = _make_symbol_pair(db_session, "600901")
    _make_task(db_session, run.id)
    db_session.add(DiscoveryCandidate(
        scan_run_id=run.id, universe_symbol_id=universe.id,
        symbol=universe.symbol, name=universe.name, asset_type="stock",
        quality_score=80.0, timing_score=70.0, priority_score=75.0,
        stage="hold", action="buy", is_promoted=0,
        warning_days=3, valid_days=5, created_at=_now(),
    ))
    db_session.commit()

    rows = get_latest_scan_run_candidates(db_session, min_score=0, limit=50)
    assert rows, "新表分支没返回候选，测试前提不成立"
    row = rows[0]
    missing = FRONTEND_CRITICAL_KEYS - set(row)
    assert not missing, f"新表分支缺前端依赖键：{sorted(missing)}"
    # 新表没有冻结概念，值必须是明确的 False，而不是靠缺键被当成 falsy
    assert row["is_frozen"] is False


def test_both_branches_disagree_only_on_candidate_specific_fields(db_session):
    """两条分支允许差在"新表专有字段"（is_promoted 等），但不能差在前端共用的展示字段上。"""
    run = _make_run(db_session)
    sym, universe = _make_symbol_pair(db_session, "600902")
    task = _make_task(db_session, run.id)

    db_session.add(ScanResult(
        scan_run_id=run.id, symbol_id=sym.id,
        result_type="quality", rank_no=1,
        quality_score=80.0, timing_score=70.0, priority_score=75.0,
        stage="hold", action="buy", is_frozen=0,
        warning_days=3, valid_days=5, created_at=_now(),
    ))
    db_session.commit()
    fallback_keys = set(get_latest_scan_run_candidates(db_session, min_score=0, limit=50)[0])

    # 建了候选之后就切到新表分支
    db_session.add(DiscoveryCandidate(
        scan_run_id=run.id, universe_symbol_id=universe.id,
        symbol=universe.symbol, name=universe.name, asset_type="stock",
        quality_score=80.0, timing_score=70.0, priority_score=75.0,
        stage="hold", action="buy", is_promoted=0,
        warning_days=3, valid_days=5, created_at=_now(),
    ))
    db_session.commit()
    new_keys = set(get_latest_scan_run_candidates(db_session, min_score=0, limit=50)[0])

    only_in_fallback = fallback_keys - new_keys
    assert not only_in_fallback, (
        f"新表分支比回退分支少了这些字段，前端在新任务上会静默退化：{sorted(only_in_fallback)}"
    )
    assert task.id  # 前置：两条分支用的是同一个任务


def test_fallback_is_frozen_reports_the_real_value(db_session):
    """回退分支不只得有这个键，值还得是真值（已冻结的就是 True）。"""
    run = _make_run(db_session)
    sym_frozen, _u1 = _make_symbol_pair(db_session, "600903")
    sym_open, _u2 = _make_symbol_pair(db_session, "600904")
    _make_task(db_session, run.id)
    db_session.add_all([
        ScanResult(
            scan_run_id=run.id, symbol_id=sym_frozen.id,
            result_type="quality", rank_no=1,
            quality_score=85.0, timing_score=75.0, priority_score=80.0,
            stage="hold", action="buy", is_frozen=1,
            warning_days=3, valid_days=5, created_at=_now(),
        ),
        ScanResult(
            scan_run_id=run.id, symbol_id=sym_open.id,
            result_type="quality", rank_no=2,
            quality_score=80.0, timing_score=70.0, priority_score=75.0,
            stage="hold", action="buy", is_frozen=0,
            warning_days=3, valid_days=5, created_at=_now(),
        ),
    ])
    db_session.commit()

    rows = get_latest_scan_run_candidates(db_session, min_score=0, limit=50)
    frozen_by_symbol = {r["symbol"]: r["is_frozen"] for r in rows}
    assert frozen_by_symbol == {"600903": True, "600904": False}, (
        f"is_frozen 没把真值带出来：{frozen_by_symbol}"
    )


def test_empty_table_returns_empty_list_not_crash(db_session):
    """没有最新任务时返回空列表（黑盒那条靠 `if data:` 跳过，这里把空态本身钉住）。"""
    assert get_latest_scan_run_candidates(db_session, min_score=0, limit=50) == []
