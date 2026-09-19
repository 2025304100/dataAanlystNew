import pathlib

p = pathlib.Path(r"D:\ai_project\dataAanlystNew\app\services\portfolio_backtest.py")
content = p.read_text(encoding="utf-8")

# ============================================================
# Patch 3.1: run_portfolio_backtest signature - 接收去重 kwargs
# 找函数签名行
# ============================================================
old_sig = """def run_portfolio_backtest(
    db: Session,
    portfolio_id: int,
    *,
    start_date: date,
    end_date: date,
    run_name: str | None = None,
    benchmark: str | None = None,
    # WP0-5a 新契约：任务启动时锁定 snapshot；缺省则自动创建任务级锁 + 回退绑定
    strategy_snapshot_id: str | None = None,
    score_weight_mode: str | None = None,
    factor_model_run_id: str | None = None,
    # WP0-5 TR-05.3 契约参数（8 字段，C-05 修复）
    initial_capital: float | None = None,
    commission_rate: float = 0.0003,
    stamp_tax_rate: float = 0.001,
    slippage_bps: int = 5,
    price_type: str = "NEXT_OPEN",
    volume_limit_pct: float = 0.10,
    rebalance_frequency: str = "on_signal",
    pit_mode: str = "legacy_research",
    **_legacy_kwargs: Any,
) -> dict[str, Any]:"""

new_sig = """def run_portfolio_backtest(
    db: Session,
    portfolio_id: int,
    *,
    start_date: date,
    end_date: date,
    run_name: str | None = None,
    benchmark: str | None = None,
    # WP0-5a 新契约：任务启动时锁定 snapshot；缺省则自动创建任务级锁 + 回退绑定
    strategy_snapshot_id: str | None = None,
    score_weight_mode: str | None = None,
    factor_model_run_id: str | None = None,
    # WP0-5 TR-05.3 契约参数（8 字段，C-05 修复）
    initial_capital: float | None = None,
    commission_rate: float = 0.0003,
    stamp_tax_rate: float = 0.001,
    slippage_bps: int = 5,
    price_type: str = "NEXT_OPEN",
    volume_limit_pct: float = 0.10,
    rebalance_frequency: str = "on_signal",
    pit_mode: str = "legacy_research",
    **_legacy_kwargs: Any,
) -> dict[str, Any]:
    # T11.1: 从路由透传的去重元数据（仅在 new-style dedup 路由调用时存在）
    _sid = _legacy_kwargs.pop("_frontend_session_id", None)
    _cfg_override = _legacy_kwargs.pop("_config_hash_override", None)

    # T11.3: 阶段进度辅助函数（统一更新 stage/progress_pct/updated_at）
    # 进度阶段 N=6：precheck(16.7) -> data_loading(33.3) -> applying_filters(50)
    #               -> generating_trades(66.7) -> aggregating_metrics(83.3) -> done(100)
    _STAGES_PCT = [
        ("precheck",   round(1 * 100 / 6, 1)),
        ("creating",   round(2 * 100 / 6, 1)),
        ("running",    round(3 * 100 / 6, 1)),
        ("running",    round(4 * 100 / 6, 1)),
        ("running",    round(5 * 100 / 6, 1)),
        ("success",    100.0),
    ]
    _stage_idx = [0]  # 可变包裹
    def _set_stage(run_obj: BacktestRun, stage_override: str | None = None):
        if run_obj is None:
            return
        idx = max(0, min(_stage_idx[0], len(_STAGES_PCT) - 1))
        s, pct = _STAGES_PCT[idx]
        try:
            run_obj.stage = stage_override or s
            run_obj.progress_pct = float(pct)
            from datetime import datetime, timezone as _tz
            run_obj.updated_at = datetime.now(_tz.utc)
            db.flush()
        except Exception:
            # 进度更新是非关键路径，失败不阻断主链路
            pass
    def _step_stage(run_obj: BacktestRun, stage_override: str | None = None):
        _set_stage(run_obj, stage_override)
        _stage_idx[0] = min(_stage_idx[0] + 1, len(_STAGES_PCT) - 1)

    # T11: 可重试异常类型集合（用于 retryable 标记）
    _RETRYABLE_EXCEPTIONS: tuple = (
        ConnectionError, TimeoutError, OSError,
    )"""

if old_sig in content:
    content = content.replace(old_sig, new_sig)
    print("PASS 3.1: signature + progress helper + dedup metadata extraction added")
else:
    print("WARN 3.1: signature not found")

