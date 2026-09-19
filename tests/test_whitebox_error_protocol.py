"""白盒测试 - WPD-05 错误协议与乱码修复。

覆盖：
1. AsyncTaskRead 含 error_code 字段且从 errors[0] 提取
2. 旧任务（errors_json 为空）error_code=None，不抛异常
3. 旧任务 errors_json 含历史 message 但无 error_code，error_code=None 但 errors[0].user_message 仍可读
4. _classify_pipeline_error 4 种分支返回正确 error_code
5. build_user_error 对 unknown error_code 降级到 UNKNOWN_ERROR
6. errors_json 写入用 ensure_ascii=False，中文不变成 \\uXXXX 转义
7. 模拟 factor_pipeline 失败，验证 errors_json[0].error_code 与 AsyncTaskRead.error_code 一致
"""
from __future__ import annotations

import json

import pytest

from app.models.async_task import AsyncTaskRecord
from app.schemas.async_task import AsyncTaskRead
from app.schemas.errors import build_user_error
from app.services import async_tasks
from app.services.factors.pipeline_task import _classify_pipeline_error

pytestmark = pytest.mark.whitebox


# ============================================================================
# 1. AsyncTaskRead 含 error_code 字段且从 errors[0] 提取
# ============================================================================


def test_async_task_read_has_error_code_field():
    """AsyncTaskRead schema 应包含 error_code 字段，默认 None。"""
    fields = AsyncTaskRead.model_fields
    assert "error_code" in fields, "AsyncTaskRead 必须包含 error_code 字段"
    # 默认 None（Optional）
    assert fields["error_code"].default is None


def test_task_to_dict_extracts_error_code_from_errors_0():
    """_task_to_dict 应从 errors_json[0].error_code 提取到顶层 error_code。"""
    user_error = build_user_error(
        "DB_LOCK_TIMEOUT",
        override_user_message="因子仓库被占用或繁忙，请稍后重试",
    )
    task = AsyncTaskRecord(
        id="qa-extract-code",
        task_type="factor_pipeline",
        status="failed",
        stage="failed",
        percent=0.0,
        message="因子仓库被占用或繁忙",
        errors_json=json.dumps(
            [user_error.model_dump(mode="json")], ensure_ascii=False, default=str
        ),
    )
    d = async_tasks._task_to_dict(task)
    assert d["error_code"] == "DB_LOCK_TIMEOUT"
    # errors 数组仍保留完整结构
    assert d["errors"][0]["error_code"] == "DB_LOCK_TIMEOUT"


# ============================================================================
# 2. 旧任务（errors_json 为空）error_code=None，不抛异常
# ============================================================================


def test_task_to_dict_empty_errors_yields_none_error_code():
    """errors_json 为空/None 时 error_code=None，不抛异常（向后兼容）。"""
    task = AsyncTaskRecord(
        id="qa-empty-errors",
        task_type="market_data_sync",
        status="done",
        stage="done",
        percent=100.0,
        message="completed",
        errors_json=None,
    )
    d = async_tasks._task_to_dict(task)
    assert d["error_code"] is None
    assert d["errors"] == []


def test_async_task_read_validates_with_none_error_code():
    """AsyncTaskRead.model_validate 应接受 error_code=None。"""
    task = AsyncTaskRecord(
        id="qa-validate-none",
        task_type="factor_pipeline",
        status="queued",
        stage="queued",
        percent=0.0,
        message="Task created",
        total=0,
        processed=0,
        ok_count=0,
        failed_count=0,
        errors_json=None,
    )
    read = AsyncTaskRead.model_validate(async_tasks._task_to_dict(task))
    assert read.error_code is None


# ============================================================================
# 3. 旧任务 errors_json 含历史 message 但无 error_code，error_code=None 但 errors[0] 可读
# ============================================================================


