import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Button,
  Descriptions,
  Drawer,
  Empty,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Progress,
  Select as AntSelect,
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
  ExclamationCircleOutlined,
  PlayCircleOutlined,
  QuestionCircleOutlined,
  ReloadOutlined,
  RollbackOutlined,
  StopOutlined,
  SyncOutlined,
} from "@ant-design/icons";
import {
  api,
  type FactorModelRun,
  type FactorOverview,
  type FactorPipelineEta,
  type FactorPipelineTask,
  type FactorSet,
  type FactorWeightMode,
  type ScoringModelRelations,
} from "../api/client";
import { useApp } from "../context/AppContext";
import { navigate as navigateApp, openSettingsSection } from "../utils/navigate";
import { enumLabel, t, template } from "../i18n";
import ChainStepsBar, { type ChainState } from "./factors/ChainStepsBar";
import FactorModelTrainingEvidence, { FactorModelResearchWarning, isObservedResearchComparison } from "./factors/FactorModelTrainingEvidence";
import { hydrateResearchModelPresentation, isTrustedResearchDataset, researchValidationPassed } from "../utils/factorModelResearch";
import useFactorModelReviewEvidence, { FactorModelReviewEvidenceNotice } from "./factors/useFactorModelReviewEvidence";
import FactorPipelineWizardNav, { type FactorPipelineWizardStep } from "./factors/FactorPipelineWizardNav";
import FactorPipelinePrepareStep from "./factors/FactorPipelinePrepareStep";
import ResearchRetrainModal from "./factors/ResearchRetrainModal";
import ProductionAdmissionModal from "./factors/ProductionAdmissionModal";
import FactorCoverageSummary from "./factors/FactorCoverageSummary";
import "./FactorModelSettings.css";
import {
  type RatedCell,
  type RatingLevel,
  LEVEL_COLOR,
  LEVEL_TEXT,
  formatDateTime,
  rateCoverage,
  rateDataCutoff,
  rateModeConsistency,
  rateValidationIC,
  ratedColorFg,
} from "../utils/factorRating";

const TERMINAL_TASK_STATES = new Set(["done", "completed", "failed", "cancelled"]);

function readableModelReadiness(model: FactorModelRun, fallback: React.ReactNode): React.ReactNode {
  if (researchValidationPassed(model)) return t("factorModelResearchIndependentRequired");
  if (model.readiness_code !== "MODEL_NOT_VALIDATED") return fallback;
  const evidence = [model.rejection_reason, model.readiness_message].filter(Boolean).join("\n");
  return <Tooltip title={evidence || undefined}><span>{t("factorModelValidationNotPassed")}</span></Tooltip>;
}

// 流水线阶段以任务状态和已提交结果为准。
// 每种态 4 类：wait=浅灰圆圈 / ready=绿对勾 / running=蓝色圆环旋转 / blocked=红感叹号
type PipelineStepStatus = "wait" | "ready" | "running" | "blocked" | "skipped";
const STEP_STATUS_CLASS: Record<PipelineStepStatus, string> = {
  wait: "ps-state-wait bg-slate-200 text-slate-500 border border-slate-300",
  ready: "ps-state-ready bg-green-500 text-white border border-green-600",
  running: "ps-state-running bg-blue-500 text-white border border-blue-600 animate-spin",
  blocked: "ps-state-blocked bg-red-500 text-white border border-red-600",
  skipped: "ps-state-wait bg-slate-100 text-slate-500 border border-slate-300",
};
const STEP_STATUS_TITLE: Record<PipelineStepStatus, string> = {
  wait: "未开始",
  ready: "已完成",
  running: "进行中",
  blocked: "阻断",
  skipped: "本次跳过",
};
interface PipelineStepsBarProps {
  trainEnabled: boolean;
  task: FactorPipelineTask | null;
}
function taskPayload(task: FactorPipelineTask | null): Record<string, unknown> {
  if (!task?.payload_json) return {};
  if (typeof task.payload_json === "object") return task.payload_json;
  try { return JSON.parse(task.payload_json); } catch { return {}; }
}

function pipelineOutcome(task: FactorPipelineTask): { label: string; detail: string; color: string; progress: "success" | "exception" | "normal" | "active" } {
  if (task.status === "failed") return { label: "任务失败", detail: task.message || "请查看失败原因后重试。", color: "red", progress: "exception" };
  if (task.status === "cancelled") return { label: "已取消", detail: "本次任务已停止，请确认已有输入后再决定是否重试。", color: "default", progress: "normal" };
  if (!TERMINAL_TASK_STATES.has(task.status)) return { label: task.status === "queued" ? "已排队，等待执行" : stageLabel(task.stage), detail: task.message || "等待后台任务执行。", color: "blue", progress: "active" };
  const model = task.result?.model as { status?: string; model_run_id?: string; research_model_gate_status?: string } | undefined;
  const bridge = task.result?.scoring_bridge as { status?: string; materialized_count?: number } | undefined;
  if (researchValidationPassed(model, task.result?.trusted_history_dataset)) return {
    label: t("factorModelResearchValidatedPending"), detail: t("factorModelResearchIndependentRequired"), color: "gold", progress: "normal",
  };
  if (task.result?.completion_status === "TRAINING_REJECTED" || model?.status === "rejected") return { label: "模型未通过验证", detail: "训练已结束，候选模型未达到验证门槛；评分未生成。请查看模型的验证结果。", color: "orange", progress: "normal" };
  if (task.result?.completion_status === "AWAITING_MODEL_ACTIVATION" || bridge?.status === "SKIPPED") return {
    label: model?.status === "validated" ? "训练验证完成，等待激活" : "输入计算完成，等待激活",
    detail: "评分未生成。请查看可正式运行的已验证模型，由本人激活后再生成评分。", color: "orange", progress: "normal",
  };
  if (typeof bridge?.materialized_count === "number" && bridge.materialized_count > 0) return {
    label: bridge.status === "PARTIAL" ? `评分部分生成（${bridge.materialized_count.toLocaleString()} 条）` : `评分已生成（${bridge.materialized_count.toLocaleString()} 条）`,
    detail: bridge.status === "PARTIAL" ? "部分标的缺少输入或业务映射，请检查本次评分覆盖。" : "已发布绑定本次模型版本的评分，可到任务与批次查看。",
    color: bridge.status === "PARTIAL" ? "orange" : "green", progress: bridge.status === "PARTIAL" ? "normal" : "success",
  };
  if (model?.status === "validated") return { label: "训练验证完成，等待激活", detail: "评分未生成。请由本人查看验证结果并激活新模型。", color: "orange", progress: "normal" };
  if (taskPayload(task).materialize_scores === false && task.result?.factors) return { label: "输入计算完成", detail: "本次未请求生成评分，已保留因子计算结果。", color: "green", progress: "success" };
  return { label: "任务结束，评分未生成", detail: "没有评分写入证据，请检查模型状态和输入覆盖。", color: "orange", progress: "normal" };
}

function PipelineStepsBar(props: PipelineStepsBarProps) {
  const { trainEnabled, task } = props;
  const payload = taskPayload(task);
  const trainingRequested = typeof payload.train_model === "boolean" ? payload.train_model : trainEnabled;
  const result = task?.result;
  const model = result?.model as { status?: string; research_model_gate_status?: string } | undefined;
  const researchPassed = researchValidationPassed(model, result?.trusted_history_dataset);
  const bridge = result?.scoring_bridge as { status?: string; materialized_count?: number } | undefined;
  const phase = ["mirror", "factors", "targets", "train", "score"].indexOf(task?.stage ?? "");
  const completed = task?.status === "done" || task?.status === "completed";
  const evidence = [Boolean(result?.bar_mirror && result?.input_mirror), Boolean(result?.factors), Boolean(result?.targets), model?.status === "validated" || researchPassed, completed && (bridge?.materialized_count ?? 0) > 0];
  const statuses: PipelineStepStatus[] = evidence.map((done, index) => done ? "ready" : task?.status === "running" && phase > index ? "ready" : "wait");
  if (task?.status === "running" && phase >= 0) statuses[phase] = "running";
  if (!trainingRequested) statuses[3] = "skipped";
  if (model?.status === "rejected" && !researchPassed) statuses[3] = "blocked";
  if (bridge?.status === "SKIPPED" || bridge?.status === "PARTIAL") statuses[4] = "blocked";
  else if (payload.materialize_scores === false) statuses[4] = "skipped";
  if (task?.status === "failed") {
    const failureStages = (task.errors ?? []).map((error) => {
      const extras = error.extras as { stage?: unknown } | undefined;
      return typeof extras?.stage === "string" ? extras.stage : error.stage;
    });
    const failedPhase = failureStages.map((stage) => ["data_sync", "factor_preprocessing", "target_generation", "training", "scoring"].indexOf(String(stage))).find((index) => index >= 0);
    if (failedPhase !== undefined) {
      statuses[failedPhase] = "blocked";
      for (let index = failedPhase + 1; index < statuses.length; index += 1) statuses[index] = "wait";
    }
  }
  const steps: Array<{ label: string; hint: string; status: PipelineStepStatus; emoji: string; idx: number }> = [
    { idx: 1, label: "整理本地数据", hint: "把已同步行情与输入写入因子仓库", status: statuses[0], emoji: "⚡" },
    { idx: 2, label: "因子计算", hint: "对集合计算指标、缺失与标准化", status: statuses[1], emoji: "🧮" },
    { idx: 3, label: "目标生成", hint: "生成真实训练和验证收益标签", status: statuses[2], emoji: "🎯" },
    { idx: 4, label: trainingRequested ? researchPassed ? t("factorModelResearchValidatedPending") : "训练模型" : "训练模型（跳过）", hint: researchPassed ? t("factorModelResearchIndependentRequired") : "训练新候选；由本人确认激活", status: statuses[3], emoji: "🏋" },
    { idx: 5, label: "生成评分", hint: "用已激活模型发布评分", status: statuses[4], emoji: "📊" },
  ];
  return (
    <div className="pipeline-steps-bar" data-testid="pipeline-steps-bar">
      {steps.filter((step) => step.idx <= 3 || step.idx === 4 && trainingRequested || step.idx === 5 && !trainingRequested && (payload.materialize_scores === true || Boolean(bridge))).map((step, i, visibleSteps) => (
        <div key={step.idx} className="pipeline-step">
          <div
            data-testid={`pipeline-step-${step.idx}`}
            className={`ps-dot ${STEP_STATUS_CLASS[step.status]}`}
            title={`${STEP_STATUS_TITLE[step.status]}：${step.hint}`}
          >
            {step.status === "ready" ? (
              <CheckCircleOutlined />
            ) : step.status === "blocked" ? (
              <ExclamationCircleOutlined />
            ) : step.status === "running" ? (
              <SyncOutlined />
            ) : (
              <span>{step.idx}</span>
            )}
          </div>
          <div className="ps-label">
            <Text strong className="ps-label-title">{step.emoji} {step.label}</Text>
            <Text type="secondary" className="ps-label-hint text-xs">{STEP_STATUS_TITLE[step.status]}</Text>
          </div>
          {i < visibleSteps.length - 1 && (
            <div className="ps-connector" />
          )}
        </div>
      ))}
    </div>
  );
}

