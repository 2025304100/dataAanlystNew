"""WP2: 因子公式编译、校验与预览白盒测试。

覆盖：
- WP2-01: AST 编译核心（白名单、节点数、深度、依赖收集、错误码）
- WP2-02: 原始表达式 DSL（字段和函数能力目录、参数范围、类型检查）
- WP2-03: 后处理配置校验（winsorize、rank/zscore、neutralize、missing policy）
- WP2-04: 稳定执行计划和哈希（canonical JSON、content_hash、compiler_version）
- WP2-05: 版本创建集成（编译器自动填充执行计划字段）

对齐 docs/专业因子库开发计划.md §WP2 和 checklist.md WP2 退出条件。
"""
from __future__ import annotations

import json

import pytest

from app.services.factors.factor_compiler import (
    COMPILER_VERSION,
    CompileError,
    CompilationResult,
    ExecutionPlan,
    FactorCompiler,
    FIELD_CATALOG,
    FUNCTION_CATALOG,
    MAX_AST_DEPTH,
    MAX_FUNCTION_CALLS,
    MAX_LOOKBACK_WINDOW,
    compile_formula,
    validate_postprocess,
    validate_formula,
)

pytestmark = pytest.mark.whitebox


# ══════════════════════════════════════════════════════════
# WP2-01: AST 编译核心 — 白名单校验
# ══════════════════════════════════════════════════════════


class TestASTWhitelist:
    """AST 节点白名单校验。"""

    def test_arithmetic_expression_valid(self):
        """基础算术表达式合法。"""
        result = compile_formula(formula="1 / pe_ttm", strict_fields=False)
        assert result.is_valid
        assert result.execution_plan is not None

    def test_comparison_expression_valid(self):
        """比较表达式合法（WP2 新增）。"""
        result = compile_formula(
            formula="pe_ttm if pe_ttm > 0 else 0", strict_fields=False
        )
        assert result.is_valid

    def test_boolean_expression_valid(self):
        """布尔运算合法（WP2 新增）。"""
        result = compile_formula(
            formula="pe_ttm > 0 and pb > 0", strict_fields=False
        )
        assert result.is_valid

    def test_conditional_expression_valid(self):
        """条件表达式合法（WP2 新增：x if cond else y）。"""
        result = compile_formula(
            formula="1 / pe_ttm if pe_ttm > 0 else 0", strict_fields=False
        )
        assert result.is_valid

    def test_nested_arithmetic_valid(self):
        """嵌套算术合法。"""
        result = compile_formula(
            formula="(close - sma(close, 20)) / stddev(close, 20)",
            strict_fields=False,
        )
        assert result.is_valid

    def test_not_operator_valid(self):
        """not 运算符合法。"""
        result = compile_formula(
            formula="not (pe_ttm > 0)", strict_fields=False
        )
        assert result.is_valid

    def test_unary_minus_valid(self):
        """一元负号合法。"""
        result = compile_formula(formula="-pb", strict_fields=False)
        assert result.is_valid


class TestForbiddenNodes:
    """禁止的 AST 节点（精确错误码）。"""

    def test_attribute_access_forbidden(self):
        """属性访问被禁止。"""
        result = compile_formula(formula="close.open", strict_fields=False)
        assert not result.is_valid
        codes = [e.error_code for e in result.errors]
        assert "attribute_access_forbidden" in codes

    def test_subscript_forbidden(self):
        """下标访问被禁止。"""
        result = compile_formula(formula="close[0]", strict_fields=False)
        assert not result.is_valid
        codes = [e.error_code for e in result.errors]
        assert "subscript_forbidden" in codes

    def test_import_forbidden(self):
        """import 被禁止。"""
        result = compile_formula(formula="__import__('os')", strict_fields=False)
        assert not result.is_valid

    def test_lambda_forbidden(self):
        """lambda 被禁止。"""
        result = compile_formula(formula="(lambda: 1)()", strict_fields=False)
        assert not result.is_valid
        codes = [e.error_code for e in result.errors]
        assert "lambda_forbidden" in codes

    def test_assignment_forbidden(self):
        """赋值被禁止。"""
        # mode="eval" 不支持赋值，会语法错误
        result = compile_formula(formula="x = 1", strict_fields=False)
        assert not result.is_valid
        codes = [e.error_code for e in result.errors]
        assert "formula_syntax_error" in codes

    def test_list_comprehension_forbidden(self):
        """列表推导式被禁止。"""
        result = compile_formula(
            formula="[x for x in close]", strict_fields=False
        )
        assert not result.is_valid
        codes = [e.error_code for e in result.errors]
        assert "comprehension_forbidden" in codes

    def test_generator_expression_forbidden(self):
        """生成器表达式被禁止。"""
        result = compile_formula(
            formula="sum(x for x in close)", strict_fields=False
        )
        assert not result.is_valid


