import pathlib
p = pathlib.Path(r"D:\ai_project\dataAanlystNew\app\models\backtest.py")
content = p.read_text(encoding="utf-8")
marker = "class BacktestTrade(Base):"
idx = content.find(marker)
if idx < 0:
    raise SystemExit("MARKER_NOT_FOUND")
before = content[:idx]
after = content[idx:]
new_block = """
    # -- T11: 状态机 + 提交去重字段 --
    frontend_session_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True,
        comment="T11 10s dedup key: frontend session id",
    )
    stage: Mapped[str | None] = mapped_column(
        String(16), nullable=True, server_default="queued", index=True,
        comment="T11 stage enum: precheck|creating|queued|running|success|failed|blocked|cancelled",
    )
    progress_pct: Mapped[float | None] = mapped_column(
        Float, nullable=True, server_default="0.0",
        comment="T11 progress percent [0, 100], increments across stages",
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        comment="T11 auto-updated timestamp per stage/progress change",
    )
    error_code: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
        comment="T11 machine-readable error code",
    )
    retryable: Mapped[int | None] = mapped_column(
        Integer, nullable=True, server_default="0",
        comment="T11 BOOLEAN: retryable transient error",
    )


"""
content = before.rstrip() + "\n" + new_block + after
p.write_text(content, encoding="utf-8")
print("OK")
