import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  Button,
  Empty,
  Input,
  InputNumber,
  Modal,
  Progress,
  Space,
  Switch,
  Table,
  Tag,
  Tooltip,
} from "antd";
import {
  CheckCircleOutlined,
  EyeOutlined,
  PlayCircleOutlined,
  ReloadOutlined,
  RollbackOutlined,
  StopOutlined,
} from "@ant-design/icons";
import {
  api,
  type FactorModelRun,
  type FactorOverview,
  type FactorPipelineTask,
  type FactorWeightMode,
} from "../api/client";
import { useApp } from "../context/AppContext";
import { t, template } from "../i18n";

const TERMINAL_TASK_STATES = new Set(["done", "completed", "failed", "cancelled"]);

function shortId(value: string | null | undefined) {
  if (!value) return "-";
  return value.length > 18 ? `${value.slice(0, 10)}...${value.slice(-6)}` : value;
}

function formatDateTime(value: string | null | undefined) {
  if (!value) return "-";
  return value.replace("T", " ").slice(0, 19);
}

function modeColor(mode: FactorWeightMode | string) {
  if (mode === "ridge") return "green";
  if (mode === "shadow") return "blue";
  return "default";
}

export default function FactorModelSettings() {
  const ctx = useApp();
  const [overview, setOverview] = useState<FactorOverview | null>(null);
  const [models, setModels] = useState<FactorModelRun[]>([]);
  const [activeTask, setActiveTask] = useState<FactorPipelineTask | null>(null);
  const [loading, setLoading] = useState(true);
  const [acting, setActing] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [fullRefresh, setFullRefresh] = useState(false);
  const [trainModel, setTrainModel] = useState(true);
  const [materializeScores, setMaterializeScores] = useState(true);
  const [windowDays, setWindowDays] = useState(250);
  const [validationDays, setValidationDays] = useState(50);
  const [fallbackOpen, setFallbackOpen] = useState(false);
  const [fallbackReason, setFallbackReason] = useState("");

  const loadAll = useCallback(async (showLoading = true) => {
    if (showLoading) setLoading(true);
    setError(null);
    try {
      const [overviewData, modelData, tasks] = await Promise.all([
        api.getFactorOverview(),
        api.getFactorModels(undefined, 20),
        api.listFactorPipelineTasks(5),
      ]);
      setOverview(overviewData);
      setModels(modelData.items);
      setActiveTask(tasks[0] ?? null);
    } catch (err: any) {
      setError(err.message || t("factorModelLoadFailed"));
    } finally {
      if (showLoading) setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadAll();
  }, [loadAll]);

  useEffect(() => {
    if (!activeTask || TERMINAL_TASK_STATES.has(activeTask.status)) return;
    const timer = window.setInterval(async () => {
      try {
        const task = await api.getFactorPipelineTask(activeTask.id);
        setActiveTask(task);
        if (TERMINAL_TASK_STATES.has(task.status)) {
          await loadAll(false);
        }
      } catch (err: any) {
        setError(err.message || t("factorModelLoadFailed"));
      }
    }, 2000);
    return () => window.clearInterval(timer);
  }, [activeTask?.id, activeTask?.status, loadAll]);

  const averageCoverage = useMemo(() => {
    const rows = overview?.factor_coverage ?? [];
    if (!rows.length) return null;
    return rows.reduce((sum, item) => sum + Number(item.coverage || 0), 0) / rows.length;
  }, [overview]);

  const startPipeline = async () => {
    setActing("pipeline");
    setError(null);
    try {
      const task = await api.createFactorPipelineTask({
        full_refresh: fullRefresh,
        train_model: trainModel,
        materialize_scores: materializeScores,
        window_days: windowDays,
        validation_days: validationDays,
      });
      setActiveTask(task);
      ctx.showToast("success", t("factorModelStarted"));
    } catch (err: any) {
      setError(err.message || t("factorModelActionFailed"));
    } finally {
      setActing(null);
    }
  };

  const toggleFeature = async (enabled: boolean) => {
    setActing("feature");
    setError(null);
    try {
      await api.updateFactorSystemConfig(enabled);
      ctx.showToast("success", enabled ? t("factorModelEnableFeature") : t("factorModelDisableFeature"));
      await loadAll(false);
    } catch (err: any) {
      setError(err.message || t("factorModelActionFailed"));
    } finally {
      setActing(null);
    }
  };

  const initializeWarehouse = async () => {
    setActing("initialize");
    setError(null);
    try {
      await api.initializeFactorWarehouse();
      ctx.showToast("success", t("factorModelInitializeWarehouse"));
      await loadAll(false);
    } catch (err: any) {
      setError(err.message || t("factorModelActionFailed"));
    } finally {
      setActing(null);
    }
  };

  const cancelPipeline = async () => {
    if (!activeTask) return;
    setActing("cancel");
    try {
      setActiveTask(await api.cancelFactorPipelineTask(activeTask.id));
    } catch (err: any) {
      setError(err.message || t("factorModelActionFailed"));
    } finally {
      setActing(null);
    }
  };

  const activate = async (model: FactorModelRun, mode: "shadow" | "ridge") => {
    setActing(`${mode}:${model.id}`);
    setError(null);
    try {
      await api.activateFactorModel(model.id, mode, `settings:${mode}`);
      ctx.showToast("success", t("factorModelActivated"));
      await loadAll(false);
    } catch (err: any) {
      setError(err.message || t("factorModelActionFailed"));
    } finally {
      setActing(null);
    }
  };

  const fallback = async () => {
    if (!fallbackReason.trim()) {
      setError(t("factorModelReasonRequired"));
      return;
    }
    setActing("fallback");
    try {
      await api.fallbackFactorModel(fallbackReason.trim());
      setFallbackOpen(false);
      setFallbackReason("");
      ctx.showToast("success", t("factorModelFallbackDone"));
      await loadAll(false);
    } catch (err: any) {
      setError(err.message || t("factorModelActionFailed"));
    } finally {
      setActing(null);
    }
  };

  const runtime = overview?.runtime;
  const taskRunning = !!activeTask && !TERMINAL_TASK_STATES.has(activeTask.status);
  const featureEnabled = overview?.feature_enabled ?? overview?.config?.feature_enabled ?? false;
  const warehouseAvailable = overview?.health.warehouse_available ?? false;
  const invalidWindows = validationDays >= windowDays;

  const columns = [
    {
      title: t("factorModelModelId"),
      dataIndex: "id",
      key: "id",
      render: (value: string, record: FactorModelRun) => (
        <div className="factor-model-id">
          <Tooltip title={value}><strong>{shortId(value)}</strong></Tooltip>
          {runtime?.active_model_run_id === record.id && (
            <Tag color={modeColor(runtime.weight_mode)}>{runtime.weight_mode}</Tag>
          )}
        </div>
      ),
    },
    {
      title: t("factorModelStatus"),
      dataIndex: "status",
      key: "status",
      render: (value: string, record: FactorModelRun) => (
        <Tooltip title={record.rejection_reason || undefined}>
          <Tag color={value === "validated" ? "green" : "red"}>
            {value === "validated" ? t("factorModelValidated") : value === "rejected" ? t("factorModelRejected") : value}
          </Tag>
        </Tooltip>
      ),
    },
    {
      title: t("factorModelValidationIc"),
      key: "validation_ic",
      render: (_: unknown, record: FactorModelRun) => {
        const value = Number(record.metrics.validation_ic);
        return Number.isFinite(value) ? value.toFixed(4) : "-";
      },
    },
    {
      title: t("factorModelSamples"),
      dataIndex: "sample_count",
      key: "sample_count",
    },
    {
      title: t("factorModelCutoff"),
      dataIndex: "data_cutoff_at",
      key: "data_cutoff_at",
      render: (value: string | null) => formatDateTime(value),
    },
    {
      title: t("factorModelActions"),
      key: "actions",
      render: (_: unknown, record: FactorModelRun) => (
        <Space size={4} wrap>
          <Button
            size="small"
            icon={<EyeOutlined />}
            disabled={record.status !== "validated"}
            loading={acting === `shadow:${record.id}`}
            onClick={() => activate(record, "shadow")}
          >
            {t("factorModelShadow")}
          </Button>
          <Button
            size="small"
            type="primary"
            icon={<CheckCircleOutlined />}
            disabled={record.status !== "validated"}
            loading={acting === `ridge:${record.id}`}
            onClick={() => activate(record, "ridge")}
          >
            {t("factorModelRidge")}
          </Button>
        </Space>
      ),
    },
  ];

  return (
    <div className="factor-model-settings">
      <div className="factor-settings-head">
        <div>
          <p className="panel-kicker">{t("factorModelTitle")}</p>
          <h3>{t("factorModelRuntime")}</h3>
        </div>
        <Space>
          {runtime?.weight_mode !== "manual" && (
            <Button
              danger
              icon={<RollbackOutlined />}
              loading={acting === "fallback"}
              onClick={() => setFallbackOpen(true)}
            >
              {t("factorModelFallback")}
            </Button>
          )}
          <Tooltip title={t("factorModelRefresh")}>
            <Button
              aria-label={t("factorModelRefresh")}
              icon={<ReloadOutlined />}
              loading={loading}
              onClick={() => loadAll()}
            />
          </Tooltip>
        </Space>
      </div>

      {error && <Alert type="error" showIcon closable message={error} onClose={() => setError(null)} />}

      {overview && !featureEnabled && (
        <Alert
          type="warning"
          showIcon
          message={t("factorModelFeatureDisabledTitle")}
          description={t("factorModelFeatureDisabledDescription")}
          action={(
            <Button type="primary" loading={acting === "feature"} onClick={() => toggleFeature(true)}>
              {t("factorModelEnableFeature")}
            </Button>
          )}
        />
      )}

      {overview && featureEnabled && !warehouseAvailable && (
        <Alert
          type="warning"
          showIcon
          message={t("factorModelWarehouseUnavailableTitle")}
          description={template("factorModelWarehouseUnavailableDesc", {
            reason: overview.warehouse_error || overview.health.reasons?.join(", ") || t("factorModelUnavailable"),
            path: overview.config.warehouse_path,
          })}
          action={(
            <Button loading={acting === "initialize"} onClick={initializeWarehouse}>
              {t("factorModelInitializeWarehouse")}
            </Button>
          )}
        />
      )}

      {invalidWindows && <Alert type="error" showIcon message={t("factorModelInvalidWindows")} />}

      <div className="factor-runtime-grid">
        <div><span>{t("factorModelFeatureStatus")}</span><strong><Tag color={featureEnabled ? "green" : "default"}>{featureEnabled ? t("factorModelFeatureEnabled") : t("factorModelFeatureDisabled")}</Tag></strong></div>
        <div><span>{t("factorModelRuntime")}</span><strong><Tag color={modeColor(runtime?.weight_mode ?? "manual")}>{runtime?.weight_mode ?? "manual"}</Tag></strong></div>
        <div><span>{t("factorModelScoreMode")}</span><strong>{runtime?.score_weight_mode ?? "manual"}</strong></div>
        <div><span>{t("factorModelActiveModel")}</span><Tooltip title={runtime?.active_model_run_id || undefined}><strong>{runtime?.active_model_run_id ? shortId(runtime.active_model_run_id) : t("factorModelNoModel")}</strong></Tooltip></div>
        <div><span>{t("factorModelWarehouse")}</span><strong>{overview?.health.warehouse_available ? t("factorModelHealthy") : t("factorModelUnavailable")}</strong></div>
        <div><span>{t("factorModelLatestDate")}</span><strong>{overview?.latest_trade_date ?? "-"}</strong></div>
        <div><span>{t("factorModelCoverage")}</span><strong>{averageCoverage == null ? "-" : `${(averageCoverage * 100).toFixed(1)}%`}</strong></div>
        <div><span>{t("factorModelWarehousePath")}</span><Tooltip title={overview?.config.warehouse_path}><strong>{shortId(overview?.config.warehouse_path)}</strong></Tooltip></div>
      </div>

      <section className="factor-pipeline-section">
        <div className="factor-section-title">
          <h3>{t("factorModelPipeline")}</h3>
          <Space>
            {taskRunning && (
              <Button
                danger
                icon={<StopOutlined />}
                loading={acting === "cancel"}
                onClick={cancelPipeline}
              >
                {t("factorModelCancel")}
              </Button>
            )}
            <Button
              type="primary"
              icon={<PlayCircleOutlined />}
              disabled={taskRunning || !featureEnabled || !warehouseAvailable || invalidWindows}
              loading={acting === "pipeline"}
              onClick={startPipeline}
            >
              {t("factorModelRun")}
            </Button>
          </Space>
        </div>
        <div className="factor-pipeline-controls">
          <label><span>{t("factorModelFeatureStatus")}</span><Switch checked={featureEnabled} loading={acting === "feature"} onChange={toggleFeature} /></label>
          <label><span>{t("factorModelFullRefresh")}</span><Switch checked={fullRefresh} onChange={setFullRefresh} /></label>
          <label><span>{t("factorModelTrainModel")}</span><Switch checked={trainModel} onChange={setTrainModel} /></label>
          <label><span>{t("factorModelMaterialize")}</span><Switch checked={materializeScores} onChange={setMaterializeScores} /></label>
          <label><span>{t("factorModelWindow")} ({t("factorModelDays")})</span><InputNumber min={60} max={1000} value={windowDays} onChange={(value) => setWindowDays(Number(value ?? 250))} /></label>
          <label><span>{t("factorModelValidation")} ({t("factorModelDays")})</span><InputNumber min={20} max={250} value={validationDays} onChange={(value) => setValidationDays(Number(value ?? 50))} /></label>
        </div>
        {activeTask && (
          <div className="factor-task-strip">
            <div>
              <strong>{t("factorModelRecentTask")}: {activeTask.stage}</strong>
              <span>{activeTask.message}</span>
            </div>
            <Tag color={activeTask.status === "done" || activeTask.status === "completed" ? "green" : activeTask.status === "failed" ? "red" : activeTask.status === "cancelled" ? "default" : "blue"}>
              {activeTask.status}
            </Tag>
            <Progress percent={Math.round(activeTask.percent)} status={activeTask.status === "failed" ? "exception" : activeTask.status === "done" || activeTask.status === "completed" ? "success" : "active"} />
          </div>
        )}
      </section>

      <section className="factor-model-list">
        <div className="factor-section-title">
          <h3>{t("factorModelModels")}</h3>
          <Tag>{models.length}</Tag>
        </div>
        <Table<FactorModelRun>
          rowKey="id"
          size="small"
          loading={loading}
          columns={columns}
          dataSource={models}
          pagination={{ pageSize: 8, hideOnSinglePage: true }}
          locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t("factorModelNoModels")} /> }}
          scroll={{ x: 860 }}
        />
      </section>

      <Modal
        open={fallbackOpen}
        title={t("factorModelFallbackTitle")}
        okText={t("factorModelConfirmFallback")}
        okButtonProps={{ danger: true, loading: acting === "fallback" }}
        onOk={fallback}
        onCancel={() => setFallbackOpen(false)}
      >
        <Input.TextArea
          rows={3}
          value={fallbackReason}
          placeholder={t("factorModelFallbackReason")}
          onChange={(event) => setFallbackReason(event.target.value)}
        />
      </Modal>
    </div>
  );
}
