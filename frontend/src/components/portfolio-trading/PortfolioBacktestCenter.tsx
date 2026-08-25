import React, { useCallback, useEffect, useState } from "react";
import { ArrowDownAZ, ArrowUpAZ, History, Play, RotateCcw, X, Calendar, CheckCircle2, Search, FileText, Loader2, AlertTriangle } from "lucide-react";
import { DatePicker, App as AntApp, Modal } from "antd";
import dayjs, { type Dayjs } from "dayjs";
import localeData from "dayjs/plugin/localeData";
import weekday from "dayjs/plugin/weekday";
import {
  api,
  type BacktestPositionLedgerMode,
  type DecisionRunRead,
  type DecisionEvidenceRead,
} from "../../api/client";
import { t } from "../../i18n";
import { useApp } from "../../context/AppContext";
import { percent } from "../../utils/format";
import type { BacktestPosition, BacktestRun, BacktestTrade } from "../../types";
// WP1-1: 证据与归因抽屉
import DecisionEvidenceDrawer from "./DecisionEvidenceDrawer";

// Ant Design's Day.js date adapter calls weekday() and localeData() when its calendar opens.
dayjs.extend(localeData);
dayjs.extend(weekday);

/**
 * PortfolioBacktestCenter — 回测中心子 Tab（Task 7）
 *
 * 一比一还原原型图「应用主框架.html」回测中心区域：
 *   1. 顶部模式切换（组合全局回测 / 单股/指标回测）+ 最近回测时间 + 回测历史按钮
 *   2. 组合全局回测面板：左参数配置 + 右 4 指标卡 / 累计净值曲线 / 水下回撤 / 回测明细
 *   3. 单股/指标回测面板：左单股配置 + 右 4 指标卡 / 股价与指标信号图 / 交易明细
 *   4. 回测历史抽屉：360px 右侧抽屉，最近 10 条回测记录
 *
 * 数据/API（参考 PortfolioBacktestPanel.tsx 的调用方式）：
 *   - GET  /portfolios/{id}/backtest/source-status → api.getPortfolioBacktestSourceStatus（来源标签，best-effort）
 *   - POST /backtest/portfolio/run                 → api.runPortfolioBacktest（按当前组合候选池/成员/持仓开始回测）
 *   - GET  /backtest/runs?portfolio_id={id}        → api.getBacktestRuns（回测历史列表）
 *   - GET  /backtest/runs/{runId}                  → api.getBacktestRun（加载单条历史回测）
 *
 * i18n：使用 t('portfolioTrading.backtest.xxx')，key 由 Task 13 补全，缺失时返回 key 字符串不阻塞。
 */
interface PortfolioBacktestCenterProps {
  portfolioId: number;
  onNavigate?: (tab: string) => void;
  /** P1-FIX: 自动交易关闭时，开始回测按钮禁用并提示需先开启自动接管 */
  autoTradeEnabled: boolean;
}

type BacktestMode = "portfolio" | "single";

interface EvidenceTarget {
  decisionRunId: string;
  evidenceId: string;
  symbolId: number;
}

interface SourceStatus {
  enabled: boolean;
  env_flag: string;
  source_label: string;
}

/**
 * 回测结果门禁状态。门禁状态必须与结果区同生命周期，不能只通过 toast
 * 短暂展示，否则用户无法判断当前页面是否仍代表一次可执行回测。
 */
interface BacktestGateState {
  kind: "blocked" | "failed";
  code?: string;
  statusCode?: number;
  message: string;
  details?: unknown;
  correlationId?: string;
}

interface CompareMetrics {
  total_return: number | null;
  total_return_pct: number | null;
  max_drawdown: number | null;
  max_drawdown_pct: number | null;
  sharpe_ratio: number | null;
  win_rate: number | null;
  profit_factor: number | null;
  avg_holding_days: number | null;
  trade_count: number;
}

interface CompareResult {
  new: {
    run_id: number;
    symbol_ids: number[];
    source_type: string;
    metrics: CompareMetrics;
  };
  old: {
    run_id: number;
    symbol_ids: number[];
    source_type: string;
    metrics: CompareMetrics;
  };
}

/** 组合回测 POST /backtest/portfolio/run 返回的轻量摘要（后端 PortfolioBacktestResult）。 */
interface EquityCurvePoint {
  date: string;
  equity: number;
  cash?: number;
  position_value?: number;
  benchmark?: number;
}
interface PortfolioBacktestLatestResult {
  run_id: number;
  portfolio_id: number;
  symbol_ids: number[];
  symbol_count: number;
  start_date: string;
  end_date: string;
  initial_capital: number;
  status: string;
  run_name: string;
  total_return: number | null;
  total_return_pct: number | null;
  max_drawdown: number | null;
  max_drawdown_pct: number | null;
  sharpe_ratio: number | null;
  win_rate: number | null;
  profit_factor: number | null;
  trade_count: number | null;
  avg_holding_days: number | null;
  equity_curve: EquityCurvePoint[];
  metrics: Record<string, any>;
  diagnostics: Record<string, any>;
  warnings: any[];
  errors: any[];
  decision_run_ids?: string[];
  evidence_summary?: Record<string, any>;
  rejected_count?: number;
  decision_snapshot?: Record<string, any> | null;
  /** 执行参数回显（BT-UI-20；旧后端可能不返回）。 */
  commission_rate?: number | null;
  stamp_tax_rate?: number | null;
  slippage_bps?: number | null;
  price_type?: string | null;
  volume_limit_pct?: number | null;
  rebalance_frequency?: string | null;
  pit_mode?: string | null;
}

function asRecord(value: unknown): Record<string, any> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, any>
    : null;
}

function displayGateValue(value: unknown): string {
  if (value == null || value === "") return "";
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function isSuccessfulBacktestStatus(status: unknown): boolean {
  const normalized = String(status ?? "").trim().toLowerCase();
  return ["completed", "complete", "success", "succeeded", "done"].includes(normalized);
}

function issueCode(issue: unknown): string | undefined {
  const record = asRecord(issue);
  const value = record?.code ?? record?.error_code ?? record?.reason_code ?? record?.type;
  return value == null || value === "" ? undefined : String(value);
}

function issueMessage(issue: unknown): string | undefined {
  if (typeof issue === "string" && issue.trim()) return issue.trim();
  const record = asRecord(issue);
  const value = record?.user_message ?? record?.message ?? record?.reason ?? record?.summary ?? record?.detail;
  return value == null || value === "" ? undefined : displayGateValue(value);
}

function isBlockingWarning(issue: unknown): boolean {
  const record = asRecord(issue);
  if (!record) return false;
  if (record.blocking === true || record.is_blocking === true || record.blocks_result === true) return true;
  const severity = String(record.severity ?? record.level ?? "").toLowerCase();
  if (["error", "critical", "block", "blocked", "fatal"].includes(severity)) return true;
  const code = issueCode(issue)?.toUpperCase() ?? "";
  return /^(BACKTEST_|DATA_|PIT_|GATE_)/.test(code);
}

/** 将摘要中的失败/阻断字段规范化为结果区可持久展示的状态。 */
function gateFromBacktestSummary(summary: Record<string, any>): BacktestGateState | null {
  const errors = Array.isArray(summary.errors) ? summary.errors : [];
  const blockingWarnings = (Array.isArray(summary.warnings) ? summary.warnings : []).filter(isBlockingWarning);
  const blockingStatus = String(summary.blocking_status ?? summary.blockingStatus ?? "").toUpperCase();
  const status = String(summary.status ?? "");
  const failedStatus = status !== "" && !isSuccessfulBacktestStatus(status);
  const blockedStatus = [
    "BLOCKED",
    "DATA_BLOCKED",
    "REJECTED",
    "DATA_INCOMPLETE_PAUSED",
    "RECONCILIATION_BLOCKED",
    "MODEL_INACTIVE",
    "SCORE_STALE",
  ].includes(blockingStatus);
  if (!errors.length && !blockingWarnings.length && !failedStatus && !blockedStatus) return null;

  const firstIssue = errors[0] ?? blockingWarnings[0];
  const code = issueCode(firstIssue)
    ?? (blockedStatus ? blockingStatus : failedStatus ? `BACKTEST_${status.toUpperCase()}` : undefined);
  const message = issueMessage(firstIssue)
    ?? (blockedStatus ? "回测所需数据未满足执行门禁" : failedStatus ? `回测状态：${status}` : "回测未生成可用结果");
  const statusCode = Number(summary.status_code ?? summary.statusCode);
  const kind: BacktestGateState["kind"] =
    blockedStatus || /^BACKTEST_(?:DATA|SCORE|MARKET|PIT|GATE)/i.test(code ?? "") || /DATA|COVERAGE|MISSING|GATE|PIT/i.test(code ?? "")
      ? "blocked"
      : "failed";
  return {
    kind,
    ...(code ? { code } : {}),
    ...(Number.isFinite(statusCode) && statusCode > 0 ? { statusCode } : {}),
    message,
    details: errors.length || blockingWarnings.length
      ? { errors, warnings: blockingWarnings }
      : summary.diagnostics ?? undefined,
    ...(summary.correlation_id ? { correlationId: String(summary.correlation_id) } : {}),
  };
}

/** 将统一错误协议/旧式 HTTPException 统一成 UI 门禁状态。 */
function gateFromBacktestError(error: any): BacktestGateState {
  const detail = asRecord(error?.detail);
  const nestedDetail = asRecord(detail?.detail);
  const code = error?.error_code ?? detail?.error_code ?? detail?.code ?? nestedDetail?.code;
  const statusCodeRaw = error?.status_code ?? detail?.status_code ?? detail?.statusCode;
  const statusCode = Number(statusCodeRaw);
  const message = error?.user_message
    ?? detail?.user_message
    ?? detail?.message
    ?? nestedDetail?.message
    ?? error?.message
    ?? "回测执行失败";
  const details = detail?.technical_details
    ?? detail?.details
    ?? detail?.issues
    ?? detail?.errors
    ?? (detail && Object.keys(detail).length ? detail : undefined);
  const normalizedCode = code == null || code === "" ? undefined : String(code);
  const blocked = [400, 409, 422].includes(statusCode)
    || /^BACKTEST_/i.test(normalizedCode ?? "")
    || /(?:DATA|COVERAGE|MISSING|GATE|PIT|POSITION|CANDIDATE)/i.test(normalizedCode ?? "");
  return {
    kind: blocked ? "blocked" : "failed",
    ...(normalizedCode ? { code: normalizedCode } : {}),
    ...(Number.isFinite(statusCode) && statusCode > 0 ? { statusCode } : {}),
    message: String(message),
    ...(details !== undefined ? { details } : {}),
    ...(error?.correlation_id || detail?.correlation_id
      ? { correlationId: String(error?.correlation_id ?? detail?.correlation_id) }
      : {}),
  };
}

function gateFromHistoricalBacktest(run: BacktestRun): BacktestGateState | null {
  const raw = run as BacktestRun & {
    errors?: unknown[];
    warnings?: unknown[];
    blocking_status?: string;
    decision_snapshot?: Record<string, any> | null;
  };
  return gateFromBacktestSummary({
    ...raw,
    errors: Array.isArray(raw.errors)
      ? raw.errors
      : raw.error_message
        ? [{ message: raw.error_message, code: "BACKTEST_RUN_FAILED" }]
        : [],
    warnings: Array.isArray(raw.warnings) ? raw.warnings : [],
  });
}

/** 历史运行只返回持久化 JSON，解析失败时维持真实空态而不是绘制占位曲线。 */
function parseStoredEquityCurve(raw: string | null | undefined): EquityCurvePoint[] {
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed as EquityCurvePoint[] : [];
  } catch {
    return [];
  }
}

/** 指标卡数据来源：优先 latestResult（新 POST 返回）→ loadedRun（历史）→ compareResult（对比）→ null 占位 */
interface MetricSource {
  totalReturnPct: number | null;
  maxDrawdownPct: number | null;
  sharpe: number | null;
  winRate: number | null;
  profitFactor: number | null;
  tradeCount: number | null;
  avgHoldingDays: number | null;
  annualReturnPct: number | null;
  annualVolatility: number | null;
  drawdownDays: number | null;
  turnoverRate: number | null;
}

type ResultTabKey = "overview" | "trades" | "positions" | "rejected" | "data";
type TradeActionFilter = "" | "BUY" | "SELL";
type TradeExecutionFilter = "" | "filled" | "open" | "rejected";
type TradeSortField = "signal_at" | "execution_at" | "symbol_id" | "price" | "quantity" | "cost";
type TradeSortDirection = "asc" | "desc";

interface TradeLedgerUrlState {
  tab: ResultTabKey;
  page: number;
  action: TradeActionFilter;
  status: TradeExecutionFilter;
  symbol: string;
  sortBy: TradeSortField;
  sortDir: TradeSortDirection;
  positionPage: number;
  positionStatus: "" | "OPEN" | "CLOSED";
  positionAsOfDate: string;
}

/** Read a persisted run id before the result panel mounts. */
function readBacktestRunIdFromUrl(): number | null {
  if (typeof window === "undefined") return null;
  const raw = new URLSearchParams(window.location.search).get("bt_run");
  const value = Number(raw);
  return Number.isInteger(value) && value > 0 ? value : null;
}

function readTradeLedgerUrlState(): TradeLedgerUrlState {
  const fallback: TradeLedgerUrlState = {
    tab: "overview",
    page: 1,
    action: "",
    status: "",
    symbol: "",
    sortBy: "signal_at",
    sortDir: "desc",
    positionPage: 1,
    positionStatus: "",
    positionAsOfDate: "",
  };
  if (typeof window === "undefined") return fallback;
  const query = new URLSearchParams(window.location.search);
  const tab = query.get("bt_tab");
  const action = query.get("bt_action");
  const status = query.get("bt_status");
  const sortBy = query.get("bt_sort");
  const sortDir = query.get("bt_dir");
  const page = Number(query.get("bt_page"));
  const positionPage = Number(query.get("bt_position_page"));
  const positionStatus = query.get("bt_position_status");
  const positionAsOfDate = query.get("bt_position_as_of") || "";
  return {
    tab: ["overview", "trades", "positions", "rejected", "data"].includes(tab || "")
      ? tab as ResultTabKey
      : fallback.tab,
    page: Number.isInteger(page) && page > 0 ? page : fallback.page,
    action: action === "BUY" || action === "SELL" ? action : fallback.action,
    status: status === "filled" || status === "open" || status === "rejected" ? status : fallback.status,
    symbol: /^\d+$/.test(query.get("bt_symbol") || "") ? query.get("bt_symbol") || "" : "",
    sortBy: ["signal_at", "execution_at", "symbol_id", "price", "quantity", "cost"].includes(sortBy || "")
      ? sortBy as TradeSortField
      : fallback.sortBy,
    sortDir: sortDir === "asc" || sortDir === "desc" ? sortDir : fallback.sortDir,
    positionPage: Number.isInteger(positionPage) && positionPage > 0 ? positionPage : fallback.positionPage,
    positionStatus: positionStatus === "OPEN" || positionStatus === "CLOSED" ? positionStatus : fallback.positionStatus,
    positionAsOfDate: /^\d{4}-\d{2}-\d{2}$/.test(positionAsOfDate) ? positionAsOfDate : fallback.positionAsOfDate,
  };
}

const BENCHMARKS = ["沪深300", "中证500", "创业板指", "上证50", "科创50"];
const INDICATORS = ["MA", "MACD", "RSI", "布林带", "KDJ"];

const sectionTitleStyle: React.CSSProperties = {
  fontSize: 14,
  fontWeight: 600,
  color: "var(--pt-foreground)",
  margin: 0,
};
const sectionSubStyle: React.CSSProperties = {
  fontSize: 12,
  color: "var(--pt-muted-foreground)",
  margin: "2px 0 0 0",
};
const fieldLabelStyle: React.CSSProperties = {
  fontSize: 12,
  color: "var(--pt-muted-foreground)",
};
const fullInputStyle: React.CSSProperties = { width: "100%" };

type BacktestRangePreset = "1y" | "3y" | "5y";

const BACKTEST_RANGE_PRESETS: Array<{ key: BacktestRangePreset; label: string; years: number }> = [
  { key: "1y", label: "近一年", years: 1 },
  { key: "3y", label: "近三年", years: 3 },
  { key: "5y", label: "近五年", years: 5 },
];

function getBacktestDateRange(years: number): [string, string] {
  const end = dayjs().startOf("day");
  return [end.subtract(years, "year").format("YYYY-MM-DD"), end.format("YYYY-MM-DD")];
}

const DEFAULT_BACKTEST_RANGE = getBacktestDateRange(1);

/** 收益正绿负红，0/缺失 muted */
function pnlColor(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(Number(v))) return "var(--pt-muted-foreground)";
  return Number(v) > 0
    ? "var(--pt-state-success)"
    : Number(v) < 0
      ? "var(--pt-state-error)"
      : "var(--pt-muted-foreground)";
}

/** 百分比格式化（带正号），缺失显示 --% */
function fmtPct(v: number | null | undefined, withSign = false): string {
  if (v == null || !Number.isFinite(Number(v))) return "--%";
  const s = percent(v);
  return withSign && Number(v) > 0 ? "+" + s : s;
}

/** 数字格式化，缺失显示 -- */
function fmtNum(v: number | null | undefined, digits = 2): string {
  if (v == null || !Number.isFinite(Number(v))) return "--";
  return Number(v).toFixed(digits);
}

function fmtMoney(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(Number(v))) return "--";
  return Number(v).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function fmtEvidenceIds(ids: string[] | null | undefined): string {
  const values = (ids || []).filter((id) => Boolean(id));
  return values.length > 0 ? values.join(", ") : "--";
}

function contractRecord(value: unknown): Record<string, any> {
  if (value && typeof value === "object" && !Array.isArray(value)) return value as Record<string, any>;
  if (typeof value === "string") {
    try {
      return contractRecord(JSON.parse(value));
    } catch {
      return {};
    }
  }
  return {};
}

function contractValue(record: Record<string, any>, ...keys: string[]): unknown {
  for (const key of keys) {
    const value = record[key];
    if (value !== undefined && value !== null && value !== "") return value;
  }
  return null;
}

function contractText(value: unknown): string {
  if (value == null || value === "") return "--";
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return String(value);
  return displayGateValue(value);
}

