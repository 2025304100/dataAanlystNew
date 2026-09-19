import pathlib

p = pathlib.Path(r"D:\ai_project\dataAanlystNew\app\api\routes\backtest.py")
content = p.read_text(encoding="utf-8")

# ============================================================
# Patch 2.1: 路由顶部 imports - 加 uuid / timedelta / Header / Request
# ============================================================
old_imp = "from fastapi import APIRouter, Depends, HTTPException, Query"
new_imp = "from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request"
if old_imp in content:
    content = content.replace(old_imp, new_imp)
    print("PASS 2.1a: imports updated (Header/Request)")
else:
    print("WARN 2.1a: import line not found")

# 加 timedelta import
old_dt = "from datetime import date"
new_dt = "from datetime import date, datetime, timedelta"
if old_dt in content:
    content = content.replace(old_dt, new_dt)
    print("PASS 2.1b: datetime/timedelta import updated")
else:
    print("WARN 2.1b")

# ============================================================
# Patch 2.2: create_portfolio_backtest_run - 函数签名加 Request + Header
# 在 precheck 通过后、run_portfolio_backtest 调用之前加 10s dedup 逻辑
# ============================================================
old_sig = "def create_portfolio_backtest_run(payload: PortfolioBacktestRequest, db: Session = Depends(get_db)):"
new_sig = "def create_portfolio_backtest_run(payload: PortfolioBacktestRequest, request: Request, db: Session = Depends(get_db), x_frontend_session_id: str | None = Header(default=None, alias=\"X-Frontend-Session-Id\")):"
if old_sig in content:
    content = content.replace(old_sig, new_sig)
    print("PASS 2.2a: route signature updated (Request + X-Frontend-Session-Id header)")
else:
    print("WARN 2.2a")

# 在 precheck 块之后，try 块开始前插入 dedup 逻辑
# 找锚点: "    try:\n        # WP0-5 Step 3：路由契约对齐"
old_anchor = """    try:
        # WP0-5 Step 3：路由契约对齐（TR-05.3 8 参数传递；only_auto / current_universe 已从 schema 移除）
        result = run_portfolio_backtest("""

new_anchor = """    # ------------------------------------------------------------
    # T11 11.1: 预检通过后、进入 run_portfolio_backtest 之前，10 秒窗口去重
    # 复合键: (frontend_session_id, config_hash, portfolio_id, start_date, end_date)
    # ------------------------------------------------------------
    # frontend_session_id 优先级：请求 body > Header X-Frontend-Session-Id > UUID4
    # 注：前端未带会话时退化为仅 per-URL 去重（效果有限），因为其他 4 键一致仍能去重
    sid = getattr(payload, "frontend_session_id", None)
    if not sid:
        sid = x_frontend_session_id
    if not sid:
        sid = uuid.uuid4().hex

    # config_hash: payload.config_hash 或空请求用 BacktestFilterConfig 默认 hash
    cfg_hash = getattr(payload, "config_hash", None) or None
    if not cfg_hash:
        try:
            from app.services.backtest_filters.config import (
                BacktestFilterConfig as _DedupCfg,
                compute_config_hash as _dedup_hash,
            )
            cfg_hash = _dedup_hash(_DedupCfg())
        except Exception:
            cfg_hash = ""

    pid = int(payload.portfolio_id)
    s_date = payload.start_date
    e_date = payload.end_date

    # 查最近 10 秒内同复合键的运行
    _dedup_cutoff = datetime.utcnow() - timedelta(seconds=10)
    _non_terminal = {"pending", "queued", "running", "creating", "precheck", "blocked"}
    _existing = db.execute(
        select(BacktestRun)
        .where(
            BacktestRun.frontend_session_id == sid,
            BacktestRun.config_hash == cfg_hash,
            BacktestRun.portfolio_id == pid,
            BacktestRun.start_date == s_date,
            BacktestRun.end_date == e_date,
            BacktestRun.created_at >= _dedup_cutoff,
        )
        .order_by(BacktestRun.created_at.desc())
        .limit(1)
    ).scalars().first()

    if _existing is not None:
        _cur_stage = getattr(_existing, "stage", None) or _existing.status
        # 若非终态 → 复用，不创建新行
        if str(_cur_stage).lower() in _non_terminal:
            _dedup_payload = {
                "run_id": _existing.id,
                "portfolio_id": pid,
                "symbol_ids": [],
                "symbol_count": 0,
                "start_date": s_date,
                "end_date": e_date,
                "initial_capital": float(_existing.initial_capital or 0),
                "status": _existing.status,
                "run_name": _existing.run_name,
                "deduplicated": True,
                "stage": getattr(_existing, "stage", None) or _map_status_to_stage(_existing.status),
                "progress_pct": getattr(_existing, "progress_pct", None),
                "updated_at": getattr(_existing, "updated_at", None),
                "error_code": getattr(_existing, "error_code", None),
                "retryable": bool(getattr(_existing, "retryable", 0)) if getattr(_existing, "retryable", None) is not None else None,
            }
            _contract = _enrich_backtest_result_contract(db, {"run_id": _existing.id})
            _dedup_payload.update({k: v for k, v in _contract.items() if k not in _dedup_payload or _dedup_payload[k] is None})
            try:
                _symbol_ids_raw = getattr(_existing, "symbol_ids_json", None)
                if _symbol_ids_raw:
                    import json as _dj
                    _sid_list = _dj.loads(_symbol_ids_raw)
                    if isinstance(_sid_list, list):
                        _dedup_payload["symbol_ids"] = [int(x) for x in _sid_list]
                        _dedup_payload["symbol_count"] = len(_dedup_payload["symbol_ids"])
            except Exception:
                pass
            return PortfolioBacktestResult(**_dedup_payload)
        # 终态（success/failed/cancelled）在 10s 窗口内 → 策略：新建（更简单，允许快速重试不同错误）

    # 写回 frontend_session_id / config_hash（通过 **kwargs 传递给 run_portfolio_backtest）
    _extra_run_kwargs: dict = {
        "_frontend_session_id": sid,
        "_config_hash_override": cfg_hash,
    }

    try:
        # WP0-5 Step 3：路由契约对齐（TR-05.3 8 参数传递；only_auto / current_universe 已从 schema 移除）
        result = run_portfolio_backtest("""

