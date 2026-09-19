"""回测过滤治理核心引擎（纯函数，无 DB 副作用）。

执行顺序（按 PRD §5）：
    1. 数据状态时效检查：任何 status=UNKNOWN 且 production_fidelity=True
       → StatusUnknownBlockingError（生产阻断）。
    2. 次新股过滤：listing_age < min_listing_age_calendar_days → 排除。
    3. ST 过滤：信号日 ST → 新开仓候选排除；持仓变 ST 仅记录事件，不强制平仓。
    4. 停牌过滤：候选停牌 → 排除；持仓停牌 → 冻结。
    5. 退市整理期：候选排除；持仓按策略但按停牌策略则冻结。
    6. 输出退市清算候选（DELISTED 且 is_listed=False 或 delisting_date<=trade_date 且持仓>0）。

每一步都对应开关；关闭时仅跳过该步的「排除/冻结」判定，但仍产生审计事件。
"""
from __future__ import annotations

import json
from datetime import date
from typing import Mapping

from app.services.backtest_filters.config import (
    BacktestFilterConfig,
    compute_config_hash,
)
from app.services.backtest_filters.rules import (
    ACTION_EXCLUDE,
    ACTION_FREEZE,
    ACTION_FORCE_LIQUIDATE,
    ACTION_INCLUDE,
    FILTER_GATE_RULE_MAP,
    DelistingCandidate,
    FilterEventDTO,
    FilterOutcome,
    FrozenPositionInfo,
    RULE_DELISTING_LIQUIDATION_MANDATORY,
    RULE_DELISTING_PERIOD_EXCLUDE,
    RULE_INCLUDE_NORMAL,
    RULE_NEW_LISTING_EXCLUDE,
    RULE_NEW_LISTING_LISTING_DATE_UNKNOWN,
    RULE_STATUS_UNKNOWN_BLOCK,
    RULE_ST_EXCLUDE,
    RULE_ST_HOLD_NO_FORCE_LIQUIDATE,
    RULE_SUSPENDED_EXCLUDE,
    RULE_SUSPENDED_FREEZE,
)
from app.services.security_status.pit_service import SecurityStatusDTO


# ---------------------------------------------------------------------------
# 辅助：PIT DTO -> 快照 JSON 字符串（写审计 raw_status_json 用）
# ---------------------------------------------------------------------------
def _status_to_json(s: SecurityStatusDTO) -> str:
    return json.dumps({
        "status": s.status,
        "listing_age_calendar_days": s.listing_age_calendar_days,
        "delisting_days_ago": s.delisting_days_ago,
        "raw_source": s.raw_source,
        "is_st": s.is_st,
        "is_suspended": s.is_suspended,
        "is_delisting_period": s.is_delisting_period,
        "is_listed": s.is_listed,
        "listing_date": s.listing_date.isoformat() if s.listing_date else None,
        "delisting_date": s.delisting_date.isoformat() if s.delisting_date else None,
    }, ensure_ascii=False, sort_keys=True)


# ---------------------------------------------------------------------------
# 1. 状态时效检查（生产阻断）
# ---------------------------------------------------------------------------
def _check_status_integrity(
    symbol_ids: list[int],
    pit_status_map: Mapping[int, SecurityStatusDTO],
    production_fidelity: bool,
    trade_date: date,
) -> tuple[list[FilterEventDTO], list[int]]:
    """校验所有 symbol 的状态不为 UNKNOWN（生产保真时）。

    返回 (events_for_unknown, unknown_symbol_ids)。
    若 production_fidelity=True 且 unknown_symbol_ids 非空，调用方应抛阻断异常。
    """
    events: list[FilterEventDTO] = []
    unknown_ids: list[int] = []
    for sid in sorted(symbol_ids):
        st = pit_status_map.get(sid)
        if st is None or st.status == "UNKNOWN":
            unknown_ids.append(sid)
            events.append(FilterEventDTO(
                trade_date=trade_date,
                symbol_id=sid,
                action=ACTION_EXCLUDE,
                rule_code=RULE_STATUS_UNKNOWN_BLOCK,
                reason=f"证券状态=UNKNOWN（production_fidelity={production_fidelity}）",
                raw_status_json=json.dumps({"from_pit": False}, sort_keys=True),
                effective_status="UNKNOWN",
                price_used=None,
                config_hash="",  # 稍后统一填充
                data_batch_id=None,
            ))
    return events, unknown_ids