const PortfolioBacktestCenter: React.FC<PortfolioBacktestCenterProps> = ({ portfolioId, autoTradeEnabled }) => {
  const { showToast, setActiveTab } = useApp();

  const openHistoryRepair = useCallback((repairMode: "scores" | "bars") => {
    // Settings reads this on mount, so the link can open the exact repair section
    // even though the settings panel owns its internal tab state.
    try {
      window.localStorage.setItem("settings_active_section", "history");
    } catch {
      // Ignore storage restrictions; the settings tab is still useful.
    }
    setActiveTab("settings");
    window.setTimeout(() => {
      document.querySelector('[data-settings-content="settings-history"]')?.scrollIntoView({
        behavior: "smooth",
        block: "start",
      });
    }, 50);
    void repairMode;
  }, [setActiveTab]);

  /** P1-FIX: 校验日期区间合法性：空非法，start > end 非法。YYYY-MM-DD 字符串可直接字典序比较。 */
  const validateDateRange = (start: string, end: string): boolean => {
    if (!start || !end) {
      showToast("info", t("portfolioTrading.backtest.rangeRequired"));
      return false;
    }
    if (start > end) {
      showToast("error", t("portfolioTrading.backtest.rangeInvalidOrder").replace("{start}", start).replace("{end}", end));
      return false;
    }
    return true;
  };

  // ---------- 顶部模式 + 抽屉 ----------
  const [mode, setMode] = useState<BacktestMode>("portfolio");
  const [historyOpen, setHistoryOpen] = useState(false);

  // ---------- 来源状态 + 最近回测时间 ----------
  const [sourceStatus, setSourceStatus] = useState<SourceStatus | null>(null);
  const [lastBacktestTime, setLastBacktestTime] = useState<string | null>(null);

  // ---------- 组合回测表单 ----------
  const [portStart, setPortStart] = useState(DEFAULT_BACKTEST_RANGE[0]);
  const [portEnd, setPortEnd] = useState(DEFAULT_BACKTEST_RANGE[1]);
  const [portRangePreset, setPortRangePreset] = useState<BacktestRangePreset | null>("1y");
  const [portCapital, setPortCapital] = useState("1000000");
  const [portBenchmark, setPortBenchmark] = useState(BENCHMARKS[0]);
  const [portCommission, setPortCommission] = useState("0.03");

  // ---------- 单股回测表单 ----------
  const [singleSymbol, setSingleSymbol] = useState("");
  const [singleStart, setSingleStart] = useState(DEFAULT_BACKTEST_RANGE[0]);
  const [singleEnd, setSingleEnd] = useState(DEFAULT_BACKTEST_RANGE[1]);
  const [singleRangePreset, setSingleRangePreset] = useState<BacktestRangePreset | null>("1y");
  const [singleIndicator, setSingleIndicator] = useState(INDICATORS[1]);
  const [singleCapital, setSingleCapital] = useState("100000");
  const [singleCommission, setSingleCommission] = useState("0.03");

  // ---------- 回测结果 ----------
  const [running, setRunning] = useState(false);
  const [compareResult, setCompareResult] = useState<CompareResult | null>(null);
  const [loadedRun, setLoadedRun] = useState<BacktestRun | null>(null);
  /** 组合回测 POST 返回的轻量摘要（含 equity_curve / metrics / diagnostics），优先级最高。 */
  const [latestResult, setLatestResult] = useState<PortfolioBacktestLatestResult | null>(null);
  /** 回测失败/数据门禁状态必须保留在结果区，不能只通过 toast 告知。 */
  const [backtestGateState, setBacktestGateState] = useState<BacktestGateState | null>(null);
  // 单标的回测结果（独立于组合全局回测，含 trades）
  const [singleResult, setSingleResult] = useState<BacktestRun | null>(null);

  // ---------- 回测历史 ----------
  const [historyList, setHistoryList] = useState<BacktestRun[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);

  // ---------- WP1-1：证据与归因抽屉 ----------
  const { message } = AntApp.useApp();
  const [evidenceOpen, setEvidenceOpen] = useState(false);
  const [evidenceRunId, setEvidenceRunId] = useState<string | null>(null);
  const [evidenceId, setEvidenceId] = useState<string | null>(null);
  const [evidenceRun, setEvidenceRun] = useState<DecisionRunRead | null>(null);
  const [evidencePreview, setEvidencePreview] = useState<DecisionEvidenceRead[]>([]);
  const [evaluating, setEvaluating] = useState(false);
  // 最近 evaluate 时用的 strategy_snapshot_id（后端 evaluate 接口必填；缺省先弹框提示用户）
  const [evaluateSnapshotId, setEvaluateSnapshotId] = useState<string>("");

  /** 打开一个已存在 DecisionRun 的证据抽屉 */
  const openEvidenceByRunId = useCallback((rid: string) => {
    setEvidenceRunId(rid);
    setEvidenceId(null);
    setEvidenceRun(null);
    setEvidencePreview([]);
    setEvidenceOpen(true);
  }, []);

  /**
   * 触发一次决策引擎评估（persist=True → 落 DecisionRun + DecisionEvidence 表）
   * 成功后直接打开抽屉显示 preview + decision_run_id。
   * 参数：trade_date=portEnd，strategy_snapshot_id 由用户输入（兜底=从 portfolio-factor-usage 最新读取）
   */
  const triggerEvaluate = useCallback(async () => {
    if (!portfolioId) { message.warning("请先选择组合"); return; }
    if (!portEnd) { message.warning("请先选择回测结束日期作为评估 trade_date"); return; }

    let snapId = evaluateSnapshotId.trim();
    // 若未填 snapshot_id：best-effort 读取当前组合 latest_snapshot_id
    if (!snapId) {
      try {
        const cur = await api.getCurrentFactorUsage(portfolioId) as any;
        snapId = String(cur?.latest_snapshot_id || "");
        if (snapId) setEvaluateSnapshotId(snapId);
      } catch { /* ignore */ }
    }
    if (!snapId) {
      // 弹窗要求用户先在策略规则页点保存（Save & Apply 才能产生 snapshot）
      Modal.warning({
        title: "缺少策略执行快照",
        content: "WP1-1 决策评估需要策略规则已保存且已绑定。请先在「策略规则」页点击「保存应用」以生成最新 strategy_snapshot_id，或手动粘贴已有的 snapshot_id。",
        okText: "我知道了",
      });
      return;
    }

    setEvaluating(true);
    try {
      const resp = await api.evaluatePortfolioDecision(portfolioId, {
        strategy_snapshot_id: snapId,
        trade_date: portEnd,
        run_type: "backtest",
        persist: true,
      });
      message.success(
        resp.persisted
          ? `决策已落库！DecisionRun=${resp.decision_run_id.slice(0, 16)}… · Evidence ${resp.evidence_count} 条`
          : `Dry-run 完成：Evidence ${resp.evidence_count} 条（未 persist）`,
      );
      setEvidenceRunId(resp.decision_run_id);
      setEvidenceId(null);
      setEvidencePreview(resp.evidence_preview || []);
      setEvidenceRun({
        id: resp.decision_run_id,
        strategy_snapshot_id: snapId,
        portfolio_id: portfolioId,
        run_type: "backtest",
        trade_date: portEnd,
        decision_at: resp.clock?.decision_at_utc || resp.clock?.decision_at_sh || new Date().toISOString(),
        data_cutoff_at: resp.clock?.data_cutoff_at_utc || resp.clock?.data_cutoff_at_sh || new Date().toISOString(),
        execution_at: resp.clock?.execution_at_utc || resp.clock?.execution_at_sh || new Date().toISOString(),
        run_mode: "research",
        pit_mode: "best_effort",
        universe_count: 0,
        member_count: 0,
        score_count_expected: null,
        score_count_actual: null,
        score_coverage_pct: resp.score_coverage_pct ?? null,
        score_max_age_days: resp.score_max_age_days ?? null,
        blocking_status: resp.blocking_status as any || "READY",
        blocking_reasons_json: (resp.blocking_reasons?.length ? resp.blocking_reasons : null) as any,
        versions_json: null,
        idempotency_key: null,
        started_at: null,
        finished_at: null,
        duration_ms: null,
        is_result_production_eligible: resp.dry_run ? false : true,
        created_at: new Date().toISOString(),
      });
      setEvidenceOpen(true);
    } catch (err: any) {
      const msg = err?.message ? String(err.message) : String(err);
      message.error("决策评估失败：" + msg);
    } finally {
      setEvaluating(false);
    }
  }, [portfolioId, portEnd, evaluateSnapshotId, message]);

  const applyPortfolioRangePreset = useCallback((preset: BacktestRangePreset) => {
    const years = BACKTEST_RANGE_PRESETS.find((item) => item.key === preset)?.years ?? 1;
    const [start, end] = getBacktestDateRange(years);
    setPortStart(start);
    setPortEnd(end);
    setPortRangePreset(preset);
  }, []);

  const applySingleRangePreset = useCallback((preset: BacktestRangePreset) => {
    const years = BACKTEST_RANGE_PRESETS.find((item) => item.key === preset)?.years ?? 1;
    const [start, end] = getBacktestDateRange(years);
    setSingleStart(start);
    setSingleEnd(end);
    setSingleRangePreset(preset);
  }, []);

  // 加载来源状态（best-effort，失败不阻塞）
  useEffect(() => {
    if (!portfolioId) {
      setSourceStatus(null);
      return;
    }
    let cancelled = false;
    api
      .getPortfolioBacktestSourceStatus(portfolioId)
      .then((data) => {
        if (!cancelled) setSourceStatus(data);
      })
      .catch(() => {
        if (!cancelled) setSourceStatus(null);
      });
    return () => {
      cancelled = true;
    };
  }, [portfolioId]);

  // 加载回测历史列表（best-effort）
  const loadHistory = useCallback(async () => {
    if (!portfolioId) {
      setHistoryList([]);
      return;
    }
    setHistoryLoading(true);
    try {
      const list = await api.getBacktestRuns(portfolioId, 10);
      setHistoryList((list as BacktestRun[]) ?? []);
      // 最近回测时间取首条 finished_at / created_at
      const first = (list as BacktestRun[])?.[0];
      if (first) {
        setLastBacktestTime(first.finished_at ?? first.created_at ?? null);
      }
    } catch {
      setHistoryList([]);
    } finally {
      setHistoryLoading(false);
    }
  }, [portfolioId]);

  useEffect(() => {
    loadHistory();
  }, [loadHistory]);

  // ---------- 组合全局回测：开始回测 ----------
  const runPortfolioBacktest = useCallback(async () => {
    if (!portfolioId) return;
    // P1-FIX: 先校验日期非空 + 起始 <= 结束（避免 2025-12-31 ~ 2024-01-01 倒置）
    if (!validateDateRange(portStart, portEnd)) return;
    setRunning(true);
    setLoadedRun(null);
    setCompareResult(null);
    setLatestResult(null);
    setBacktestGateState(null);
    try {
      const initialCapital = Number(portCapital.replace(/,/g, ""));
      const commissionRate = Number(portCommission) / 100;
      if (!Number.isFinite(initialCapital) || initialCapital <= 0) {
        showToast("error", "初始资金必须是大于 0 的数字");
        return;
      }
      if (!Number.isFinite(commissionRate) || commissionRate < 0) {
        showToast("error", "手续费必须是非负数字");
        return;
      }
      const summary = (await api.runPortfolioBacktest(portfolioId, {
        start_date: portStart,
        end_date: portEnd,
        benchmark: portBenchmark,
        initial_capital: initialCapital,
        commission_rate: commissionRate,
        // 组合回测固定使用保存并应用的当前执行策略。
        pit_mode: "production_pit",
      })) as PortfolioBacktestLatestResult;
      const gate = gateFromBacktestSummary(summary as unknown as Record<string, any>);
      if (gate) {
        // 后端可能返回 HTTP 200 但 status=blocked/failed；此时绝不能把
        // 摘要中的曲线和指标当作成功结果展示。
        setLatestResult(null);
        setBacktestGateState(gate);
        showToast("error", gate.message);
        loadHistory().catch(() => {});
        return;
      }
      // 响应只含摘要。交易流水必须由结果 Tab 的分页接口按需读取，
      // 避免一次将多年成交记录常驻在浏览器内存。
      setLatestResult(summary);
      setBacktestGateState(null);
      setLastBacktestTime(new Date().toISOString().replace("T", " ").slice(0, 16));
      showToast("success", t("portfolioTrading.backtest.runSuccess"));
      // 刷新历史列表
      loadHistory().catch(() => {});
    } catch (err: any) {
      const gate = gateFromBacktestError(err);
      setBacktestGateState(gate);
      // 后端对多种错误都返回 user_message="服务暂时不可用"，需通过 status_code
      // 和 technical_details.error_message 区分真实原因：
      //   409 + "auto_trade_enabled"  → 自动接管未开启
      //   400 + "no positions" / "no current" → 组合无持仓和候选标的
      const statusCode = err?.status_code;
      const techMsg =
        (err?.detail as { technical_details?: { error_message?: string } } | undefined)
          ?.technical_details?.error_message ?? "";
      const errMsg = err?.message || String(err);
      if (statusCode === 409 || techMsg.includes("auto_trade_enabled")) {
        showToast("error", t("portfolioTrading.backtest.autoTradeRequired"));
      } else if (
        techMsg.includes("no positions") ||
        techMsg.includes("no scan candidates") ||
        techMsg.includes("no current positions") ||
        techMsg.includes("portfolio candidates")
      ) {
        showToast("error", t("portfolioTrading.backtest.noPositions"));
      } else {
        showToast("error", t("portfolioTrading.backtest.runFailed") + ": " + errMsg);
      }
    } finally {
      setRunning(false);
    }
  }, [portfolioId, portStart, portEnd, portCapital, portBenchmark, portCommission, showToast, loadHistory, validateDateRange]);

  // ---------- 单股回测：解析代码 → runBacktest → getBacktestRun（含 trades）----------
  const runSingleBacktest = useCallback(async () => {
    const code = singleSymbol.trim();
    if (!code) {
      showToast("info", t("portfolioTrading.backtest.symbolRequired"));
      return;
    }
    if (!portfolioId) return;
    // P1-FIX: 先校验日期非空 + 起始 <= 结束
    if (!validateDateRange(singleStart, singleEnd)) return;
    setRunning(true);
    setSingleResult(null);
    setBacktestGateState(null);
    try {
      // 1. 代码 → symbol_id（精确匹配优先，回退首条）
      const matches = await api.getSymbols(code, { pageSize: 50 });
      const list = Array.isArray(matches) ? matches : [];
      const exact = list.find((s: any) => s?.symbol === code) ?? list[0];
      if (!exact || !exact.id) {
        showToast("error", t("portfolioTrading.backtest.singleRunFailed") + ": " + code);
        setRunning(false);
        return;
      }
      // 2. 构造最小可用 rule_config（根据 singleIndicator 生成不同的买卖阈值，避免"看起来选了实际没影响"）
      //   MA趋势     → 保守：质量/择时 阈值偏高，持有期较长
      //   MACD       → 平衡：经典双均线，默认
      //   RSI        → 激进：质量 30，择时 3，低位反转
      //   布林带      → 均值回归：宽止损，短持有
      //   KDJ        → 短线：严格质量，宽松择时，止盈小
      const indicatorPreset = (() => {
        const ind = singleIndicator;
        if (ind === "MA") return { quality_min: 60, timing_min: 70, tp: 0.25, sl: 0.1, hold_days: 45 };
        if (ind === "MACD") return { quality_min: 50, timing_min: 60, tp: 0.20, sl: 0.08, hold_days: 30 };
        if (ind === "RSI") return { quality_min: 30, timing_min: 30, tp: 0.12, sl: 0.08, hold_days: 15 };
        if (ind === "布林带") return { quality_min: 45, timing_min: 50, tp: 0.10, sl: 0.12, hold_days: 20 };
        if (ind === "KDJ") return { quality_min: 70, timing_min: 40, tp: 0.08, sl: 0.06, hold_days: 10 };
        return { quality_min: 50, timing_min: 60, tp: 0.20, sl: 0.08, hold_days: 30 };
      })();
      const commissionPct = Number(singleCommission) || 0;
      const initialCap = Number(singleCapital);
      const ruleConfig = {
        buy_conditions: {
          quality_score_min: indicatorPreset.quality_min,
          timing_score_min: indicatorPreset.timing_min,
          actions: ["buy", "open"],
          stages: [],
          indicator_key: singleIndicator,
        },
        sell_conditions: {
          take_profit_pct: indicatorPreset.tp,
          stop_loss_pct: indicatorPreset.sl,
          max_hold_days: indicatorPreset.hold_days,
          score_actions: ["exit", "reduce"],
        },
        position_config: { type: "fixed_pct", value: 1, max_positions: 1 },
        execution_config: { entry_timing: "next_open", exit_timing: "next_open", entry_price_field: "open", exit_price_field: "open" },
      };
      // 3. 发起回测（initial_capital 真实提交）
      const created = (await api.runBacktest({
        portfolio_id: portfolioId,
        symbol_ids: [Number(exact.id)],
        start_date: singleStart,
        end_date: singleEnd,
        run_name: `${code}-${singleIndicator}`,
        initial_capital: Number.isFinite(initialCap) && initialCap > 0 ? initialCap : undefined,
        rule_config: ruleConfig,
        cost_config: {
          commission_rate: commissionPct / 100,
          min_commission: 5,
          stamp_tax_rate: 0.001,
          slippage_rate: 0.001,
        },
      })) as BacktestRun;
      // 4. 拉取明细（含 trades）
      const detail = (await api.getBacktestRun(created.id)) as BacktestRun;
      setSingleResult(detail);
      setLastBacktestTime(new Date().toISOString().replace("T", " ").slice(0, 16));
      showToast("success", t("portfolioTrading.backtest.singleRunSuccess"));
      loadHistory().catch(() => {});
    } catch (err: any) {
      const msg = err?.message || String(err);
      const errorText = JSON.stringify(err?.detail ?? "");
      const repairMode = errorText.includes("BACKTEST_SCORE_COVERAGE_INSUFFICIENT")
        ? "scores"
        : errorText.includes("BACKTEST_MARKET_DATA_MISSING")
          ? "bars"
          : null;
      if (repairMode) {
        showToast(
          "error",
          <span>
            {repairMode === "scores"
              ? "历史评分覆盖不足，请先初始化评分历史。"
              : "该标的在回测区间内没有行情数据，请先初始化历史行情。"}{" "}
            <a
              href="#settings-history"
              onClick={(event) => {
                event.preventDefault();
                openHistoryRepair(repairMode);
              }}
            >
              去修复
            </a>
          </span>,
        );
      } else {
        showToast("error", t("portfolioTrading.backtest.singleRunFailed") + ": " + msg);
      }
    } finally {
      setRunning(false);
    }
  }, [singleSymbol, singleStart, singleEnd, singleIndicator, singleCommission, portfolioId, showToast, loadHistory, validateDateRange, openHistoryRepair]);

  // ---------- 回测历史：加载某条记录 ----------
  const loadHistoryItem = useCallback(
    async (runId: number) => {
      try {
        const run = (await api.getBacktestRun(runId)) as BacktestRun;
        // A selected historical run replaces the just-created summary.  Keep
        // this paired with the render priority below so stale latestResult
        // cannot continue to drive metrics, curves or ledger requests.
        setLatestResult(null);
        setLoadedRun(run);
        setCompareResult(null);
        setBacktestGateState(gateFromHistoricalBacktest(run));
        setMode("portfolio");
        setHistoryOpen(false);
        showToast("success", t("portfolioTrading.backtest.historyLoaded"));
      } catch (err: any) {
        const msg = err?.message || String(err);
        showToast("error", t("portfolioTrading.backtest.historyLoadFailed") + ": " + msg);
      }
    },
    [showToast],
  );

  // Re-open a historical run when a result URL is refreshed or shared.  The
  // result panel owns the tab/filter query state, while the parent owns the
  // persisted run itself, so restoration must happen here before any paged
  // endpoint can be queried.
  const restoredRunIdRef = React.useRef<number | null | undefined>(undefined);
  useEffect(() => {
    if (!portfolioId) return;
    const runId = readBacktestRunIdFromUrl();
    if (!runId || restoredRunIdRef.current === runId) return;
    if (latestResult?.run_id === runId || loadedRun?.id === runId) {
      restoredRunIdRef.current = runId;
      return;
    }
    restoredRunIdRef.current = runId;
    void loadHistoryItem(runId);
  }, [portfolioId, latestResult?.run_id, loadedRun?.id, loadHistoryItem]);

  // ---------- 派生：指标卡数据来源 ----------
  const metrics: MetricSource = React.useMemo(() => {
    // P2-TDD-FIX：优先 latestResult（组合回测 POST 返回的全量结果，含 metrics 全字段）
    // → 确保回测明细 9 项（年化波动/回撤天数/换手率）不显示 --，也确保曲线/明细按真实数据重渲染
    if (latestResult) {
      const m = latestResult.metrics ?? {};
      const pickNum = (keys: (keyof PortfolioBacktestLatestResult | string)[]): number | null => {
        for (const k of keys) {
          const val = (latestResult as any)[k] ?? m[k];
          if (val != null && Number.isFinite(Number(val))) return Number(val);
        }
        return null;
      };
      const eqArr = (latestResult.equity_curve || []).map((p) => Number(p.equity) || 0).filter((v) => v > 0);
      // 兜底计算：annual_volatility / drawdown_days / turnover_rate
      // 如果后端有值（已修复后新回测）→ 直接用；后端缺失（老回测/走 loadedRun）→ 前端即时算
      const fallbackAnnualVol = (() => {
        if (eqArr.length < 5) return null;
        const daily: number[] = [];
        for (let i = 1; i < eqArr.length; i++) {
          daily.push(eqArr[i] / eqArr[i - 1] - 1);
        }
        const mean = daily.reduce((s, r) => s + r, 0) / daily.length;
        const variance = daily.reduce((s, r) => s + (r - mean) ** 2, 0) / daily.length;
        const sigma = Math.sqrt(variance);
        const annual = sigma * Math.sqrt(252) * 100;
        return Number.isFinite(annual) ? Number(annual.toFixed(2)) : null;
      })();
      const fallbackDrawdownDays = (() => {
        if (eqArr.length < 2) return null;
        let peak = eqArr[0];
        let underWater = 0;
        let maxUnderWater = 0;
        for (let i = 1; i < eqArr.length; i++) {
          const v = eqArr[i];
          if (v > peak) { peak = v; underWater = 0; }
          else if (v < peak) { underWater++; if (underWater > maxUnderWater) maxUnderWater = underWater; }
        }
        return maxUnderWater;
      })();
      return {
        totalReturnPct: pickNum(["total_return_pct"]) ?? null,
        maxDrawdownPct: pickNum(["max_drawdown_pct"]) ?? null,
        sharpe: pickNum(["sharpe_ratio"]) ?? null,
        winRate: pickNum(["win_rate"]) ?? null,
        profitFactor: pickNum(["profit_factor"]) ?? null,
        tradeCount: pickNum(["trade_count"]) ?? null,
        avgHoldingDays: pickNum(["avg_holding_days"]) ?? null,
        // 回测明细真实字段（后端 PortfolioBacktestResult.metrics 里的全量）
        annualReturnPct:
          (m["annual_return_pct"] != null && Number.isFinite(Number(m["annual_return_pct"]))
            ? Number(m["annual_return_pct"])
            : null) ??
          pickNum(["total_return_pct"]),
        annualVolatility:
          (m["annual_volatility"] != null && Number.isFinite(Number(m["annual_volatility"]))
            ? Number(m["annual_volatility"])
            : null) ?? fallbackAnnualVol,
        drawdownDays:
          (m["drawdown_days"] != null && Number.isFinite(Number(m["drawdown_days"]))
            ? Number(m["drawdown_days"])
            : null) ?? fallbackDrawdownDays,
        turnoverRate:
          (m["turnover_rate"] != null && Number.isFinite(Number(m["turnover_rate"]))
            ? Number(m["turnover_rate"])
            : null),
      };
    }
    if (loadedRun) {
      return {
        totalReturnPct: loadedRun.total_return_pct ?? null,
        maxDrawdownPct: loadedRun.max_drawdown_pct ?? null,
        sharpe: loadedRun.sharpe_ratio ?? null,
        winRate: loadedRun.win_rate ?? null,
        profitFactor: loadedRun.profit_factor ?? null,
        tradeCount: loadedRun.trade_count ?? null,
        avgHoldingDays: loadedRun.avg_holding_days ?? null,
        annualReturnPct: loadedRun.total_return_pct ?? null,
        annualVolatility: null,
        drawdownDays: null,
        turnoverRate: null,
      };
    }
    if (compareResult) {
      const m = compareResult.new.metrics;
      return {
        totalReturnPct: m.total_return_pct ?? null,
        maxDrawdownPct: m.max_drawdown_pct ?? null,
        sharpe: m.sharpe_ratio ?? null,
        winRate: m.win_rate ?? null,
        profitFactor: m.profit_factor ?? null,
        tradeCount: m.trade_count ?? null,
        avgHoldingDays: m.avg_holding_days ?? null,
        annualReturnPct: m.total_return_pct ?? null,
        annualVolatility: null,
        drawdownDays: null,
        turnoverRate: null,
      };
    }
    return {
      totalReturnPct: null,
      maxDrawdownPct: null,
      sharpe: null,
      winRate: null,
      profitFactor: null,
      tradeCount: null,
      avgHoldingDays: null,
      annualReturnPct: null,
      annualVolatility: null,
      drawdownDays: null,
      turnoverRate: null,
    };
  }, [latestResult, loadedRun, compareResult]);

  const hasResult = !backtestGateState && !!(latestResult || loadedRun || compareResult);
  /** 当前运行的净值曲线：新运行用摘要，历史运行用持久化快照。 */
  const latestEquityCurve: EquityCurvePoint[] = React.useMemo(
    () => latestResult ? latestResult.equity_curve : parseStoredEquityCurve(loadedRun?.equity_curve_json),
    [latestResult, loadedRun],
  );
  /** 当前运行绑定的执行快照；历史运行优先读取持久化 snapshot 字段。 */
  const decisionSnapshot = React.useMemo<Record<string, any> | null>(() => {
    const raw = latestResult?.decision_snapshot ?? (loadedRun as any)?.decision_snapshot;
    if (typeof raw === "string") {
      try {
        return asRecord(JSON.parse(raw));
      } catch {
        return null;
      }
    }
    return asRecord(raw);
  }, [latestResult, loadedRun]);
  /** 成本/撮合参数与快照并列回显，避免历史运行重新打开后丢失执行假设。 */
  const executionParameters = React.useMemo<Record<string, any> | null>(() => {
    if (latestResult) {
      const fields = [
        "commission_rate", "stamp_tax_rate", "slippage_bps", "price_type",
        "volume_limit_pct", "rebalance_frequency", "pit_mode",
      ];
      const values = fields.reduce<Record<string, any>>((result, key) => {
        const value = (latestResult as any)[key];
        if (value != null) result[key] = value;
        return result;
      }, {});
      return Object.keys(values).length ? values : null;
    }
    const raw = loadedRun?.cost_config_json;
    if (!raw) return null;
    try {
      return asRecord(JSON.parse(raw));
    } catch {
      return null;
    }
  }, [latestResult, loadedRun]);
  /**
   * 交易明细绝不从运行摘要取得。表格按页读取，曲线不再为了装饰标记
   * 而载入完整流水。
   */
  const latestTrades: (BacktestTrade | any)[] = React.useMemo(
    () => [],
    [],
  );

  return (
    <section className="pt-backtest-center" style={{ padding: 16, display: "flex", flexDirection: "column", gap: 16 }}>
      {/* ========== SubTask 7.1: 顶部模式切换 ========== */}
      <div className="pt-card" style={{ padding: 12 }}>
        <div className="pt-backtest-toolbar" style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
          {/* 左侧：模式切换按钮组 */}
          <div
            className="pt-backtest-mode-switch"
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 2,
              padding: 2,
              borderRadius: "var(--pt-radius-md)",
              background: "var(--pt-surface-3)",
            }}
          >
            <button
              type="button"
              className={`pt-btn pt-btn-sm ${mode === "portfolio" ? "pt-btn-primary" : "pt-btn-outline"}`}
              style={{ borderColor: "transparent" }}
              onClick={() => setMode("portfolio")}
            >
              {t("portfolioTrading.backtest.modePortfolio")}
            </button>
            <button
              type="button"
              className={`pt-btn pt-btn-sm ${mode === "single" ? "pt-btn-primary" : "pt-btn-outline"}`}
              style={{ borderColor: "transparent" }}
              onClick={() => setMode("single")}
            >
              {t("portfolioTrading.backtest.modeSingle")}
            </button>
          </div>

          {/* 右侧：最近回测时间 + 回测历史按钮 + 决策证据（WP1-1） */}
          <div className="pt-backtest-toolbar-actions" style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
            <span style={{ fontSize: 12, color: "var(--pt-muted-foreground)" }}>
              {t("portfolioTrading.backtest.lastRun")}
              {lastBacktestTime ? `：${lastBacktestTime}` : ""}
            </span>
            <button
              type="button"
              className="pt-btn pt-btn-secondary pt-btn-sm"
              onClick={() => setHistoryOpen(true)}
              title="查看回测历史记录"
            >
              <History size={14} />
              {t("portfolioTrading.backtest.historyBtn")}
            </button>
            {/* WP1-1：生成/查看决策证据入口 */}
            <div
              className="pt-backtest-evidence-controls"
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 4,
                padding: "3px 6px",
                borderRadius: "var(--pt-radius-md)",
                border: "1px solid var(--pt-border)",
                background: "var(--pt-surface)",
              }}
              title="WP1-1：在 trade_date 上跑一次决策引擎（persist=True）并打开证据抽屉；或传入 decision_run_id 直接打开"
            >
              <input
                type="text"
                className="pt-backtest-evidence-input"
                placeholder="strategy_snapshot_id（可选，默认取最新）"
                value={evaluateSnapshotId}
                onChange={(e) => setEvaluateSnapshotId(e.target.value)}
                style={{
                  width: 210, height: 28,
                  border: "none", outline: "none", background: "transparent",
                  fontSize: 12, color: "var(--pt-foreground)",
                  fontFamily: "var(--pt-font-mono)",
                }}
              />
              <button
                type="button"
                className="pt-btn pt-btn-sm pt-btn-outline"
                onClick={triggerEvaluate}
                disabled={evaluating || !portfolioId}
                style={{ borderColor: "rgba(14,165,233,0.35)", color: "var(--pt-state-info)" }}
                title="触发 persist=True 的决策评估，生成 DecisionRun/Evidence 后自动打开抽屉"
              >
                {evaluating ? <Loader2 size={13} className="pt-rotate" /> : <AlertTriangle size={13} />}
                {evaluating ? "评估中…" : "生成决策证据"}
              </button>
              <button
                type="button"
                className="pt-btn pt-btn-sm pt-btn-primary"
                onClick={() => {
                  // 若已存在刚 evaluate 的 run 直接打开；否则让用户粘贴 ID（此处做一个简单 prompt 兜底）
                  if (evidenceRunId) {
                    setEvidenceId(null);
                    setEvidenceRun(null);
                    setEvidencePreview([]);
                    setEvidenceOpen(true);
                    return;
                  }
                  const id = window.prompt(
                    "请输入要查看的 decision_run_id（可从 GET /portfolios/{pid}/decision-runs 列表获取）",
                    "",
                  );
                  if (id && id.trim()) openEvidenceByRunId(id.trim());
                }}
                disabled={!portfolioId}
                title="查看指定 decision_run_id 的完整证据抽屉"
              >
                <FileText size={13} />
                查看证据抽屉
              </button>
            </div>
          </div>
        </div>
      </div>

      {/* ========== SubTask 7.2 / 7.3: 回测面板（按模式切换） ========== */}
      {mode === "portfolio" ? (
        <PortfolioBacktestPanel
          portStart={portStart}
          portEnd={portEnd}
          setPortStart={setPortStart}
          setPortEnd={setPortEnd}
          portRangePreset={portRangePreset}
          setPortRangePreset={setPortRangePreset}
          onApplyRangePreset={applyPortfolioRangePreset}
          portCapital={portCapital}
          setPortCapital={setPortCapital}
          portBenchmark={portBenchmark}
          setPortBenchmark={setPortBenchmark}
          portCommission={portCommission}
          setPortCommission={setPortCommission}
          sourceStatus={sourceStatus}
          gateState={backtestGateState}
          decisionSnapshot={decisionSnapshot}
          executionParameters={executionParameters}
          runMetadata={(latestResult ?? loadedRun) as Record<string, any> | null}
          running={running}
          hasResult={hasResult}
          metrics={metrics}
          onRun={runPortfolioBacktest}
          autoTradeEnabled={autoTradeEnabled}
          latestEquityCurve={latestEquityCurve}
          latestTrades={latestTrades}
          latestRunId={latestResult?.run_id ?? loadedRun?.id ?? null}
          onOpenEvidence={(target) => {
            setEvidenceRunId(target.decisionRunId);
            setEvidenceId(target.evidenceId);
            setEvidenceRun(null);
            setEvidencePreview([]);
            setEvidenceOpen(true);
          }}
          initialCapital={latestResult?.initial_capital ?? loadedRun?.initial_capital}
        />
      ) : (
        <SingleBacktestPanel
          singleSymbol={singleSymbol}
          setSingleSymbol={setSingleSymbol}
          singleStart={singleStart}
          setSingleStart={setSingleStart}
          singleEnd={singleEnd}
          setSingleEnd={setSingleEnd}
          singleRangePreset={singleRangePreset}
          setSingleRangePreset={setSingleRangePreset}
          onApplyRangePreset={applySingleRangePreset}
          singleIndicator={singleIndicator}
          setSingleIndicator={setSingleIndicator}
          singleCapital={singleCapital}
          setSingleCapital={setSingleCapital}
          singleCommission={singleCommission}
          setSingleCommission={setSingleCommission}
          running={running}
          result={singleResult}
          onRun={runSingleBacktest}
          autoTradeEnabled={autoTradeEnabled}
        />
      )}

      {/* ========== SubTask 7.4: 回测历史抽屉 ========== */}
      <HistoryDrawer
        open={historyOpen}
        onClose={() => setHistoryOpen(false)}
        list={historyList}
        loading={historyLoading}
        onSelect={loadHistoryItem}
      />

      {/* ========== WP1-1: 证据与归因抽屉（DecisionRun + DecisionEvidence） ========== */}
      <DecisionEvidenceDrawer
        open={evidenceOpen}
        onClose={() => {
          setEvidenceOpen(false);
          setEvidenceId(null);
        }}
        portfolioId={portfolioId}
        decisionRunId={evidenceRunId}
        initialRun={evidenceRun}
        initialEvidence={evidencePreview}
        selectedEvidenceId={evidenceId}
        onSelectedEvidenceIdChange={setEvidenceId}
      />
    </section>
  );
};