function shortId(value: string | null | undefined) {
  if (!value) return "-";
  return value.length > 18 ? `${value.slice(0, 10)}...${value.slice(-6)}` : value;
}

function factorSetOptionName(fs: FactorSet): string {
  const raw = String((fs as any).label ?? (fs as any).name ?? "").trim();
  if (raw && !/^[?\uFFFD\s]+$/.test(raw)) return raw;
  const id = String(fs.id ?? "");
  const codes = (fs.members ?? []).filter((member) => member.role === "feature").map((member) => member.factor_code).filter(Boolean);
  if (codes.length) return `${codes.slice(0, 3).join("、")}${codes.length > 3 ? "等" : ""}（未命名集合）`;
  return id || "未命名因子集合";
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

interface FactorModelSettingsProps {
  /**
   * 仅测试/Storybook 场景：允许以 initialTrainModel 初始化训练开关，
   * 避免在 vitest/jsdom 中为了定位 Switch 而依赖脆弱 DOM 索引。
   * 真实页面不传 => 默认 true。
   */
  initialTrainModel?: boolean;
}

export default function FactorModelSettings(props: FactorModelSettingsProps = {}) {
  const { initialTrainModel = true } = props;
  const ctx = useApp();
  const [overview, setOverview] = useState<FactorOverview | null>(null);
  const [models, setModels] = useState<FactorModelRun[]>([]);
  const [activeTask, setActiveTask] = useState<FactorPipelineTask | null>(null);
  const [researchRetrainId, setResearchRetrainId] = useState<string | null>(null);
  const [productionAdmissionId, setProductionAdmissionId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [acting, setActing] = useState<string | null>(null);
  const activationInFlightRef = useRef(false);
  const activationReviewRef = useRef(false);
  const [activationReview, setActivationReview] = useState<{ modelId: string; factorSetId: string; mode: "shadow" | "ridge"; runtimeIdentity: string } | null>(null);
  const activationEvidence = useFactorModelReviewEvidence();
  const [error, setError] = useState<string | null>(null);
  const [stateReadError, setStateReadError] = useState<string | null>(null);
  const [activationScoreError, setActivationScoreError] = useState<string | null>(null);
  const [fullRefresh, setFullRefresh] = useState(false);
  const [trainModel, setTrainModel] = useState<boolean>(initialTrainModel);
  const [materializeScores, setMaterializeScores] = useState(true);
  const [windowDays, setWindowDays] = useState(250);
  const [validationDays, setValidationDays] = useState(50);
  const [wizardStep, setWizardStep] = useState<FactorPipelineWizardStep>(props.initialTrainModel === false ? "score" : "prepare");
  const [prepareInputsOpen, setPrepareInputsOpen] = useState(false);
  const [runtimeOpen, setRuntimeOpen] = useState(false);
  const [researchStepsOpen, setResearchStepsOpen] = useState(false);
  const [scoreFullRefresh, setScoreFullRefresh] = useState(false);
  const wizardBootstrappedRef = useRef(false);
  const wizardPageRef = useRef<HTMLElement | null>(null);
  const [fallbackOpen, setFallbackOpen] = useState(false);
  const [fallbackReason, setFallbackReason] = useState("");
  // Task 7: 关联详情只读 Drawer 状态
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [drawerModelId, setDrawerModelId] = useState<string | null>(null);
  const [drawerModelDetail, setDrawerModelDetail] = useState<FactorModelRun | null>(null);
  const [drawerLoading, setDrawerLoading] = useState(false);
  const [drawerData, setDrawerData] = useState<ScoringModelRelations | null>(null);
  const [drawerReadError, setDrawerReadError] = useState<string | null>(null);
  const pageMounted = useRef(true);
  const loadRequest = useRef(0);
  const relationsRequest = useRef(0);
  const relationsTargetId = useRef<string | null>(null);
  useEffect(() => {
    pageMounted.current = true;
    return () => {
      pageMounted.current = false;
      relationsRequest.current += 1;
      relationsTargetId.current = null;
      loadRequest.current += 1;
    };
  }, []);
  // 任务进度追踪：已运行秒数 + 进度停滞标志（percent 30 秒未变则标记为停滞）
  const [elapsed, setElapsed] = useState(0);
  const [stalled, setStalled] = useState(false);
  const lastProgressRef = useRef<{ signature: string; time: number } | null>(null);
  // 预估时长：基于历史已完成任务统计
  const [eta, setEta] = useState<FactorPipelineEta | null>(null);
  const [trainingFactorSetId, setTrainingFactorSetId] = useState<string | null>(null);
  // Task 8：frozen 因子集合列表 + 流水线 5 阶段预览态（Step 4 训练会在启动时切 running）
  const [frozenFactorSets, setFrozenFactorSets] = useState<FactorSet[]>([]);
  const [setReadiness, setSetReadiness] = useState<Awaited<ReturnType<typeof api.scoringGetFactorSetReadiness>> | null>(null);
  const [readinessLoading, setReadinessLoading] = useState(false);
  const [readinessError, setReadinessError] = useState<string | null>(null);
  const [readinessRefreshKey, setReadinessRefreshKey] = useState(0);
  const readinessGeneration = useRef(0);
  const pipelineSectionRef = useRef<HTMLElement | null>(null);
  const modelsSectionRef = useRef<HTMLElement | null>(null);
  const taskSectionRef = useRef<HTMLElement | null>(null);
  const modeChosenRef = useRef(false);
  const submittedPayload = taskPayload(activeTask);
  const submittedTrainModel = typeof submittedPayload.train_model === "boolean" ? submittedPayload.train_model : activeTask?.stage === "train" || Boolean(activeTask?.result?.model);
  const submittedFullRefresh = submittedPayload.full_refresh === true;

  const loadAll = useCallback(async (showLoading = true) => {
    if (!pageMounted.current) return;
    const request = ++loadRequest.current;
    if (showLoading) setLoading(true);
    setError(null);
    try {
      // Factor-domain internal - DO NOT USE outside factor center
      // Task 8：使用 scoring* listFactorSets('frozen', 50) 拿 frozen 集合供显式下拉
      const factorSetLoader: Promise<any[]> =
        typeof api.scoringListFactorSetsAsFactor === "function"
          // Factor-domain internal - DO NOT USE outside factor center
          ? (api.scoringListFactorSetsAsFactor as any)("frozen", 50)
          : typeof (api as any).listFactorSets === "function"
          // Factor-domain internal - DO NOT USE outside factor center
          ? (api as any).listFactorSets("frozen", 50)
          : Promise.resolve([]);
      const [overviewData, modelData, tasks, frozenFactorSetsData] = await Promise.all([
        // Factor-domain internal - DO NOT USE outside factor center
        api.scoringGetOverviewAsFactor(),
        // Factor-domain internal - DO NOT USE outside factor center
        api.scoringGetFactorModelListAsFactor(20),
        // Factor-domain internal - DO NOT USE outside factor center
        api.scoringListTasks("factor_pipeline", 5),
        factorSetLoader,
      ]);
      const displayModels = await hydrateResearchModelPresentation<FactorModelRun>(modelData.items, (id) => api.getFactorModel(id));
      if (!pageMounted.current || request !== loadRequest.current) return;
      setOverview((current) => current?.runtime && current.runtime.version > overviewData.runtime?.version
        ? { ...overviewData, runtime: current.runtime } : overviewData);
      setStateReadError(null);
      setModels(displayModels);
      if (!modeChosenRef.current && props.initialTrainModel === undefined) {
        const currentModel = modelData.items.find((model: FactorModelRun) => model.id === overviewData.runtime?.active_model_run_id);
        if (["ridge", "shadow"].includes(overviewData.runtime?.weight_mode ?? "manual") && currentModel?.production_eligible === true && currentModel.status === "validated") {
          setTrainModel(false);
          setMaterializeScores(true);
        }
        modeChosenRef.current = true;
      }
      // Task 8：显式写入 frozenFactorSets 状态（只保留 frozen，作为下拉唯一数据源）
      const usableSets = Array.isArray(frozenFactorSetsData)
        ? frozenFactorSetsData.filter((item) => item && (item.status === "frozen" || !("status" in item)))
        : [];
      setFrozenFactorSets(usableSets);
      setReadinessRefreshKey((key) => key + 1);

      // 从因子中心进入时优先使用当前生产模型绑定的集合；否则保持手动选择策略。
      const preferredId = typeof window !== "undefined"
        ? window.localStorage.getItem("settings_pipeline_factor_set_id")
        : null;
      const preferredSet = preferredId ? usableSets.find((s) => s.id === preferredId) : null;
      setTrainingFactorSetId((current) => {
        if (preferredSet) return preferredSet.id;
        if (current && usableSets.some((set) => set.id === current)) return current;
        if (current) return null;
        return usableSets.length === 1 ? usableSets[0].id : null;
      });
      if (preferredSet) window.localStorage.removeItem("settings_pipeline_factor_set_id");
      // 优先选择运行中的任务，避免被最新的终态任务（cancelled/failed）覆盖
      const activeFromList = tasks.find((t) => !TERMINAL_TASK_STATES.has(t.status)) ?? null;
      const recentTask = activeFromList ?? tasks[0] ?? null;
      setActiveTask(recentTask);
      if (!wizardBootstrappedRef.current) {
        wizardBootstrappedRef.current = true;
        const recentPayload = taskPayload(recentTask);
        const currentModel = modelData.items.find((model: FactorModelRun) => model.id === overviewData.runtime?.active_model_run_id && model.production_eligible === true && model.status === "validated");
        const resultModelId = (recentTask?.result?.model as { model_run_id?: string } | undefined)?.model_run_id;
        const candidate = modelData.items.find((model: FactorModelRun) => model.id === resultModelId && model.production_eligible === true && model.status === "validated");
        if (preferredSet) {
          setWizardStep("train"); setTrainModel(true); setMaterializeScores(false);
        } else if (activeFromList && recentPayload.train_model === true) {
          setWizardStep("train"); setTrainModel(true); setMaterializeScores(false);
          const setId = recentPayload.factor_set_id;
          if (typeof setId === "string" && usableSets.some((set) => set.id === setId)) setTrainingFactorSetId(setId);
        } else if (currentModel && ["ridge", "shadow"].includes(overviewData.runtime?.weight_mode ?? "manual")) {
          setWizardStep("score"); setTrainModel(false); setMaterializeScores(true);
          const setId = currentModel.hyperparameters?.factor_set_id;
          if (typeof setId === "string") setTrainingFactorSetId(setId);
        } else if (candidate) {
          setWizardStep("review"); setTrainModel(true); setMaterializeScores(false);
          const setId = candidate.hyperparameters?.factor_set_id;
          if (typeof setId === "string") setTrainingFactorSetId(setId);
        } else if (props.initialTrainModel === false) {
          setWizardStep("score"); setTrainModel(false); setMaterializeScores(true);
        } else {
          setWizardStep("prepare"); setTrainModel(false); setMaterializeScores(false);
        }
      }
    } catch (err: any) {
      if (!pageMounted.current || request !== loadRequest.current) return;
      setStateReadError(err.message || t("factorModelLoadFailed"));
      setError(err.message || t("factorModelLoadFailed"));
    } finally {
      if (pageMounted.current && request === loadRequest.current) setLoading(false);
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
      api.scoringGetPipelineEta(submittedTrainModel, submittedFullRefresh).then(setEta).catch(() => { /* 预估失败不影响主流程 */ });
    };
    fetchEta();
    const etaTimer = window.setInterval(fetchEta, 30000);
    return () => window.clearInterval(etaTimer);
  }, [activeTask?.id, activeTask?.status, submittedTrainModel, submittedFullRefresh]);

  // 预计剩余时间：优先用本次任务自身的 percent 反推，percent 还没动时才退回历史中位数。
  // VIZ-0930-26：本轮实测 factor inputs 镜像段把 percent 冻在 27% 达 40 分钟，
  // 而历史中位数只有 432s —— 纯历史样本在数据规模变化（需镜像 132 万根 K 线）时严重误导。
  // 自反推的估算会随实际进度走，哪怕越算越久，也是诚实的"比预期慢"。
  const remainingSeconds = useMemo(() => {
    const pct = Number(activeTask?.percent ?? 0);
    if (elapsed > 0 && pct > 0 && pct < 100) {
      return Math.round((elapsed * (100 - pct)) / pct);
    }
    if (!eta || elapsed <= 0) return null;
    return Math.max(0, Math.round(eta.recommended_seconds - elapsed));
  }, [activeTask?.percent, eta, elapsed]);

  const averageCoverage = useMemo(() => {
    if (typeof overview?.model_average_coverage === "number") return overview.model_average_coverage;
    if (typeof overview?.average_coverage === "number") return overview.average_coverage;
    const rows = overview?.factor_coverage ?? [];
    if (!rows.length) return null;
    return rows.reduce((sum, item) => sum + Number(item.coverage || 0), 0) / rows.length;
  }, [overview]);

  // Task 8：Readiness 前端轻检查（T4 3 门），返回 ok + 失败原因数组
  const selectedFactorSet = useMemo<FactorSet | null>(() => {
    if (!trainModel || !trainingFactorSetId) return null;
    return frozenFactorSets.find((s) => s.id === trainingFactorSetId) ?? null;
  }, [trainModel, trainingFactorSetId, frozenFactorSets]);

  const checkTrainingReadiness = useCallback(async (factorSetId: string) => {
    const generation = ++readinessGeneration.current;
    setReadinessLoading(true);
    setReadinessError(null);
    setSetReadiness(null);
    try {
      const result = await api.scoringGetFactorSetReadiness(factorSetId);
      if (generation !== readinessGeneration.current) return;
      if (result.factor_set_id !== factorSetId) throw new Error("集合资格返回与当前选择不一致，请重新检查");
      setSetReadiness(result);
    } catch (err: any) {
      if (generation === readinessGeneration.current) setReadinessError(err?.message || "训练资格读取失败");
    } finally {
      if (generation === readinessGeneration.current) setReadinessLoading(false);
    }
  }, []);

  useEffect(() => {
    if (trainModel && trainingFactorSetId) void checkTrainingReadiness(trainingFactorSetId);
    else {
      ++readinessGeneration.current;
      setSetReadiness(null);
      setReadinessError(null);
      setReadinessLoading(false);
    }
    return () => { ++readinessGeneration.current; };
  }, [trainModel, trainingFactorSetId, readinessRefreshKey, checkTrainingReadiness]);

  const readiness = useMemo<{ ok: boolean; reasons: string[] }>(() => {
    const reasons: string[] = [];
    if (!trainModel) return { ok: true, reasons }; // 不训练模式不卡
    if (!selectedFactorSet) {
      reasons.push("请先选择用于训练的因子集合。");
      return { ok: false, reasons };
    }
    if (String(selectedFactorSet.asset_type ?? "stock").toLowerCase() !== "stock") reasons.push("当前真实训练流水线仅支持股票集合。");
    const members = Number(selectedFactorSet.n_members ?? (selectedFactorSet as any).member_count ?? 0);
    if (members < 1) reasons.push("集合为空（n_members < 1）。");
    // feature_count 字段：优先 feature_count（DTO 别名）；否则回退 member_count（当所有成员都是 feature 场景
    // 视为兼容）；再回退使用 members.length。
    const fsAny = selectedFactorSet as any;
    const inferredFeatures = Array.isArray(fsAny.members) && fsAny.members.length
      ? fsAny.members.filter((m: any) => !m.role || m.role === "feature").length : members;
    const featureCount = Number(fsAny.feature_count ?? fsAny.featureCount ?? inferredFeatures);
    if (featureCount < 1) reasons.push("集合没有 feature 角色因子（feature_count < 1）。");
    // 成员 trainable 100%：若后端在集合条目上给了 members_trainable_ratio/all_trainable
    // 则前端直接用；否则认为就绪（让后端强制门拦住不做双重假阳性）。
    if ("members_trainable_ratio" in fsAny) {
      if (Number(fsAny.members_trainable_ratio ?? 0) < 1) {
        reasons.push("成员中存在不可训练版本（members_trainable_ratio < 100%）。");
      }
    } else if ("all_members_trainable" in fsAny && fsAny.all_members_trainable === false) {
      reasons.push("成员中存在不可训练版本（all_members_trainable = false）。");
    }
    if (readinessLoading) reasons.push("正在检查所选集合的训练资格。");
    else if (readinessError) reasons.push(`训练资格读取失败：${readinessError}`);
    else if (!setReadiness || setReadiness.factor_set_id !== selectedFactorSet.id) reasons.push("所选集合的训练资格尚未确认。");
    else if (!setReadiness.ready) reasons.push(setReadiness.message || "所选集合未通过训练资格检查。");
    return { ok: reasons.length === 0, reasons };
  }, [trainModel, selectedFactorSet, setReadiness, readinessLoading, readinessError]);

  const startPipeline = async (purpose: "train" | "score" | "inputs") => {
    const doTrain = purpose === "train";
    const doScores = purpose === "score";
    if (!featureEnabled || !warehouseAvailable || taskRunning || acting) return;
    if (doTrain && (!readiness.ok || invalidWindows)) {
      setError(readiness.reasons.join("；"));
      return;
    }
    if (doScores && scoringModeIssue) { setError(scoringModeIssue); return; }
    setActing("pipeline");
    setError(null);
    try {
      // Factor-domain internal - DO NOT USE outside factor center
      const task = await api.scoringCreateTask({
        full_refresh: doTrain ? fullRefresh : doScores ? scoreFullRefresh : false,
        train_model: doTrain,
        materialize_scores: doScores,
        ...(doTrain ? { window_days: windowDays, validation_days: validationDays } : {}),
        factor_set_id: doTrain ? trainingFactorSetId : doScores ? activeScoringFactorSetId : undefined,
        ...(doScores && activeScoringModel ? {
          hyperparameters: {
            factor_set_id: activeScoringFactorSetId,
            expected_model_run_id: activeScoringModel.id,
            expected_weight_mode: runtime?.weight_mode,
          },
          factor_scope: { factor_set_id: activeScoringFactorSetId, asset_type: "stock" },
        } : {}),
        actor: "settings:pipeline",
        source_hint: "factor_model_settings",
      });
      setActiveTask(task);
      chooseWizardStep(doTrain ? "train" : doScores ? "score" : "prepare");
      if (doScores) setActivationScoreError(null);
      ctx.showToast("success", doTrain
        ? `训练任务已提交：${task.id}，完成后需本人激活新模型，再生成评分。`
        : `${t("factorModelStarted")}：${task.id}`);
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

  const requestActivation = (model: FactorModelRun, mode: "shadow" | "ridge") => {
    if (activationUnavailable || activationInFlightRef.current || activationReviewRef.current) return;
    if (model.production_eligible !== true || model.status !== "validated") return;
    activationReviewRef.current = true;
    setActivationReview({ modelId: model.id, factorSetId: String(model.hyperparameters?.factor_set_id || ""), mode, runtimeIdentity: JSON.stringify([overview?.runtime?.active_model_run_id, overview?.runtime?.weight_mode, overview?.runtime?.version]) });
    void activationEvidence.read(model.id, drawerOpen && !drawerLoading && drawerModelDetail?.id === model.id ? drawerModelDetail : null);
  };
  const closeActivationReview = () => { activationReviewRef.current = false; setActivationReview(null); activationEvidence.clear(); };

  const activate = async (model: FactorModelRun, mode: "shadow" | "ridge") => {
    if (activationUnavailable || activationInFlightRef.current) return;
    if (model.production_eligible !== true || model.status !== "validated") {
      setError(model.readiness_message || "模型正式运行资格未知或未通过，无法激活。");
      return;
    }
    activationInFlightRef.current = true;
    let viewRequest = relationsRequest.current;
    const isCurrentView = () => pageMounted.current && viewRequest === relationsRequest.current;
    setActing(`${mode}:${model.id}`);
    setError(null);
    setActivationScoreError(null);
    try {
      // Factor-domain internal - DO NOT USE outside factor center
      const newRuntime = await api.scoringActivateModel(model.id, mode, `settings:${mode}`);
      if (pageMounted.current) setOverview((current) => current ? { ...current, runtime: newRuntime } : current);
      if (isCurrentView()) {
        setTrainingFactorSetId(String(model.hyperparameters?.factor_set_id ?? "").trim() || null);
        chooseWizardStep("score");
        viewRequest = relationsRequest.current;
      }
      let submittedTask: FactorPipelineTask | null = null;
      try {
        const factorSetId = String(model.hyperparameters?.factor_set_id ?? "").trim();
        if (!factorSetId) throw new Error("该模型未记录冻结因子集合，无法生成绑定评分。");
        submittedTask = await api.scoringCreateTask({
          train_model: false,
          materialize_scores: true,
          factor_set_id: factorSetId,
          actor: "settings:activate_scores",
          source_hint: "settings:activate_scores",
          hyperparameters: {
            factor_set_id: factorSetId,
            expected_model_run_id: model.id,
            expected_weight_mode: newRuntime.weight_mode || mode,
          },
          factor_scope: { factor_set_id: factorSetId, asset_type: "stock" },
        });
        if (isCurrentView()) {
          setActiveTask(submittedTask);
          ctx.showToast("success", `模型已激活，评分任务已提交：${submittedTask.id}，${submittedTask.status === "queued" ? "已排队，等待生成" : "正在生成评分"}。`);
        }
      } catch (err: any) {
        const notice = "模型已激活，评分生成未启动：" + (err.message || String(err));
        if (isCurrentView()) {
          setActivationScoreError(notice);
          ctx.showToast("info", notice);
        }
      }
      await loadAll(false);
      // The task-list endpoint may lag behind the accepted submission.
      if (submittedTask && isCurrentView()) setActiveTask(submittedTask);
    } catch (err: any) {
      if (isCurrentView()) setError(err.message || t("factorModelActionFailed"));
    } finally {
      activationInFlightRef.current = false;
      if (pageMounted.current) setActing(null);
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
      setActivationScoreError(null);
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

  // Task 7: 打开关联详情只读 Drawer
  const openRelationsDrawer = async (model: FactorModelRun) => {
    const request = ++relationsRequest.current;
    relationsTargetId.current = model.id;
    const isCurrent = () => pageMounted.current && request === relationsRequest.current && relationsTargetId.current === model.id;
    setDrawerModelId(model.id);
    setDrawerOpen(true);
    setDrawerLoading(true);
    setDrawerData(null);
    setDrawerModelDetail(null);
    setDrawerReadError(null);
    try {
      // Factor-domain internal - DO NOT USE outside factor center
      const [relations, detail] = await Promise.allSettled([
        api.scoringGetModelRelations(model.id),
        api.getFactorModel(model.id),
      ]);
      if (!isCurrent()) return;
      // The scoring brief omits sample parameters. Only this exact model's
      // full detail can supply them; it does not replace scoring eligibility.
      if (detail.status === "fulfilled" && detail.value?.id === model.id) setDrawerModelDetail(detail.value);
      if (relations.status === "rejected") throw relations.reason;
      const data = relations.value;
      if (!data || data.model_id !== model.id) throw new Error(t("factorModelRelationsTargetMismatch"));
      setDrawerData(data);
    } catch (err: any) {
      if (!isCurrent()) return;
      setDrawerData(null);
      setDrawerReadError(err?.message || t("factorModelLoadFailed"));
    } finally {
      if (isCurrent()) setDrawerLoading(false);
    }
  };

  const closeRelationsDrawer = () => {
    relationsRequest.current += 1;
    relationsTargetId.current = null;
    setDrawerOpen(false);
    setDrawerModelId(null);
    setDrawerModelDetail(null);
    setDrawerData(null);
    setDrawerReadError(null);
    setDrawerLoading(false);
  };

  const runtime = overview?.runtime;
  const taskRunning = !!activeTask && !TERMINAL_TASK_STATES.has(activeTask.status);
  const featureEnabled = overview?.feature_enabled ?? overview?.config?.feature_enabled ?? false;
  const warehouseAvailable = overview?.health.warehouse_available ?? false;
  const invalidWindows = trainModel && validationDays >= windowDays;
  const activeScoringModel = runtime && ["ridge", "shadow"].includes(runtime.weight_mode)
    ? models.find((model) => model.id === runtime.active_model_run_id && model.production_eligible === true && model.status === "validated") : undefined;
  const activeScoringFactorSetId = typeof activeScoringModel?.hyperparameters?.factor_set_id === "string"
    ? activeScoringModel.hyperparameters.factor_set_id.trim() : undefined;
  const workflowFactorSetId = trainingFactorSetId || activeScoringFactorSetId || null;
  const associatedModels = workflowFactorSetId
    ? models.filter((model) => model.hyperparameters?.factor_set_id === workflowFactorSetId) : models;
  const scoringModeIssue = !activeScoringModel
      ? "当前没有已激活且可评分的模型。请先训练并由本人启用，或选择“仅计算输入”。"
      : !activeScoringFactorSetId ? "该模型未记录冻结因子集合，无法生成绑定评分。请重新训练，或仅计算因子输入。"
      : workflowFactorSetId !== activeScoringFactorSetId ? "当前集合尚未启用对应模型。请先验证并启用此集合的模型，再生成评分。" : null;
  const taskOutcome = activeTask ? pipelineOutcome(activeTask) : null;
  const setPipelineMode = (mode: "train" | "score" | "inputs") => {
    modeChosenRef.current = true;
    setTrainModel(mode === "train");
    setMaterializeScores(mode === "score");
  };
  const openFactorData = (tab: "inputs" | "coverage" | "tasks" = "inputs") => {
    try {
      window.localStorage.setItem("settings_data_center_tab", tab);
      if (trainingFactorSetId) window.localStorage.setItem("settings_pipeline_factor_set_id", trainingFactorSetId);
    } catch { /* Section navigation remains available. */ }
    openSettingsSection("data-center");
  };
  const chooseWizardStep = (step: FactorPipelineWizardStep) => {
    wizardBootstrappedRef.current = true;
    setWizardStep(step);
    setPipelineMode(step === "train" || step === "review" ? "train" : step === "score" ? "score" : "inputs");
    if (step !== wizardStep) setPrepareInputsOpen(false);
    closeRelationsDrawer();
    window.requestAnimationFrame(() => {
      wizardPageRef.current?.closest(".factor-model-settings")?.scrollIntoView?.({ behavior: "smooth", block: "start" });
      wizardPageRef.current?.focus?.({ preventScroll: true });
    });
  };
  const focusPipeline = (mode?: "train" | "score" | "inputs") => {
    chooseWizardStep(mode === "score" ? "score" : mode === "inputs" ? "prepare" : "train");
  };
  const focusTask = () => {
    chooseWizardStep(taskPurpose);
  };
  const focusModels = (candidate?: FactorModelRun) => {
    if (typeof candidate?.hyperparameters?.factor_set_id === "string") setTrainingFactorSetId(candidate.hyperparameters.factor_set_id);
    chooseWizardStep("review");
    if (candidate) void openRelationsDrawer(candidate);
  };
  const wizardModels = associatedModels;
  const runModelId = (activeTask?.result?.model as { model_run_id?: string } | undefined)?.model_run_id;
  const runModel = submittedTrainModel && ["done", "completed"].includes(activeTask?.status || "")
    ? models.find(model => model.id === runModelId) : undefined;
  const selectedTrainingModel = wizardModels.find(model => model.id === runModel?.id);
  const wizardCandidate = selectedTrainingModel?.status === "validated" && selectedTrainingModel.production_eligible === true ? selectedTrainingModel : undefined;
  const hasTrainingRecord = wizardModels.some((model) => ["validated", "pending_validation", "rejected", "trained", "done", "completed"].includes(model.status) && (model.production_eligible === true || model.training_mode === "warehouse"));
  const wizardActiveModel = !scoringModeIssue ? activeScoringModel : undefined;
  const bridge = activeTask?.result?.scoring_bridge as { model_run_id?: string; status?: string; materialized_count?: number } | undefined;
  const scoresPublished = Boolean(wizardActiveModel && bridge?.model_run_id === wizardActiveModel.id && bridge.status === "PUBLISHED" && (bridge.materialized_count ?? 0) > 0 && ["done", "completed"].includes(activeTask?.status ?? ""));
  const taskPurpose: FactorPipelineWizardStep = submittedTrainModel ? "train" : submittedPayload.materialize_scores === true || Boolean(bridge) ? "score" : "prepare";
  const preparationReady = !loading && Boolean(overview) && !stateReadError && featureEnabled && warehouseAvailable;
  const activationUnavailable = !preparationReady || taskRunning || Boolean(acting);
  const wizardSteps = [
    { id: "prepare" as const, title: "准备输入", state: loading ? "waiting" as const : preparationReady ? "complete" as const : "blocked" as const },
    { id: "train" as const, title: "训练模型", state: hasTrainingRecord ? "complete" as const : "waiting" as const },
    { id: "review" as const, title: "验证与启用", state: wizardActiveModel ? "complete" as const : hasTrainingRecord ? "current" as const : "waiting" as const },
    { id: "score" as const, title: "生成评分", state: scoresPublished ? "complete" as const : "waiting" as const },
  ];
  const currentIndex = ["prepare", "train", "review", "score"].indexOf(wizardStep);
  const nextDisabled = Boolean(acting) || loading || (wizardStep === "prepare" ? !preparationReady || prepareInputsOpen : wizardStep === "train" ? !hasTrainingRecord || taskRunning : wizardStep === "review" ? !wizardActiveModel : false);
  const nextReason = wizardStep === "prepare" && !preparationReady ? "确认功能和仓库可用后继续。" : wizardStep === "train" && !hasTrainingRecord ? "先完成所选集合的训练，再查看验证结果。" : wizardStep === "train" && taskRunning ? "训练正在执行，完成后再查看验证结果。" : wizardStep === "review" && !wizardActiveModel ? "先查看候选结果，并明确选择影子运行或正式启用。" : "配置与运行记录会保留，可返回上一页。";
  const drawerModel = models.find((model) => model.id === drawerModelId);
  const drawerModelEligible = drawerModel?.status === "validated" && drawerModel.production_eligible === true;
  const drawerResearchComparison = drawerModelDetail?.id === drawerModelId && isObservedResearchComparison(drawerModelDetail?.hyperparameters);
  const reviewModel = activationReview ? models.find(model => model.id === activationReview.modelId) : undefined;
  const reviewStillCurrent = !!reviewModel && reviewModel.status === "validated" && reviewModel.production_eligible === true
    && String(reviewModel.hyperparameters?.factor_set_id || "") === activationReview?.factorSetId
    && activationReview?.runtimeIdentity === JSON.stringify([runtime?.active_model_run_id, runtime?.weight_mode, runtime?.version]);
  const drawerEvidenceReady = drawerOpen && !drawerLoading && drawerData?.model_id === drawerModelId && !drawerData?.unbound_reason;
  const drawerTrainingSetId = drawerModel?.hyperparameters?.factor_set_id;
  const drawerSetAvailable = typeof drawerTrainingSetId === "string" && frozenFactorSets.some((set) => set.id === drawerTrainingSetId);
  const drawerTrustedResearch = isTrustedResearchDataset(drawerModel?.hyperparameters?.trusted_history_dataset)
    || drawerModelDetail?.id === drawerModelId && isTrustedResearchDataset(drawerModelDetail.hyperparameters?.trusted_history_dataset);
  const runPublished = Boolean(bridge?.status === "PUBLISHED" && (bridge.materialized_count ?? 0) > 0 && ["done", "completed"].includes(activeTask?.status ?? ""));
  const runCandidate = runModel?.status === "validated" && runModel.production_eligible === true ? runModel : undefined;
  const runNext = runPublished
    ? { description: "评分已发布，可查看本次任务、范围与批次。", label: "查看任务与批次", action: () => openFactorData("tasks") }
    : submittedTrainModel && activeTask && TERMINAL_TASK_STATES.has(activeTask.status)
      ? runModel ? { description: researchValidationPassed(runModel) ? t("factorModelResearchIndependentRequired") : runCandidate ? "本次候选模型已通过资格检查，下一步由你查看验证结果并启用。" : "本次候选未通过启用资格，请查看该模型的真实验证结果。", label: t("factorModelReviewCurrentCandidate"), action: () => focusModels(runModel) }
        : { description: t("factorModelCandidateEvidenceMissing"), label: t("factorModelCheckCurrentTrainingTask"), action: () => openFactorData("tasks") }
      : activeScoringModel && activeScoringFactorSetId
        ? { description: "已有可评分的活动模型，可以检查输入后更新模型评分。", label: "配置模型评分", action: () => focusPipeline("score") }
        : submittedPayload.materialize_scores === false && !submittedTrainModel
          ? { description: "因子输入计算已结束，建议检查数据覆盖，再决定是否训练新模型。", label: "检查输入覆盖", action: () => openFactorData("coverage") }
          : { description: "目前没有可启用的合格模型。选择冻结集合，用本地数据重新训练。", label: "配置新模型训练", action: () => focusPipeline("train") };
  const runOutcomeDetail = taskOutcome && activeTask?.result?.completion_status === "AWAITING_MODEL_ACTIVATION" && !runCandidate && !activeScoringModel
    ? "评分未生成。当前没有可启用的合格模型，请选择冻结集合重新训练；通过验证后，再由你启用。"
    : taskOutcome?.detail;

  // ─── Task 3：ChainStepsBar 状态派生（设置页只读视角）─────────────
  const chainState: ChainState = React.useMemo<ChainState>(() => {
    const factorWarehouseReady = featureEnabled && warehouseAvailable;
    // Task8 只加载了 frozen 集合 → 此处用 frozenFactorSets 作为「存在 frozen」判断；
    // 「集合存在/成员就绪」按宽松处理：frozenFactorSets 至少 1 条 → 集合存在，成员已就绪
    const anyFactorSetCreated = frozenFactorSets.length > 0;
    const relevantSets = workflowFactorSetId ? frozenFactorSets.filter((set) => set.id === workflowFactorSetId) : frozenFactorSets;
    const anyFactorSetMembersReady = relevantSets.some(
      (fs: any) => {
        const ms = (fs as any).members ?? [];
        const members = ms.length || Number((fs as any).n_members ?? (fs as any).member_count ?? 0);
        const features = Number((fs as any).feature_count ?? (ms.length ? ms.filter((m: any) => m.role === "feature").length : members));
        return members > 0 && features > 0;
      }
    );
    const anyFactorSetFrozen = relevantSets.length > 0 && (!selectedFactorSet || readiness.ok);
    const relevantModels = workflowFactorSetId ? models.filter((model) => model.hyperparameters?.factor_set_id === workflowFactorSetId) : models;
    const anyModelTrained = relevantModels.some((m) => {
      const s = String(m.status ?? "").toLowerCase();
      return ["validated", "pending_validation", "trained", "rejected", "done", "completed"].includes(s) && (m.production_eligible === true || m.training_mode === "warehouse");
    });
    const anyModelValidated = relevantModels.some((m) => m.status === "validated" && m.production_eligible === true);
    const decisionModeEqualsFormalActive =
      ((runtime?.weight_mode === "ridge") || (runtime as any)?.decision_mode === "formal") &&
      Boolean(activeScoringModel);
    const pipelineReady =
      factorWarehouseReady && anyFactorSetFrozen && decisionModeEqualsFormalActive;
    return {
      factorWarehouseReady,
      anyFactorSetCreated,
      anyFactorSetMembersReady,
      anyFactorSetFrozen,
      anyModelTrained,
      anyModelValidated,
      decisionModeEqualsFormalActive,
      pipelineReady,
    };
  }, [featureEnabled, warehouseAvailable, frozenFactorSets, models, runtime, workflowFactorSetId, selectedFactorSet, readiness.ok, activeScoringModel]);

  // 设置页：Step 2/3/4/5 的直达按钮直接跳回「因子中心 Tab」（按任务 7 规范设置页不编辑集合）
  const handleChainGoStep = (stepIdx: number) => {
    if (stepIdx === 1) { chooseWizardStep("prepare"); return; }
    if (stepIdx === 5) {
      focusPipeline("train");
      return;
    }
    if (stepIdx === 6 || stepIdx === 7) {
      if (!selectedTrainingModel || (stepIdx === 7 && !wizardCandidate)) { setError(t("factorModelCandidateEvidenceMissing")); return; }
      chooseWizardStep("review");
      void openRelationsDrawer(selectedTrainingModel);
      return;
    }
    if (stepIdx === 8) {
      focusPipeline("score");
      return;
    }
    // 其他 Step 不在设置页提供写入口 → 直接跳因子中心
    navigateApp(trainingFactorSetId ? `/factors?set_id=${encodeURIComponent(trainingFactorSetId)}&next=factor-center` : "/factors?next=factor-center");
  };

  const columns = [
    {
      title: t("factorModelModelId"),
      width: 180,
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
      width: 260,
      dataIndex: "status",
      key: "status",
      render: (value: string, record: FactorModelRun) => (
        <Space direction="vertical" size={2}>
          <Tooltip title={researchValidationPassed(record) ? t("factorModelResearchIndependentRequired") : record.rejection_reason || undefined}>
            <Tag color={researchValidationPassed(record) || value === "pending_validation" ? "gold" : value === "validated" ? "green" : "red"}>
              {researchValidationPassed(record) ? t("factorModelResearchValidatedPending") : value === "pending_validation" ? t("productionCandidate.modelPending") : value === "validated" ? t("factorModelValidated") : value === "rejected" ? t("factorModelRejected") : value}
            </Tag>
          </Tooltip>
          {record.production_eligible !== true && <Text type="warning" style={{ fontSize: 12 }}>
            {readableModelReadiness(record, record.readiness_code === "MODEL_FACTOR_VERSION_MISMATCH"
              ? "冻结集合与模型权重版本不一致，请重新训练。"
              : record.readiness_message || "正式运行资格尚未通过，请查看验证详情。")}
          </Text>}
        </Space>
      ),
    },
    {
      title: t("factorModelValidationIc"),
      width: 130,
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
      title: <Tooltip title={t("factorModelSamplesHelp")}><span>{t("factorModelSamples")} <QuestionCircleOutlined /></span></Tooltip>,
      width: 120,
      dataIndex: "sample_count",
      key: "sample_count",
      render: (_: unknown, record: FactorModelRun) => typeof record.sample_count === "number" && Number.isFinite(record.sample_count)
        ? record.sample_count.toLocaleString() : t("factorModelEvidenceNotRecorded"),
    },
    {
      title: t("factorModelFactorSet"),
      width: 170,
      key: "factor_set_id",
      render: (_: unknown, record: FactorModelRun) => {
        const factorSetId = record.hyperparameters?.factor_set_id;
        return factorSetId ? (
          <Tooltip title="该模型训练时使用的冻结因子集合；到“因子中心 → 因子模型”可展开查看具体成员因子和版本。">
            <Space size={4}>
              <Tag color="blue">{shortId(String(factorSetId))}</Tag>
              {record.weights?.length ? <Text type="secondary">{record.weights.length} 个因子</Text> : null}
            </Space>
          </Tooltip>
        ) : (
          <Tooltip title="未记录因子集合，无法确认该模型由哪些因子训练产生。">
            <Tag>未关联</Tag>
          </Tooltip>
        );
      },
    },
    {
      title: <Tooltip title={t("factorModelInputAvailableAtHelp")}><span>{t("factorModelInputAvailableAt")} <QuestionCircleOutlined /></span></Tooltip>,
      width: 170,
      dataIndex: "data_cutoff_at",
      key: "data_cutoff_at",
      render: (value: string | null) => {
        const rated = rateDataCutoff(value, overview?.latest_trade_date ?? null);
        return ratedCell(<span>{rated.text}</span>, rated, { align: "left" });
      },
    },
    {
      title: t("factorModelActions"),
      width: 200,
      key: "actions",
      fixed: "right" as const,
      render: (_: unknown, record: FactorModelRun) => (
        <Space size={4} wrap>
          {isTrustedResearchDataset(record.hyperparameters?.trusted_history_dataset) && record.feature_versions?.turnover_z20 === 3 && Object.keys(record.feature_versions).length === 1 && <Button size="small" onClick={() => setProductionAdmissionId(record.id)}>{t("productionAdmission.title")}</Button>}
          <Button
            size="small"
            icon={<EyeOutlined />}
            disabled={activationUnavailable || !!activationReview || record.status !== "validated" || record.production_eligible !== true}
            title={record.production_eligible === true ? undefined : record.readiness_message || "正式运行资格未通过"}
            loading={acting === `shadow:${record.id}`}
            onClick={() => requestActivation(record, "shadow")}
          >
            {t("factorModelShadow")}
          </Button>
          <Button
            size="small"
            type="primary"
            icon={<CheckCircleOutlined />}
            disabled={activationUnavailable || !!activationReview || record.status !== "validated" || record.production_eligible !== true}
            title={record.production_eligible === true ? undefined : record.readiness_message || "正式运行资格未通过"}
            loading={acting === `ridge:${record.id}`}
            onClick={() => requestActivation(record, "ridge")}
          >
            {t("factorModelRidge")}
          </Button>
        </Space>
      ),
    },
    // ── Task 7 只读关联列追加（不删/不改既有列） ──
    {
      title: "关联因子集合",
      key: "linked_factor_set",
      width: 260,
      render: (_: unknown, record: FactorModelRun) => {
        const hpAny = record.hyperparameters as any;
        // 优先从 relations 返回拿；未加载时退回到 hyperparameters
        const rawName: string | null | undefined =
          (drawerData && drawerData.model_id === record.id ? drawerData.factor_set_name : null) ??
          hpAny?.factor_set_name ??
          hpAny?.factorset_label ??
          null;
        const id: string | null | undefined =
          (drawerData && drawerData.model_id === record.id ? drawerData.factor_set_id : null) ??
          hpAny?.factor_set_id ??
          hpAny?.factorset_id ??
          null;
        const linkedSet = frozenFactorSets.find((set) => set.id === id);
        const name = rawName && !/^[?\uFFFD\s]+$/.test(rawName)
          ? rawName : linkedSet ? factorSetOptionName(linkedSet) : null;
        const nRaw: number | string | null | undefined =
          (drawerData && drawerData.model_id === record.id ? drawerData.n_members : null) ??
          hpAny?.n_members ??
          hpAny?.factorset_member_count ??
          linkedSet?.n_members ??
          null;
        const n: number | null = nRaw == null || Number.isNaN(Number(nRaw)) ? null : Number(nRaw);
        if (!name || !id || n == null) {
          return <Text type="secondary" data-testid={`linked-fs-dash-${record.id}`}>—</Text>;
        }
        return (
          <Button
            type="link"
            size="small"
            className="linked-fs-cell"
            style={{ padding: 0, height: "auto", whiteSpace: "normal", textAlign: "left" }}
            data-testid={`linked-fs-cell-${record.id}`}
            onClick={() => navigateApp(`/factors?set_id=${encodeURIComponent(String(id))}`)}
            title={`${name} / ${id} / ${n} 成员`}
          >
            <Space direction="vertical" size={0} style={{ lineHeight: 1.45 }}>
              <Text strong>{name}</Text>
              <Text type="secondary" style={{ fontSize: 12 }}>
                {shortId(String(id))}
              </Text>
              <Text type="secondary" style={{ fontSize: 12 }}>
                n={n}
              </Text>
            </Space>
          </Button>
        );
      },
    },
    {
      title: "关联详情（只读）",
      key: "linked_details",
      width: 130,
      fixed: "right" as const,
      render: (_: unknown, record: FactorModelRun) => (
        <Button
          size="small"
          icon={<EyeOutlined />}
          data-testid={`view-relations-${record.id}`}
          onClick={() => openRelationsDrawer(record)}
        >
          查看
        </Button>
      ),
    },
  ];

  const taskPanel = activeTask && taskPurpose === wizardStep ? (
      <section className="factor-run-section" ref={taskSectionRef} tabIndex={-1} aria-label="最近一次运行">
        <div className="factor-section-title"><div><span className="factor-section-kicker">实际运行</span><h2>最近一次运行</h2></div>{activeTask && <Tag>提交用途 · {typeof submittedPayload.train_model === "boolean" ? submittedTrainModel ? "训练新模型" : submittedPayload.materialize_scores === true ? "更新模型评分" : "仅计算输入" : "历史任务"}</Tag>}</div>
        <p className="factor-section-description">这里记录已经提交的任务；切换步骤和修改参数不会改变这次运行。</p>
        <PipelineStepsBar trainEnabled={submittedTrainModel} task={activeTask} />

        {activeTask && taskOutcome && (
          <div className={`factor-task-strip factor-task-strip--${taskOutcome.color}`}>
            <div>
              <strong>
                {t("factorModelRecentTask")}: {taskOutcome.label}
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
              <span>{runOutcomeDetail}</span>
              <span>任务：{activeTask.id}</span>
              {eta != null && eta.sample_count > 0 && activeTask.status !== "done" && activeTask.status !== "completed" && activeTask.status !== "failed" && activeTask.status !== "cancelled" && (
                <span className="factor-task-eta-hint">{template("factorModelEtaBasedOnHistory", { count: eta.sample_count })}</span>
              )}
              {stalled && activeTask.status !== "done" && activeTask.status !== "completed" && activeTask.status !== "failed" && activeTask.status !== "cancelled" && (
                <span className="factor-task-stalled">{t("factorModelTaskStalled")}</span>
              )}
            </div>
            <Tag color={taskOutcome.color}>
              {activeTask.status === "done" || activeTask.status === "completed" ? "任务已结束" : activeTask.status === "queued" ? "等待执行" : activeTask.status === "running" ? "执行中" : activeTask.status === "failed" ? "失败" : "已取消"}
            </Tag>
            <Progress percent={Math.round(activeTask.percent)} status={taskOutcome.progress} />
          </div>
        )}
        {taskRunning && <Popconfirm title={t("factorModelCancelConfirmTitle")} description={t("factorModelCancelConfirmDesc")} okText={t("factorModelCancel")} cancelText={t("cancel")} okButtonProps={{ danger: true }} onConfirm={cancelPipeline}><Button danger icon={<StopOutlined />} loading={acting === "cancel"}>{t("factorModelCancel")}</Button></Popconfirm>}
        {activeTask && !taskRunning && <div className="factor-task-next-action"><span>{runNext.description}</span><Button onClick={runNext.action}>{runNext.label}</Button></div>}
      </section>

  ) : null;
  const operationUnavailable = taskRunning || !preparationReady || Boolean(acting);
  const trainingDisabled = operationUnavailable || invalidWindows || !readiness.ok;
  const pageTitle = { prepare: "准备输入", train: "训练模型", review: "验证与启用", score: "生成评分" }[wizardStep];
  const pageDescription = {
    prepare: "确认输入和仓库可用，再进入训练。",
    train: "选择冻结集合并训练候选模型。训练完成后，下一页查看验证结果。",
    review: "查看模型来源、因子版本和验证指标，再由你确认启用。",
    score: "使用已启用的模型生成评分，评分固定绑定该模型版本。",
  }[wizardStep];

  return (
    <div className="factor-model-settings">
      <div className="factor-settings-head">
        <div>
          <p className="panel-kicker">数据 · 模型 · 评分</p>
          <h1>因子流水线</h1>
          <p className="factor-page-intro">按顺序准备输入、训练模型、确认启用，再生成评分。</p>
        </div>
        <Space wrap>
          <Button onClick={() => setRuntimeOpen(true)}>运行详情</Button>
          <Button onClick={() => setResearchStepsOpen(true)}>完整研发步骤</Button>
          {runtime?.weight_mode !== "manual" && <Button danger icon={<RollbackOutlined />} loading={acting === "fallback"} onClick={() => setFallbackOpen(true)}>{t("factorModelFallback")}</Button>}
          <Button aria-label={t("factorModelRefresh")} icon={<ReloadOutlined />} loading={loading} onClick={() => void loadAll()} />
        </Space>
      </div>

      <FactorPipelineWizardNav step={wizardStep} steps={wizardSteps} onSelect={chooseWizardStep} busy={loading || Boolean(acting)} />
      {error && <Alert type="error" showIcon closable message={error} onClose={() => setError(null)} />}
      {activationScoreError && <Alert type="warning" showIcon closable message={activationScoreError} onClose={() => setActivationScoreError(null)} />}
      {taskRunning && taskPurpose !== wizardStep && <div className="factor-wizard-running-note"><SyncOutlined spin /><span>后台任务仍在执行，当前操作只修改下一次配置。</span><Button type="link" onClick={focusTask}>查看运行进度</Button></div>}

      <section className="factor-wizard-page" data-testid={`wizard-page-${wizardStep}`} ref={wizardPageRef} tabIndex={-1} aria-label={`步骤 ${currentIndex + 1}：${pageTitle}`}>
        {wizardStep !== "prepare" && <div className="factor-wizard-page-head"><h2>{pageTitle}</h2><p>{pageDescription}</p>{workflowFactorSetId && <Tag>当前集合 · {frozenFactorSets.find((set) => set.id === workflowFactorSetId) ? factorSetOptionName(frozenFactorSets.find((set) => set.id === workflowFactorSetId)!) : workflowFactorSetId}</Tag>}</div>}

        {wizardStep === "prepare" && <>
          <FactorPipelinePrepareStep loading={loading} featureEnabled={featureEnabled} warehouseAvailable={warehouseAvailable} coverage={averageCoverage} latestDate={overview?.latest_trade_date ?? null} error={stateReadError} busy={Boolean(acting)} onEnable={() => void toggleFeature(true)} onInitialize={() => void initializeWarehouse()} onRefresh={() => void loadAll()} onCoverage={() => openFactorData("coverage")} onViewChange={(view) => setPrepareInputsOpen(view === "inputs")} />
          {!prepareInputsOpen && <div className="factor-wizard-input-calculation"><div><strong>已有本地输入，需要重新计算？</strong><p>仅整理已有输入、计算因子与目标标签。之后仍需训练和启用模型。</p></div><Button disabled={operationUnavailable} loading={acting === "pipeline"} onClick={() => void startPipeline("inputs")}>仅计算已有输入</Button></div>}
          {!prepareInputsOpen && taskPanel}
        </>}

        {wizardStep === "train" && <>
          <section className="factor-pipeline-section" ref={pipelineSectionRef} tabIndex={-1} aria-label="本次运行配置">
                      <div
            className="factor-pipeline-train-block mt-3 rounded-lg border border-slate-200 bg-slate-50 p-3"
            data-testid="train-mode-block"
          >
            <div className="pipeline-factor-set-row mb-2">
              <label
                htmlFor="pipeline-factor-set-select"
                className="text-sm font-medium text-slate-700 mr-2"
              >
                选择用于训练的因子集合
              </label>
              <AntSelect
                id="pipeline-factor-set-select"
                data-testid="factor-set-select"
                className="min-w-[320px] flex-1"
                variant="outlined"
                disabled={frozenFactorSets.length === 0}
                value={trainingFactorSetId ?? undefined}
                onChange={(value) => setTrainingFactorSetId(value ?? null)}
                options={frozenFactorSets.map((fsRaw) => {
                  const fs = fsRaw as any;
                  return {
                    value: fs.id,
                    label: `${factorSetOptionName(fs)}（${fs.n_members ?? fs.member_count ?? 0} 因子）`,
                  };
                })}
                placeholder={
                  frozenFactorSets.length === 0
                    ? "暂无已冻结集合"
                    : "请选择一个已冻结的集合"
                }
              />
              {frozenFactorSets.length === 1 && (
                <Tag
                  data-testid="auto-preselect-badge"
                  color="green"
                  className="preselect-badge border border-green-300 bg-green-50 text-green-800"
                  icon={<CheckCircleOutlined />}
                >
                  已自动预选：
                  {factorSetOptionName(frozenFactorSets[0])}（
                  {frozenFactorSets[0].n_members ?? (frozenFactorSets[0] as any).member_count ?? 0} 因子）
                </Tag>
              )}
              {frozenFactorSets.length === 0 && (
                <Button
                  type="primary"
                  data-testid="goto-factor-center"
                  onClick={() => navigateApp("/factors?next=pipeline")}
                  className="goto-factor-center-btn"
                >
                  去因子中心新建→
                </Button>
              )}
            </div>

            <Alert
              showIcon
              type={readinessLoading ? "info" : readiness.ok ? "success" : "warning"}
              message={readiness.ok ? "集合训练资格已确认" : readinessLoading ? "正在检查训练资格" : "暂不能启动训练"}
              description={readiness.ok ? "集合前置检查已通过；数据覆盖和模型统计验证将在训练任务中检查。" : readiness.reasons.join("；")}
              style={{ marginBottom: 12 }}
              action={readinessError && trainingFactorSetId
                ? <Button onClick={() => void checkTrainingReadiness(trainingFactorSetId)}>重试训练资格检查</Button>
                : !readiness.ok && !readinessLoading && trainingFactorSetId
                  ? <Button onClick={() => navigateApp(`/factors?set_id=${encodeURIComponent(trainingFactorSetId)}&next=pipeline`)}>查看并修复集合</Button>
                  : undefined}
            />

          </div>
            <div className="factor-pipeline-controls">
              <label><span>{t("factorModelWindow")} ({t("factorModelDays")})</span><InputNumber min={60} max={1000} value={windowDays} onChange={(value) => setWindowDays(Number(value ?? 250))} /></label>
              <label><span>{t("factorModelValidation")} ({t("factorModelDays")})</span><InputNumber min={20} max={250} value={validationDays} onChange={(value) => setValidationDays(Number(value ?? 50))} /></label>
            </div>
            {invalidWindows && <Alert type="error" showIcon message={t("factorModelInvalidWindows")} />}
            <details className="factor-advanced-options"><summary>高级选项 <span>全量重算</span></summary><label className="factor-wizard-switch"><span>{t("factorModelFullRefresh")}</span><Switch checked={fullRefresh} onChange={setFullRefresh} /></label><p>历史输入修正后，可按需重新计算全部范围。</p></details>
            {!preparationReady && !loading && <Alert type="warning" showIcon message="请先完成输入准备" description="功能和仓库可用后才能提交训练。" action={<Button onClick={() => chooseWizardStep("prepare")}>返回输入准备</Button>} />}
            <div className="factor-submit-row"><div><strong>将训练一个新的候选模型</strong><p>本次不会自动启用模型。验证合格后，由你在下一页查看并启用；启用后会提交评分任务。</p></div><Tooltip title={taskRunning ? "当前有任务正在执行，请等待完成。" : !readiness.ok ? readiness.reasons.join("；") : undefined}><Button type="primary" icon={<PlayCircleOutlined />} className="pipeline-run-train" disabled={trainingDisabled} loading={acting === "pipeline"} onClick={() => void startPipeline("train")}>启动流水线（训练模式）</Button></Tooltip></div>
          </section>
          {taskPanel}
        </>}

        {wizardStep === "review" && <>
                <section className="factor-model-list" ref={modelsSectionRef} tabIndex={-1} aria-label="模型验证与启用">
        <div className="factor-section-title">
          <Text strong>候选模型</Text>
          <Tag>{wizardModels.length}</Tag>
        </div>
        <Text type="secondary">影子评分用于观察，正式模型参与模型决策；启用后会自动提交固定该版本的评分任务。</Text>
        {wizardModels.some(model => model.status === "pending_validation" && model.training_mode === "warehouse")
          ? <Alert type="info" showIcon message={t("productionCandidate.modelPending")} description={t("productionCandidate.pending")} />
          : wizardModels.length > 0 && !wizardModels.some((model) => model.status === "validated" && model.production_eligible === true) && <div className="factor-model-empty-ready"><ExclamationCircleOutlined /><div><strong>目前没有可启用的合格模型</strong><p>旧验证记录仍需通过来源和版本资格检查。查看具体原因，或使用冻结集合重新训练。</p></div><Button onClick={() => focusPipeline("train")}>配置重新训练</Button></div>}
        {wizardModels.length === 0 && <Alert type="info" showIcon message="还没有此集合的训练结果" description="先返回训练步骤，使用冻结集合生成候选模型。" action={<Button onClick={() => chooseWizardStep("train")}>去训练模型</Button>} />}
        <Table<FactorModelRun>
          rowKey="id"
          size="small"
          loading={loading}
          columns={columns}
          dataSource={wizardModels}
          pagination={{ pageSize: 8, hideOnSinglePage: true }}
          locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t("factorModelNoModels")} /> }}
          scroll={{ x: 1620 }}
        />
      </section>

          <div className="factor-goto-center-row"><Button data-testid="goto-factor-center-global" onClick={() => navigateApp("/factors")}>↗ 前往因子中心</Button></div>
        </>}

        {wizardStep === "score" && <>
          <section className="factor-pipeline-section" ref={pipelineSectionRef} tabIndex={-1} aria-label="本次评分配置">
            <div data-testid="no-train-mode-block"><Alert type={scoringModeIssue ? "warning" : "info"} showIcon className="pipeline-no-train-banner" data-testid="pipeline-no-train-banner" message="本次不训练新模型" description={scoringModeIssue ?? "计算最新输入后，使用已启用的模型生成评分。启用后已提交的任务会显示在下方。"} action={scoringModeIssue ? <Button onClick={() => chooseWizardStep("prepare")}>仅计算输入</Button> : undefined} /></div>
            {wizardActiveModel ? <dl className="factor-wizard-score-binding"><div><dt>评分模型</dt><dd>{wizardActiveModel.id}</dd></div><div><dt>冻结集合</dt><dd>{activeScoringFactorSetId}</dd></div><div><dt>运行方式</dt><dd>{runtime?.weight_mode === "shadow" ? "影子观察" : "正式模型"}</dd></div></dl> : <div className="factor-wizard-repair"><p>先完成训练与启用，再回到本页生成评分。</p><Button onClick={() => chooseWizardStep(wizardCandidate ? "review" : "train")}>{wizardCandidate ? "去验证与启用" : "去训练模型"}</Button></div>}
            <details className="factor-advanced-options"><summary>高级选项 <span>评分全量重算</span></summary><label className="factor-wizard-switch"><span>{t("factorModelFullRefresh")}</span><Switch checked={scoreFullRefresh} onChange={setScoreFullRefresh} /></label></details>
            <div className="factor-submit-row"><div><strong>将使用已启用模型更新评分</strong><p>{wizardActiveModel ? `使用${runtime?.weight_mode === "shadow" ? "影子观察" : "正式"}模型 ${shortId(wizardActiveModel.id)}，评分与该模型版本绑定。` : "当前尚无可评分模型，完成启用后才能提交。"}</p></div><Button type="primary" icon={<PlayCircleOutlined />} className="pipeline-run-no-train" disabled={operationUnavailable || Boolean(scoringModeIssue)} loading={acting === "pipeline"} onClick={() => void startPipeline("score")}>启动流水线（不训练模式）</Button></div>
          </section>
          {taskPanel}
        </>}

        {!prepareInputsOpen && <footer className="factor-wizard-footer"><div>{currentIndex > 0 && <Button onClick={() => chooseWizardStep((["prepare", "train", "review"] as const)[currentIndex - 1])}>上一页</Button>}<p>{nextReason}</p></div>{currentIndex < 3 ? <Button type="primary" disabled={nextDisabled} onClick={() => chooseWizardStep((["train", "review", "score"] as const)[currentIndex])}>下一步：{["训练模型", "验证与启用", "生成评分"][currentIndex]}</Button> : <Button onClick={() => openFactorData("tasks")}>查看任务与批次</Button>}</footer>}
      </section>

      <Modal open={runtimeOpen} title="运行详情" footer={null} onCancel={() => setRuntimeOpen(false)} width={960} destroyOnHidden><div className="factor-model-settings">      <div className="factor-runtime-grid">
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
          <Tooltip title={runtime?.active_model_run_id ? "当前启用的模型版本；影子运行用于观察评分，正式启用才会用于模型决策。" : "当前使用手工权重。要生成模型评分，需先训练并启用一个合格模型。"}>
            <QuestionCircleOutlined style={{ color: "#94a3b8", fontSize: 12, marginLeft: 4, cursor: "help" }} />
          </Tooltip>
          <Tooltip title={runtime?.active_model_run_id || undefined}>
            <strong>
              {runtime?.active_model_run_id
                ? shortId(runtime.active_model_run_id)
                : <Text type="secondary">{t("factorModelNoModel")}</Text>}
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
          <Tooltip title={t("factorCoverageLatestRecordHelp")}><strong>{overview?.latest_trade_date || "—"}</strong></Tooltip>
        </div>
        <div>
          <FactorCoverageSummary overview={overview} />
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
      </div></div></Modal>
      <Modal open={researchStepsOpen} title="完整研发步骤" footer={null} onCancel={() => setResearchStepsOpen(false)} width={1100} destroyOnHidden><ChainStepsBar state={chainState} disabledSteps={{ ...(!selectedTrainingModel ? { 6: t("factorModelCandidateEvidenceMissing") } : {}), ...(!wizardCandidate ? { 7: t("factorModelCandidateEvidenceMissing") } : {}) }} onGoStep={(index) => { setResearchStepsOpen(false); handleChainGoStep(index); }} /></Modal>

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

      <Modal title={t("factorModelEnableReviewTitle")} open={!!activationReview} onCancel={closeActivationReview}
        okText={t("factorModelEnableReviewConfirm")} cancelText={t("cancel")} confirmLoading={!!acting}
        okButtonProps={{ disabled: activationUnavailable || !reviewStillCurrent || !activationEvidence.readyFor(activationReview?.modelId) }}
        onOk={() => { if (!activationReview || !reviewStillCurrent || activationUnavailable || !reviewModel || !activationEvidence.readyFor(activationReview.modelId)) return; const mode = activationReview.mode; closeActivationReview(); void activate(reviewModel, mode); }}>
        {activationReview && <>
          <FactorModelReviewEvidenceNotice state={activationEvidence.state} onRetry={activationEvidence.retry} />
          <FactorModelResearchWarning hyperparameters={activationEvidence.readyFor(activationReview.modelId) ? activationEvidence.state?.model?.hyperparameters : null} />
          <Descriptions size="small" column={1} bordered>
            <Descriptions.Item label={t("factorModelModelId")}>{activationReview.modelId}</Descriptions.Item>
            <Descriptions.Item label={t("factorModelFactorSet")}>{activationReview.factorSetId || t("factorModelEnableUnbound")}</Descriptions.Item>
            <Descriptions.Item label={t("factorModelEnableCurrentModel")}>{runtime?.active_model_run_id || t("factorModelEnableNoActive")}</Descriptions.Item>
            <Descriptions.Item label={t("factorModelEnableCurrentMode")}>{enumLabel("factorMode", runtime?.weight_mode || "manual")}</Descriptions.Item>
            <Descriptions.Item label={t("factorModelEnableTargetMode")}>{enumLabel("factorMode", activationReview.mode)}</Descriptions.Item>
            <Descriptions.Item label={t("factorModelEnableEligibility")}>{reviewModel?.production_eligible === true ? t("factorModelEnableEligible") : reviewModel?.readiness_message || t("factorModelEnableNotEligible")}</Descriptions.Item>
          </Descriptions>
          <Alert type="warning" showIcon message={t("factorModelEnableReviewEffects")} style={{ marginTop: 12 }} />
          {!reviewStillCurrent && <Alert type="error" showIcon message={t("factorModelEnableReviewChanged")} style={{ marginTop: 12 }} />}
        </>}
      </Modal>

      <ResearchRetrainModal modelId={researchRetrainId} onClose={() => setResearchRetrainId(null)} onSubmitted={task => {
        setActiveTask(task); setResearchRetrainId(null); setWizardStep("train");
        setTrainModel(true); setMaterializeScores(false);
      }} />
      <ProductionAdmissionModal modelId={productionAdmissionId} onClose={() => setProductionAdmissionId(null)} />
      {/* Task 7: 只读关联详情 Drawer */}
      <Drawer
        title={drawerModelId ? `模型「${drawerModelId}」关联详情（只读）` : "模型关联详情（只读）"}
        open={drawerOpen}
        onClose={closeRelationsDrawer}
        width={820}
        loading={drawerLoading}
        destroyOnHidden
        data-testid="relations-drawer"
      >
        {drawerReadError && <Alert type="error" showIcon message={t("factorModelLoadFailed")} description={drawerReadError}
          action={drawerModel ? <Button onClick={() => void openRelationsDrawer(drawerModel)}>{t("factorModelRetryRead")}</Button> : undefined} />}
        {drawerModel && <div className="factor-model-review-summary">
          <Alert showIcon type={drawerModelEligible ? drawerResearchComparison ? "info" : "success" : "warning"} message={researchValidationPassed(drawerModel) ? t("factorModelResearchValidatedPending") : drawerModelEligible ? drawerResearchComparison ? t("factorModelResearchFormalGatePassed") : "模型已通过启用资格检查" : "这个模型目前不能启用"} description={drawerModelEligible ? "请结合验证指标和因子版本确认本次候选；影子运行和正式启用均由你选择。" : readableModelReadiness(drawerModel, drawerModel.readiness_code === "MODEL_FACTOR_VERSION_MISMATCH" ? "冻结集合与模型权重版本不一致，请使用此集合重新训练。" : drawerModel.readiness_message || "正式运行资格未通过，请先查看完整验证结果。")} />
          <dl className="factor-model-review-facts">
            <div><dt><Tooltip title={t("factorModelValidationIcHelp")}><span>{t("factorModelValidationIc")} <QuestionCircleOutlined /></span></Tooltip></dt><dd>{typeof drawerModel.metrics?.validation_ic === "number" ? drawerModel.metrics.validation_ic.toFixed(4) : "暂无"}</dd></div>
            <div><dt><Tooltip title={t("factorModelSamplesHelp")}><span>{t("factorModelSamples")} <QuestionCircleOutlined /></span></Tooltip></dt><dd>{typeof drawerModel.sample_count === "number" ? drawerModel.sample_count.toLocaleString() : "暂无"}</dd></div>
            <div><dt>绑定集合</dt><dd>{String(drawerModel.hyperparameters?.factor_set_id || "尚未关联")}</dd></div>
            <div><dt>{t("factorModelSampleCutoffDate")}</dt><dd>{drawerModelDetail?.id === drawerModel.id && typeof drawerModelDetail.hyperparameters?.data_cutoff_date === "string" ? drawerModelDetail.hyperparameters.data_cutoff_date : t("factorModelSampleCutoffNotRecorded")}</dd></div>
            <div><dt><Tooltip title={t("factorModelInputAvailableAtHelp")}><span>{t("factorModelInputAvailableAt")} <QuestionCircleOutlined /></span></Tooltip></dt><dd>{formatDateTime(drawerModel.data_cutoff_at)}</dd></div>
          </dl>
          <FactorModelTrainingEvidence model={drawerModelDetail?.id === drawerModel.id ? drawerModelDetail : null} />
          <p>{researchValidationPassed(drawerModel) ? t("factorModelResearchIndependentRequired") : "启用模型后会自动提交评分任务，评分固定使用你确认的模型版本。影子评分用于观察；正式启用后才用于模型决策。"}</p>
          <div className="factor-model-review-actions">
            {drawerModelEligible ? <>
              <Button disabled={activationUnavailable || !!activationReview || !drawerEvidenceReady} onClick={() => { if (!drawerEvidenceReady) return; closeRelationsDrawer(); requestActivation(drawerModel, "shadow"); }} icon={<EyeOutlined />} loading={acting === `shadow:${drawerModel.id}`}>确认影子运行</Button>
              <Button disabled={activationUnavailable || !!activationReview || !drawerEvidenceReady} type="primary" onClick={() => { if (!drawerEvidenceReady) return; closeRelationsDrawer(); requestActivation(drawerModel, "ridge"); }} icon={<CheckCircleOutlined />} loading={acting === `ridge:${drawerModel.id}`}>确认正式启用</Button>
            </> : drawerTrustedResearch ? <Button onClick={() => { setResearchRetrainId(drawerModel.id); closeRelationsDrawer(); }}>{t("factorResearchRetrainTitle")}</Button> : <Button onClick={() => { closeRelationsDrawer(); focusPipeline("train"); setTrainingFactorSetId(drawerSetAvailable ? drawerTrainingSetId as string : null); }}>{drawerSetAvailable ? "使用此集合配置重新训练" : "选择冻结集合重新训练"}</Button>}
            {drawerTrustedResearch && drawerModel.feature_versions?.turnover_z20 === 3 && Object.keys(drawerModel.feature_versions).length === 1 && <Button onClick={() => {setProductionAdmissionId(drawerModel.id); closeRelationsDrawer();}}>{t("productionAdmission.title")}</Button>}
            <Button type="link" onClick={() => navigateApp(`/factors?model_id=${encodeURIComponent(drawerModel.id)}`)}>完整验证详情</Button>
          </div>
        </div>}
        {drawerData?.unbound_reason ? (
          <div className="relations-unbound-block">
            <Alert
              type="warning"
              showIcon
              data-testid="relations-unbound-alert"
              message="该历史模型未关联因子集合"
              description={drawerData.unbound_reason}
              style={{ marginBottom: 16 }}
            />
            <div style={{ textAlign: "right" }}>
              <Button
                type="primary"
                data-testid="view-migration-report"
                onClick={() => navigateApp("/factors/migration")}
              >
                查看迁移报告
              </Button>
            </div>
          </div>
        ) : (
          <Table
            rowKey={(r: any, idx) => `${r.factor_code ?? ""}-${idx}`}
            size="small"
            dataSource={drawerData?.factors ?? []}
            pagination={{ pageSize: 20, hideOnSinglePage: true }}
            locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无关联因子" /> }}
            data-testid="relations-details-table"
            columns={[
              { title: t("factorModelFactorCode"), dataIndex: "factor_code", key: "factor_code", width: 150 },
              { title: t("factorModelFactorVersionId"), dataIndex: "factor_version_id", key: "factor_version_id", width: 150,
                render: (v: unknown) => {
                  if (v == null) return <span className="na-cell" style={{ color: "#94a3b8", fontStyle: "italic" }}>N/A</span>;
                  return String(v);
                },
              },
              { title: <Tooltip title={t("factorModelCoefficientHelp")}><span>{t("factorModelCoefficient")} <QuestionCircleOutlined /></span></Tooltip>, dataIndex: "coef_raw", key: "coef_raw", width: 110, align: "right",
                render: (v: unknown) => {
                  if (v == null || Number.isNaN(Number(v))) return <span className="na-cell" data-testid="na-coef_raw" style={{ color: "#94a3b8", fontStyle: "italic" }}>N/A</span>;
                  return Number(v).toFixed(6);
                },
              },
              { title: <Tooltip title={t("factorModelNormalizedWeightHelp")}><span>{t("factorModelNormalizedWeight")} <QuestionCircleOutlined /></span></Tooltip>, dataIndex: "weight_norm", key: "weight_norm", width: 110, align: "right",
                render: (v: unknown) => {
                  if (v == null || Number.isNaN(Number(v))) return <span className="na-cell" style={{ color: "#94a3b8", fontStyle: "italic" }}>N/A</span>;
                  return Number(v).toFixed(6);
                },
              },
              { title: <Tooltip title={t("factorModelFactorValidationIcHelp")}><span>{t("factorModelFactorValidationIc")} <QuestionCircleOutlined /></span></Tooltip>, dataIndex: "validation_ic", key: "validation_ic", width: 120, align: "right",
                render: (v: unknown) => {
                  if (v == null || Number.isNaN(Number(v))) return <span className="na-cell" data-testid="na-validation_ic" style={{ color: "#94a3b8", fontStyle: "italic" }}>N/A</span>;
                  return Number(v).toFixed(4);
                },
              },
              { title: <Tooltip title={t("factorModelTrainingCoverageHelp")}><span>{t("factorModelTrainingCoverage")} <QuestionCircleOutlined /></span></Tooltip>, dataIndex: "coverage", key: "coverage", width: 100, align: "right",
                render: (v: unknown) => {
                  if (v == null || Number.isNaN(Number(v))) return <span className="na-cell" style={{ color: "#94a3b8", fontStyle: "italic" }}>N/A</span>;
                  return `${(Number(v) * 100).toFixed(1)}%`;
                },
              },
            ]}
          />
        )}
      </Drawer>
    </div>
  );
}