def test_legacy_errors_without_error_code_yields_none_top_level():
    """历史 errors_json 含旧式 {code, error} 但无 error_code 字段时，
    顶层 error_code=None，但 errors[0] 原始内容仍可读（向后兼容）。"""
    legacy_errors = [
        {
            "code": "BACKEND_RESTART_INTERRUPTED",
            "stage": "mirror",
            "error": "Backend restarted before task completed",
        }
    ]
    task = AsyncTaskRecord(
        id="qa-legacy-errors",
        task_type="factor_pipeline",
        status="failed",
        stage="interrupted",
        percent=50.0,
        message="Backend restarted before task completed",
        errors_json=json.dumps(legacy_errors, ensure_ascii=False),
    )
    d = async_tasks._task_to_dict(task)
    # 旧式 error 没有 error_code 字段 → 顶层 None
    assert d["error_code"] is None
    # 但 errors[0] 原始内容仍可读
    assert d["errors"][0]["code"] == "BACKEND_RESTART_INTERRUPTED"
    assert d["errors"][0]["error"] == "Backend restarted before task completed"


# ============================================================================
# 4. _classify_pipeline_error 4 种分支返回正确 error_code
# ============================================================================


def test_classify_pipeline_error_disabled():
    """disabled/enable 关键词 → FACTOR_PIPELINE_DISABLED。"""
    code, msg = _classify_pipeline_error(
        ValueError("Factor pipeline is disabled; enable it in Settings"),
        "initializing",
    )
    assert code == "FACTOR_PIPELINE_DISABLED"
    assert "因子模型未启用" in msg


def test_classify_pipeline_error_validation_days():
    """validation_days/window_days 关键词 → VALIDATION_ERROR（参数类）。"""
    code, msg = _classify_pipeline_error(
        ValueError("validation_days must be less than window_days"),
        "initializing",
    )
    assert code == "VALIDATION_ERROR"
    assert "参数校验" in msg or "训练/验证窗口" in msg


def test_classify_pipeline_error_duckdb_lock():
    """duckdb/lock/concurrent 关键词 → DB_LOCK_TIMEOUT。"""
    code, msg = _classify_pipeline_error(
        RuntimeError("duckdb.CatalogException: database is locked by concurrent process"),
        "mirror",
    )
    assert code == "DB_LOCK_TIMEOUT"
    assert "因子仓库被占用" in msg


def test_classify_pipeline_error_unknown_fallback():
    """其他异常 → UNKNOWN_ERROR 兜底。"""
    code, msg = _classify_pipeline_error(
        ConnectionError("redis connection refused"),
        "score",
    )
    assert code == "UNKNOWN_ERROR"
    assert "因子流水线" in msg


# ============================================================================
# 5. build_user_error 对 unknown error_code 降级到 UNKNOWN_ERROR
# ============================================================================


def test_build_user_error_unknown_code_falls_back_to_unknown():
    """不存在的 error_code 应降级到 UNKNOWN_ERROR 文案，不抛 KeyError。"""
    user_error = build_user_error("NONEXISTENT_CODE_XYZ")
    # error_code 字段保留原始值（供排查），但文案用 UNKNOWN_ERROR
    assert user_error.error_code == "NONEXISTENT_CODE_XYZ"
    # user_message 来自 UNKNOWN_ERROR 模板
    assert user_error.user_message == "服务暂时不可用，请稍后重试"
    assert user_error.retryable is True


def test_build_user_error_known_code_uses_library_message():
    """已存在的 error_code 使用 ERROR_CODE_LIBRARY 中的文案。"""
    user_error = build_user_error("DB_LOCK_TIMEOUT")
    assert user_error.error_code == "DB_LOCK_TIMEOUT"
    assert "数据库繁忙" in user_error.user_message


# ============================================================================
# 6. errors_json 写入用 ensure_ascii=False，中文不变成 \uXXXX 转义
# ============================================================================