/* ------------------------------------------------------------------ */
/* SubTask 7.2: 组合全局回测面板                                       */
/* ------------------------------------------------------------------ */

interface BacktestDateRangeFieldProps {
  start: string;
  end: string;
  activePreset: BacktestRangePreset | null;
  onDatesChange: (start: string, end: string) => void;
  onPresetChange: (preset: BacktestRangePreset | null) => void;
  onApplyPreset: (preset: BacktestRangePreset) => void;
  testId: string;
}

const BacktestDateRangeField: React.FC<BacktestDateRangeFieldProps> = ({
  start,
  end,
  activePreset,
  onDatesChange,
  onPresetChange,
  onApplyPreset,
  testId,
}) => {
  const value: [Dayjs, Dayjs] = [dayjs(start), dayjs(end)];
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <DatePicker.RangePicker
        className="pt-backtest-date-picker"
        value={value}
        allowClear={false}
        format="YYYY-MM-DD"
        style={{ width: "100%" }}
        data-testid={`${testId}-picker`}
        onChange={(dates) => {
          if (!dates?.[0] || !dates?.[1]) return;
          onDatesChange(dates[0].format("YYYY-MM-DD"), dates[1].format("YYYY-MM-DD"));
          onPresetChange(null);
        }}
      />
      <div style={{ display: "flex", gap: 6 }} aria-label="回测区间快捷选择">
        {BACKTEST_RANGE_PRESETS.map((preset) => (
          <button
            key={preset.key}
            type="button"
            className={`pt-btn pt-btn-sm ${activePreset === preset.key ? "pt-btn-secondary" : "pt-btn-ghost"}`}
            aria-pressed={activePreset === preset.key}
            data-testid={`${testId}-${preset.key}`}
            onClick={() => onApplyPreset(preset.key)}
          >
            {preset.label}
          </button>
        ))}
      </div>
    </div>
  );
};

interface PortfolioBacktestPanelProps {
  portStart: string;
  portEnd: string;
  setPortStart: (v: string) => void;
  setPortEnd: (v: string) => void;
  portRangePreset: BacktestRangePreset | null;
  setPortRangePreset: (preset: BacktestRangePreset | null) => void;
  onApplyRangePreset: (preset: BacktestRangePreset) => void;
  portCapital: string;
  setPortCapital: (v: string) => void;
  portBenchmark: string;
  setPortBenchmark: (v: string) => void;
  portCommission: string;
  setPortCommission: (v: string) => void;
  sourceStatus: SourceStatus | null;
  gateState?: BacktestGateState | null;
  decisionSnapshot?: Record<string, any> | null;
  executionParameters?: Record<string, any> | null;
  /** 持久化回测结果元数据，用于持仓和数据说明 Tab。 */
  runMetadata?: Record<string, any> | null;
  running: boolean;
  hasResult: boolean;
  metrics: MetricSource;
  onRun: () => void;
  /** P1-FIX: 自动交易关闭时按钮禁用 */
  autoTradeEnabled: boolean;
  /** P2-TDD: 最新 equity_curve（真实数据驱动 SVG 曲线重渲染） */
  latestEquityCurve: EquityCurvePoint[];
  /** P2-TDD: 最新 trades（净值曲线买卖点▲▼ 标记） */
  latestTrades: (BacktestTrade | any)[];
  /** 当前展示的回测运行，用于服务端分页的流水/证据查询。 */
  latestRunId: number | null;
  onOpenEvidence: (target: EvidenceTarget) => void;
  /** P2-TDD: 初始资金（用于净值曲线 Y 轴净值格式化） */
  initialCapital?: number;
}

