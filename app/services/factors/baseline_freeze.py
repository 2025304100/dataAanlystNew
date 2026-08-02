"""因子基线冻结（WP0）。

为 8 个系统因子、API 契约和计算结果对账样本提供可重复生成的基线快照，
确保后续 WP1~WP8 开发过程中：
- 8 系统因子的 ID、版本、公式、方向、依赖不发生意外漂移
- manual/shadow/ridge API 契约的响应字段不意外变更
- 旧计算结果可对账验证（固定 data_cutoff_at、标的集、因子值和 Score）

对齐 docs/专业因子库开发计划.md §WP0 基线冻结与开发护栏。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import select

from app.services.factors.definitions import FACTOR_DEFINITIONS, _PARAMS, _SOURCE_MAPPINGS


# ── 工具函数 ──────────────────────────────────────────────


def _json_safe(obj: Any) -> Any:
    """递归将 date/datetime 转为 ISO 字符串，确保 JSON 可序列化。"""
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(item) for item in obj]
    return obj


def _utcnow_iso() -> str:
    """当前 UTC 时间 ISO 字符串。"""
    return datetime.now(timezone.utc).isoformat()


def _sha256_16(payload: str) -> str:
    """SHA256 哈希前 16 位。"""
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# ── Part 1 (WP0-01): 因子基线快照 ─────────────────────────


@dataclass(frozen=True)
class FactorBaselineEntry:
    """单个因子的基线条目。"""

    code: str
    name: str
    category: str
    direction: str
    formula: str
    version: int
    missing_policy: str
    frequency: str
    model_enabled: bool
    health_required: bool
    source_mapping: dict[str, Any]
    params: dict[str, Any]
    # 数据库实际记录（运行时填充）
    db_id: int | None = None
    db_version_id: int | None = None
    db_status: str | None = None
    db_is_active: int | None = None
    db_source_type: str | None = None
    # 内容哈希（基于公式+参数+依赖+方向）
    content_hash: str = ""


@dataclass(frozen=True)
class FactorBaselineSnapshot:
    """8 系统因子基线快照。"""

    generated_at: str
    factor_count: int
    factors: list[FactorBaselineEntry]
    baseline_hash: str  # 基于全部因子 content_hash 的聚合哈希
    db_consistent: bool  # 数据库记录与 FACTOR_DEFINITIONS 是否一致
    inconsistencies: list[str]  # 不一致列表

    def to_dict(self) -> dict[str, Any]:
        return _json_safe(asdict(self))

    def get_factor(self, code: str) -> FactorBaselineEntry | None:
        for factor in self.factors:
            if factor.code == code:
                return factor
        return None


def compute_factor_content_hash(
    *, formula: str, params: dict, source_mapping: dict, direction: str
) -> str:
    """计算因子版本的内容哈希（SHA256 前 16 位）。

    哈希输入：canonical JSON of {formula, params, source_mapping, direction}
    canonical JSON = json.dumps(..., sort_keys=True, ensure_ascii=False)
    """
    payload = {
        "formula": formula,
        "params": params,
        "source_mapping": source_mapping,
        "direction": direction,
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return _sha256_16(canonical)


def freeze_factor_baseline(db_session) -> FactorBaselineSnapshot:
    """从 FACTOR_DEFINITIONS 和数据库生成因子基线快照。

    流程：
    1. 遍历 FACTOR_DEFINITIONS（8 个）
    2. 对每个因子计算 content_hash
    3. 查询数据库 Factor 和 FactorVersions 表
    4. 比对数据库记录与定义的一致性
    5. 生成聚合 baseline_hash
    """
    from app.models.factor import Factor
    from app.models.factor_model import FactorVersion

    entries: list[FactorBaselineEntry] = []
    inconsistencies: list[str] = []

    for definition in FACTOR_DEFINITIONS:
        source_mapping = _SOURCE_MAPPINGS.get(definition.code, {})
        params = _PARAMS.get(definition.code, {})
        content_hash = compute_factor_content_hash(
            formula=definition.formula,
            params=params,
            source_mapping=source_mapping,
            direction=definition.direction,
        )

        db_id: int | None = None
        db_version_id: int | None = None
        db_status: str | None = None
        db_is_active: int | None = None
        db_source_type: str | None = None

        try:
            factor_row = db_session.execute(
                select(Factor).where(Factor.code == definition.code)
            ).scalars().first()

            if factor_row is None:
                inconsistencies.append(
                    f"missing_factor_in_db:{definition.code}"
                )
            else:
                db_id = factor_row.id
                db_status = factor_row.status
                db_is_active = factor_row.is_active
                db_source_type = factor_row.source_type

                if factor_row.formula_expr != definition.formula:
                    inconsistencies.append(
                        f"formula_mismatch:{definition.code}:"
                        f"db={factor_row.formula_expr!r} "
                        f"vs def={definition.formula!r}"
                    )
                if factor_row.direction != definition.direction:
                    inconsistencies.append(
                        f"direction_mismatch:{definition.code}:"
                        f"db={factor_row.direction!r} "
                        f"vs def={definition.direction!r}"
                    )
                if factor_row.status != "active":
                    inconsistencies.append(
                        f"status_not_active:{definition.code}:"
                        f"db={factor_row.status!r}"
                    )
                if factor_row.is_active != 1:
                    inconsistencies.append(
                        f"is_active_not_1:{definition.code}:"
                        f"db={factor_row.is_active!r}"
                    )

                versions = db_session.execute(
                    select(FactorVersion)
                    .where(FactorVersion.factor_id == factor_row.id)
                    .order_by(FactorVersion.version)
                ).scalars().all()

                if not versions:
                    inconsistencies.append(
                        f"no_versions_in_db:{definition.code}"
                    )
                else:
                    latest = max(versions, key=lambda v: v.version)
                    db_version_id = latest.id
                    if latest.version != definition.version:
                        inconsistencies.append(
                            f"version_mismatch:{definition.code}:"
                            f"db_latest={latest.version} "
                            f"vs def={definition.version}"
                        )
        except Exception as exc:
            inconsistencies.append(
                f"db_query_error:{definition.code}:{exc}"
            )

        entries.append(
            FactorBaselineEntry(
                code=definition.code,
                name=definition.name,
                category=definition.category,
                direction=definition.direction,
                formula=definition.formula,
                version=definition.version,
                missing_policy=definition.missing_policy,
                frequency=definition.frequency,
                model_enabled=definition.model_enabled,
                health_required=definition.health_required,
                source_mapping=source_mapping,
                params=params,
                db_id=db_id,
                db_version_id=db_version_id,
                db_status=db_status,
                db_is_active=db_is_active,
                db_source_type=db_source_type,
                content_hash=content_hash,
            )
        )

    all_hashes = "".join(entry.content_hash for entry in entries)
    baseline_hash = _sha256_16(all_hashes)

    return FactorBaselineSnapshot(
        generated_at=_utcnow_iso(),
        factor_count=len(entries),
        factors=entries,
        baseline_hash=baseline_hash,
        db_consistent=len(inconsistencies) == 0,
        inconsistencies=inconsistencies,
    )


# ── Part 2 (WP0-02): API 契约冻结 ─────────────────────────


@dataclass(frozen=True)
class ApiContractEntry:
    """单个 API 端点的契约定义。"""

    method: str  # GET/POST/PATCH
    path: str  # 如 "/factors/overview"
    description: str
    required_fields: list[str]  # 响应必须包含的字段
    optional_fields: list[str]  # 可选字段
    status_codes: list[int]  # 可能的 HTTP 状态码


@dataclass(frozen=True)
class ApiContractSnapshot:
    """API 契约基线快照。"""

    generated_at: str
    endpoint_count: int
    contracts: list[ApiContractEntry]
    manual_mode_fields: dict[str, list[str]]  # manual 模式下各端点的字段快照

    def to_dict(self) -> dict[str, Any]:
        return _json_safe(asdict(self))


# 定义 24 个端点的契约（基于现有 API 路由）
API_CONTRACTS: list[ApiContractEntry] = [
    # factors.py (13 endpoints)
    ApiContractEntry(
        "GET", "/factors/overview", "因子总览",
        required_fields=["runtime", "config", "feature_enabled", "health"],
        optional_fields=["warehouse_error", "latest_trade_date", "factor_coverage"],
        status_codes=[200],
    ),
    ApiContractEntry(
        "GET", "/factors/config", "因子系统配置",
        required_fields=["feature_enabled", "warehouse_path", "updated_by", "updated_at"],
        optional_fields=[],
        status_codes=[200],
    ),
    ApiContractEntry(
        "PATCH", "/factors/config", "更新配置",
        required_fields=["feature_enabled", "warehouse_path", "updated_by", "updated_at"],
        optional_fields=[],
        status_codes=[200, 409],
    ),
    ApiContractEntry(
        "POST", "/factors/warehouse/initialize", "初始化仓库",
        required_fields=["runtime", "config", "feature_enabled"],
        optional_fields=["health", "warehouse_error"],
        status_codes=[200, 409],
    ),
    ApiContractEntry(
        "GET", "/factors", "因子列表",
        required_fields=["code", "name", "category", "direction", "status"],
        optional_fields=["source_type", "frequency", "default_missing_policy", "is_active", "description", "formula_expr"],
        status_codes=[200],
    ),
    ApiContractEntry(
        "GET", "/factors/{factor_code}/versions", "因子版本列表",
        required_fields=["factor_code", "version", "formula_expr", "direction"],
        optional_fields=["params", "source_mapping", "effective_from", "change_note", "is_latest", "created_at"],
        status_codes=[200, 404],
    ),
    ApiContractEntry(
        "GET", "/factors/symbols/{symbol_id}/explanation", "标的因子解释",
        required_fields=["symbol_id", "symbol", "name", "trade_date", "weight_mode", "explanation"],
        optional_fields=["model_run_id", "factor_data_cutoff_at", "factor_quality_score", "factor_timing_score", "model_alpha_score", "macro_regime", "macro_position_multiplier"],
        status_codes=[200, 404],
    ),
    ApiContractEntry(
        "GET", "/factors/readiness", "因子 readiness 报告",
        required_fields=["generated_at", "warehouse_available", "complete_trade_day", "factors", "summary"],
        optional_fields=["warehouse_path", "complete_trade_day_evidence", "source_table_stats"],
        status_codes=[200, 503],
    ),
    ApiContractEntry(
        "GET", "/factors/{factor_code}/readiness", "单因子 readiness",
        required_fields=["factor_code", "factor_name", "status", "layer", "coverage"],
        optional_fields=["latest_factor_date", "latest_source_date", "universe_symbols", "eligible_symbols", "evidence", "blocking_reasons", "recommended_action", "complete_trade_day_evidence", "data_source_policy"],
        status_codes=[200, 404, 503],
    ),
    ApiContractEntry(
        "GET", "/factors/batches", "批次列表",
        required_fields=["warehouse_available", "batches"],
        optional_fields=["warehouse_path", "error"],
        status_codes=[200, 503],
    ),
    ApiContractEntry(
        "GET", "/factors/batches/audit", "批次审计报告",
        required_fields=["generated_at", "batches", "lag_diagnostic"],
        optional_fields=[],
        status_codes=[200, 503],
    ),
    ApiContractEntry(
        "GET", "/factors/batches/{batch_id}", "批次详情",
        required_fields=["batch_id", "source_key", "status", "atomicity_evidence"],
        optional_fields=["rows_received", "rows_written", "started_at", "finished_at", "error_json", "actual_rows_in_table", "table_name"],
        status_codes=[200, 404, 503],
    ),
    ApiContractEntry(
        "GET", "/factors/data-source-roadmap", "数据源路线",
        required_fields=["generated_at", "policies", "source_states", "factor_to_source_map", "summary"],
        optional_fields=["valuation_completion_start_date"],
        status_codes=[200, 503],
    ),
    # factor_pipeline.py (5 endpoints)
    ApiContractEntry(
        "GET", "/factor-pipeline/eta", "流水线 ETA",
        required_fields=[],
        optional_fields=["recommended_seconds", "sample_count"],
        status_codes=[200],
    ),
    ApiContractEntry(
        "POST", "/factor-pipeline/tasks", "创建流水线任务",
        required_fields=["id", "task_type", "status", "stage", "percent"],
        optional_fields=["message", "total", "processed", "payload_json", "created_at"],
        status_codes=[200, 409, 422],
    ),
    ApiContractEntry(
        "GET", "/factor-pipeline/tasks", "任务列表",
        required_fields=[],
        optional_fields=["id", "task_type", "status"],
        status_codes=[200],
    ),
    ApiContractEntry(
        "GET", "/factor-pipeline/tasks/{task_id}", "任务详情",
        required_fields=["id", "task_type", "status", "stage", "percent"],
        optional_fields=["message", "total", "processed", "result_json", "errors_json", "created_at", "started_at", "finished_at"],
        status_codes=[200, 404],
    ),
    ApiContractEntry(
        "POST", "/factor-pipeline/tasks/{task_id}/cancel", "取消任务",
        required_fields=["id", "task_type", "status"],
        optional_fields=["stage", "percent", "message", "finished_at"],
        status_codes=[200, 404],
    ),
    # factor_models.py (6 endpoints)
    ApiContractEntry(
        "GET", "/factor-models/runtime", "模型运行时",
        required_fields=["weight_mode", "active_model_run_id", "updated_by", "version", "updated_at", "score_weight_mode"],
        optional_fields=["fallback_reason"],
        status_codes=[200],
    ),
    ApiContractEntry(
        "GET", "/factor-models/latest", "最新模型",
        required_fields=["id", "model_type", "status"],
        optional_fields=["asset_type", "target_code", "train_start_date", "train_end_date", "data_cutoff_at", "metrics", "sample_count", "rejection_reason", "created_at", "weights"],
        status_codes=[200, 404],
    ),
    ApiContractEntry(
        "GET", "/factor-models", "模型列表",
        required_fields=["runtime", "items"],
        optional_fields=[],
        status_codes=[200],
    ),
    ApiContractEntry(
        "GET", "/factor-models/{model_run_id}", "模型详情",
        required_fields=["id", "model_type", "status"],
        optional_fields=["asset_type", "target_code", "train_start_date", "train_end_date", "data_cutoff_at", "feature_versions", "hyperparameters", "metrics", "sample_count", "symbol_count", "trade_date_count", "rejection_reason", "artifact_path", "created_at", "activated_at", "weights", "audit"],
        status_codes=[200, 404],
    ),
    ApiContractEntry(
        "POST", "/factor-models/{model_run_id}/activate", "激活模型",
        required_fields=["weight_mode", "active_model_run_id", "version"],
        optional_fields=["updated_by", "fallback_reason", "updated_at", "score_weight_mode"],
        status_codes=[200, 400],
    ),
    ApiContractEntry(
        "POST", "/factor-models/fallback", "回退模型",
        required_fields=["weight_mode", "active_model_run_id", "version"],
        optional_fields=["updated_by", "fallback_reason", "updated_at", "score_weight_mode"],
        status_codes=[200, 400],
    ),
]


def freeze_api_contract() -> ApiContractSnapshot:
    """生成 API 契约基线快照（静态定义，不调用实际 API）。"""
    manual_mode_fields: dict[str, list[str]] = {}
    for contract in API_CONTRACTS:
        key = f"{contract.method} {contract.path}"
        manual_mode_fields[key] = list(contract.required_fields) + list(
            contract.optional_fields
        )

    return ApiContractSnapshot(
        generated_at=_utcnow_iso(),
        endpoint_count=len(API_CONTRACTS),
        contracts=list(API_CONTRACTS),
        manual_mode_fields=manual_mode_fields,
    )


def _match_path(template: str, actual: str) -> bool:
    """将实际路径与模板路径匹配（{param} 段为通配）。"""
    template_segs = [s for s in template.strip("/").split("/") if s]
    actual_segs = [s for s in actual.strip("/").split("/") if s]
    if len(template_segs) != len(actual_segs):
        return False
    for t, a in zip(template_segs, actual_segs):
        if t.startswith("{") and t.endswith("}"):
            continue
        if t != a:
            return False
    return True


def _find_contract(path: str) -> ApiContractEntry | None:
    """按路径查找 API 契约定义。"""
    clean_path = path.split("?")[0]
    for contract in API_CONTRACTS:
        if _match_path(contract.path, clean_path):
            return contract
    return None


def validate_api_response_fields(
    path: str, response_data: dict | list, *, status_code: int = 200
) -> list[str]:
    """验证 API 响应是否符合契约。

    返回违规字段列表（空列表表示通过）。
    对于列表响应，检查第一个元素的字段。
    """
    contract = _find_contract(path)
    if contract is None:
        return [f"unknown_endpoint:{path}"]

    violations: list[str] = []

    if status_code not in contract.status_codes:
        violations.append(
            f"unexpected_status:{path}:{status_code} not in {contract.status_codes}"
        )

    # 非 2xx 响应只检查状态码，不检查字段
    if not (200 <= status_code < 300):
        return violations

    data = response_data
    if isinstance(response_data, list):
        if not response_data:
            return violations
        data = response_data[0]
        if not isinstance(data, dict):
            return [f"list_element_not_dict:{path}"]

    if not isinstance(data, dict):
        return [f"response_not_dict:{path}"]

    for field_name in contract.required_fields:
        if field_name not in data:
            violations.append(f"missing_required_field:{path}:{field_name}")

    return violations


# ── Part 3 (WP0-03): 计算结果对账样本 ─────────────────────


@dataclass(frozen=True)
class ReconciliationSampleEntry:
    """对账样本单条记录。"""

    symbol_id: int
    symbol: str
    trade_date: str
    factor_code: str
    raw_value: float | None
    normalized_value: float | None
    calc_batch_id: str | None


@dataclass(frozen=True)
class ScoreSampleEntry:
    """Score 对账样本单条记录。"""

    symbol_id: int
    symbol: str
    trade_date: str
    quality_score: float | None
    timing_score: float | None
    priority_score: float | None
    weight_mode: str | None
    calc_batch_id: str | None


@dataclass(frozen=True)
class ReconciliationSample:
    """计算结果对账样本。"""

    generated_at: str
    data_cutoff_at: str
    trade_date: str
    symbol_count: int
    factor_value_count: int
    score_count: int
    factor_values: list[ReconciliationSampleEntry]
    scores: list[ScoreSampleEntry]
    sample_hash: str  # 基于样本内容的哈希

    def to_dict(self) -> dict[str, Any]:
        return _json_safe(asdict(self))


# 固定对账基准（从 WPD-02 完整交易日 + WPD-01 审计基线导出）
RECONCILIATION_BASE_DATE = date(2026, 7, 24)  # WPD-02 验证的完整交易日
RECONCILIATION_SYMBOL_LIMIT = 20  # 固定取 20 个标的作为对账样本


def build_reconciliation_sample(
    db_session,
    *,
    trade_date: date | None = None,
    symbol_limit: int = RECONCILIATION_SYMBOL_LIMIT,
) -> ReconciliationSample:
    """构建计算结果对账样本。

    流程：
    1. 选择固定的完整交易日（默认 2026-07-24）
    2. 选择前 N 个有因子值的标的
    3. 导出 factor_values 样本
    4. 导出 Score 样本
    5. 计算样本哈希
    """
    from app.models.factor import Factor, FactorValue
    from app.models.score import Score
    from app.models.symbol import Symbol

    target_date = trade_date or RECONCILIATION_BASE_DATE

    factor_values: list[ReconciliationSampleEntry] = []
    scores: list[ScoreSampleEntry] = []
    symbol_count = 0
    sample_hash = ""

    try:
        # 1. 选择前 N 个有因子值的标的
        symbol_ids = list(
            db_session.execute(
                select(FactorValue.symbol_id)
                .where(FactorValue.trade_date == target_date)
                .distinct()
                .order_by(FactorValue.symbol_id)
                .limit(symbol_limit)
            ).scalars().all()
        )
        symbol_count = len(symbol_ids)

        if symbol_ids:
            # 2. 导出 factor_values 样本（join Factor + Symbol 获取 code/symbol）
            fv_rows = db_session.execute(
                select(FactorValue, Factor.code, Symbol.symbol)
                .join(Factor, FactorValue.factor_id == Factor.id)
                .join(Symbol, FactorValue.symbol_id == Symbol.id)
                .where(
                    FactorValue.trade_date == target_date,
                    FactorValue.symbol_id.in_(symbol_ids),
                )
                .order_by(FactorValue.symbol_id, Factor.code)
            ).all()

            for fv, factor_code, symbol in fv_rows:
                factor_values.append(
                    ReconciliationSampleEntry(
                        symbol_id=fv.symbol_id,
                        symbol=symbol,
                        trade_date=target_date.isoformat(),
                        factor_code=factor_code,
                        raw_value=fv.raw_value,
                        normalized_value=fv.normalized_value,
                        calc_batch_id=fv.calc_batch_id,
                    )
                )

            # 3. 导出 Score 样本（join Symbol 获取 symbol）
            score_rows = db_session.execute(
                select(Score, Symbol.symbol)
                .join(Symbol, Score.symbol_id == Symbol.id)
                .where(
                    Score.trade_date == target_date,
                    Score.symbol_id.in_(symbol_ids),
                )
                .order_by(Score.symbol_id)
            ).all()

            for sc, symbol in score_rows:
                scores.append(
                    ScoreSampleEntry(
                        symbol_id=sc.symbol_id,
                        symbol=symbol,
                        trade_date=target_date.isoformat(),
                        quality_score=sc.quality_score,
                        timing_score=sc.timing_score,
                        priority_score=sc.priority_score,
                        weight_mode=sc.weight_mode,
                        calc_batch_id=sc.calc_batch_id,
                    )
                )

            # 4. 计算样本哈希
            payload = {
                "trade_date": target_date.isoformat(),
                "factor_values": [asdict(e) for e in factor_values],
                "scores": [asdict(e) for e in scores],
            }
            canonical = json.dumps(
                payload, sort_keys=True, ensure_ascii=False, default=str
            )
            sample_hash = _sha256_16(canonical)
    except Exception:
        pass

    return ReconciliationSample(
        generated_at=_utcnow_iso(),
        data_cutoff_at=target_date.isoformat(),
        trade_date=target_date.isoformat(),
        symbol_count=symbol_count,
        factor_value_count=len(factor_values),
        score_count=len(scores),
        factor_values=factor_values,
        scores=scores,
        sample_hash=sample_hash,
    )


# ── Part 4: 统一基线冻结报告 ──────────────────────────────


@dataclass(frozen=True)
class BaselineFreezeReport:
    """WP0 基线冻结完整报告。"""

    generated_at: str
    factor_baseline: FactorBaselineSnapshot
    api_contract: ApiContractSnapshot
    reconciliation_sample: ReconciliationSample | None  # 可能无数据
    runtime_state: dict[str, Any]  # weight_mode, active_model_run_id, feature_enabled
    repo_head_revision: str | None  # Alembic head

    def to_dict(self) -> dict[str, Any]:
        return _json_safe(
            {
                "generated_at": self.generated_at,
                "factor_baseline": self.factor_baseline.to_dict(),
                "api_contract": self.api_contract.to_dict(),
                "reconciliation_sample": (
                    self.reconciliation_sample.to_dict()
                    if self.reconciliation_sample is not None
                    else None
                ),
                "runtime_state": self.runtime_state,
                "repo_head_revision": self.repo_head_revision,
            }
        )


def _detect_alembic_head() -> str | None:
    """扫描 alembic/versions/ 目录，找出 head revision（未被任何文件作为 down_revision）。"""
    import re
    from pathlib import Path

    try:
        versions_dir = Path(__file__).resolve().parents[3] / "alembic" / "versions"
        if not versions_dir.is_dir():
            return None

        revisions: dict[str, str] = {}
        down_revisions: set[str] = set()

        pattern_rev = re.compile(
            r'^revision\s*[:=].*?=\s*["\']([^"\']+)["\']', re.MULTILINE
        )
        pattern_down = re.compile(
            r'down_revision\s*[:=].*?=\s*["\']([^"\']+)["\']'
        )

        for py_file in versions_dir.glob("*.py"):
            try:
                content = py_file.read_text(encoding="utf-8")
                rev_match = pattern_rev.search(content)
                down_match = pattern_down.search(content)
                if rev_match:
                    rev = rev_match.group(1)
                    revisions[rev] = py_file.name
                    if down_match:
                        down_revisions.add(down_match.group(1))
            except Exception:
                continue

        heads = [r for r in revisions if r not in down_revisions]
        return heads[0] if heads else None
    except Exception:
        return None


def generate_baseline_freeze_report(db_session) -> BaselineFreezeReport:
    """生成完整的 WP0 基线冻结报告。"""
    # Part 1: 因子基线
    factor_baseline = freeze_factor_baseline(db_session)

    # Part 2: API 契约
    api_contract = freeze_api_contract()

    # Part 3: 对账样本（可能无数据）
    reconciliation_sample: ReconciliationSample | None = None
    try:
        sample = build_reconciliation_sample(db_session)
        if sample.sample_hash:
            reconciliation_sample = sample
    except Exception:
        reconciliation_sample = None

    # 运行时状态
    runtime_state: dict[str, Any] = {}
    try:
        from app.services.factors.config import get_factor_system_config
        from app.services.factors.runtime import get_factor_runtime_snapshot

        runtime = get_factor_runtime_snapshot(db_session)
        config = get_factor_system_config(db_session)
        runtime_state = {
            "weight_mode": runtime.weight_mode,
            "active_model_run_id": runtime.active_model_run_id,
            "score_weight_mode": runtime.score_weight_mode,
            "runtime_version": runtime.version,
            "feature_enabled": config.feature_enabled,
            "warehouse_path": config.warehouse_path,
        }
    except Exception:
        pass

    # Alembic head revision
    repo_head_revision = _detect_alembic_head()

    return BaselineFreezeReport(
        generated_at=_utcnow_iso(),
        factor_baseline=factor_baseline,
        api_contract=api_contract,
        reconciliation_sample=reconciliation_sample,
        runtime_state=runtime_state,
        repo_head_revision=repo_head_revision,
    )


__all__ = [
    "API_CONTRACTS",
    "ApiContractEntry",
    "ApiContractSnapshot",
    "BaselineFreezeReport",
    "RECONCILIATION_BASE_DATE",
    "RECONCILIATION_SYMBOL_LIMIT",
    "ReconciliationSample",
    "ReconciliationSampleEntry",
    "ScoreSampleEntry",
    "compute_factor_content_hash",
    "build_reconciliation_sample",
    "freeze_api_contract",
    "freeze_factor_baseline",
    "generate_baseline_freeze_report",
    "validate_api_response_fields",
]