class TestASTLimits:
    """AST 深度、节点数、函数调用数限制。"""

    def test_depth_within_limit(self):
        """深度在限制内（默认 4）。"""
        # depth 3: 1 / (pe_ttm + 1)
        result = compile_formula(
            formula="1 / (pe_ttm + 1)", strict_fields=False
        )
        assert result.is_valid
        assert result.execution_plan.ast_depth <= MAX_AST_DEPTH

    def test_depth_exceeded(self):
        """深度超限被拒绝。"""
        # 构造超过当前 DSL 上限的深层表达式。
        deep_formula = "((((((pe_ttm + 1) + 1) + 1) + 1) + 1) + 1)"
        result = compile_formula(
            formula=deep_formula, strict_fields=False
        )
        assert not result.is_valid
        codes = [e.error_code for e in result.errors]
        assert "ast_depth_exceeded" in codes

    def test_function_call_count_within_limit(self):
        """函数调用数在限制内。"""
        # 3 calls
        result = compile_formula(
            formula="abs(max(min(pe_ttm, 1), 0))", strict_fields=False
        )
        assert result.is_valid
        assert result.execution_plan.function_call_count <= MAX_FUNCTION_CALLS

    def test_function_call_count_exceeded(self):
        """函数调用数超限被拒绝。"""
        # 构造超过 12 次调用的表达式
        expr = "abs(" * 13 + "pe_ttm" + ")" * 13
        result = compile_formula(
            formula=expr, strict_fields=False
        )
        assert not result.is_valid
        codes = [e.error_code for e in result.errors]
        assert "function_call_count_exceeded" in codes or "ast_depth_exceeded" in codes


# ══════════════════════════════════════════════════════════
# WP2-02: DSL — 字段和函数能力目录
# ══════════════════════════════════════════════════════════


class TestFieldCatalog:
    """字段目录校验。"""

    def test_known_field_accepted(self):
        """已知字段被接受。"""
        result = compile_formula(formula="pe_ttm", strict_fields=True)
        assert result.is_valid
        assert "pe_ttm" in result.execution_plan.data_dependencies["fields"]

    def test_unknown_field_rejected_strict(self):
        """严格模式下未知字段被拒绝。"""
        result = compile_formula(formula="unknown_field", strict_fields=True)
        assert not result.is_valid
        codes = [e.error_code for e in result.errors]
        assert "field_not_in_catalog" in codes

    def test_unknown_field_allowed_non_strict(self):
        """非严格模式下未知字段不报错（可能是参数引用）。"""
        result = compile_formula(formula="my_param", strict_fields=False)
        assert result.is_valid
        assert "my_param" in result.execution_plan.data_dependencies["unknown_names"]

    def test_param_resolves_unknown_field(self):
        """参数可解析未知字段。"""
        result = compile_formula(
            formula="threshold",
            params={"threshold": 0.5},
            strict_fields=True,
        )
        assert result.is_valid

    def test_field_catalog_has_core_fields(self):
        """字段目录包含核心字段。"""
        required = {"pe_ttm", "pb", "close", "amount", "turnover_rate", "volume"}
        assert required.issubset(set(FIELD_CATALOG.keys()))

    def test_point_in_time_fields_marked(self):
        """point-in-time 字段被标记。"""
        result = compile_formula(
            formula="1 / pe_ttm", strict_fields=True
        )
        assert result.is_valid
        pit = result.execution_plan.data_dependencies["point_in_time_fields"]
        assert "pe_ttm" in pit


