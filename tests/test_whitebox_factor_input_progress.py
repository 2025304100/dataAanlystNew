"""白盒测试 - 因子输入镜像的进度上报 (VIZ-0930-26)。

回归场景：流水线在 "Mirroring factor inputs (valuations, financials, flows)" 上
把 percent 写死为 27，实测同一个百分比能冻住 40 分钟以上（updated_at 一直在动，
说明是在跑不是僵尸），用户完全无法区分"在跑"和"卡死"。
修法：mirror_factor_inputs 接 progress_callback，按已完成的输入类数上报，
流水线把 factor_inputs 映射到 27%→35% 区间插值。
"""
from __future__ import annotations

import inspect
from unittest.mock import MagicMock, patch

import pytest

from app.services.factors import data_sync, pipeline_task

pytestmark = pytest.mark.whitebox


def test_mirror_factor_inputs_accepts_progress_callback():
    """签名必须暴露 progress_callback，且与 bar_mirror 的 4 参回调同形。"""
    sig = inspect.signature(data_sync.mirror_factor_inputs)
    assert "progress_callback" in sig.parameters, "因子输入镜像没有进度上报入口"
    assert sig.parameters["progress_callback"].default is None


def test_mirror_factor_inputs_reports_every_enabled_section():
    """只启用 3 类输入时，应恰好上报 3 次且 processed 递增到 total。"""
    calls: list[tuple[str, int, int, str]] = []

    def fake_callback(phase: str, processed: int, total: int, message: str) -> None:
        calls.append((phase, processed, total, message))

    with patch.object(data_sync, "FactorWarehouse"), patch.object(
        data_sync, "_mirror_valuations", return_value=None
    ), patch.object(data_sync, "_mirror_fund_flows", return_value=None), patch.object(
        data_sync, "_mirror_macro", return_value=None
    ):
        data_sync.mirror_factor_inputs(
            db=None,  # 所有 _mirror_* 已打桩，不会触库
            warehouse=MagicMock(),  # 会走 target.initialize()
            include_valuations=True,
            include_financial_reports=False,
            include_fund_flows=True,
            include_sentiment=False,
            include_tail_proxy=False,
            include_etf_indicators=False,
            include_macro=True,
            progress_callback=fake_callback,
        )

    assert [c[1] for c in calls] == [1, 2, 3], f"processed 应逐段递增: {calls}"
    assert {c[2] for c in calls} == {3}, f"total 应等于启用段数: {calls}"
    assert {c[0] for c in calls} == {"factor_inputs"}


def test_factor_inputs_phase_has_percent_band():
    """factor_inputs 必须有自己的 percent 区间，否则又会退回写死 27。"""
    band = pipeline_task._MIRROR_PHASE_PERCENT.get("factor_inputs")
    assert band is not None, "factor_inputs 未加入 mirror 子阶段映射"
    start, end = band
    assert 27.0 <= start < end <= 35.0, f"区间应落在 mirror 之后、factors 之前: {band}"


def test_pipeline_wires_callback_into_input_mirror():
    """流水线调用点必须真的把回调传进去（只加参数不传等于没修）。"""
    src = inspect.getsource(pipeline_task)
    marker = "inputs = mirror_factor_inputs("
    assert marker in src, "未找到 mirror_factor_inputs 调用点"
    call_text = src.split(marker, 1)[1].split("\n        )", 1)[0]
    assert "progress_callback=_make_mirror_progress_callback(task_id)" in call_text, (
        f"调用点未传 progress_callback:\n{call_text[:400]}"
    )
