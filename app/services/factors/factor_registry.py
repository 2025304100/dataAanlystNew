"""WP1-04: 因子库注册表服务（CRUD）。

提供因子与因子版本的查询、创建能力，是 API 层与 ORM 之间的薄服务层。
对齐 docs/专业因子库开发计划.md §WP1-04。

设计要点：
- 创建草稿时强制 lifecycle_status='draft'、origin='user'、is_active=0（向后兼容 legacy status）
- 创建版本时计算 content_hash 实现幂等（同内容直接返回既有版本）
- 创建版本时检测上一最新版本是否被引用（不可变），不可变则阻止 is_latest 切换
- 引用检测覆盖：EvaluationRun / FactorSetMember / FactorWeightSnapshot
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models.factor import Factor
from app.models.factor_evaluation import EvaluationRun, FactorSetMember
from app.models.factor_model import FactorVersion, FactorWeightSnapshot
from app.schemas.factor_library import (
    FactorDraftCreate,
    FactorReferenceRead,
    FactorVersionCreate,
)
from app.services.factors.baseline_freeze import compute_factor_content_hash


# ── 工具函数 ──────────────────────────────────────────────


def _dumps(value: Any) -> str:
    """安全 JSON 序列化，保证 None 与边界值不抛错。"""
    if value is None:
        return ""
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return ""


def _dumps_or_none(value: Any) -> str | None:
    """同 _dumps，但 None 输入返回 None（用于 nullable JSON 字段）。"""
    if value is None:
        return None
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return None


# ── 因子查询 ──────────────────────────────────────────────


def list_factors(
    db: Session,
    *,
    lifecycle_status: str | None = None,
    origin: str | None = None,
    factor_kind: str | None = None,
    category: str | None = None,
    search: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[Factor], int]:
    """分页查询因子列表，返回 (items, total)。"""
    conditions = []
    if lifecycle_status:
        conditions.append(Factor.lifecycle_status == lifecycle_status)
    if origin:
        conditions.append(Factor.origin == origin)
    if factor_kind:
        conditions.append(Factor.factor_kind == factor_kind)
    if category:
        conditions.append(Factor.category == category)
    if search:
        pattern = f"%{search}%"
        conditions.append(
            or_(
                Factor.code.ilike(pattern),
                Factor.name.ilike(pattern),
                Factor.description.ilike(pattern),
            )
        )

    count_query = select(func.count()).select_from(Factor)
    for cond in conditions:
        count_query = count_query.where(cond)
    total = int(db.execute(count_query).scalar_one())

    offset = (page - 1) * page_size
    list_query = select(Factor)
    for cond in conditions:
        list_query = list_query.where(cond)
    list_query = (
        list_query.order_by(Factor.id.desc()).offset(offset).limit(page_size)
    )
    items = list(db.execute(list_query).scalars().all())
    return items, total


def get_factor(db: Session, factor_id: int) -> Factor | None:
    """按主键获取因子。"""
    return db.get(Factor, factor_id)


def get_factor_by_code(db: Session, code: str) -> Factor | None:
    """按 code 获取因子。"""
    return db.execute(
        select(Factor).where(Factor.code == code)
    ).scalar_one_or_none()


# ── 因子创建 ──────────────────────────────────────────────


def create_factor_draft(
    db: Session,
    *,
    draft: FactorDraftCreate,
) -> Factor:
    """创建因子草稿。

    约束：
    - lifecycle_status = 'draft'
    - origin = 'user'
    - status = 'draft'（legacy 兼容字段）
    - is_active = 0
    - code 必须唯一
    """
    existing = get_factor_by_code(db, draft.code)
    if existing is not None:
        raise ValueError(f"factor_code_conflict:{draft.code}")

    factor = Factor(
        code=draft.code,
        name=draft.name,
        category=draft.category,
        direction=draft.direction,
        status="draft",  # legacy compat
        is_active=0,
        default_missing_policy=draft.default_missing_policy,
        description=draft.description,
        formula_expr=None,
        frequency=draft.frequency,
        # WP1 lifecycle fields
        origin="user",
        lifecycle_status="draft",
        owner=draft.owner,
        thesis=draft.thesis,
        factor_kind=draft.factor_kind,
        asset_scope_json=_dumps_or_none(draft.asset_scope),
        risk_level=draft.risk_level,
    )
    db.add(factor)
    db.flush()
    return factor


# ── 因子版本查询 ──────────────────────────────────────────


def list_factor_versions(db: Session, factor_id: int) -> list[FactorVersion]:
    """列出因子的全部版本，按 version 降序。"""
    return list(
        db.execute(
            select(FactorVersion)
            .where(FactorVersion.factor_id == factor_id)
            .order_by(FactorVersion.version.desc())
        ).scalars().all()
    )


def get_factor_version(db: Session, version_id: int) -> FactorVersion | None:
    """按主键获取因子版本。"""
    return db.get(FactorVersion, version_id)


def get_latest_version(db: Session, factor_id: int) -> FactorVersion | None:
    """获取因子的当前最新版本（is_latest=1）。"""
    return db.execute(
        select(FactorVersion)
        .where(
            FactorVersion.factor_id == factor_id,
            FactorVersion.is_latest == 1,
        )
        .limit(1)
    ).scalar_one_or_none()


# ── 因子版本创建 ──────────────────────────────────────────


def create_factor_version(
    db: Session,
    *,
    factor_id: int,
    request: FactorVersionCreate,
) -> FactorVersion:
    """创建因子版本。

    流程：
    1. 校验因子存在
    2. 编译公式（WP2-04：生成执行计划、content_hash、依赖收集）
    3. 幂等检查（同内容返回既有版本）
    4. 检查上一最新版本是否被引用（不可变），若不可变则阻止切换 is_latest
    5. 新建版本，version = max + 1，is_latest = 1
    6. 旧最新版本 is_latest 置 0

    WP2 集成：
    - 编译成功：填充 formula_ast_json、data_dependencies_json、compiler_version、
      execution_plan_hash、complexity_score，validation_status="valid"
    - 编译失败：validation_status="invalid"，validation_errors_json 记录错误，
      execution_plan_hash 仍用基础 content_hash 保证幂等
    """
    factor = get_factor(db, factor_id)
    if factor is None:
        raise ValueError("factor_not_found")

    # WP2-04: 编译公式
    from app.services.factors.factor_compiler import compile_formula, COMPILER_VERSION

    compile_result = compile_formula(
        formula=request.formula_expr,
        params=request.params,
        direction=request.direction,
        postprocess=request.postprocess,
        strict_fields=False,  # 版本创建允许未知字段（可能是参数引用）
    )

    # 计算 content_hash（编译成功用执行计划哈希，失败用基础哈希）
    if compile_result.is_valid and compile_result.execution_plan:
        new_content_hash = compile_result.execution_plan.content_hash()
        formula_ast_json = _dumps_or_none(compile_result.execution_plan.formula_ast)
        data_dependencies_json = _dumps_or_none(
            compile_result.execution_plan.data_dependencies
        )
        compiler_version = COMPILER_VERSION
        complexity_score = compile_result.execution_plan.complexity_score
        validation_status = "valid"
        validation_errors_json = None
    else:
        # 编译失败：仍用基础 content_hash 保证幂等
        new_content_hash = compute_factor_content_hash(
            formula=request.formula_expr,
            params=request.params,
            source_mapping=request.source_mapping,
            direction=request.direction,
        )
        formula_ast_json = None
        data_dependencies_json = None
        compiler_version = None
        complexity_score = None
        validation_status = "invalid"
        validation_errors_json = _dumps_or_none(
            [e.to_dict() for e in compile_result.errors]
        )

    # 幂等：若任意既有版本内容哈希一致，直接返回该版本
    # 优先比较 execution_plan_hash（WP2 编译生成的哈希），
    # 对旧版本（无 execution_plan_hash）回退到基础 content_hash
    existing_versions = list_factor_versions(db, factor_id)
    for existing in existing_versions:
        if existing.execution_plan_hash and existing.execution_plan_hash == new_content_hash:
            return existing
        existing_params = _loads(existing.params_json, {})
        existing_source_mapping = _loads(existing.source_mapping_json, {})
        existing_hash = compute_factor_content_hash(
            formula=existing.formula_expr,
            params=existing_params,
            source_mapping=existing_source_mapping,
            direction=existing.direction,
        )
        if existing_hash == new_content_hash:
            return existing

    # 找到上一最新版本
    previous_latest: FactorVersion | None = None
    for v in existing_versions:
        if v.is_latest == 1:
            previous_latest = v
            break

    # 不可变检查：若上一最新版本被引用，禁止 demote
    if previous_latest is not None and check_version_immutable(
        db, previous_latest.id
    ):
        raise ValueError(
            f"version_immutable:{previous_latest.id}"
        )

    next_version_no = (
        max((v.version for v in existing_versions), default=0) + 1
    )

    new_version = FactorVersion(
        factor_id=factor_id,
        version=next_version_no,
        formula_expr=request.formula_expr,
        params_json=_dumps(request.params) or "{}",
        direction=request.direction,
        source_mapping_json=_dumps(request.source_mapping) or "{}",
        change_note=request.change_note or "",
        is_latest=1,
        postprocess_json=_dumps_or_none(request.postprocess),
        parameter_schema_json=_dumps_or_none(request.parameter_schema),
        created_by=request.created_by,
        created_via=request.created_via,
        # WP2 编译字段
        formula_ast_json=formula_ast_json,
        data_dependencies_json=data_dependencies_json,
        compiler_version=compiler_version,
        execution_plan_hash=new_content_hash,
        complexity_score=complexity_score,
        validation_status=validation_status,
        validation_errors_json=validation_errors_json,
    )
    db.add(new_version)

    if previous_latest is not None:
        previous_latest.is_latest = 0

    db.flush()
    return new_version


# ── WP4-01: 数值指标提升 ─────────────────────────────────


def promote_factor_from_indicator(
    db: Session,
    *,
    indicator_id: int,
    request: Any,  # CustomIndicatorPromoteRequest，避免循环导入用 Any
    indicator_row: Any = None,  # CustomIndicator，可选预取
    indicator_version: int | None = None,  # 指标当前版本号
) -> tuple[Factor, FactorVersion, dict[str, Any]]:
    """将 value_type=number 的自定义指标提升为因子草稿（WP4-01）。

    约束：
    - indicator.value_type 必须为 'number'，否则抛 ValueError('indicator_not_number')
    - 创建 Factor 草稿（lifecycle_status='draft', origin='user'）
    - 创建 FactorVersion，formula_expr 来自 indicator.formula
    - source_mapping 保存溯源：{indicator_id, indicator_key, indicator_version, promoted_at}
    - params 从 list 转换为 dict（指标 params 是 list[dict]，因子 params 是 dict）
    - 幂等：若已存在同 code 的因子且同 execution_plan_hash 版本，直接返回既有

    返回：(Factor, FactorVersion, source_mapping)
    """
    from app.models.custom_indicator import CustomIndicator  # 延迟导入避免循环

    if indicator_row is None:
        indicator_row = db.get(CustomIndicator, indicator_id)
    if indicator_row is None:
        raise ValueError("indicator_not_found")

    if indicator_row.value_type != "number":
        raise ValueError("indicator_not_number")

    # 字段映射
    code = request.code or str(indicator_row.key or "").lower().replace("-", "_")
    if not code or not code[0].isalpha():
        raise ValueError("invalid_factor_code")
    name = request.name or indicator_row.name
    category = request.category or indicator_row.category or "custom"

    # params 转换：指标 list[dict] → 因子 dict
    raw_params: Any = []
    try:
        raw_params = json.loads(indicator_row.params_json or "[]")
    except (json.JSONDecodeError, TypeError):
        raw_params = []
    params: dict[str, Any] = {}
    if isinstance(raw_params, list):
        for i, p in enumerate(raw_params):
            if isinstance(p, dict) and "name" in p and "value" in p:
                params[str(p["name"])] = p["value"]
            elif isinstance(p, dict):
                params[f"param_{i}"] = p
    elif isinstance(raw_params, dict):
        params = raw_params

    # 幂等检查：若同 code 因子已存在，且同 execution_plan_hash 版本存在，直接返回
    # 注意：execution_plan_hash 由 factor_compiler 生成（不含 source_mapping），
    # 因此这里必须用同一路径计算哈希才能与既有版本匹配。
    existing_factor = get_factor_by_code(db, code)
    if existing_factor is not None:
        new_hash: str | None = None
        try:
            from app.services.factors.factor_compiler import compile_formula
            compile_res = compile_formula(
                formula=indicator_row.formula or "",
                params=params,
                direction=request.direction,
                strict_fields=False,
            )
            if compile_res.is_valid and compile_res.execution_plan is not None:
                new_hash = compile_res.execution_plan.content_hash()
        except Exception:
            new_hash = None
        if new_hash is None:
            # 编译失败时回退到基础 content_hash（不含 source_mapping，保持稳定）
            from app.services.factors.baseline_freeze import compute_factor_content_hash
            new_hash = compute_factor_content_hash(
                formula=indicator_row.formula or "",
                params=params,
                source_mapping={},  # 不含溯源字段，保证哈希稳定
                direction=request.direction,
            )
        for ver in list_factor_versions(db, existing_factor.id):
            if ver.execution_plan_hash and ver.execution_plan_hash == new_hash:
                source_mapping = _build_indicator_source_mapping(
                    indicator_id, indicator_row.key, indicator_version
                )
                return existing_factor, ver, source_mapping
        # 同 code 但公式不同：在已有因子上创建新版本
        factor = existing_factor
    else:
        # 创建新因子草稿
        draft = FactorDraftCreate(
            code=code,
            name=name,
            category=category,
            direction=request.direction,
            factor_kind=request.factor_kind,
            description=request.description or indicator_row.description,
            thesis=request.thesis,
            risk_level=request.risk_level,
            asset_scope=["cn-stock"],
            default_missing_policy="exclude",
        )
        factor = create_factor_draft(db, draft=draft)
        db.flush()

    # 构造 source_mapping（溯源信息）
    source_mapping = _build_indicator_source_mapping(
        indicator_id, indicator_row.key, indicator_version
    )

    # 创建因子版本
    version_request = FactorVersionCreate(
        formula_expr=indicator_row.formula or "",
        params=params,
        direction=request.direction,
        source_mapping=source_mapping,
        change_note=request.change_note or "promoted from custom indicator",
        created_by=request.created_by,
        created_via="manual",
    )
    version = create_factor_version(db, factor_id=factor.id, request=version_request)
    db.flush()
    return factor, version, source_mapping


def _build_indicator_source_mapping(
    indicator_id: int,
    indicator_key: str | None,
    indicator_version: int | None,
) -> dict[str, Any]:
    """构造指标→因子的来源映射，用于审计溯源。"""
    from datetime import datetime, timezone
    return {
        "source_type": "custom_indicator",
        "indicator_id": indicator_id,
        "indicator_key": indicator_key,
        "indicator_version": indicator_version,
        "promoted_at": datetime.now(timezone.utc).isoformat(),
    }


# ── 引用与不可变 ──────────────────────────────────────────


def get_factor_references(
    db: Session,
    factor_id: int,
    version_id: int,
) -> FactorReferenceRead:
    """统计因子版本被评估/因子集/模型运行引用的数量。"""
    version = get_factor_version(db, version_id)
    if version is None:
        raise ValueError("version_not_found")
    factor = get_factor(db, factor_id)
    if factor is None:
        raise ValueError("factor_not_found")

    eval_count = int(
        db.execute(
            select(func.count())
            .select_from(EvaluationRun)
            .where(EvaluationRun.factor_version_id == version_id)
        ).scalar_one()
    )

    factor_set_count = int(
        db.execute(
            select(func.count())
            .select_from(FactorSetMember)
            .where(FactorSetMember.factor_version_id == version_id)
        ).scalar_one()
    )

    # FactorWeightSnapshot 通过 factor_code + factor_version 引用
    model_run_count = int(
        db.execute(
            select(func.count())
            .select_from(FactorWeightSnapshot)
            .where(
                FactorWeightSnapshot.factor_code == factor.code,
                FactorWeightSnapshot.factor_version == version.version,
            )
        ).scalar_one()
    )

    is_immutable = (eval_count + factor_set_count + model_run_count) > 0

    return FactorReferenceRead(
        factor_code=factor.code,
        factor_version=version.version,
        referenced_by_evaluations=eval_count,
        referenced_by_factor_sets=factor_set_count,
        referenced_by_model_runs=model_run_count,
        is_immutable=is_immutable,
    )


def check_version_immutable(db: Session, version_id: int) -> bool:
    """判断因子版本是否已被引用（不可变）。

    引用来源：
    - EvaluationRun.factor_version_id
    - FactorSetMember.factor_version_id
    - FactorWeightSnapshot(factor_code, factor_version)
    """
    version = get_factor_version(db, version_id)
    if version is None:
        return False

    eval_count = int(
        db.execute(
            select(func.count())
            .select_from(EvaluationRun)
            .where(EvaluationRun.factor_version_id == version_id)
        ).scalar_one()
    )
    if eval_count > 0:
        return True

    factor_set_count = int(
        db.execute(
            select(func.count())
            .select_from(FactorSetMember)
            .where(FactorSetMember.factor_version_id == version_id)
        ).scalar_one()
    )
    if factor_set_count > 0:
        return True

    factor = get_factor(db, version.factor_id)
    if factor is None:
        return False

    model_run_count = int(
        db.execute(
            select(func.count())
            .select_from(FactorWeightSnapshot)
            .where(
                FactorWeightSnapshot.factor_code == factor.code,
                FactorWeightSnapshot.factor_version == version.version,
            )
        ).scalar_one()
    )
    return model_run_count > 0


# ── 内部工具 ──────────────────────────────────────────────


def _loads(value: str | None, fallback: Any) -> Any:
    """安全 JSON 反序列化。"""
    if value is None or value == "":
        return fallback
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return fallback


__all__ = [
    "list_factors",
    "get_factor",
    "get_factor_by_code",
    "create_factor_draft",
    "list_factor_versions",
    "get_factor_version",
    "get_latest_version",
    "create_factor_version",
    "get_factor_references",
    "check_version_immutable",
]