class TestFunctionCatalog:
    """函数目录校验。"""

    def test_known_function_accepted(self):
        """已知函数被接受。"""
        result = compile_formula(
            formula="abs(close)", strict_fields=False
        )
        assert result.is_valid
        assert "abs" in result.execution_plan.data_dependencies["functions"]

    def test_unknown_function_rejected(self):
        """未知函数被拒绝。"""
        result = compile_formula(
            formula="eval('os.system')", strict_fields=False
        )
        assert not result.is_valid
        codes = [e.error_code for e in result.errors]
        assert "function_not_in_catalog" in codes or "function_not_allowed" in codes

    def test_function_arg_count_too_few(self):
        """函数参数过少被拒绝。"""
        result = compile_formula(
            formula="sma(close)", strict_fields=False
        )
        assert not result.is_valid
        codes = [e.error_code for e in result.errors]
        assert "function_arg_count" in codes

    def test_function_arg_count_too_many(self):
        """函数参数过多被拒绝。"""
        result = compile_formula(
            formula="abs(close, 1, 2)", strict_fields=False
        )
        assert not result.is_valid
        codes = [e.error_code for e in result.errors]
        assert "function_arg_count" in codes

    def test_function_keyword_args_forbidden(self):
        """关键字参数被禁止。"""
        result = compile_formula(
            formula="sma(field=close, window=20)", strict_fields=False
        )
        assert not result.is_valid
        codes = [e.error_code for e in result.errors]
        assert "function_keyword_args" in codes

    def test_rolling_function_window_must_be_constant(self):
        """滚动函数窗口必须是常量。"""
        result = compile_formula(
            formula="sma(close, pe_ttm)", strict_fields=False
        )
        assert not result.is_valid
        codes = [e.error_code for e in result.errors]
        assert "function_window_invalid" in codes

    def test_rolling_function_window_must_be_positive(self):
        """滚动函数窗口必须为正。"""
        result = compile_formula(
            formula="sma(close, 0)", strict_fields=False
        )
        assert not result.is_valid
        codes = [e.error_code for e in result.errors]
        assert "negative_lag" in codes

    def test_rolling_function_window_exceeds_limit(self):
        """滚动函数窗口超限被拒绝。"""
        result = compile_formula(
            formula="sma(close, 999)", strict_fields=False
        )
        assert not result.is_valid
        codes = [e.error_code for e in result.errors]
        assert "lookback_window_exceeded" in codes

    def test_rolling_function_window_at_limit(self):
        """滚动函数窗口在限制内合法。"""
        result = compile_formula(
            formula=f"sma(close, {MAX_LOOKBACK_WINDOW})", strict_fields=False
        )
        assert result.is_valid


# ══════════════════════════════════════════════════════════
# WP2-01: 依赖收集
# ══════════════════════════════════════════════════════════


class TestDependencyCollection:
    """依赖收集测试。"""

    def test_collects_fields(self):
        """收集字段依赖。"""
        result = compile_formula(
            formula="1 / pe_ttm", strict_fields=True
        )
        assert result.is_valid
        deps = result.execution_plan.data_dependencies
        assert "pe_ttm" in deps["fields"]

    def test_collects_multiple_fields(self):
        """收集多个字段依赖。"""
        result = compile_formula(
            formula="pe_ttm / pb", strict_fields=True
        )
        assert result.is_valid
        deps = result.execution_plan.data_dependencies
        assert set(deps["fields"]) == {"pe_ttm", "pb"}

    def test_collects_functions(self):
        """收集函数依赖。"""
        result = compile_formula(
            formula="abs(close)", strict_fields=False
        )
        assert result.is_valid
        deps = result.execution_plan.data_dependencies
        assert "abs" in deps["functions"]

    def test_collects_source_tables(self):
        """收集源表依赖。"""
        result = compile_formula(
            formula="1 / pe_ttm", strict_fields=True
        )
        assert result.is_valid
        deps = result.execution_plan.data_dependencies
        assert "raw_valuation_snapshots" in deps["source_tables"]

    def test_collects_max_lookback(self):
        """收集最大回看窗口。"""
        result = compile_formula(
            formula="sma(close, 20)", strict_fields=False
        )
        assert result.is_valid
        deps = result.execution_plan.data_dependencies
        assert deps["max_lookback"] == 20

    def test_collects_point_in_time_fields(self):
        """收集 point-in-time 字段。"""
        result = compile_formula(
            formula="roe_ttm", strict_fields=True
        )
        assert result.is_valid
        deps = result.execution_plan.data_dependencies
        assert "roe_ttm" in deps["point_in_time_fields"]


