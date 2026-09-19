"""WP5 执行计划诊断修复测试。

验证：
1. ExecutionPlan dataclass 不再被当作 list 遍历（TypeError 修复）
2. 缺失字段的结构化诊断（source_table, field_name, category, correlation_id）
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.services.factors.factor_compiler import (
    COMPILER_VERSION,
    CompilationResult,
    ExecutionPlan,
)
from app.services.factors.factor_executor import FactorExecutor, PreviewOutcome
from app.services.factors.store import FactorWarehouse


# ══════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════


@pytest.fixture
def valid_execution_plan() -> ExecutionPlan:
    """构造含有 data_dependencies 的合法 ExecutionPlan。"""
    return ExecutionPlan(
        compiler_version=COMPILER_VERSION,
        formula="close / sma(close, 5)",
        formula_ast={"type": "Expression", "body": {}},
        params={},
        postprocess=None,
        direction="higher_better",
        data_dependencies={
            "fields": ["close", "volume"],
            "source_tables": ["raw_daily_bars"],
            "max_lookback": 5,
            "pit_fields": [],
            "functions": ["sma"],
        },
        max_lookback=5,
        complexity_score=10.0,
        ast_depth=2,
        node_count=5,
        function_call_count=1,
    )


@pytest.fixture
def compile_result_valid(valid_execution_plan) -> CompilationResult:
    return CompilationResult(
        success=True,
        execution_plan=valid_execution_plan,
        errors=[],
    )


# ══════════════════════════════════════════════════════════
# 测试 1：ExecutionPlan 不再被当作 list 遍历
# ══════════════════════════════════════════════════════════


class TestExecutionPlanNotIterable:
    """验证 no_factor_data 诊断分支不抛 TypeError。"""

    def test_no_factor_data_reads_data_dependencies_not_iterable(
        self, compile_result_valid, valid_execution_plan
    ):
        """
        模拟：
        - compile_result.execution_plan 是合法 ExecutionPlan dataclass
          data_dependencies: {fields:['close','volume'], source_tables:['raw_daily_bars']}
        - executor.preview 所有日期均返回空（factor_values_dict 为空）
        - warehouse.describe_table 返回 ['close','open','symbol','trade_date']
        - warehouse.list_trade_dates 返回 5 个日期（<20 触发交易日不足）

        断言：
        - 诊断分支不抛 TypeError
        - result_json 中 required_fields == ['close','volume']
        - result_json 中 source_tables == ['raw_daily_bars']
        - result_json 中 pit_fields == []
        """
        # 准备 mock
        mock_warehouse = MagicMock(spec=FactorWarehouse)
        mock_warehouse.describe_table.return_value = [
            "symbol", "trade_date", "open", "high", "low", "close",
        ]
        mock_warehouse.list_trade_dates.return_value = [
            date(2026, 8, 13) - timedelta(days=i) for i in range(5)
        ]

        mock_executor = MagicMock(spec=FactorExecutor)
        mock_executor.preview.return_value = PreviewOutcome(
            values=[],
            trade_date=None,
            symbol_count=0,
            coverage=0.0,
            errors=[],
        )

        # 捕获 _set_task 调用
        captured: dict[str, Any] = {}

        def fake_set_task(db, task_id, **kwargs):
            captured.update(kwargs)

        # 模拟 DB 会话
        mock_db = MagicMock()
        mock_factor = MagicMock()
        mock_factor.id = 1
        mock_version = MagicMock()
        mock_version.id = 1
        mock_version.version = 1
        mock_version.formula_expr = "close / sma(close, 5)"
        mock_version.params_json = "{}"
        mock_version.postprocess_json = None

        mock_task = MagicMock()
        mock_task.status = "running"
        mock_task.payload_json = json.dumps({
            "factor_code": "TEST_FACTOR",
            "factor_kind": "continuous",
            "target_horizon": 5,
            "n_groups": 5,
            "cost_rate": 0.001,
            "created_by": "test_user",
        })
        mock_db.get.return_value = mock_task

        from app.services.factors import trade_calendar

        class FakeSessionLocal:
            def __call__(self):
                return mock_db
            def __enter__(self):
                return mock_db
            def __exit__(self, *a):
                pass

        with patch(
            "app.services.factors.wp5_eval_task.get_session_local",
            return_value=FakeSessionLocal(),
        ), patch(
            "app.services.factors.wp5_eval_task.get_factor_by_code",
            return_value=mock_factor,
        ), patch(
            "app.services.factors.wp5_eval_task.get_latest_version",
            return_value=mock_version,
        ), patch(
            "app.services.factors.wp5_eval_task.latest_complete_trade_date",
            return_value=trade_calendar.CompleteTradeDayEvidence(
                selected_trade_date=date(2026, 8, 13),
                observed_symbols=200,
                expected_symbols=200,
                completeness_ratio=1.0,
                fallback_reason=None,
                median_baseline=200,
            ),
        ), patch(
            "app.services.factors.wp5_eval_task.compile_formula",
            return_value=compile_result_valid,
        ), patch(
            "app.services.factors.wp5_eval_task.FactorWarehouse",
            return_value=mock_warehouse,
        ), patch(
            "app.services.factors.wp5_eval_task.FactorExecutor",
            return_value=mock_executor,
        ), patch(
            "app.services.factors.wp5_eval_task._set_task",
            side_effect=fake_set_task,
        ), patch(
            "app.services.factors.wp5_eval_task._cancelled",
            return_value=False,
        ), patch(
            "app.services.factors.wp5_eval_task._start_task_heartbeat",
            return_value=(MagicMock(), MagicMock()),
        ):
            from app.services.factors.wp5_eval_task import _run_evaluation_worker

            task_id = "task-" + "a" * 24
            _run_evaluation_worker(task_id)

        # 断言不抛 TypeError（上面运行无异常即通过一半）
        # 现在验证 result_json 和 errors_json
        assert "result_json" in captured, f"captured keys: {list(captured.keys())}"
        result = json.loads(captured["result_json"])

        # 核心断言：needed_fields 正确从 ExecutionPlan.data_dependencies 读取
        assert set(result["required_fields"]) == {"close", "volume"}, (
            f"required_fields mismatch: {result['required_fields']}"
        )
        # source_tables 也被记录
        assert result["source_tables"] == ["raw_daily_bars"], (
            f"source_tables mismatch: {result['source_tables']}"
        )
        assert result["pit_fields"] == [], (
            f"pit_fields mismatch: {result['pit_fields']}"
        )

        # errors_json 中的 blocker 数量合理（交易日不足 + 字段缺失 + 查看覆盖 等）
        assert "errors_json" in captured
        blockers = json.loads(captured["errors_json"])
        assert isinstance(blockers, list) and len(blockers) >= 2, (
            f"blockers count: {len(blockers)}"
        )


# ══════════════════════════════════════════════════════════
# 测试 2：缺失字段有结构化诊断条目
# ══════════════════════════════════════════════════════════


class TestMissingFieldStructuredDiagnosis:
    """验证 DESCRIBE 返回不含某字段时，结构化错误被记录。"""

    def test_missing_field_has_structured_diagnosis(self):
        """
        模拟：
        - ExecutionPlan 含有 fields=['close', 'non_exist_col']，source_tables=['raw_daily_bars']
        - warehouse.describe_table('raw_daily_bars') 返回 ['symbol','trade_date','close','volume']
          → 'non_exist_col' 缺失
        - executor.preview 全空

        断言：
        - result_json.read_errors 至少有 1 条
        - 缺失字段的条目包含：source_table, field_name, category, correlation_id, message
        """
        plan = ExecutionPlan(
            compiler_version=COMPILER_VERSION,
            formula="close + non_exist_col",
            formula_ast={"type": "Expression", "body": {}},
            params={},
            postprocess=None,
            direction="higher_better",
            data_dependencies={
                "fields": ["close", "non_exist_col"],
                "source_tables": ["raw_daily_bars"],
                "max_lookback": 1,
                "pit_fields": [],
                "functions": [],
            },
            max_lookback=1,
            complexity_score=10.0,
            ast_depth=2,
            node_count=3,
            function_call_count=0,
        )
        compile_result = CompilationResult(
            success=True, execution_plan=plan, errors=[]
        )

        # Monkey patch warehouse: describe_table 不含 non_exist_col
        mock_warehouse = MagicMock(spec=FactorWarehouse)
        # DESCRIBE raw_daily_bars 返回这些列 → non_exist_col 缺失
        mock_warehouse.describe_table.return_value = [
            "symbol", "trade_date", "open", "high", "low", "close", "volume",
        ]
        # 交易日很多 → 不会触发起始化不足
        mock_warehouse.list_trade_dates.return_value = [
            date(2026, 8, 13) - timedelta(days=i) for i in range(100)
        ]

        mock_executor = MagicMock(spec=FactorExecutor)
        mock_executor.preview.return_value = PreviewOutcome(
            values=[],
            trade_date=None,
            symbol_count=0,
            coverage=0.0,
            errors=[],
        )

        captured: dict[str, Any] = {}

        def fake_set_task(db, task_id, **kwargs):
            captured.update(kwargs)

        mock_db = MagicMock()
        mock_factor = MagicMock()
        mock_factor.id = 1
        mock_version = MagicMock()
        mock_version.id = 1
        mock_version.version = 1
        mock_version.formula_expr = "close + non_exist_col"
        mock_version.params_json = "{}"
        mock_version.postprocess_json = None

        mock_task = MagicMock()
        mock_task.status = "running"
        mock_task.payload_json = json.dumps({
            "factor_code": "TEST_FACTOR2",
            "factor_kind": "continuous",
            "target_horizon": 5,
            "n_groups": 5,
            "cost_rate": 0.001,
            "created_by": "test_user",
        })
        mock_db.get.return_value = mock_task

        from app.services.factors import trade_calendar

        class FakeSessionLocal2:
            def __call__(self):
                return mock_db
            def __enter__(self):
                return mock_db
            def __exit__(self, *a):
                pass

        with patch(
            "app.services.factors.wp5_eval_task.get_session_local",
            return_value=FakeSessionLocal2(),
        ), patch(
            "app.services.factors.wp5_eval_task.get_factor_by_code",
            return_value=mock_factor,
        ), patch(
            "app.services.factors.wp5_eval_task.get_latest_version",
            return_value=mock_version,
        ), patch(
            "app.services.factors.wp5_eval_task.latest_complete_trade_date",
            return_value=trade_calendar.CompleteTradeDayEvidence(
                selected_trade_date=date(2026, 8, 13),
                observed_symbols=200,
                expected_symbols=200,
                completeness_ratio=1.0,
                fallback_reason=None,
                median_baseline=200,
            ),
        ), patch(
            "app.services.factors.wp5_eval_task.compile_formula",
            return_value=compile_result,
        ), patch(
            "app.services.factors.wp5_eval_task.FactorWarehouse",
            return_value=mock_warehouse,
        ), patch(
            "app.services.factors.wp5_eval_task.FactorExecutor",
            return_value=mock_executor,
        ), patch(
            "app.services.factors.wp5_eval_task._set_task",
            side_effect=fake_set_task,
        ), patch(
            "app.services.factors.wp5_eval_task._cancelled",
            return_value=False,
        ), patch(
            "app.services.factors.wp5_eval_task._start_task_heartbeat",
            return_value=(MagicMock(), MagicMock()),
        ):
            from app.services.factors.wp5_eval_task import _run_evaluation_worker

            task_id = "task-" + "b" * 24
            _run_evaluation_worker(task_id)

        # 校验 result_json.read_errors 有结构化条目
        assert "result_json" in captured
        result = json.loads(captured["result_json"])
        read_errors = result.get("read_errors", [])
        assert isinstance(read_errors, list), "read_errors should be list"

        # 应该至少有 1 条 schema category 的错误对应 non_exist_col
        schema_errs = [e for e in read_errors if e.get("category") == "schema"]
        assert len(schema_errs) >= 1, (
            f"expected schema errors, got: {read_errors}"
        )

        # 断言缺失字段的结构化条目字段齐全
        missing_err = None
        for err in schema_errs:
            if err.get("field_name") == "non_exist_col":
                missing_err = err
                break
        assert missing_err is not None, (
            f"no schema error for non_exist_col. schema_errs={schema_errs}"
        )

        assert "source_table" in missing_err
        assert "field_name" in missing_err
        assert "category" in missing_err
        assert "correlation_id" in missing_err
        assert "message" in missing_err
        # 进一步断言值的合理性
        assert missing_err["field_name"] == "non_exist_col"
        assert missing_err["category"] == "schema"
        # correlation_id 应该是 8 位 hex
        assert isinstance(missing_err["correlation_id"], str)
        assert len(missing_err["correlation_id"]) == 8, (
            f"correlation_id length should be 8, got {len(missing_err['correlation_id'])}"
        )
        assert missing_err["source_table"] == "raw_daily_bars", (
            f"source_table should be raw_daily_bars, got {missing_err['source_table']}"
        )


# ══════════════════════════════════════════════════════════
# 测试 3：factor_executor._read_source_data 结构化错误
# ══════════════════════════════════════════════════════════


class TestFactorExecutorReadErrors:
    """验证 factor_executor 的 SQL 异常不再静默吞掉。"""

    def test_read_source_data_returns_structured_sql_errors(
        self, valid_execution_plan
    ):
        """
        模拟 warehouse.connection 的 execute 在 SQL 查询时抛异常
        → _read_source_data 返回 (空DataFrame, 结构化错误list)
        """
        mock_warehouse = MagicMock(spec=FactorWarehouse)

        # 构造 mock 连接：DESCRIBE/读取抛异常
        mock_conn = MagicMock()
        mock_conn.execute.side_effect = Exception("Table 'raw_daily_bars' not found")

        mock_ctx = MagicMock()
        mock_ctx.__enter__ = MagicMock(return_value=mock_conn)
        mock_ctx.__exit__ = MagicMock(return_value=False)
        mock_warehouse.connection.return_value = mock_ctx

        executor = FactorExecutor(mock_warehouse)
        df, errors = executor._read_source_data(
            valid_execution_plan,
            trade_date=date(2026, 8, 13),
        )

        # DataFrame 为空
        assert df.empty
        # 至少 1 条结构化错误
        assert len(errors) >= 1
        err = errors[0]
        assert "source_table" in err
        assert "required_fields" in err
        assert "category" in err
        assert "correlation_id" in err
        assert "message" in err
        assert err["category"] in {"sql", "read"}
        assert isinstance(err["correlation_id"], str)
        assert len(err["correlation_id"]) == 8