# ---------------------------------------------------------------------------
# 2. 次新股过滤
# ---------------------------------------------------------------------------
def _filter_new_listing(
    candidates_in: list[int],
    pit_status_map: Mapping[int, SecurityStatusDTO],
    trade_date: date,
    min_age_days: int,
    enabled: bool,
) -> tuple[list[int], list[FilterEventDTO]]:
    """返回 (post_candidates, events)。"""
    post: list[int] = []
    events: list[FilterEventDTO] = []
    for sid in candidates_in:
        st = pit_status_map.get(sid)
        if st is None:
            # UNKNOWN 已由 Step 1 处理；此处不重复但保险 include
            post.append(sid)
            continue
        if not enabled:
            post.append(sid)
            continue
        age = st.listing_age_calendar_days
        if st.listing_date is None:
            # 上市日期未知 → 阻断
            events.append(FilterEventDTO(
                trade_date=trade_date, symbol_id=sid, action=ACTION_EXCLUDE,
                rule_code=RULE_NEW_LISTING_LISTING_DATE_UNKNOWN,
                reason="listing_date 未知，无法判定次新股年龄",
                raw_status_json=_status_to_json(st), effective_status=st.status,
                config_hash="", data_batch_id=None,
            ))
            continue
        if age is None or age < min_age_days:
            # 次新股排除
            events.append(FilterEventDTO(
                trade_date=trade_date, symbol_id=sid, action=ACTION_EXCLUDE,
                rule_code=RULE_NEW_LISTING_EXCLUDE,
                reason=f"次新股 listing_age={age or 0} 天 < 阈值 {min_age_days} 天",
                raw_status_json=_status_to_json(st), effective_status=st.status,
                config_hash="", data_batch_id=None,
            ))
            continue
        post.append(sid)
    return post, events


# ---------------------------------------------------------------------------
# 3. ST 过滤
# ---------------------------------------------------------------------------
def _filter_st(
    candidates_in: list[int],
    position_map: Mapping[int, float],
    pit_status_map: Mapping[int, SecurityStatusDTO],
    trade_date: date,
    enabled: bool,
) -> tuple[list[int], list[FilterEventDTO]]:
    """候选池 ST 排除；持仓变 ST 仅事件不动作。"""
    post: list[int] = []
    events: list[FilterEventDTO] = []
    for sid in candidates_in:
        st = pit_status_map.get(sid)
        if st is None:
            post.append(sid)
            continue
        if not enabled:
            post.append(sid)
            continue
        if st.status == "ST":
            # 新开户候选排除
            events.append(FilterEventDTO(
                trade_date=trade_date, symbol_id=sid, action=ACTION_EXCLUDE,
                rule_code=RULE_ST_EXCLUDE,
                reason="信号日为 ST/*ST，禁止新开仓",
                raw_status_json=_status_to_json(st), effective_status="ST",
                config_hash="", data_batch_id=None,
            ))
            continue
        post.append(sid)
    # 持仓变 ST：仅事件，不强平（仅对 candidates_in 之外但仍在持仓中的 symbol）
    for sid, qty in position_map.items():
        if qty <= 0 or sid in candidates_in:
            continue
        st_dto = pit_status_map.get(sid)
        if st_dto and st_dto.status == "ST":
            events.append(FilterEventDTO(
                trade_date=trade_date, symbol_id=sid, action=ACTION_INCLUDE,
                rule_code=RULE_ST_HOLD_NO_FORCE_LIQUIDATE,
                reason="持仓期间变为 ST，按需求不强制平仓，保持持仓",
                raw_status_json=_status_to_json(st_dto), effective_status="ST",
                config_hash="", data_batch_id=None,
            ))
    return post, events