const PortfolioBacktestPanel: React.FC<PortfolioBacktestPanelProps> = ({
  portStart,
  portEnd,
  setPortStart,
  setPortEnd,
  portRangePreset,
  setPortRangePreset,
  onApplyRangePreset,
  portCapital,
  setPortCapital,
  portBenchmark,
  setPortBenchmark,
  portCommission,
  setPortCommission,
  sourceStatus,
  gateState = null,
  decisionSnapshot = null,
  executionParameters = null,
  runMetadata = null,
  running,
  hasResult,
  metrics,
  onRun,
  autoTradeEnabled,
  latestEquityCurve,
  latestTrades,
  latestRunId,
  onOpenEvidence,
  initialCapital,
}) => {
  const [initialLedgerUrlState] = useState(readTradeLedgerUrlState);
  const [resultTab, setResultTab] = useState<ResultTabKey>(initialLedgerUrlState.tab);
  const [tradePage, setTradePage] = useState(initialLedgerUrlState.page);
  const [tradeRows, setTradeRows] = useState<BacktestTrade[]>([]);
  const [tradeTotal, setTradeTotal] = useState(0);
  const [tradesLoading, setTradesLoading] = useState(false);
  const [tradeAction, setTradeAction] = useState<TradeActionFilter>(initialLedgerUrlState.action);
  const [tradeExecutionStatus, setTradeExecutionStatus] = useState<TradeExecutionFilter>(initialLedgerUrlState.status);
  const [tradeSymbol, setTradeSymbol] = useState(initialLedgerUrlState.symbol);
  const [tradeSortBy, setTradeSortBy] = useState<TradeSortField>(initialLedgerUrlState.sortBy);
  const [tradeSortDir, setTradeSortDir] = useState<TradeSortDirection>(initialLedgerUrlState.sortDir);
  const [rejectedPage, setRejectedPage] = useState(1);
  const [rejectedRows, setRejectedRows] = useState<DecisionEvidenceRead[]>([]);
  const [rejectedTotal, setRejectedTotal] = useState(0);
  const [rejectedLoading, setRejectedLoading] = useState(false);
  const [positionPage, setPositionPage] = useState(initialLedgerUrlState.positionPage);
  const [positionStatus, setPositionStatus] = useState<"" | "OPEN" | "CLOSED">(initialLedgerUrlState.positionStatus);
  const [positionAsOfDate, setPositionAsOfDate] = useState(initialLedgerUrlState.positionAsOfDate);
  const [positionRows, setPositionRows] = useState<BacktestPosition[]>([]);
  const [positionTotal, setPositionTotal] = useState(0);
  const [positionAsOf, setPositionAsOf] = useState<string | null>(null);
  const [positionLedgerMode, setPositionLedgerMode] = useState<BacktestPositionLedgerMode | null>(null);
  const [positionsLoading, setPositionsLoading] = useState(false);
  const [positionsError, setPositionsError] = useState<string | null>(null);
  const pageSize = 20;
  const resultTabs = [
    ["overview", "绩效概览"],
    ["trades", `交易流水 ${tradeTotal || metrics.tradeCount || 0}`],
    ["positions", "持仓变化"],
    ["rejected", "拒绝记录"],
    ["data", "数据说明"],
  ] as const;
  const previousRunIdRef = React.useRef<number | null | undefined>(undefined);
  useEffect(() => {
    if (previousRunIdRef.current !== undefined && previousRunIdRef.current !== latestRunId) {
      setTradePage(1);
      setRejectedPage(1);
      setTradeRows([]);
      setRejectedRows([]);
      setPositionPage(1);
      setPositionRows([]);
      setPositionTotal(0);
      setPositionAsOf(null);
      setPositionLedgerMode(null);
    }
    previousRunIdRef.current = latestRunId;
  }, [latestRunId]);
  useEffect(() => {
    if (typeof window === "undefined") return;
    const query = new URLSearchParams(window.location.search);
    const setOrDelete = (key: string, value: string | null) => {
      if (value) query.set(key, value);
      else query.delete(key);
    };
    // Keep a valid deep-linked run id while the parent is restoring it.  If
    // the child deletes it during the first render, refresh/share can no
    // longer recover the historical result.
    if (latestRunId != null) setOrDelete("bt_run", String(latestRunId));
    setOrDelete("bt_tab", resultTab === "overview" ? null : resultTab);
    setOrDelete("bt_page", resultTab === "trades" && tradePage > 1 ? String(tradePage) : null);
    setOrDelete("bt_action", tradeAction || null);
    setOrDelete("bt_status", tradeExecutionStatus || null);
    setOrDelete("bt_symbol", tradeSymbol || null);
    setOrDelete("bt_sort", tradeSortBy === "signal_at" ? null : tradeSortBy);
    setOrDelete("bt_dir", tradeSortDir === "desc" ? null : tradeSortDir);
    setOrDelete("bt_position_page", resultTab === "positions" && positionPage > 1 ? String(positionPage) : null);
    setOrDelete("bt_position_status", resultTab === "positions" && positionStatus ? positionStatus : null);
    setOrDelete("bt_position_as_of", resultTab === "positions" && positionAsOfDate ? positionAsOfDate : null);
    const search = query.toString();
    window.history.replaceState(window.history.state, "", `${window.location.pathname}${search ? `?${search}` : ""}${window.location.hash}`);
  }, [latestRunId, resultTab, tradePage, tradeAction, tradeExecutionStatus, tradeSymbol, tradeSortBy, tradeSortDir, positionPage, positionStatus, positionAsOfDate]);
  useEffect(() => {
    if (gateState || resultTab !== "trades" || !latestRunId) return;
    let alive = true;
    setTradesLoading(true);
    const parsedSymbolId = Number(tradeSymbol);
    const symbolId = Number.isInteger(parsedSymbolId) && parsedSymbolId > 0
      ? parsedSymbolId
      : undefined;
    const options = {
      page: tradePage,
      pageSize,
      ...(tradeAction ? { action: tradeAction } : {}),
      ...(symbolId != null ? { symbolId } : {}),
      ...(tradeExecutionStatus ? { executionStatus: tradeExecutionStatus } : {}),
      sortBy: tradeSortBy,
      sortDir: tradeSortDir,
    };
    api.getBacktestTrades(latestRunId, options)
      .then((resp) => {
        if (!alive) return;
        setTradeRows(resp.items || []);
        setTradeTotal(resp.total || 0);
      })
      .catch(() => { if (alive) { setTradeRows([]); setTradeTotal(0); } })
      .finally(() => { if (alive) setTradesLoading(false); });
    return () => { alive = false; };
  }, [gateState, latestRunId, resultTab, tradePage, tradeAction, tradeExecutionStatus, tradeSymbol, tradeSortBy, tradeSortDir]);
  useEffect(() => {
    if (gateState || resultTab !== "rejected" || !latestRunId) return;
    let alive = true;
    setRejectedLoading(true);
    api.getBacktestEvidence(latestRunId, {
      page: rejectedPage,
      pageSize,
      action: "REJECTED,DATA_BLOCKED",
    })
      .then((resp) => {
        if (!alive) return;
        setRejectedRows(resp.items || []);
        setRejectedTotal(resp.total || 0);
      })
      .catch(() => { if (alive) { setRejectedRows([]); setRejectedTotal(0); } })
      .finally(() => { if (alive) setRejectedLoading(false); });
    return () => { alive = false; };
  }, [gateState, latestRunId, resultTab, rejectedPage]);
  useEffect(() => {
    if (gateState || resultTab !== "positions" || !latestRunId) return;
    let alive = true;
    setPositionsLoading(true);
    setPositionsError(null);
    setPositionLedgerMode(null);
    const options = {
      page: positionPage,
      pageSize,
      ...(positionStatus ? { status: positionStatus } : {}),
      ...(positionAsOfDate ? { asOfDate: positionAsOfDate } : {}),
    };
    api.getBacktestPositions(latestRunId, options)
      .then((resp) => {
        if (!alive) return;
        setPositionRows(resp.items || []);
        setPositionTotal(resp.total || 0);
        setPositionAsOf(resp.as_of_date || null);
        setPositionLedgerMode(resp.ledger_mode || null);
      })
      .catch((error) => {
        if (!alive) return;
        setPositionRows([]);
        setPositionTotal(0);
        setPositionAsOf(null);
        setPositionLedgerMode(null);
        setPositionsError(error?.message ? String(error.message) : "持仓投影加载失败");
      })
      .finally(() => { if (alive) setPositionsLoading(false); });
    return () => { alive = false; };
  }, [gateState, latestRunId, resultTab, positionPage, positionStatus, positionAsOfDate]);
  const metadata = contractRecord(runMetadata);
  const dataSnapshot = contractRecord(metadata.data_snapshot);
  const dataCutoff = contractValue(metadata, "data_cutoff_at", "factor_data_cutoff_at")
    ?? contractValue(dataSnapshot, "data_cutoff_at", "factor_data_cutoff_at");
  const pitMode = contractValue(metadata, "pit_mode", "match_mode")
    ?? contractValue(dataSnapshot, "pit_mode");
  const benchmarkStatus = contractValue(metadata, "benchmark_status")
    ?? contractValue(dataSnapshot, "benchmark_status");
  const benchmarkGapDays = contractValue(metadata, "benchmark_gap_days")
    ?? contractValue(dataSnapshot, "benchmark_gap_days", "gap_days");
  const sourceType = contractValue(dataSnapshot, "market_source", "source")
    ?? contractValue(metadata, "source_type", "symbol_source");
  const snapshotId = contractValue(metadata, "strategy_snapshot_id", "snapshot_id")
    ?? contractValue(decisionSnapshot || {}, "id", "snapshot_id");
  const snapshotHash = contractValue(metadata, "snapshot_hash")
    ?? contractValue(decisionSnapshot || {}, "snapshot_hash");
  const modelRunId = contractValue(metadata, "factor_model_run_id")
    ?? contractValue(decisionSnapshot || {}, "factor_model_run_id");
  const factorSetId = contractValue(metadata, "factor_set_id")
    ?? contractValue(decisionSnapshot || {}, "factor_set_id");
  const reproducibilityStatus = contractValue(metadata, "reproducibility_status")
    ?? (snapshotId ? "reproducible" : "legacy/non_reproducible");
  const reproducibilityReason = contractValue(metadata, "reproducibility_reason");
  const reproducibilityText = reproducibilityStatus === "reproducible"
    ? "可复现（已绑定执行快照与决策链）"
    : "历史结果，暂不可复现";
  const metadataWarnings = (() => {
    const value = contractValue(metadata, "warnings", "blocking_reasons")
      ?? contractValue(dataSnapshot, "warnings", "issues");
    return Array.isArray(value) ? value : value == null ? [] : [value];
  })();
  return (
    <div className="pt-backtest-layout pt-backtest-layout-portfolio" style={{ display: "grid", gridTemplateColumns: "minmax(0, 320px) minmax(0, 1fr)", gap: 16 }}>
      {/* ---------- 左侧：参数配置面板 ---------- */}
      <div className="pt-card" style={{ overflow: "hidden" }}>
        <div
          style={{
            padding: "12px 16px",
            borderBottom: "1px solid var(--pt-border)",
          }}
        >
          <h3 style={sectionTitleStyle}>{t("portfolioTrading.backtest.paramConfig")}</h3>
          <p style={sectionSubStyle}>{t("portfolioTrading.backtest.paramConfigSub")}</p>
        </div>
        <div style={{ padding: 16, display: "flex", flexDirection: "column", gap: 16 }}>
          {/* 回测区间 */}
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            <label style={fieldLabelStyle}>{t("portfolioTrading.backtest.dateRange")}</label>
            <BacktestDateRangeField
              start={portStart}
              end={portEnd}
              activePreset={portRangePreset}
              onDatesChange={(start, end) => {
                setPortStart(start);
                setPortEnd(end);
              }}
              onPresetChange={setPortRangePreset}
              onApplyPreset={onApplyRangePreset}
              testId="portfolio-backtest-range"
            />
          </div>

          {/* 初始资金（¥ 前缀） */}
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            <label style={fieldLabelStyle}>{t("portfolioTrading.backtest.initialCapital")}</label>
            <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <span style={{ fontSize: 13, color: "var(--pt-muted-foreground)" }}>¥</span>
              <input
                type="text"
                className="pt-input"
                style={fullInputStyle}
                value={portCapital}
                onChange={(e) => setPortCapital(e.target.value)}
                placeholder="1,000,000"
              />
            </div>
          </div>

          {/* 基准指数 */}
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            <label style={fieldLabelStyle}>{t("portfolioTrading.backtest.benchmark")}</label>
            <select
              className="pt-select"
              style={fullInputStyle}
              value={portBenchmark}
              onChange={(e) => setPortBenchmark(e.target.value)}
            >
              {BENCHMARKS.map((b) => (
                <option key={b} value={b}>{b}</option>
              ))}
            </select>
          </div>

          {/* 手续费滑点（% 后缀） */}
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            <label style={fieldLabelStyle}>{t("portfolioTrading.backtest.commission")}</label>
            <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <input
                type="text"
                className="pt-input"
                style={{ flex: 1 }}
                value={portCommission}
                onChange={(e) => setPortCommission(e.target.value)}
              />
              <span style={{ fontSize: 13, color: "var(--pt-muted-foreground)" }}>%</span>
            </div>
          </div>

          {/* 当前执行策略是服务端快照的唯一来源，不提供无效覆盖开关。 */}
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            <label style={fieldLabelStyle}>{t("portfolioTrading.backtest.useStrategy")}</label>
            <div
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                padding: "10px 12px",
                borderRadius: "var(--pt-radius-md)",
                background: "var(--pt-surface-3)",
              }}
            >
              <span style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 13, color: "var(--pt-foreground)" }}>
                <CheckCircle2 size={14} style={{ color: "var(--pt-primary)" }} />
                {t("portfolioTrading.backtest.currentStrategy")}
              </span>
              <span className="pt-tag pt-tag-success">已应用</span>
            </div>
            {/* 来源标签（best-effort） */}
            {sourceStatus && (
              <span
                className={`pt-tag ${sourceStatus.enabled ? "pt-tag-success" : "pt-tag-warning"}`}
                style={{ alignSelf: "flex-start", marginTop: 4 }}
              >
                {t("portfolioTrading.backtest.sourceLabel")}: {sourceStatus.source_label}
              </span>
            )}
          </div>

          {/* 开始回测按钮 */}
          {!autoTradeEnabled && (
            <div
              style={{
                padding: "8px 10px",
                borderRadius: "var(--pt-radius-md)",
                border: "1px solid var(--pt-state-warning)",
                background: "color-mix(in srgb, var(--pt-state-warning) 10%, transparent)",
                fontSize: 12,
                color: "var(--pt-state-warning)",
                lineHeight: 1.5,
              }}
            >
              {t("portfolioTrading.backtest.autoTradeRequiredHint")}
            </div>
          )}
          <button
            type="button"
            className="pt-btn pt-btn-primary pt-btn-lg"
            style={{ width: "100%", marginTop: 4 }}
            onClick={onRun}
            disabled={running || !autoTradeEnabled}
            title={
              !autoTradeEnabled
                ? t("portfolioTrading.backtest.autoTradeRequiredHint")
                : undefined
            }
          >
            <Play size={16} />
            {running
              ? t("portfolioTrading.backtest.running")
              : t("portfolioTrading.backtest.startRun")}
          </button>
        </div>
      </div>

      {/* ---------- 右侧：结果区 ---------- */}
      <div className="pt-backtest-result-column" style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        {gateState && (
          <div
            role="alert"
            data-testid="backtest-gate-state"
            className="pt-card"
            style={{
              padding: 16,
              border: `1px solid ${gateState.kind === "blocked" ? "var(--pt-state-warning)" : "var(--pt-state-error)"}`,
              background: gateState.kind === "blocked"
                ? "color-mix(in srgb, var(--pt-state-warning) 10%, var(--pt-surface))"
                : "color-mix(in srgb, var(--pt-state-error) 10%, var(--pt-surface))",
            }}
          >
            <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 12 }}>
              <div>
                <h3 style={{ ...sectionTitleStyle, color: gateState.kind === "blocked" ? "var(--pt-state-warning)" : "var(--pt-state-error)" }}>
                  {gateState.kind === "blocked" ? "回测被数据门禁阻断" : "回测执行失败"}
                </h3>
                <p style={{ margin: "8px 0 0", color: "var(--pt-foreground)", lineHeight: 1.5 }}>
                  {gateState.message}
                </p>
                {(gateState.code || gateState.statusCode || gateState.correlationId) && (
                  <div style={{ display: "flex", flexWrap: "wrap", gap: 8, marginTop: 10, fontSize: 12, color: "var(--pt-muted-foreground)" }}>
                    {gateState.code && <code>{gateState.code}</code>}
                    {gateState.statusCode && <span>HTTP {gateState.statusCode}</span>}
                    {gateState.correlationId && <span>追踪 ID: {gateState.correlationId}</span>}
                  </div>
                )}
                {gateState.details !== undefined && (
                  <details style={{ marginTop: 10 }}>
                    <summary style={{ cursor: "pointer", fontSize: 12 }}>查看错误详情</summary>
                    <pre style={{ margin: "8px 0 0", maxHeight: 180, overflow: "auto", whiteSpace: "pre-wrap", fontSize: 11 }}>
                      {displayGateValue(gateState.details)}
                    </pre>
                  </details>
                )}
              </div>
              <button
                type="button"
                className="pt-btn pt-btn-sm pt-btn-outline"
                data-testid="backtest-gate-data-link"
                onClick={() => setResultTab("data")}
              >
                查看数据说明
              </button>
            </div>
          </div>
        )}

        {decisionSnapshot && !gateState && (
          <div className="pt-card" data-testid="backtest-decision-snapshot" style={{ padding: 16 }}>
            <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", gap: 12 }}>
              <h3 style={sectionTitleStyle}>执行快照</h3>
              <span style={{ fontSize: 11, color: "var(--pt-muted-foreground)" }}>只读</span>
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: "8px 16px", marginTop: 12, fontSize: 12 }}>
              {[
                ["snapshot_id", "Snapshot ID"],
                ["id", "Snapshot ID"],
                ["snapshot_hash", "Snapshot Hash"],
                ["factor_model_run_id", "Factor model run"],
                ["factor_set_id", "Factor set"],
                ["rule_id", "Rule"],
                ["rule_version", "Rule version"],
                ["pit_mode", "PIT mode"],
                ["snapshot_type", "Snapshot type"],
                ["effective_from", "Effective from"],
              ].map(([key, label]) => {
                const value = decisionSnapshot[key];
                if (value == null || value === "") return null;
                return (
                  <div key={key} style={{ minWidth: 0 }}>
                    <span style={{ color: "var(--pt-muted-foreground)" }}>{label}: </span>
                    <code style={{ overflowWrap: "anywhere" }}>{displayGateValue(value)}</code>
                  </div>
                );
              })}
            </div>
            {executionParameters && (
              <details style={{ marginTop: 12 }}>
                <summary style={{ cursor: "pointer", fontSize: 12 }}>成本与撮合参数</summary>
                <pre style={{ margin: "8px 0 0", maxHeight: 160, overflow: "auto", whiteSpace: "pre-wrap", fontSize: 11 }}>
                  {displayGateValue(executionParameters)}
                </pre>
              </details>
            )}
            <details style={{ marginTop: 8 }}>
              <summary style={{ cursor: "pointer", fontSize: 12 }}>查看完整快照</summary>
              <pre style={{ margin: "8px 0 0", maxHeight: 220, overflow: "auto", whiteSpace: "pre-wrap", fontSize: 11 }}>
                {displayGateValue(decisionSnapshot)}
              </pre>
            </details>
          </div>
        )}

        {/* 4 指标卡 */}
        <div className="pt-backtest-metric-grid" style={{ display: gateState ? "none" : "grid", gridTemplateColumns: "repeat(4, minmax(0, 1fr))", gap: 12 }}>
          <MetricCard
            label={t("portfolioTrading.backtest.metricAbsoluteReturn")}
            value={fmtPct(metrics.totalReturnPct, true)}
            color={pnlColor(metrics.totalReturnPct)}
          />
          <MetricCard
            label={t("portfolioTrading.backtest.metricMaxDrawdown")}
            value={fmtPct(metrics.maxDrawdownPct)}
            color={pnlColor(metrics.maxDrawdownPct)}
          />
          <MetricCard
            label={t("portfolioTrading.backtest.metricSharpe")}
            value={fmtNum(metrics.sharpe)}
            color="var(--pt-primary)"
          />
          <MetricCard
            label={t("portfolioTrading.backtest.metricWinRate")}
            value={metrics.winRate == null ? "--%" : `${(Number(metrics.winRate) * 100).toFixed(1)}%`}
            color="var(--pt-foreground)"
          />
        </div>

        {/* 累计净值曲线 */}
        <div className="pt-card" style={{ display: gateState ? "none" : undefined, overflow: "hidden" }}>
          <div
            className="pt-backtest-card-header"
            style={{
              padding: "12px 16px",
              borderBottom: "1px solid var(--pt-border)",
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
            }}
          >
            <h3 style={sectionTitleStyle}>
              {t("portfolioTrading.backtest.equityCurveTitle")}
            </h3>
            <div className="pt-backtest-legend" style={{ display: "flex", alignItems: "center", gap: 12 }}>
              <LegendLine color="var(--pt-primary)" label={t("portfolioTrading.backtest.strategyLegend")} />
              <LegendLine color="var(--pt-slate-500)" label={t("portfolioTrading.backtest.benchmarkLegend")} />
            </div>
          </div>
          <div style={{ padding: 16 }}>
            <div
              style={{
                width: "100%",
                height: 240,
                borderRadius: "var(--pt-radius-md)",
                background: "var(--pt-surface-2)",
                position: "relative",
                overflow: "hidden",
              }}
            >
              <EquityCurveSvg equityCurve={latestEquityCurve} trades={latestTrades} initialCapital={initialCapital} />
            </div>
          </div>
        </div>

        <div
          role="tablist"
          aria-label="回测结果"
          style={{ display: gateState ? "none" : "flex", gap: 4, borderBottom: "1px solid var(--pt-border)", overflowX: "auto" }}
        >
          {resultTabs.map(([key, label]) => (
            <button
              key={key}
              type="button"
              role="tab"
              aria-selected={resultTab === key}
              onClick={() => setResultTab(key)}
              className="pt-btn pt-btn-sm"
              style={{
                border: 0,
                borderBottom: resultTab === key ? "2px solid var(--pt-primary)" : "2px solid transparent",
                borderRadius: 0,
                background: "transparent",
                color: resultTab === key ? "var(--pt-primary)" : "var(--pt-muted-foreground)",
              }}
            >
              {label}
            </button>
          ))}
        </div>

        {!gateState && resultTab === "positions" && (
          <div className="pt-card" style={{ overflowX: "auto" }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap", padding: "10px 12px", borderBottom: "1px solid var(--pt-border)" }}>
              <select
                className="pt-input"
                data-testid="backtest-position-status-filter"
                aria-label="持仓状态筛选"
                value={positionStatus}
                onChange={(event) => { setPositionStatus(event.target.value as "" | "OPEN" | "CLOSED"); setPositionPage(1); }}
                style={{ width: 116, height: 30 }}
              >
                <option value="">全部状态</option>
                <option value="OPEN">持仓中</option>
                <option value="CLOSED">已平仓</option>
              </select>
              <input
                className="pt-input pt-mono"
                data-testid="backtest-position-as-of-date"
                aria-label="持仓估值日期"
                type="date"
                value={positionAsOfDate}
                onChange={(event) => { setPositionAsOfDate(event.target.value); setPositionPage(1); }}
                style={{ width: 154, height: 30 }}
              />
              {positionAsOf && <span style={{ fontSize: 12, color: "var(--pt-muted-foreground)" }}>估值截至 {positionAsOf}</span>}
            </div>
            {positionLedgerMode === "LEGACY_TRADE_APPROXIMATION" && (
              <div
                role="note"
                data-testid="backtest-position-legacy-ledger-notice"
                style={{
                  margin: "10px 12px 0",
                  padding: "8px 10px",
                  border: "1px solid rgba(202, 138, 4, 0.45)",
                  background: "rgba(202, 138, 4, 0.08)",
                  color: "var(--pt-foreground)",
                  fontSize: 12,
                  lineHeight: 1.5,
                }}
              >
                <strong>历史近似账本：</strong>
                该回测运行未提供执行事件账本，持仓由历史交易记录近似推导，不能作为成交或执行审计依据。
              </div>
            )}
            {!latestRunId ? (
              <div style={{ padding: 24, color: "var(--pt-muted-foreground)" }}>运行回测后展示该次运行的持仓投影。</div>
            ) : positionsLoading ? (
              <div style={{ padding: 24, color: "var(--pt-muted-foreground)" }}>正在加载持仓变化…</div>
            ) : positionsError ? (
              <div role="alert" style={{ padding: 24, color: "var(--pt-state-error)" }}>{positionsError}</div>
            ) : positionRows.length === 0 ? (
              <div style={{ padding: 24, color: "var(--pt-muted-foreground)" }}>该日期和筛选条件下没有持仓记录。</div>
            ) : (
              <>
                <table className="pt-table" style={{ minWidth: 1500 }}>
                  <thead><tr><th>交易日</th><th>证券</th><th>状态</th><th>期初</th><th>买入变化</th><th>卖出变化</th><th>期末</th><th>估值</th><th>市值</th><th>组合权益</th><th>权重</th><th>买入证据</th><th>卖出证据</th></tr></thead>
                  <tbody>{positionRows.map((position) => (
                    <tr key={`${position.symbol_id}-${position.trade_date}`} data-testid={`backtest-position-row-${position.symbol_id}-${position.trade_date}`}>
                      <td className="pt-mono">{position.trade_date}</td>
                      <td className="pt-mono">#{position.symbol_id}</td>
                      <td>{position.status === "OPEN" ? "持仓中" : "已平仓"}</td>
                      <td className="pt-mono">{fmtNum(position.opening_quantity, 0)}</td>
                      <td className="pt-mono" style={{ color: pnlColor(position.buy_quantity) }}>{fmtNum(position.buy_quantity, 0)}</td>
                      <td className="pt-mono" style={{ color: pnlColor(-position.sell_quantity) }}>{fmtNum(position.sell_quantity, 0)}</td>
                      <td className="pt-mono">{fmtNum(position.closing_quantity, 0)}</td>
                      <td className="pt-mono">{position.mark_price == null ? "--" : `¥${fmtNum(position.mark_price)}`}</td>
                      <td className="pt-mono">¥{fmtMoney(position.market_value)}</td>
                      <td className="pt-mono">¥{fmtMoney(position.portfolio_equity)}</td>
                      <td className="pt-mono">{fmtPct(position.weight)}</td>
                      <td className="pt-mono" title={fmtEvidenceIds(position.buy_evidence_ids)}>{fmtEvidenceIds(position.buy_evidence_ids)}</td>
                      <td className="pt-mono" title={fmtEvidenceIds(position.sell_evidence_ids)}>{fmtEvidenceIds(position.sell_evidence_ids)}</td>
                    </tr>
                  ))}</tbody>
                </table>
                <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, padding: "10px 12px", alignItems: "center" }}>
                  <span style={{ fontSize: 12, color: "var(--pt-muted-foreground)" }}>{positionTotal} 条</span>
                  <button type="button" className="pt-btn pt-btn-sm pt-btn-outline" disabled={positionPage <= 1 || positionsLoading} onClick={() => setPositionPage((page) => Math.max(1, page - 1))}>上一页</button>
                  <span style={{ fontSize: 12 }}>第 {positionPage} 页</span>
                  <button type="button" className="pt-btn pt-btn-sm pt-btn-outline" disabled={positionPage * pageSize >= positionTotal || positionsLoading} onClick={() => setPositionPage((page) => page + 1)}>下一页</button>
                </div>
              </>
            )}
          </div>
        )}

        {resultTab === "data" && (
          <div className="pt-card" data-testid="backtest-data-explanation" style={{ padding: 16, color: "var(--pt-muted-foreground)" }}>
            {gateState ? (
              <>
                <strong style={{ color: "var(--pt-foreground)" }}>当前回测未生成可执行结果。</strong>
                <p style={{ margin: "8px 0 0" }}>{gateState.message}</p>
                <p style={{ margin: "8px 0 0" }}>请根据错误码和详情补齐行情、评分或 PIT 数据后重新运行。</p>
              </>
            ) : (
              <>
                <h3 style={sectionTitleStyle}>数据与执行说明</h3>
                <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(210px, 1fr))", gap: "10px 16px", marginTop: 12, fontSize: 12 }}>
                  <DataExplanationItem label="PIT 模式" value={contractText(pitMode)} />
                  <DataExplanationItem label="数据截止时间" value={contractText(dataCutoff)} />
                  <DataExplanationItem label="数据来源" value={contractText(sourceType)} />
                  <DataExplanationItem label="基准" value={contractText(contractValue(metadata, "benchmark", "benchmark_code"))} />
                  <DataExplanationItem label="基准状态" value={contractText(benchmarkStatus)} />
                  <DataExplanationItem label="基准缺口" value={benchmarkGapDays == null ? "--" : `缺口 ${contractText(benchmarkGapDays)} 天`} />
                  <DataExplanationItem label="执行快照" value={contractText(snapshotId)} />
                  <DataExplanationItem label="快照校验值" value={contractText(snapshotHash)} />
                  <DataExplanationItem label="因子模型" value={contractText(modelRunId)} />
                  <DataExplanationItem label="因子集合" value={contractText(factorSetId)} />
                  <DataExplanationItem label="结果可复现性" value={reproducibilityText} />
                  {Boolean(reproducibilityReason) && (
                    <DataExplanationItem label="不可复现原因" value={contractText(reproducibilityReason)} />
                  )}
                </div>
                {metadataWarnings.length > 0 && (
                  <details style={{ marginTop: 14 }}>
                    <summary style={{ cursor: "pointer", fontSize: 12 }}>数据告警与说明（{metadataWarnings.length}）</summary>
                    <pre style={{ margin: "8px 0 0", maxHeight: 180, overflow: "auto", whiteSpace: "pre-wrap", fontSize: 11 }}>
                      {displayGateValue(metadataWarnings)}
                    </pre>
                  </details>
                )}
                <p style={{ margin: "14px 0 0", fontSize: 12, lineHeight: 1.5 }}>
                  基准缺失或不完整仅影响相对指标；系统不会生成虚拟基准曲线，也不会把数据阻断误作正常空结果。
                </p>
              </>
            )}
          </div>
        )}

        {!gateState && resultTab === "trades" && (
          <div className="pt-card" style={{ overflowX: "auto" }}>
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: 8,
                flexWrap: "wrap",
                padding: "10px 12px",
                borderBottom: "1px solid var(--pt-border)",
              }}
            >
              <select
                className="pt-input"
                data-testid="backtest-trade-action-filter"
                aria-label="交易动作筛选"
                title="交易动作"
                value={tradeAction}
                onChange={(event) => {
                  setTradeAction(event.target.value as TradeActionFilter);
                  setTradePage(1);
                }}
                style={{ width: 104, height: 30 }}
              >
                <option value="">全部动作</option>
                <option value="BUY">买入</option>
                <option value="SELL">卖出</option>
              </select>
              <select
                className="pt-input"
                data-testid="backtest-trade-status-filter"
                aria-label="成交状态筛选"
                title="成交状态"
                value={tradeExecutionStatus}
                onChange={(event) => {
                  setTradeExecutionStatus(event.target.value as TradeExecutionFilter);
                  setTradePage(1);
                }}
                style={{ width: 104, height: 30 }}
              >
                <option value="">全部状态</option>
                <option value="filled">已成交</option>
                <option value="open">持仓中</option>
                <option value="rejected">未成交</option>
              </select>
              <input
                className="pt-input pt-mono"
                data-testid="backtest-trade-symbol-filter"
                aria-label="证券 ID 筛选"
                title="证券 ID"
                inputMode="numeric"
                pattern="[0-9]*"
                placeholder="证券 ID"
                value={tradeSymbol}
                onChange={(event) => {
                  setTradeSymbol(event.target.value.replace(/[^0-9]/g, ""));
                  setTradePage(1);
                }}
                style={{ width: 112, height: 30 }}
              />
              <span style={{ width: 1, alignSelf: "stretch", background: "var(--pt-border)" }} />
              <select
                className="pt-input"
                data-testid="backtest-trade-sort-filter"
                aria-label="排序字段"
                title="排序字段"
                value={tradeSortBy}
                onChange={(event) => {
                  setTradeSortBy(event.target.value as TradeSortField);
                  setTradePage(1);
                }}
                style={{ width: 112, height: 30 }}
              >
                <option value="signal_at">信号时间</option>
                <option value="execution_at">执行时间</option>
                <option value="symbol_id">证券</option>
                <option value="price">成交价</option>
                <option value="quantity">成交数量</option>
                <option value="cost">成本</option>
              </select>
              <button
                type="button"
                className="pt-btn pt-btn-outline pt-btn-sm"
                data-testid="backtest-trade-sort-direction"
                aria-label={tradeSortDir === "asc" ? "升序" : "降序"}
                title={tradeSortDir === "asc" ? "升序" : "降序"}
                onClick={() => {
                  setTradeSortDir((current) => current === "asc" ? "desc" : "asc");
                  setTradePage(1);
                }}
                style={{ width: 32, height: 30, padding: 0 }}
              >
                {tradeSortDir === "asc" ? <ArrowUpAZ size={15} /> : <ArrowDownAZ size={15} />}
              </button>
              <button
                type="button"
                className="pt-btn pt-btn-ghost pt-btn-sm"
                data-testid="backtest-trade-clear-filters"
                aria-label="清除筛选和排序"
                title="清除筛选和排序"
                onClick={() => {
                  setTradeAction("");
                  setTradeExecutionStatus("");
                  setTradeSymbol("");
                  setTradeSortBy("signal_at");
                  setTradeSortDir("desc");
                  setTradePage(1);
                }}
                style={{ width: 32, height: 30, padding: 0 }}
              >
                <RotateCcw size={15} />
              </button>
            </div>
            <table className="pt-table" style={{ minWidth: 1040 }}>
              <thead>
                <tr>
                  <th>信号时间</th>
                  <th>执行时间</th>
                  <th>证券</th>
                  <th>动作</th>
                  <th>触发原因</th>
                  <th>计划/成交</th>
                  <th>计划价</th>
                  <th>成交价</th>
                  <th>成本</th>
                  <th>状态</th>
                  <th>依据</th>
                </tr>
              </thead>
              <tbody>
                {tradesLoading ? (
                  <tr><td colSpan={11} style={{ padding: 24, textAlign: "center", color: "var(--pt-muted-foreground)" }}>正在加载交易流水…</td></tr>
                ) : tradeRows.length === 0 ? (
                  <tr>
                    <td colSpan={11} style={{ padding: 24, textAlign: "center", color: "var(--pt-muted-foreground)" }}>
                      {hasResult ? "本次回测没有形成交易计划" : "运行回测后展示真实交易流水"}
                    </td>
                  </tr>
                ) : tradeRows.map((trade) => {
                  const isSell = Boolean(trade.exit_date);
                  const signalAt = trade.entry_signal_date ?? trade.entry_date ?? "--";
                  const executionAt = isSell ? trade.exit_date : trade.entry_date;
                  const price = isSell ? trade.exit_price : trade.entry_price;
                  const cost = Number(trade.entry_cost ?? 0) + Number(trade.exit_cost ?? 0);
                  const evidenceId = isSell ? trade.exit_evidence_id : trade.decision_evidence_id;
                  const evidenceRunId = isSell ? trade.exit_decision_run_id : trade.entry_decision_run_id;
                  const requestedQuantity = isSell ? trade.exit_requested_quantity : trade.entry_requested_quantity;
                  const filledQuantity = isSell ? trade.exit_filled_quantity : trade.entry_filled_quantity;
                  const remainingQuantity = isSell ? trade.exit_remaining_quantity : trade.entry_remaining_quantity;
                  const orderPlanStatus = isSell ? trade.exit_order_plan_status : trade.entry_order_plan_status;
                  const unfilledReason = isSell ? trade.exit_unfilled_reason : trade.entry_unfilled_reason;
                  const statusLabel = orderPlanStatus === "FILLED"
                    ? "已成交"
                    : orderPlanStatus === "PARTIAL_FILL" || orderPlanStatus === "PARTIAL_FILL_PENDING"
                      ? "部分成交 · 待重试"
                      : orderPlanStatus === "PENDING_RETRY"
                        ? "待重试"
                        : orderPlanStatus === "REJECTED"
                          ? "未成交"
                          : "已成交";
                  return (
                    <tr key={trade.id}>
                      <td className="pt-mono">{String(signalAt).replace("T", " ")}</td>
                      <td className="pt-mono">{executionAt ? String(executionAt).replace("T", " ") : "--"}</td>
                      <td className="pt-mono">#{trade.symbol_id}</td>
                      <td>{isSell ? "卖出" : "买入"}</td>
                      <td title={trade.exit_reason ?? "入场规则通过"}>{trade.exit_reason ?? "入场规则通过"}</td>
                      <td className="pt-mono" title={unfilledReason ? `未成交原因：${unfilledReason}` : undefined}>
                        {fmtNum(filledQuantity ?? trade.quantity, 0)} / {fmtNum(requestedQuantity ?? trade.quantity, 0)}
                        {remainingQuantity != null && Number(remainingQuantity) > 0 ? ` · 余 ${fmtNum(remainingQuantity, 0)}` : ""}
                      </td>
                      <td className="pt-mono" title="来自独立订单计划账本的意向价">
                        {(isSell ? trade.intended_exit_price : trade.intended_entry_price) != null
                          ? `¥${fmtNum(isSell ? trade.intended_exit_price : trade.intended_entry_price)}`
                          : "--"}
                      </td>
                      <td className="pt-mono">{price != null ? `¥${fmtNum(price)}` : "--"}</td>
                      <td className="pt-mono">¥{fmtNum(cost)}</td>
                      <td>{statusLabel}</td>
                      <td title={evidenceId ? `证据 ID: ${evidenceId}` : "该历史回测尚未关联 DecisionEvidence"}>
                        {evidenceId && evidenceRunId ? (
                          <button
                            type="button"
                            className="pt-btn pt-btn-ghost pt-btn-sm"
                            onClick={() => onOpenEvidence({
                              decisionRunId: evidenceRunId,
                              evidenceId,
                              symbolId: trade.symbol_id,
                            })}
                            title={`打开 DecisionRun ${evidenceRunId}（证据 ${evidenceId}）`}
                          >
                            已关联 · 查看证据
                          </button>
                        ) : evidenceId ? "已关联 · 无精确运行 ID" : "--"}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, padding: "10px 12px", alignItems: "center" }}>
              <span style={{ fontSize: 12, color: "var(--pt-muted-foreground)" }}>{tradeTotal} 条</span>
              <button type="button" className="pt-btn pt-btn-sm pt-btn-outline" disabled={tradePage <= 1 || tradesLoading} onClick={() => setTradePage((p) => Math.max(1, p - 1))}>上一页</button>
              <span style={{ fontSize: 12 }}>第 {tradePage} 页</span>
              <button type="button" className="pt-btn pt-btn-sm pt-btn-outline" disabled={tradePage * pageSize >= tradeTotal || tradesLoading} onClick={() => setTradePage((p) => p + 1)}>下一页</button>
            </div>
          </div>
        )}

        {!gateState && resultTab === "rejected" && (
          <div className="pt-card" style={{ overflowX: "auto" }}>
            {!latestRunId ? (
              <div style={{ padding: 24, color: "var(--pt-muted-foreground)" }}>本次回测没有关联 DecisionRun，暂无拒绝证据。</div>
            ) : rejectedLoading ? (
              <div style={{ padding: 24, color: "var(--pt-muted-foreground)" }}>正在加载拒绝证据…</div>
            ) : rejectedRows.length === 0 ? (
              <div style={{ padding: 24, color: "var(--pt-muted-foreground)" }}>本次回测没有 REJECTED / DATA_BLOCKED 记录。</div>
            ) : (
              <>
                <table className="pt-table" style={{ minWidth: 760 }}>
                  <thead><tr><th>交易日</th><th>证券</th><th>动作</th><th>原因</th><th>详情</th><th>证据</th></tr></thead>
                  <tbody>{rejectedRows.map((row) => (
                    <tr key={row.id}>
                      <td className="pt-mono">{row.trade_date}</td>
                      <td className="pt-mono">#{row.symbol_id}</td>
                      <td>{row.action}</td>
                      <td>{row.rejection_reason || row.action_subtype || "--"}</td>
                      <td title={row.rejection_detail || ""}>{row.rejection_detail || "--"}</td>
                      <td>
                        <button
                          type="button"
                          className="pt-btn pt-btn-ghost pt-btn-sm"
                          onClick={() => onOpenEvidence({
                            decisionRunId: row.decision_run_id,
                            evidenceId: row.id,
                            symbolId: row.symbol_id,
                          })}
                        >
                          查看证据
                        </button>
                      </td>
                    </tr>
                  ))}</tbody>
                </table>
                <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, padding: "10px 12px", alignItems: "center" }}>
                  <span style={{ fontSize: 12, color: "var(--pt-muted-foreground)" }}>{rejectedTotal} 条</span>
                  <button type="button" className="pt-btn pt-btn-sm pt-btn-outline" disabled={rejectedPage <= 1 || rejectedLoading} onClick={() => setRejectedPage((p) => Math.max(1, p - 1))}>上一页</button>
                  <span style={{ fontSize: 12 }}>第 {rejectedPage} 页</span>
                  <button type="button" className="pt-btn pt-btn-sm pt-btn-outline" disabled={rejectedPage * pageSize >= rejectedTotal || rejectedLoading} onClick={() => setRejectedPage((p) => p + 1)}>下一页</button>
                </div>
              </>
            )}
          </div>
        )}

        {/* 水下回撤 + 回测明细 */}
        <div className="pt-backtest-overview-grid" style={{ display: !gateState && resultTab === "overview" ? "grid" : "none", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", gap: 16 }}>
          {/* 水下回撤图 */}
          <div className="pt-card" style={{ overflow: "hidden" }}>
            <div
              style={{
                padding: "12px 16px",
                borderBottom: "1px solid var(--pt-border)",
              }}
            >
              <h3 style={{ ...sectionTitleStyle, fontSize: 13 }}>
                {t("portfolioTrading.backtest.drawdownTitle")}
              </h3>
            </div>
            <div style={{ padding: 16 }}>
              <div
                style={{
                  width: "100%",
                  height: 120,
                  borderRadius: "var(--pt-radius-md)",
                  background: "var(--pt-surface-2)",
                  position: "relative",
                  overflow: "hidden",
                }}
              >
                <DrawdownSvg equityCurve={latestEquityCurve} />
              </div>
            </div>
          </div>

          {/* 绩效统计 */}
          <div className="pt-card" style={{ overflow: "hidden" }}>
            <div
              style={{
                padding: "12px 16px",
                borderBottom: "1px solid var(--pt-border)",
              }}
            >
              <h3 style={{ ...sectionTitleStyle, fontSize: 13 }}>
                绩效统计
              </h3>
            </div>
            <div style={{ padding: 16, display: "flex", flexDirection: "column", gap: 10 }}>
              <DetailRow
                label={t("portfolioTrading.backtest.detailAnnualReturn")}
                value={fmtPct(metrics.annualReturnPct, true)}
                color={pnlColor(metrics.annualReturnPct)}
              />
              <DetailRow
                label={t("portfolioTrading.backtest.detailVolatility")}
                value={
                  metrics.annualVolatility != null
                    ? `${Number(metrics.annualVolatility).toFixed(1)}%`
                    : "--%"
                }
                color="var(--pt-foreground)"
              />
              <DetailRow
                label={t("portfolioTrading.backtest.detailSharpe")}
                value={fmtNum(metrics.sharpe)}
                color="var(--pt-primary)"
              />
              <DetailRow
                label={t("portfolioTrading.backtest.detailWinRate")}
                value={metrics.winRate == null ? "--" : `${(Number(metrics.winRate) * 100).toFixed(1)}%`}
                color="var(--pt-foreground)"
              />
              <DetailRow
                label={t("portfolioTrading.backtest.detailProfitFactor")}
                value={fmtNum(metrics.profitFactor)}
                color={pnlColor(metrics.profitFactor)}
              />
              <DetailRow
                label={t("portfolioTrading.backtest.detailMaxDrawdown")}
                value={fmtPct(metrics.maxDrawdownPct)}
                color="var(--pt-state-error)"
              />
              <DetailRow
                label={t("portfolioTrading.backtest.detailDrawdownDays")}
                value={
                  metrics.drawdownDays != null
                    ? `${Math.round(Number(metrics.drawdownDays))}`
                    : metrics.avgHoldingDays != null
                      ? `${Math.round(Number(metrics.avgHoldingDays))}`
                      : "--"
                }
                color="var(--pt-foreground)"
              />
              <DetailRow
                label={t("portfolioTrading.backtest.detailTurnover")}
                value={
                  metrics.turnoverRate != null
                    ? `${Number(metrics.turnoverRate).toFixed(2)}x/年`
                    : "--"
                }
                color="var(--pt-foreground)"
              />
            </div>
          </div>
        </div>

        {/* 无结果占位提示 */}
        {!hasResult && !running && !gateState && (
          <div
            style={{
              padding: 16,
              borderRadius: "var(--pt-radius-md)",
              background: "var(--pt-surface-2)",
              textAlign: "center",
              fontSize: 12,
              color: "var(--pt-muted-foreground)",
            }}
          >
            {t("portfolioTrading.backtest.noResultTip")}
          </div>
        )}
      </div>
    </div>
  );
};

