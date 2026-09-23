import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  App,
  Badge,
  Button,
  Card,
  Col,
  DatePicker,
  Descriptions,
  Divider,
  Drawer,
  Empty,
  InputNumber,
  List,
  Modal,
  Progress,
  Row,
  Select,
  Skeleton,
  Space,
  Spin,
  Statistic,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import {
  ExclamationCircleOutlined,
  ReloadOutlined,
  StopOutlined,
  ThunderboltOutlined,
  WarningOutlined,
  ArrowRightOutlined,
  InfoCircleOutlined,
  CheckCircleOutlined,
  ClockCircleOutlined,
  SettingOutlined,
  DownOutlined,
  UpOutlined,
  CloseCircleOutlined,
  CheckCircleFilled,
  ExclamationCircleFilled,
  CloseCircleFilled,
  InfoCircleFilled,
  CopyOutlined,
  BugOutlined,
  SolutionOutlined,
} from "@ant-design/icons";
import dayjs, { type Dayjs } from "dayjs";
import { useApp } from "../../context/AppContext";
import { t, template, factorLabel } from "../../i18n";
import "./FactorEvaluationLab.css";
import {
  api,
  type EvaluationRunRead,
  type EvaluationMetrics,
  type EvaluationTaskCreatePayload,
  type EvaluationTaskRead,
  type EvaluationTaskHeartbeat,
  type ExcludedSymbolDays,
  type ParameterPerturbationResult,
  type PreflightResponse,
  type StressTestSummary,
} from "../../api/client";
import SafeMetric from "../common/SafeMetric";

// `warn` is a completed evaluation with a non-blocking gate result, not a
// running task. Treating it as active makes the UI keep polling and counting
// elapsed time indefinitely after the backend has reached 100%.
const TERMINAL_TASK_STATES = new Set(["done", "completed", "warn", "failed", "cancelled"]);
const POLL_INTERVAL_MS = 2000;
const STALL_THRESHOLD_SECONDS = 30;

/** 终态门禁颜色映射。 */
function gateColor(gate: string | null | undefined): string {
  if (!gate) return "default";
  const map: Record<string, string> = {
    passed: "green",
    rejected: "red",
    warn: "orange",
  };
  return map[gate] || "default";
}

function gateLabel(gate: string | null | undefined): string {
  if (!gate) return t("evalLabGatePending");
  const key = `evalLabGate_${gate}`;
  const translated = t(key);
  return translated === key ? gate : translated;
}

function taskStatusLabel(status: string): string {
  const map: Record<string, string> = {
    running: "evalLabTaskRunning",
    queued: "evalLabTaskQueued",
    done: "evalLabTaskDone",
    completed: "evalLabTaskDone",
    warn: "evalLabTaskWarn",
    failed: "evalLabTaskFailed",
    cancelled: "evalLabTaskCancelled",
  };
  const key = map[status] || "evalLabTaskUnknown";
  const translated = t(key);
  return translated === key ? status : translated;
}

function taskStatusColor(status: string): string {
  const map: Record<string, string> = {
    running: "processing",
    queued: "default",
    done: "success",
    completed: "success",
    warn: "warning",
    failed: "error",
    cancelled: "default",
  };
  return map[status] || "default";
}

function stageLabel(stage: string | null | undefined): string {
  if (!stage) return "-";
  const key = `evalLabStage_${stage}`;
  const translated = t(key);
  return translated === key ? stage : translated;
}

function errorCategoryLabel(category: unknown): string {
  const value = typeof category === "string" ? category : "";
  const labels: Record<string, string> = {
    formula: "公式",
    data: "数据",
    config: "配置",
    sample: "样本",
    pit: "时点风险",
    target: "目标标签",
    stability: "稳定性",
    gate: "门禁",
  };
  return labels[value] || value || "未分类";
}

const METRIC_HINTS: Record<string, string> = {
  rankIc: "排名信息系数：衡量因子排序与未来收益排序的相关性，越接近 1 越好，负值表示方向相反。",
  rankIcMedian: "排名 IC 的中位数，用来观察典型交易日表现，减少极端日期影响。",
  rankIcStd: "排名 IC 标准差，越小表示不同交易日的表现越稳定。",
  icir: "ICIR = IC 均值 / IC 标准差，衡量单位波动对应的预测能力，越高越稳定。通常 0.15 以上才具备较强参考价值。",
  positiveRatio: "正 IC 占比：未来收益方向判断正确的交易日比例。",
  coverage: "覆盖率：有有效因子值并参与计算的样本占比。",
  samples: "样本数：实际参与评价的股票-交易日样本数量。",
  monotonicity: "分组单调性：各分组收益是否随因子排序稳定变化，越接近 1 越好。",
  longShort: "多空收益：高分组与低分组之间的收益差。",
  turnover: "换手率：策略调仓频率，越高通常意味着交易成本越高。",
  costAdjusted: "成本后收益：扣除手续费和滑点后的收益。",
};

function MetricLabel({ label, hint }: { label: string; hint: keyof typeof METRIC_HINTS }) {
  return (
    <Space size={4}>
      <span>{label}</span>
      <Tooltip title={METRIC_HINTS[hint]}>
        <InfoCircleOutlined style={{ color: "#8c8c8c", cursor: "help" }} />
      </Tooltip>
    </Space>
  );
}

/** 将历史任务中的内部异常标识转换为用户可读的提示。 */
function taskMessageLabel(message: string | null | undefined): string {
  if (!message) return "-";
  if (/^evaluation_failed:/i.test(message)) return t("evalLabErrorEvaluationFailed");
  if (/^factor_not_found:/i.test(message)) return t("evalLabErrorFactorNotFound");
  if (/^no_factor_version$/i.test(message)) return t("evalLabErrorNoFactorVersion");
  if (/^no_factor_data:/i.test(message)) return t("evalLabErrorNoFactorData");
  if (/^missing factor_code$/i.test(message)) return t("evalLabErrorFactorRequired");
  return message;
}

// ══════════════════════════════════════════════════════════════════════════════
// 失败维度（blocker）的结构化解析 + 渲染
// ══════════════════════════════════════════════════════════════════════════════
type BlockerSeverity = "error" | "warning" | string;
type BlockerCategory =
  | "data"
  | "formula"
  | "config"
  | "version"
  | "universe"
  | string;
interface BlockerFixLink {
  tab?: string;
  subtab?: string;
  route?: string;
  label_zh?: string;
  label_en?: string;
  label?: string;
  params?: Record<string, unknown>;
}
interface EvalBlocker {
  code?: string;
  severity?: BlockerSeverity;
  category?: BlockerCategory;
  title_zh?: string;
  title_en?: string;
  title?: string;
  detail_zh?: string;
  detail_en?: string;
  detail?: string;
  fix_link?: BlockerFixLink | null;
  [k: string]: unknown;
}

function severityTagColor(severity: BlockerSeverity | undefined): string {
  switch (severity) {
    case "error":
      return "red";
    case "warning":
      return "orange";
    case "info":
      return "blue";
    default:
      return "default";
  }
}
function severityIcon(severity: BlockerSeverity | undefined) {
  return severity === "warning" ? <WarningOutlined /> : <ExclamationCircleOutlined />;
}
function categoryLabel(cat: BlockerCategory | undefined): string {
  switch (cat) {
    case "data":
      return t("evalLabBlockerCatData");
    case "formula":
      return t("evalLabBlockerCatFormula");
    case "version":
      return t("evalLabBlockerCatVersion");
    case "config":
      return t("evalLabBlockerCatConfig");
    case "universe":
      return t("evalLabBlockerCatUniverse");
    default:
      return cat || "-";
  }
}
function blockerTitle(b: EvalBlocker): string {
  const isZh = true; // 后端目前只写中文，i18n 用下面的 fallback
  void isZh;
  return (
    b.title_zh ||
    b.title_en ||
    b.title ||
    (typeof b.message === "string" ? b.message : "") ||
    (typeof b.msg === "string" ? b.msg : "") ||
    (b.code ? String(b.code) : t("evalLabBlockerUnknown"))
  );
}
function blockerDetail(b: EvalBlocker): string {
  return (
    b.detail_zh ||
    b.detail_en ||
    b.detail ||
    (typeof b.description === "string" ? b.description : "") ||
    ""
  );
}
function blockerFixLabel(b: EvalBlocker): string | null {
  const link = b.fix_link;
  if (!link) return null;
  return link.label_zh || link.label_en || link.label || null;
}

/**
 * 从后端 errors 里尽力解析出 blockers 列表。
 * 兼容三种形态：
 *  ① 新后端直接写 [{code,severity,category,title_zh,...}] — 直接用
 *  ② 旧的 compile_errors 风格 [{message, type, detail}] — 包一层
 *  ③ 其他杂项 — 兜底成一条
 */
function parseBlockers(errors: Array<Record<string, unknown>> | undefined | null): EvalBlocker[] {
  if (!Array.isArray(errors) || errors.length === 0) return [];
  const first = errors[0];
  const looksNewShape =
    "severity" in first || "category" in first || "title_zh" in first || "fix_link" in first;
  if (looksNewShape) {
    return errors as EvalBlocker[];
  }
  // 旧形态：每项像 compile error，包成 blocker
  return errors.map((e, i) => {
    const msg =
      (typeof e.message === "string" ? e.message : "") ||
      (typeof e.msg === "string" ? e.msg : "") ||
      t("evalLabBlockerUnknown");
    const type = typeof e.type === "string" ? e.type : undefined;
    const detail = typeof e.detail === "string"
      ? e.detail
      : e.detail && typeof e.detail === "object"
        ? JSON.stringify(e.detail)
        : "";
    return {
      code: type ? `legacy.${type}_${i}` : `legacy.${i}`,
      severity: "error",
      category: type?.startsWith("factor_") || type?.startsWith("formula") ? "formula" : "config",
      title_zh: msg,
      detail_zh: detail || undefined,
      fix_link: { tab: "factors", subtab: "editor", label_zh: t("evalLabBlockerFixToEditor") },
    };
  });
}

function rejectionReasonLabel(reason: unknown): string {
  // 新版后端返回结构化 blocker，旧任务仍可能只返回 "code:detail" 字符串。
  if (reason && typeof reason === "object" && !Array.isArray(reason)) {
    const item = reason as Record<string, unknown>;
    const code = typeof item.code === "string" ? item.code : "";
    const detailValue = item.detail_zh ?? item.detail ?? item.message ?? item.title_zh ?? item.title;
    const detail = typeof detailValue === "string" ? detailValue : "";
    if (!code) return detail || JSON.stringify(reason);
    const key = `evalLabReason_${code}`;
    const translated = t(key);
    const label = translated === key ? code : translated;
    return detail ? `${label}：${detail}` : label;
  }
  if (typeof reason !== "string") return reason == null ? "-" : String(reason);
  const [code, ...detail] = reason.split(":");
  const key = `evalLabReason_${code}`;
  const translated = t(key);
  if (translated === key) return reason;
  return detail.length > 0 ? `${translated}：${detail.join(":")}` : translated;
}

function evidenceKeyLabel(key: string): string {
  const labels: Record<string, string> = {
    correlation_id: "追踪号",
    failure_reason: "失败原因",
    icir: "ICIR",
    threshold: "阈值",
    coverage: "覆盖率",
  };
  return labels[key] || key.replace(/_/g, " ");
}

function stressFailureReasonLabel(reason: string): string {
  const [code, ...detail] = reason.split(":");
  if (code === "time_segment_unstable") return t("evalLabStressReasonTimeUnstable");
  if (code === "missing_sensitive") return t("evalLabStressReasonMissingSensitive");
  const paramMatch = code.match(/^param_(.+)_(cliff_drop|unstable)$/);
  if (paramMatch) {
    const key = paramMatch[2] === "cliff_drop"
      ? "evalLabStressReasonParamCliffDrop"
      : "evalLabStressReasonParamUnstable";
    return `${paramMatch[1]}：${t(key)}`;
  }
  return detail.length > 0 ? detail.join(":") : reason;
}

function factorKindLabel(kind: unknown): string {
  if (typeof kind !== "string" || !kind) return "-";
  const key = `factorKind_${kind}`;
  const translated = t(key);
  return translated === key ? kind : translated;
}

function targetCodeLabel(code: string | null | undefined): string {
  if (!code) return "-";
  return code === "target_5d_return" ? t("evalLabTarget5dReturn") : code;
}

function parseServerDateTime(value: string | null | undefined): Date | null {
  if (!value) return null;
  const hasTimeZone = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(value);
  const parsed = new Date(hasTimeZone ? value : `${value}Z`);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

function formatDateTime(value: string | null | undefined): string {
  const parsed = parseServerDateTime(value);
  if (!parsed) return "-";
  const pad = (item: number) => String(item).padStart(2, "0");
  return [
    `${parsed.getFullYear()}-${pad(parsed.getMonth() + 1)}-${pad(parsed.getDate())}`,
    `${pad(parsed.getHours())}:${pad(parsed.getMinutes())}:${pad(parsed.getSeconds())}`,
  ].join(" ");
}

function shortId(value: string | null | undefined): string {
  if (!value) return "-";
  return value.length > 22 ? `${value.slice(0, 12)}...${value.slice(-6)}` : value;
}

function formatNumber(value: unknown, digits = 4): string {
  if (value == null || value === "") return "-";
  const num = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(num)) return "-";
  if (Math.abs(num) >= 1000) return num.toFixed(digits);
  return num.toFixed(digits);
}

