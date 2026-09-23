import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Button,
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
import { navigate as navigateApp } from "../utils/navigate";
import { enumLabel, t, template } from "../i18n";
import ChainStepsBar, { type ChainState } from "./factors/ChainStepsBar";
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

// Task 8：流水线 5 阶段预览 Steps 条（静态，不做实时同步）
// 每种态 4 类：wait=浅灰圆圈 / ready=绿对勾 / running=蓝色圆环旋转 / blocked=红感叹号
type PipelineStepStatus = "wait" | "ready" | "running" | "blocked";
const STEP_STATUS_CLASS: Record<PipelineStepStatus, string> = {
  wait: "ps-state-wait bg-slate-200 text-slate-500 border border-slate-300",
  ready: "ps-state-ready bg-green-500 text-white border border-green-600",
  running: "ps-state-running bg-blue-500 text-white border border-blue-600 animate-spin",
  blocked: "ps-state-blocked bg-red-500 text-white border border-red-600",
};
const STEP_STATUS_TITLE: Record<PipelineStepStatus, string> = {
  wait: "未开始",
  ready: "就绪",
  running: "进行中",
  blocked: "阻断",
};
interface PipelineStepsBarProps {
  trainEnabled: boolean;
  stepTwoReady: boolean;
  stepTwoBlocked: boolean;
  stepFourRunning: boolean;
}
function PipelineStepsBar(props: PipelineStepsBarProps) {
  const { trainEnabled, stepTwoReady, stepTwoBlocked, stepFourRunning } = props;

  // Step 1 同步数据：⚡准备中 → 统一 ready（仓库已在上方 health 校验过，这里只表达顺序态）
  // Step 2 因子预处理：✅就绪 或 ⏸未就绪（取决于传入）
  // Step 3 目标生成：🎯未启动 → wait
  // Step 4 训练评分：🏋待启动 → 按下启动后 running；train=false 保持灰 wait
  // Step 5 回测消费：📊未启动 → wait
  const s1: PipelineStepStatus = "ready";
  const s2: PipelineStepStatus = stepTwoBlocked ? "blocked" : stepTwoReady ? "ready" : "wait";
  const s3: PipelineStepStatus = "wait";
  const s4: PipelineStepStatus = trainEnabled ? (stepFourRunning ? "running" : "wait") : "wait";
  const s5: PipelineStepStatus = "wait";
  const steps: Array<{ label: string; hint: string; status: PipelineStepStatus; emoji: string; idx: number }> = [
    { idx: 1, label: "同步数据", hint: "同步市场数据与因子快照", status: s1, emoji: "⚡" },
    { idx: 2, label: "因子预处理", hint: "对选定集合计算指标、缺失、标准化", status: s2, emoji: "🧮" },
    { idx: 3, label: "目标生成", hint: "按标签定义生成训练/验证目标", status: s3, emoji: "🎯" },
    { idx: 4, label: trainEnabled ? "训练评分" : "训练评分（跳过）", hint: "Ridge 回归训练 + 评分快照", status: s4, emoji: "🏋" },
    { idx: 5, label: "回测消费", hint: "评分写入组合权重与回测面板", status: s5, emoji: "📊" },
  ];
  return (
    <div className="pipeline-steps-bar" data-testid="pipeline-steps-bar">
      {steps.map((step, i) => (
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
          {i < steps.length - 1 && (
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
  if (id === "fs-set-3126b70") return "20日换手率 Z 分数集合";
  if (id === "legacy-system-v1") return "系统基础因子集合";
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
  const [loading, setLoading] = useState(true);
  const [acting, setActing] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [fullRefresh, setFullRefresh] = useState(false);
  const [trainModel, setTrainModel] = useState<boolean>(initialTrainModel);
  const [materializeScores, setMaterializeScores] = useState(true);
  const [windowDays, setWindowDays] = useState(250);
  const [validationDays, setValidationDays] = useState(50);
  const [fallbackOpen, setFallbackOpen] = useState(false);
  const [fallbackReason, setFallbackReason] = useState("");
  // Task 7: 关联详情只读 Drawer 状态
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [drawerModelId, setDrawerModelId] = useState<string | null>(null);
  const [drawerLoading, setDrawerLoading] = useState(false);
  const [drawerData, setDrawerData] = useState<ScoringModelRelations | null>(null);
  // 任务进度追踪：已运行秒数 + 进度停滞标志（percent 30 秒未变则标记为停滞）
  const [elapsed, setElapsed] = useState(0);
  const [stalled, setStalled] = useState(false);
  const lastProgressRef = useRef<{ signature: string; time: number } | null>(null);
  // 预估时长：基于历史已完成任务统计
  const [eta, setEta] = useState<FactorPipelineEta | null>(null);
  const [trainingFactorSetId, setTrainingFactorSetId] = useState<string | null>(null);
  // Task 8：frozen 因子集合列表 + 流水线 5 阶段预览态（Step 4 训练会在启动时切 running）
  const [frozenFactorSets, setFrozenFactorSets] = useState<FactorSet[]>([]);
  const [stepFourRunning, setStepFourRunning] = useState(false);

  const loadAll = useCallback(async (showLoading = true) => {
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
      setOverview(overviewData);
      setModels(modelData.items);
      // Task 8：显式写入 frozenFactorSets 状态（只保留 frozen，作为下拉唯一数据源）
      const usableSets = Array.isArray(frozenFactorSetsData)
        ? frozenFactorSetsData.filter((item) => !item || item.status === "frozen" || !("status" in item))
        : [];
      setFrozenFactorSets(usableSets);

      // 从因子中心进入时优先使用当前生产模型绑定的集合；否则保持手动选择策略。
      const preferredId = typeof window !== "undefined"
        ? window.localStorage.getItem("settings_pipeline_factor_set_id")
        : null;
      const preferredSet = preferredId ? usableSets.find((s) => s.id === preferredId) : null;
      if (preferredSet) {
        setTrainingFactorSetId(preferredSet.id);
        window.localStorage.removeItem("settings_pipeline_factor_set_id");
      } else if (usableSets.length === 1) {
        setTrainingFactorSetId(usableSets[0].id);
      } else {
        setTrainingFactorSetId(null);
      }
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

  // Task 8：Readiness 前端轻检查（T4 3 门），返回 ok + 失败原因数组
  const selectedFactorSet = useMemo<FactorSet | null>(() => {
    if (!trainModel || !trainingFactorSetId) return null;
    return frozenFactorSets.find((s) => s.id === trainingFactorSetId) ?? null;
  }, [trainModel, trainingFactorSetId, frozenFactorSets]);

  const readiness = useMemo<{ ok: boolean; reasons: string[] }>(() => {
    const reasons: string[] = [];
    if (!trainModel) return { ok: true, reasons }; // 不训练模式不卡
    if (!selectedFactorSet) {
      reasons.push("请先选择用于训练的因子集合。");
      return { ok: false, reasons };
    }
    const members = Number(selectedFactorSet.n_members ?? (selectedFactorSet as any).member_count ?? 0);
    if (members < 1) reasons.push("集合为空（n_members < 1）。");
    // feature_count 字段：优先 feature_count（DTO 别名）；否则回退 member_count（当所有成员都是 feature 场景
    // 视为兼容）；再回退使用 members.length。
    const fsAny = selectedFactorSet as any;
    const featureCount = Number(
      fsAny.feature_count ??
        fsAny.featureCount ??
        (Array.isArray(fsAny.members)
          ? fsAny.members.filter((m: any) => !m.role || m.role === "feature").length
          : undefined) ??
        members,
    );
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
    return { ok: reasons.length === 0, reasons };
  }, [trainModel, selectedFactorSet]);

  const startPipeline = async () => {
    if (trainModel && !readiness.ok) {
      setError(readiness.reasons.join("；"));
      return;
    }
    setActing("pipeline");
    setError(null);
    // Task 8：按下启动按钮，Steps 条的 Step 4 训练 立即切到 running 态
    setStepFourRunning(trainModel);
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

  // Task 7: 打开关联详情只读 Drawer
  const openRelationsDrawer = async (model: FactorModelRun) => {
    setDrawerModelId(model.id);
    setDrawerOpen(true);
    setDrawerLoading(true);
    setDrawerData(null);
    try {
      // Factor-domain internal - DO NOT USE outside factor center
      const data = await api.scoringGetModelRelations(model.id);
      setDrawerData(data);
    } catch (err: any) {
      setDrawerData({
        model_id: model.id,
        status: model.status,
        factor_set_id: null,
        factor_set_name: null,
        n_members: null,
        factors: [],
        data_cutoff_at: null,
        created_at: null,
        unbound_reason: err?.message || "加载关联详情失败，请稍后重试。",
      });
    } finally {
      setDrawerLoading(false);
    }
  };

  const runtime = overview?.runtime;
  const taskRunning = !!activeTask && !TERMINAL_TASK_STATES.has(activeTask.status);
  const featureEnabled = overview?.feature_enabled ?? overview?.config?.feature_enabled ?? false;
  const warehouseAvailable = overview?.health.warehouse_available ?? false;
  const invalidWindows = validationDays >= windowDays;

  // ─── Task 3：ChainStepsBar 状态派生（设置页只读视角）─────────────
  const chainState: ChainState = React.useMemo<ChainState>(() => {
    const factorWarehouseReady = featureEnabled && warehouseAvailable;
    // Task8 只加载了 frozen 集合 → 此处用 frozenFactorSets 作为「存在 frozen」判断；
    // 「集合存在/成员就绪」按宽松处理：frozenFactorSets 至少 1 条 → 集合存在，成员已就绪
    const anyFactorSetCreated = frozenFactorSets.length > 0;
    const anyFactorSetMembersReady = frozenFactorSets.some(
      (fs: any) => {
        const ms = (fs as any).members ?? [];
        const members = Array.isArray(ms) ? ms.length : Number((fs as any).member_count ?? 0) || 0;
        const features = ms.filter?.((m: any) => m.role === "feature").length ??
          (Number((fs as any).feature_count ?? 0) || 0);
        return members > 0 && features > 0;
      }
    );
    const anyFactorSetFrozen = frozenFactorSets.length > 0;
    const anyModelTrained = models.some((m) => {
      const s = String(m.status ?? "").toLowerCase();
      return ["validated", "trained", "rejected", "done", "completed"].includes(s);
    });
    const anyModelValidated = models.some((m) => String(m.status ?? "").toLowerCase() === "validated");
    const decisionModeEqualsFormalActive =
      ((runtime?.weight_mode === "ridge") || (runtime as any)?.decision_mode === "formal") &&
      !!runtime?.active_model_run_id;
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
  }, [featureEnabled, warehouseAvailable, frozenFactorSets, models, runtime]);

  // 设置页：Step 2/3/4/5 的直达按钮直接跳回「因子中心 Tab」（按任务 7 规范设置页不编辑集合）
  const handleChainGoStep = React.useCallback((stepIdx: number) => {
    if (stepIdx === 8) {
      navigateApp("/settings/pipeline");
      return;
    }
    // 其他 Step 不在设置页提供写入口 → 直接跳因子中心
    navigateApp("/factors?next=factor-center");
  }, []);

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
      title: t("factorModelFactorSet"),
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
    // ── Task 7 只读关联列追加（不删/不改既有列） ──
    {
      title: "关联因子集合",
      key: "linked_factor_set",
      width: 260,
      render: (_: unknown, record: FactorModelRun) => {
        const hpAny = record.hyperparameters as any;
        // 优先从 relations 返回拿；未加载时退回到 hyperparameters
        const name: string | null | undefined =
          (drawerData && drawerData.model_id === record.id ? drawerData.factor_set_name : null) ??
          hpAny?.factor_set_name ??
          hpAny?.factorset_label ??
          null;
        const id: string | null | undefined =
          (drawerData && drawerData.model_id === record.id ? drawerData.factor_set_id : null) ??
          hpAny?.factor_set_id ??
          hpAny?.factorset_id ??
          null;
        const nRaw: number | string | null | undefined =
          (drawerData && drawerData.model_id === record.id ? drawerData.n_members : null) ??
          hpAny?.n_members ??
          hpAny?.factorset_member_count ??
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

      <Alert
        type="info"
        showIcon
        message={t("factorModelPipelineGuideTitle")}
        description={t("factorModelPipelineGuideDescription")}
        style={{ marginBottom: 16 }}
      />

      {/* Task 3：ChainStepsBar 链式步骤导航（只读视图，缺前置直接跳因子中心） */}
      <div style={{ marginBottom: 16 }}>
        <ChainStepsBar state={chainState} onGoStep={handleChainGoStep} />
      </div>

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
            <Tooltip
              title={
                taskRunning
                  ? "当前有流水线任务正在运行，启动按钮暂不可用。"
                  : !featureEnabled
                  ? "请先启用因子功能开关后再运行流水线。"
                  : !warehouseAvailable
                  ? "请先完成因子仓库初始化（见上方告警）。"
                  : invalidWindows
                  ? "训练窗口 ≤ 验证窗口，请调整天数后重试。"
                  : trainModel && !readiness.ok
                  ? readiness.reasons.map((r, idx) => `${idx + 1}. ${r}`).join(" ")
                  : undefined
              }
            >
              <Button
                type="primary"
                icon={<PlayCircleOutlined />}
                disabled={
                  taskRunning ||
                  !featureEnabled ||
                  !warehouseAvailable ||
                  invalidWindows ||
                  (trainModel && !readiness.ok)
                }
                loading={acting === "pipeline"}
                onClick={startPipeline}
                className={trainModel ? "pipeline-run-train" : "pipeline-run-no-train"}
              >
                {trainModel
                  ? "启动流水线（训练模式）"
                  : "启动流水线（不训练模式）"}
              </Button>
            </Tooltip>
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
          <label>
            <Tooltip title={t("factorModelTrainModelTip")}>
              <span style={{ cursor: "help", borderBottom: "1px dashed var(--pt-muted-foreground)" }}>
                {t("factorModelTrainModel")} <QuestionCircleOutlined style={{ marginLeft: 4, color: "#94a3b8" }} />
              </span>
            </Tooltip>
            <Switch checked={trainModel} onChange={setTrainModel} />
          </label>
          <label><span>{t("factorModelMaterialize")}</span><Switch checked={materializeScores} onChange={setMaterializeScores} /></label>
          <label><span>{t("factorModelWindow")} ({t("factorModelDays")})</span><InputNumber min={60} max={1000} value={windowDays} onChange={(value) => setWindowDays(Number(value ?? 250))} /></label>
          <label><span>{t("factorModelValidation")} ({t("factorModelDays")})</span><InputNumber min={20} max={250} value={validationDays} onChange={(value) => setValidationDays(Number(value ?? 50))} /></label>
        </div>

        {/* Task 8：train=true / train=false 两块新增 UI（仅追加，不删除上面既有 controls） */}
        {trainModel ? (
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

            {/* 5 阶段预览 Steps 条（静态图形，不做实时同步） */}
            <PipelineStepsBar
              trainEnabled={trainModel}
              stepTwoReady={!selectedFactorSet
                ? false
                : (Number(selectedFactorSet.n_members ?? (selectedFactorSet as any).member_count ?? 0) >= 1 &&
                   readiness.ok)}
              stepTwoBlocked={
                !!selectedFactorSet &&
                (Number(selectedFactorSet.n_members ?? (selectedFactorSet as any).member_count ?? 0) < 1 ||
                  !readiness.ok)
              }
              stepFourRunning={stepFourRunning}
            />
          </div>
        ) : (
          <div className="mt-3" data-testid="no-train-mode-block">
            <Alert
              type="info"
              showIcon
              className="pipeline-no-train-banner"
              data-testid="pipeline-no-train-banner"
              message="当前配置 train=false"
              description="当前配置 train=false，本次流水线运行将不会创建新因子模型，直接按手选权重进入评分→回测消费阶段。"
            />
          </div>
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

      {/* Task 7: 跳转因子中心按钮 — ChainStepsBar（流水线区）下方 / 模型表上方 */}
      <div className="factor-goto-center-row mt-3 flex items-center justify-end" style={{ marginBottom: 8 }}>
        <Button
          type="default"
          className="goto-factor-center-link"
          data-testid="goto-factor-center-global"
          onClick={() => navigateApp("/factors")}
        >
          ↗ 前往因子中心
        </Button>
      </div>

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

      {/* Task 7: 只读关联详情 Drawer */}
      <Drawer
        title={drawerModelId ? `模型「${drawerModelId}」关联详情（只读）` : "模型关联详情（只读）"}
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        width={820}
        loading={drawerLoading}
        destroyOnHidden
        data-testid="relations-drawer"
      >
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
              { title: "factor_code", dataIndex: "factor_code", key: "factor_code", width: 150 },
              { title: "factor_version_id", dataIndex: "factor_version_id", key: "factor_version_id", width: 150,
                render: (v: unknown) => {
                  if (v == null) return <span className="na-cell" style={{ color: "#94a3b8", fontStyle: "italic" }}>N/A</span>;
                  return String(v);
                },
              },
              { title: "coef_raw", dataIndex: "coef_raw", key: "coef_raw", width: 110, align: "right",
                render: (v: unknown) => {
                  if (v == null || Number.isNaN(Number(v))) return <span className="na-cell" data-testid="na-coef_raw" style={{ color: "#94a3b8", fontStyle: "italic" }}>N/A</span>;
                  return Number(v).toFixed(6);
                },
              },
              { title: "weight_norm", dataIndex: "weight_norm", key: "weight_norm", width: 110, align: "right",
                render: (v: unknown) => {
                  if (v == null || Number.isNaN(Number(v))) return <span className="na-cell" style={{ color: "#94a3b8", fontStyle: "italic" }}>N/A</span>;
                  return Number(v).toFixed(6);
                },
              },
              { title: "validation_ic", dataIndex: "validation_ic", key: "validation_ic", width: 120, align: "right",
                render: (v: unknown) => {
                  if (v == null || Number.isNaN(Number(v))) return <span className="na-cell" data-testid="na-validation_ic" style={{ color: "#94a3b8", fontStyle: "italic" }}>N/A</span>;
                  return Number(v).toFixed(4);
                },
              },
              { title: "coverage", dataIndex: "coverage", key: "coverage", width: 100, align: "right",
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
