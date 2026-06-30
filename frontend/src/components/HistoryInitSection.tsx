import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Alert, Button, Empty, InputNumber, Popconfirm, Progress, Radio, Tag } from "antd";
import { CloseOutlined, DeleteOutlined, DownOutlined, HistoryOutlined, ReloadOutlined, SyncOutlined, UpOutlined } from "@ant-design/icons";
import { api } from "../api/client";
import { formatDate } from "../utils/format";
import { t, template } from "../i18n";
import type {
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
};

const HISTORY_STATUS_COLORS: Record<string, string> = {
  idle: "default",
  running: "blue",
  completed: "green",
  failed: "red",
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
  if (status === "failed") return "exception";
  if (status === "completed") return "success";
  if (status === "running") return "active";
  return "normal";
}

export default function HistoryInitSection() {
  const [historyPreset, setHistoryPreset] = useState<HistoryInitializationTask["preset"]>("1y");
  const [historyTask, setHistoryTask] = useState<HistoryInitializationTask | null>(null);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [expandedRunId, setExpandedRunId] = useState<string | null>(null);
  const [cleanupKeep, setCleanupKeep] = useState(5);
  const [cleanupLoading, setCleanupLoading] = useState(false);
  const [cleanupMsg, setCleanupMsg] = useState<{ type: "success" | "error"; text: string } | null>(null);
  const [cancelMsg, setCancelMsg] = useState<{ type: "success" | "error"; text: string } | null>(null);
  const historyPollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const pollingInFlightRef = useRef(false);

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
  }, [clearHistoryPolling]);

  const runHistoryInitialization = async (preset: HistoryInitializationTask["preset"]) => {
    try {
      setHistoryLoading(true);
      const task = await api.startHistoryInitialization({ preset, adjust: "qfq" }) as HistoryInitializationTask;
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
      // Refresh task list
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
  const latestFailure = latestRun?.status === "failed" ? latestRun.message : null;
  const isRunning = historyTask?.status === "running";

  const handleStartHistoryInitialization = async () => {
    await runHistoryInitialization(historyPreset);
  };

  const handleRetryLastRun = async () => {
    const retryPreset = latestRun?.preset ?? historyPreset;
    await runHistoryInitialization(retryPreset);
  };

  const renderHistoryStage = (stage: HistoryInitializationStage, compact = false) => {
    const percent = stagePercent(stage);
    const progressStatus = stageProgressStatus(stage.status);

    return (
      <div key={stage.key} className="history-init-stage" style={compact ? { marginBottom: 8 } : undefined}>
        <div className="history-init-stage__head">
          <strong>{stageLabel(stage.key)}</strong>
          <span>
            {stage.total > 0
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

  const renderRunDetails = (run: HistoryInitializationRunRecord) => {
    const runId = run.task_id || `${run.started_at}-${run.preset}`;
    const expanded = expandedRunId === runId;
    const stages = (run as any).stages as HistoryInitializationStage[] | undefined;

    return (
      <div key={runId} className="history-run-item">
        <div className="history-run-item__head">
          <div>
            <strong>{presetLabel(run.preset) || run.preset}</strong>
            <span>{formatDateRange(run.start_date, run.end_date)}</span>
          </div>
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <Tag color={HISTORY_STATUS_COLORS[run.status]}>{statusLabel(run.status)}</Tag>
            {stages && stages.length > 0 && (
              <Button size="small" type="link" onClick={() => toggleRunExpand(runId)}>
                {expanded ? <><UpOutlined /> {t("histRunCollapse")}</> : <><DownOutlined /> {t("histRunExpand")}</>}
              </Button>
            )}
          </div>
        </div>
        <div className="history-run-item__meta">
          <span>{template("histRunStarted", { date: formatDate(run.started_at) })}</span>
          <span>{template("histRunDuration", { duration: formatDuration(deriveDurationSeconds(run)) })}</span>
          <span>{template("histRunBarsOk", { count: run.summary.sync_ok_count })}</span>
          <span>{template("histRunScoreDone", { done: run.summary.score_days_completed, total: run.summary.score_days_total })}</span>
        </div>
        {run.message && run.status === "failed" && (
          <div className="history-run-item__message" style={{ color: "#cf1322" }}>{run.message}</div>
        )}
        {expanded && stages && stages.length > 0 && (
          <div className="history-run-item__details" style={{ marginTop: 12, padding: "12px 16px", background: "#fafafa", borderRadius: 6 }}>
            {stages.map((stage) => renderHistoryStage(stage, true))}
          </div>
        )}
      </div>
    );
  };

  return (
    <div className="settings-history-section">
      <div className="panel history-init-panel">
        <div className="panel-head">
          <div>
            <p className="panel-kicker">{t("histPanelKicker")}</p>
            <h2>{t("histPanelTitle")}</h2>
          </div>
          <Tag color={HISTORY_STATUS_COLORS[historyTask?.status || "idle"]}>
            {statusLabel(historyTask?.status || "idle")}
          </Tag>
        </div>

        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
          message={t("histPanelDesc")}
        />

        <div className="history-init-toolbar">
          <Radio.Group
            value={historyPreset}
            onChange={(e) => setHistoryPreset(e.target.value)}
            optionType="button"
            buttonStyle="solid"
          >
            {HISTORY_PRESET_VALUES.map((item) => (
              <Radio.Button key={item.value} value={item.value} disabled={isRunning}>{t(item.i18nKey)}</Radio.Button>
            ))}
          </Radio.Group>
          <div className="history-init-toolbar__actions">
            <Button icon={<ReloadOutlined />} onClick={loadHistoryTask} disabled={isRunning}>
              {t("histBtnRefresh")}
            </Button>
            <Button
              icon={<HistoryOutlined />}
              onClick={handleRetryLastRun}
              disabled={!latestRun || isRunning || historyLoading}
            >
              {t("histBtnRetry")}
            </Button>
            {isRunning ? (
              <Popconfirm
                title={t("histCancelConfirm")}
                onConfirm={handleCancelTask}
                okText={t("histBtnCancel")}
                cancelText={t("cancel")}
              >
                <Button danger icon={<CloseOutlined />}>
                  {t("histBtnCancel")}
                </Button>
              </Popconfirm>
            ) : (
              <Button
                type="primary"
                icon={<SyncOutlined spin={historyTask?.status === "running"} />}
                loading={historyLoading}
                disabled={isRunning}
                onClick={handleStartHistoryInitialization}
              >
                {t("histBtnInit")}
              </Button>
            )}
          </div>
        </div>

        <div className="history-init-overview-grid">
          <div className="history-init-overview-card">
            <span>{t("histLastRun")}</span>
            <strong>{formatDate(latestRun?.started_at)}</strong>
            <small>{latestRun ? presetLabel(latestRun.preset) || latestRun.preset : t("histNoRecord")}</small>
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
          <Alert
            type={cancelMsg.type}
            showIcon
            closable
            style={{ marginBottom: 16 }}
            message={cancelMsg.text}
            onClose={() => setCancelMsg(null)}
          />
        )}

        {latestFailure && (
          <Alert
            type="error"
            showIcon
            style={{ marginBottom: 16 }}
            message={t("histLastFailed")}
            description={latestFailure}
          />
        )}

        {historyTask && historyTask.status !== "idle" && (
          <div className="history-init-progress-wrap">
            <div className="history-init-progress-head">
              <strong>{t("histOverallProgress")}</strong>
              <span>{historyTask.progress_pct}%</span>
            </div>
            <Progress
              percent={historyTask.progress_pct}
              status={historyTask.status === "failed" ? "exception" : historyTask.status === "completed" ? "success" : "active"}
            />
            {historyTask.message && <div className="history-init-status-text">{historyTask.message}</div>}

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

      {/* Cleanup section */}
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
          <InputNumber
            min={1}
            max={50}
            value={cleanupKeep}
            onChange={(v) => setCleanupKeep(v ?? 5)}
            size="small"
            style={{ width: 80 }}
          />
          <Popconfirm
            title={template("histCleanupConfirm", { deleted: Math.max(0, recentRuns.length - cleanupKeep), keep: cleanupKeep })}
            onConfirm={handleCleanup}
            okText={t("histCleanupBtn")}
            cancelText={t("cancel")}
            disabled={recentRuns.length <= cleanupKeep}
          >
            <Button
              icon={<DeleteOutlined />}
              loading={cleanupLoading}
              disabled={recentRuns.length <= cleanupKeep}
              danger
            >
              {t("histCleanupBtn")}
            </Button>
          </Popconfirm>
        </div>
        {cleanupMsg && (
          <Alert
            type={cleanupMsg.type}
            showIcon
            style={{ marginTop: 12 }}
            message={cleanupMsg.text}
            closable
            onClose={() => setCleanupMsg(null)}
          />
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
            {recentRuns.map((run) => renderRunDetails(run))}
          </div>
        )}
      </div>
    </div>
  );
}