# ============================================================
# Patch 3.2: 在完成 portfolio / capital / price_type 校验后 → Stage 1: precheck
# 找锚点：if rebalance_frequency not in {"daily"...} 的 raise ValueError 之后
# ============================================================
old_stage1_anchor = """    if rebalance_frequency not in {"daily", "weekly", "monthly", "on_signal"}:
        raise ValueError(
            \"rebalance_frequency 仅允许 daily/weekly/monthly/on_signal，\"
            f\"传入={rebalance_frequency}\"
        )

    # WP0-5a / C-02 修复：不再硬编码"""
new_stage1_anchor = """    if rebalance_frequency not in {"daily", "weekly", "monthly", "on_signal"}:
        raise ValueError(
            "rebalance_frequency 仅允许 daily/weekly/monthly/on_signal，"
            f"传入={rebalance_frequency}"
        )

    # T11.3 Stage 1/6: precheck_checked —— 全部前置校验通过
    # progress_pct = 16.7%
    _step_stage(None, "precheck")  # run 还未创建，先推进阶段索引

    # WP0-5a / C-02 修复：不再硬编码"""
if old_stage1_anchor in content:
    content = content.replace(old_stage1_anchor, new_stage1_anchor)
    print("PASS 3.2: Stage 1 precheck added")
else:
    print("WARN 3.2: Stage 1 anchor not found")

# ============================================================
# Patch 3.3: symbol_ids 推导后 → Stage 2: creating / 数据加载中
# 锚点：在 ensure_symbol_ids_in_scope 调用完成之后，rule_config 构造之前
# ============================================================
old_stage2_anchor = """    ensure_symbol_ids_in_scope(db, portfolio, symbol_ids)

    # 构造 rule_config（从 PortfolioRule + SignalRule 读取，避免"策略规则没打通"）"""
new_stage2_anchor = """    ensure_symbol_ids_in_scope(db, portfolio, symbol_ids)

    # T11.3 Stage 2/6: creating —— 标的已推导完成，规则/数据加载中
    # progress_pct = 33.3%
    _step_stage(None, "creating")

    # 构造 rule_config（从 PortfolioRule + SignalRule 读取，避免"策略规则没打通"）"""
if old_stage2_anchor in content:
    content = content.replace(old_stage2_anchor, new_stage2_anchor)
    print("PASS 3.3: Stage 2 creating (data loading) added")
else:
    print("WARN 3.3: Stage 2 anchor not found")

# ============================================================
# Patch 3.4: _filter_symbol_ids_by_rule 之后 → Stage 3: running / 应用 filters
# 锚点：if not symbol_ids: raise ValueError("策略规则过滤后无可回测标的"...
# ============================================================
old_stage3_anchor = """    symbol_ids = _filter_symbol_ids_by_rule(db, portfolio_id, rule, symbol_ids, as_of_date=end_date)
    if not symbol_ids:
        raise ValueError(\"策略规则过滤后无可回测标的（stock_pool/质量/择时阈值过严或PIT截止后无有效Score），请调整策略规则后再回测。\")"""
new_stage3_anchor = """    symbol_ids = _filter_symbol_ids_by_rule(db, portfolio_id, rule, symbol_ids, as_of_date=end_date)
    if not symbol_ids:
        raise ValueError("策略规则过滤后无可回测标的（stock_pool/质量/择时阈值过严或PIT截止后无有效Score），请调整策略规则后再回测。")

    # T11.3 Stage 3/6: running —— 策略规则/filters 已应用完成，准备调用回测引擎
    # progress_pct = 50%
    _step_stage(None, "running")"""
if old_stage3_anchor in content:
    content = content.replace(old_stage3_anchor, new_stage3_anchor)
    print("PASS 3.4: Stage 3 running (filters applied) added")
else:
    print("WARN 3.4")

# ============================================================
# Patch 3.5: run_backtest 返回之后（run = run_backtest(...)）
# → Stage 4: running / 生成成交
# ============================================================
old_stage4_anchor = """    run = run_backtest(
        db=db,
        portfolio_id=portfolio_id,
        symbol_ids=symbol_ids,
        start_date=start_date,
        end_date=end_date,
        rule_config=rule_config,
        cost_config=cost_config_for_run,
        run_name=run_name,
        score_weight_mode=effective_score_mode,
        factor_model_run_id=effective_model_id,
        initial_capital=effective_capital,
        **snapshot_kwargs,
    )

    # P2 BFG：回填 backtest_runs.config_hash / filter_config_json / production_fidelity"""
