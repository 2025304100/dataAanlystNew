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

const TERMINAL_TASK_STATES = new Set(["done", "completed", "failed", "cancelled"]);

const LABELS = {
  "zh-CN": {
    title: "动态因子与模型",
    refresh: "刷新",
    runtime: "当前决策模式",
    scoreMode: "实际评分来源",
    activeModel: "活动模型",
    warehouse: "因子仓库",
    latestDate: "最新交易日",
    coverage: "平均覆盖率",
    healthy: "正常",
    unavailable: "不可用",
    noModel: "手工权重",
    pipeline: "本地因子流水线",
    fullRefresh: "全量重算",
    trainModel: "训练 Ridge",
    materialize: "生成评分快照",
    window: "训练窗口",
    validation: "验证窗口",
    days: "日",
    run: "运行流水线",
    cancel: "取消任务",
    recentTask: "最近任务",
    models: "模型版本",
    modelId: "模型 ID",
    status: "状态",
    validationIc: "验证 IC",
    samples: "样本",
    cutoff: "数据截止",
    actions: "操作",
    shadow: "影子运行",
    ridge: "正式启用",
    fallback: "回退手工",
    fallbackTitle: "回退到手工权重",
    fallbackReason: "回退原因",
    confirmFallback: "确认回退",
    reasonRequired: "请输入回退原因",
    loadFailed: "加载因子运行状态失败",
    actionFailed: "操作失败",
    started: "因子流水线已启动",
    activated: "模型运行模式已更新",
    fallbackDone: "已回退到手工权重",
    rejected: "已拒绝",
    validated: "已验证",
    noModels: "暂无训练模型",
  },
  "en-US": {
    title: "Dynamic Factors & Models",
    refresh: "Refresh",
    runtime: "Decision Mode",
    scoreMode: "Score Source",
    activeModel: "Active Model",
    warehouse: "Factor Warehouse",
    latestDate: "Latest Trade Date",
    coverage: "Average Coverage",
    healthy: "Healthy",
    unavailable: "Unavailable",
    noModel: "Manual Weights",
    pipeline: "Local Factor Pipeline",
    fullRefresh: "Full Refresh",
    trainModel: "Train Ridge",
    materialize: "Materialize Scores",
    window: "Training Window",
    validation: "Validation Window",
    days: "days",
    run: "Run Pipeline",
    cancel: "Cancel Task",
    recentTask: "Latest Task",
    models: "Model Versions",
    modelId: "Model ID",
    status: "Status",
    validationIc: "Validation IC",
    samples: "Samples",
    cutoff: "Data Cutoff",
    actions: "Actions",
    shadow: "Run Shadow",
    ridge: "Activate Live",
    fallback: "Fallback Manual",
    fallbackTitle: "Fallback to Manual Weights",
    fallbackReason: "Fallback reason",
    confirmFallback: "Confirm Fallback",
    reasonRequired: "Fallback reason is required",
    loadFailed: "Failed to load factor runtime",
    actionFailed: "Action failed",
    started: "Factor pipeline started",
    activated: "Model runtime updated",
    fallbackDone: "Fallback to manual weights completed",
    rejected: "Rejected",
    validated: "Validated",
    noModels: "No trained models",
  },
};

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
  const labels = LABELS[ctx.locale as keyof typeof LABELS] ?? LABELS["zh-CN"];
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
      setError(err.message || labels.loadFailed);
    } finally {
      if (showLoading) setLoading(false);
    }
  }, [labels.loadFailed]);

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
        setError(err.message || labels.loadFailed);
      }
    }, 2000);
    return () => window.clearInterval(timer);
  }, [activeTask?.id, activeTask?.status, labels.loadFailed, loadAll]);

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
      ctx.showToast("success", labels.started);
    } catch (err: any) {
      setError(err.message || labels.actionFailed);
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
      setError(err.message || labels.actionFailed);
    } finally {
      setActing(null);
    }
  };

  const activate = async (model: FactorModelRun, mode: "shadow" | "ridge") => {
    setActing(`${mode}:${model.id}`);
    setError(null);
    try {
      await api.activateFactorModel(model.id, mode, `settings:${mode}`);
      ctx.showToast("success", labels.activated);
      await loadAll(false);
    } catch (err: any) {
      setError(err.message || labels.actionFailed);
    } finally {
      setActing(null);
    }
  };

  const fallback = async () => {
    if (!fallbackReason.trim()) {
      setError(labels.reasonRequired);
      return;
    }
    setActing("fallback");
    try {
      await api.fallbackFactorModel(fallbackReason.trim());
      setFallbackOpen(false);
      setFallbackReason("");
      ctx.showToast("success", labels.fallbackDone);
      await loadAll(false);
    } catch (err: any) {
      setError(err.message || labels.actionFailed);
    } finally {
      setActing(null);
    }
  };

  const runtime = overview?.runtime;
  const taskRunning = !!activeTask && !TERMINAL_TASK_STATES.has(activeTask.status);

  const columns = [
    {
      title: labels.modelId,
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
      title: labels.status,
      dataIndex: "status",
      key: "status",
      render: (value: string, record: FactorModelRun) => (
        <Tooltip title={record.rejection_reason || undefined}>
          <Tag color={value === "validated" ? "green" : "red"}>
            {value === "validated" ? labels.validated : value === "rejected" ? labels.rejected : value}
          </Tag>
        </Tooltip>
      ),
    },
    {
      title: labels.validationIc,
      key: "validation_ic",
      render: (_: unknown, record: FactorModelRun) => {
        const value = Number(record.metrics.validation_ic);
        return Number.isFinite(value) ? value.toFixed(4) : "-";
      },
    },
    {
      title: labels.samples,
      dataIndex: "sample_count",
      key: "sample_count",
    },
    {
      title: labels.cutoff,
      dataIndex: "data_cutoff_at",
      key: "data_cutoff_at",
      render: (value: string | null) => formatDateTime(value),
    },
    {
      title: labels.actions,
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
            {labels.shadow}
          </Button>
          <Button
            size="small"
            type="primary"
            icon={<CheckCircleOutlined />}
            disabled={record.status !== "validated"}
            loading={acting === `ridge:${record.id}`}
            onClick={() => activate(record, "ridge")}
          >
            {labels.ridge}
          </Button>
        </Space>
      ),
    },
  ];

  return (
    <div className="factor-model-settings">
      <div className="factor-settings-head">
        <div>
          <p className="panel-kicker">{labels.title}</p>
          <h3>{labels.runtime}</h3>
        </div>
        <Space>
          {runtime?.weight_mode !== "manual" && (
            <Button
              danger
              icon={<RollbackOutlined />}
              loading={acting === "fallback"}
              onClick={() => setFallbackOpen(true)}
            >
              {labels.fallback}
            </Button>
          )}
          <Tooltip title={labels.refresh}>
            <Button
              aria-label={labels.refresh}
              icon={<ReloadOutlined />}
              loading={loading}
              onClick={() => loadAll()}
            />
          </Tooltip>
        </Space>
      </div>

      {error && <Alert type="error" showIcon closable message={error} onClose={() => setError(null)} />}

      <div className="factor-runtime-grid">
        <div><span>{labels.runtime}</span><strong><Tag color={modeColor(runtime?.weight_mode ?? "manual")}>{runtime?.weight_mode ?? "manual"}</Tag></strong></div>
        <div><span>{labels.scoreMode}</span><strong>{runtime?.score_weight_mode ?? "manual"}</strong></div>
        <div><span>{labels.activeModel}</span><Tooltip title={runtime?.active_model_run_id || undefined}><strong>{runtime?.active_model_run_id ? shortId(runtime.active_model_run_id) : labels.noModel}</strong></Tooltip></div>
        <div><span>{labels.warehouse}</span><strong>{overview?.health.warehouse_available ? labels.healthy : labels.unavailable}</strong></div>
        <div><span>{labels.latestDate}</span><strong>{overview?.latest_trade_date ?? "-"}</strong></div>
        <div><span>{labels.coverage}</span><strong>{averageCoverage == null ? "-" : `${(averageCoverage * 100).toFixed(1)}%`}</strong></div>
      </div>

      <section className="factor-pipeline-section">
        <div className="factor-section-title">
          <h3>{labels.pipeline}</h3>
          <Space>
            {taskRunning && (
              <Button
                danger
                icon={<StopOutlined />}
                loading={acting === "cancel"}
                onClick={cancelPipeline}
              >
                {labels.cancel}
              </Button>
            )}
            <Button
              type="primary"
              icon={<PlayCircleOutlined />}
              disabled={taskRunning}
              loading={acting === "pipeline"}
              onClick={startPipeline}
            >
              {labels.run}
            </Button>
          </Space>
        </div>
        <div className="factor-pipeline-controls">
          <label><span>{labels.fullRefresh}</span><Switch checked={fullRefresh} onChange={setFullRefresh} /></label>
          <label><span>{labels.trainModel}</span><Switch checked={trainModel} onChange={setTrainModel} /></label>
          <label><span>{labels.materialize}</span><Switch checked={materializeScores} onChange={setMaterializeScores} /></label>
          <label><span>{labels.window} ({labels.days})</span><InputNumber min={60} max={1000} value={windowDays} onChange={(value) => setWindowDays(Number(value ?? 250))} /></label>
          <label><span>{labels.validation} ({labels.days})</span><InputNumber min={20} max={250} value={validationDays} onChange={(value) => setValidationDays(Number(value ?? 50))} /></label>
        </div>
        {activeTask && (
          <div className="factor-task-strip">
            <div>
              <strong>{labels.recentTask}: {activeTask.stage}</strong>
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
          <h3>{labels.models}</h3>
          <Tag>{models.length}</Tag>
        </div>
        <Table<FactorModelRun>
          rowKey="id"
          size="small"
          loading={loading}
          columns={columns}
          dataSource={models}
          pagination={{ pageSize: 8, hideOnSinglePage: true }}
          locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={labels.noModels} /> }}
          scroll={{ x: 860 }}
        />
      </section>

      <Modal
        open={fallbackOpen}
        title={labels.fallbackTitle}
        okText={labels.confirmFallback}
        okButtonProps={{ danger: true, loading: acting === "fallback" }}
        onOk={fallback}
        onCancel={() => setFallbackOpen(false)}
      >
        <Input.TextArea
          rows={3}
          value={fallbackReason}
          placeholder={labels.fallbackReason}
          onChange={(event) => setFallbackReason(event.target.value)}
        />
      </Modal>
    </div>
  );
}
