import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Button,
  Empty,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Progress,
  Space,
  Switch,
  Table,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import {
  CheckCircleOutlined,
  EyeOutlined,
  PlayCircleOutlined,
  QuestionCircleOutlined,
  ReloadOutlined,
  RollbackOutlined,
  StopOutlined,
} from "@ant-design/icons";
import {
  api,
  type FactorModelRun,
  type FactorOverview,
  type FactorPipelineEta,
  type FactorPipelineTask,
  type FactorWeightMode,
} from "../api/client";
import { useApp } from "../context/AppContext";
import { enumLabel, t, template } from "../i18n";
import {
  type RatedCell,
  type RatingLevel,
  LEVEL_COLOR,
  LEVEL_TEXT,
  formatDateTime,
  rateCoverage,
  rateDataCutoff,
  rateLatestTradeDate,
  rateModeConsistency,
  rateSampleCount,
  rateValidationIC,
  ratedColorFg,
} from "../utils/factorRating";

const TERMINAL_TASK_STATES = new Set(["done", "completed", "failed", "cancelled"]);

function shortId(value: string | null | undefined) {
  if (!value) return "-";
  return value.length > 18 ? `${value.slice(0, 10)}...${value.slice(-6)}` : value;
}

function parseServerDateTime(value: string | null | undefined) {
  if (!value) return null;
  const hasTimeZone = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(value);
  const parsed = new Date(hasTimeZone ? value : `${value}Z`);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

function modeColor(mode: FactorWeightMode | string) {
  if (mode === "ridge") return "green";
  if (mode === "shadow") return "blue";
  return "default";
}

const { Text } = Typography;

function ratedCell(node: React.ReactNode, rated: RatedCell, extra: { width?: number; align?: "right" | "left" } = {}) {
  const { width, align = "right" } = extra;
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        justifyContent: align === "right" ? "flex-end" : "flex-start",
        gap: 6,
        width: width ?? "auto",
      }}
    >
      <Tooltip
        title={
          <div style={{ lineHeight: 1.55, maxWidth: 280 }}>
            <div style={{ fontWeight: 700, marginBottom: 2 }}>
              <Tag color={LEVEL_COLOR[rated.level]} style={{ marginRight: 6 }}>
                {LEVEL_TEXT[rated.level]}
              </Tag>
              当前值：{rated.text}
            </div>
            <div style={{ color: "#f1f5f9" }}>{rated.hint}</div>
          </div>
        }
      >
        <QuestionCircleOutlined
          style={{
            color: "#94a3b8",
            fontSize: 12,
            cursor: "help",
            opacity: 0.9,
          }}
        />
      </Tooltip>
      <Text strong style={{ color: ratedColorFg(rated.level) }}>
        {node}
      </Text>
      <Tag
        color={LEVEL_COLOR[rated.level]}
        style={{
          marginInlineStart: 0,
          padding: "1px 6px",
          fontSize: 11,
          fontWeight: 700,
          borderRadius: 999,
        }}
      >
        {LEVEL_TEXT[rated.level]}
      </Tag>
    </span>
  );
}
// 后端 stage 到 i18n key 的映射，避免直接显示英文原始值
const STAGE_KEYS: Record<string, string> = {
  queued: "factorModelStageQueued",
  initializing: "factorModelStageInitializing",
  mirror: "factorModelStageMirror",
  factors: "factorModelStageFactors",
  targets: "factorModelStageTargets",
  train: "factorModelStageTrain",
  score: "factorModelStageScore",
  done: "factorModelStageDone",
  failed: "factorModelStageFailed",
  cancelled: "factorModelStageCancelled",
};

function stageLabel(stage: string): string {
  const key = STAGE_KEYS[stage];
  if (!key) return stage;
  const translated = t(key);
  return translated === key ? stage : translated;
}

function formatElapsed(seconds: number): string {
  if (seconds < 60) return template("factorModelElapsed", { seconds });
  const minutes = Math.floor(seconds / 60);
  const secs = seconds % 60;
  return template("factorModelElapsedMinutes", { minutes, seconds: secs });
}