if old_anchor in content:
    content = content.replace(old_anchor, new_anchor, 1)
    print("PASS 2.2b: dedup logic injected before run_portfolio_backtest")
else:
    print("WARN 2.2b: anchor not found")

# 因为添加了 _extra_run_kwargs，需要把它传给 run_portfolio_backtest 调用
# 找到 pit_mode=... 的调用参数，在后面加 **_extra_run_kwargs
old_call = """            pit_mode=getattr(payload, \"pit_mode\", \"legacy_research\"),
        )"""
new_call = """            pit_mode=getattr(payload, "pit_mode", "legacy_research"),
            **_extra_run_kwargs,
        )"""
if old_call in content:
    content = content.replace(old_call, new_call)
    print("PASS 2.2c: extra kwargs passed to run_portfolio_backtest")
else:
    print("WARN 2.2c")

# ============================================================
# Patch 2.3: _map_status_to_stage helper + _enrich + get_backtest_run stage 投影
# ============================================================
old_list_fn = """def _exact_evidence_decision_run_ids(
    db: Session,
    trades: list[BacktestTrade],
) -> dict[str, str]:"""
new_list_fn = """def _map_status_to_stage(status: str | None) -> str:
    \"\"\"T11 11.2: 旧 status 到新 stage 的兼容映射。

    若 DB 新列 stage 非空则直接用新列；否则用本函数从旧 status 映射。
    映射规则：pending→queued；running→running；completed→success；
    error→failed；blocked→blocked；cancelled→cancelled。
    \"\"\"
    s = str(status).lower() if status else "queued"
    _legacy_map = {
        "pending": "queued",
        "queued": "queued",
        "running": "running",
        "success": "success",
        "completed": "success",
        "failed": "failed",
        "error": "failed",
        "blocked": "blocked",
        "cancelled": "cancelled",
        "precheck": "precheck",
        "creating": "creating",
    }
    return _legacy_map.get(s, "queued")


def _exact_evidence_decision_run_ids(
    db: Session,
    trades: list[BacktestTrade],
) -> dict[str, str]:"""
if old_list_fn in content:
    content = content.replace(old_list_fn, new_list_fn)
    print("PASS 2.3a: _map_status_to_stage helper added")
else:
    print("WARN 2.3a")

# 2.3b: 在 _enrich_backtest_result_contract 末尾加 stage/progress_pct/updated_at/error_code/retryable
# 找 return result 之前，result["data_snapshot"] = {...} 结束后，decision_ids 检查之前
old_enrich = """    # A run with any non-ready DecisionRun is a blocked result, not an empty"""
new_enrich = """    # T11 11.2: 投影 stage / progress_pct / updated_at / error_code / retryable
    # 兼容：新列 stage 非空用新列，否则用旧 status → _map_status_to_stage 映射
    _raw_stage = getattr(run, "stage", None)
    result["stage"] = _raw_stage if _raw_stage else _map_status_to_stage(run.status)
    result["progress_pct"] = getattr(run, "progress_pct", None)
    _ua = getattr(run, "updated_at", None)
    result["updated_at"] = _ua.isoformat() if _ua is not None else None
    result["error_code"] = getattr(run, "error_code", None)
    _ry = getattr(run, "retryable", None)
    result["retryable"] = bool(_ry) if _ry is not None else None

    # A run with any non-ready DecisionRun is a blocked result, not an empty"""
if old_enrich in content:
    content = content.replace(old_enrich, new_enrich)
    print("PASS 2.3b: _enrich stage/progress/updated_at projection added")
else:
    print("WARN 2.3b")

p.write_text(content, encoding="utf-8")
print("DONE: routes/backtest.py patched")
