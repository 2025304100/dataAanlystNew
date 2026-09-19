import { useState, useCallback, useEffect } from "react";
import { Button, Space, Tag, Typography, Alert, Progress, Tooltip } from "antd";
import { DatabaseOutlined, SyncOutlined, RocketOutlined, LoadingOutlined } from "@ant-design/icons";
import { useApp } from "../../context/AppContext";
import { api } from "../../api/client";
import { t, template } from "../../i18n";
import { SnapshotStatusRead } from "../../types";

const { Text } = Typography;

export function DataPrepActions({ scope = "cn-stock" }: { scope?: string }) {
  const ctx = useApp();
  const [preparing, setPreparing] = useState(false);
  const [snapshotStatus, setSnapshotStatus] = useState<SnapshotStatusRead | null>(null);
  const [error, setError] = useState<string | null>(null);

  const syncTask = ctx.syncTask as any;
  const isSyncing = !!syncTask && ["queued", "running"].includes(syncTask.status);
  const syncPercent = syncTask?.percent ?? 0;
  const syncStage = syncTask?.stage ?? "";
  const syncMessage = syncTask?.message ?? "";
  const syncProcessed = syncTask?.processed ?? 0;
  const syncTotal = syncTask?.total ?? 0;

  const refreshStatus = useCallback(async () => {
    try {
      const status = await api.getSnapshotStatus(scope);
      setSnapshotStatus(status);
      setError(null);
    } catch (e: any) {
      console.warn("getSnapshotStatus failed", e);
    }
  }, [scope]);

  useEffect(() => {
    void refreshStatus();
  }, [refreshStatus]);

  const handleGoToMarketData = useCallback(() => {
    ctx.setActiveTab("macro");
    ctx.showToast("info", t("opportunity.navigatingToMarketData"));
  }, [ctx]);

  const handleRunSync = useCallback(() => {
    if (isSyncing) return;
    ctx.runSync();
  }, [ctx, isSyncing]);

  const handleAutoScan = useCallback(async () => {
    if (isSyncing) {
      ctx.showToast("info", t("opportunity.syncInProgressWait"));
      return;
    }
    setPreparing(true);
    setError(null);
    try {
      await api.startDataPrep({
        scope,
        trigger_fast_scan_after_ready: true,
      });
      ctx.showToast("info", t("opportunity.dataPrepRunning"));
      await refreshStatus();
    } catch (e: any) {
      setError(e.message || t("opportunity.dataPrepFailed"));
      ctx.showToast("error", e.message || t("opportunity.dataPrepFailed"));
    } finally {
      setPreparing(false);
    }
  }, [ctx, scope, refreshStatus, isSyncing]);

  const statusTag = (() => {
    if (!snapshotStatus) return null;
    if (snapshotStatus.has_building_snapshot || snapshotStatus.last_data_prep_status === "running") {
      return <Tag icon={<LoadingOutlined />} color="processing">{t("opportunity.dataPrepRunning")}</Tag>;
    }
    if (snapshotStatus.has_ready_snapshot && snapshotStatus.ready_snapshot_generated_at) {
      return <Tag color="success">{t("opportunity.dataPrepReady")}: {snapshotStatus.ready_snapshot_generated_at.slice(0, 10)}</Tag>;
    }
    if (snapshotStatus.last_data_prep_status === "failed") {
      return <Tag color="error">{t("opportunity.dataPrepFailed")}</Tag>;
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

  return (
    <div style={{ marginBottom: 12, padding: 12, background: "#fafafa", borderRadius: 4 }}>
      <Space direction="vertical" style={{ width: "100%" }}>
        <Space align="center">
          <Text strong>{t("opportunity.dataPrepTitle")}</Text>
          {statusTag}
        </Space>

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
              disabled={isSyncing}
              onClick={handleRunSync}
            >
              {isSyncing ? t("opportunity.syncInProgress") : t("opportunity.runIncrementalSync")}
            </Button>
          </Tooltip>
          <Tooltip title={isSyncing ? t("opportunity.waitForSyncToScan") : t("opportunity.autoScanTip")}>
            <Button
              type="primary"
              icon={preparing ? <LoadingOutlined /> : <RocketOutlined />}
              loading={preparing}
              disabled={isSyncing}
              onClick={handleAutoScan}
            >
              {t("opportunity.autoScanWhenReady")}
            </Button>
          </Tooltip>
        </Space>
        {error && <Alert type="error" message={error} showIcon closable onClose={() => setError(null)} />}
        {!snapshotStatus && !error && (
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
        {snapshotStatus?.recommended_action && !error && (
          <Alert type="info" message={snapshotStatus.recommended_action} showIcon />
        )}
      </Space>
    </div>
  );
}