# ══════════════════════════════════════════════════════════
# WP2-03: 后处理配置校验
# ══════════════════════════════════════════════════════════


class TestPostprocessValidation:
    """后处理配置校验。"""

    def test_none_postprocess_valid(self):
        """None 后处理配置合法。"""
        errors = validate_postprocess(None)
        assert len(errors) == 0

    def test_empty_postprocess_valid(self):
        """空字典后处理配置合法。"""
        errors = validate_postprocess({})
        assert len(errors) == 0

    def test_valid_winsorize_mad(self):
        """合法的 MAD winsorize 配置。"""
        errors = validate_postprocess({
            "winsorize": {"method": "mad", "mad_multiplier": 3.0}
        })
        assert len(errors) == 0

    def test_valid_winsorize_quantile(self):
        """合法的 quantile winsorize 配置。"""
        errors = validate_postprocess({
            "winsorize": {
                "method": "quantile",
                "lower_quantile": 0.01,
                "upper_quantile": 0.99,
            }
        })
        assert len(errors) == 0

    def test_invalid_winsorize_method(self):
        """非法的 winsorize 方法。"""
        errors = validate_postprocess({
            "winsorize": {"method": "invalid"}
        })
        assert len(errors) == 1
        assert errors[0].error_code == "postprocess_invalid"

    def test_winsorize_multiplier_out_of_range(self):
        """mad_multiplier 超范围。"""
        errors = validate_postprocess({
            "winsorize": {"mad_multiplier": 10.0}
        })
        assert len(errors) == 1
        assert errors[0].error_code == "postprocess_invalid"

    def test_valid_rank(self):
        """合法的 rank 配置。"""
        errors = validate_postprocess({
            "rank": {"method": "percentile", "ascending": True}
        })
        assert len(errors) == 0

    def test_invalid_rank_method(self):
        """非法的 rank 方法。"""
        errors = validate_postprocess({
            "rank": {"method": "invalid"}
        })
        assert len(errors) == 1

    def test_valid_zscore(self):
        """合法的 zscore 配置。"""
        errors = validate_postprocess({"zscore": {"ddof": 0}})
        assert len(errors) == 0

    def test_invalid_zscore_ddof(self):
        """非法的 zscore ddof。"""
        errors = validate_postprocess({"zscore": {"ddof": 5}})
        assert len(errors) == 1

    def test_valid_neutralize(self):
        """合法的 neutralize 配置。"""
        errors = validate_postprocess({
            "neutralize": {"by": "industry"}
        })
        assert len(errors) == 0

    def test_invalid_neutralize_by(self):
        """非法的 neutralize by。"""
        errors = validate_postprocess({
            "neutralize": {"by": "invalid"}
        })
        assert len(errors) == 1

    def test_valid_missing_policy(self):
        """合法的 missing_policy。"""
        for policy in ("exclude", "impute_zero", "ignore"):
            errors = validate_postprocess({"missing_policy": policy})
            assert len(errors) == 0

    def test_invalid_missing_policy(self):
        """非法的 missing_policy。"""
        errors = validate_postprocess({"missing_policy": "fill_mean"})
        assert len(errors) == 1
        assert errors[0].error_code == "missing_policy_invalid"

    def test_postprocess_in_formula(self):
        """公式编译集成后处理校验。"""
        result = compile_formula(
            formula="close",
            postprocess={"missing_policy": "invalid_policy"},
            strict_fields=False,
        )
        assert not result.is_valid
        codes = [e.error_code for e in result.errors]
        assert "missing_policy_invalid" in codes


# ══════════════════════════════════════════════════════════
# WP2-04: 稳定执行计划和哈希
# ══════════════════════════════════════════════════════════