# ---------------------------------------------------------------------------
# 4. 停牌过滤
# ---------------------------------------------------------------------------
def _filter_suspended(
    candidates_in: list[int],
    position_map: Mapping[int, float],
    pit_status_map: Mapping[int, SecurityStatusDTO],
    trade_date: date,
    enabled: bool,
) -> tuple[list[int], dict[int, FrozenPositionInfo], list[FilterEventDTO]]:
    post: list[int] = []
    frozen: dict[int, FrozenPositionInfo] = {}
    events: list[FilterEventDTO] = []
    for sid in candidates_in:
        st = pit_status_map.get(sid)
        if st is None:
            post.append(sid)
            continue
        if not enabled:
            post.append(sid)
            continue
        if st.status == "SUSPENDED":
            events.append(FilterEventDTO(
                trade_date=trade_date, symbol_id=sid, action=ACTION_EXCLUDE,
                rule_code=RULE_SUSPENDED_EXCLUDE,
                reason="信号日停牌，禁止新开仓和调仓",
                raw_status_json=_status_to_json(st), effective_status="SUSPENDED",
                config_hash="", data_batch_id=None,
            ))
            continue
        post.append(sid)
    # 持仓停牌 → 冻结
    if enabled:
        for sid, qty in position_map.items():
            if qty <= 0:
                continue
            st_dto = pit_status_map.get(sid)
            if st_dto and st_dto.status == "SUSPENDED":
                info = FrozenPositionInfo(
                    symbol_id=sid,
                    reason_rule_code=RULE_SUSPENDED_FREEZE,
                    reason_detail="持仓停牌，当日跳过买入和卖出撮合，成交量=0",
                    opening_quantity=float(qty),
                )
                frozen[sid] = info
                events.append(FilterEventDTO(
                    trade_date=trade_date, symbol_id=sid, action=ACTION_FREEZE,
                    rule_code=RULE_SUSPENDED_FREEZE,
                    reason=info.reason_detail,
                    raw_status_json=_status_to_json(st_dto), effective_status="SUSPENDED",
                    config_hash="", data_batch_id=None,
                ))
    return post, frozen, events


# ---------------------------------------------------------------------------
# 5. 退市整理期过滤
# ---------------------------------------------------------------------------
def _filter_delisting_period(
    candidates_in: list[int],
    pit_status_map: Mapping[int, SecurityStatusDTO],
    trade_date: date,
    enabled: bool,
) -> tuple[list[int], list[FilterEventDTO]]:
    post: list[int] = []
    events: list[FilterEventDTO] = []
    for sid in candidates_in:
        st = pit_status_map.get(sid)
        if st is None:
            post.append(sid)
            continue
        if not enabled:
            post.append(sid)
            continue
        if st.status == "DELISTING_PERIOD":
            events.append(FilterEventDTO(
                trade_date=trade_date, symbol_id=sid, action=ACTION_EXCLUDE,
                rule_code=RULE_DELISTING_PERIOD_EXCLUDE,
                reason="退市整理期，禁止新开仓",
                raw_status_json=_status_to_json(st), effective_status="DELISTING_PERIOD",
                config_hash="", data_batch_id=None,
            ))
            continue
        post.append(sid)
    return post, events


# ---------------------------------------------------------------------------
# 6. 退市清算候选（正式摘牌且持仓 > 0）
# ---------------------------------------------------------------------------
def _collect_delisting_candidates(
    position_map: Mapping[int, float],
    pit_status_map: Mapping[int, SecurityStatusDTO],
    trade_date: date,
    enabled: bool,
) -> tuple[list[DelistingCandidate], list[FilterEventDTO]]:
    candidates: list[DelistingCandidate] = []
    events: list[FilterEventDTO] = []
    if not enabled:
        return candidates, events
    for sid, qty in position_map.items():
        if qty <= 0:
            continue
        st = pit_status_map.get(sid)
        if st is None:
            continue
        if st.status == "DELISTED":
            # last_valid_close 留空（由后续清算器从 DailyBar 取）
            candidates.append(DelistingCandidate(
                symbol_id=sid,
                trade_date=trade_date,
                holding_quantity=float(qty),
                last_valid_close=None,
            ))
            events.append(FilterEventDTO(
                trade_date=trade_date, symbol_id=sid, action=ACTION_FORCE_LIQUIDATE,
                rule_code=RULE_DELISTING_LIQUIDATION_MANDATORY,
                reason=f"正式摘牌，按持仓 {qty} 股生成退市强制清算候选",
                raw_status_json=_status_to_json(st), effective_status="DELISTED",
                price_used=None,  # 清算后回填
                config_hash="", data_batch_id=None,
            ))
    return candidates, events


