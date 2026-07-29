import { useState, useCallback, useEffect } from "react";
import { Button, Space, Tag, Typography, Alert } from "antd";
import { DatabaseOutlined, SyncOutlined, RocketOutlined, LoadingOutlined } from "@ant-design/icons";
import { useApp } from "../../context/AppContext";
import { api } from "../../api/client";
import { t } from "../../i18n";
import { SnapshotStatusRead } from "../../types";

const { Text } = Typography;

export function DataPrepActions({ scope = "cn-stock" }: { scope?: string }) {
  const ctx = useApp();
  const [preparing, setPreparing] = useState(false);
  const [snapshotStatus, setSnapshotStatus] = useState<SnapshotStatusRead | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refreshStatus = useCallback(async () => {
    try {
      const status = await api.getSnapshotStatus(scope);
      setSnapshotStatus(status);
      setError(null);
    } catch (e: any) {
      // 静默失败，不弹 toast
      console.warn("getSnapshotStatus failed", e);
    }
  }, [scope]);

  // 首次挂载加载一次状态
  useEffect(() => {
    void refreshStatus();
  }, [refreshStatus]);

  const handleGoToMarketData = useCallback(() => {
    ctx.setActiveTab("macro");
  }, [ctx]);

  const handleRunSync = useCallback(() => {
    ctx.runSync();
  }, [ctx]);

  const handleAutoScan = useCallback(async () => {
    setPreparing(true);
    setError(null);
    try {
      await api.startDataPrep({
        scope,
        trigger_fast_scan_after_ready: true,
      });
      ctx.showToast("info", t("opportunity.dataPrepRunning"));
      // 刷新状态
      await refreshStatus();
    } catch (e: any) {
      setError(e.message || t("opportunity.dataPrepFailed"));
      ctx.showToast("error", e.message || t("opportunity.dataPrepFailed"));
    } finally {
      setPreparing(false);
    }
  }, [ctx, scope, refreshStatus]);

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

  return (
    <div style={{ marginBottom: 12, padding: 12, background: "#fafafa", borderRadius: 4 }}>
      <Space direction="vertical" style={{ width: "100%" }}>
        <Space align="center">
          <Text strong>{t("opportunity.dataPrepTitle")}</Text>
          {statusTag}
        </Space>
        <Space wrap>
          <Button icon={<DatabaseOutlined />} onClick={handleGoToMarketData}>
            {t("opportunity.goToMarketData")}
          </Button>
          <Button icon={<SyncOutlined />} onClick={handleRunSync}>
            {t("opportunity.runIncrementalSync")}
          </Button>
          <Button
            type="primary"
            icon={preparing ? <LoadingOutlined /> : <RocketOutlined />}
            loading={preparing}
            onClick={handleAutoScan}
          >
            {t("opportunity.autoScanWhenReady")}
          </Button>
        </Space>
        {error && <Alert type="error" message={error} showIcon closable onClose={() => setError(null)} />}
        {snapshotStatus?.recommended_action && !error && (
          <Alert type="info" message={snapshotStatus.recommended_action} showIcon />
        )}
      </Space>
    </div>
  );
}