class TestExecutionPlan:
    """执行计划与哈希测试。"""

    def test_same_input_same_hash(self):
        """相同输入生成相同 content_hash（确定性）。"""
        r1 = compile_formula(formula="1 / pe_ttm", strict_fields=True)
        r2 = compile_formula(formula="1 / pe_ttm", strict_fields=True)
        assert r1.is_valid and r2.is_valid
        assert r1.execution_plan.content_hash() == r2.execution_plan.content_hash()

    def test_different_formula_different_hash(self):
        """不同公式生成不同 content_hash。"""
        r1 = compile_formula(formula="1 / pe_ttm", strict_fields=True)
        r2 = compile_formula(formula="-pb", strict_fields=True)
        assert r1.is_valid and r2.is_valid
        assert r1.execution_plan.content_hash() != r2.execution_plan.content_hash()

    def test_different_direction_different_hash(self):
        """不同方向生成不同 content_hash。"""
        r1 = compile_formula(
            formula="close", direction="higher_better", strict_fields=False
        )
        r2 = compile_formula(
            formula="close", direction="lower_better", strict_fields=False
        )
        assert r1.execution_plan.content_hash() != r2.execution_plan.content_hash()

    def test_different_postprocess_different_hash(self):
        """不同后处理配置生成不同 content_hash。"""
        r1 = compile_formula(
            formula="close",
            postprocess=None,
            strict_fields=False,
        )
        r2 = compile_formula(
            formula="close",
            postprocess={"missing_policy": "exclude"},
            strict_fields=False,
        )
        assert r1.execution_plan.content_hash() != r2.execution_plan.content_hash()

    def test_canonical_json_is_deterministic(self):
        """canonical JSON 是确定性的（排序键）。"""
        result = compile_formula(formula="1 / pe_ttm", strict_fields=True)
        json1 = result.execution_plan.to_canonical_json()
        json2 = result.execution_plan.to_canonical_json()
        assert json1 == json2

    def test_content_hash_length(self):
        """content_hash 长度为 16（SHA256 前 16 位）。"""
        result = compile_formula(formula="close", strict_fields=False)
        assert len(result.execution_plan.content_hash()) == 16

    def test_compiler_version_set(self):
        """编译器版本被设置。"""
        result = compile_formula(formula="close", strict_fields=False)
        assert result.execution_plan.compiler_version == COMPILER_VERSION

    def test_complexity_score_positive(self):
        """复杂度分数为正。"""
        result = compile_formula(
            formula="sma(close, 20)", strict_fields=False
        )
        assert result.execution_plan.complexity_score > 0

    def test_execution_plan_to_dict_has_hash(self):
        """执行计划字典包含 execution_plan_hash。"""
        result = compile_formula(formula="close", strict_fields=False)
        d = result.execution_plan.to_dict()
        assert "execution_plan_hash" in d
        assert d["execution_plan_hash"] == result.execution_plan.content_hash()

    def test_params_normalized_sorted(self):
        """参数被排序规范化。"""
        result = compile_formula(
            formula="threshold",
            params={"z": 1, "a": 2, "m": 3},
            strict_fields=False,
        )
        assert result.is_valid
        params = result.execution_plan.params
        assert list(params.keys()) == ["a", "m", "z"]


# ══════════════════════════════════════════════════════════
# WP2-01: 错误码体系
# ══════════════════════════════════════════════════════════


class TestErrorCodes:
    """错误码稳定性测试。"""

    def test_empty_formula_error(self):
        """空公式返回 formula_empty。"""
        result = compile_formula(formula="", strict_fields=False)
        assert not result.is_valid
        assert result.errors[0].error_code == "formula_empty"

    def test_syntax_error_code(self):
        """语法错误返回 formula_syntax_error。"""
        result = compile_formula(formula="1 +", strict_fields=False)
        assert not result.is_valid
        assert result.errors[0].error_code == "formula_syntax_error"

    def test_error_to_dict_has_code(self):
        """错误转 dict 包含 error_code。"""
        result = compile_formula(formula="", strict_fields=False)
        err_dict = result.errors[0].to_dict()
        assert "error_code" in err_dict
        assert "message" in err_dict

    def test_multiple_errors_collected(self):
        """多个错误被收集。"""
        # 同时有未知函数和未知字段
        result = compile_formula(
            formula="unknown_func(unknown_field)", strict_fields=True
        )
        assert not result.is_valid
        assert len(result.errors) >= 1


# ══════════════════════════════════════════════════════════
# WP2-05: 版本创建集成（编译器自动填充执行计划字段）
# ══════════════════════════════════════════════════════════