# ---------------------------------------------------------------------------
# 主入口：按固定顺序装配
# ---------------------------------------------------------------------------
def apply_daily_filters(
    trade_date: date,
    candidate_symbol_ids: list[int],
    position_map: Mapping[int, float],
    pit_status_map: Mapping[int, SecurityStatusDTO],
    config: BacktestFilterConfig,
    data_batch_id: str | None = None,
    run_id: int | None = None,
) -> FilterOutcome:
    """按固定顺序执行全部过滤。

    位置参数严格稳定，调用顺序不能错。返回的 filter_events 按 symbol_id ASC 排序。
    """
    cfg_hash = compute_config_hash(config)

    all_symbol_ids = sorted(set(
        list(candidate_symbol_ids) + list(position_map.keys())
    ))

    all_events: list[FilterEventDTO] = []

    # Step 1: 状态时效完整性（生产阻断）
    integ_events, unknown_ids = _check_status_integrity(
        all_symbol_ids, pit_status_map, config.production_fidelity, trade_date
    )
    all_events.extend(integ_events)
    if config.production_fidelity and unknown_ids:
        # 导入异常并抛
        from app.services.backtest_filters.rules import StatusUnknownBlockingError
        raise StatusUnknownBlockingError(
            symbol_ids=list(sorted(unknown_ids)),
            trade_date=trade_date,
        )

    # 从候选池移除 unknown（非保真模式下视为排除，不阻断）
    working_candidates = [sid for sid in candidate_symbol_ids if sid not in set(unknown_ids)]

    # Step 2: 次新股
    working_candidates, e = _filter_new_listing(
        working_candidates, pit_status_map, trade_date,
        config.min_listing_age_calendar_days, config.filter_new_listing,
    )
    all_events.extend(e)

    # Step 3: ST
    working_candidates, e = _filter_st(
        working_candidates, position_map, pit_status_map, trade_date, config.filter_st
    )
    all_events.extend(e)

    # Step 4: 停牌
    working_candidates, frozen_map, e = _filter_suspended(
        working_candidates, position_map, pit_status_map, trade_date, config.filter_suspended
    )
    all_events.extend(e)

    # Step 5: 退市整理期
    working_candidates, e = _filter_delisting_period(
        working_candidates, pit_status_map, trade_date, config.delisting_period_excluded
    )
    all_events.extend(e)

    # Step 6: 退市清算候选
    liquidation_list, e = _collect_delisting_candidates(
        position_map, pit_status_map, trade_date, config.force_delisting_liquidation
    )
    all_events.extend(e)

    # 为剩余的候选添加 INCLUDE 事件（保证每个 candidate 至少有一条事件）
    included_set = set(working_candidates)
    excluded_or_actioned_ids = {ev.symbol_id for ev in all_events}
    for sid in sorted(included_set):
        if sid in excluded_or_actioned_ids:
            continue
        st = pit_status_map.get(sid)
        eff = st.status if st is not None else "UNKNOWN"
        all_events.append(FilterEventDTO(
            trade_date=trade_date, symbol_id=sid, action=ACTION_INCLUDE,
            rule_code=RULE_INCLUDE_NORMAL,
            reason="通过全部过滤规则，正常进入候选池",
            raw_status_json=(_status_to_json(st) if st is not None else "{}"),
            effective_status=eff, price_used=None,
            config_hash=cfg_hash, data_batch_id=data_batch_id,
            run_id=run_id,
        ))

    # 回填所有事件的 config_hash / run_id / data_batch_id
    normalized_events: list[FilterEventDTO] = []
    for ev in all_events:
        patch_kwargs: dict = {
            "config_hash": cfg_hash,
            "data_batch_id": data_batch_id,
        }
        if run_id is not None and ev.run_id is None:
            patch_kwargs["run_id"] = run_id
        new_ev = ev.model_copy(update=patch_kwargs)
        normalized_events.append(new_ev)

    # 按 symbol_id ASC 稳定排序；symbol 相同按 rule_code 字典序
    normalized_events.sort(key=lambda e: (e.symbol_id, e.rule_code))

    # eligible_candidates 按 input candidate_symbol_ids 的相对顺序保序（仅保留 included 中实际出现）
    eligible_ordered = [sid for sid in candidate_symbol_ids if sid in included_set]
    # 去重保序
    seen: set[int] = set()
    eligible_final: list[int] = []
    for sid in eligible_ordered:
        if sid in seen:
            continue
        seen.add(sid)
        eligible_final.append(sid)

    return FilterOutcome(
        eligible_candidates=eligible_final,
        frozen_positions=dict(frozen_map),
        delisting_candidates=list(liquidation_list),
        filter_events=normalized_events,
    )