def test_append_error_writes_utf8_not_escaped():
    """_append_error 使用 ensure_ascii=False，中文应原样存储，不变成 \\uXXXX。"""
    task = AsyncTaskRecord(
        id="qa-utf8-append",
        task_type="factor_pipeline",
        status="failed",
        stage="failed",
        percent=0.0,
        message="",
        errors_json=None,
    )
    async_tasks._append_error(
        task,
        {
            "error_code": "DB_LOCK_TIMEOUT",
            "user_message": "因子仓库被占用或繁忙，请稍后重试",
        },
    )
    # 中文应原样存在于 errors_json 字符串中，而非 \uXXXX 转义
    assert "因子仓库被占用" in task.errors_json
    assert "\\u" not in task.errors_json, (
        "errors_json 不应包含 \\uXXXX 转义，应使用 ensure_ascii=False"
    )


def test_build_user_error_serialization_preserves_chinese():
    """build_user_error + json.dumps(ensure_ascii=False) 序列化后中文不丢失。"""
    user_error = build_user_error(
        "VALIDATION_ERROR",
        override_user_message="因子流水线启动失败：参数校验未通过",
    )
    serialized = json.dumps(
        [user_error.model_dump(mode="json")], ensure_ascii=False, default=str
    )
    assert "因子流水线启动失败" in serialized
    assert "\\u" not in serialized


# ============================================================================
# 7. 模拟 factor_pipeline 失败，验证 errors_json[0].error_code 与 AsyncTaskRead.error_code 一致
# ============================================================================


def test_pipeline_failure_errors_json_matches_async_task_read_error_code():
    """模拟 factor_pipeline 失败：errors_json[0].error_code 应与 _task_to_dict 提取的顶层 error_code 一致。"""
    # 模拟 _run_factor_pipeline 失败分支的写入逻辑
    error_code, override_message = _classify_pipeline_error(
        ValueError("Factor pipeline was disabled before execution"),
        "initializing",
    )
    assert error_code == "FACTOR_PIPELINE_DISABLED"

    user_error = build_user_error(
        error_code,
        override_user_message=override_message,
    )
    errors_json = json.dumps(
        [user_error.model_dump(mode="json")], ensure_ascii=False, default=str
    )

    task = AsyncTaskRecord(
        id="qa-pipeline-fail",
        task_type="factor_pipeline",
        status="failed",
        stage="failed",
        percent=0.0,
        message=user_error.user_message,
        total=0,
        processed=0,
        ok_count=0,
        failed_count=0,
        errors_json=errors_json,
    )

    # 通过 _task_to_dict 提取（与 _task_to_read 一致）
    d = async_tasks._task_to_dict(task)
    # errors_json[0].error_code 与顶层 error_code 必须一致
    assert d["errors"][0]["error_code"] == "FACTOR_PIPELINE_DISABLED"
    assert d["error_code"] == "FACTOR_PIPELINE_DISABLED"
    assert d["error_code"] == d["errors"][0]["error_code"]

    # AsyncTaskRead schema 验证
    read = AsyncTaskRead.model_validate(d)
    assert read.error_code == "FACTOR_PIPELINE_DISABLED"
    assert read.errors[0]["error_code"] == "FACTOR_PIPELINE_DISABLED"


def test_pipeline_failure_duckdb_lock_error_code_consistency():
    """模拟 DuckDB 锁失败：error_code=DB_LOCK_TIMEOUT 一致性。"""
    error_code, override_message = _classify_pipeline_error(
        RuntimeError("duckdb.IOException: Could not set lock on file"),
        "mirror",
    )
    assert error_code == "DB_LOCK_TIMEOUT"

    user_error = build_user_error(
        error_code, override_user_message=override_message
    )
    errors_json = json.dumps(
        [user_error.model_dump(mode="json")], ensure_ascii=False, default=str
    )
    task = AsyncTaskRecord(
        id="qa-pipeline-lock",
        task_type="factor_pipeline",
        status="failed",
        stage="failed",
        percent=25.0,
        message=user_error.user_message,
        errors_json=errors_json,
    )
    d = async_tasks._task_to_dict(task)
    assert d["error_code"] == "DB_LOCK_TIMEOUT"
    assert d["errors"][0]["error_code"] == "DB_LOCK_TIMEOUT"
    # 中文 message 原样可读
    assert "因子仓库被占用" in d["message"]