function formatPercent(value: unknown, digits = 2): string {
  if (value == null || value === "") return "-";
  const num = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(num)) return "-";
  return `${(num * 100).toFixed(digits)}%`;
}

function finiteNumberOrUndefined(value: unknown): number | undefined {
  if (value == null || value === "") return undefined;
  const num = typeof value === "number" ? value : Number(value);
  return Number.isFinite(num) ? num : undefined;
}

/**
 * 兼容评估 API 的嵌套指标和旧版扁平指标。
 * 后端把指标按领域分组保存（ic/quantile/turnover 等），而报告组件
 * 使用的是历史扁平字段；统一在边界归一化，避免报告与门禁证据不一致。
 */
function normalizeEvaluationMetrics(
  raw: EvaluationRunRead["metrics"] | Record<string, unknown> | null | undefined,
): EvaluationMetrics {
  if (!raw || typeof raw !== "object") return {};
  const source = raw as Record<string, unknown>;
  if (Object.keys(source).length === 0) return {};
  const ic = source.ic && typeof source.ic === "object" ? source.ic as Record<string, unknown> : {};
  const quantile = source.quantile && typeof source.quantile === "object"
    ? source.quantile as Record<string, unknown>
    : {};
  const turnover = source.turnover && typeof source.turnover === "object"
    ? source.turnover as Record<string, unknown>
    : {};
  const costAdjusted = source.cost_adjusted && typeof source.cost_adjusted === "object"
    ? source.cost_adjusted as Record<string, unknown>
    : {};
  const coverage = source.coverage && typeof source.coverage === "object"
    ? source.coverage as Record<string, unknown>
    : {};
  const timeSplit = source.time_split && typeof source.time_split === "object"
    ? source.time_split as Record<string, unknown>
    : {};
  const first = (...values: unknown[]) => values.find((value) => value != null && value !== "");
  const groupReturns = first(quantile.group_returns, source.quantile_returns);

  return {
    ...source,
    rank_ic_mean: finiteNumberOrUndefined(first(ic.rank_ic_mean, source.rank_ic_mean)),
    rank_ic_median: finiteNumberOrUndefined(first(ic.rank_ic_median, source.rank_ic_median)),
    rank_ic_std: finiteNumberOrUndefined(first(ic.rank_ic_std, source.rank_ic_std)),
    icir: finiteNumberOrUndefined(first(ic.icir, source.icir)),
    positive_ic_ratio: finiteNumberOrUndefined(first(ic.positive_ic_ratio, source.positive_ic_ratio)),
    quantile_returns: Array.isArray(groupReturns)
      ? groupReturns.filter((value): value is number => finiteNumberOrUndefined(value) != null)
          .map((value) => finiteNumberOrUndefined(value) as number)
      : [],
    monotonicity_score: finiteNumberOrUndefined(first(quantile.monotonicity_score, source.monotonicity_score)),
    long_short_return: finiteNumberOrUndefined(
      first(quantile.top_bottom_return, source.long_short_return),
    ),
    turnover: finiteNumberOrUndefined(first(turnover.avg_turnover, source.turnover)),
    cost_adjusted_return: finiteNumberOrUndefined(
      first(costAdjusted.net_return, source.cost_adjusted_return),
    ),
    coverage: finiteNumberOrUndefined(first(coverage.coverage, source.coverage)),
    n_samples: finiteNumberOrUndefined(first(source.effective_samples, source.n_samples)),
    train_start: String(first(timeSplit.train_start, source.train_start) ?? ""),
    train_end: String(first(timeSplit.train_end, source.train_end) ?? ""),
    validation_start: String(first(timeSplit.validation_start, source.validation_start) ?? ""),
    validation_end: String(first(timeSplit.validation_end, source.validation_end) ?? ""),
  };
}

function verdictLabel(verdict: string | null | undefined): string {
  if (!verdict) return "-";
  if (verdict === "stable") return t("evalLabStressVerdictStable");
  if (verdict === "unstable") return t("evalLabStressVerdictUnstable");
  if (verdict === "cliff_drop") return t("evalLabStressVerdictCliffDrop");
  if (verdict === "robust") return t("evalLabStressVerdictRobust");
  if (verdict === "sensitive") return t("evalLabStressVerdictSensitive");
  return verdict;
}

function verdictColor(verdict: string | null | undefined): string {
  if (!verdict) return "default";
  if (verdict === "stable") return "green";
  if (verdict === "robust") return "green";
  if (verdict === "unstable") return "orange";
  if (verdict === "sensitive") return "red";
  if (verdict === "cliff_drop") return "red";
  return "default";
}

interface EvalLabState {
  tasks: EvaluationTaskRead[];
  runs: EvaluationRunRead[];
  activeTask: EvaluationTaskRead | null;
  selectedRun: EvaluationRunRead | null;
  gateFilter: string;
  loadingTasks: boolean;
  loadingRuns: boolean;
  submitting: boolean;
  cancelling: boolean;
  error: string | null;
}

const initialState: EvalLabState = {
  tasks: [],
  runs: [],
  activeTask: null,
  selectedRun: null,
  gateFilter: "",
  loadingTasks: false,
  loadingRuns: false,
  submitting: false,
  cancelling: false,
  error: null,
};