/* ------------------------------------------------------------------ */
/* SubTask 7.3: 单股/指标回测面板                                      */
/* ------------------------------------------------------------------ */

interface SingleBacktestPanelProps {
  singleSymbol: string;
  setSingleSymbol: (v: string) => void;
  singleStart: string;
  setSingleStart: (v: string) => void;
  singleEnd: string;
  setSingleEnd: (v: string) => void;
  singleRangePreset: BacktestRangePreset | null;
  setSingleRangePreset: (preset: BacktestRangePreset | null) => void;
  onApplyRangePreset: (preset: BacktestRangePreset) => void;
  singleIndicator: string;
  setSingleIndicator: (v: string) => void;
  singleCapital: string;
  setSingleCapital: (v: string) => void;
  singleCommission: string;
  setSingleCommission: (v: string) => void;
  running: boolean;
  result: BacktestRun | null;
  onRun: () => void;
  /** P1-FIX: 自动交易关闭时按钮禁用 */
  autoTradeEnabled: boolean;
}

const SingleBacktestPanel: React.FC<SingleBacktestPanelProps> = ({
  singleSymbol,
  setSingleSymbol,
  singleStart,
  setSingleStart,
  singleEnd,
  setSingleEnd,
  singleRangePreset,
  setSingleRangePreset,
  onApplyRangePreset,
  singleIndicator,
  setSingleIndicator,
  singleCapital,
  setSingleCapital,
  singleCommission,
  setSingleCommission,
  running,
  result,
  onRun,
  autoTradeEnabled,
}) => {
  const retPct = result?.total_return_pct ?? null;
  const ddPct = result?.max_drawdown_pct ?? null;
  const sharpe = result?.sharpe_ratio ?? null;
  const winRate = result?.win_rate ?? null;
  const trades: BacktestTrade[] = result?.trades ?? [];
  return (
    <div className="pt-backtest-layout pt-backtest-layout-single" style={{ display: "grid", gridTemplateColumns: "minmax(0, 320px) minmax(0, 1fr)", gap: 16 }}>
      {/* ---------- 左侧：单股回测配置 ---------- */}
      <div className="pt-card" style={{ overflow: "hidden" }}>
        <div
          style={{
            padding: "12px 16px",
            borderBottom: "1px solid var(--pt-border)",
          }}
        >
          <h3 style={sectionTitleStyle}>{t("portfolioTrading.backtest.singleConfigTitle")}</h3>
          <p style={sectionSubStyle}>{t("portfolioTrading.backtest.singleConfigSub")}</p>
        </div>
        <div style={{ padding: 16, display: "flex", flexDirection: "column", gap: 16 }}>
          {/* 标的代码（搜索） */}
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            <label style={fieldLabelStyle}>{t("portfolioTrading.backtest.symbolCode")}</label>
            <div style={{ position: "relative" }}>
              <input
                type="text"
                className="pt-input"
                style={{ ...fullInputStyle, paddingLeft: 32 }}
                value={singleSymbol}
                onChange={(e) => setSingleSymbol(e.target.value)}
                placeholder={t("portfolioTrading.backtest.symbolPlaceholder")}
              />
              <Search
                size={14}
                style={{
                  position: "absolute",
                  left: 10,
                  top: "50%",
                  transform: "translateY(-50%)",
                  color: "var(--pt-muted-foreground)",
                }}
              />
            </div>
          </div>

          {/* 回测区间 */}
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            <label style={fieldLabelStyle}>{t("portfolioTrading.backtest.dateRange")}</label>
            <BacktestDateRangeField
              start={singleStart}
              end={singleEnd}
              activePreset={singleRangePreset}
              onDatesChange={(start, end) => {
                setSingleStart(start);
                setSingleEnd(end);
              }}
              onPresetChange={setSingleRangePreset}
              onApplyPreset={onApplyRangePreset}
              testId="single-backtest-range"
            />
          </div>

          {/* 指标类型 */}
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            <label style={fieldLabelStyle}>{t("portfolioTrading.backtest.indicatorType")}</label>
            <select
              className="pt-select"
              style={fullInputStyle}
              value={singleIndicator}
              onChange={(e) => setSingleIndicator(e.target.value)}
            >
              {INDICATORS.map((i) => (
                <option key={i} value={i}>{i}</option>
              ))}
            </select>
          </div>

          {/* 初始资金 */}
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            <label style={fieldLabelStyle}>{t("portfolioTrading.backtest.initialCapital")}</label>
            <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <span style={{ fontSize: 13, color: "var(--pt-muted-foreground)" }}>¥</span>
              <input
                type="text"
                className="pt-input"
                style={fullInputStyle}
                value={singleCapital}
                onChange={(e) => setSingleCapital(e.target.value)}
                placeholder="100,000"
              />
            </div>
          </div>

          {/* 手续费 */}
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            <label style={fieldLabelStyle}>{t("portfolioTrading.backtest.commission")}</label>
            <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <input
                type="text"
                className="pt-input"
                style={{ flex: 1 }}
                value={singleCommission}
                onChange={(e) => setSingleCommission(e.target.value)}
              />
              <span style={{ fontSize: 13, color: "var(--pt-muted-foreground)" }}>%</span>
            </div>
          </div>

          {/* 开始单股回测按钮 */}
          {!autoTradeEnabled && (
            <div
              style={{
                padding: "8px 10px",
                borderRadius: "var(--pt-radius-md)",
                border: "1px solid var(--pt-state-warning)",
                background: "color-mix(in srgb, var(--pt-state-warning) 10%, transparent)",
                fontSize: 12,
                color: "var(--pt-state-warning)",
                lineHeight: 1.5,
              }}
            >
              {t("portfolioTrading.backtest.autoTradeRequiredHint")}
            </div>
          )}
          <button
            type="button"
            className="pt-btn pt-btn-primary pt-btn-lg"
            style={{ width: "100%", marginTop: 4 }}
            onClick={onRun}
            disabled={running || !autoTradeEnabled}
            title={
              !autoTradeEnabled
                ? t("portfolioTrading.backtest.autoTradeRequiredHint")
                : undefined
            }
          >
            <Play size={16} />
            {running
              ? t("portfolioTrading.backtest.running")
              : t("portfolioTrading.backtest.startSingleRun")}
          </button>
        </div>
      </div>

      {/* ---------- 右侧：结果区 ---------- */}
      <div className="pt-backtest-result-column" style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        {/* 4 指标卡（来自真实回测结果，无结果显示 --） */}
        <div className="pt-backtest-metric-grid" style={{ display: "grid", gridTemplateColumns: "repeat(4, minmax(0, 1fr))", gap: 12 }}>
          <MetricCard
            label={t("portfolioTrading.backtest.metricAbsoluteReturn")}
            value={fmtPct(retPct, true)}
            color={pnlColor(retPct)}
          />
          <MetricCard
            label={t("portfolioTrading.backtest.metricMaxDrawdown")}
            value={fmtPct(ddPct)}
            color={pnlColor(ddPct)}
          />
          <MetricCard
            label={t("portfolioTrading.backtest.metricSharpe")}
            value={fmtNum(sharpe)}
            color="var(--pt-primary)"
          />
          <MetricCard
            label={t("portfolioTrading.backtest.metricWinRate")}
            value={winRate == null ? "--%" : `${(Number(winRate) * 100).toFixed(1)}%`}
            color="var(--pt-foreground)"
          />
        </div>

        {/* 股价与指标信号图 */}
        <div className="pt-card" style={{ overflow: "hidden" }}>
          <div
            className="pt-backtest-card-header"
            style={{
              padding: "12px 16px",
              borderBottom: "1px solid var(--pt-border)",
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
            }}
          >
            <h3 style={sectionTitleStyle}>{t("portfolioTrading.backtest.signalChartTitle")}</h3>
            <div className="pt-backtest-legend" style={{ display: "flex", alignItems: "center", gap: 12 }}>
              <LegendLine color="var(--pt-primary)" label={t("portfolioTrading.backtest.priceLegend")} />
              <LegendDot color="#22c55e" label={t("portfolioTrading.backtest.buySignal")} />
              <LegendDot color="#ef4444" label={t("portfolioTrading.backtest.sellSignal")} />
            </div>
          </div>
          <div style={{ padding: 16 }}>
            <div
              style={{
                width: "100%",
                height: 280,
                borderRadius: "var(--pt-radius-md)",
                background: "var(--pt-surface-2)",
                position: "relative",
                overflow: "hidden",
              }}
            >
              <SignalChartSvg />
            </div>
          </div>
        </div>

        {/* 交易明细列表 */}
        <div className="pt-card" style={{ overflowX: "auto" }}>
          <div
            style={{
              padding: "12px 16px",
              borderBottom: "1px solid var(--pt-border)",
            }}
          >
            <h3 style={{ ...sectionTitleStyle, fontSize: 13 }}>
              {t("portfolioTrading.backtest.tradeDetailTitle")}
            </h3>
          </div>
          <div style={{ padding: 12 }}>
            <table className="pt-table" style={{ minWidth: 560 }}>
              <thead>
                <tr>
                  <th>{t("portfolioTrading.backtest.colDate")}</th>
                  <th>{t("portfolioTrading.backtest.colDirection")}</th>
                  <th>{t("portfolioTrading.backtest.colPrice")}</th>
                  <th>{t("portfolioTrading.backtest.colQuantity")}</th>
                  <th>{t("portfolioTrading.backtest.colPnL")}</th>
                </tr>
              </thead>
              <tbody>
                {trades.length === 0 ? (
                  <tr>
                    <td colSpan={5} style={{ textAlign: "center", padding: "24px 0", color: "var(--pt-muted-foreground)" }}>
                      {result ? t("portfolioTrading.backtest.noResultTip") : t("portfolioTrading.backtest.historyEmpty")}
                    </td>
                  </tr>
                ) : (
                  trades.map((tr) => {
                    const isBuy = !tr.exit_date;
                    return (
                      <tr key={tr.id}>
                        <td style={{ color: "var(--pt-muted-foreground)" }}>
                          {(tr.exit_date ?? tr.entry_date ?? "").replace("T", " ").slice(0, 10)}
                        </td>
                        <td>
                          <span
                            style={{
                              color: isBuy ? "#22c55e" : "#ef4444",
                              fontWeight: 500,
                            }}
                          >
                            {isBuy
                              ? t("portfolioTrading.backtest.dirBuy")
                              : t("portfolioTrading.backtest.dirSell")}
                          </span>
                        </td>
                        <td className="pt-mono">¥{fmtNum(isBuy ? tr.entry_price : tr.exit_price ?? tr.entry_price)}</td>
                        <td className="pt-mono">{fmtNum(tr.quantity, 0)}</td>
                        <td className="pt-mono" style={{ color: pnlColor(tr.pnl) }}>
                          {tr.pnl == null ? "--" : `${tr.pnl >= 0 ? "+" : ""}¥${fmtNum(tr.pnl)}`}
                        </td>
                      </tr>
                    );
                  })
                )}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  );
};