class TestVersionCreationIntegration:
    """版本创建时编译器集成测试。"""

    def test_valid_version_has_execution_plan(self, db_session):
        """合法公式的版本包含执行计划字段。"""
        from app.schemas.factor_library import FactorDraftCreate, FactorVersionCreate
        from app.services.factors.factor_registry import (
            create_factor_draft,
            create_factor_version,
        )

        draft = FactorDraftCreate(
            code="test_compiler_valid", name="Test", category="fundamental"
        )
        factor = create_factor_draft(db_session, draft=draft)

        request = FactorVersionCreate(formula_expr="1 / pe_ttm")
        version = create_factor_version(
            db_session, factor_id=factor.id, request=request
        )

        assert version.validation_status == "valid"
        assert version.compiler_version == COMPILER_VERSION
        assert version.execution_plan_hash is not None
        assert version.complexity_score is not None
        assert version.formula_ast_json is not None
        assert version.data_dependencies_json is not None
        assert version.validation_errors_json is None

    def test_invalid_version_marks_validation_failed(self, db_session):
        """非法公式的版本标记为 invalid。"""
        from app.schemas.factor_library import FactorDraftCreate, FactorVersionCreate
        from app.services.factors.factor_registry import (
            create_factor_draft,
            create_factor_version,
        )

        draft = FactorDraftCreate(
            code="test_compiler_invalid", name="Test", category="fundamental"
        )
        factor = create_factor_draft(db_session, draft=draft)

        # 使用包含禁止节点的公式
        request = FactorVersionCreate(formula_expr="close.open")
        version = create_factor_version(
            db_session, factor_id=factor.id, request=request
        )

        assert version.validation_status == "invalid"
        assert version.validation_errors_json is not None
        errors = json.loads(version.validation_errors_json)
        assert len(errors) > 0
        assert any(
            e["error_code"] == "attribute_access_forbidden" for e in errors
        )

    def test_version_idempotent_same_content(self, db_session):
        """同内容版本幂等。"""
        from app.schemas.factor_library import FactorDraftCreate, FactorVersionCreate
        from app.services.factors.factor_registry import (
            create_factor_draft,
            create_factor_version,
        )

        draft = FactorDraftCreate(
            code="test_compiler_idem", name="Test", category="fundamental"
        )
        factor = create_factor_draft(db_session, draft=draft)

        request = FactorVersionCreate(formula_expr="1 / pe_ttm")
        v1 = create_factor_version(
            db_session, factor_id=factor.id, request=request
        )
        v2 = create_factor_version(
            db_session, factor_id=factor.id, request=request
        )
        assert v1.id == v2.id

    def test_version_execution_plan_hash_deterministic(self, db_session):
        """版本执行计划哈希确定性。"""
        from app.schemas.factor_library import FactorDraftCreate, FactorVersionCreate
        from app.services.factors.factor_registry import (
            create_factor_draft,
            create_factor_version,
        )

        # 创建两个因子，相同公式
        for code in ["test_hash_a", "test_hash_b"]:
            draft = FactorDraftCreate(
                code=code, name=f"Test {code}", category="fundamental"
            )
            factor = create_factor_draft(db_session, draft=draft)
            request = FactorVersionCreate(formula_expr="1 / pe_ttm")
            version = create_factor_version(
                db_session, factor_id=factor.id, request=request
            )
            assert version.execution_plan_hash is not None

        # 两个版本的哈希应该相同（相同公式、参数、方向）
        from app.services.factors.factor_registry import get_factor_by_code
        f_a = get_factor_by_code(db_session, "test_hash_a")
        f_b = get_factor_by_code(db_session, "test_hash_b")
        from app.services.factors.factor_registry import get_latest_version
        v_a = get_latest_version(db_session, f_a.id)
        v_b = get_latest_version(db_session, f_b.id)
        assert v_a.execution_plan_hash == v_b.execution_plan_hash


# ══════════════════════════════════════════════════════════
# WP2-05: 校验 API 端点测试
# ══════════════════════════════════════════════════════════