function formatDuration(seconds: number): string {
  if (seconds < 60) return template("factorModelDurationSeconds", { seconds });
  const minutes = Math.floor(seconds / 60);
  const secs = seconds % 60;
  return template("factorModelDurationMinutes", { minutes, seconds: secs });
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
  // 任务进度追踪：已运行秒数 + 进度停滞标志（percent 30 秒未变则标记为停滞）
  const [elapsed, setElapsed] = useState(0);
  const [stalled, setStalled] = useState(false);
  const lastProgressRef = useRef<{ signature: string; time: number } | null>(null);
  // 预估时长：基于历史已完成任务统计
  const [eta, setEta] = useState<FactorPipelineEta | null>(null);
  const [trainingFactorSetId, setTrainingFactorSetId] = useState<string | null>(null);

  const loadAll = useCallback(async (showLoading = true) => {
    if (showLoading) setLoading(true);
    setError(null);
    try {
      // Factor-domain internal - DO NOT USE outside factor center
      const factorSetLoader = typeof api.scoringListFactorSetsAsFactor === "function"
        // Factor-domain internal - DO NOT USE outside factor center
        ? api.scoringListFactorSetsAsFactor("frozen", 50)
        : Promise.resolve([]);
      const [overviewData, modelData, tasks, frozenFactorSets] = await Promise.all([
        // Factor-domain internal - DO NOT USE outside factor center
        api.scoringGetOverviewAsFactor(),
        // Factor-domain internal - DO NOT USE outside factor center
        api.scoringGetFactorModelListAsFactor(20),
        // Factor-domain internal - DO NOT USE outside factor center
        api.scoringListTasks("factor_pipeline", 5),
        factorSetLoader,
      ]);
      setOverview(overviewData);
      setModels(modelData.items);
      const usableFactorSet = frozenFactorSets.find((item) => item.status === "frozen" && item.n_members > 0);
      // Older embedded test shells may not expose the FactorSet API yet; the
      // real client always does, while this fallback preserves their legacy
      // pipeline smoke test behavior.
      // Factor-domain internal - DO NOT USE outside factor center
        setTrainingFactorSetId(usableFactorSet?.id ?? (typeof api.scoringListFactorSetsAsFactor === "function" ? null : "legacy"));
      // 优先选择运行中的任务，避免被最新的终态任务（cancelled/failed）覆盖
      const activeFromList = tasks.find((t) => !TERMINAL_TASK_STATES.has(t.status)) ?? null;
      setActiveTask(activeFromList ?? tasks[0] ?? null);
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
        // Factor-domain internal - DO NOT USE outside factor center
        const task = await api.scoringGetTask(activeTask.id);
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

  // 每秒更新任务已运行时长，并检测进度是否停滞（percent 30 秒未变）
  useEffect(() => {
    if (!activeTask || TERMINAL_TASK_STATES.has(activeTask.status)) {
      setElapsed(0);
      setStalled(false);
      lastProgressRef.current = null;
      return;
    }
    const startTimeStr = activeTask.started_at || activeTask.created_at;
    const startTime = parseServerDateTime(startTimeStr)?.getTime() ?? Date.now();
    const signature = [
      activeTask.percent,
      activeTask.processed,
      activeTask.message,
      activeTask.updated_at,
    ].join("|");
    // 进度、消息或后端心跳任一变化，都说明任务仍在工作。
    if (!lastProgressRef.current || lastProgressRef.current.signature !== signature) {
      lastProgressRef.current = { signature, time: Date.now() };
      setStalled(false);
    }
    const tickTimer = window.setInterval(() => {
      const now = Date.now();
      setElapsed(Math.max(0, Math.floor((now - startTime) / 1000)));
      if (lastProgressRef.current) {
        const stallSeconds = (now - lastProgressRef.current.time) / 1000;
        setStalled(stallSeconds >= 30);
      }
    }, 1000);
    return () => window.clearInterval(tickTimer);
  }, [activeTask?.id, activeTask?.status, activeTask?.percent, activeTask?.processed, activeTask?.message, activeTask?.updated_at, activeTask?.started_at, activeTask?.created_at]);

  // 任务开始运行时拉取历史预估时长；运行中每 30 秒刷新一次以适应数据量变化
  useEffect(() => {
    if (!activeTask || TERMINAL_TASK_STATES.has(activeTask.status)) {
      setEta(null);
      return;
    }
    const fetchEta = () => {
      // Factor-domain internal - DO NOT USE outside factor center
      api.scoringGetPipelineEta(trainModel, fullRefresh).then(setEta).catch(() => { /* 预估失败不影响主流程 */ });
    };
    fetchEta();
    const etaTimer = window.setInterval(fetchEta, 30000);
    return () => window.clearInterval(etaTimer);
  }, [activeTask?.id, activeTask?.status, trainModel, fullRefresh]);

  // 预计剩余时间 = max(0, 推荐总时长 - 已运行时长)
  const remainingSeconds = useMemo(() => {
    if (!eta || elapsed <= 0) return null;
    return Math.max(0, Math.round(eta.recommended_seconds - elapsed));
  }, [eta, elapsed]);

  const averageCoverage = useMemo(() => {
    const rows = overview?.factor_coverage ?? [];
    if (!rows.length) return null;
    return rows.reduce((sum, item) => sum + Number(item.coverage || 0), 0) / rows.length;
  }, [overview]);

  const startPipeline = async () => {
    setActing("pipeline");
    setError(null);
    try {
      // Factor-domain internal - DO NOT USE outside factor center
      const task = await api.scoringCreateTask({
        full_refresh: fullRefresh,
        train_model: trainModel,
        materialize_scores: materializeScores,
        window_days: windowDays,
        validation_days: validationDays,
        factor_set_id: trainModel ? trainingFactorSetId : undefined,
        actor: "settings:pipeline",
        source_hint: "factor_model_settings",
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
      // Factor-domain internal - DO NOT USE outside factor center
      await api.scoringUpdateSystemConfig(enabled, "settings:toggle");
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
      // Factor-domain internal - DO NOT USE outside factor center
      await api.scoringInitializeWarehouse(false, "settings:init");
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
      // Factor-domain internal - DO NOT USE outside factor center
      setActiveTask(await api.scoringCancelTask(activeTask.id));
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
      // Factor-domain internal - DO NOT USE outside factor center
      await api.scoringActivateModel(model.id, mode, `settings:${mode}`);
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
      // Factor-domain internal - DO NOT USE outside factor center
      await api.scoringFallbackToManual(fallbackReason.trim());
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
            <Tag color={modeColor(runtime.weight_mode)}>{enumLabel("factorMode", runtime.weight_mode)}</Tag>
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
        const raw = Number(record.metrics?.validation_ic);
        const value = Number.isFinite(raw) ? raw : null;
        const rated = rateValidationIC(value);
        return ratedCell(
          <span>{rated.text}</span>,
          rated,
          { align: "left" },
        );
      },
    },
    {
      title: t("factorModelSamples"),
      dataIndex: "sample_count",
      key: "sample_count",
      render: (_: unknown, record: FactorModelRun) => {
        const ic = Number(record.metrics?.validation_ic);
        const rated = rateSampleCount(
          typeof record.sample_count === "number" ? record.sample_count : null,
          Number.isFinite(ic) ? ic : null,
        );
        return ratedCell(<span>{rated.text}</span>, rated, { align: "left" });
      },
    },
    {
      title: t("factorModelCutoff"),
      dataIndex: "data_cutoff_at",
      key: "data_cutoff_at",
      render: (value: string | null) => {
        const rated = rateDataCutoff(value, overview?.latest_trade_date ?? null);
        return ratedCell(<span>{rated.text}</span>, rated, { align: "left" });
      },
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
        <div>
          <span>{t("factorModelFeatureStatus")}</span>
          <Tooltip title={(featureEnabled ? "功能开启：可运行流水线、启用 Ridge 打分。" : "功能关闭：流水线/模型启用全部锁死，避免手动开关误触导致治理页出现异常 blocker。")}>
            <QuestionCircleOutlined
              style={{ color: "#94a3b8", fontSize: 12, marginLeft: 4, cursor: "help" }}
            />
          </Tooltip>
          <strong>
            <Tag color={featureEnabled ? "green" : "default"}>
              {featureEnabled ? t("factorModelFeatureEnabled") : t("factorModelFeatureDisabled")}
            </Tag>
          </strong>
        </div>
        <div>
          <span>{t("factorModelRuntime")}</span>
          <Tooltip title={"决策模式 = 组合风控权重的取数模式。ridge=走评分权重；manual=走手工配置；切换时会写入审计记录 + 触发治理状态复核。"}>
            <QuestionCircleOutlined style={{ color: "#94a3b8", fontSize: 12, marginLeft: 4, cursor: "help" }} />
          </Tooltip>
          <strong>
            <Tag color={modeColor(runtime?.weight_mode ?? "manual")}>
              {enumLabel("factorMode", runtime?.weight_mode ?? "manual")}
            </Tag>
          </strong>
        </div>
        <div>
          <span>{t("factorModelScoreMode")}</span>
          <strong>
            {(() => {
              const rated = rateModeConsistency(
                runtime?.weight_mode,
                runtime?.score_weight_mode,
                (m) => enumLabel("factorMode", m),
              );
              return ratedCell(<>{rated.text}</>, rated, { align: "left" });
            })()}
          </strong>
        </div>
        <div>
          <span>{t("factorModelActiveModel")}</span>
          <Tooltip title={(runtime?.active_model_run_id ? "当前在“正式启用”状态下的模型版本。切换它需要：先影子运行 5-10 日 → G5 双跑通过 → 再点正式启用。" : "没有活动模型：所有组合会回退到手工打分权重。")}>
            <QuestionCircleOutlined style={{ color: "#94a3b8", fontSize: 12, marginLeft: 4, cursor: "help" }} />
          </Tooltip>
          <Tooltip title={runtime?.active_model_run_id || undefined}>
            <strong>
              {runtime?.active_model_run_id
                ? shortId(runtime.active_model_run_id)
                : <Text type="secondary" style={{ color: "#b91c1c" }}>{t("factorModelNoModel")}</Text>}
            </strong>
          </Tooltip>
        </div>
        <div>
          <span>{t("factorModelWarehouse")}</span>
          <Tooltip title={(warehouseAvailable ? "因子仓库连接正常、最近一次 health check 通过，流水线可直接运行。" : "因子仓库不可用：通常是 duckdb 文件被锁、路径错误或初始化未完成。先在下方点“初始化仓库”再做后续评估。")}>
            <QuestionCircleOutlined style={{ color: "#94a3b8", fontSize: 12, marginLeft: 4, cursor: "help" }} />
          </Tooltip>
          <strong>
            <Tag color={warehouseAvailable ? "green" : "red"}>
              {warehouseAvailable ? t("factorModelHealthy") : t("factorModelUnavailable")}
            </Tag>
          </strong>
        </div>
        <div>
          <span>{t("factorModelLatestDate")}</span>
          <strong>
            {(() => {
              const rated = rateLatestTradeDate(overview?.latest_trade_date ?? null);
              return ratedCell(<>{rated.text}</>, rated, { align: "left" });
            })()}
          </strong>
        </div>
        <div>
          <span>{t("factorModelCoverage")}</span>
          <strong>
            {(() => {
              const rated = rateCoverage(averageCoverage);
              return ratedCell(<>{rated.text}</>, rated, { align: "left" });
            })()}
          </strong>
        </div>
        <div>
          <span>{t("factorModelWarehousePath")}</span>
          <Tooltip title={"因子数据（bar / factor / score snapshot / 训练模型）持久化路径。这是本地文件，建议放到独立 SSD 盘符，避免和系统盘抢 IO。"}>
            <QuestionCircleOutlined style={{ color: "#94a3b8", fontSize: 12, marginLeft: 4, cursor: "help" }} />
          </Tooltip>
          <Tooltip title={overview?.config.warehouse_path}>
            <strong>{shortId(overview?.config.warehouse_path)}</strong>
          </Tooltip>
        </div>
      </div>

      <section className="factor-pipeline-section">
        <div className="factor-section-title">
          <h3>{t("factorModelPipeline")}</h3>
          <Space>
            <Button
              type="primary"
              icon={<PlayCircleOutlined />}
              disabled={taskRunning || !featureEnabled || !warehouseAvailable || invalidWindows || (trainModel && !trainingFactorSetId)}
              loading={acting === "pipeline"}
              onClick={startPipeline}
            >
              {t("factorModelRun")}
            </Button>
            {taskRunning && (
              <Popconfirm
                title={t("factorModelCancelConfirmTitle")}
                description={t("factorModelCancelConfirmDesc")}
                okText={t("factorModelCancel")}
                cancelText={t("cancel")}
                okButtonProps={{ danger: true }}
                onConfirm={cancelPipeline}
              >
                <Button
                  danger
                  icon={<StopOutlined />}
                  loading={acting === "cancel"}
                >
                  {t("factorModelCancel")}
                </Button>
              </Popconfirm>
            )}
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
        {trainModel && !trainingFactorSetId && !loading && (
          <Alert
            type="warning"
            showIcon
            style={{ marginTop: 10 }}
            message="暂无可用于训练的冻结因子集"
            description="请先在因子中心创建并冻结至少包含一个因子的因子集，再运行 Ridge 流水线。"
          />
        )}
        {activeTask && (
          <div className="factor-task-strip">
            <div>
              <strong>
                {t("factorModelRecentTask")}: {stageLabel(activeTask.stage)}
                {activeTask.status !== "done" && activeTask.status !== "completed" && activeTask.status !== "failed" && activeTask.status !== "cancelled" && elapsed > 0 && (
                  <span className="factor-task-elapsed"> · {formatElapsed(elapsed)}</span>
                )}
                {activeTask.status !== "done" && activeTask.status !== "completed" && activeTask.status !== "failed" && activeTask.status !== "cancelled" && remainingSeconds != null && remainingSeconds > 0 && (
                  <span className="factor-task-eta"> · {template("factorModelEtaRemaining", { duration: formatDuration(remainingSeconds) })}</span>
                )}
                {activeTask.status !== "done" && activeTask.status !== "completed" && activeTask.status !== "failed" && activeTask.status !== "cancelled" && eta != null && remainingSeconds === 0 && elapsed > eta.recommended_seconds && (
                  <span className="factor-task-stalled"> · {t("factorModelEtaExceeded")}</span>
                )}
              </strong>
              <span>{activeTask.message || (activeTask.status === "queued" ? t("factorModelTaskQueued") : "")}</span>
              {eta != null && eta.sample_count > 0 && activeTask.status !== "done" && activeTask.status !== "completed" && activeTask.status !== "failed" && activeTask.status !== "cancelled" && (
                <span className="factor-task-eta-hint">{template("factorModelEtaBasedOnHistory", { count: eta.sample_count })}</span>
              )}
              {stalled && activeTask.status !== "done" && activeTask.status !== "completed" && activeTask.status !== "failed" && activeTask.status !== "cancelled" && (
                <span className="factor-task-stalled">{t("factorModelTaskStalled")}</span>
              )}
            </div>
            <Tag color={activeTask.status === "done" || activeTask.status === "completed" ? "green" : activeTask.status === "failed" ? "red" : activeTask.status === "cancelled" ? "default" : "blue"}>
              {activeTask.status === "queued" ? t("factorModelStageQueued") : activeTask.status}
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