/* ------------------------------------------------------------------ */
/* SubTask 7.4: 回测历史抽屉                                           */
/* ------------------------------------------------------------------ */

interface HistoryDrawerProps {
  open: boolean;
  onClose: () => void;
  list: BacktestRun[];
  loading: boolean;
  onSelect: (runId: number) => void;
}

const HistoryDrawer: React.FC<HistoryDrawerProps> = ({ open, onClose, list, loading, onSelect }) => {
  return (
    <div
      style={{
        position: "fixed",
        top: 0,
        left: 0,
        width: "100%",
        height: "100%",
        overflow: "hidden",
        zIndex: 1000,
        pointerEvents: open ? "auto" : "none",
        // Keep a closed history drawer out of accessibility trees and E2E
        // locator results; opacity alone still counts as visible to browsers.
        visibility: open ? "visible" : "hidden",
      }}
      aria-hidden={!open}
    >
      {/* 遮罩层 */}
      <div
        onClick={onClose}
        style={{
          position: "absolute",
          top: 0,
          left: 0,
          width: "100%",
          height: "100%",
          background: "rgba(0, 0, 0, 0.6)",
          backdropFilter: "blur(2px)",
          opacity: open ? 1 : 0,
          transition: "opacity 0.25s ease",
        }}
      />
      {/* 抽屉面板 */}
      <div
        style={{
          position: "absolute",
          top: 0,
          right: 0,
          width: 360,
          height: "100%",
          background: "var(--pt-surface-2)",
          borderLeft: "1px solid var(--pt-border)",
          transform: open ? "translateX(0)" : "translateX(100%)",
          transition: "transform 0.3s ease",
          display: "flex",
          flexDirection: "column",
        }}
      >
        {/* 头部 */}
        <div
          style={{
            padding: "12px 16px",
            borderBottom: "1px solid var(--pt-border)",
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
          }}
        >
          <div>
            <h3 style={sectionTitleStyle}>{t("portfolioTrading.backtest.historyTitle")}</h3>
            <p style={sectionSubStyle}>{t("portfolioTrading.backtest.historySub")}</p>
          </div>
          <button
            type="button"
            className="pt-btn pt-btn-ghost pt-btn-sm"
            onClick={onClose}
            aria-label={t("portfolioTrading.backtest.closeHistory")}
          >
            <X size={16} />
          </button>
        </div>

        {/* 列表 */}
        <div
          className="thin-scrollbar"
          style={{
            padding: 12,
            display: "flex",
            flexDirection: "column",
            gap: 8,
            overflowY: "auto",
            flex: 1,
          }}
        >
          {loading && list.length === 0 && (
            <div style={{ padding: 16, textAlign: "center", fontSize: 12, color: "var(--pt-muted-foreground)" }}>
              {t("portfolioTrading.backtest.historyLoading")}
            </div>
          )}
          {!loading && list.length === 0 && (
            <div style={{ padding: 16, textAlign: "center", fontSize: 12, color: "var(--pt-muted-foreground)" }}>
              {t("portfolioTrading.backtest.historyEmpty")}
            </div>
          )}
          {list.map((run) => {
            const ret = run.total_return_pct ?? null;
            // P1-FIX: 日期区间展示兜底：若后端返回倒置区间（例如 2025-12-31 ~ 2024-01-01），
            // 交换起止日期后再展示，避免误导用户。记录异常 flag 用于视觉标记。
            const rawStart = run.start_date ?? "";
            const rawEnd = run.end_date ?? "";
            const inverted = rawStart && rawEnd && rawStart > rawEnd;
            const dispStart = inverted ? rawEnd : rawStart;
            const dispEnd = inverted ? rawStart : rawEnd;
            return (
              <div
                key={run.id}
                onClick={() => onSelect(run.id)}
                style={{
                  padding: "10px 12px",
                  borderRadius: "var(--pt-radius-md)",
                  background: "var(--pt-surface-3)",
                  border: "1px solid transparent",
                  cursor: "pointer",
                  transition: "background 0.15s ease, border-color 0.15s ease",
                }}
                onMouseEnter={(e) => {
                  e.currentTarget.style.background = "var(--pt-surface-4)";
                  e.currentTarget.style.borderColor = "var(--pt-border)";
                }}
                onMouseLeave={(e) => {
                  e.currentTarget.style.background = "var(--pt-surface-3)";
                  e.currentTarget.style.borderColor = "transparent";
                }}
              >
                <div
                  style={{
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "space-between",
                    marginBottom: 4,
                  }}
                >
                  <span style={{ fontSize: 13, fontWeight: 500, color: "var(--pt-foreground)" }}>
                    {run.run_name || `#${run.id}`}
                  </span>
                  <span
                    className="pt-mono"
                    style={{ fontSize: 12, fontWeight: 500, color: pnlColor(ret) }}
                  >
                    {fmtPct(ret, true)}
                  </span>
                </div>
                <div
                  style={{
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "space-between",
                    fontSize: 12,
                    color: "var(--pt-muted-foreground)",
                    gap: 8,
                    flexWrap: "wrap",
                  }}
                >
                  <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                    <Calendar size={11} />
                    {(run.finished_at ?? run.created_at ?? "").replace("T", " ").slice(0, 16)}
                  </span>
                  <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                    <span>
                      {dispStart} ~ {dispEnd}
                    </span>
                    {inverted && (
                      <span
                        className="pt-tag pt-tag-warning"
                        style={{ padding: "0 4px", fontSize: 10 }}
                      >
                        已校正顺序
                      </span>
                    )}
                  </span>
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
};

/* ------------------------------------------------------------------ */
/* 通用子组件                                                          */
/* ------------------------------------------------------------------ */

interface MetricCardProps {
  label: string;
  value: string;
  color: string;
}

const MetricCard: React.FC<MetricCardProps> = ({ label, value, color }) => (
  <div className="pt-card pt-backtest-metric-card" style={{ padding: 12 }}>
    <p style={{ fontSize: 12, color: "var(--pt-muted-foreground)", margin: "0 0 4px 0" }}>{label}</p>
    <p className="pt-mono" style={{ fontSize: 22, fontWeight: 700, color, margin: 0 }}>
      {value}
    </p>
  </div>
);

interface DetailRowProps {
  label: string;
  value: string;
  color: string;
}

const DetailRow: React.FC<DetailRowProps> = ({ label, value, color }) => (
  <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
    <span style={{ fontSize: 12, color: "var(--pt-muted-foreground)" }}>{label}</span>
    <span className="pt-mono" style={{ fontSize: 13, fontWeight: 500, color }}>
      {value}
    </span>
  </div>
);

const DataExplanationItem: React.FC<{ label: string; value: string }> = ({ label, value }) => (
  <div style={{ minWidth: 0 }}>
    <span style={{ color: "var(--pt-muted-foreground)" }}>{label}: </span>
    <code style={{ overflowWrap: "anywhere", color: "var(--pt-foreground)" }}>{value}</code>
  </div>
);

interface LegendLineProps {
  color: string;
  label: string;
}

const LegendLine: React.FC<LegendLineProps> = ({ color, label }) => (
  <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
    <span style={{ width: 12, height: 2, background: color, display: "inline-block" }} />
    <span style={{ fontSize: 12, color: "var(--pt-muted-foreground)" }}>{label}</span>
  </div>
);

interface LegendDotProps {
  color: string;
  label: string;
}

const LegendDot: React.FC<LegendDotProps> = ({ color, label }) => (
  <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
    <span
      style={{
        width: 8,
        height: 8,
        borderRadius: "50%",
        background: color,
        display: "inline-block",
      }}
    />
    <span style={{ fontSize: 12, color: "var(--pt-muted-foreground)" }}>{label}</span>
  </div>
);

/* ------------------------------------------------------------------ */
/* SVG 占位图（一比一取自原型图）                                       */
/* ------------------------------------------------------------------ */

/* ------------------------------------------------------------------ */
/* P2-TDD: 真实数据驱动的 SVG 曲线（替代原硬编码 path）                  */
/* ------------------------------------------------------------------ */

/** 把 (x,y) 点序列连成 SVG polyline path: "M x1 y1 L x2 y2 ..." */
function polylinePath(pts: [number, number][]): string {
  if (pts.length === 0) return "";
  return pts.map(([x, y], i) => (i === 0 ? `M ${x} ${y}` : `L ${x} ${y}`)).join(" ");
}
/** 面积填充：polyline + 向右下/左回到 baseline 再闭合 */
function areaPath(pts: [number, number][], baseY: number, minX: number, maxX: number): string {
  if (pts.length === 0) return "";
  const line = polylinePath(pts);
  return `${line} L ${maxX} ${baseY} L ${minX} ${baseY} Z`;
}

/** Keep SVG coordinates aligned with the rendered viewport instead of stretching a fixed viewBox. */
function useResponsiveSvgViewport(fallbackWidth: number, fallbackHeight: number) {
  const ref = React.useRef<SVGSVGElement>(null);
  const [viewport, setViewport] = useState({ width: fallbackWidth, height: fallbackHeight });

  useEffect(() => {
    const element = ref.current;
    if (!element) return;
    const update = () => {
      const { width, height } = element.getBoundingClientRect();
      if (width > 0 && height > 0) {
        setViewport({ width: Math.round(width), height: Math.round(height) });
      }
    };
    update();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(update);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  return [ref, viewport] as const;
}

function formatAxisDate(date: string, includeYear: boolean): string {
  const parsed = dayjs(date);
  if (!parsed.isValid()) return String(date).slice(0, includeYear ? 7 : 5);
  return parsed.format(includeYear ? "YYYY-MM" : "MM-DD");
}

/** 净值曲线：策略青色 + 面积 + 基准灰色虚线（按 equity_curve 真实值生成）+ 买卖点▲▼ */
interface EquityCurveSvgProps {
  equityCurve: EquityCurvePoint[];
  trades?: (BacktestTrade | any)[];
  initialCapital?: number;  // 初始资金，用于 Y 轴净值显示（equity/initial_capital）
}
const EquityCurveSvg: React.FC<EquityCurveSvgProps> = ({ equityCurve, trades = [], initialCapital }) => {
  const [svgRef, viewport] = useResponsiveSvgViewport(800, 260);
  const idSuffix = React.useId().replace(/:/g, "");
  const { width: W, height: H } = viewport;
  // 刻度/标签需要外边距，否则 x/y tick label 会被裁掉（这是问题 1「刻度没了」根因：原 MARGIN_L=0/MARGIN_B=20，标签画不下）
  const MARGIN_L = 56;   // 左侧 y 轴数值标签占位（≈ "¥123.4万" 的宽度）
  const MARGIN_R = 12;   // 右侧少量留白
  const MARGIN_T = 10;   // 顶部留白，避免 y 轴 max 标签贴边
  const MARGIN_B = 34;   // 底部 x 轴日期标签占位（10px 字高 + 4px 下沉 tick line + 20px 边距）
  const PLOT_W = Math.max(W - MARGIN_L - MARGIN_R, 2);
  const PLOT_H = Math.max(H - MARGIN_T - MARGIN_B, 2);

  // No result means no line: a simulated curve here can be mistaken for an actual backtest.
  if (!equityCurve || equityCurve.length === 0) {
    return (
      <svg ref={svgRef} viewBox={`0 0 ${W} ${H}`} style={{ width: "100%", height: "100%" }} preserveAspectRatio="xMidYMid meet">
        <text x={W / 2} y={H / 2} textAnchor="middle" fontSize="12" fill="var(--pt-muted-foreground)" fontFamily="inherit">
          暂无回测净值数据
        </text>
      </svg>
    );
  }

  // 无数据 → 退回原静态占位 SVG（保持与设计稿一致；这里也给占位 SVG 加了 x/y 刻度，避免「空状态也没刻度」）
  if (!equityCurve || equityCurve.length === 0) {
    const placeholderYMin = 1e6; // 100 万
    const placeholderYMax = 1.06e6; // 106 万
    const initCap = initialCapital && initialCapital > 0 ? initialCapital : placeholderYMin;
    const fmtNav = (v: number) => (v / initCap).toFixed(2);
    const yTicks = [0, 0.25, 0.5, 0.75, 1].map((t) => placeholderYMin + (placeholderYMax - placeholderYMin) * t);
    const xTicks = ["2025-06-27", "2026-01-15", "2026-08-01"];
    return (
        <svg ref={svgRef} viewBox={`0 0 ${W} ${H}`} style={{ width: "100%", height: "100%" }} preserveAspectRatio="xMidYMid meet">
        {/* 网格线 */}
        {[0.25, 0.5, 0.75].map((t, idx) => {
          const y = MARGIN_T + t * PLOT_H;
          return (
            <line
              key={`g-${idx}`}
              x1={MARGIN_L}
              y1={y}
              x2={MARGIN_L + PLOT_W}
              y2={y}
              stroke="var(--pt-surface-3)"
              strokeWidth="1"
              strokeDasharray="4,4"
            />
          );
        })}
        {/* Y 轴刻度（数值标签 + tick） */}
        {yTicks.map((v, i) => {
          const t = 1 - i / (yTicks.length - 1); // 顶部 = max
          const y = MARGIN_T + t * PLOT_H;
          return (
            <g key={`yt-${i}`}>
              <line
                x1={MARGIN_L - 4}
                y1={y}
                x2={MARGIN_L}
                y2={y}
                stroke="var(--pt-surface-4)"
                strokeWidth="1"
              />
              <text
                x={MARGIN_L - 8}
                y={y + 3}
                textAnchor="end"
                fontSize="10"
                fill="var(--pt-muted-foreground)"
                fontFamily="inherit"
              >
                {fmtNav(v)}
              </text>
            </g>
          );
        })}
        {/* X 轴刻度（日期标签 + tick） */}
        {xTicks.map((lbl, i) => {
          const t = i / (xTicks.length - 1);
          const x = MARGIN_L + t * PLOT_W;
          return (
            <g key={`xt-${i}`}>
              <line
                x1={x}
                y1={MARGIN_T + PLOT_H}
                x2={x}
                y2={MARGIN_T + PLOT_H + 4}
                stroke="var(--pt-surface-4)"
                strokeWidth="1"
              />
              <text
                x={x}
                y={MARGIN_T + PLOT_H + 16}
                textAnchor="middle"
                fontSize="10"
                fill="var(--pt-muted-foreground)"
                fontFamily="inherit"
              >
                {lbl}
              </text>
            </g>
          );
        })}
        <defs>
          <linearGradient id="pt-bt-strategy-fill" x1="0%" y1="0%" x2="0%" y2="100%">
            <stop offset="0%" style={{ stopColor: "#06b6d4", stopOpacity: 0.2 }} />
            <stop offset="100%" style={{ stopColor: "#06b6d4", stopOpacity: 0 }} />
          </linearGradient>
        </defs>
        <path
          d={`M${MARGIN_L},${MARGIN_T + PLOT_H * 0.83} Q${MARGIN_L + 40},${MARGIN_T + PLOT_H * 0.81} ${MARGIN_L + 80},${MARGIN_T + PLOT_H * 0.73} T${MARGIN_L + 160},${MARGIN_T + PLOT_H * 0.68} T${MARGIN_L + 240},${MARGIN_T + PLOT_H * 0.58} T${MARGIN_L + 320},${MARGIN_T + PLOT_H * 0.55} T${MARGIN_L + 400},${MARGIN_T + PLOT_H * 0.44} T${MARGIN_L + 480},${MARGIN_T + PLOT_H * 0.33} T${MARGIN_L + 560},${MARGIN_T + PLOT_H * 0.24} T${MARGIN_L + 640},${MARGIN_T + PLOT_H * 0.14} T${MARGIN_L + 720},${MARGIN_T + PLOT_H * 0.06} T${MARGIN_L + PLOT_W},${MARGIN_T + PLOT_H * 0.02} L${MARGIN_L + PLOT_W},${MARGIN_T + PLOT_H} L${MARGIN_L},${MARGIN_T + PLOT_H} Z`}
          fill="url(#pt-bt-strategy-fill)"
        />
        <path
          d={`M${MARGIN_L},${MARGIN_T + PLOT_H * 0.83} Q${MARGIN_L + 40},${MARGIN_T + PLOT_H * 0.81} ${MARGIN_L + 80},${MARGIN_T + PLOT_H * 0.73} T${MARGIN_L + 160},${MARGIN_T + PLOT_H * 0.68} T${MARGIN_L + 240},${MARGIN_T + PLOT_H * 0.58} T${MARGIN_L + 320},${MARGIN_T + PLOT_H * 0.55} T${MARGIN_L + 400},${MARGIN_T + PLOT_H * 0.44} T${MARGIN_L + 480},${MARGIN_T + PLOT_H * 0.33} T${MARGIN_L + 560},${MARGIN_T + PLOT_H * 0.24} T${MARGIN_L + 640},${MARGIN_T + PLOT_H * 0.14} T${MARGIN_L + 720},${MARGIN_T + PLOT_H * 0.06} T${MARGIN_L + PLOT_W},${MARGIN_T + PLOT_H * 0.02}`}
          fill="none"
          stroke="#06b6d4"
          strokeWidth="2"
        />
        <path
          d={`M${MARGIN_L},${MARGIN_T + PLOT_H * 0.84} Q${MARGIN_L + 40},${MARGIN_T + PLOT_H * 0.83} ${MARGIN_L + 80},${MARGIN_T + PLOT_H * 0.79} T${MARGIN_L + 160},${MARGIN_T + PLOT_H * 0.74} T${MARGIN_L + 240},${MARGIN_T + PLOT_H * 0.69} T${MARGIN_L + 320},${MARGIN_T + PLOT_H * 0.66} T${MARGIN_L + 400},${MARGIN_T + PLOT_H * 0.61} T${MARGIN_L + 480},${MARGIN_T + PLOT_H * 0.58} T${MARGIN_L + 560},${MARGIN_T + PLOT_H * 0.53} T${MARGIN_L + 640},${MARGIN_T + PLOT_H * 0.48} T${MARGIN_L + 720},${MARGIN_T + PLOT_H * 0.46} T${MARGIN_L + PLOT_W},${MARGIN_T + PLOT_H * 0.41}`}
          fill="none"
          stroke="#64748b"
          strokeWidth="1.5"
          strokeDasharray="5,3"
        />
      </svg>
    );
  }
  // 基准缺失时必须保持不可用，不能在前端构造虚假对照曲线。
  const eqArr = equityCurve.map((p) => Number(p.equity) || 0);
  const benchmarkAvailable = equityCurve.every(
    (p) => p.benchmark != null && Number.isFinite(Number(p.benchmark)) && Number(p.benchmark) > 0,
  );
  const finalBmArr = benchmarkAvailable
    ? equityCurve.map((p) => Number(p.benchmark))
    : [];

  // Normalize to return percentages so normalized and amount-based backend values render identically.
  const strategyBase = eqArr.find((value) => value > 0) ?? 1;
  const benchmarkBase = finalBmArr.find((value) => value > 0) ?? strategyBase;
  const strategyReturns = eqArr.map((value) => ((value / strategyBase) - 1) * 100);
  const benchmarkReturns = benchmarkAvailable
    ? finalBmArr.map((value) => ((value / benchmarkBase) - 1) * 100)
    : [];
  const rawMin = Math.min(0, ...strategyReturns, ...benchmarkReturns);
  const rawMax = Math.max(0, ...strategyReturns, ...benchmarkReturns);
  const padding = Math.max((rawMax - rawMin) * 0.12, 0.25);
  const vMin = rawMin - padding;
  const vMax = rawMax + padding;
  const span = Math.max(vMax - vMin, 0.5);
  const yScale = (value: number) => MARGIN_T + ((vMax - value) / span) * PLOT_H;
  const xScale = (i: number) =>
    eqArr.length <= 1 ? MARGIN_L + PLOT_W / 2 : MARGIN_L + (i / (eqArr.length - 1)) * PLOT_W;
  const stratPts: [number, number][] = strategyReturns.map((value, i) => [xScale(i), yScale(value)]);
  const bmPts: [number, number][] = benchmarkReturns.map((value, i) => [xScale(i), yScale(value)]);
  const baseY = yScale(0);

  // 2) Y 轴刻度（5 档：min → max 等间隔），X 轴刻度（3 档：首/中/尾）
  const Y_TICKS = 5;
  const yTicks: number[] = Array.from({ length: Y_TICKS }, (_, i) => vMin + (span * i) / (Y_TICKS - 1));
  const initCap = initialCapital && initialCapital > 0 ? initialCapital : (eqArr[0] || 1_000_000);
  const fmtNav = (v: number): string => {
    // 净值格式化：v / initial_capital → 小数，如 0.95、1.00、1.05
    return `${v > 0 ? "+" : ""}${v.toFixed(2)}%`;
  };
  const xTicks: { i: number; label: string }[] = (() => {
    const n = equityCurve.length;
    if (n === 0) return [];
    if (n === 1) return [{ i: 0, label: formatAxisDate(String(equityCurve[0].date ?? ""), true) }];
    const mid = Math.floor((n - 1) / 2);
    const short = (d: string) => String(d).slice(5); // 只取 MM-DD，避免长日期挤在一起
    return [
      { i: 0, label: formatAxisDate(String(equityCurve[0].date ?? ""), true) },
      { i: mid, label: formatAxisDate(String(equityCurve[mid].date ?? ""), true) },
      { i: n - 1, label: formatAxisDate(String(equityCurve[n - 1].date ?? ""), true) },
    ];
  })();

  // 3) 买卖点映射：按 date 精确匹配 equity_curve 索引 → (x, y)
  type EntryExit = { kind: "entry" | "exit"; i: number; symbol_id?: number | string };
  const dateToIdx = new Map<string, number>();
  equityCurve.forEach((p, i) => dateToIdx.set(String(p.date ?? ""), i));
  const marks: EntryExit[] = [];
  for (const t of trades || []) {
    const e = String(t.entry_date ?? "");
    const x = String(t.exit_date ?? "");
    const sid = t.symbol_id;
    if (dateToIdx.has(e)) marks.push({ kind: "entry", i: dateToIdx.get(e)!, symbol_id: sid });
    if (dateToIdx.has(x)) marks.push({ kind: "exit", i: dateToIdx.get(x)!, symbol_id: sid });
  }

  return (
    <svg ref={svgRef} viewBox={`0 0 ${W} ${H}`} style={{ width: "100%", height: "100%" }} preserveAspectRatio="xMidYMid meet">
      {/* 4 条水平网格线 + 2 条垂直网格线（视觉锚点） */}
      {[0.2, 0.4, 0.6, 0.8].map((t, idx) => {
        const y = MARGIN_T + t * PLOT_H;
        return (
          <line
            key={`hg-${idx}`}
            x1={MARGIN_L}
            y1={y}
            x2={MARGIN_L + PLOT_W}
            y2={y}
            stroke="var(--pt-surface-3)"
            strokeWidth="1"
            strokeDasharray="4,4"
          />
        );
      })}
      {/* Y 轴刻度 & 数值标签 */}
      {yTicks.map((v, i) => {
        const y = yScale(v);
        return (
          <g key={`yt-${i}`}>
            <line x1={MARGIN_L - 4} y1={y} x2={MARGIN_L} y2={y} stroke="var(--pt-surface-4)" strokeWidth="1" />
            <text
              x={MARGIN_L - 8}
              y={y + 3}
              textAnchor="end"
              fontSize="10"
              fill="var(--pt-muted-foreground)"
              fontFamily="inherit"
            >
              {fmtNav(v)}
            </text>
          </g>
        );
      })}
      {/* X 轴刻度 & 日期标签 */}
      {xTicks.map(({ i, label }, idx) => {
        const x = xScale(i);
        return (
          <g key={`xt-${idx}`}>
            <line x1={x} y1={MARGIN_T + PLOT_H} x2={x} y2={MARGIN_T + PLOT_H + 4} stroke="var(--pt-surface-4)" strokeWidth="1" />
            <text
              x={x}
              y={MARGIN_T + PLOT_H + 16}
              textAnchor="middle"
              fontSize="10"
              fill="var(--pt-muted-foreground)"
              fontFamily="inherit"
            >
              {label}
            </text>
          </g>
        );
      })}

      <defs>
        <linearGradient id={`pt-bt-strategy-fill-${idSuffix}`} x1="0%" y1="0%" x2="0%" y2="100%">
          <stop offset="0%" style={{ stopColor: "#06b6d4", stopOpacity: 0.22 }} />
          <stop offset="100%" style={{ stopColor: "#06b6d4", stopOpacity: 0 }} />
        </linearGradient>
      </defs>

      {/* 策略面积 + 策略线 */}
      <path
        d={areaPath(stratPts, baseY, xScale(0), xScale(eqArr.length - 1))}
        fill={`url(#pt-bt-strategy-fill-${idSuffix})`}
      />
      <path d={polylinePath(stratPts)} fill="none" stroke="#06b6d4" strokeWidth="2" />
      {/* 基准线（灰色虚线，必然非水平，因为前端兜底也独立生成） */}
      <path
        d={polylinePath(bmPts)}
        fill="none"
        stroke="#64748b"
        strokeWidth="1.5"
        strokeDasharray="5,3"
      />

      {/* 买卖时机标记：entry 绿▲ 在曲线上方（y - 10 上移），exit 红▼ 在曲线下方（y + 10 下移） */}
      {marks.map((m, idx) => {
        const [px, py] = stratPts[m.i];
        if (m.kind === "entry") {
          // 三角形▲ 朝上：顶点 = (px, py - 10)，底边 y = py - 2（高 8px，宽 10px），位置略向上，避免挡曲线
          const topY = py - 11;
          const baseY_ = py - 3;
          return (
            <polygon
              key={`m-e-${idx}`}
              points={`${px},${topY} ${px - 5},${baseY_} ${px + 5},${baseY_}`}
              fill="#22c55e"
              stroke="var(--pt-card)"
              strokeWidth="0.5"
            >
              <title>{`买入 ${m.symbol_id ?? ""}`}</title>
            </polygon>
          );
        }
        // 退出▼ 朝下：顶点 = (px, py + 11)，底边 y = py + 3（高 8px，宽 10px）
        const bottomY = py + 11;
        const baseY_ = py + 3;
        return (
          <polygon
            key={`m-x-${idx}`}
            points={`${px},${bottomY} ${px - 5},${baseY_} ${px + 5},${baseY_}`}
            fill="#ef4444"
            stroke="var(--pt-card)"
            strokeWidth="0.5"
          >
            <title>{`卖出 ${m.symbol_id ?? ""}`}</title>
          </polygon>
        );
      })}
    </svg>
  );
};

/** 水下回撤：红色半透明面积 + 红色描边（按 equity_curve 真实值前端计算 drawdown）+ x/y 轴刻度 */
interface DrawdownSvgProps {
  equityCurve: EquityCurvePoint[];
}
const DrawdownSvg: React.FC<DrawdownSvgProps> = ({ equityCurve }) => {
  const [svgRef, viewport] = useResponsiveSvgViewport(400, 128);
  const idSuffix = React.useId().replace(/:/g, "");
  const { width: W, height: H } = viewport;
  // 画内外边距：y 轴百分比要左侧空间，x 轴 MM-DD 要底部空间
  const MARGIN_L = 40;  // y 轴「-6.1%」宽度约 30px，加 10px 边距
  const MARGIN_R = 8;
  const TOP = 6;        // 0 回撤 y（顶部 6px 透气）
  const BOTTOM = H - 26; // 最大回撤 y（底部留 26px 给 x 轴日期标签 + tick）
  const PLOT_W = Math.max(W - MARGIN_L - MARGIN_R, 2);
  const PLOT_H = Math.max(BOTTOM - TOP, 2);

  if (!equityCurve || equityCurve.length === 0) {
    return (
      <svg ref={svgRef} viewBox={`0 0 ${W} ${H}`} style={{ width: "100%", height: "100%" }} preserveAspectRatio="xMidYMid meet">
        <text x={W / 2} y={H / 2} textAnchor="middle" fontSize="12" fill="var(--pt-muted-foreground)" fontFamily="inherit">
          暂无回撤数据
        </text>
      </svg>
    );
  }

  if (!equityCurve || equityCurve.length === 0) {
    // 占位状态也加刻度，避免空状态没刻度
    const yTickLbls = ["0%", "-3%", "-6%"];
    const xTickLbls = ["06-27", "01-15", "08-01"];
    const yScalePh = (i: number) => TOP + (i / (yTickLbls.length - 1)) * PLOT_H;
    const xScalePh = (t: number) => MARGIN_L + t * PLOT_W;
    return (
        <svg ref={svgRef} viewBox={`0 0 ${W} ${H}`} style={{ width: "100%", height: "100%" }} preserveAspectRatio="xMidYMid meet">
        <defs>
          <linearGradient id="pt-bt-drawdown-fill" x1="0%" y1="0%" x2="0%" y2="100%">
            <stop offset="0%" style={{ stopColor: "#ef4444", stopOpacity: 0.3 }} />
            <stop offset="100%" style={{ stopColor: "#ef4444", stopOpacity: 0.05 }} />
          </linearGradient>
        </defs>
        {/* Y 轴刻度：0% / -3% / -6% */}
        {yTickLbls.map((lbl, i) => {
          const y = yScalePh(i);
          return (
            <g key={`pyt-${i}`}>
              <line x1={MARGIN_L - 4} y1={y} x2={MARGIN_L} y2={y} stroke="var(--pt-surface-4)" strokeWidth="1" />
              <text x={MARGIN_L - 8} y={y + 3} textAnchor="end" fontSize="10"
                fill="var(--pt-muted-foreground)" fontFamily="inherit">{lbl}</text>
            </g>
          );
        })}
        {/* X 轴刻度 */}
        {xTickLbls.map((lbl, i) => {
          const t = i / (xTickLbls.length - 1);
          const x = xScalePh(t);
          return (
            <g key={`pxt-${i}`}>
              <line x1={x} y1={BOTTOM} x2={x} y2={BOTTOM + 4} stroke="var(--pt-surface-4)" strokeWidth="1" />
              <text x={x} y={BOTTOM + 15} textAnchor="middle" fontSize="10"
                fill="var(--pt-muted-foreground)" fontFamily="inherit">{lbl}</text>
            </g>
          );
        })}
        <path
          d={`M${MARGIN_L},${TOP} Q${MARGIN_L + 20},${TOP + 8} ${MARGIN_L + 40},${TOP + 20} T${MARGIN_L + 80},${TOP + 32} T${MARGIN_L + 120},${TOP + 44} T${MARGIN_L + 160},${TOP + 56} T${MARGIN_L + 200},${TOP + 72} T${MARGIN_L + 240},${TOP + 60} T${MARGIN_L + 280},${TOP + 48} T${MARGIN_L + 320},${TOP + 64} T${MARGIN_L + 360},${TOP + 52} T${MARGIN_L + PLOT_W},${TOP + 40} L${MARGIN_L + PLOT_W},${BOTTOM} L${MARGIN_L},${BOTTOM} Z`}
          fill="url(#pt-bt-drawdown-fill)"
        />
        <path
          d={`M${MARGIN_L},${TOP} Q${MARGIN_L + 20},${TOP + 8} ${MARGIN_L + 40},${TOP + 20} T${MARGIN_L + 80},${TOP + 32} T${MARGIN_L + 120},${TOP + 44} T${MARGIN_L + 160},${TOP + 56} T${MARGIN_L + 200},${TOP + 72} T${MARGIN_L + 240},${TOP + 60} T${MARGIN_L + 280},${TOP + 48} T${MARGIN_L + 320},${TOP + 64} T${MARGIN_L + 360},${TOP + 52} T${MARGIN_L + PLOT_W},${TOP + 40}`}
          fill="none"
          stroke="#ef4444"
          strokeWidth="1.5"
        />
        <line x1={MARGIN_L} y1={TOP} x2={MARGIN_L + PLOT_W} y2={TOP} stroke="var(--pt-surface-4)" strokeWidth="1" />
      </svg>
    );
  }

  // 计算 drawdown: running peak → dd = (peak - equity) / peak (正数表示水下深度比例)
  let peak = -Infinity;
  const dds: number[] = equityCurve.map((p) => {
    const v = Number(p.equity) || 0;
    if (v > peak) peak = v;
    return peak <= 0 ? 0 : (peak - v) / peak;
  });
  const maxDd = Math.max(...dds, 0);
  // Retain headroom and a 1% minimum scale so small drawdowns are not visually exaggerated.
  const span = Math.max(maxDd * 1.12, 0.01);
  const yScale = (dd: number) => TOP + (dd / span) * PLOT_H;
  const xScale = (i: number) =>
    dds.length <= 1 ? MARGIN_L + PLOT_W / 2 : MARGIN_L + (i / (dds.length - 1)) * PLOT_W;
  const ddPts: [number, number][] = dds.map((d, i) => [xScale(i), yScale(d)]);

  // Y 轴刻度：3 档（0% / -max/2 / -max）
  const yTicksPct = [0, span / 2, span];
  const fmtPct1 = (v: number) => `-${(v * 100).toFixed(1)}%`;
  // X 轴刻度：3 档 首/中/尾 MM-DD
  const xTicks: { i: number; label: string }[] = (() => {
    const n = equityCurve.length;
    if (n === 0) return [];
    if (n === 1) return [{ i: 0, label: formatAxisDate(String(equityCurve[0].date ?? ""), true) }];
    const mid = Math.floor((n - 1) / 2);
    const short = (d: string) => String(d).slice(5);
    return [
      { i: 0, label: formatAxisDate(String(equityCurve[0].date ?? ""), true) },
      { i: mid, label: formatAxisDate(String(equityCurve[mid].date ?? ""), true) },
      { i: n - 1, label: formatAxisDate(String(equityCurve[n - 1].date ?? ""), true) },
    ];
  })();

  return (
    <svg ref={svgRef} viewBox={`0 0 ${W} ${H}`} style={{ width: "100%", height: "100%" }} preserveAspectRatio="xMidYMid meet">
      {/* Y 轴刻度 & 百分比标签 */}
      {yTicksPct.map((v, i) => {
        const y = yScale(v);
        return (
          <g key={`dyt-${i}`}>
            <line x1={MARGIN_L - 4} y1={y} x2={MARGIN_L} y2={y} stroke="var(--pt-surface-4)" strokeWidth="1" />
            <text x={MARGIN_L - 8} y={y + 3} textAnchor="end" fontSize="10"
              fill="var(--pt-muted-foreground)" fontFamily="inherit">
              {i === 0 ? "0%" : fmtPct1(v)}
            </text>
          </g>
        );
      })}
      {/* X 轴刻度 & 日期标签 */}
      {xTicks.map(({ i, label }, idx) => {
        const x = xScale(i);
        return (
          <g key={`dxt-${idx}`}>
            <line x1={x} y1={BOTTOM} x2={x} y2={BOTTOM + 4} stroke="var(--pt-surface-4)" strokeWidth="1" />
            <text x={x} y={BOTTOM + 15} textAnchor="middle" fontSize="10"
              fill="var(--pt-muted-foreground)" fontFamily="inherit">{label}</text>
          </g>
        );
      })}

      <defs>
        <linearGradient id={`pt-bt-drawdown-fill-${idSuffix}`} x1="0%" y1="0%" x2="0%" y2="100%">
          <stop offset="0%" style={{ stopColor: "#ef4444", stopOpacity: 0.3 }} />
          <stop offset="100%" style={{ stopColor: "#ef4444", stopOpacity: 0.05 }} />
        </linearGradient>
      </defs>
      {/* 回撤面积（从 dd 曲线向下闭合到底部基线） */}
      <path
        d={areaPath(ddPts, BOTTOM, xScale(0), xScale(dds.length - 1))}
        fill={`url(#pt-bt-drawdown-fill-${idSuffix})`}
      />
      {/* 回撤曲线描边 */}
      <path d={polylinePath(ddPts)} fill="none" stroke="#ef4444" strokeWidth="1.5" />
      {/* 顶部 0 回撤基线 */}
      <line x1={MARGIN_L} y1={TOP} x2={MARGIN_L + PLOT_W} y2={TOP} stroke="var(--pt-surface-4)" strokeWidth="1" />
    </svg>
  );
};

/** 股价与指标信号图：青色折线 + 买点绿色三角↑ + 卖点红色三角↓ */
const SignalChartSvg: React.FC = () => (
  <svg viewBox="0 0 800 260" style={{ width: "100%", height: "100%" }} preserveAspectRatio="xMidYMid meet">
    <line x1="0" y1="65" x2="800" y2="65" stroke="var(--pt-surface-3)" strokeWidth="1" strokeDasharray="4,4" />
    <line x1="0" y1="130" x2="800" y2="130" stroke="var(--pt-surface-3)" strokeWidth="1" strokeDasharray="4,4" />
    <line x1="0" y1="195" x2="800" y2="195" stroke="var(--pt-surface-3)" strokeWidth="1" strokeDasharray="4,4" />
    <path
      d="M0,180 Q60,160 120,140 T240,150 T360,120 T480,100 T600,110 T720,80 T800,60"
      fill="none"
      stroke="#06b6d4"
      strokeWidth="2"
    />
    {/* 买点：绿色三角↑ */}
    <polygon points="120,148 115,158 125,158" fill="#22c55e" />
    <polygon points="360,128 355,138 365,138" fill="#22c55e" />
    <polygon points="600,118 595,128 605,128" fill="#22c55e" />
    {/* 卖点：红色三角↓ */}
    <polygon points="240,142 235,132 245,132" fill="#ef4444" />
    <polygon points="480,92 475,82 485,82" fill="#ef4444" />
  </svg>
);

export default PortfolioBacktestCenter;
