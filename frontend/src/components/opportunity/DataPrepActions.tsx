import { useState, useCallback, useEffect, useRef } from "react";
import { Button, Space, Tag, Typography, Alert, Progress, Tooltip } from "antd";
import { DatabaseOutlined, SyncOutlined, RocketOutlined, LoadingOutlined } from "@ant-design/icons";
import { useApp } from "../../context/AppContext";
import { api } from "../../api/client";
import { t, template } from "../../i18n";
import { AsyncTaskRead, SnapshotStatusRead } from "../../types";
import { openSettingsSection } from "../../utils/navigate";

const { Text } = Typography;

interface DataPrepActionsProps {
  scope?: string;
  onPrepared?: (scope: string, taskId: string) => void;
  simpleMode?: boolean;
}

export function DataPrepActions({ scope = "cn-stock", onPrepared, simpleMode = false }: DataPrepActionsProps) {
  return <ScopedDataPrepActions key={scope} scope={scope} onPrepared={onPrepared} simpleMode={simpleMode} />;
}

function ScopedDataPrepActions({ scope = "cn-stock", onPrepared, simpleMode = false }: DataPrepActionsProps) {
  const ctx = useApp();
  const [preparing, setPreparing] = useState(false);
  const [snapshotStatus, setSnapshotStatus] = useState<SnapshotStatusRead | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [statusLoading, setStatusLoading] = useState(true);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [prepTask, setPrepTask] = useState<AsyncTaskRead | null>(null);
  const [taskReadError, setTaskReadError] = useState<string | null>(null);
  const [readAttempt, setReadAttempt] = useState(0);
  const [waitForSyncId, setWaitForSyncId] = useState<string | null>(null);
  const mounted = useRef(true);
  const statusRequest = useRef(0);
  const trackedTask = useRef<string | null>(null);
  const notifiedTask = useRef<string | null>(null);
  const starting = useRef(false);
  const onPreparedRef = useRef(onPrepared);
  onPreparedRef.current = onPrepared;
  const prepActive = !!prepTask && ["queued", "running"].includes(prepTask.status);
  const prepProcessed = prepTask?.processed ?? 0;
  const prepTotal = prepTask?.total ?? 0;
  const prepPercent = prepTotal > 0 ? Math.max(0, Math.min(100, Math.round(prepProcessed * 100 / prepTotal))) : 0;
  const scanCompleted = (prepTask?.result?.fast_scan as { status?: string } | undefined)?.status === "ok";
  const hasIntradayData = prepTask?.result?.source_data_status === "intraday";
  const prepOutcomeKey = prepActive ? "opportunity.dataPrepRunning"
    : prepTask?.status === "done" ? (scanCompleted ? "opportunity.prepCompleted" : "opportunity.prepScanPending")
    : prepTask?.status === "partial" ? (hasIntradayData
      ? (scanCompleted ? "opportunity.prepIntraday" : "opportunity.prepIntradayPending")
      : (scanCompleted ? "opportunity.prepPartial" : "opportunity.prepIncomplete"))
    : prepTask?.status === "cancelled" ? "opportunity.prepCancelled" : "opportunity.dataPrepFailed";
  const busy = preparing || prepActive || !!taskReadError;

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; statusRequest.current += 1; };
  }, []);

  const syncTask = ctx.syncTask as any;
  const isSyncing = !!syncTask && ["queued", "running"].includes(syncTask.status);
  const syncStage = syncTask?.stage ?? "";
  const syncMessage = syncTask?.message ?? "";
  const syncProcessed = syncTask?.processed ?? 0;
  const syncTotal = syncTask?.total ?? 0;
  const syncPercent = syncTotal > 0 ? Math.max(0, Math.min(100, Math.round(syncProcessed * 100 / syncTotal))) : 0;

  const refreshStatus = useCallback(async () => {
    const request = ++statusRequest.current;
    const current = () => mounted.current && request === statusRequest.current;
    setStatusLoading(true);
    setStatusError(null);
    try {
      const status = await api.getSnapshotStatus(scope);
      if (!current()) return;
      setSnapshotStatus(status);
      if (!starting.current && !trackedTask.current && status.last_data_prep_task_id && status.last_data_prep_status) {
        trackedTask.current = status.last_data_prep_task_id;
        setPrepTask({ id: status.last_data_prep_task_id, status: status.last_data_prep_status } as AsyncTaskRead);
      }
    } catch (e: any) {
      if (current()) setStatusError(e?.message || t("opportunity.snapshotReadFailed"));
    } finally {
      if (current()) setStatusLoading(false);
    }
  }, [scope]);

  useEffect(() => {
    void refreshStatus();
  }, [refreshStatus]);

  useEffect(() => {
    if (!prepTask?.id) return;
    const taskId = prepTask.id;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const current = () => !cancelled && mounted.current && trackedTask.current === taskId;
    const poll = async () => {
      try {
        const task = await api.getAsyncTask(taskId);
        if (!current()) return;
        setTaskReadError(null);
        setPrepTask(task);
        if (["queued", "running"].includes(task.status)) {
          timer = setTimeout(() => void poll(), 3000);
          return;
        }
        await refreshStatus();
        if (!current()) return;
        const scan = task.result?.fast_scan as { status?: string } | undefined;
        if (["done", "partial"].includes(task.status) && scan?.status === "ok" && notifiedTask.current !== taskId) {
          notifiedTask.current = taskId;
          onPreparedRef.current?.(scope, taskId);
        }
      } catch (e: any) {
        if (current()) setTaskReadError(e?.message || t("opportunity.prepTaskReadFailed"));
      }
    };
    void poll();
    return () => { cancelled = true; if (timer) clearTimeout(timer); };
  }, [prepTask?.id, readAttempt, refreshStatus, scope]);

  const handleGoToMarketData = useCallback(() => {
    try { localStorage.setItem("settings_data_center_tab", "market"); } catch { /* optional persistence */ }
    openSettingsSection("data-center");
    ctx.setActiveTab("settings");
    ctx.showToast("info", t("opportunity.navigatingToMarketData"));
  }, [ctx]);

  const handleRunSync = useCallback(() => {
    if (isSyncing || busy) return;
    ctx.runSync();
  }, [ctx, isSyncing, busy]);

  const handleTaskDetails = useCallback(() => {
    openSettingsSection("tasks");
    ctx.setActiveTab("settings");
  }, [ctx]);

  const handleAutoScan = useCallback(async () => {
    if (busy || starting.current) return;
    if (isSyncing) {
      ctx.showToast("info", t("opportunity.syncInProgressWait"));
      return;
    }
    setPreparing(true);
    starting.current = true;
    setError(null);
    try {
      const task = await api.startDataPrep({
        scope,
        trigger_fast_scan_after_ready: true,
      });
      if (!mounted.current) return;
      trackedTask.current = task.id;
      setTaskReadError(null);
      setPrepTask(task);
      ctx.showToast("info", t("opportunity.dataPrepRunning"));
      await refreshStatus();
    } catch (e: any) {
      if (!mounted.current) return;
      setError(e?.message || t("opportunity.dataPrepFailed"));
      ctx.showToast("error", e?.message || t("opportunity.dataPrepFailed"));
    } finally {
      starting.current = false;
      if (mounted.current) setPreparing(false);
    }
  }, [ctx, scope, refreshStatus, isSyncing, busy]);

  useEffect(() => {
    if (!waitForSyncId || isSyncing) return;
    if (syncTask?.id !== waitForSyncId || syncTask?.status !== "done") {
      setWaitForSyncId(null);
      setError(t("opportunity.syncStopped"));
      return;
    }
    if (busy || statusLoading || statusError) return;
    setWaitForSyncId(null);
    void handleAutoScan();
  }, [waitForSyncId, isSyncing, syncTask?.id, syncTask?.status, busy, statusLoading, statusError, handleAutoScan]);

  const statusTag = (() => {
    if (statusLoading) return <Tag icon={<LoadingOutlined />} color="processing">{t("opportunity.snapshotLoading")}</Tag>;
    if (statusError) return null;
    if (busy || waitForSyncId) return <Tag icon={<LoadingOutlined />} color="processing">{t("opportunity.dataPrepRunning")}</Tag>;
    if (!snapshotStatus) return null;
    if (snapshotStatus.has_building_snapshot || snapshotStatus.last_data_prep_status === "running") {
      return <Tag icon={<LoadingOutlined />} color="processing">{t("opportunity.dataPrepRunning")}</Tag>;
    }
    if ((prepTask?.status ?? snapshotStatus.last_data_prep_status) === "cancelled") {
      return <Tag color="orange">{t("opportunity.prepCancelled")}</Tag>;
    }
    if ((prepTask?.status ?? snapshotStatus.last_data_prep_status) === "failed") {
      return <Tag color="error">{t("opportunity.dataPrepFailed")}</Tag>;
    }
    if (snapshotStatus.has_ready_snapshot) {
      const cutoff = snapshotStatus.ready_snapshot_data_cutoff_at || snapshotStatus.ready_snapshot_trade_date;
      const builtAt = snapshotStatus.ready_snapshot_generated_at;
      return <Tooltip title={builtAt ? `${t("opportunity.snapshotBuiltAt")}: ${builtAt}` : undefined}>
        <Tag color={cutoff ? "success" : "warning"}>
          {t("opportunity.dataPrepReady")} · {cutoff
            ? `${t("opportunity.snapshotDataAsOf")}: ${cutoff.slice(0, 10)}`
            : t("opportunity.snapshotDataDateUnknown")}
        </Tag>
      </Tooltip>;
    }
    return <Tag color="default">{t("opportunity.dataPrepIdle")}</Tag>;
  })();

  const stageLabel = (() => {
    switch (syncStage) {
      case "prepare": return t("opportunity.syncStagePrepare");
      case "sync": return t("opportunity.syncStageSync");
      case "score": return t("opportunity.syncStageScore");
      case "scan": return t("opportunity.syncStageScan");
      case "done": return t("opportunity.syncStageDone");
      default: return syncStage;
    }
  })();

  if (simpleMode) {
    const retryRead = !!statusError || !!taskReadError;
    const actionLabel = retryRead ? t("opportunity.retry")
      : statusLoading ? t("opportunity.readingData")
      : busy || waitForSyncId ? t("opportunity.updatingAndScreening") : t("opportunity.updateAndScreen");
    const retryOrStart = () => {
      if (taskReadError) {
        setTaskReadError(null);
        setReadAttempt(value => value + 1);
      } else if (statusError) {
        void refreshStatus();
      } else if (isSyncing) {
        setError(null);
        setWaitForSyncId(syncTask.id);
      } else {
        void handleAutoScan();
      }
    };
    const notice = taskReadError ? t("opportunity.prepTaskReadFailed")
      : statusError ? t("opportunity.snapshotReadFailed")
      : error ? (error === t("opportunity.syncStopped") ? error : t("opportunity.dataPrepFailed"))
      : waitForSyncId ? t("opportunity.backgroundSyncQueued")
      : isSyncing ? t("opportunity.backgroundSyncWait")
      : prepTask ? t(prepOutcomeKey) : t("opportunity.firstUseHint");
    const noticeType = retryRead || error || prepTask?.status === "failed" ? "error"
      : isSyncing || prepActive || !prepTask ? "info"
      : prepTask.status === "done" && scanCompleted ? "success" : "warning";
    return (
      <section aria-label={t("opportunity.dataPrepTitle")} style={{ marginBottom: 12, padding: 12, border: "1px solid var(--border-color, #d9d9d9)", borderRadius: 8 }}>
        <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 8, marginBottom: 8 }}>
          <Text strong>{t("opportunity.dataPrepTitle")}</Text>
          {statusTag}
          <Tooltip title={t("opportunity.updateAndScreenTip")}>
            <Button type="primary" aria-label={actionLabel} onClick={retryOrStart}
              loading={!retryRead && (statusLoading || busy || !!waitForSyncId)} disabled={!retryRead && (statusLoading || busy || !!waitForSyncId)}>
              {actionLabel}
            </Button>
          </Tooltip>
        </div>
        <Alert
          type={noticeType}
          message={notice}
          showIcon
          action={
            noticeType === "warning" && !busy && !isSyncing ? (
              <Button size="small" onClick={retryOrStart}>{t("opportunity.continueScan")}</Button>
            ) : undefined
          }
        />
        {(prepActive || isSyncing) && <div className="data-prep-progress">
          <Progress percent={prepActive ? prepPercent : syncPercent} size="small" />
          <Text type="secondary">{prepActive ? `${prepProcessed}/${prepTotal}` : `${syncProcessed}/${syncTotal}`}</Text>
        </div>}
        <details style={{ marginTop: 8 }}>
          <summary style={{ cursor: "pointer" }}>{t("opportunity.showDetails")}</summary>
          <Space direction="vertical" style={{ marginTop: 8, width: "100%" }}>
            {(statusError || taskReadError || error) && <Text type="danger">{statusError || taskReadError || error}</Text>}
            {prepTask && <>
              <Text>{(prepActive && prepTask.current_step_description) || prepTask.message || prepTask.stage}</Text>
              {(prepTask.errors ?? []).slice(0, 3).map((entry, index) => <Text type="danger" key={index}>{String(entry.error || entry.message || "")}</Text>)}
              <Button onClick={handleTaskDetails}>{t("opportunity.prepTaskDetails")}</Button>
            </>}
            {syncTask && <Text>{stageLabel}{syncMessage && ` · ${syncMessage}`}{syncTotal > 0 && ` (${syncProcessed}/${syncTotal})`}</Text>}
            <Button onClick={handleGoToMarketData}>{t("opportunity.dataSettings")}</Button>
          </Space>
        </details>
      </section>
    );
  }

  return (
    <div style={{ marginBottom: 12, padding: 12, background: "#fafafa", borderRadius: 4 }}>
      <Space direction="vertical" style={{ width: "100%" }}>
        <Space align="center">
          <Text strong>{t("opportunity.dataPrepTitle")}</Text>
          {statusTag}
        </Space>
        {statusError && (
          <Alert type="error" showIcon message={t("opportunity.snapshotReadFailed")} description={statusError}
            action={<Button onClick={() => void refreshStatus()}>{t("opportunity.retryStatusRead")}</Button>} />
        )}
        {taskReadError && (
          <Alert type="error" showIcon message={t("opportunity.prepTaskReadFailed")} description={taskReadError}
            action={<Button onClick={() => { setTaskReadError(null); setReadAttempt(value => value + 1); }}>{t("opportunity.retryTaskRead")}</Button>} />
        )}
        {prepTask && (
          <Alert showIcon
            action={<Button onClick={handleTaskDetails}>{t("opportunity.prepTaskDetails")}</Button>}
            type={prepActive ? "info" : prepTask.status === "done" && scanCompleted ? "success" : ["done", "partial", "cancelled"].includes(prepTask.status) ? "warning" : "error"}
            message={t(prepOutcomeKey)}
            description={<Space direction="vertical" style={{ width: "100%" }}>
              {prepActive && <>
                <Progress percent={prepPercent} size="small" status="active" />
                <Text type="secondary">{prepProcessed}/{prepTotal}</Text>
              </>}
              <Text>{(prepActive && prepTask.current_step_description) || prepTask.message || prepTask.stage}</Text>
              {(prepTask.errors ?? []).slice(0, 3).map((entry, index) => (
                <Text type="danger" key={index}>{String(entry.error || entry.message || "")}</Text>
              ))}
            </Space>} />
        )}

        {isSyncing && (
          <Alert
            type="info"
            showIcon
            icon={<SyncOutlined spin />}
            message={t("opportunity.syncInProgress")}
            description={
              <Space direction="vertical" size={4} style={{ marginTop: 4 }}>
                <Progress
                  percent={Math.round(syncPercent)}
                  size="small"
                  status="active"
                  strokeColor={{ from: "#1677ff", to: "#52c41a" }}
                />
                <Text type="secondary" style={{ fontSize: 12 }}>
                  {stageLabel}
                  {syncMessage && ` · ${syncMessage}`}
                  {syncTotal > 0 && ` (${syncProcessed}/${syncTotal})`}
                </Text>
              </Space>
            }
          />
        )}

        {!isSyncing && syncTask && syncTask.status === "done" && (
          <Alert
            type="success"
            showIcon
            message={t("opportunity.syncCompleted")}
            description={
              <Text type="secondary" style={{ fontSize: 12 }}>
                {template("opportunity.syncSummaryMsg", {
                  ok: syncTask.result?.ok_count ?? 0,
                  total: syncTask.result?.symbols_total ?? syncTask.total ?? 0,
                  failed: syncTask.result?.failed_count ?? 0,
                })}
              </Text>
            }
          />
        )}

        {!isSyncing && syncTask && syncTask.status === "failed" && (
          <Alert
            type="error"
            showIcon
            message={t("opportunity.syncFailed")}
            description={
              <Text type="secondary" style={{ fontSize: 12 }}>
                {syncTask.message || t("opportunity.syncFailedDetail")}
              </Text>
            }
          />
        )}

        <Space wrap>
          <Tooltip title={t("opportunity.goToMarketDataTip")}>
            <Button icon={<DatabaseOutlined />} onClick={handleGoToMarketData}>
              {t("opportunity.goToMarketData")}
            </Button>
          </Tooltip>
          <Tooltip title={isSyncing ? t("opportunity.syncInProgress") : t("opportunity.runIncrementalSyncTip")}>
            <Button
              icon={isSyncing ? <LoadingOutlined /> : <SyncOutlined />}
              loading={isSyncing}
              disabled={isSyncing || busy}
              onClick={handleRunSync}
            >
              {isSyncing ? t("opportunity.syncInProgress") : t("opportunity.runIncrementalSync")}
            </Button>
          </Tooltip>
          <Tooltip title={isSyncing ? t("opportunity.waitForSyncToScan") : t("opportunity.autoScanTip")}>
            <Button
              type="primary"
              icon={busy ? <LoadingOutlined /> : <RocketOutlined />}
              loading={busy}
              disabled={isSyncing || busy}
              onClick={handleAutoScan}
            >
              {t("opportunity.autoScanWhenReady")}
            </Button>
          </Tooltip>
        </Space>
        {error && <Alert type="error" message={error} showIcon closable onClose={() => setError(null)} />}
        {snapshotStatus && !snapshotStatus.has_ready_snapshot && !snapshotStatus.has_building_snapshot && !busy && !prepTask && !error && !statusLoading && !statusError && (
          <Alert
            type="warning"
            showIcon
            message="还没有评分快照，建议先初始化数据"
            description={
              <Space direction="vertical" size={4} style={{ marginTop: 4 }}>
                <Text type="secondary">首次使用流程：</Text>
                <Text>1. 先点击「前往基础数据」，执行 <Text strong>智能同步</Text>，补齐全市场尾部 K 线；</Text>
                <Text>2. 同步完成后再点击「数据就绪后自动扫描」，系统会自动生成首条评分快照，机会中心即出现候选。</Text>
              </Space>
            }
          />
        )}
        {snapshotStatus?.recommended_action && !error && !statusError && !busy && (
          <Alert type="info" message={snapshotStatus.recommended_action} showIcon />
        )}
      </Space>
    </div>
  );
}