export default function FactorEvaluationLab() {
  const ctx = useApp();
  const { message } = App.useApp();
  const { setActiveTab, setActiveSubTab } = ctx;

  const [state, setState] = useState<EvalLabState>(initialState);
  const lastProgressRef = useRef<{ signature: string; time: number } | null>(null);
  const [stalled, setStalled] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [factorOptions, setFactorOptions] = useState<{ value: string; label: string }[]>([]);
  const [factorLoading, setFactorLoading] = useState(true);
  const [factorLoadError, setFactorLoadError] = useState<string | null>(null);

  // 加载因子列表用于下拉选择
  useEffect(() => {
    let cancelled = false;
    setFactorLoading(true);
    setFactorLoadError(null);
    // Factor-domain internal - DO NOT USE outside factor center
    api.scoringListFactorDefinitions({ page_size: 100 }).then((res) => {
      if (cancelled) return;
      const options = (res.items || []).map((f: { code: string; name?: string | null }) => ({
        value: f.code,
        label: `${factorLabel(f.code, f.name ?? undefined)} (${f.code})`,
      }));
      setFactorOptions(options);
    }).catch((err) => {
      if (cancelled) return;
      setFactorLoadError(err?.message || "加载因子列表失败");
    }).finally(() => {
      if (!cancelled) setFactorLoading(false);
    });
    return () => { cancelled = true; };
  }, []);

  // 表单字段
  const [factorCode, setFactorCode] = useState("");
  const [factorVersionId, setFactorVersionId] = useState<string | undefined>(undefined);
  const [factorKind, setFactorKind] = useState<"continuous" | "event" | "regime">("continuous");
  const [targetHorizon, setTargetHorizon] = useState(5);
  const [nGroups, setNGroups] = useState(5);
  const [costRate, setCostRate] = useState(0.001);
  const [dateRange, setDateRange] = useState<[Dayjs | null, Dayjs | null] | null>(null);
  const [direction, setDirection] = useState<"higher_better" | "lower_better" | "nonlinear">("higher_better");
  const [preflightResult, setPreflightResult] = useState<PreflightResponse | null>(null);
  const [preflightLoading, setPreflightLoading] = useState(false);
  const [dataCutoffDate, setDataCutoffDate] = useState<string | null>(null);
  const [executionHash, setExecutionHash] = useState<string | null>(null);

  // 新布局状态
  const [advancedDrawerOpen, setAdvancedDrawerOpen] = useState(false);
  const [activeResultTab, setActiveResultTab] = useState("overview");
  const [universe, setUniverse] = useState<string>("ashare_all");

  // ============================================================
  // 【新增】任务/运行详情 弹窗 Drawer（不再依赖左侧页面 Tab 切换，点击直接打开）
  // ============================================================
  type DetailDrawerState = {
    open: boolean;
    loading: boolean;
    taskId: string | null;
    runId: string | null;
    task: EvaluationTaskRead | null;
    run: EvaluationRunRead | null;
    /** 默认 Tab：失败/取消任务默认打开 "errors"（错误与门禁） */
    activeTab: "overview" | "stability" | "diagnostics" | "errors";
  };
  const [detailDrawer, setDetailDrawer] = useState<DetailDrawerState>({
    open: false,
    loading: false,
    taskId: null,
    runId: null,
    task: null,
    run: null,
    activeTab: "overview",
  });

  /** 错误项结构（与后端 errors_json / preflight items 一致） */
  type EvalErrorItem = {
    code?: string | null;
    severity?: "error" | "warning" | "info" | "pass" | string | null;
    category?: string | null;
    title_zh?: string | null;
    title?: string | null;
    detail_zh?: string | null;
    detail?: string | null;
    retryable?: boolean | null;
    evidence?: Record<string, unknown> | null;
    fix_link?: { tab?: string; subtab?: string; label_zh?: string } | null;
  };

  const severityIcon = (s: EvalErrorItem["severity"]) => {
    if (s === "error") return <CloseCircleFilled style={{ color: "#ff4d4f" }} />;
    if (s === "warning") return <ExclamationCircleFilled style={{ color: "#faad14" }} />;
    if (s === "pass") return <CheckCircleFilled style={{ color: "#52c41a" }} />;
    return <InfoCircleFilled style={{ color: "#1677ff" }} />;
  };
  const severityTagColor = (s: EvalErrorItem["severity"]) =>
    s === "error" ? "red" : s === "warning" ? "orange" : s === "pass" ? "green" : "blue";

  /** 从 errors_json（list/dict/string）解析出错误列表 */
  const parseErrors = (raw: unknown): EvalErrorItem[] => {
    if (!raw) return [];
    if (Array.isArray(raw)) return raw as EvalErrorItem[];
    if (typeof raw === "string") {
      try {
        const p = JSON.parse(raw);
        return Array.isArray(p) ? p : [p as EvalErrorItem];
      } catch {
        // 纯文本兜底：当成一条 generic error
        return [{
          code: "eval.client.generic_message",
          severity: "error",
          title_zh: "原始错误消息",
          detail_zh: raw.slice(0, 800),
        }];
      }
    }
    if (typeof raw === "object") return [raw as EvalErrorItem];
    return [];
  };

  /** 从任务里提取 correlation_id（从 errors[0].evidence.correlation_id） */
  const getCorrelationId = (taskOrRun: EvaluationTaskRead | EvaluationRunRead | null): string | null => {
    if (!taskOrRun) return null;
    const errs = parseErrors((taskOrRun as unknown as Record<string, unknown>)?.errors as unknown);
    const rejections = parseErrors((taskOrRun as unknown as Record<string, unknown>)?.rejection_reasons as unknown);
    for (const e of [...errs, ...rejections]) {
      const ev = e.evidence;
      if (ev && typeof ev === "object" && typeof (ev as Record<string, unknown>).correlation_id === "string") {
        return (ev as Record<string, unknown>).correlation_id as string;
      }
    }
    return null;
  };

  /** 取第一个 error（用于 status 列旁的摘要显示） */
  const getFirstError = (task: EvaluationTaskRead | null): EvalErrorItem | null => {
    if (!task) return null;
    const errs = parseErrors((task as unknown as Record<string, unknown>)?.errors as unknown);
    // 优先 severity=error
    const err = errs.find((e) => e.severity === "error") || errs[0] || null;
    if (err) return err;
    const localizedMessage = taskMessageLabel(task.message);
    if (task.status === "failed" && task.message && localizedMessage !== task.message) {
      return {
        code: task.error_code || "eval.task.legacy_failure",
        severity: "error",
        title_zh: localizedMessage,
        detail_zh: localizedMessage,
        evidence: {},
      };
    }
    // 如果没有 errors，但 MESSAGE 里暗示 Worker crashed EMPTY，则兜底生成一条
    const msg = task.message || "";
    const emptyRes =
      !task.result && !(task as unknown as Record<string, unknown>).result_json;
    if ((task.status === "failed") && (/crashed/i.test(msg) || /unexpectedly/i.test(msg) || (emptyRes && !errs.length))) {
      return {
        code: "eval.worker.crashed_no_diagnostics",
        severity: "error",
        title_zh: "Worker 线程异常退出，无诊断信息",
        detail_zh:
          "Worker 在执行过程中进程级崩溃（通常因：MySQL 连接池耗尽 / 未捕获 OOM / 嵌套异常连 errors_json 都无法写入）。请查看后端日志 stack trace，并联系开发；已为本次错误生成兜底 correlation_id。",
        evidence: { reason: task.message || "EMPTY_result_json_AND_errors_json" },
      };
    }
    return null;
  };

  /** 根据错误对象，渲染 Tooltip 长文本（给 status 列 hover 用） */
  const errorTooltip = (e: EvalErrorItem | null): string => {
    if (!e) return "";
    const parts: string[] = [];
    if (e.code) parts.push("错误编号：" + e.code);
    if (e.title_zh || e.title) parts.push("标题：" + (e.title_zh || e.title));
    if (e.detail_zh || e.detail) parts.push("详情：" + ((e.detail_zh || e.detail) as string).slice(0, 400));
    const corr = (e.evidence as Record<string, unknown> | undefined)?.correlation_id;
    if (typeof corr === "string") parts.push("追踪号：" + corr);
    return parts.join("  ·  ");
  };

  /** 打开详情 Drawer（任务优先；没有 runId 也强制打开，默认打开错误 Tab） */
  const handleOpenDetailDrawer = useCallback(async (payload: { taskId?: string | null; runId?: string | null }) => {
    const { taskId = null, runId = null } = payload;
    if (!taskId && !runId) return;
    // 先确定默认 Tab：有任务且失败/取消 → 默认 errors；否则 overview
    let activeTab: DetailDrawerState["activeTab"] = "overview";
    let initialTask: EvaluationTaskRead | null = null;
    if (taskId) {
      initialTask = state.tasks.find((t) => t.id === taskId) || null;
      if (initialTask && ["failed", "cancelled"].includes(initialTask.status)) {
        activeTab = "errors";
      }
    }
    // 从 task.result.run_id 找已有 run（缓存命中则不用请求）
    let initialRun: EvaluationRunRead | null = null;
    if (initialTask?.id) {
      const rid = (initialTask.result as Record<string, unknown> | null)?.run_id;
      if (typeof rid === "string" && rid) {
        initialRun = state.runs.find((r) => r.id === rid) || null;
      }
    } else if (runId) {
      initialRun = state.runs.find((r) => r.id === runId) || null;
    }
    setDetailDrawer({
      open: true,
      loading: true,
      taskId,
      runId,
      task: initialTask,
      run: initialRun,
      activeTab,
    });
    try {
      // 同时加载 task 详情（errors_json）和 run（如果有 id）
      const [taskResp, runResp] = await Promise.all([
        taskId ? api.getEvaluationTask(taskId) : Promise.resolve(null),
        runId ? api.getEvaluationRun(runId) : (
          (initialTask?.result as Record<string, unknown> | null)?.run_id
            ? api.getEvaluationRun((initialTask!.result as Record<string, unknown>).run_id as string)
            : Promise.resolve(null)
        ),
      ]);
      const finalTask = taskResp || initialTask;
      let finalActiveTab = activeTab;
      if (finalTask && ["failed", "cancelled"].includes(finalTask.status) && !runId) {
        finalActiveTab = "errors";
      }
      setDetailDrawer((prev) => ({
        ...prev,
        loading: false,
        task: taskResp || prev.task,
        run: runResp || prev.run,
        activeTab: finalActiveTab,
      }));
      // 也同步更新页面的 selectedRun，保持和原来行为一致（用户不切 Drawer 切回页面 Tab 也能看到）
      if (runResp) setState((prev) => ({ ...prev, selectedRun: runResp }));
    } catch (err: any) {
      setDetailDrawer((prev) => ({ ...prev, loading: false }));
      message.error(err?.message || "详情加载失败");
    }
  }, [state.tasks, state.runs, message]);

  const closeDetailDrawer = useCallback(() => {
    setDetailDrawer({
      open: false,
      loading: false,
      taskId: null,
      runId: null,
      task: null,
      run: null,
      activeTab: "overview",
    });
  }, []);

  /** 从任务上下文恢复因子代码，避免提交后清空表单导致修复链接打开“新增”。 */
  const resolveRepairFactorCode = useCallback((): string | null => {
    if (factorCode.trim()) return factorCode.trim();
    const task = detailDrawer.task;
    const resultCode = (task?.result as Record<string, unknown> | null)?.factor_code;
    if (typeof resultCode === "string" && resultCode.trim()) return resultCode.trim();
    const payloadRaw = (task as unknown as Record<string, unknown> | null)?.payload_json;
    if (typeof payloadRaw === "string") {
      try {
        const payload = JSON.parse(payloadRaw) as Record<string, unknown>;
        const payloadCode = payload.factor_code;
        if (typeof payloadCode === "string" && payloadCode.trim()) return payloadCode.trim();
      } catch {
        // Keep the fallback path below when an old task has malformed payload JSON.
      }
    }
    return null;
  }, [detailDrawer.task, factorCode]);

  /** 点击 fix_link：根据类型跳转到对应位置或打开弹窗 */
  const handleFixLinkClick = useCallback((fixLink: { tab?: string; subtab?: string; label_zh?: string }) => {
    if (!fixLink) return;
    const tab = fixLink.tab;
    const subtab = fixLink.subtab;

    const dispatchFactorCenterNavigation = (target: string, factor_code?: string | null) => {
      const dispatch = () => window.dispatchEvent(new CustomEvent("factor-center:navigate", {
        detail: { target, factor_code: factor_code || null },
      }));
      // FactorCenter 可能刚由顶层导航挂载，延迟一拍确保监听器已注册。
      window.setTimeout(dispatch, 0);
    };

    // 1. 跳转到因子编辑器弹窗（修复公式/版本）
    if (tab === "factors" && subtab === "editor") {
      ctx.setActiveTab("settings");
      window.dispatchEvent(new CustomEvent("settings:navigate", { detail: "factor-center" }));
      dispatchFactorCenterNavigation("editor", resolveRepairFactorCode());
      return;
    }

    // 目标标签管理目前归属于设置中的因子中心；兼容旧的 factor-laboratory 链接。
    if ((tab === "settings" && subtab === "factor-center") || tab === "factor-laboratory") {
      ctx.setActiveTab("settings");
      window.dispatchEvent(new CustomEvent("settings:navigate", { detail: "factor-center" }));
      dispatchFactorCenterNavigation("evaluation");
      return;
    }

    // 兼容旧的 factors/* 链接（例如 PIT Join），统一落到真实存在的因子中心。
    if (tab === "factors") {
      ctx.setActiveTab("settings");
      window.dispatchEvent(new CustomEvent("settings:navigate", { detail: "factor-center" }));
      dispatchFactorCenterNavigation("evaluation");
      return;
    }

    // 2. 跳转到评估实验室内部的指定 tab（如数据诊断）
    if (tab === "evaluation" || !tab) {
      if (subtab === "diagnostics") {
        setActiveResultTab("diagnostics");
        return;
      }
      if (subtab === "stability") {
        setActiveResultTab("stability");
        return;
      }
      if (subtab === "runs") {
        setActiveResultTab("runs");
        return;
      }
      // 默认跳转到评价结果（overview）
      if (subtab === "overview" || !subtab) {
        setActiveResultTab("overview");
        return;
      }
    }

    // 3. 其他情况：使用原逻辑切换顶层 tab
    if (tab) {
      // 历史 blocker 使用 factors 作为顶层 tab，但实际入口在设置页。
      ctx.setActiveTab(tab === "factors" ? "settings" : tab);
    }
  }, [ctx, resolveRepairFactorCode]);

  /** 错误详情抽屉中的修复路径：复用相同逻辑并关闭抽屉 */
  const applyFixLink = useCallback((fixLink: NonNullable<EvalErrorItem["fix_link"]>) => {
    if (!fixLink) return;
    handleFixLinkClick(fixLink);
    closeDetailDrawer();
  }, [handleFixLinkClick, closeDetailDrawer]);


  const runPreflight = useCallback(async () => {
    const code = factorCode.trim();
    if (!code) {
      setPreflightResult(null);
      return;
    }
    setPreflightLoading(true);
    try {
      const response = await api.preflightFactorEvaluation({
        factorCode: code,
        factorVersionId,
        universe,
        startDate: dateRange?.[0]?.format("YYYY-MM-DD"),
        endDate: dateRange?.[1]?.format("YYYY-MM-DD"),
        targetHorizon,
      });
      setPreflightResult(response);
      const marketItem = response.items.find((item) => item.code.startsWith("preflight.market_coverage."));
      const marketLatest = marketItem?.evidence?.latest_trade_date;
      setDataCutoffDate(
        response.overall.data_cutoff_date
        || (typeof marketLatest === "string" ? marketLatest : null),
      );
      const compileItem = response.items.find((item) => item.code === "preflight.formula_compile.ok");
      const contentHash = compileItem?.evidence?.content_hash;
      setExecutionHash(typeof contentHash === "string" ? contentHash : null);
      if (
        response.overall.recommended_date_range &&
        response.overall.recommended_date_range.length === 2 &&
        (!dateRange || (!dateRange[0] && !dateRange[1]))
      ) {
        const [recStart, recEnd] = response.overall.recommended_date_range;
        const recStartD = recStart ? dayjs(recStart) : null;
        const recEndD = recEnd ? dayjs(recEnd) : null;
        if ((recStartD && recStartD.isValid()) || (recEndD && recEndD.isValid())) {
          setDateRange([recStartD, recEndD]);
        }
      }
    } catch (err: any) {
      setPreflightResult(null);
    } finally {
      setPreflightLoading(false);
    }
  }, [factorCode, factorVersionId, universe, dateRange?.[0]?.format("YYYY-MM-DD"), dateRange?.[1]?.format("YYYY-MM-DD"), targetHorizon]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      runPreflight();
    }, 400);
    return () => window.clearTimeout(timer);
  }, [runPreflight]);

  const update = useCallback((patch: Partial<EvalLabState>) => {
    setState((prev) => ({ ...prev, ...patch }));
  }, []);

  const loadTasks = useCallback(async (showLoading = true) => {
    if (showLoading) update({ loadingTasks: true });
    try {
      const tasks = await api.listEvaluationTasks(50);
      const activeFromList =
        tasks.find((t) => !TERMINAL_TASK_STATES.has(t.status)) ?? null;
      update({ tasks, activeTask: activeFromList ?? tasks[0] ?? null, error: null });
    } catch (err: any) {
      update({ error: err?.message || t("evalLabLoadFailed") });
    } finally {
      if (showLoading) update({ loadingTasks: false });
    }
  }, [update]);

  const loadRuns = useCallback(async (showLoading = true) => {
    if (showLoading) update({ loadingRuns: true });
    try {
      const runs = await api.listEvaluationRuns({
        gateResult: state.gateFilter || undefined,
        limit: 50,
      });
      update({ runs, error: null });
    } catch (err: any) {
      update({ error: err?.message || t("evalLabRunLoadFailed") });
    } finally {
      if (showLoading) update({ loadingRuns: false });
    }
  }, [state.gateFilter, update]);

  const loadAll = useCallback(async (showLoading = true) => {
    await Promise.all([loadTasks(showLoading), loadRuns(showLoading)]);
  }, [loadTasks, loadRuns]);

  useEffect(() => {
    loadAll();
  }, [loadAll]);

  // 运行中任务轮询：拿到 task id 后立即拉一次，之后每 2 秒刷新，避免
  // 提交成功到第一次进度反馈之间出现一个完整轮询周期的空窗。
  useEffect(() => {
    const taskId = state.activeTask?.id;
    if (!taskId || !state.activeTask || TERMINAL_TASK_STATES.has(state.activeTask.status)) return;
    let cancelled = false;
    const poll = async () => {
      try {
        const pulse = await api.getEvaluationTaskHeartbeat(taskId);
        if (cancelled) return;
        if (TERMINAL_TASK_STATES.has(pulse.status)) {
          const task = await api.getEvaluationTask(taskId);
          if (!cancelled) update({ activeTask: task });
          await loadAll(false);
        } else {
          // Merge the lightweight pulse into the full task snapshot so the UI
          // updates progress/heartbeat without downloading result/error JSON.
          update({ activeTask: { ...(state.activeTask || {}), ...pulse } as EvaluationTaskRead });
        }
      } catch (err: any) {
        if (cancelled) return;
        update({ error: err?.message || t("evalLabLoadFailed") });
      }
    };
    void poll();
    const timer = window.setInterval(() => { void poll(); }, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [state.activeTask?.id, state.activeTask?.status, loadAll, update]);

  // 计算已运行时长与停滞检测
  useEffect(() => {
    const activeTask = state.activeTask;
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
    if (!lastProgressRef.current || lastProgressRef.current.signature !== signature) {
      lastProgressRef.current = { signature, time: Date.now() };
      setStalled(false);
    }
    const tickTimer = window.setInterval(() => {
      const now = Date.now();
      setElapsed(Math.max(0, Math.floor((now - startTime) / 1000)));
      if (lastProgressRef.current) {
        const stallSeconds = (now - lastProgressRef.current.time) / 1000;
        setStalled(stallSeconds >= STALL_THRESHOLD_SECONDS);
      }
    }, 1000);
    return () => window.clearInterval(tickTimer);
  }, [
    state.activeTask?.id,
    state.activeTask?.status,
    state.activeTask?.percent,
    state.activeTask?.processed,
    state.activeTask?.message,
    state.activeTask?.updated_at,
    state.activeTask?.started_at,
    state.activeTask?.created_at,
  ]);

  // 当任务变为 done 时，从 result.run_id 自动选中对应运行
  useEffect(() => {
    const activeTask = state.activeTask;
    if (!activeTask || !["done", "completed", "warn"].includes(activeTask.status)) return;
    const runId = (activeTask.result as Record<string, unknown> | null)?.run_id;
    if (typeof runId === "string" && runId && !state.selectedRun) {
      api.getEvaluationRun(runId)
        .then((run) => update({ selectedRun: run }))
        .catch(() => { /* 运行记录可能尚未写入，忽略 */ });
    }
  }, [state.activeTask, state.selectedRun, update]);

  const handleSubmit = async () => {
    const code = factorCode.trim();
    if (!code) {
      message.warning(t("evalLabCodeRequired"));
      return;
    }
    update({ submitting: true, error: null });
    try {
      const payload: EvaluationTaskCreatePayload = {
        factor_code: code,
        factor_version_id: factorVersionId ?? undefined,
        universe,
        start_date: dateRange?.[0]?.format("YYYY-MM-DD"),
        end_date: dateRange?.[1]?.format("YYYY-MM-DD"),
        target_horizon: targetHorizon,
        n_groups: nGroups,
        cost_rate: costRate,
        factor_kind: factorKind,
        direction,
        created_by: "local_user",
        // 用户再次点击提交代表显式重跑；后端仍对 queued/running 做并发去重。
        force_new: true,
      };
      const task = await api.createEvaluationTask(payload);
      message.success(t("evalLabCreateSuccess"));
      // 保留当前因子选择，用户可以在任务运行/完成后继续查看同一因子的结果。
      update({ activeTask: task, selectedRun: null });
      await loadAll(false);
      // 列表刷新可能早于异步任务入库，不能用旧列表覆盖刚创建的任务，
      // 否则顶部进度条会在“创建成功”后立即消失。后续由轮询更新状态。
      update({ activeTask: task });
    } catch (err: any) {
      const msg = err?.user_message || err?.message || t("evalLabCreateFailed");
      message.error(msg);
      update({ error: msg });
    } finally {
      update({ submitting: false });
    }
  };

  const handleCancel = async () => {
    const activeTask = state.activeTask;
    if (!activeTask) return;
    update({ cancelling: true });
    try {
      const task = await api.cancelEvaluationTask(activeTask.id);
      update({ activeTask: task });
      message.success(t("evalLabCancelSuccess"));
    } catch (err: any) {
      const msg = err?.user_message || err?.message || t("evalLabCancelFailed");
      message.error(msg);
    } finally {
      update({ cancelling: false });
    }
  };

  const handleViewRun = useCallback(async (runId: string) => {
    try {
      const run = await api.getEvaluationRun(runId);
      update({ selectedRun: run });
    } catch (err: any) {
      message.error(err?.message || t("evalLabRunLoadFailed"));
    }
  }, [update, message]);

  const handleViewTaskRun = useCallback(async (taskId: string) => {
    // 从任务 result.run_id 跳转到运行详情（保留为公共方法，供任务列表/进度卡片复用）
    try {
      const task = await api.getEvaluationTask(taskId);
      update({ activeTask: task });
      const runId = (task.result as Record<string, unknown> | null)?.run_id;
      if (typeof runId === "string" && runId) {
        await handleViewRun(runId);
      }
    } catch (err: any) {
      message.error(err?.message || t("evalLabLoadFailed"));
    }
  }, [update, message, handleViewRun]);
  void handleViewTaskRun; // 预留接口，未来可由行点击触发

  // —— 任务列表列定义 ——
  const taskColumns = useMemo(
    () => [
      {
        title: t("evalLabColFactorCode"),
        dataIndex: ["result", "factor_code"],
        key: "factor_code",
        width: 140,
        render: (value: unknown, record: EvaluationTaskRead) => {
          // 优先从 result 取，回退到 current_item
          const fromResult = (record.result as Record<string, unknown> | null)?.factor_code;
          const v = (fromResult as string) || record.current_item || "-";
          return <span>{String(v)}</span>;
        },
      },
      {
        title: t("evalLabColStatus"),
        dataIndex: "status",
        key: "status",
        width: 360,
        render: (status: string, record: EvaluationTaskRead) => {
          const err = getFirstError(record);
          const corr = getCorrelationId(record);
          const tag = <Tag color={taskStatusColor(status)}>{taskStatusLabel(status)}</Tag>;
          if (!err) {
            if (!corr) return tag;
            return (
              <Space size={4} wrap>
                {tag}
                <Typography.Text code style={{ fontSize: 11, padding: "0 4px" }}>
                  #{corr}
                </Typography.Text>
              </Space>
            );
          }
          const shortCode = (err.code || "unknown").slice(0, 28);
          const shortTitle = (err.title_zh || err.title || "未知错误").slice(0, 14);
          const tipText = errorTooltip(err);
          return (
            <Tooltip
              title={
                <div style={{ maxWidth: 520, whiteSpace: "pre-wrap" }}>
                  {tipText || "无详情"}
                  {corr ? <div style={{ marginTop: 8 }}>追踪号 correlation_id：<span className="mono">{corr}</span></div> : null}
                </div>
              }
              placement="topLeft"
            >
              <Space size={6} wrap direction="vertical" style={{ lineHeight: 1.4 }}>
                <Space size={4} wrap>
                  {tag}
                  {corr ? (
                    <Typography.Text copyable={{ text: corr }} style={{ fontSize: 11, color: "#8c8c8c" }}>
                      #{corr.slice(0, 8)}
                    </Typography.Text>
                  ) : null}
                </Space>
                <Typography.Text
                  type={err.severity === "error" ? "danger" : "warning"}
                  style={{ fontSize: 12, display: "block", lineHeight: 1.3 }}
                >
                  <span className="mono" style={{ opacity: 0.9 }}>[{shortCode}]</span>
                  {" "}
                  {shortTitle}
                </Typography.Text>
              </Space>
            </Tooltip>
          );
        },
      },
      {
        title: t("evalLabColStage"),
        dataIndex: "stage",
        key: "stage",
        width: 120,
        render: (stage: string) => stageLabel(stage),
      },
      {
        title: t("evalLabColProgress"),
        dataIndex: "percent",
        key: "percent",
        width: 100,
        render: (percent: number) => `${(percent || 0).toFixed(1)}%`,
      },
      {
        title: t("evalLabColStartedAt"),
        dataIndex: "started_at",
        key: "started_at",
        width: 160,
        render: (value: string | null) => formatDateTime(value),
      },
      {
        title: t("evalLabColFinishedAt"),
        dataIndex: "finished_at",
        key: "finished_at",
        width: 160,
        render: (value: string | null) => formatDateTime(value),
      },
      {
        title: t("evalLabColActions"),
        key: "actions",
        width: 140,
        render: (_: unknown, record: EvaluationTaskRead) => {
          const runIdRaw = (record.result as Record<string, unknown> | null)?.run_id;
          const runId = typeof runIdRaw === "string" && runIdRaw ? runIdRaw : null;
          const hasError = getFirstError(record) != null;
          return (
            <Button
              size="small"
              type={hasError ? "primary" : "link"}
              danger={hasError}
              icon={hasError ? <BugOutlined /> : undefined}
              onClick={() => handleOpenDetailDrawer({ taskId: record.id, runId })}
            >
              {hasError ? "查看错误" : "查看详情"}
            </Button>
          );
        },
      },
    ],
    [getFirstError, getCorrelationId, errorTooltip, handleOpenDetailDrawer, t],
  );

  // —— 运行历史列定义 ——
  const runColumns = useMemo(
    () => [
      {
        title: t("evalLabColRunId"),
        dataIndex: "id",
        key: "id",
        width: 200,
        render: (id: string) => (
          <Tooltip title={id}>
            <span>{shortId(id)}</span>
          </Tooltip>
        ),
      },
      {
        title: t("evalLabColVersion"),
        dataIndex: "factor_version_id",
        key: "factor_version_id",
        width: 80,
      },
      {
        title: t("evalLabColGate"),
        dataIndex: "gate_result",
        key: "gate_result",
        width: 100,
        render: (gate: string | null) => <Tag color={gateColor(gate)}>{gateLabel(gate)}</Tag>,
      },
      {
        title: "保真",
        key: "fidelity",
        width: 130,
        render: (_: unknown, record: EvaluationRunRead) => {
          const r = record as unknown as Record<string, unknown>;
          const f = r.production_fidelity;
          const fidelity =
            f === true || f === 1 || f === "1" || f === "true";
          const reason = r.non_fidelity_reason as string | null | undefined;
          if (fidelity) {
            return (
              <Badge status="success" text={<span>生产保真</span>} />
            );
          }
          return (
            <Badge
              status="warning"
              text={
                reason ? (
                  <Tooltip title={reason}>
                    <span>非保真实验</span>
                  </Tooltip>
                ) : (
                  <span>非保真实验</span>
                )
              }
            />
          );
        },
      },
      {
        title: t("evalLabColCutoff"),
        dataIndex: "data_cutoff_at",
        key: "data_cutoff_at",
        width: 160,
        render: (value: string | null) => formatDateTime(value),
      },
      {
        title: t("evalLabColCreatedAt"),
        dataIndex: "created_at",
        key: "created_at",
        width: 160,
        render: (value: string | null) => formatDateTime(value),
      },
      {
        title: t("evalLabColActions"),
        key: "actions",
        width: 140,
        render: (_: unknown, record: EvaluationRunRead) => (
          <Button
            size="small"
            type="link"
            icon={<SolutionOutlined />}
            onClick={() => handleOpenDetailDrawer({ runId: record.id })}
          >
            {"查看运行"}
          </Button>
        ),
      },
    ],
    [handleOpenDetailDrawer],
  );

  const activeTask = state.activeTask;
  const selectedRun = state.selectedRun;
  const metrics = normalizeEvaluationMetrics(selectedRun?.metrics);
  const stress = metrics?.stress_test;
  const heartbeatStr = activeTask?.heartbeat_at ? formatDateTime(activeTask.heartbeat_at) : "-";

  const taskRunning = !!state.activeTask && !TERMINAL_TASK_STATES.has(state.activeTask!.status);

  const preflightPassed = preflightResult?.items.filter((i) => i.severity === "pass").length ?? 0;
  const preflightHasBlocker = !!preflightResult && !preflightResult.overall.passed;
  const preflightTotal = preflightResult?.items.length ?? 0;

  // —— 结果区页签配置 ——
  const resultTabItems = [
    {
      key: "overview",
      label: t("evalTabOverview"),
      children: (
        <div className="eval-result-panel">
          {selectedRun ? (
            <EvaluationRunReport run={selectedRun} metrics={metrics} stress={stress} />
          ) : (
            <Card size="small">
              <Empty
                description={t("evalLabNoMetrics")}
                image={Empty.PRESENTED_IMAGE_SIMPLE}
              />
            </Card>
          )}
        </div>
      ),
    },
    {
      key: "stability",
      label: t("evalTabStability"),
      children: (
        <div className="eval-result-panel">
          {selectedRun && stress ? (
            <Card size="small" title={t("evalLabSectionStress")}>
              <Descriptions column={2} size="small" bordered style={{ marginBottom: 12 }}>
                <Descriptions.Item label={t("evalLabStressOverall")}>
                  <Tag color={verdictColor(stress.overall_verdict)}>
                    {verdictLabel(stress.overall_verdict)}
                  </Tag>
                </Descriptions.Item>
                {stress.time_result ? (
                  <Descriptions.Item label={t("evalLabStressTime")}>
                    <Tag color={verdictColor(stress.time_result.verdict)}>
                      {verdictLabel(stress.time_result.verdict)}
                    </Tag>
                    <span style={{ marginLeft: 8 }}>
                      IC 稳定性：{formatNumber(stress.time_result.ic_stability)}
                    </span>
                  </Descriptions.Item>
                ) : null}
              </Descriptions>
              <Typography.Text type="secondary" style={{ fontSize: 13 }}>
                {t("evalStabilityPlaceholder")}
              </Typography.Text>
            </Card>
          ) : (
            <Card size="small">
              <Empty description={t("evalNoStabilityData")} image={Empty.PRESENTED_IMAGE_SIMPLE} />
            </Card>
          )}
        </div>
      ),
    },
    {
      key: "diagnostics",
      label: t("evalTabDiagnostics"),
      children: (
        <div className="eval-result-panel">
          <Card size="small" title={t("evalLabSectionDiagnostics")}>
            <Typography.Paragraph type="secondary">
              {t("evalDiagnosticsPlaceholder")}
            </Typography.Paragraph>
            {selectedRun ? (
              <Descriptions column={2} size="small" bordered>
                <Descriptions.Item label={t("evalLabEvidenceSelectedDate")}>
                  {selectedRun.selected_trade_date || "-"}
                </Descriptions.Item>
                <Descriptions.Item label={t("evalLabEvidenceObserved")}>
                  {selectedRun.observed_symbols ?? "-"}
                </Descriptions.Item>
                <Descriptions.Item label={t("evalLabEvidenceExpected")}>
                  {selectedRun.expected_symbols ?? "-"}
                </Descriptions.Item>
                <Descriptions.Item label={t("evalLabEvidenceRatio")}>
                  {selectedRun.completeness_ratio != null
                    ? formatPercent(selectedRun.completeness_ratio)
                    : "-"}
                </Descriptions.Item>
              </Descriptions>
            ) : null}
          </Card>
        </div>
      ),
    },
    {
      key: "runs",
      label: t("evalTabRuns"),
      children: (
        <div className="eval-result-panel">
          <Card size="small" title={t("evalLabRunHistory")} extra={
            <Select
              size="small"
              value={state.gateFilter || ""}
              onChange={(v) => update({ gateFilter: v || "" })}
              style={{ width: 120 }}
              options={[
                { value: "", label: t("evalLabFilterAll") },
                { value: "passed", label: t("evalLabGate_passed") },
                { value: "rejected", label: t("evalLabGate_rejected") },
                { value: "warn", label: t("evalLabGate_warn") },
              ]}
            />
          }>
            <Spin spinning={state.loadingRuns}>
              {state.runs.length === 0 ? (
                <Empty description={t("evalLabEmptyRuns")} />
              ) : (
                <Table
                  rowKey="id"
                  dataSource={state.runs}
                  columns={runColumns}
                  size="small"
                  pagination={{ pageSize: 10, showSizeChanger: false }}
                  scroll={{ x: 800 }}
                  className="eval-runs-table"
                />
              )}
            </Spin>
          </Card>

          {/* 任务列表 */}
          <Card size="small" title={t("evalLabTaskList")} style={{ marginTop: 12 }}>
            <Spin spinning={state.loadingTasks}>
              {state.tasks.length === 0 ? (
                <Empty description={t("evalLabEmptyTasks")} />
              ) : (
                <Table
                  rowKey="id"
                  dataSource={state.tasks}
                  columns={taskColumns}
                  size="small"
                  pagination={{ pageSize: 10, showSizeChanger: false }}
                  scroll={{ x: 900 }}
                />
              )}
            </Spin>
          </Card>
        </div>
      ),
    },
  ];

  return (
    <div className="factor-eval-lab factor-eval-lab-redesign">
      {/* 页面标题栏 */}
      <div className="eval-header">
        <div className="eval-header-left">
          <h2 className="eval-title">{t("evalLabTitle")}</h2>
          <p className="eval-subtitle">{t("evalLabSubtitleNew")}</p>
        </div>
        <Space>
          <Button
            icon={<ReloadOutlined />}
            onClick={() => loadAll(true)}
            loading={state.loadingTasks || state.loadingRuns}
          >
            {t("evalLabRefresh")}
          </Button>
          <Button icon={<InfoCircleOutlined />}>{t("evalLabMethodology")}</Button>
        </Space>
      </div>

      {state.error ? (
        <Alert
          type="error"
          message={state.error}
          closable
          onClose={() => update({ error: null })}
          style={{ marginBottom: 12 }}
        />
      ) : null}

      {/* 吸顶配置工作台 */}
      <div className="eval-workbench">
        <Row gutter={[12, 12]} align="bottom">
          <Col xs={24} sm={12} md={8} lg={6}>
            <div className="eval-field">
              <label className="eval-field-label">{t("evalWorkbenchFactor")}</label>
              <Select
                showSearch
                value={factorCode || undefined}
                onChange={(v) => setFactorCode(v)}
                placeholder={t("evalLabFactorCodePlaceholder")}
                disabled={taskRunning || state.submitting}
                style={{ width: "100%" }}
                options={factorOptions}
                filterOption={(input, option) =>
                  (option?.label as string ?? "").toLowerCase().includes(input.toLowerCase())
                }
                notFoundContent={factorLoadError ? factorLoadError : t("evalLabLoadingFactors")}
                allowClear
                size="large"
              />
            </div>
          </Col>
          <Col xs={12} sm={6} md={4} lg={3}>
            <div className="eval-field">
              <label className="eval-field-label">{t("evalWorkbenchKind")}</label>
              <Select
                value={factorKind}
                onChange={(v) => setFactorKind(v)}
                disabled={taskRunning || state.submitting}
                style={{ width: "100%" }}
                size="large"
                options={[
                  { value: "continuous", label: t("factorKind_continuous") },
                  { value: "event", label: t("factorKind_event") },
                  { value: "regime", label: t("factorKind_regime") },
                ]}
              />
            </div>
          </Col>
          <Col xs={12} sm={6} md={4} lg={3}>
            <div className="eval-field">
              <label className="eval-field-label">{t("evalWorkbenchUniverse")}</label>
              <Select
                value={universe}
                onChange={(v) => setUniverse(v)}
                disabled={taskRunning || state.submitting}
                style={{ width: "100%" }}
                size="large"
                options={[
                  { value: "ashare_all", label: t("evalUniverseAllA") },
                  { value: "hs300", label: t("evalUniverseHS300") },
                  { value: "zz500", label: t("evalUniverseZZ500") },
                  { value: "zz1000", label: t("evalUniverseZZ1000") },
                ]}
              />
            </div>
          </Col>
          <Col xs={12} sm={8} md={4} lg={3}>
            <div className="eval-field">
              <label className="eval-field-label">{t("evalWorkbenchPeriod")}</label>
              <div className="eval-date-range">
                <DatePicker
                  size="large"
                  style={{ width: "100%" }}
                  placeholder="选择开始日期"
                  value={dateRange?.[0] ?? null}
                  onChange={(v) => setDateRange([v, dateRange?.[1] ?? null])}
                />
              </div>
            </div>
          </Col>
          <Col xs={12} sm={8} md={4} lg={3}>
            <div className="eval-field">
              <label className="eval-field-label">&nbsp;</label>
              <div className="eval-date-range">
                <DatePicker
                  size="large"
                  style={{ width: "100%" }}
                  placeholder="选择结束日期"
                  value={dateRange?.[1] ?? null}
                  onChange={(v) => setDateRange([dateRange?.[0] ?? null, v])}
                />
              </div>
            </div>
          </Col>
          <Col xs={12} sm={8} md={4} lg={3}>
            <div className="eval-field">
              <label className="eval-field-label">{t("evalWorkbenchTarget")}</label>
              <Select
                value={targetHorizon}
                onChange={(v) => setTargetHorizon(Number(v) || 5)}
                disabled={taskRunning || state.submitting}
                style={{ width: "100%" }}
                size="large"
                options={[
                  { value: 1, label: "T+1 开盘 → T+1 收盘" },
                  { value: 3, label: "T+1 开盘 → T+3 收盘" },
                  { value: 5, label: "T+1 开盘 → T+5 收盘" },
                  { value: 10, label: "T+1 开盘 → T+10 收盘" },
                  { value: 20, label: "T+1 开盘 → T+20 收盘" },
                ]}
              />
            </div>
          </Col>
          <Col xs={24} sm={24} md={24} lg={3} xl={3} xxl={3} className="eval-submit-col">
            <div className="eval-field eval-field-submit">
              <Button
                type="primary"
                size="large"
                onClick={handleSubmit}
                loading={state.submitting}
                disabled={
                  taskRunning ||
                  preflightLoading ||
                  !preflightResult ||
                  !preflightResult.overall.passed ||
                  !factorCode
                }
                title={!factorCode ? "请先选择因子，系统会自动执行运行前检查" : undefined}
                block
                className="eval-run-btn"
              >
                {t("evalLabSubmit")}
              </Button>
              {!factorCode ? (
                <Typography.Text type="secondary" style={{ fontSize: 12, marginTop: 4, display: "block" }}>
                  请先选择因子，系统会自动执行运行前检查。
                </Typography.Text>
              ) : preflightResult && !preflightResult.overall.passed ? (
                <Typography.Text type="danger" style={{ fontSize: 12, marginTop: 4, display: "block" }}>
                  就绪检查未通过，请先解决阻断项。
                </Typography.Text>
              ) : null}
            </div>
          </Col>
        </Row>

        {/* 第二行：只读摘要 + 高级参数 */}
        <Row gutter={[12, 8]} align="middle" className="eval-workbench-meta">
          <Col flex="auto">
            <Space size="middle" wrap className="eval-meta-list">
              <span className="eval-meta-item">
                <span className="eval-meta-label">{t("evalMetaDirection")}：</span>
                <span className="eval-meta-value">{t("evalMetaDirectionHigh")}</span>
              </span>
              <span className="eval-meta-item">
                <span className="eval-meta-label">{t("evalMetaAdjust")}：</span>
                <span className="eval-meta-value">{t("evalMetaAdjustForward")}</span>
              </span>
              <span className="eval-meta-item">
                <span className="eval-meta-label">{t("evalMetaCutoff")}：</span>
                <span className="eval-meta-value">{dataCutoffDate || "-"}</span>
              </span>
              <span className="eval-meta-item">
                <span className="eval-meta-label">{t("evalMetaExecHash")}：</span>
                <span className="eval-meta-value mono">{executionHash || "-"}</span>
              </span>
              <span className="eval-meta-item">
                <span className="eval-meta-label">{t("evalMetaFrozen")}：</span>
                <span className="eval-meta-value">{t("evalMetaFrozenYes")}</span>
              </span>
            </Space>
          </Col>
          <Col flex="none">
            <Button icon={<SettingOutlined />} onClick={() => setAdvancedDrawerOpen(true)}>
              {t("evalAdvancedParams")}
            </Button>
          </Col>
        </Row>

        {/* 运行中任务轻量提示条 */}
        {activeTask || state.submitting ? (
          <div className="eval-running-banner" aria-busy={state.submitting || taskRunning}>
            <Space size="middle">
              <Tag
                color={state.submitting || taskRunning ? "processing" : taskStatusColor(activeTask!.status)}
                icon={state.submitting || taskRunning ? <ClockCircleOutlined /> : undefined}
              >
                {state.submitting ? "提交中" : taskStatusLabel(activeTask!.status)}
              </Tag>
              <span className="eval-running-stage">
                {state.submitting && !activeTask ? "创建任务：正在提交评估任务，请稍候…" : `${stageLabel(activeTask!.stage)}：${taskMessageLabel(activeTask!.message)}`}
              </span>
              <Progress
                percent={Math.round(activeTask?.percent || 0)}
                status={!activeTask || activeTask.status === "running" || activeTask.status === "queued" ? "active" : activeTask.status === "failed" ? "exception" : activeTask.status === "done" || activeTask.status === "completed" || activeTask.status === "warn" ? "success" : "active"}
                size="small"
                style={{ width: 200 }}
              />
              <span className="eval-running-elapsed">{elapsed}s</span>
              {taskRunning && activeTask ? (
                <Button size="small" danger icon={<StopOutlined />} onClick={handleCancel} loading={state.cancelling}>
                  {t("evalLabCancel")}
                </Button>
              ) : null}
            </Space>
          </div>
        ) : null}
      </div>

      {/* 双栏主内容区 */}
      <Row gutter={12} className="eval-main-content">
        {/* 左侧：结果页签区 */}
        <Col xs={24} md={14} lg={16} xl={17} xxl={18}>
          <Card size="small" className="eval-result-card eval-result-main-card">
            <Tabs
              activeKey={activeResultTab}
              onChange={setActiveResultTab}
              items={resultTabItems}
              size="small"
            />
          </Card>
        </Col>

        {/* 右侧：就绪检查区 */}
        <Col xs={24} md={10} lg={8} xl={7} xxl={6}>
          <div className="eval-preflight-panel">
            <Card size="small" title={
              <div className="eval-preflight-title">
                <span>{t("evalPreflightTitle")}</span>
                <Tag color={preflightHasBlocker ? "red" : preflightTotal > 0 ? "green" : "default"}>
                  {preflightPassed}/{preflightTotal}
                </Tag>
              </div>
            }>
              {preflightLoading ? (
                <Alert
                  type="info"
                  message="正在进行就绪检查..."
                  showIcon
                  icon={<Spin size="small" />}
                  style={{ marginBottom: 12 }}
                />
              ) : preflightHasBlocker ? (
                <Alert
                  type="error"
                  message={t("evalPreflightHasBlocker")}
                  description={t("evalPreflightBlockerHint")}
                  showIcon
                  style={{ marginBottom: 12 }}
                />
              ) : preflightResult && factorCode ? (
                <Alert
                  type="success"
                  message={t("evalPreflightReady")}
                  description={t("evalPreflightReadyHint")}
                  showIcon
                  style={{ marginBottom: 12 }}
                />
              ) : (
                <Alert
                  type="info"
                  message={t("evalPreflightSelectFactor")}
                  showIcon
                  style={{ marginBottom: 12 }}
                />
              )}

              <List
                size="small"
                dataSource={preflightResult?.items ?? []}
                locale={{ emptyText: factorCode && !preflightLoading ? "暂无检查项" : "-" }}
                loading={preflightLoading}
                renderItem={(item) => {
                  const severityColor =
                    item.severity === "pass"
                      ? "#52c41a"
                      : item.severity === "warn"
                        ? "#faad14"
                        : item.severity === "error"
                          ? "#ff4d4f"
                          : "#1890ff";
                  const icon =
                    item.severity === "pass" ? (
                      <CheckCircleFilled style={{ color: severityColor, fontSize: 16 }} />
                    ) : item.severity === "warn" ? (
                      <ExclamationCircleFilled style={{ color: severityColor, fontSize: 16 }} />
                    ) : item.severity === "error" ? (
                      <CloseCircleFilled style={{ color: severityColor, fontSize: 16 }} />
                    ) : (
                      <InfoCircleFilled style={{ color: severityColor, fontSize: 16 }} />
                    );
                  const evidenceEntries = item.evidence ? Object.entries(item.evidence).filter(([k]) => k !== "__proto__") : [];
                  const evidenceDisplay = evidenceEntries.slice(0, 5).map(([k, v]) => {
                    let valueStr: string;
                    if (Array.isArray(v) && v.length > 10) {
                      valueStr = `[${v.slice(0, 10).join(", ")}, ...] (共 ${v.length} 项)`;
                    } else if (typeof v === "object" && v !== null) {
                      valueStr = JSON.stringify(v).slice(0, 80);
                      if (JSON.stringify(v).length > 80) valueStr += "...";
                    } else {
                      valueStr = String(v);
                    }
                    return (
                      <div key={k} style={{ fontSize: 12, color: "#8c8c8c" }}>
                        {k}: {valueStr}
                      </div>
                    );
                  });
                  const itemContent = (
                    <List.Item className="preflight-item">
                      <Space size={10} style={{ width: "100%", alignItems: "flex-start" }}>
                        {icon}
                        <div style={{ flex: 1, minWidth: 0 }}>
                          <div className="preflight-item-name" style={{ color: severityColor }}>
                            {item.title_zh}
                          </div>
                          <div className="preflight-item-detail">{item.detail_zh}</div>
                          {evidenceDisplay.length > 0 ? (
                            <div style={{ marginTop: 4 }}>{evidenceDisplay}</div>
                          ) : null}
                          {item.fix_link && item.fix_link.label_zh ? (
                            <div style={{ marginTop: 6 }}>
                              <Button
                                type="link"
                                size="small"
                                style={{ padding: 0, height: "auto" }}
                                className="preflight-fix-link-btn"
                                onClick={() => {
                                  handleFixLinkClick(item.fix_link!);
                                }}
                              >
                                修复路径 → {item.fix_link.label_zh}
                              </Button>
                            </div>
                          ) : null}
                        </div>
                      </Space>
                    </List.Item>
                  );
                  if (item.severity === "error") {
                    return (
                      <Tooltip title="阻断项，点击修复路径解决后再运行。" key={item.code}>
                        {itemContent}
                      </Tooltip>
                    );
                  }
                  return <div key={item.code}>{itemContent}</div>;
                }}
              />
            </Card>
          </div>
        </Col>
      </Row>

      {/* 高级参数抽屉 */}
      <Drawer
        title={t("evalAdvancedParams")}
        placement="right"
        width={480}
        open={advancedDrawerOpen}
        onClose={() => setAdvancedDrawerOpen(false)}
        className="eval-advanced-drawer"
      >
        <div className="eval-advanced-section">
          <h4 className="eval-advanced-section-title">{t("evalAdvancedGroup")}</h4>
          <Row gutter={[12, 12]}>
            <Col span={12}>
              <div className="eval-field">
                <label className="eval-field-label">{t("evalLabNGroups")}</label>
                <InputNumber
                  value={nGroups}
                  onChange={(v) => setNGroups(Number(v) || 5)}
                  min={2}
                  max={10}
                  style={{ width: "100%" }}
                />
              </div>
            </Col>
            <Col span={12}>
              <div className="eval-field">
                <label className="eval-field-label">{t("evalLabCostRate")}</label>
                <InputNumber
                  value={costRate}
                  onChange={(v) => setCostRate(Number(v) || 0)}
                  min={0}
                  max={0.01}
                  step={0.0005}
                  style={{ width: "100%" }}
                />
              </div>
            </Col>
          </Row>
        </div>
        <div className="eval-advanced-section">
          <h4 className="eval-advanced-section-title">{t("evalAdvancedTime")}</h4>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {t("evalAdvancedTimeHint")}
          </Typography.Text>
        </div>
        <div className="eval-advanced-section">
          <h4 className="eval-advanced-section-title">{t("evalAdvancedPostprocess")}</h4>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {t("evalAdvancedPostprocessHint")}
          </Typography.Text>
        </div>
        <div className="eval-advanced-footer">
          <Space>
            <Button>{t("evalAdvancedReset")}</Button>
            <Button type="primary" onClick={() => setAdvancedDrawerOpen(false)}>
              {t("evalAdvancedApply")}
            </Button>
          </Space>
        </div>
      </Drawer>

      {/* ============================================================ */}
      {/* 【新增】任务/运行详情 Drawer（弹窗式，不依赖页面 Tab 切换） */}
      {/* ============================================================ */}
      <Drawer
        title={
          <Space size={8}>
            <BugOutlined style={{ color: "#eb2f96" }} />
            <span>
              任务/运行详情
              {detailDrawer.task ? (
                <Tag color="blue" style={{ marginLeft: 8 }}>
                  Task: {String((detailDrawer.task.id || "").slice(0, 12))}
                </Tag>
              ) : null}
              {detailDrawer.run ? (
                <Tag color="purple" style={{ marginLeft: 4 }}>
                  Run: {String((detailDrawer.run.id || "").slice(0, 12))}
                </Tag>
              ) : null}
            </span>
          </Space>
        }
        open={detailDrawer.open}
        onClose={closeDetailDrawer}
        width="min(1200px, 85vw)"
        destroyOnHidden
        maskClosable={false}
      >
        <Spin spinning={detailDrawer.loading}>
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 12 }}
            message={"提示：弹窗内所有操作（查看错误 / 跳转修复）均不阻塞主页面，关闭后可立即发起下一次评价"}
            icon={<InfoCircleOutlined />}
          />
          <Tabs
            activeKey={detailDrawer.activeTab}
            onChange={(k) =>
              setDetailDrawer((p) => ({ ...p, activeTab: k as DetailDrawerState["activeTab"] }))
            }
            size="small"
            items={[
              /* ===== Tab 1：评价结果 ===== */
              {
                key: "overview",
                label: "评价结果",
                children: (
                  <div>
                    {detailDrawer.run ? (
                      <EvaluationRunReport
                        run={detailDrawer.run}
                        metrics={normalizeEvaluationMetrics(detailDrawer.run.metrics)}
                        stress={
                          normalizeEvaluationMetrics(detailDrawer.run.metrics).stress_test
                        }
                      />
                    ) : (
                      <Card size="small">
                        <Empty description={"暂无评价结果（可能在执行 70% 前失败，请看错误与门禁 Tab）"} />
                      </Card>
                    )}
                  </div>
                ),
              },
              /* ===== Tab 2：稳定性 ===== */
              {
                key: "stability",
                label: "稳定性",
                children: (() => {
                  const run = detailDrawer.run;
                  const stress = run ? normalizeEvaluationMetrics(run.metrics).stress_test : undefined;
                  if (!run || !stress) {
                    return (
                      <Card size="small">
                        <Empty description={"暂无稳定性数据（请先完成 70% 之后的评价阶段）"} />
                      </Card>
                    );
                  }
                  return (
                    <Card size="small" title={"压力测试结论"}>
                      <Descriptions column={2} size="small" bordered style={{ marginBottom: 12 }}>
                        <Descriptions.Item label={"总体结论"}>
                          <Tag color={verdictColor(stress.overall_verdict)}>
                            {verdictLabel(stress.overall_verdict)}
                          </Tag>
                        </Descriptions.Item>
                        {stress.time_result ? (
                          <Descriptions.Item label={"时间稳定性"}>
                            <Tag color={verdictColor(stress.time_result.verdict)}>
                              {verdictLabel(stress.time_result.verdict)}
                            </Tag>
                            <span style={{ marginLeft: 8 }}>
                              IC 稳定性：{formatNumber(stress.time_result.ic_stability)}
                            </span>
                          </Descriptions.Item>
                        ) : null}
                      </Descriptions>
                      {Array.isArray((stress as unknown as Record<string, unknown>).stress_failures) &&
                      ((stress as unknown as Record<string, unknown>).stress_failures as unknown[]).length > 0 ? (
                        <>
                          <Typography.Title level={5} style={{ marginTop: 8 }}>
                            ⚠️ 压力失败项
                          </Typography.Title>
                          <List
                            size="small"
                            bordered
                            dataSource={
                              (stress as unknown as Record<string, unknown>).stress_failures as Record<string, unknown>[]
                            }
                            renderItem={(item) => (
                              <List.Item>
                                <Tag color="red">FAIL</Tag>
                                <span style={{ marginLeft: 8 }}>
                                  {String((item.code as string) || "")} · {String((item.detail as string) || "")}
                                </span>
                              </List.Item>
                            )}
                          />
                        </>
                      ) : null}
                    </Card>
                  );
                })(),
              },
              /* ===== Tab 3：数据诊断 ===== */
              {
                key: "diagnostics",
                label: "数据诊断",
                children: (() => {
                  const run = detailDrawer.run;
                  if (!run) {
                    return (
                      <Card size="small">
                        <Empty description={"暂无运行数据"} />
                      </Card>
                    );
                  }
                  return (
                    <Card size="small" title={"覆盖情况"}>
                      <Descriptions column={2} size="small" bordered>
                        <Descriptions.Item label={"数据截止"}>
                          {formatDateTime((run as unknown as Record<string, unknown>).data_cutoff_at as string)}
                        </Descriptions.Item>
                        <Descriptions.Item label={"选中交易日"}>
                          {run.selected_trade_date || "-"}
                        </Descriptions.Item>
                        <Descriptions.Item label={"观测股票数"}>
                          {run.observed_symbols ?? "-"}
                        </Descriptions.Item>
                        <Descriptions.Item label={"预期股票数"}>
                          {run.expected_symbols ?? "-"}
                        </Descriptions.Item>
                        <Descriptions.Item label={"完整率"}>
                          {run.completeness_ratio != null ? formatPercent(run.completeness_ratio) : "-"}
                        </Descriptions.Item>
                        <Descriptions.Item label={"目标标签"}>
                          {String((run as unknown as Record<string, unknown>).target_code || "-")}
                        </Descriptions.Item>
                      </Descriptions>
                    </Card>
                  );
                })(),
              },
              /* ===== Tab 4：错误与门禁（新增！） ===== */
              {
                key: "errors",
                label: (
                  <Space>
                    错误与门禁
                    {(() => {
                      const t = detailDrawer.task;
                      const r = detailDrawer.run;
                      const total =
                        parseErrors((t as unknown as Record<string, unknown> | null)?.errors as unknown).length +
                        parseErrors((r as unknown as Record<string, unknown> | null)?.rejection_reasons as unknown).length;
                      return total > 0 ? <Badge count={total} color="red" /> : null;
                    })()}
                  </Space>
                ),
                children: (() => {
                  const task = detailDrawer.task;
                  const run = detailDrawer.run;
                  const taskErrors = parseErrors(
                    (task as unknown as Record<string, unknown> | null)?.errors as unknown,
                  );
                  const rejectionReasons = parseErrors(
                    (run as unknown as Record<string, unknown> | null)?.rejection_reasons as unknown,
                  );

                  const renderErrorsList = (items: EvalErrorItem[], title: string, badgeColor: string) => (
                    <Card
                      size="small"
                      style={{ marginBottom: 12 }}
                      title={
                        <Space>
                          <Badge color={badgeColor} />
                          {title}
                          <Tag style={{ marginLeft: 8 }}>{items.length}</Tag>
                        </Space>
                      }
                    >
                      {items.length === 0 ? (
                        <Empty
                          description={
                            <Typography.Text type="success">
                              <CheckCircleFilled style={{ marginRight: 4 }} />
                              无错误/拒绝项
                            </Typography.Text>
                          }
                        />
                      ) : (
                        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
                          {items.map((e, idx) => {
                            const corr =
                              (e.evidence as Record<string, unknown> | undefined)?.correlation_id ||
                              null;
                            return (
                              <Alert
                                key={idx}
                                type={
                                  e.severity === "error"
                                    ? "error"
                                    : e.severity === "warning"
                                    ? "warning"
                                    : e.severity === "pass"
                                    ? "success"
                                    : "info"
                                }
                                showIcon
                                icon={severityIcon(e.severity)}
                                message={
                                  <Space size={8} wrap>
                                    <Tag color={severityTagColor(e.severity)} style={{ margin: 0 }}>
                                      {errorCategoryLabel(e.category)}
                                    </Tag>
                                    <span className="mono" style={{ fontSize: 12 }}>
                                      [{e.code || "unknown_code"}]
                                    </span>
                                    <Typography.Text strong>{e.title_zh || e.title || "未知错误"}</Typography.Text>
                                    {corr ? (
                                      <Tooltip title={"点击复制追踪号"}>
                                        <Typography.Link
                                          copyable={{ text: String(corr) }}
                                          style={{ fontSize: 12 }}
                                        >
                                          #
                                          <span className="mono">
                                            {String(corr).slice(0, 8)}
                                          </span>
                                        </Typography.Link>
                                      </Tooltip>
                                    ) : null}
                                  </Space>
                                }
                                description={
                                  <div>
                                    <Typography.Paragraph style={{ margin: "4px 0 8px 0" }}>
                                      {e.detail_zh || e.detail || "——"}
                                    </Typography.Paragraph>
                                    {e.evidence &&
                                    typeof e.evidence === "object" &&
                                    Object.keys(e.evidence).length > 0 ? (
                                      <Card size="small" type="inner" title={"证据"}>
                                        <Descriptions column={1} size="small" bordered>
                                          {Object.entries(e.evidence).map(([k, v]) => (
                                            <Descriptions.Item key={k} label={evidenceKeyLabel(k)}>
                                              {typeof v === "object" ? (
                                                <pre style={{ margin: 0, whiteSpace: "pre-wrap" }}>
                                                  {JSON.stringify(v, null, 2).slice(0, 800)}
                                                </pre>
                                              ) : (
                                                <Typography.Text code>{String(v).slice(0, 300)}</Typography.Text>
                                              )}
                                            </Descriptions.Item>
                                          ))}
                                        </Descriptions>
                                      </Card>
                                    ) : null}
                                    {e.fix_link ? (
                                      <div style={{ marginTop: 8, textAlign: "right" }}>
                                        <Button
                                          type="primary"
                                          size="small"
                                          icon={<ArrowRightOutlined />}
                                          onClick={() => applyFixLink(e.fix_link!)}
                                        >
                                          {e.fix_link.label_zh || "前往修复"}
                                        </Button>
                                      </div>
                                    ) : null}
                                  </div>
                                }
                              />
                            );
                          })}
                        </div>
                      )}
                    </Card>
                  );

                  return (
                    <div>
                      {/* 门禁概览 */}
                      <Card size="small" style={{ marginBottom: 12 }} title={"门禁概览"}>
                        <Descriptions column={3} size="small" bordered>
                          <Descriptions.Item label={"门禁结论"}>
                            {run ? (
                              <Tag color={gateColor(run.gate_result)}>{gateLabel(run.gate_result)}</Tag>
                            ) : (
                              <Tag color="default">无运行记录（任务未到写入阶段就失败了）</Tag>
                            )}
                          </Descriptions.Item>
                          <Descriptions.Item label={"任务状态"}>
                            {task ? (
                              <Tag color={taskStatusColor(task.status)}>
                                {taskStatusLabel(task.status)} · {stageLabel(task.stage)}
                              </Tag>
                            ) : (
                              "-"
                            )}
                          </Descriptions.Item>
                          <Descriptions.Item label={"进度"}>
                            {task ? `${(task.percent || 0).toFixed(1)}% —— ${(task.message || "")
                              .replace(/gate=rejected/g, "门禁=拒绝")
                              .replace(/gate=warn/g, "门禁=警告")
                              .replace(/gate=passed/g, "门禁=通过")
                              .replace(/压力=stable/g, "压力=稳定")
                              .replace(/压力=unstable/g, "压力=不稳定")}` : "-"}
                          </Descriptions.Item>
                          <Descriptions.Item label={"追踪号"} span={3}>
                            {(() => {
                              const c = getCorrelationId(task) || getCorrelationId(run);
                              if (!c) {
                                return (
                                  <Typography.Text type="secondary">
                                    <WarningOutlined /> 暂无追踪号（后端修复后新任务会自动生成 8 位十六进制 correlation_id）
                                  </Typography.Text>
                                );
                              }
                              return (
                                <Space>
                                  <Typography.Text copyable={{ text: c }} className="mono">
                                    {c}
                                  </Typography.Text>
                                  <Button size="small" icon={<CopyOutlined />} onClick={() => navigator.clipboard?.writeText(c)}>
                                    复制
                                  </Button>
                                </Space>
                              );
                            })()}
                          </Descriptions.Item>
                        </Descriptions>
                      </Card>

                      {renderErrorsList(taskErrors, "任务错误（执行阶段）", "#ff4d4f")}
                      {renderErrorsList(rejectionReasons, "门禁拒绝原因（评价阶段）", "#faad14")}
                    </div>
                  );
                })(),
              },
            ]}
          />
        </Spin>
      </Drawer>
    </div>
  );
}

