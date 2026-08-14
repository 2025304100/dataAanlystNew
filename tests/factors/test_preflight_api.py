"""Part D: POST /factor-evaluation/preflight 接口层测试。

覆盖用例：
- test_preflight_valid_factor：合法因子 → passed=true，items≥6，schema 字段齐全
- test_preflight_missing_field：依赖字段缺失 → dependency check severity=error，evidence 有 missing/available
- test_preflight_horizon_10_unavailable：horizon=10 无目标标签 → target_availability severity=error，overall.passed=false
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from app.models.factor import Factor
from app.models.factor_model import FactorVersion


def _seed_factor(db_session, factor_code="pf_valid_001", formula="close", postprocess=None):
    factor = Factor(
        code=factor_code,
        name=f"Preflight Test Factor {factor_code}",
        category="test",
        direction="higher_better",
        status="active",
        source_type="daily_bars",
        frequency="daily",
        default_missing_policy="exclude",
        lifecycle_status="testing",
        origin="user",
        is_active=1,
        factor_kind="continuous",
    )
    db_session.add(factor)
    db_session.flush()
    version = FactorVersion(
        factor_id=factor.id,
        version=1,
        formula_expr=formula,
        params_json="{}",
        postprocess_json=json.dumps(postprocess) if postprocess else None,
        direction="higher_better",
        is_latest=1,
        execution_plan_hash="abc_preflight_demo",
        formula_ast_json="{}",
        data_dependencies_json="{}",
        created_via="manual",
    )
    db_session.add(version)
    db_session.flush()
    return factor, version


class TestPreflightApi:
    """preflight_factor_evaluation 服务层契约测试。"""

    def test_preflight_valid_factor(self, db_session):
        """合法因子 → passed=true，items≥6，每个 item 字段齐全。"""
        from app.services.factors.wp5_eval_task import preflight_factor_evaluation

        factor, version = _seed_factor(db_session, factor_code="pf_valid")

        # mock warehouse：3 张表、400 个交易日（覆盖 <3 天新鲜窗口）、target_5d 有批次
        fake_trade_dates_desc = [
            date.today() - timedelta(days=i) for i in range(400)
        ]
        with patch(
            "app.services.factors.wp5_eval_task.FactorWarehouse"
        ) as WHMock, patch(
            "app.services.factors.wp5_eval_task.FactorCompiler"
        ) as FCMock, patch(
            "app.services.factors.wp5_eval_task.FactorExecutor"
        ) as ExecutorMock:
            wh = MagicMock()
            WHMock.return_value = wh
            wh.describe_table.return_value = [
                "symbol", "trade_date", "open", "high", "low", "close",
                "volume", "amount",
            ]
            wh.list_trade_dates.return_value = fake_trade_dates_desc
            wh.get_latest_target_batch_id.return_value = "batch_target_5d_0001"

            compiler = MagicMock()
            FCMock.return_value = compiler
            fake_plan = MagicMock()
            fake_plan.data_dependencies = {
                "fields": ["close", "volume", "amount"],
                "source_tables": ["daily_bars"],
                "pit_fields": [],
                "target_fields": [],
                "model_features": [],
            }
            fake_compilation = MagicMock(is_valid=True, execution_plan=fake_plan, errors=[], metadata={})
            compiler.compile.return_value = fake_compilation
            sample_dates = fake_trade_dates_desc[1:6]
            ExecutorMock.return_value.execute_panel.return_value = MagicMock(
                factors_long=pd.DataFrame(
                    {
                        "trade_date": sample_dates,
                        "symbol": ["000001"] * 5,
                        "raw_value": [1.0, 2.0, 3.0, 4.0, 5.0],
                        "winsorized_value": [1.0, 2.0, 3.0, 4.0, 5.0],
                        "normalized_value": [0.0, 0.0, 0.0, 0.0, 0.0],
                    }
                ),
                n_cells=5,
                trade_dates=sample_dates,
                symbols=["000001"],
                read_errors=[],
            )

            result = preflight_factor_evaluation(
                factor_code="pf_valid",
                factor_version_id=None,
                universe="all_a_shares",
                start_date=None,
                end_date=None,
                target_horizon=5,
            )

        # overall 结构
        assert "overall" in result
        assert "items" in result
        overall = result["overall"]
        items = result["items"]
        assert isinstance(overall["passed"], bool)
        assert isinstance(overall["blocking_count"], int)
        assert "recommended_date_range" in overall

        # 至少 6 项
        assert len(items) >= 6, f"expected >=6 preflight items, got {len(items)}"

        # 顺序固定
        order_codes = [it["code"].split(".")[1] for it in items[:6]]
        expected_prefixes = [
            "formula_compile",
            "data_dependencies",
            "market_coverage",
            "target_availability",
            "pit_risk",
            "sample_size",
        ]
        for i, prefix in enumerate(expected_prefixes):
            assert order_codes[i].startswith(prefix), (
                f"item {i} expected {prefix}, got {order_codes[i]}"
            )

        # schema 字段齐全：每个 item 必有 code/severity/category/title_zh/detail_zh/evidence
        required = {"code", "severity", "category", "title_zh", "detail_zh", "evidence"}
        for it in items:
            missing = required - it.keys()
            assert not missing, f"item {it.get('code')} missing fields: {missing}"
            assert it["severity"] in {"pass", "warn", "error", "info"}
            assert it["category"] in {"formula", "data", "config", "sample", "pit", "target"}
            assert isinstance(it["evidence"], dict)
            # 有 fix_link 时结构正确
            if it.get("fix_link") is not None:
                assert isinstance(it["fix_link"], dict)
                assert "label_zh" in it["fix_link"]

        # 合法因子：整体通过
        assert overall["passed"] is True, (
            f"expected passed=true but blocking_count={overall['blocking_count']}. "
            f"error items: {[x['code'] for x in items if x['severity']=='error']}"
        )
        assert overall["blocking_count"] == 0

    def test_preflight_blocks_when_sample_execution_is_all_nan(self, db_session):
        """字段存在但真实公式取样无有效值时，预检必须阻断提交。"""
        from app.services.factors.wp5_eval_task import preflight_factor_evaluation

        _seed_factor(
            db_session,
            factor_code="pf_all_nan",
            formula="sum(main_net_inflow, 20) / max(sum(amount, 20), 1)",
        )
        fake_trade_dates = [date.today() - timedelta(days=i) for i in range(200)]

        with patch(
            "app.services.factors.wp5_eval_task.FactorWarehouse"
        ) as WHMock, patch(
            "app.services.factors.wp5_eval_task.FactorCompiler"
        ) as FCMock, patch(
            "app.services.factors.wp5_eval_task.FactorExecutor"
        ) as ExecutorMock:
            warehouse = MagicMock()
            WHMock.return_value = warehouse
            warehouse.describe_table.return_value = [
                "symbol", "trade_date", "amount", "main_net_inflow",
            ]
            warehouse.list_trade_dates.return_value = fake_trade_dates
            warehouse.get_latest_target_batch_id.return_value = "batch_target_5d_ok"

            plan = MagicMock()
            plan.data_dependencies = {
                "fields": ["amount", "main_net_inflow"],
                "source_tables": ["raw_daily_bars", "raw_fund_flows"],
                "pit_fields": [],
            }
            FCMock.return_value.compile.return_value = MagicMock(
                is_valid=True,
                execution_plan=plan,
                errors=[],
                metadata={},
            )
            ExecutorMock.return_value.execute_panel.return_value = MagicMock(
                factors_long=pd.DataFrame(
                    {
                        "trade_date": [fake_trade_dates[1], fake_trade_dates[1]],
                        "symbol": ["000001", "000002"],
                        "raw_value": [float("nan"), float("nan")],
                        "winsorized_value": [float("nan"), float("nan")],
                        "normalized_value": [float("nan"), float("nan")],
                    }
                ),
                n_cells=2,
                trade_dates=[fake_trade_dates[1]],
                symbols=["000001", "000002"],
                read_errors=[],
            )

            result = preflight_factor_evaluation(
                factor_code="pf_all_nan",
                target_horizon=5,
            )

        value_checks = [
            item for item in result["items"]
            if item["code"] == "preflight.factor_values.all_nan"
        ]
        assert len(value_checks) == 1
        assert value_checks[0]["severity"] == "error"
        assert value_checks[0]["evidence"]["valid_factor_rows"] == 0
        assert result["overall"]["passed"] is False
        assert result["overall"]["blocking_count"] >= 1

    def test_preflight_missing_field(self, db_session):
        """依赖字段缺失 → data_dependencies severity=error，evidence 有 missing 和 available。"""
        from app.services.factors.wp5_eval_task import preflight_factor_evaluation

        factor, version = _seed_factor(
            db_session, factor_code="pf_miss", formula="(close - prev_close) / prev_close"
        )

        fake_trade_dates = [date.today() - timedelta(days=i) for i in range(100)]
        with patch(
            "app.services.factors.wp5_eval_task.FactorWarehouse"
        ) as WHMock, patch(
            "app.services.factors.wp5_eval_task.FactorCompiler"
        ) as FCMock:
            wh = MagicMock()
            WHMock.return_value = wh
            # 故意没有 prev_close（只有 close/open/high/low/volume）
            wh.describe_table.return_value = [
                "symbol", "trade_date", "open", "high", "low", "close", "volume",
            ]
            wh.list_trade_dates.return_value = fake_trade_dates
            wh.get_latest_target_batch_id.return_value = "batch_target_5d_ok"

            # 让 FactorCompiler 返回包含 prev_close 字段依赖的 plan
            compiler = MagicMock()
            FCMock.return_value = compiler
            fake_plan = MagicMock()
            fake_plan.data_dependencies = {
                "fields": ["close", "prev_close"],
                "source_tables": ["daily_bars"],
                "pit_fields": [],
                "target_fields": [],
                "model_features": [],
            }
            fake_compilation = MagicMock(is_valid=True, execution_plan=fake_plan, errors=[], metadata={})
            compiler.compile.return_value = fake_compilation

            result = preflight_factor_evaluation(
                factor_code="pf_miss",
                target_horizon=5,
            )

        items = result["items"]
        dep_items = [
            it for it in items
            if it["category"] == "data" and "data_dependencies" in it["code"]
        ]
        assert dep_items, "data_dependencies 检查项未找到"
        dep = dep_items[0]
        # prev_close 必须出现在 missing 中
        missing_fields = dep["evidence"].get("missing") or []
        assert "prev_close" in missing_fields, (
            f"prev_close should be missing, got missing={missing_fields}"
        )
        # evidence 必须有 available 列表（非空结构）
        assert "available" in dep["evidence"], "evidence 需要包含 available 字段"
        assert isinstance(dep["evidence"]["available"], dict)
        # severity=error → overall.passed=false
        assert dep["severity"] == "error"
        assert result["overall"]["passed"] is False
        assert result["overall"]["blocking_count"] >= 1

    def test_preflight_horizon_10_unavailable(self, db_session):
        """horizon=10 且无批次 → target_availability severity=error，overall.passed=false。"""
        from app.services.factors.wp5_eval_task import preflight_factor_evaluation

        _seed_factor(db_session, factor_code="pf_h10", formula="log(pe_ttm)")
        fake_trade_dates = [date.today() - timedelta(days=i) for i in range(200)]

        with patch(
            "app.services.factors.wp5_eval_task.FactorWarehouse"
        ) as WHMock, patch(
            "app.services.factors.wp5_eval_task.FactorCompiler"
        ) as FCMock:
            wh = MagicMock()
            WHMock.return_value = wh
            wh.describe_table.return_value = [
                "symbol", "trade_date", "open", "high", "low", "close",
                "volume", "pe_ttm",
            ]
            wh.list_trade_dates.return_value = fake_trade_dates
            compiler = MagicMock()
            FCMock.return_value = compiler
            fake_plan = MagicMock()
            fake_plan.data_dependencies = {
                "fields": ["pe_ttm"],
                "source_tables": ["daily_bars"],
                "pit_fields": [],
                "target_fields": [],
                "model_features": [],
            }
            compiler.compile.return_value = MagicMock(
                is_valid=True, execution_plan=fake_plan, errors=[], metadata={}
            )
            # horizon=10 没有批次（target_10d_return 无批次）
            def _fake_latest(tc):
                if tc == "target_10d_return":
                    return None
                return "other_ok"
            wh.get_latest_target_batch_id.side_effect = _fake_latest

            result = preflight_factor_evaluation(
                factor_code="pf_h10",
                target_horizon=10,
            )

        items = result["items"]
        target_items = [it for it in items if it["category"] == "target"]
        assert target_items, "target 检查项未找到"
        target = target_items[0]
        assert target["severity"] == "error", (
            f"horizon=10 无批次应为 error，实际 {target['severity']}: {target['code']}"
        )
        assert "horizon_unavailable" in target["code"] or target["evidence"].get(
            "target_horizon"
        ) == 10, "target error 证据中应体现 horizon=10"
        assert result["overall"]["passed"] is False
        assert result["overall"]["blocking_count"] >= 1
