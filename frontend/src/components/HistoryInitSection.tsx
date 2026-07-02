import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Alert, Button, Empty, Input, InputNumber, Popconfirm, Progress, Radio, Tag } from "antd";
import { CloseOutlined, DeleteOutlined, DownloadOutlined, DownOutlined, HistoryOutlined, ReloadOutlined, SyncOutlined, UpOutlined } from "@ant-design/icons";
import { api } from "../api/client";
import { useApp } from "../context/AppContext";
import { formatDate } from "../utils/format";
import { t, template } from "../i18n";
import type {
  HistoryInitializationFailureItem,
  HistoryInitializationRunRecord,
  HistoryInitializationStage,
  HistoryInitializationTask,
} from "../types";

function safeErrorMessage(error: any): string {
  if (!error) return "Unknown error";
  if (typeof error === "string") return error;
  const msg = error.message ?? error.detail ?? error.toString();
  return typeof msg === "string" ? msg : JSON.stringify(msg);
}

const HISTORY_PRESET_VALUES: Array<{ i18nKey: string; value: HistoryInitializationTask["preset"] }> = [
  { i18nKey: "histPreset1m", value: "1m" },
  { i18nKey: "histPreset1q", value: "1q" },
  { i18nKey: "histPreset1y", value: "1y" },
  { i18nKey: "histPreset3y", value: "3y" },
];

const HISTORY_STAGE_I18N: Record<string, string> = {
  prepare: "histStagePrepare",
  sync_bars: "histStageSyncBars",
  calc_scores: "histStageCalcScores",
  finalize: "histStageFinalize",
};

const HISTORY_STATUS_I18N: Record<string, string> = {
  idle: "histStatusIdle",
  running: "histStatusRunning",
  completed: "histStatusCompleted",
  failed: "histStatusFailed",
  cancelled: "histStatusCancelled",
};

const HISTORY_STATUS_COLORS: Record<string, string> = {
  idle: "default",
  running: "blue",
  completed: "green",
  failed: "red",
  cancelled: "orange",
};

function stageLabel(key: string): string {
  const i18nKey = HISTORY_STAGE_I18N[key];
  return i18nKey ? t(i18nKey) : key;
}

function statusLabel(key: string): string {
  const i18nKey = HISTORY_STATUS_I18N[key];
  return i18nKey ? t(i18nKey) : key;
}

function presetLabel(value: string): string {
  const found = HISTORY_PRESET_VALUES.find((item) => item.value === value);
  return found ? t(found.i18nKey) : value;
}

function repairModeLabel(mode?: "both" | "bars" | "scores"): string {
  if (mode === "bars") return t("diagRepairBars");
  if (mode === "scores") return t("diagRepairScores");
  return t("diagRepairBoth");
}

function repairModeColor(mode?: "both" | "bars" | "scores"): string {
  if (mode === "bars") return "orange";
  if (mode === "scores") return "purple";
  return "blue";
}

function repairModeHint(mode?: "both" | "bars" | "scores"): string {
  if (mode === "bars") return t("histModeExplainBars");
  if (mode === "scores") return t("histModeExplainScores");
  return t("histModeExplainBoth");
}

function isSkippedStage(stage: HistoryInitializationStage): boolean {
  return stage.status === "completed" && stage.total === 0 && (stage.message || "").toLowerCase().includes("skip");
}

function formatDuration(seconds?: number | null): string {
  if (seconds === null || seconds === undefined || Number.isNaN(Number(seconds))) return "-";
  const value = Math.max(0, Math.round(Number(seconds)));
  if (value < 60) return template("histDurationSec", { value });
  const minutes = Math.floor(value / 60);
  const remainSeconds = value % 60;
  if (minutes < 60) {
    return remainSeconds
      ? template("histDurationMinSec", { min: minutes, sec: remainSeconds })
      : template("histDurationMin", { value: minutes });
  }
  const hours = Math.floor(minutes / 60);
  const remainMinutes = minutes % 60;
  return remainMinutes
    ? template("histDurationHourMin", { hours, min: remainMinutes })
    : template("histDurationHour", { value: hours });
}

function deriveDurationSeconds(task?: Pick<HistoryInitializationTask, "duration_seconds" | "started_at" | "finished_at" | "status"> | null): number | null {
  if (!task) return null;
  if (task.duration_seconds !== null && task.duration_seconds !== undefined) {
    return Number(task.duration_seconds);
  }
  if (!task.started_at) return null;
  const started = new Date(task.started_at).getTime();
  const finished = task.finished_at ? new Date(task.finished_at).getTime() : task.status === "running" ? Date.now() : NaN;
  if (Number.isNaN(started) || Number.isNaN(finished)) return null;
  return Math.max(0, Math.round((finished - started) / 1000));
}