// ══════════════════════════════════════════════════════════
// 评估运行报告子组件
// ══════════════════════════════════════════════════════════

interface EvaluationRunReportProps {
  run: EvaluationRunRead;
  metrics: EvaluationRunRead["metrics"] | undefined;
  stress: StressTestSummary | undefined;
}

const DEFAULT_ZERO_EXCLUDED: ExcludedSymbolDays = {
  new_listing: 0,
  st: 0,
  suspended: 0,
  delisting_period: 0,
  status_unknown: 0,
  price_or_volume_invalid: 0,
};

type AuditEventRow = {
  symbol_id: number;
  trade_date: string;
  rule_code: string;
  reason?: string | null;
};

function EvaluationRunReport({ run, metrics, stress }: EvaluationRunReportProps) {
  const m = normalizeEvaluationMetrics(metrics);
  const hasMetrics = Object.keys(m).length > 0;
  const quantileReturns = Array.isArray(m.quantile_returns) ? m.quantile_returns : [];

  // ---- Task 20: 过滤与数据治理（BFG） ----
  const [auditOpen, setAuditOpen] = useState(false);
  // 兼容扁平/嵌套两种 DTO：evaluation 字段可能直接挂在 run 上
  const evaluation = (run as unknown as Record<string, unknown>) || {};
  const excluded: ExcludedSymbolDays =
    ((evaluation.excluded_symbol_days as ExcludedSymbolDays | undefined) ??
      DEFAULT_ZERO_EXCLUDED);
  const rawCount = evaluation.raw_count as number | null | undefined;
  const filteredCount = evaluation.filtered_count as number | null | undefined;
  const effectiveCount = evaluation.effective_count as number | null | undefined;
  const filterConfig = evaluation.filter_config as
    | Record<string, unknown>
    | null
    | undefined;
  const dataCutoff = (evaluation.data_cutoff_date as string | null | undefined)
    ?? (evaluation.data_cutoff_at as string | null | undefined)
    ?? run.data_cutoff_at;
  const targetBatchId = evaluation.target_batch_id as string | null | undefined;
  const auditEvents: AuditEventRow[] =
    (evaluation.audit_events as AuditEventRow[] | null | undefined) ?? [];
  const fidelityFlag = evaluation.production_fidelity as
    | number
    | boolean
    | null
    | undefined;
  const nonFidelityReason = evaluation.non_fidelity_reason as
    | string
    | null
    | undefined;

  const ruleState = (key: string): boolean => {
    if (!filterConfig) return true; // 未提供则默认视为开启（展示保守）
    const val = (filterConfig as Record<string, unknown>)[key];
    if (typeof val === "boolean") return val;
    if (typeof val === "number") return val !== 0;
    return true;
  };
  const ruleRows: Array<{ k: string; label: string; on: boolean }> = [
    { k: "filter_new_listing", label: "次新股过滤", on: ruleState("filter_new_listing") },
    { k: "filter_st", label: "ST 过滤", on: ruleState("filter_st") },
    { k: "filter_suspended", label: "停牌过滤", on: ruleState("filter_suspended") },
    {
      k: "delisting_period_excluded",
      label: "退市整理期过滤",
      on: ruleState("delisting_period_excluded"),
    },
  ];
  const fidelityBool =
    fidelityFlag === true ||
    fidelityFlag === 1 ||
    (fidelityFlag as unknown as string) === "1" ||
    (fidelityFlag as unknown as string) === "true";

  return (
    <Card
      title={
        <Space>
          <span>{t("evalLabSectionReport")}</span>
          <Tag color={gateColor(run.gate_result)}>{gateLabel(run.gate_result)}</Tag>
        </Space>
      }
      size="small"
      style={{ marginBottom: 12 }}
      className="eval-report-card"
      extra={
        <Tooltip title={run.id}>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {shortId(run.id)}
          </Typography.Text>
        </Tooltip>
      }
    >
      <Alert
        type="info"
        message={t("evalLabRunImmutable")}
        showIcon
        style={{ marginBottom: 12 }}
      />

      {/* 门禁结论 + 拒绝原因 */}
      <Card
        type="inner"
        title={t("evalLabSectionGate")}
        size="small"
        style={{ marginBottom: 12 }}
      >
        <Descriptions column={2} size="small">
          <Descriptions.Item label={t("evalLabColGate")}>
            <Tag color={gateColor(run.gate_result)}>{gateLabel(run.gate_result)}</Tag>
          </Descriptions.Item>
          <Descriptions.Item label={t("evalLabRejectionReasons")}>
            {run.rejection_reasons && run.rejection_reasons.length > 0 ? (
              <Space direction="vertical" size={2}>
                {run.rejection_reasons.map((reason, idx) => (
                  <Tag key={idx} color="red">
                    {rejectionReasonLabel(reason)}
                  </Tag>
                ))}
              </Space>
            ) : (
              <Typography.Text type="secondary">{t("evalLabNoRejectionReasons")}</Typography.Text>
            )}
          </Descriptions.Item>
        </Descriptions>
      </Card>

      {/* 基础指标 */}
      <Card
        type="inner"
        title={t("evalLabSectionMetrics")}
        size="small"
        style={{ marginBottom: 12 }}
      >
        {!hasMetrics ? (
          <Empty description={t("evalLabNoMetrics")} />
        ) : (
          <>
            <Descriptions column={2} size="small" bordered>
              <Descriptions.Item label={<MetricLabel label={t("evalLabMetricRankIc")} hint="rankIc" />}>
                <SafeMetric value={m.rank_ic_mean} precision={4} />
              </Descriptions.Item>
              <Descriptions.Item label={<MetricLabel label={t("evalLabMetricRankIcMedian")} hint="rankIcMedian" />}>
                <SafeMetric value={m.rank_ic_median} precision={4} />
              </Descriptions.Item>
              <Descriptions.Item label={<MetricLabel label={t("evalLabMetricRankIcStd")} hint="rankIcStd" />}>
                <SafeMetric value={m.rank_ic_std} precision={4} />
              </Descriptions.Item>
              <Descriptions.Item label={<MetricLabel label={t("evalLabMetricIcir")} hint="icir" />}>
                <SafeMetric value={m.icir} precision={4} />
              </Descriptions.Item>
              <Descriptions.Item label={<MetricLabel label={t("evalLabMetricPositiveRatio")} hint="positiveRatio" />}>
                <SafeMetric
                  value={
                    typeof m.positive_ic_ratio === "number"
                      ? m.positive_ic_ratio * 100
                      : (m.positive_ic_ratio as number | null | undefined)
                  }
                  precision={2}
                  suffix="%"
                />
              </Descriptions.Item>
              <Descriptions.Item label={<MetricLabel label={t("evalLabMetricCoverage")} hint="coverage" />}>
                <SafeMetric
                  value={
                    typeof m.coverage === "number"
                      ? m.coverage * 100
                      : (m.coverage as number | null | undefined)
                  }
                  precision={2}
                  suffix="%"
                />
              </Descriptions.Item>
              <Descriptions.Item label={<MetricLabel label={t("evalLabMetricSamples")} hint="samples" />}>
                <SafeMetric value={m.n_samples} precision={0} />
              </Descriptions.Item>
              <Descriptions.Item label={<MetricLabel label={t("evalLabMetricMonotonicity")} hint="monotonicity" />}>
                <SafeMetric value={m.monotonicity_score} precision={4} />
              </Descriptions.Item>
              <Descriptions.Item label={<MetricLabel label={t("evalLabMetricLongShort")} hint="longShort" />}>
                <SafeMetric value={m.long_short_return} precision={4} />
              </Descriptions.Item>
              <Descriptions.Item label={<MetricLabel label={t("evalLabMetricTurnover")} hint="turnover" />}>
                <SafeMetric value={m.turnover} precision={4} />
              </Descriptions.Item>
              <Descriptions.Item label={<MetricLabel label={t("evalLabMetricCostAdjusted")} hint="costAdjusted" />}>
                <SafeMetric value={m.cost_adjusted_return} precision={4} />
              </Descriptions.Item>
            </Descriptions>
            {quantileReturns.length > 0 ? (
              <div style={{ marginTop: 8 }}>
                <Typography.Text strong style={{ fontSize: 13 }}>
                  {t("evalLabMetricQuantileReturns")}：
                </Typography.Text>
                <Space size={4} wrap style={{ marginTop: 4 }}>
                  {quantileReturns.map((ret, idx) => (
                    <Tag key={idx} color="blue">
                      Q{idx + 1}: <SafeMetric value={ret} precision={4} />
                    </Tag>
                  ))}
                </Space>
              </div>
            ) : null}
            <Descriptions column={2} size="small" style={{ marginTop: 8 }}>
              <Descriptions.Item label={t("evalLabMetricTrainRange")}>
                {formatDateRange(m.train_start, m.train_end)}
              </Descriptions.Item>
              <Descriptions.Item label={t("evalLabMetricValidationRange")}>
                {formatDateRange(m.validation_start, m.validation_end)}
              </Descriptions.Item>
            </Descriptions>
          </>
        )}
      </Card>

      {/* 压力测试 */}
      <Card
        type="inner"
        title={t("evalLabSectionStress")}
        size="small"
        style={{ marginBottom: 12 }}
      >
        {!stress ? (
          <Empty description={t("evalLabNoStress")} />
        ) : (
          <>
            <Descriptions column={1} size="small" bordered style={{ marginBottom: 8 }}>
              <Descriptions.Item label={t("evalLabStressOverall")}>
                <Tag color={verdictColor(stress.overall_verdict)}>
                  {verdictLabel(stress.overall_verdict)}
                </Tag>
              </Descriptions.Item>
              {stress.failure_reasons && stress.failure_reasons.length > 0 ? (
                <Descriptions.Item label={t("evalLabStressFailureReasons")}>
                  <Space direction="vertical" size={2}>
                    {stress.failure_reasons.map((reason, idx) => (
                      <Tag key={idx} color="red">
                        {stressFailureReasonLabel(reason)}
                      </Tag>
                    ))}
                  </Space>
                </Descriptions.Item>
              ) : null}
            </Descriptions>

            {/* 参数扰动 */}
            <Typography.Text strong style={{ display: "block", margin: "8px 0 4px" }}>
              {t("evalLabStressParameter")}
            </Typography.Text>
            {stress.parameter_results && stress.parameter_results.length > 0 ? (
              <Table
                rowKey="param_name"
                dataSource={stress.parameter_results}
                size="small"
                pagination={false}
                scroll={{ x: 600 }}
                columns={[
                  {
                    title: t("evalLabStressColParam"),
                    dataIndex: "param_name",
                    key: "param_name",
                  },
                  {
                    title: t("evalLabStressColBaseline"),
                    dataIndex: "baseline_value",
                    key: "baseline_value",
                    render: (v: number) => formatNumber(v, 2),
                  },
                  {
                    title: t("evalLabStressColVerdict"),
                    dataIndex: "verdict",
                    key: "verdict",
                    render: (v: string) => <Tag color={verdictColor(v)}>{verdictLabel(v)}</Tag>,
                  },
                  {
                    title: t("evalLabStressColSignRatio"),
                    dataIndex: "sign_consistency_ratio",
                    key: "sign_consistency_ratio",
                    render: (v: number) => formatPercent(v),
                  },
                  {
                    title: t("evalLabStressColMedianRatio"),
                    dataIndex: "median_ic_ratio",
                    key: "median_ic_ratio",
                    render: (v: number) => formatNumber(v),
                  },
                  {
                    title: t("evalLabStressColPassing"),
                    dataIndex: "passing_neighbor_count",
                    key: "passing_neighbor_count",
                  },
                  {
                    title: t("evalLabStressColCliff"),
                    dataIndex: "has_cliff_drop",
                    key: "has_cliff_drop",
                    render: (v: boolean) => (v ? t("evalLabStressYes") : t("evalLabStressNo")),
                  },
                ]}
                expandable={{
                  expandedRowRender: (record: ParameterPerturbationResult) => (
                    <Table
                      rowKey="label"
                      dataSource={record.points || []}
                      size="small"
                      pagination={false}
                      columns={[
                        { title: t("evalLabStressColLabel"), dataIndex: "label", key: "label" },
                        {
                          title: t("evalLabStressColValue"),
                          dataIndex: "param_value",
                          key: "param_value",
                          render: (v: number) => formatNumber(v, 2),
                        },
                        {
                          title: t("evalLabStressColIcMean"),
                          dataIndex: "ic_mean",
                          key: "ic_mean",
                          render: (v: number) => formatNumber(v),
                        },
                        {
                          title: t("evalLabStressColIcir"),
                          dataIndex: "icir",
                          key: "icir",
                          render: (v: number) => formatNumber(v),
                        },
                        {
                          title: t("evalLabStressColPassed"),
                          dataIndex: "passed_min_gate",
                          key: "passed_min_gate",
                          render: (v: boolean) =>
                            v ? (
                              <Tag color="green">{t("evalLabStressYes")}</Tag>
                            ) : (
                              <Tag color="red">{t("evalLabStressNo")}</Tag>
                            ),
                        },
                      ]}
                    />
                  ),
                }}
              />
            ) : (
              <Empty description={t("evalLabNoParameterResults")} />
            )}

            {/* 时间段稳定性 */}
            {stress.time_result ? (
              <>
                <Typography.Text strong style={{ display: "block", margin: "12px 0 4px" }}>
                  {t("evalLabStressTime")}
                </Typography.Text>
                <Descriptions column={2} size="small" bordered>
                  <Descriptions.Item label={t("evalLabStressColVerdict")}>
                    <Tag color={verdictColor(stress.time_result.verdict)}>
                      {verdictLabel(stress.time_result.verdict)}
                    </Tag>
                  </Descriptions.Item>
                  <Descriptions.Item label={t("evalLabStressIcStability")}>
                    {formatNumber(stress.time_result.ic_stability)}
                  </Descriptions.Item>
                </Descriptions>
                {stress.time_result.segments && stress.time_result.segments.length > 0 ? (
                  <Table
                    rowKey="segment_label"
                    dataSource={stress.time_result.segments}
                    size="small"
                    pagination={false}
                    style={{ marginTop: 8 }}
                    columns={[
                      {
                        title: t("evalLabStressColSegment"),
                        dataIndex: "segment_label",
                        key: "segment_label",
                      },
                      {
                        title: t("evalLabStressColIcMean"),
                        dataIndex: "ic_mean",
                        key: "ic_mean",
                        render: (v: number) => formatNumber(v),
                      },
                      {
                        title: t("evalLabStressColIcir"),
                        dataIndex: "icir",
                        key: "icir",
                        render: (v: number) => formatNumber(v),
                      },
                    ]}
                  />
                ) : null}
              </>
            ) : null}

            {/* 缺失敏感度 */}
            {stress.missing_result ? (
              <>
                <Typography.Text strong style={{ display: "block", margin: "12px 0 4px" }}>
                  {t("evalLabStressMissing")}
                </Typography.Text>
                <Descriptions column={2} size="small" bordered>
                  <Descriptions.Item label={t("evalLabStressColVerdict")}>
                    <Tag color={verdictColor(stress.missing_result.verdict)}>
                      {verdictLabel(stress.missing_result.verdict)}
                    </Tag>
                  </Descriptions.Item>
                  <Descriptions.Item label={t("evalLabStressIcDecayRatio")}>
                    {formatNumber(stress.missing_result.ic_decay_ratio)}
                  </Descriptions.Item>
                </Descriptions>
              </>
            ) : null}
          </>
        )}
      </Card>

      {/* 完整交易日证据 */}
      <Card
        type="inner"
        title={t("evalLabSectionEvidence")}
        size="small"
        style={{ marginBottom: 12 }}
      >
        <Descriptions column={2} size="small" bordered>
          <Descriptions.Item label={t("evalLabEvidenceSelectedDate")}>
            {run.selected_trade_date || "-"}
          </Descriptions.Item>
          <Descriptions.Item label={t("evalLabEvidenceObserved")}>
            {run.observed_symbols ?? "-"}
          </Descriptions.Item>
          <Descriptions.Item label={t("evalLabEvidenceExpected")}>
            {run.expected_symbols ?? "-"}
          </Descriptions.Item>
          <Descriptions.Item label={t("evalLabEvidenceRatio")}>
            {run.completeness_ratio != null
              ? formatPercent(run.completeness_ratio)
              : "-"}
          </Descriptions.Item>
          <Descriptions.Item label={t("evalLabEvidenceFallback")} span={2}>
            {run.fallback_reason || t("evalLabNoRejectionReasons")}
          </Descriptions.Item>
        </Descriptions>
      </Card>

      {/* 评估配置 */}
      <Card
        type="inner"
        title={t("evalLabSectionConfig")}
        size="small"
      >
        <Descriptions column={2} size="small" bordered>
          <Descriptions.Item label={t("evalLabColVersion")}>
            {run.factor_version_id}
          </Descriptions.Item>
          <Descriptions.Item label={t("evalLabTargetCode")}>
            {targetCodeLabel(run.target_code)}
          </Descriptions.Item>
          <Descriptions.Item label={t("evalLabConfigFactorKind")}>
            {factorKindLabel(run.config?.factor_kind)}
          </Descriptions.Item>
          <Descriptions.Item label={t("evalLabConfigTargetHorizon")}>
            {String(run.config?.target_horizon ?? "-")}
          </Descriptions.Item>
          <Descriptions.Item label={t("evalLabConfigNGroups")}>
            {String(run.config?.n_groups ?? "-")}
          </Descriptions.Item>
          <Descriptions.Item label={t("evalLabConfigCostRate")}>
            {formatNumber(run.config?.cost_rate, 6)}
          </Descriptions.Item>
          <Descriptions.Item label={t("evalLabConfigEvaluatorVersion")}>
            {String(run.config?.evaluator_version ?? "-")}
          </Descriptions.Item>
          <Descriptions.Item label={t("evalLabColCutoff")}>
            {formatDateTime(run.data_cutoff_at)}
          </Descriptions.Item>
          <Descriptions.Item label={t("evalLabMetricTrainRange")}>
            {formatDateRange(run.train_start_date, run.train_end_date)}
          </Descriptions.Item>
          <Descriptions.Item label={t("evalLabMetricValidationRange")}>
            {formatDateRange(run.validation_start_date, run.validation_end_date)}
          </Descriptions.Item>
          <Descriptions.Item label={t("evalLabTaskIdLabel")}>
            {run.task_id ? (
              <Tooltip title={run.task_id}>
                <span>{shortId(run.task_id)}</span>
              </Tooltip>
            ) : (
              "-"
            )}
          </Descriptions.Item>
        </Descriptions>
      </Card>

      {/* ========== Task 20: 过滤与数据治理（BFG） ========== */}
      <Divider orientation="left" plain style={{ margin: "4px 0 12px" }}>
        过滤与数据治理
      </Divider>
      <Row gutter={[16, 16]} style={{ marginBottom: 12 }}>
        <Col xs={24} md={12}>
          <Card size="small" title="四项规则开关状态">
            <Space wrap size={[6, 6]}>
              {ruleRows.map((r) => (
                <Tag
                  key={r.k}
                  color={r.on ? "green" : "red"}
                  style={{
                    textDecoration: r.on ? "none" : "line-through",
                    margin: 0,
                  }}
                >
                  {r.label} {r.on ? "✓" : "✗ 关"}
                </Tag>
              ))}
            </Space>
            <Divider style={{ margin: "12px 0" }} />
            <Tag color={fidelityBool ? "green" : "gold"}>
              生产保真：{fidelityBool ? "TRUE" : "非保真实验"}
            </Tag>
            {!fidelityBool && nonFidelityReason ? (
              <Tooltip title={nonFidelityReason}>
                <Tag color="orange">
                  原因：
                  {nonFidelityReason.length > 30
                    ? nonFidelityReason.slice(0, 30) + "…"
                    : nonFidelityReason}
                </Tag>
              </Tooltip>
            ) : null}
          </Card>
        </Col>
        <Col xs={24} md={12}>
          <Card
            size="small"
            title="排除计数（股票日）"
            extra={
              <Button
                type="link"
                size="small"
                onClick={() => setAuditOpen(true)}
                disabled={auditEvents.length === 0}
              >
                查看详情
              </Button>
            }
          >
            <Row gutter={[8, 8]}>
              <Col span={8}>
                <Statistic title="次新股" value={excluded.new_listing} />
              </Col>
              <Col span={8}>
                <Statistic title="ST" value={excluded.st} />
              </Col>
              <Col span={8}>
                <Statistic title="停牌" value={excluded.suspended} />
              </Col>
              <Col span={8}>
                <Statistic title="退市整理期" value={excluded.delisting_period} />
              </Col>
              <Col span={8}>
                <Statistic title="状态未知" value={excluded.status_unknown} />
              </Col>
              <Col span={8}>
                <Statistic title="价量异常" value={excluded.price_or_volume_invalid} />
              </Col>
            </Row>
            <Divider style={{ margin: "12px 0" }} />
            <Space direction="vertical" size="small" style={{ width: "100%" }}>
              <div style={{ fontSize: 13 }}>
                raw_count / filtered_count / effective_count：
                <b style={{ marginLeft: 6 }}>
                  {rawCount == null ? "—" : rawCount}
                </b>
                {" / "}
                <b>{filteredCount == null ? "—" : filteredCount}</b>
                {" / "}
                <b>{effectiveCount == null ? "—" : effectiveCount}</b>
              </div>
              <div>
                <Tag>
                  数据截止日：
                  {dataCutoff
                    ? String(dataCutoff).slice(0, 10)
                    : "—"}
                </Tag>
                <Tag>目标标签批次：{targetBatchId || "—"}</Tag>
              </div>
            </Space>
          </Card>
        </Col>
      </Row>

      {/* 审计事件 Modal */}
      <Modal
        title="过滤审计事件（前 100 行）"
        open={auditOpen}
        onCancel={() => setAuditOpen(false)}
        footer={null}
        width={900}
        destroyOnHidden
      >
        {auditEvents.length === 0 ? (
          <Empty description="暂未提供审计事件（后端可能尚未启用 BFG 过滤审计输出）" />
        ) : (
          <Table<AuditEventRow>
            size="small"
            dataSource={auditEvents.slice(0, 100)}
            rowKey={(r, i) =>
              `${r.symbol_id}-${r.trade_date}-${r.rule_code}-${i}`
            }
            pagination={false}
            scroll={{ x: 720 }}
            columns={[
              { title: "日期", dataIndex: "trade_date", width: 120 },
              { title: "symbol_id", dataIndex: "symbol_id", width: 100 },
              { title: "规则", dataIndex: "rule_code", width: 220 },
              { title: "原因", dataIndex: "reason" },
            ]}
          />
        )}
      </Modal>
    </Card>
  );
}

function formatDateRange(start: string | null | undefined, end: string | null | undefined): string {
  if (!start && !end) return "-";
  const s = start ? start.slice(0, 10) : "?";
  const e = end ? end.slice(0, 10) : "?";
  return `${s} ~ ${e}`;
}
