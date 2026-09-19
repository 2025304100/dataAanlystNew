import pathlib

# -------- Patch 1: schemas/backtest.py BacktestRunRead --------
p = pathlib.Path(r"D:\ai_project\dataAanlystNew\app\schemas\backtest.py")
content = p.read_text(encoding="utf-8")

# 1.1 在 BacktestRunRead 末尾 (is_result_production_eligible: bool = True) 之后插入 5 字段
old1 = "    is_result_production_eligible: bool = True\n\n\nclass BacktestRunDetail"
new1 = """    is_result_production_eligible: bool = True
    # T11: 状态机 + 进度跟踪（兼容老数据：stage/progress_pct 可 None）
    stage: str | None = None
    progress_pct: float | None = None
    updated_at: datetime | None = None
    error_code: str | None = None
    retryable: bool | None = None


class BacktestRunDetail"""
if old1 not in content:
    print("WARN: old1 not found, trying alt")
else:
    content = content.replace(old1, new1)
    print("PASS: patch1.1 BacktestRunRead 5 fields added")

# 1.2 PortfolioBacktestResult: 在最后一行附近加 stage/progress_pct/updated_at/error_code/retryable + deduplicated
# 找 PortfolioBacktestResult 类中最后一个字段附近："metrics: dict = Field(default_factory=dict)\n    # diagnostics:"
old2 = "    # metrics: 复用 BacktestRun 指标 + 前端 BacktestSummary 常用 key（冗余一份，向后兼容）\n    metrics: dict = Field(default_factory=dict)\n    # diagnostics: buy_signal_days / skip_reasons / sample_misses 等诊断（对齐 BacktestRunDetail.diagnostics）"
new2 = """    # metrics: 复用 BacktestRun 指标 + 前端 BacktestSummary 常用 key（冗余一份，向后兼容）
    metrics: dict = Field(default_factory=dict)
    # diagnostics: buy_signal_days / skip_reasons / sample_misses 等诊断（对齐 BacktestRunDetail.diagnostics）
    # T11: 状态机 + 进度跟踪 + 去重标记
    deduplicated: bool = False
    stage: str | None = None
    progress_pct: float | None = None
    updated_at: datetime | None = None
    error_code: str | None = None
    retryable: bool | None = None"""
if old2 in content:
    content = content.replace(old2, new2)
    print("PASS: patch1.2 PortfolioBacktestResult 6 fields added")
else:
    print("WARN: old2 not found")

# 1.3 PortfolioBacktestRequest: 新增可选 frontend_session_id + config_hash
old3 = "    pit_mode: Literal[\"legacy_research\", \"research_pit\", \"production_pit\"] = \"legacy_research\"\n\n    @field_validator(\"end_date\")"
new3 = """    pit_mode: Literal["legacy_research", "research_pit", "production_pit"] = "legacy_research"
    # T11: 10s 窗口去重复合键（2/5 可选；缺省时后端从 Header 取或 UUID4 生成）
    frontend_session_id: str | None = Field(default=None, description="T11 去重键：前端会话 ID；若未携带则从 Header X-Frontend-Session-Id 读取或 UUID4 生成")
    config_hash: str | None = Field(default=None, description="T11 去重键：过滤配置 SHA256 hex；为空则用默认 BacktestFilterConfig 的 hash")

    @field_validator("end_date")"""
if old3 in content:
    content = content.replace(old3, new3)
    print("PASS: patch1.3 PortfolioBacktestRequest 2 fields added")
else:
    print("WARN: old3 not found")

p.write_text(content, encoding="utf-8")
print("DONE: schemas/backtest.py")
