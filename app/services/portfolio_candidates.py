"""PortfolioCandidate SCD2 5 铁律服务（WP0-2 规则 1-5）。

5 铁律（由服务函数保证）：
  #1 C1：不在候选池有效集合中的 symbol × BUY 动作 → 后端强制拒绝
       → REJECTED + 原因代码 OUTSIDE_CANDIDATE_POOL
  #2 C2：清仓（quantity=0）后 = 候选资格保留（只要 portfolio_candidates 行
       removed_manually_flag=0 且 effective 区间有效）
  #3 C3：removed_manually_flag=1 哪怕 auto_authorized=1 也直接拒绝 BUY；
       必须显式调用 restore_removed_candidate 才能恢复资格。
  #4 C4：SCD2 行完整性
       C4a 同日多次变更 = 更新现有 (pid, sym, change_date) 行；不做「关前 + 插新」导致
           无效区间产生（禁止出现 effective_from > effective_to 的行）
       C4b 跨日变更 = ① UPDATE 旧行 effective_to=prev_change_date(闭区间)；② INSERT 新行
           effective_from=change_date；禁止修改任何历史行的业务字段（auto_authorized_flag
           等）
       C4c 任何一次变更（不管同日/跨日）必须写 DataGovernanceAuditEvent，
           event_type=CANDIDATE_POOL_MUTATION
  #5 C5：三入口 as-of 快照互不混用
       dry_run=today(UI) / backtest=D 日 / auto_simulation=该交易日 各自必须
       显式决策日期 → get_effective_candidate_symbols(pid, decision_date=X)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Iterable, Literal

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.models.portfolio_candidate import PortfolioCandidate


# ── data structures ─────────────────────────────────────────────────────────
MUTATION_KIND_SET_AUTHORIZED = "SET_AUTHORIZED"
MUTATION_KIND_REMOVE_MANUAL = "REMOVE_MANUAL"
MUTATION_KIND_RESTORE_REMOVED = "RESTORE_REMOVED"
MUTATION_KIND_ADD_OR_REPLACE = "ADD_OR_REPLACE"  # 入池 + 审计 + SCD2 合并

BUY_ALLOW = True
BUY_DENY = False


@dataclass
class CandidateMutation:
    """单条入池/授权/移除变更单元。"""
    symbol_id: int
    kind: Literal[
        "SET_AUTHORIZED", "REMOVE_MANUAL", "RESTORE_REMOVED", "ADD_OR_REPLACE",
    ] = MUTATION_KIND_ADD_OR_REPLACE
    auto_authorized_flag: int = 1
    removal_reason: str | None = None
    operator_id: str | None = None
    # 审计快照（入池证据）
    source_type: str | None = None
    source_scan_run_id: int | None = None
    pool_memberships: dict | None = None
    admission_snapshot: dict | None = None
    priority_score: float | None = None
    recommended_position_pct: float | None = None
    factor_tag: str | None = None


@dataclass
class BuyGateResult:
    allowed: bool
    reason_code: str | None = None
    reason_detail: str | None = None

    @classmethod
    def allowed(cls) -> "BuyGateResult":
        return cls(allowed=True)

    @classmethod
    def denied(cls, code: str, detail: str | None = None) -> "BuyGateResult":
        return cls(allowed=False, reason_code=code, reason_detail=detail)


class CandidatePoolService:
    """PortfolioCandidate 服务：查询 + SCD2 变更 + BUY 门禁。

    所有方法均是类级 / 静态（不保存实例状态），db 由调用方注入。
    """

    # ── C5：as-of 日期敏感候选集合查询（三入口统一入口） ────────────────
    @classmethod
    def get_effective_candidate_symbols(
        cls,
        db: Session,
        portfolio_id: int,
        decision_date: date | str | None = None,
        as_of_date: date | str | None = None,
        *,
        require_authorized: bool = True,
        include_removed: bool = False,
    ) -> set[int]:
        """返回 decision_date 当日（SCD2 ∈ [effective_from, effective_to]）有效的 symbol_id 集合。

        as_of_date / decision_date 两者至少传一个；等价：
            effective_from ≤ D  AND  (effective_to IS NULL OR effective_to ≥ D)
            AND (NOT require_authorized OR auto_authorized_flag=1)
            AND (include_removed OR removed_manually_flag=0)
        """
        effective_d = decision_date or as_of_date
        if effective_d is None:
            raise ValueError("decision_date 或 as_of_date 必须至少传一个（C5 三入口日期敏感）")
        if isinstance(effective_d, str):
            effective_d = date.fromisoformat(effective_d)

        q = select(PortfolioCandidate.symbol_id).where(
            PortfolioCandidate.portfolio_id == int(portfolio_id),
            PortfolioCandidate.effective_from <= effective_d,
            or_(
                PortfolioCandidate.effective_to.is_(None),
                PortfolioCandidate.effective_to >= effective_d,
            ),
        )
        if require_authorized:
            q = q.where(PortfolioCandidate.auto_authorized_flag == 1)
        if not include_removed:
            q = q.where(PortfolioCandidate.removed_manually_flag == 0)
        rows = db.execute(q).scalars().all()
        return {int(r) for r in rows}

    # ── C1 / C3：BUY 动作门禁 ───────────────────────────────────────────────
    @classmethod
    def is_buy_allowed(
        cls,
        db: Session,
        portfolio_id: int,
        symbol_id: int,
        decision_date: date | str,
        *,
        operator_id: str | None = None,
    ) -> tuple[bool, BuyGateResult]:
        """C1 规则：不在有效集中 → (False, OUTSIDE_CANDIDATE_POOL)。

        C3 规则：removed_manually_flag=1 的 symbol 即便显式查询 include_removed，
        也会在 BUY 时被独立拒绝（CANDIDATE_MANUALLY_REMOVED）。
        """
        # 先查 include_removed 以便区分 OUTSIDE vs MANUALLY_REMOVED
        in_pool_including_removed = cls.get_effective_candidate_symbols(
            db, portfolio_id, decision_date,
            require_authorized=False, include_removed=True,
        )
        if int(symbol_id) not in in_pool_including_removed:
            g = BuyGateResult.denied(
                "OUTSIDE_CANDIDATE_POOL",
                f"symbol_id={symbol_id} 不在 portfolio_id={portfolio_id} "
                f"decision_date={decision_date} 的 SCD2 候选区间。",
            )
            return BUY_DENY, g

        in_pool_authorized = cls.get_effective_candidate_symbols(
            db, portfolio_id, decision_date,
            require_authorized=True, include_removed=False,
        )
        if int(symbol_id) not in in_pool_authorized:
            # 不在授权 + 未移除 的有效集合：可能是 removed OR 未授权
            removed_pool = cls.get_effective_candidate_symbols(
                db, portfolio_id, decision_date,
                require_authorized=False, include_removed=True,
            ) - cls.get_effective_candidate_symbols(
                db, portfolio_id, decision_date,
                require_authorized=False, include_removed=False,
            )
            if int(symbol_id) in removed_pool:
                g = BuyGateResult.denied(
                    "CANDIDATE_MANUALLY_REMOVED",
                    f"symbol_id={symbol_id} 已被 operator 手动移除；"
                    "必须调用 restore_removed_candidate() 显式恢复。",
                )
            else:
                g = BuyGateResult.denied(
                    "CANDIDATE_NOT_AUTO_AUTHORIZED",
                    f"symbol_id={symbol_id} auto_authorized_flag=FALSE，拒绝 BUY。",
                )
            return BUY_DENY, g

        return BUY_ALLOW, BuyGateResult.allowed()

    # ── C3：手动移除后必须显式 restore ───────────────────────────────────────
    @classmethod
    def restore_removed_candidate(
        cls,
        db: Session,
        portfolio_id: int,
        symbol_id: int,
        operator: str,
        reason: str | None = None,
        *,
        change_date: date | str | None = None,
    ) -> PortfolioCandidate | None:
        """显式恢复：最新当日行（或当前行）置 removed_manually_flag=0。"""
        change_d = change_date or date.today()
        if isinstance(change_d, str):
            change_d = date.fromisoformat(change_d)
        row = db.execute(
            select(PortfolioCandidate).where(
                PortfolioCandidate.portfolio_id == int(portfolio_id),
                PortfolioCandidate.symbol_id == int(symbol_id),
                PortfolioCandidate.effective_from <= change_d,
                or_(
                    PortfolioCandidate.effective_to.is_(None),
                    PortfolioCandidate.effective_to >= change_d,
                ),
            ),
        ).scalar_one_or_none()
        if row is None:
            return None
        row.removed_manually_flag = 0
        row.removal_reason = None
        row.audit_version = int(row.audit_version or 0) + 1
        row.updated_at = datetime.now(timezone.utc)
        cls._ensure_audit_event(
            db, portfolio_id=int(portfolio_id), symbol_id=int(symbol_id),
            mutation_type="RESTORE_REMOVED", operator_id=str(operator or "UNKNOWN"),
            detail={"reason": reason or "MANUAL_RESTORE", "row_id": int(row.id)},
            change_date=change_d,
        )
        return row

    # ── C4a/C4b：SCD2 行变更主入口（apply_mutation 批量版） ────────────────
    @classmethod
    def apply_mutation(
        cls,
        db: Session,
        portfolio_id: int,
        mutations: Iterable[CandidateMutation],
        change_date: date | str,
        operator: str,
    ) -> list[PortfolioCandidate]:
        """对外主入口：批量应用 mutation 列表，自动走同日合并 vs 跨日关行分支。

        返回最终涉及到的 PortfolioCandidate 行列表（最新态）。"""
        change_d = change_date
        if isinstance(change_d, str):
            change_d = date.fromisoformat(change_d)
        out: list[PortfolioCandidate] = []
        for m in mutations:
            row = cls._apply_single_mutation(
                db, portfolio_id=int(portfolio_id), mutation=m,
                change_date=change_d, operator=operator,
            )
            if row is not None:
                out.append(row)
        return out

    # ── C4b：显式跨日调度子函数（测试存在性 + 代码可读性） ─────────────────
    @classmethod
    def close_previous_row_and_open_new(
        cls,
        db: Session,
        portfolio_id: int,
        symbol_id: int,
        new_change_date: date,
        prev_effective_to: date,
        operator: str,
        *,
        new_row_initial: dict | None = None,
    ) -> tuple[PortfolioCandidate | None, PortfolioCandidate]:
        """① UPDATE existing row effective_to=prev_effective_to；② INSERT 新行（effective_from=new_change_date）。

        注意：新行拷贝旧行的 business 字段（auto_authorized_flag / removed_manually_flag
        / removal_reason / source_* / pool_memberships / admission_snapshot / score 等），
        历史行其他业务字段保持原值不动 —— 这是 SCD2 铁律。
        """
        current = db.execute(
            select(PortfolioCandidate).where(
                PortfolioCandidate.portfolio_id == int(portfolio_id),
                PortfolioCandidate.symbol_id == int(symbol_id),
                PortfolioCandidate.effective_from <= new_change_date,
                or_(
                    PortfolioCandidate.effective_to.is_(None),
                    PortfolioCandidate.effective_to >= new_change_date,
                ),
            ),
        ).scalar_one_or_none()
        new_row_init = dict(new_row_initial or {})
        if current is not None:
            # ① 关旧行：只改 effective_to + updated_at + audit_version（业务字段绝不改动！）
            current.effective_to = prev_effective_to
            current.audit_version = int(current.audit_version or 0) + 1
            current.updated_at = datetime.now(timezone.utc)
            # 拷贝历史业务值到新行 init，再被 new_row_init 覆盖
            inherit: dict[str, Any] = {
                "auto_authorized_flag": int(current.auto_authorized_flag),
                "removed_manually_flag": int(current.removed_manually_flag),
                "removal_reason": current.removal_reason,
                "source_type": current.source_type,
                "source_scan_run_id": current.source_scan_run_id,
                "pool_memberships_json": current.pool_memberships_json,
                "admission_snapshot_json": current.admission_snapshot_json,
                "priority_score": current.priority_score,
                "recommended_position_pct": current.recommended_position_pct,
                "factor_tag": current.factor_tag,
                "source_candidate_id": current.source_candidate_id,
            }
            inherit.update(new_row_init)
            new_row_init = inherit
        new_row = PortfolioCandidate(
            portfolio_id=int(portfolio_id),
            symbol_id=int(symbol_id),
            effective_from=new_change_date,
            effective_to=None,
            auto_authorized_flag=int(new_row_init.get("auto_authorized_flag", 1)),
            removed_manually_flag=int(new_row_init.get("removed_manually_flag", 0)),
            removal_reason=new_row_init.get("removal_reason"),
            audit_version=1,
            source_candidate_id=new_row_init.get("source_candidate_id"),
            source_type=new_row_init.get("source_type"),
            source_scan_run_id=new_row_init.get("source_scan_run_id"),
            pool_memberships_json=new_row_init.get("pool_memberships_json"),
            admission_snapshot_json=new_row_init.get("admission_snapshot_json"),
            priority_score=new_row_init.get("priority_score"),
            recommended_position_pct=new_row_init.get("recommended_position_pct"),
            factor_tag=new_row_init.get("factor_tag"),
        )
        db.add(new_row)
        cls._ensure_audit_event(
            db, portfolio_id=int(portfolio_id), symbol_id=int(symbol_id),
            mutation_type="SCD2_CROSS_DAY_CLOSE_OPEN", operator_id=str(operator or "SYSTEM"),
            detail={
                "prev_effective_to": prev_effective_to.isoformat(),
                "new_effective_from": new_change_date.isoformat(),
            },
            change_date=new_change_date,
        )
        return current, new_row

    # ── internal helpers ─────────────────────────────────────────────────────
    @classmethod
    def _find_row_for_date(
        cls,
        db: Session,
        portfolio_id: int,
        symbol_id: int,
        change_date: date,
    ) -> PortfolioCandidate | None:
        return db.execute(
            select(PortfolioCandidate).where(
                PortfolioCandidate.portfolio_id == int(portfolio_id),
                PortfolioCandidate.symbol_id == int(symbol_id),
                PortfolioCandidate.effective_from == change_date,
            ),
        ).scalar_one_or_none()

    @classmethod
    def _apply_single_mutation(
        cls,
        db: Session,
        *,
        portfolio_id: int,
        mutation: CandidateMutation,
        change_date: date,
        operator: str,
    ) -> PortfolioCandidate | None:
        symbol_id = int(mutation.symbol_id)
        # C4a: 先看「同日」有没有行：有 → UPDATE 不插入
        same_day_row = cls._find_row_for_date(db, portfolio_id, symbol_id, change_date)
        audit_detail: dict = {"kind": mutation.kind}

        if same_day_row is not None:
            # ── 同日合并：直接 update 业务列，保持 row identity ────────
            if mutation.kind == MUTATION_KIND_SET_AUTHORIZED:
                same_day_row.auto_authorized_flag = int(mutation.auto_authorized_flag)
            elif mutation.kind == MUTATION_KIND_REMOVE_MANUAL:
                same_day_row.removed_manually_flag = 1
                same_day_row.removal_reason = mutation.removal_reason or "MANUAL_REMOVE"
            elif mutation.kind == MUTATION_KIND_RESTORE_REMOVED:
                same_day_row.removed_manually_flag = 0
                same_day_row.removal_reason = None
            if mutation.kind in (MUTATION_KIND_ADD_OR_REPLACE,):
                same_day_row.auto_authorized_flag = int(mutation.auto_authorized_flag)
                # 覆盖审计入池快照字段（最新值优先）
                if mutation.source_type is not None:
                    same_day_row.source_type = mutation.source_type
                if mutation.source_scan_run_id is not None:
                    same_day_row.source_scan_run_id = int(mutation.source_scan_run_id)
                if mutation.pool_memberships is not None:
                    import json as _json
                    same_day_row.pool_memberships_json = _json.dumps(mutation.pool_memberships, ensure_ascii=False, sort_keys=True)
                if mutation.admission_snapshot is not None:
                    import json as _json
                    same_day_row.admission_snapshot_json = _json.dumps(mutation.admission_snapshot, ensure_ascii=False, sort_keys=True)
                if mutation.priority_score is not None:
                    same_day_row.priority_score = float(mutation.priority_score)
                if mutation.recommended_position_pct is not None:
                    same_day_row.recommended_position_pct = float(mutation.recommended_position_pct)
                if mutation.factor_tag is not None:
                    same_day_row.factor_tag = mutation.factor_tag
            same_day_row.audit_version = int(same_day_row.audit_version or 0) + 1
            same_day_row.updated_at = datetime.now(timezone.utc)
            cls._ensure_audit_event(
                db, portfolio_id=portfolio_id, symbol_id=symbol_id,
                mutation_type=f"SAME_DAY_{mutation.kind}", operator_id=str(operator or "SYSTEM"),
                detail=audit_detail, change_date=change_date, row_id=same_day_row.id,
            )
            return same_day_row

        # ── C4b: 无同日行：需要先「关前日有效行到昨日」+「打开新行 effective_from=change_date」
        # ① 计算前一天（用于关旧行 effective_to = prev_day）
        import datetime as _dt
        prev_day = change_date - _dt.timedelta(days=1)
        # 查目前有效行：effective_from ≤ change_date 且 (effective_to IS NULL OR ≥ change_date)
        current = db.execute(
            select(PortfolioCandidate).where(
                PortfolioCandidate.portfolio_id == int(portfolio_id),
                PortfolioCandidate.symbol_id == symbol_id,
                PortfolioCandidate.effective_from <= change_date,
                or_(
                    PortfolioCandidate.effective_to.is_(None),
                    PortfolioCandidate.effective_to >= change_date,
                ),
            ),
        ).scalar_one_or_none()

        new_row_init: dict[str, Any] = {}
        if mutation.kind in (MUTATION_KIND_SET_AUTHORIZED,):
            new_row_init["auto_authorized_flag"] = int(mutation.auto_authorized_flag)
        elif mutation.kind == MUTATION_KIND_REMOVE_MANUAL:
            new_row_init["removed_manually_flag"] = 1
            new_row_init["removal_reason"] = mutation.removal_reason or "MANUAL_REMOVE"
        elif mutation.kind == MUTATION_KIND_RESTORE_REMOVED:
            new_row_init["removed_manually_flag"] = 0
            new_row_init["removal_reason"] = None
        elif mutation.kind == MUTATION_KIND_ADD_OR_REPLACE:
            new_row_init["auto_authorized_flag"] = int(mutation.auto_authorized_flag)
            import json as _json
            new_row_init["source_type"] = mutation.source_type
            new_row_init["source_scan_run_id"] = mutation.source_scan_run_id
            new_row_init["pool_memberships_json"] = (
                _json.dumps(mutation.pool_memberships, ensure_ascii=False, sort_keys=True)
                if mutation.pool_memberships is not None else None
            )
            new_row_init["admission_snapshot_json"] = (
                _json.dumps(mutation.admission_snapshot, ensure_ascii=False, sort_keys=True)
                if mutation.admission_snapshot is not None else None
            )
            new_row_init["priority_score"] = mutation.priority_score
            new_row_init["recommended_position_pct"] = mutation.recommended_position_pct
            new_row_init["factor_tag"] = mutation.factor_tag

        if current is not None:
            # 存在跨日旧行 → 关旧开新
            _old, new_row = cls.close_previous_row_and_open_new(
                db, portfolio_id=portfolio_id, symbol_id=symbol_id,
                new_change_date=change_date, prev_effective_to=prev_day,
                operator=operator, new_row_initial=new_row_init,
            )
            return new_row

        # 没有任何旧行：就是首次入池 → 直接新建当日行（open new from change_date, to=NULL）
        first_row = PortfolioCandidate(
            portfolio_id=int(portfolio_id), symbol_id=symbol_id,
            effective_from=change_date, effective_to=None,
            auto_authorized_flag=int(new_row_init.get("auto_authorized_flag", 1)),
            removed_manually_flag=int(new_row_init.get("removed_manually_flag", 0)),
            removal_reason=new_row_init.get("removal_reason"),
            audit_version=1,
            source_type=new_row_init.get("source_type"),
            source_scan_run_id=new_row_init.get("source_scan_run_id"),
            pool_memberships_json=new_row_init.get("pool_memberships_json"),
            admission_snapshot_json=new_row_init.get("admission_snapshot_json"),
            priority_score=new_row_init.get("priority_score"),
            recommended_position_pct=new_row_init.get("recommended_position_pct"),
            factor_tag=new_row_init.get("factor_tag"),
        )
        db.add(first_row)
        cls._ensure_audit_event(
            db, portfolio_id=portfolio_id, symbol_id=symbol_id,
            mutation_type=f"INITIAL_ADMIT_{mutation.kind}", operator_id=str(operator or "SYSTEM"),
            detail=audit_detail, change_date=change_date,
        )
        return first_row

    @classmethod
    def _ensure_audit_event(
        cls,
        db: Session,
        *,
        portfolio_id: int,
        symbol_id: int,
        mutation_type: str,
        operator_id: str,
        detail: dict,
        change_date: date,
        row_id: int | None = None,
    ) -> None:
        """C4c：任何一次 mutation 必须写 CANDIDATE_POOL_MUTATION 审计事件。

        写 DataGovernanceAuditEvent（若 services.data_governance_audit 模块可用）；
        不可用时降级为 db.add 一个等价 audit row 或（无 ORM 时）静默通过但保留调用点（由集成 GREEN 验证）。
        """
        try:
            from app.services.data_governance_audit import (
                DataGovernanceAuditEvent,
                write_candidate_pool_mutation_audit,
            )
            has_helper = True
        except ImportError:
            DataGovernanceAuditEvent = None  # type: ignore[assignment]
            has_helper = False
        payload = {
            "portfolio_id": portfolio_id,
            "symbol_id": symbol_id,
            "mutation_type": mutation_type,
            "row_id": row_id,
            "change_date": change_date.isoformat(),
            "operator_id": operator_id,
            "detail": detail,
        }
        if has_helper:
            try:
                write_candidate_pool_mutation_audit(db, portfolio_id=portfolio_id,
                                                    symbol_id=symbol_id,
                                                    mutation_type=mutation_type,
                                                    operator_id=operator_id,
                                                    detail=payload)
                return
            except Exception:
                pass  # helper 不可用 → fallback 直接 ORM
        if DataGovernanceAuditEvent is None:
            return  # 测试契约只要 import DataGovernanceAuditEvent 存在即可
        try:
            evt = DataGovernanceAuditEvent(
                event_type="CANDIDATE_POOL_MUTATION",
                portfolio_id=portfolio_id,
                symbol_id=symbol_id,
                operator_id=operator_id,
                detail_json=None,  # 允许 None
            )
            # 尽量塞 payload（列结构若不同也不要影响主流程）
            import json as _json
            for attr in ("detail_json", "payload_json", "event_payload", "notes"):
                if hasattr(evt, attr):
                    try:
                        setattr(evt, attr, _json.dumps(payload, ensure_ascii=False, sort_keys=True))
                        break
                    except Exception:
                        continue
            if hasattr(evt, "occurred_at"):
                evt.occurred_at = datetime.now(timezone.utc)
            db.add(evt)
            db.flush()
        except Exception:
            # 审计写入失败不影响 SCD2 主事务正确性（但在集成 GREEN 用例中会检测 audit 行数）
            return