class TestValidateAPI:
    """POST /factors/validate 端点测试。"""

    def test_validate_valid_formula(self):
        """校验合法公式。"""
        from app.api.routes.factors import validate_factor_formula
        from app.schemas.factor_library import FactorValidateRequest

        payload = FactorValidateRequest(
            formula_expr="1 / pe_ttm",
            direction="higher_better",
        )
        response = validate_factor_formula(payload)
        assert response["is_valid"] is True
        assert response["execution_plan"] is not None
        assert response["errors"] == []
        assert response["data_dependencies"] is not None

    def test_validate_invalid_formula_attribute(self):
        """校验非法公式（属性访问）。"""
        from app.api.routes.factors import validate_factor_formula
        from app.schemas.factor_library import FactorValidateRequest

        payload = FactorValidateRequest(
            formula_expr="close.open",
        )
        response = validate_factor_formula(payload)
        assert response["is_valid"] is False
        assert len(response["errors"]) > 0
        assert any(
            e["error_code"] == "attribute_access_forbidden"
            for e in response["errors"]
        )

    def test_validate_unknown_field_strict(self):
        """严格模式校验未知字段。"""
        from app.api.routes.factors import validate_factor_formula
        from app.schemas.factor_library import FactorValidateRequest

        payload = FactorValidateRequest(
            formula_expr="unknown_field_xyz",
        )
        response = validate_factor_formula(payload)
        assert response["is_valid"] is False
        assert any(
            e["error_code"] == "field_not_in_catalog"
            for e in response["errors"]
        )

    def test_validate_returns_data_dependencies(self):
        """校验返回数据依赖。"""
        from app.api.routes.factors import validate_factor_formula
        from app.schemas.factor_library import FactorValidateRequest

        payload = FactorValidateRequest(
            formula_expr="1 / pe_ttm",
        )
        response = validate_factor_formula(payload)
        assert response["is_valid"] is True
        deps = response["data_dependencies"]
        assert "pe_ttm" in deps["fields"]
        assert "raw_valuation_snapshots" in deps["source_tables"]

    def test_validate_with_postprocess(self):
        """校验带后处理配置。"""
        from app.api.routes.factors import validate_factor_formula
        from app.schemas.factor_library import FactorValidateRequest

        payload = FactorValidateRequest(
            formula_expr="close",
            postprocess={"missing_policy": "exclude"},
        )
        response = validate_factor_formula(payload)
        assert response["is_valid"] is True

    def test_validate_invalid_postprocess(self):
        """校验非法后处理配置。"""
        from app.api.routes.factors import validate_factor_formula
        from app.schemas.factor_library import FactorValidateRequest

        payload = FactorValidateRequest(
            formula_expr="close",
            postprocess={"missing_policy": "invalid"},
        )
        response = validate_factor_formula(payload)
        assert response["is_valid"] is False
        assert any(
            e["error_code"] == "missing_policy_invalid"
            for e in response["errors"]
        )


# ══════════════════════════════════════════════════════════
# WP2-05: 预览 API 端点测试
# ══════════════════════════════════════════════════════════


class TestPreviewAPI:
    """POST /factors/preview 端点测试。"""

    def test_preview_valid_formula(self, db_session):
        """预览合法公式返回执行计划。"""
        from app.api.routes.factors import preview_factor_formula
        from app.schemas.factor_library import FactorPreviewRequest

        payload = FactorPreviewRequest(
            formula_expr="1 / pe_ttm",
        )
        response = preview_factor_formula(payload, db_session)
        assert response["is_valid"] is True
        assert response["execution_plan"] is not None

    def test_preview_invalid_formula(self, db_session):
        """预览非法公式返回错误。"""
        from app.api.routes.factors import preview_factor_formula
        from app.schemas.factor_library import FactorPreviewRequest

        payload = FactorPreviewRequest(
            formula_expr="close.open",
        )
        response = preview_factor_formula(payload, db_session)
        assert response["is_valid"] is False
        assert len(response["errors"]) > 0

    def test_preview_returns_data_dependencies(self, db_session):
        """预览返回数据依赖。"""
        from app.api.routes.factors import preview_factor_formula
        from app.schemas.factor_library import FactorPreviewRequest

        payload = FactorPreviewRequest(
            formula_expr="1 / pe_ttm",
        )
        response = preview_factor_formula(payload, db_session)
        assert response["is_valid"] is True
        assert response["data_dependencies"] is not None
        assert "pe_ttm" in response["data_dependencies"]["fields"]

    def test_preview_returns_execution_plan_hash(self, db_session):
        """预览返回执行计划哈希。"""
        from app.api.routes.factors import preview_factor_formula
        from app.schemas.factor_library import FactorPreviewRequest

        payload = FactorPreviewRequest(
            formula_expr="1 / pe_ttm",
        )
        response = preview_factor_formula(payload, db_session)
        assert response["is_valid"] is True
        plan = response["execution_plan"]
        assert "execution_plan_hash" in plan
        assert len(plan["execution_plan_hash"]) == 16

    def test_preview_same_input_same_hash(self, db_session):
        """相同预览输入生成相同哈希（可重复）。"""
        from app.api.routes.factors import preview_factor_formula
        from app.schemas.factor_library import FactorPreviewRequest

        payload = FactorPreviewRequest(
            formula_expr="1 / pe_ttm",
        )
        r1 = preview_factor_formula(payload, db_session)
        r2 = preview_factor_formula(payload, db_session)
        assert r1["execution_plan"]["execution_plan_hash"] == r2["execution_plan"]["execution_plan_hash"]


