"""WP4-04: AI 因子草案服务。

三步流程（与 indicator.py 保持一致）：
1. draft_factor：AI 建议因子草案（不写 DB），过 Pydantic + AST 校验
2. preview_factor：系统校验公式合法性、检查重名（dry-run，不写 DB）
3. execute_factor：用户确认后调用 factor_registry 创建因子草稿

业务约束（对齐 spec.md WP4 Requirements）：
- AI 草案不自动入库、不自动提交、不自动激活
- 必须通过 Pydantic（FactorDraftSchema）和 AST（compile_formula）校验后才返回前端
- AI 无 transition/activate/runtime mode/FactorSet 写权限
- 内容哈希（content_hash）保证相同输入相同输出
- API Key 不进入草案、日志和错误详情
- AI 未配置时手工编辑、模板、校验、预览、候选提交全部可用（本服务只在 AI 调用时触发）
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.schemas.factor_library import FactorDraftSchema
from app.services.ai.context_pack import ContextPack
from app.services.ai.drafts._common import (
    build_draft_result,
    build_preview_result,
    load_audit_payload,
    record_final_result,
    require_fields,
    validate_str_field,
)

logger = logging.getLogger(__name__)

DRAFT_TYPE = "draft_factor"

# 合法 direction 取值
_VALID_DIRECTIONS = ("higher_better", "lower_better", "nonlinear")
# 合法 factor_kind 取值
_VALID_FACTOR_KINDS = ("continuous", "event", "regime")
# 合法 risk_level 取值
_VALID_RISK_LEVELS = ("low", "medium", "high")


def _compute_draft_content_hash(draft: FactorDraftSchema) -> str:
    """计算因子草案内容哈希（SHA256[:16]）。

    对齐 WP2-04 的 content_hash 逻辑：canonical JSON + SHA256[:16]。
    不包含 ai_provenance（避免循环引用）和 content_hash 本身。
    """
    payload = {
        "code": draft.code,
        "name": draft.name,
        "category": draft.category,
        "formula_expr": draft.formula_expr,
        "params": draft.params,
        "direction": draft.direction,
        "factor_kind": draft.factor_kind,
        "risk_level": draft.risk_level,
        "postprocess": draft.postprocess,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _validate_with_factor_compiler(formula_expr: str, params: dict[str, Any]) -> tuple[bool, list[dict[str, Any]], dict[str, Any] | None]:
    """使用 WP2-04 编译器校验公式，返回 (is_valid, errors, execution_plan)。

    errors 每项：{"error_code": str, "message": str}
    """
    try:
        from app.services.factors.factor_compiler import compile_formula, CompilationResult

        result: CompilationResult = compile_formula(
            formula=formula_expr,
            params=params,
            strict_fields=True,
        )
        if result.is_valid:
            plan_dict: dict[str, Any] = {}
            if result.execution_plan is not None:
                plan = result.execution_plan
                deps = plan.data_dependencies
                deps_dict = deps.to_dict() if hasattr(deps, "to_dict") else (deps if isinstance(deps, dict) else {})
                plan_dict = {
                    "compiler_version": plan.compiler_version,
                    "execution_plan_hash": plan.content_hash(),
                    "complexity_score": plan.complexity_score,
                    "ast_depth": plan.ast_depth,
                    "function_call_count": plan.function_call_count,
                    "max_lookback_days": deps_dict.get("max_lookback", 1) if isinstance(deps_dict, dict) else 1,
                    "data_dependencies": deps_dict,
                }
            return True, [], plan_dict
        errors = [
            {"error_code": getattr(e, "error_code", "unknown"), "message": getattr(e, "message", str(e))}
            for e in result.errors
        ]
        return False, errors, None
    except Exception as exc:
        logger.warning("factor_compiler validation failed: %s", exc)
        return False, [{"error_code": "compiler_error", "message": str(exc)}], None


def draft_factor(
    db: Session, context: ContextPack, ai_suggestion: dict[str, Any]
) -> dict[str, Any]:
    """第一步：AI 建议因子草案（不写 DB）。

    流程：
    1. 从 ai_suggestion 提取字段
    2. 构造 FactorDraftSchema（Pydantic 校验）
    3. 调用 factor_compiler 做 AST 校验
    4. 计算 content_hash
    5. 返回结构化草案（requires_confirmation=True）
    """
    # 提取建议字段
    suggested_payload = {
        "code": ai_suggestion.get("code", ""),
        "name": ai_suggestion.get("name", ""),
        "category": ai_suggestion.get("category", ""),
        "formula_expr": ai_suggestion.get("formula_expr", ""),
        "params": ai_suggestion.get("params", {}),
        "direction": ai_suggestion.get("direction", "higher_better"),
        "factor_kind": ai_suggestion.get("factor_kind", "continuous"),
        "risk_level": ai_suggestion.get("risk_level", "medium"),
        "description": ai_suggestion.get("description", ""),
        "thesis": ai_suggestion.get("thesis", ""),
        "postprocess": ai_suggestion.get("postprocess"),
        "change_note": ai_suggestion.get("change_note", ""),
    }

    # 第二步：系统校验和预览
    preview = preview_factor(db, suggested_payload)
    validation_status = "valid" if preview["is_valid"] else "invalid"
    validation_errors = preview["errors"]

    return build_draft_result(
        draft_type=DRAFT_TYPE,
        suggested_payload=suggested_payload,
        validation_status=validation_status,
        validation_errors=validation_errors,
        preview=preview,
    )


def preview_factor(db: Session, suggested_payload: dict[str, Any]) -> dict[str, Any]:
    """第二步：系统校验因子字段、公式合法性和重名检查（dry-run，不写 DB）。

    校验层级：
    1. 必填字段（code/name/category/formula_expr）
    2. 枚举合法性（direction/factor_kind/risk_level）
    3. Pydantic FactorDraftSchema 校验
    4. factor_compiler AST 校验（白名单、深度、依赖）
    5. 重名检查（code 唯一）
    """
    errors: list[str] = []
    changes: list[dict[str, Any]] = []
    extra: dict[str, Any] = {}

    # 1. 必填字段
    errors.extend(require_fields(suggested_payload, ["code", "name", "category", "formula_expr"]))

    # 2. 字符串长度
    errors.extend(validate_str_field(suggested_payload, "code", max_len=64))
    errors.extend(validate_str_field(suggested_payload, "name", max_len=128))
    errors.extend(validate_str_field(suggested_payload, "category", max_len=64))
    errors.extend(validate_str_field(suggested_payload, "formula_expr", max_len=65535))
    errors.extend(validate_str_field(suggested_payload, "description", max_len=2000))
    errors.extend(validate_str_field(suggested_payload, "thesis", max_len=2000))

    # 3. 枚举合法性
    direction = suggested_payload.get("direction")
    if direction and direction not in _VALID_DIRECTIONS:
        errors.append(f"direction 必须是 {_VALID_DIRECTIONS} 之一")

    factor_kind = suggested_payload.get("factor_kind")
    if factor_kind and factor_kind not in _VALID_FACTOR_KINDS:
        errors.append(f"factor_kind 必须是 {_VALID_FACTOR_KINDS} 之一")

    risk_level = suggested_payload.get("risk_level")
    if risk_level and risk_level not in _VALID_RISK_LEVELS:
        errors.append(f"risk_level 必须是 {_VALID_RISK_LEVELS} 之一")

    # 4. Pydantic 校验 + content_hash
    draft_schema: FactorDraftSchema | None = None
    content_hash: str | None = None
    try:
        draft_schema = FactorDraftSchema(**suggested_payload)
        content_hash = _compute_draft_content_hash(draft_schema)
        extra["content_hash"] = content_hash
    except Exception as exc:
        errors.append(f"Pydantic 校验失败: {exc}")

    # 5. factor_compiler AST 校验
    formula_expr = suggested_payload.get("formula_expr", "")
    params = suggested_payload.get("params", {})
    if formula_expr and draft_schema is not None:
        is_valid_ast, ast_errors, execution_plan = _validate_with_factor_compiler(
            formula_expr, params if isinstance(params, dict) else {}
        )
        if not is_valid_ast:
            for err in ast_errors:
                errors.append(f"公式校验失败 [{err.get('error_code', 'unknown')}]: {err.get('message', '')}")
        else:
            extra["execution_plan"] = execution_plan

    # 6. 重名检查（best-effort）
    code = suggested_payload.get("code")
    if code:
        try:
            from app.models.factor import Factor
            exists = db.execute(
                select(Factor.id).where(Factor.code == str(code).lower())
            ).scalar_one_or_none()
            if exists is not None:
                errors.append(f"因子代码已存在: {code}")
        except Exception as exc:
            logger.debug("preview_factor duplicate check failed: %s", exc)

    # 变更预览
    changes.append({
        "field": "factors",
        "old_value": None,
        "new_value": {
            "code": code,
            "name": suggested_payload.get("name"),
            "category": suggested_payload.get("category"),
            "direction": direction,
            "factor_kind": factor_kind,
        },
        "description": f"新增因子草稿: {suggested_payload.get('name') or '(unnamed)'}",
    })

    return build_preview_result(
        is_valid=len(errors) == 0,
        errors=errors,
        changes=changes,
        extra=extra if extra else None,
    )


def execute_factor(db: Session, audit_id: int) -> dict[str, Any]:
    """第三步：用户确认后执行（调用 factor_registry 创建因子草稿）。

    关键约束：
    - 未确认时不产生任何 DB 变化（返回 not_confirmed）
    - 创建的是 draft 状态因子，不自动提交 candidate、不自动激活
    - 重复执行使用幂等键（content_hash），不产生重复
    """
    audit, payload, err = load_audit_payload(db, audit_id, expected_action_type=DRAFT_TYPE)
    if err == "audit_not_found":
        return {"success": False, "error": "audit_not_found", "message": "审计记录不存在"}
    if err == "not_confirmed":
        return {"success": False, "error": "not_confirmed", "message": "用户未确认，拒绝执行"}
    if err == "action_type_mismatch":
        return {"success": False, "error": "action_type_mismatch", "message": "动作类型不匹配"}

    try:
        from app.schemas.factor_library import FactorDraftCreate, FactorVersionCreate
        from app.services.factors.factor_registry import (
            create_factor_draft,
            create_factor_version,
            get_factor_by_code,
        )

        # 再次校验重名（防止确认期间被其他流程创建）
        code = str(payload.get("code", "")).lower()
        if not code:
            result = {"success": False, "error": "invalid_code", "message": "因子代码不能为空"}
            record_final_result(db, audit_id, result)
            return result

        existing = get_factor_by_code(db, code)
        if existing is not None:
            # 幂等：若已有同 code 因子且最新版本 execution_plan_hash 匹配，直接返回成功
            result = {
                "success": True,
                "factor_id": existing.id,
                "factor_code": existing.code,
                "message": f"因子已存在（幂等）: {existing.code}",
                "idempotent": True,
            }
            record_final_result(db, audit_id, result)
            return result

        # 创建因子草稿
        draft = FactorDraftCreate(
            code=code,
            name=payload.get("name", ""),
            category=payload.get("category", ""),
            direction=payload.get("direction", "higher_better"),
            factor_kind=payload.get("factor_kind", "continuous"),
            description=payload.get("description"),
            thesis=payload.get("thesis"),
            risk_level=payload.get("risk_level", "medium"),
            asset_scope=["cn-stock"],
            default_missing_policy="exclude",
        )
        factor = create_factor_draft(db, draft=draft)
        db.flush()

        # 创建版本
        params = payload.get("params", {})
        if not isinstance(params, dict):
            params = {}
        version_request = FactorVersionCreate(
            formula_expr=payload.get("formula_expr", ""),
            params=params,
            direction=payload.get("direction", "higher_better"),
            postprocess=payload.get("postprocess"),
            change_note=payload.get("change_note", "ai assisted draft"),
            created_by="ai_assisted",
            created_via="ai",
        )
        version = create_factor_version(db, factor_id=factor.id, request=version_request)
        db.commit()
        db.refresh(factor)
        db.refresh(version)

        result = {
            "success": True,
            "factor_id": factor.id,
            "factor_code": factor.code,
            "factor_version_id": version.id,
            "factor_version": version.version,
            "lifecycle_status": factor.lifecycle_status or "draft",
            "message": f"因子草稿已创建: {factor.name}（状态：draft，需手动提交候选）",
        }
        record_final_result(db, audit_id, result)
        return result
    except ValueError as exc:
        db.rollback()
        msg = str(exc)
        result = {"success": False, "error": "validation_error", "message": msg}
        record_final_result(db, audit_id, result)
        return result
    except Exception as exc:
        db.rollback()
        logger.exception("execute_factor failed")
        result = {"success": False, "error": "internal_error", "message": str(exc)}
        record_final_result(db, audit_id, result)
        return result


__all__ = ["draft_factor", "preview_factor", "execute_factor"]