function formatDateRange(startDate?: string | null, endDate?: string | null): string {
  if (!startDate || !endDate) return "-";
  return `${startDate} ~ ${endDate}`;
}

function stagePercent(stage: HistoryInitializationStage): number {
  if (stage.total > 0) return Math.round((stage.done / stage.total) * 100);
  if (stage.status === "completed") return 100;
  return 0;
}

function stageProgressStatus(status: string): "success" | "exception" | "active" | "normal" {
  if (status === "failed" || status === "cancelled") return "exception";
  if (status === "completed") return "success";
  if (status === "running") return "active";
  return "normal";
}

function csvEscape(value: string | number | null | undefined): string {
  const text = value === null || value === undefined ? "" : String(value);
  return `"${text.replace(/"/g, '""')}"`;
}

type HistoryInitSectionProps = {
  context?: { symbolId?: number | null; symbolLabel?: string | null; repairMode?: "both" | "bars" | "scores" } | null;
  onClearContext?: () => void;
  focusSignal?: number;
  onOpenDiagnostic?: (symbolId: number) => void;
};

function formatScopedSymbolLabel(rawLabel?: string | null, symbol?: { symbol: string; name?: string | null } | null, symbolId?: number | null): string {
  const normalizedLabel = rawLabel?.trim();
  if (normalizedLabel) return normalizedLabel;
  if (symbol?.symbol) {
    return symbol.name ? `${symbol.symbol} | ${symbol.name}` : symbol.symbol;
  }
  if (symbolId !== null && symbolId !== undefined) return String(symbolId);
  return "-";
}