# ══════════════════════════════════════════════════════════
# 综合场景测试
# ══════════════════════════════════════════════════════════


class TestSystemFactorFormulas:
    """系统因子公式编译测试（验证现有因子可被编译器处理）。"""

    @pytest.mark.parametrize("formula", [
        "1 / pe_ttm",           # ep_ttm
        "-pb",                  # negative_pb
        "close",                # 基础引用
        "turnover_rate",        # turnover_z20 基础
        "main_net_inflow",      # 资金流基础
        "lhb_institution_net",  # 龙虎榜基础
        "hot_rank_pct",         # 热度基础
        "proxy_score",          # 尾盘基础
    ])
    def test_system_factor_formula_compilable(self, formula):
        """系统因子基础公式可编译。"""
        result = compile_formula(
            formula=formula, strict_fields=True
        )
        assert result.is_valid, f"Formula '{formula}' should be valid: {[e.to_dict() for e in result.errors]}"

    def test_zscore_formula_compilable(self):
        """Z-score 公式可编译。"""
        # (turnover - mean_20d) / stddev_20d 的简化版本
        result = compile_formula(
            formula="(turnover_rate - sma(turnover_rate, 20)) / stddev(turnover_rate, 20)",
            strict_fields=True,
        )
        assert result.is_valid

    def test_conditional_ep_formula_compilable(self):
        """条件 EP 公式可编译。"""
        result = compile_formula(
            formula="1 / pe_ttm if pe_ttm > 0 else 0",
            strict_fields=True,
        )
        assert result.is_valid

    def test_rolling_sum_formula_compilable(self):
        """滚动求和公式可编译。"""
        result = compile_formula(
            formula="sum(main_net_inflow, 5) / sum(amount, 5)",
            strict_fields=True,
        )
        assert result.is_valid
        deps = result.execution_plan.data_dependencies
        assert deps["max_lookback"] == 5


class TestCompilerConfig:
    """编译器配置测试。"""

    def test_custom_depth_limit(self):
        """自定义深度限制。"""
        compiler = FactorCompiler(max_depth=2)
        # depth 3 的表达式
        result = compiler.compile(
            formula="1 / (pe_ttm + 1)", strict_fields=False
        )
        assert not result.is_valid
        assert any(e.error_code == "ast_depth_exceeded" for e in result.errors)

    def test_custom_function_call_limit(self):
        """自定义函数调用数限制。"""
        compiler = FactorCompiler(max_function_calls=2)
        result = compiler.compile(
            formula="abs(max(min(pe_ttm, 1), 0))", strict_fields=False
        )
        assert not result.is_valid
        assert any(
            e.error_code == "function_call_count_exceeded" for e in result.errors
        )

    def test_custom_lookback_limit(self):
        """自定义回看窗口限制。"""
        compiler = FactorCompiler(max_lookback=10)
        result = compiler.compile(
            formula="sma(close, 20)", strict_fields=False
        )
        assert not result.is_valid
        assert any(
            e.error_code == "lookback_window_exceeded" for e in result.errors
        )