new_stage4_anchor = """    run = run_backtest(
        db=db,
        portfolio_id=portfolio_id,
        symbol_ids=symbol_ids,
        start_date=start_date,
        end_date=end_date,
        rule_config=rule_config,
        cost_config=cost_config_for_run,
        run_name=run_name,
        score_weight_mode=effective_score_mode,
        factor_model_run_id=effective_model_id,
        initial_capital=effective_capital,
        **snapshot_kwargs,
    )

    # T11.1 透传：写回 frontend_session_id / config_hash 到新建行
    if _sid is not None and hasattr(run, "frontend_session_id"):
        run.frontend_session_id = _sid
    if _cfg_override is not None and hasattr(run, "config_hash"):
        run.config_hash = _cfg_override

    # T11.3 Stage 4/6: running —— 成交已生成，进入 BFG 配置持久化与指标汇总
    # progress_pct = 66.7%
    _step_stage(run, "running")

    # P2 BFG：回填 backtest_runs.config_hash / filter_config_json / production_fidelity"""
if old_stage4_anchor in content:
    content = content.replace(old_stage4_anchor, new_stage4_anchor)
    print("PASS 3.5: Stage 4 (post run_backtest) added + dedup metadata persisted")
else:
    print("WARN 3.5")

# ============================================================
# Patch 3.6: BFG 配置回填 try/except 块之后（db.flush pass）
# → Stage 5: running / 汇总指标
# 锚点：except Exception: # BFG 配置持久化回填是非关键路径
# ============================================================
old_stage5_anchor = """    except Exception:
        # BFG 配置持久化回填是非关键路径，不得中断主回测链路
        pass

    # Q29：保留已经由统一 DecisionEngine 持久化的 backtest DecisionRun 关联。"""
new_stage5_anchor = """    except Exception:
        # BFG 配置持久化回填是非关键路径，不得中断主回测链路
        pass

    # T11.3 Stage 5/6: running —— BFG 持久化完成，进入 DecisionRun 关联汇总
    # progress_pct = 83.3%
    _step_stage(run, "running")

    # Q29：保留已经由统一 DecisionEngine 持久化的 backtest DecisionRun 关联。"""
if old_stage5_anchor in content:
    content = content.replace(old_stage5_anchor, new_stage5_anchor)
    print("PASS 3.6: Stage 5 (aggregating metrics) added")
else:
    print("WARN 3.5")

# ============================================================
# Patch 3.7: 函数末尾 result dict 组装完成 return 之前 → Stage 6: success / 完成
# 锚点：result = {...} 后还有更多字段，找 "equity_curve" 组装后的 return
# ============================================================
# 先读文件末尾 result 后的内容
old_final_anchor = """        decision_run_ids = []

    result = {
        \"run_id\": run.id,"""
new_final_anchor = """        decision_run_ids = []

    # T11.3 Stage 6/6: success —— 全部阶段完成
    # progress_pct = 100%
    _step_stage(run, "success")
    # 确保最终 stage = success 且 progress_pct = 100（_step_stage 因边界可能未到）
    try:
        if run.stage != "success":
            run.stage = "success"
        run.progress_pct = 100.0
        db.flush()
    except Exception:
        pass

    result = {
        "run_id": run.id,"""
if old_final_anchor in content:
    content = content.replace(old_final_anchor, new_final_anchor)
    print("PASS 3.7: Stage 6 success (final) added")
else:
    print("WARN 3.7: final anchor not found")

# ============================================================
# Patch 3.8: 在 result 中回显 stage/progress_pct/updated_at/error_code/retryable + deduplicated
# 找 result dict 闭合后、末尾的 return result。在 equity_curve 组装之后加。
# ============================================================
# 找 result["metrics"] = {} 的位置。先读取当前文件末尾
# 搜索 "return result"
return_marker = "\n    return result"
if return_marker in content:
    _insert_proj = """
    # T11: 结果回显 —— stage / progress_pct / updated_at / error_code / retryable
    result["deduplicated"] = False
    result["stage"] = getattr(run, "stage", None)
    result["progress_pct"] = getattr(run, "progress_pct", None)
    _ua = getattr(run, "updated_at", None)
    result["updated_at"] = _ua.isoformat() if _ua is not None else None
    result["error_code"] = getattr(run, "error_code", None)
    _ry = getattr(run, "retryable", None)
    result["retryable"] = bool(_ry) if _ry is not None else None
"""
    content = content.replace(return_marker, _insert_proj + return_marker, 1)
    print("PASS 3.8: stage/progress projection added to result dict")
else:
    print("WARN 3.8: return result marker not found")

p.write_text(content, encoding="utf-8")
print("DONE: services/portfolio_backtest.py patched")