export default function HistoryInitSection({ focusSignal = 0, context = null, onClearContext, onOpenDiagnostic }: HistoryInitSectionProps) {
  const ctx = useApp();
  const [historyPreset, setHistoryPreset] = useState<HistoryInitializationTask["preset"]>("1y");
  const [historyTask, setHistoryTask] = useState<HistoryInitializationTask | null>(null);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [expandedRunId, setExpandedRunId] = useState<string | null>(null);
  const [cleanupKeep, setCleanupKeep] = useState(5);
  const [cleanupLoading, setCleanupLoading] = useState(false);
  const [cleanupMsg, setCleanupMsg] = useState<{ type: "success" | "error"; text: string } | null>(null);
  const [cancelMsg, setCancelMsg] = useState<{ type: "success" | "error"; text: string } | null>(null);
  const [retryFailedMsg, setRetryFailedMsg] = useState<{ type: "success" | "error"; text: string } | null>(null);
  const [retryFailedLoading, setRetryFailedLoading] = useState<string | null>(null);
  const [failedFilterKeyword, setFailedFilterKeyword] = useState("");
  const [failedFilterStage, setFailedFilterStage] = useState<"all" | "sync_bars" | "calc_scores">("all");
  const [failedCompareScope, setFailedCompareScope] = useState<"all" | "new_only">("all");
  const historyPollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const pollingInFlightRef = useRef(false);

  const panelRef = useRef<HTMLDivElement | null>(null);
  const presetFocusRef = useRef<HTMLDivElement | null>(null);
  const scopedSymbolId = context?.symbolId ?? null;
  const scopedSymbolLabel = context?.symbolLabel ?? null;
  const requestedRepairMode = context?.repairMode ?? "both";
  const getSymbolRecord = useCallback((symbolId?: number | null) => {
    if (symbolId == null) return null;
    if (ctx.detail?.symbol?.id === symbolId) return ctx.detail.symbol;
    return ctx.symbolDirectory[symbolId] ?? null;
  }, [ctx.detail, ctx.symbolDirectory]);
  const activeScopedSymbolId = scopedSymbolId ?? (historyTask?.symbol_ids?.length === 1 ? historyTask.symbol_ids[0] : null);
  const activeScopedSymbolLabel = useMemo(
    () => formatScopedSymbolLabel(scopedSymbolLabel, getSymbolRecord(activeScopedSymbolId), activeScopedSymbolId),
    [activeScopedSymbolId, getSymbolRecord, scopedSymbolLabel],
  );
  const focusFlashTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [focusFlash, setFocusFlash] = useState(false);
  const clearHistoryPolling = useCallback(() => {
    if (historyPollRef.current) {
      clearInterval(historyPollRef.current);
      historyPollRef.current = null;
    }
    pollingInFlightRef.current = false;
  }, []);

  const startHistoryPolling = useCallback(() => {
    clearHistoryPolling();
    historyPollRef.current = setInterval(async () => {
      if (pollingInFlightRef.current) return;
      pollingInFlightRef.current = true;
      try {
        const status = await api.getHistoryInitializationStatus() as HistoryInitializationTask;
        setHistoryTask(status);
        if (status.status !== "running") {
          clearHistoryPolling();
        }
      } catch {
        clearHistoryPolling();
      } finally {
        pollingInFlightRef.current = false;
      }
    }, 1200);
  }, [clearHistoryPolling]);

  const loadHistoryTask = useCallback(async () => {
    try {
      const status = await api.getHistoryInitializationStatus() as HistoryInitializationTask;
      setHistoryTask(status);
      if (status.preset) {
        setHistoryPreset(status.preset);
      }
      if (status.status === "running") {
        startHistoryPolling();
      }
    } catch {
      // noop
    }
  }, [startHistoryPolling]);

  useEffect(() => {
    loadHistoryTask();
  }, [loadHistoryTask]);

  useEffect(() => () => {
    clearHistoryPolling();
    if (focusFlashTimerRef.current) {
      clearTimeout(focusFlashTimerRef.current);
      focusFlashTimerRef.current = null;
    }
  }, [clearHistoryPolling]);

  useEffect(() => {
    if (!focusSignal) return;
    panelRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
    window.setTimeout(() => {
      presetFocusRef.current?.focus();
    }, 80);
    setFocusFlash(true);
    if (focusFlashTimerRef.current) {
      clearTimeout(focusFlashTimerRef.current);
    }
    focusFlashTimerRef.current = setTimeout(() => {
      setFocusFlash(false);
      focusFlashTimerRef.current = null;
    }, 1800);
  }, [focusSignal]);

  const runHistoryInitialization = async (preset: HistoryInitializationTask["preset"]) => {
    try {
      setHistoryLoading(true);
      const task = await api.startHistoryInitialization({ preset, adjust: "qfq", symbol_ids: activeScopedSymbolId ? [activeScopedSymbolId] : undefined, repair_mode: requestedRepairMode }) as HistoryInitializationTask;
      setHistoryPreset(preset);
      setHistoryTask(task);
      startHistoryPolling();
    } catch (error: any) {
      setHistoryTask((prev) => (
        prev
          ? { ...prev, status: "failed", message: safeErrorMessage(error) }
          : {
              status: "failed",
              preset,
              adjust: "qfq",
              progress_pct: 0,
              message: safeErrorMessage(error),
              stages: [],
              failed_items: [],
              summary: {
                symbols_total: 0,
                sync_ok_count: 0,
                sync_failed_count: 0,
                empty_count: 0,
                bars_rows: 0,
                score_days_total: 0,
                score_days_completed: 0,
              },
              recent_runs: [],
            }
      ));
    } finally {
      setHistoryLoading(false);
    }
  };

  const handleCancelTask = async () => {
    setCancelMsg(null);
    try {
      const result = await api.cancelHistoryInitialization() as HistoryInitializationTask;
      setHistoryTask(result);
      clearHistoryPolling();
      setCancelMsg({ type: "success", text: t("histCancelled") });
    } catch (error: any) {
      setCancelMsg({ type: "error", text: safeErrorMessage(error) });
    }
  };

  const handleRetryFailedOnly = async (taskId?: string | null) => {
    if (!taskId) {
      setRetryFailedMsg({ type: "error", text: t("histRetryFailedEmpty") });
      return;
    }
    setRetryFailedMsg(null);
    try {
      setRetryFailedLoading(taskId);
      const task = await api.retryHistoryInitializationFailed(taskId) as HistoryInitializationTask;
      setHistoryTask(task);
      setHistoryPreset(task.preset);
      startHistoryPolling();
      setRetryFailedMsg({ type: "success", text: t("histRetryFailedSuccess") });
    } catch (error: any) {
      setRetryFailedMsg({ type: "error", text: safeErrorMessage(error) });
    } finally {
      setRetryFailedLoading(null);
    }
  };

  const handleCleanup = async () => {
    try {
      setCleanupLoading(true);
      setCleanupMsg(null);
      const result = await api.cleanupHistoryRecords(cleanupKeep) as { deleted_count: number; kept: number; total: number };
      if (result.deleted_count > 0) {
        setCleanupMsg({
          type: "success",
          text: template("histCleanupSuccess", { deleted: result.deleted_count, keep: result.kept }),
        });
      } else {
        setCleanupMsg({ type: "success", text: t("histCleanupNoNeed") });
      }
      const status = await api.getHistoryInitializationStatus() as HistoryInitializationTask;
      setHistoryTask(status);
    } catch (error: any) {
      setCleanupMsg({ type: "error", text: safeErrorMessage(error) });
    } finally {
      setCleanupLoading(false);
    }
  };

  const toggleRunExpand = (runId: string) => {
    setExpandedRunId((prev) => (prev === runId ? null : runId));
  };

  const latestRun = useMemo<HistoryInitializationTask | HistoryInitializationRunRecord | null>(() => {
    if (historyTask?.task_id) return historyTask;
    return historyTask?.recent_runs?.[0] ?? null;
  }, [historyTask]);

  const recentRuns = historyTask?.recent_runs ?? [];
  const lastDuration = formatDuration(deriveDurationSeconds(latestRun));
  const latestFailure = latestRun?.status === "failed" || latestRun?.status === "cancelled" ? latestRun.message : null;
  const isRunning = historyTask?.status === "running";
  const scopedLookupKey = useMemo(() => {
    const ids = new Set<number>();
    if (activeScopedSymbolId != null) ids.add(activeScopedSymbolId);
    recentRuns.forEach((run) => {
      if ((run.symbol_ids?.length ?? 0) === 1 && run.symbol_ids?.[0] != null) {
        ids.add(run.symbol_ids[0]);
      }
    });
    return Array.from(ids).sort((a, b) => a - b).join(",");
  }, [activeScopedSymbolId, recentRuns]);

  useEffect(() => {
    if (!scopedLookupKey) return;
    const unresolvedIds = scopedLookupKey
      .split(",")
      .map((item) => Number(item))
      .filter((item) => Number.isFinite(item) && !getSymbolRecord(item));
    if (unresolvedIds.length === 0) return;
    ctx.fetchVisibleSymbols().catch(() => {});
  }, [ctx, getSymbolRecord, scopedLookupKey]);

  const getRunScopeLabel = useCallback((run?: Pick<HistoryInitializationRunRecord, "symbol_ids"> | Pick<HistoryInitializationTask, "symbol_ids"> | null) => {
    const symbolId = run?.symbol_ids?.length === 1 ? run.symbol_ids[0] : null;
    if (symbolId == null) return null;
    return formatScopedSymbolLabel(null, getSymbolRecord(symbolId), symbolId);
  }, [getSymbolRecord]);

  const handleStartHistoryInitialization = async () => {
    await runHistoryInitialization(historyPreset);
  };

  const handleRetryLastRun = async () => {
    const retryPreset = latestRun?.preset ?? historyPreset;
    await runHistoryInitialization(retryPreset);
  };

  const exportFailedItems = (run: HistoryInitializationRunRecord) => {
    const failedItems = run.failed_items ?? [];
    if (failedItems.length === 0) return;
    const rows = [
      [
        t("histFailedSymbol"),
        t("histFailedStage"),
        t("histFailedReason"),
        t("histFailedDays"),
        t("histFailedLastDate"),
      ].map((item) => csvEscape(item)).join(","),
      ...failedItems.map((item) => [
        item.name ? `${item.symbol} / ${item.name}` : item.symbol,
        stageLabel(item.stage),
        item.message,
        item.failed_days ?? "",
        item.last_trade_date ?? "",
      ].map((cell) => csvEscape(cell)).join(",")),
    ];
    const blob = new Blob(["\uFEFF", rows.join("\n")], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    const dateLabel = (run.started_at || new Date().toISOString()).slice(0, 10);
    anchor.href = url;
    anchor.download = `history-init-failed-${dateLabel}.csv`;
    document.body.appendChild(anchor);
    anchor.click();
    document.body.removeChild(anchor);
    URL.revokeObjectURL(url);
  };

  const renderHistoryStage = (stage: HistoryInitializationStage, compact = false) => {
    const percent = stagePercent(stage);
    const progressStatus = stageProgressStatus(stage.status);

    return (
      <div key={stage.key} className="history-init-stage" style={compact ? { marginBottom: 8 } : undefined}>
        <div className="history-init-stage__head">
          <span style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
            <strong>{stageLabel(stage.key)}</strong>
            {isSkippedStage(stage) ? <Tag>{t("histStageSkipped")}</Tag> : null}
          </span>
          <span>
            {isSkippedStage(stage)
              ? t("histStageSkipped")
              : stage.total > 0
                ? `${stage.done}/${stage.total}`
                : stage.status === "completed"
                  ? t("histStageCompleted")
                  : t("histStageWaiting")}
          </span>
        </div>
        <Progress percent={percent} size="small" status={progressStatus as any} showInfo={false} />
        {stage.message && <div className="history-init-stage__message">{stage.message}</div>}
      </div>
    );
  };

  const renderFailureItem = (item: HistoryInitializationFailureItem) => (
    <div key={`${item.symbol_id}-${item.stage}`} className="history-run-failed-item" style={{ padding: "8px 0", borderTop: "1px solid #f0f0f0" }}>
      <div style={{ display: "grid", gap: 6, gridTemplateColumns: "minmax(160px, 1.3fr) minmax(90px, 0.8fr) minmax(220px, 2fr) minmax(90px, 0.8fr) minmax(120px, 1fr) auto" }}>
        <span><strong>{item.symbol}</strong>{item.name ? ` / ${item.name}` : ""}</span>
        <span>{stageLabel(item.stage)}</span>
        <span>{item.message}</span>
        <span>{item.failed_days || "-"}</span>
        <span>{item.last_trade_date || "-"}</span>
        {onOpenDiagnostic && <Button size="small" type="link" onClick={() => onOpenDiagnostic(item.symbol_id)}>{t("diagTitle")}</Button>}
      </div>
    </div>
  );

  const failureItemCompareKey = (item: HistoryInitializationFailureItem) => `${item.symbol_id}:${item.stage}`;

  const renderRunDetails = (run: HistoryInitializationRunRecord, runIndex: number) => {
    const runId = run.task_id || `${run.started_at}-${run.preset}`;
    const expanded = expandedRunId === runId;
    const stages = run.stages;
    const failedItems = run.failed_items ?? [];
    const previousRun = recentRuns[runIndex + 1] ?? null;
    const previousFailureKeys = new Set((previousRun?.failed_items ?? []).map(failureItemCompareKey));
    const newFailedItems = failedItems.filter((item) => !previousFailureKeys.has(failureItemCompareKey(item)));
    const comparedFailedItems = failedCompareScope === "new_only" && previousRun ? newFailedItems : failedItems;
    const syncFailedItems = failedItems.filter((item) => item.stage === "sync_bars");
    const scoreFailedItems = failedItems.filter((item) => item.stage === "calc_scores");
    const normalizedKeyword = failedFilterKeyword.trim().toLowerCase();
    const filteredItems = comparedFailedItems.filter((item) => {
      const stageMatched = failedFilterStage === "all" || item.stage === failedFilterStage;
      if (!stageMatched) return false;
      if (!normalizedKeyword) return true;
      return [item.symbol, item.name ?? "", item.message]
        .join(" ")
        .toLowerCase()
        .includes(normalizedKeyword);
    });
    const filteredSyncFailedItems = filteredItems.filter((item) => item.stage === "sync_bars");
    const filteredScoreFailedItems = filteredItems.filter((item) => item.stage === "calc_scores");
    const hasFailureFilter = failedFilterStage !== "all" || normalizedKeyword.length > 0 || failedCompareScope !== "all";
    const retryable = failedItems.length > 0 && !isRunning;
    const runScopeLabel = getRunScopeLabel(run);

    return (
      <div key={runId} className="history-run-item">
        <div className="history-run-item__head">
          <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
            <strong>{presetLabel(run.preset) || run.preset}</strong>
            <Tag color={repairModeColor(run.repair_mode)}>{repairModeLabel(run.repair_mode)}</Tag>
            <span>{formatDateRange(run.start_date, run.end_date)}</span>
          </div>
          <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap", justifyContent: "flex-end" }}>
            <Tag color={HISTORY_STATUS_COLORS[run.status]}>{statusLabel(run.status)}</Tag>
            <Button size="small" onClick={() => handleRetryFailedOnly(run.task_id)} disabled={!retryable} loading={retryFailedLoading === run.task_id}>
              {t("histRetryFailedOnly")}
            </Button>
            <Button size="small" type="link" onClick={() => toggleRunExpand(runId)}>
              {expanded ? <><UpOutlined /> {t("histHideDetails")}</> : <><DownOutlined /> {t("histViewDetails")}</>}
            </Button>
          </div>
        </div>
        <div className="history-run-item__meta">
          <span>{template("histRunStarted", { date: formatDate(run.started_at) })}</span>
          <span>{template("histRunDuration", { duration: formatDuration(deriveDurationSeconds(run)) })}</span>
          <span>{template("histRunBarsOk", { count: run.summary.sync_ok_count })}</span>
          <span>{template("histRunScoreDone", { done: run.summary.score_days_completed, total: run.summary.score_days_total })}</span>
          <span><Tag color={repairModeColor(run.repair_mode)}>{repairModeLabel(run.repair_mode)}</Tag></span>
          {runScopeLabel && <span>{template("histScopeCurrentSymbol", { symbol: runScopeLabel })}</span>}
        </div>
        {run.message && (run.status === "failed" || run.status === "cancelled") && (
          <div className="history-run-item__message" style={{ color: "#cf1322" }}>{run.message}</div>
        )}
        {expanded && (
          <div className="history-run-item__details" style={{ marginTop: 12, padding: "12px 16px", background: "#fafafa", borderRadius: 6 }}>
            <div style={{ marginBottom: 12 }}>
              <div className="item-subline" style={{ marginBottom: 10 }}>{repairModeHint(run.repair_mode)}</div>
              {stages && stages.length > 0
                ? stages.map((stage) => renderHistoryStage(stage, true))
                : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t("histNoStageDetail")} />}
            </div>
            <div>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8, gap: 12, flexWrap: "wrap" }}>
                <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                  <strong>{t("histFailedItemsTitle")}</strong>
                  <Tag>{failedItems.length}</Tag>
                  <Tag color="orange">{`${t("histStageSyncBars")} ${syncFailedItems.length}`}</Tag>
                  <Tag color="purple">{`${t("histStageCalcScores")} ${scoreFailedItems.length}`}</Tag>
                  {previousRun && <Tag color="volcano">{template("histFailedNewCount", { count: newFailedItems.length })}</Tag>}
                </div>
                <Button size="small" icon={<DownloadOutlined />} onClick={() => exportFailedItems(run)} disabled={failedItems.length === 0}>
                  {t("histExportFailed")}
                </Button>
              </div>
              {failedItems.length > 0 ? (
                <div>
                  <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center", marginBottom: 12 }}>
                    <Input
                      allowClear
                      size="small"
                      value={failedFilterKeyword}
                      onChange={(e) => setFailedFilterKeyword(e.target.value)}
                      placeholder={t("histFailedSearchPlaceholder")}
                      style={{ width: 260, maxWidth: "100%" }}
                    />
                    <Radio.Group
                      size="small"
                      optionType="button"
                      buttonStyle="solid"
                      value={failedCompareScope}
                      onChange={(e) => setFailedCompareScope(e.target.value)}
                    >
                      <Radio.Button value="all">{t("histFailedCompareAll")}</Radio.Button>
                      <Radio.Button value="new_only">{t("histFailedCompareNew")}</Radio.Button>
                    </Radio.Group>
                    <Radio.Group
                      size="small"
                      optionType="button"
                      buttonStyle="solid"
                      value={failedFilterStage}
                      onChange={(e) => setFailedFilterStage(e.target.value)}
                    >
                      <Radio.Button value="all">{t("histFailedFilterAll")}</Radio.Button>
                      <Radio.Button value="sync_bars">{t("histFailedFilterSync")}</Radio.Button>
                      <Radio.Button value="calc_scores">{t("histFailedFilterScore")}</Radio.Button>
                    </Radio.Group>
                  </div>
                  <div style={{ display: "grid", gap: 6, gridTemplateColumns: "minmax(160px, 1.3fr) minmax(90px, 0.8fr) minmax(220px, 2fr) minmax(90px, 0.8fr) minmax(120px, 1fr)", paddingBottom: 8, color: "#8c8c8c", fontSize: 12 }}>
                    <span>{t("histFailedSymbol")}</span>
                    <span>{t("histFailedStage")}</span>
                    <span>{t("histFailedReason")}</span>
                    <span>{t("histFailedDays")}</span>
                    <span>{t("histFailedLastDate")}</span>
                  </div>
                  {filteredItems.length > 0 ? (
                    <div style={{ display: "grid", gap: 12 }}>
                      <div>
                        <div style={{ marginBottom: 6, display: "flex", alignItems: "center", gap: 8 }}>
                          <strong>{t("histStageSyncBars")}</strong>
                          <Tag color="orange">{filteredSyncFailedItems.length}</Tag>
                        </div>
                        {filteredSyncFailedItems.length > 0 ? filteredSyncFailedItems.map(renderFailureItem) : (
                          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={hasFailureFilter ? t("histNoFilteredFailedItems") : t("histNoFailedItems")} />
                        )}
                      </div>
                      <div>
                        <div style={{ marginBottom: 6, display: "flex", alignItems: "center", gap: 8 }}>
                          <strong>{t("histStageCalcScores")}</strong>
                          <Tag color="purple">{filteredScoreFailedItems.length}</Tag>
                        </div>
                        {filteredScoreFailedItems.length > 0 ? filteredScoreFailedItems.map(renderFailureItem) : (
                          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={hasFailureFilter ? t("histNoFilteredFailedItems") : t("histNoFailedItems")} />
                        )}
                      </div>
                    </div>
                  ) : (
                    <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={hasFailureFilter ? t("histNoFilteredFailedItems") : t("histNoFailedItems")} />
                  )}
                </div>
              ) : (
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t("histNoFailedItems")} />
              )}
            </div>
          </div>
        )}
      </div>
    );
  };

  return (
    <div className="settings-history-section">
      <div ref={panelRef} className="panel history-init-panel" style={focusFlash ? { boxShadow: "0 0 0 2px rgba(22, 119, 255, 0.18), 0 12px 28px rgba(22, 119, 255, 0.10)", transition: "box-shadow 0.2s ease" } : undefined}>
        <div className="panel-head">
          <div>
            <p className="panel-kicker">{t("histPanelKicker")}</p>
            <h2>{t("histPanelTitle")}</h2>
          </div>
          <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          <Tag color={repairModeColor(historyTask?.repair_mode ?? requestedRepairMode)}>{repairModeLabel(historyTask?.repair_mode ?? requestedRepairMode)}</Tag>
          <Tag color={HISTORY_STATUS_COLORS[historyTask?.status || "idle"]}>
            {statusLabel(historyTask?.status || "idle")}
          </Tag>
        </div>
        </div>

        <Alert type="info" showIcon style={{ marginBottom: 16 }} message={t("histPanelDesc")} />

        {activeScopedSymbolId && (
          <Alert
            type="success"
            showIcon
            style={{ marginBottom: 16 }}
            message={t("histScopePinned")}
            description={
              <div style={{ display: "flex", justifyContent: "space-between", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
                <span>{template("histScopeCurrentSymbol", { symbol: activeScopedSymbolLabel || String(activeScopedSymbolId) })}<Tag style={{ marginInlineStart: 8 }}>{repairModeLabel(requestedRepairMode)}</Tag></span>
                {onClearContext && scopedSymbolId ? <Button size="small" type="link" style={{ padding: 0, height: "auto" }} onClick={onClearContext}>{t("histClearScope")}</Button> : null}
              </div>
            }
          />
        )}

        <div ref={presetFocusRef} className="history-init-toolbar" tabIndex={-1} style={focusFlash ? { outline: "none", borderRadius: 8, background: "rgba(22, 119, 255, 0.06)", padding: 8, margin: "0 -8px 0 -8px", transition: "background 0.2s ease" } : undefined}>
          <Radio.Group value={historyPreset} onChange={(e) => setHistoryPreset(e.target.value)} optionType="button" buttonStyle="solid">
            {HISTORY_PRESET_VALUES.map((item) => (
              <Radio.Button key={item.value} value={item.value} disabled={isRunning}>{t(item.i18nKey)}</Radio.Button>
            ))}
          </Radio.Group>
          <div className="history-init-toolbar__actions">
            <Button icon={<ReloadOutlined />} onClick={loadHistoryTask} disabled={isRunning}>
              {t("histBtnRefresh")}
            </Button>
            <Button icon={<HistoryOutlined />} onClick={handleRetryLastRun} disabled={!latestRun || isRunning || historyLoading}>
              {t("histBtnRetry")}
            </Button>
            {isRunning ? (
              <Popconfirm title={t("histCancelConfirm")} onConfirm={handleCancelTask} okText={t("histBtnCancel")} cancelText={t("cancel")}>
                <Button danger icon={<CloseOutlined />}>
                  {t("histBtnCancel")}
                </Button>
              </Popconfirm>
            ) : (
              <Button type="primary" icon={<SyncOutlined spin={historyTask?.status === "running"} />} loading={historyLoading} disabled={isRunning} onClick={handleStartHistoryInitialization}>
                {t("histBtnInit")}
              </Button>
            )}
          </div>
        </div>

        <div className="history-init-overview-grid">
          <div className="history-init-overview-card">
            <span>{t("histLastRun")}</span>
            <strong>{formatDate(latestRun?.started_at)}</strong>
            <small>{latestRun ? `${presetLabel(latestRun.preset) || latestRun.preset} / ${repairModeLabel(latestRun.repair_mode)}` : t("histNoRecord")}</small>
          </div>
          <div className="history-init-overview-card">
            <span>{t("histDuration")}</span>
            <strong>{lastDuration}</strong>
            <small>{latestRun?.finished_at ? template("histEndedAt", { date: formatDate(latestRun.finished_at) }) : isRunning ? t("histTaskRunning") : "-"}</small>
          </div>
          <div className="history-init-overview-card">
            <span>{t("histRange")}</span>
            <strong>{formatDateRange(latestRun?.start_date, latestRun?.end_date)}</strong>
            <small>{latestRun ? t("histRangeDesc") : t("histRangeDefault")}</small>
          </div>
          <div className="history-init-overview-card">
            <span>{t("histLastResult")}</span>
            <strong>{latestRun ? statusLabel(latestRun.status) : t("histNotRun")}</strong>
            <small>{latestRun?.message || t("histKeepRecord")}</small>
          </div>
        </div>

        {cancelMsg && (
          <Alert type={cancelMsg.type} showIcon closable style={{ marginBottom: 16 }} message={cancelMsg.text} onClose={() => setCancelMsg(null)} />
        )}

        {retryFailedMsg && (
          <Alert type={retryFailedMsg.type} showIcon closable style={{ marginBottom: 16 }} message={retryFailedMsg.text} onClose={() => setRetryFailedMsg(null)} />
        )}

        {latestFailure && (
          <Alert type="error" showIcon style={{ marginBottom: 16 }} message={t("histLastFailed")} description={latestFailure} />
        )}

        {historyTask && historyTask.status !== "idle" && (
          <div className="history-init-progress-wrap">
            <div className="history-init-progress-head">
              <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                <strong>{t("histOverallProgress")}</strong>
                <Tag color={repairModeColor(historyTask.repair_mode)}>{repairModeLabel(historyTask.repair_mode)}</Tag>
              </div>
              <span>{historyTask.progress_pct}%</span>
            </div>
            <Progress percent={historyTask.progress_pct} status={historyTask.status === "failed" || historyTask.status === "cancelled" ? "exception" : historyTask.status === "completed" ? "success" : "active"} />
            {historyTask.message && <div className="history-init-status-text">{historyTask.message}</div>}
            <div className="item-subline" style={{ marginTop: 6 }}>{repairModeHint(historyTask.repair_mode)}</div>

            <div className="history-init-summary-grid">
              <div>
                <span>{t("histTotalSymbols")}</span>
                <strong>{historyTask.summary.symbols_total}</strong>
              </div>
              <div>
                <span>{t("histBarsSynced")}</span>
                <strong>{historyTask.summary.sync_ok_count}</strong>
              </div>
              <div>
                <span>{t("histBarsFailed")}</span>
                <strong>{historyTask.summary.sync_failed_count}</strong>
              </div>
              <div>
                <span>{t("histScoreCompleted")}</span>
                <strong>{historyTask.summary.score_days_completed}/{historyTask.summary.score_days_total}</strong>
              </div>
            </div>

            <div className="history-init-stage-list">
              {historyTask.stages.map((s) => renderHistoryStage(s))}
            </div>
          </div>
        )}
      </div>

      <div className="panel history-cleanup-panel" style={{ marginBottom: 16 }}>
        <div className="panel-head">
          <div>
            <p className="panel-kicker">{t("histCleanupTitle")}</p>
            <h2>{t("histCleanupTitle")}</h2>
          </div>
        </div>
        <p style={{ color: "#666", marginBottom: 12 }}>{t("histCleanupDesc")}</p>
        <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
          <span>{t("histCleanupKeep")}</span>
          <InputNumber min={1} max={50} value={cleanupKeep} onChange={(v) => setCleanupKeep(v ?? 5)} size="small" style={{ width: 80 }} />
          <Popconfirm title={template("histCleanupConfirm", { deleted: Math.max(0, recentRuns.length - cleanupKeep), keep: cleanupKeep })} onConfirm={handleCleanup} okText={t("histCleanupBtn")} cancelText={t("cancel")} disabled={recentRuns.length <= cleanupKeep}>
            <Button icon={<DeleteOutlined />} loading={cleanupLoading} disabled={recentRuns.length <= cleanupKeep} danger>
              {t("histCleanupBtn")}
            </Button>
          </Popconfirm>
        </div>
        {cleanupMsg && (
          <Alert type={cleanupMsg.type} showIcon style={{ marginTop: 12 }} message={cleanupMsg.text} closable onClose={() => setCleanupMsg(null)} />
        )}
      </div>

      <div className="panel history-run-panel">
        <div className="panel-head">
          <div>
            <p className="panel-kicker">{t("histRunLogKicker")}</p>
            <h2>{t("histRunLogTitle")}</h2>
          </div>
          <Tag>{template("histRunCount", { count: recentRuns.length })}</Tag>
        </div>

        {recentRuns.length === 0 ? (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t("histNoRunLog")} />
        ) : (
          <div className="history-run-list">
            {recentRuns.map((run, index) => renderRunDetails(run, index))}
          </div>
        )}
      </div>
    </div>
  );
}



