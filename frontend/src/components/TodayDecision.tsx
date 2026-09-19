import { useEffect, useCallback, useMemo, useState } from "react";
import { Alert, Button, Card, Col, Empty, Modal, Progress, Row, Space, Tag, Tooltip, Typography } from "antd";
import {
  AlertOutlined,
  ArrowRightOutlined,
  BarChartOutlined,
  CheckCircleOutlined,
  ClearOutlined,
  DatabaseOutlined,
  ExperimentOutlined,
  FireOutlined,
  InfoCircleOutlined,
  LoadingOutlined,
  LockOutlined,
  PlayCircleOutlined,
  ReloadOutlined,
  SafetyOutlined,
  StopOutlined,
  WarningOutlined,
} from "@ant-design/icons";
import { api, type FactorOverview, type ScoringOverview } from "../api/client";
import { actionLabel, enumLabel, stageLabel, t, template } from "../i18n";
import { useApp } from "../context/AppContext";
import { navigateToResearch } from "../utils/sourceContext";
import { baseOpportunityScoreValue, formatRelativeTime, opportunityScoreValue, score, withFinalOpportunityScore } from "../utils/format";
import type {
  AutoSimulationPreflightResponse,
  DataHealth,
  DataHealthBarIssue,
  MacroOverview,
  MarketEvent,
  PortfolioStatePermissions,
  PortfolioStatus,
  PortfolioStatusResponse,
  WorkbenchCandidate,
} from "../types";
// WP1-FIX.1：今日决策接入 OpportunityStatusBadges（compact 模式）
import { OpportunityStatusBadges } from "./opportunity/OpportunityStatusBadges";

const { Text } = Typography;

const TODO_ACTIONS = new Set(["open", "buy_dip", "hold"]);

/* ============================================================================
 * FR-P0-10 / FR-P1-8a HG1 门禁横幅 9 状态配置（与 GovernanceTab 色板/9×4 矩阵严格对齐）
 * alert_type → antd Alert.type
 * permissions: 覆盖默认 9×4 矩阵中的非对齐值（仅当 API 未返回时使用）
 * ========================================================================== */
interface HG1BannerConfig {
  alert_type: "success" | "warning" | "error" | "info";
  icon: React.ReactNode;
  title_key: string;              // i18n key for banner title（未翻译兜底=硬编码）
  desc_key: string;               // i18n key for description
  default_perm: PortfolioStatePermissions;
}
const HG1_FALLBACK_PERM_READY: PortfolioStatePermissions = {
  allow_new_buys: true, allow_risk_exits: true, allow_auto_recovery: false, requires_manual_ack: false,
};
const HG1_STATUS_CFG: Record<PortfolioStatus | string, HG1BannerConfig> = {
  PENDING_INITIAL_REVIEW: {
    alert_type: "warning", icon: <WarningOutlined />,
    title_key: "hg1.banner.PENDING_INITIAL_REVIEW.title",
    desc_key: "hg1.banner.PENDING_INITIAL_REVIEW.desc",
    default_perm: { allow_new_buys: false, allow_risk_exits: false, allow_auto_recovery: false, requires_manual_ack: true },
  },
  READY: {
    alert_type: "success", icon: <SafetyOutlined />,
    title_key: "hg1.banner.READY.title",
    desc_key: "hg1.banner.READY.desc",
    default_perm: HG1_FALLBACK_PERM_READY,
  },
  RUNNING_AUTO_SIMULATION: {
    alert_type: "info", icon: <LoadingOutlined />,
    title_key: "hg1.banner.RUNNING_AUTO_SIMULATION.title",
    desc_key: "hg1.banner.RUNNING_AUTO_SIMULATION.desc",
    default_perm: { allow_new_buys: true, allow_risk_exits: true, allow_auto_recovery: false, requires_manual_ack: false },
  },
  RUNNING_BACKTEST: {
    alert_type: "info", icon: <ExperimentOutlined />,
    title_key: "hg1.banner.RUNNING_BACKTEST.title",
    desc_key: "hg1.banner.RUNNING_BACKTEST.desc",
    default_perm: { allow_new_buys: true, allow_risk_exits: true, allow_auto_recovery: false, requires_manual_ack: false },
  },
  DATA_INCOMPLETE_PAUSED: {
    alert_type: "warning", icon: <WarningOutlined />,
    title_key: "hg1.banner.DATA_INCOMPLETE_PAUSED.title",
    desc_key: "hg1.banner.DATA_INCOMPLETE_PAUSED.desc",
    default_perm: { allow_new_buys: false, allow_risk_exits: true, allow_auto_recovery: true, requires_manual_ack: false },
  },
  RECONCILIATION_BLOCKED: {
    alert_type: "error", icon: <StopOutlined />,
    title_key: "hg1.banner.RECONCILIATION_BLOCKED.title",
    desc_key: "hg1.banner.RECONCILIATION_BLOCKED.desc",
    default_perm: { allow_new_buys: false, allow_risk_exits: true, allow_auto_recovery: false, requires_manual_ack: true },
  },
  MODEL_INACTIVE: {
    alert_type: "warning", icon: <AlertOutlined />,
    title_key: "hg1.banner.MODEL_INACTIVE.title",
    desc_key: "hg1.banner.MODEL_INACTIVE.desc",
    default_perm: { allow_new_buys: false, allow_risk_exits: true, allow_auto_recovery: false, requires_manual_ack: false },
  },
  SCORE_STALE: {
    alert_type: "warning", icon: <WarningOutlined />,
    title_key: "hg1.banner.SCORE_STALE.title",
    desc_key: "hg1.banner.SCORE_STALE.desc",
    default_perm: { allow_new_buys: false, allow_risk_exits: true, allow_auto_recovery: true, requires_manual_ack: false },
  },
  INTERRUPTED: {
    alert_type: "warning", icon: <AlertOutlined />,
    title_key: "hg1.banner.INTERRUPTED.title",
    desc_key: "hg1.banner.INTERRUPTED.desc",
    default_perm: { allow_new_buys: false, allow_risk_exits: true, allow_auto_recovery: true, requires_manual_ack: false },
  },
  ADMIN_PAUSED: {
    alert_type: "error", icon: <LockOutlined />,
    title_key: "hg1.banner.ADMIN_PAUSED.title",
    desc_key: "hg1.banner.ADMIN_PAUSED.desc",
    default_perm: { allow_new_buys: false, allow_risk_exits: false, allow_auto_recovery: false, requires_manual_ack: true },
  },
};
const HG1_FALLBACK_CFG: HG1BannerConfig = {
  alert_type: "info", icon: <InfoCircleOutlined />,
  title_key: "", desc_key: "",
  default_perm: { allow_new_buys: false, allow_risk_exits: false, allow_auto_recovery: false, requires_manual_ack: true },
};

const GOVERNANCE_CODE_LABELS: Record<string, string> = {
  PENDING_INITIAL_REVIEW: "初始审查中",
  READY: "生产就绪",
  RUNNING_AUTO_SIMULATION: "自动推演进行中",
  RUNNING_BACKTEST: "历史回测进行中",
  RECONCILIATION_BLOCKED: "对账差异阻断",
  SCORE_STALE: "评分已过期",
  DATA_INCOMPLETE_PAUSED: "数据不完整，已暂停",
  MODEL_INACTIVE: "因子模型未激活",
  INTERRUPTED: "任务异常中断",
  ADMIN_PAUSED: "管理员已暂停",
  PORTFOLIO_NOT_READY: "组合尚未就绪",
  SCHEDULE_TOO_EARLY: "未到自动推演时间",
  DUAL_EXECUTION_RISK_PROHIBITED: "检测到重复执行风险",
  RECONCILIATION_GAP_WARNING: "对账连续性存在缺口",
};

function governanceCodeLabel(value: string | null | undefined): string {
  if (!value) return "—";
  return GOVERNANCE_CODE_LABELS[value] ?? value.replace(/_/g, " ").toLowerCase();
}

/** 从 PortfolioStatusResponse → 当前状态的权限矩阵（API 返回优先；未返回则按 9×4 静态兜底） */
function resolvePermissions(resp: PortfolioStatusResponse | null): PortfolioStatePermissions {
  if (!resp) return { allow_new_buys: false, allow_risk_exits: false, allow_auto_recovery: false, requires_manual_ack: true };
  const cfg = HG1_STATUS_CFG[resp.current_state] ?? HG1_FALLBACK_CFG;
  // 只要 current_state 落在枚举内，兜底权限 = cfg.default_perm；后端 API 可后续覆盖扩展
  return cfg.default_perm;
}

function scoreValue(value: number | null | undefined) {
  if (value === null || value === undefined || Number.isNaN(value)) return "-";
  return value.toFixed(1);
}

function pct(value: number | null | undefined) {
  if (value === null || value === undefined || Number.isNaN(value)) return "-";
  return value.toFixed(1) + "%";
}

function marketLabel(value: number | null | undefined) {
  if (value === null || value === undefined) return t("tdEmpty");
  if (value >= 68) return t("tdActive");
  if (value <= 42) return t("tdDefensive");
  if (value <= 52) return t("tdCautious");
  return t("tdNeutral");
}

function scoreColor(value: number | null | undefined) {
  if (value === null || value === undefined) return "#64748b";
  if (value >= 68) return "#0f766e";
  if (value <= 42) return "#b42318";
  if (value <= 52) return "#d97706";
  return "#2563eb";
}

function eventTone(event: MarketEvent) {
  if (event.importance_level >= 5) return "red";
  if (event.sentiment === "negative") return "volcano";
  if (event.sentiment === "positive") return "green";
  if (event.importance_level >= 4) return "orange";
  return "blue";
}

function healthLabel(health: DataHealth | null) {
  if (!health) return t("tdEmpty");
  if (health.status === "ok") return t("tdHealthOk");
  if (health.status === "error") return t("tdHealthError");
  return t("tdHealthWarn");
}

function healthColor(health: DataHealth | null) {
  if (!health) return "#64748b";
  if (health.status === "ok") return "#0f766e";
  if (health.status === "error") return "#b42318";
  return "#d97706";
}

function ageText(days: number | null | undefined) {
  if (days === null || days === undefined) return "-";
  if (days === 0) return t("tdToday");
  return template("tdDaysAgo", { days });
}

function confidenceText(value: number | null | undefined) {
  const n = Number(value ?? 0);
  if (n >= 0.7) return t("tdHigh");
  if (n >= 0.4) return t("tdMedium");
  return t("tdLow");
}

function reasonText(item: WorkbenchCandidate) {
  return item.reason_tags?.length ? item.reason_tags.join(" / ") : t("tdEmpty");
}

/** Facade ScoringOverview → 旧 FactorOverview 适配（防腐层），避免改动大面积渲染代码。 */
function adaptScoringToFactorOverview(s: ScoringOverview | null): FactorOverview | null {
  if (!s) return null;
  const coverage: FactorOverview["factor_coverage"] = [];
  // 用平均覆盖率构造一个合成项，避免 TodayDecision 的覆盖率/低覆盖计数渲染出错
  if (s.active_factor_avg_coverage != null) {
    coverage.push({
      factor_code: "__avg__",
      latest_trade_date: s.latest_trade_date,
      universe_symbols: s.active_factor_coverage_count,
      eligible_symbols: Math.max(0, Math.round(s.active_factor_coverage_count * s.active_factor_avg_coverage)),
      imputed_symbols: 0,
      coverage: s.active_factor_avg_coverage,
    });
  }
  return {
    feature_enabled: s.feature_enabled,
    warehouse_error: s.warehouse_error,
    latest_trade_date: s.latest_trade_date,
    factor_coverage: coverage,
    runtime: {
      weight_mode: s.weight_mode,
      score_weight_mode: s.weight_mode === "manual" ? "manual" : "ridge",
      active_model_run_id: s.active_model_id,
      updated_by: "facade",
      fallback_reason: null,
      version: s.runtime_version,
      updated_at: s.updated_at,
    },
    config: {
      feature_enabled: s.feature_enabled,
      warehouse_path: s.warehouse_path,
      updated_by: "facade",
      updated_at: s.updated_at,
    },
    health: {
      status: s.health_status || "unknown",
      warehouse_available: s.warehouse_available,
      warehouse_path: s.warehouse_path,
      schema_version: null,
      calc_batch_id: null,
      latest_bar_date: s.latest_trade_date,
      raw_tables: [],
      factors: coverage,
      reasons: [],
    },
  };
}

export default function TodayDecision() {
  const ctx = useApp();
  const workbench = ctx.workbench;
  const portfolioId = ctx.portfolioId ?? 1;
  const [macro, setMacro] = useState<MacroOverview | null>(null);
  const [events, setEvents] = useState<MarketEvent[]>([]);
  const [health, setHealth] = useState<DataHealth | null>(null);
  const [factorOverview, setFactorOverview] = useState<FactorOverview | null>(null);
  const [portfolioStatus, setPortfolioStatus] = useState<PortfolioStatusResponse | null>(null);
  const [statusLoading, setStatusLoading] = useState(false);
  const [selectedExplain, setSelectedExplain] = useState<WorkbenchCandidate | null>(null);
  const [repairingSymbolId, setRepairingSymbolId] = useState<number | null>(null);
  const [openingSymbolId, setOpeningSymbolId] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [repairAllLoading, setRepairAllLoading] = useState(false);
  const [repairModalOpen, setRepairModalOpen] = useState(false);
  const [discoveryCleanupLoading, setDiscoveryCleanupLoading] = useState(false);
  const [discoveryRescanLoading, setDiscoveryRescanLoading] = useState(false);
  // FR-P1-3 三硬门禁：预检响应 + loading（fail-closed，未预检视为 proceed=false）
  const [preflightResult, setPreflightResult] = useState<AutoSimulationPreflightResponse | null>(null);
  const [preflightLoading, setPreflightLoading] = useState(false);

  const load = async () => {
    setLoading(true);
    setStatusLoading(true);
    setError(null);
    try {
      const [macroData, newsData, healthData, factorData, statusData] = await Promise.all([
        api.getMacroOverview("all"),
        api.getMarketEvents({ importance_level_min: 3, limit: 8, sort_by: "importance_level" }),
        api.getDataHealth(),
        // Factor-domain overview: via Public Facade, not /factors/overview
        api.scoringGetOverview().catch(() => null),
        // FR-P1-8a HG1：拉取组合状态（失败不阻塞今日决策其它面板）
        api.getPortfolioStatus(portfolioId).catch(() => null),
      ]);
      setMacro(macroData);
      setEvents(newsData.events ?? []);
      setHealth(healthData);
      setFactorOverview(adaptScoringToFactorOverview(factorData));
      setPortfolioStatus(statusData ?? null);
      await ctx.loadWorkbench();
    } catch (err: any) {
      setError(err.message || t("tdLoadFailed"));
    } finally {
      setLoading(false);
      setStatusLoading(false);
    }
  };

  useEffect(() => { load(); }, [portfolioId]);

  // ── FR-P0-10：HG1 权限 & 横幅配置（API返回优先，静态9×4兜底） ──
  const perm = useMemo<PortfolioStatePermissions>(() => resolvePermissions(portfolioStatus), [portfolioStatus]);
  const bannerCfg = portfolioStatus
    ? HG1_STATUS_CFG[portfolioStatus.current_state] ?? HG1_FALLBACK_CFG
    : HG1_FALLBACK_CFG;
  const currentState = portfolioStatus?.current_state ?? "UNKNOWN";
  const bannerTitle = bannerCfg.title_key ? t(bannerCfg.title_key) : "";
  const bannerDescription = bannerCfg.desc_key ? t(bannerCfg.desc_key) : "";

  // 兜底的 banner 中文文案（i18n key 未翻译时，保证中文用户可读；不阻塞 UI）
  const HG1_HARDCODED_ZH: Record<string, { title: string; desc: string }> = {
    PENDING_INITIAL_REVIEW: { title: "🔎 组合待初始审查", desc: "新建组合尚未通过管理员合规审查，所有交易动作已暂停。请联系审核人员在「治理」Tab 中完成初始审查后再操作。" },
    READY: { title: "✅ 生产就绪 · HG1 通过", desc: "Score 覆盖率/新鲜度、基准数据、对账连续性、模型状态全部通过门禁校验。可执行新买单与风险退出。" },
    RUNNING_AUTO_SIMULATION: { title: "⚙ 20:30 自动推演进行中", desc: "自动推演（auto-simulation）任务正在执行，状态与买单能力与 READY 一致。最终决策以当日 20:30 完成的推演结果为准。" },
    RUNNING_BACKTEST: { title: "📊 后台回测运行中", desc: "组合绑定的回测任务正在后台运行，不影响前台交易动作。若同时存在自动推演，请优先关注自动推演任务状态。" },
    DATA_INCOMPLETE_PAUSED: { title: "⚠ 数据缺失 · 已暂停新买单", desc: "行情/因子/Score 数据缺口(HEAVY)触发风控软暂停：❌ 禁止 NEW_BUY 新增买单；✅ 允许 RISK_EXIT 风险退出强制止损/清仓。数据补齐后恢复扫描器会自动转 READY。" },
    RECONCILIATION_BLOCKED: { title: "🛑 对账差异 · 人工介入", desc: "昨日实盘-撮合-仓位-现金10项守恒校验存在非零差异，已被 RECONCILIATION_BLOCKED 保护。需管理员在「治理」Tab 中单人确认差异清零后再转 READY。禁止新买单。" },
    MODEL_INACTIVE: { title: "⚡ 绑定因子模型已退役/未激活", desc: "当前组合绑定的 factor_model 不在 active 状态，评分流水线拒绝产出 Score。请管理员在模型设置中激活绑定的模型版本，或在「治理」Tab 切换模型。" },
    SCORE_STALE: { title: "⚠ Score 新鲜度/覆盖率未通过门禁", desc: "今日 Score 覆盖率 < 95%（生产阈值）或距离上一交易日评分产出 > 18h。数据补齐/重新跑分后会自动降级解除。禁止新买单。" },
    INTERRUPTED: { title: "💥 任务心跳超时 · 异常中断", desc: "自动推演/回测 Worker 心跳超时或异常退出被恢复扫描器检测到。恢复扫描器如能自动接续将转回 READY；否则需要人工在「治理」Tab 诊断。" },
    ADMIN_PAUSED: { title: "🔒 管理员一键刹车（紧急暂停）", desc: "管理员已触发 ADMIN_PAUSED 紧急暂停：❌ 禁止 NEW_BUY 新买单 / ❌ 禁止 RISK_EXIT 风险退出 / ❌ 禁止自动恢复。所有操作需管理员在「治理」Tab 中解除刹车回到 READY 后再执行。" },
    UNKNOWN: { title: "❓ 状态未知", desc: "无法从治理 API 读取当前组合状态（可能后端版本过旧/网络异常）。已按 fail-closed 原则禁用新买单。请点击「前往治理」确认状态。" },
  };

  const goGovernance = () => {
    // TodayDecision 顶层 Tab → 切换到 Investment（组合工作台）；治理 SubTab 的切换由 PortfolioTradingShell 内部的 subtab 状态负责
    // Set both levels before switching the portfolio workbench.  The old
    // implementation used the legacy ``investment`` route, so the button
    // appeared to do nothing because it opened the research shell instead.
    ctx.setActiveSubTab("governance");
    ctx.setActiveTab("portfolio");
  };

  // ── FR-P1-3：三硬门禁预检（HG1 READY / HG2 20:00 时间窗 / HG3 对账连续性） ──
  const handlePreflight = useCallback(async () => {
    if (!portfolioId) return;
    setPreflightLoading(true);
    setPreflightResult(null);
    try {
      const now = new Date();
      const tradeDate = now.toISOString().slice(0, 10);
      const resp = await api.preflightAutoSimulation(portfolioId, {
        trade_date: tradeDate,
        decision_at: now.toISOString(),
      });
      setPreflightResult(resp);
      if (resp.proceed === false) {
        ctx.showToast("info", `⚠️ FR-P1-3 预检未通过（${resp.skip_reason ?? "未知原因"}），进入自动推演已被硬门禁阻断。`);
      } else if (resp.skip_reason === "RECONCILIATION_GAP_WARNING") {
        ctx.showToast("info", "⚠️ FR-P1-3 HG3 告警：对账连续性缺口>1 天；虽允许进入 Shadow，但已同步写入 AUTO_SIMULATION_RESULT=FAILED 告警追踪。");
      } else {
        ctx.showToast("success", "FR-P1-3 三硬门禁预检全部通过，可进入自动推演/Shadow 模式。");
      }
    } catch (err: any) {
      ctx.showToast("error", "FR-P1-3 预检失败：" + (err?.message || String(err)));
    } finally {
      setPreflightLoading(false);
    }
  }, [portfolioId, ctx]);
  // Fail-closed：未预检 或 预检明确返回 proceed=false → 进入 Shadow/自动推演按钮禁用
  const frp13Proceed = Boolean(preflightResult?.proceed);
  const frp13FailReason = (() => {
    if (!preflightResult) return "未执行 FR-P1-3 预检；请先点击「预检自动推演」";
    if (preflightResult.proceed === false) return `预检未通过：${preflightResult.skip_reason ?? "未知原因"} · ${preflightResult.skip_detail ?? ""}`;
    if (preflightResult.skip_reason === "RECONCILIATION_GAP_WARNING") return null; // Warning 不阻断
    return null;
  })();

  const macroScore = macro?.snapshot?.market_score ?? null;
  const candidates = useMemo(() => {
    const rows = [...(workbench?.candidates ?? [])].map((item) => withFinalOpportunityScore(item, ctx.newsSnapshot) as WorkbenchCandidate);
    return rows.sort((a, b) => Number(opportunityScoreValue(b)) - Number(opportunityScoreValue(a))).slice(0, 5);
  }, [workbench, ctx.newsSnapshot]);

  const riskEvents = useMemo(() => events.filter((item) => item.importance_level >= 4 || item.sentiment === "negative").slice(0, 5), [events]);
  const investedPct = workbench?.account_summary?.invested_pct ?? workbench?.overview?.total_position_pct ?? 0;
  const positionStatus = investedPct >= 75 ? t("tdHigh") : t("tdSafe");
  const actions = useMemo(() => {
    return [...(workbench?.candidates ?? [])]
      .map((item) => withFinalOpportunityScore(item, ctx.newsSnapshot) as WorkbenchCandidate)
      .filter((item) => TODO_ACTIONS.has(String(item.action)))
      .sort((a, b) => Number(opportunityScoreValue(b)) - Number(opportunityScoreValue(a)))
      .slice(0, 5);
  }, [workbench, ctx.newsSnapshot]);

  const allRepairSamples = useMemo(() => {
    const missing = (health?.bars.missing_samples ?? []).map((item) => ({ ...item, repairKind: t("tdMissingBars"), tagColor: "red" }));
    const stale = (health?.bars.stale_samples ?? []).map((item) => ({ ...item, repairKind: t("tdOutdatedBars"), tagColor: "orange" }));
    return [...missing, ...stale];
  }, [health]);
  const visibleRepairSamples = useMemo(() => allRepairSamples.slice(0, 6), [allRepairSamples]);
  const hasMoreRepairSamples = allRepairSamples.length > 6;

  const factorCoverage = useMemo(() => {
    const rows = factorOverview?.factor_coverage ?? [];
    if (!rows.length) return null;
    return rows.reduce((sum, item) => sum + Number(item.coverage || 0), 0) / rows.length;
  }, [factorOverview]);

  const missingBarCount = health?.bars.missing_symbols ?? 0;
  const outdatedBarCount = health?.bars.outdated_symbols ?? 0;
  const factorCoveragePct = factorCoverage == null ? null : factorCoverage * 100;
  const lowFactorCount = (factorOverview?.factor_coverage ?? []).filter((item) => Number(item.coverage || 0) < 0.95).length;
  const needsMarketData = missingBarCount > 0 || outdatedBarCount > 0;
  const needsFactorData = factorCoveragePct != null && (factorCoveragePct < 95 || lowFactorCount > 0);

  const repairSymbol = async (item: DataHealthBarIssue) => {
    setRepairingSymbolId(item.symbol_id);
    try {
      await api.repairSymbolMarketData(item.symbol_id, { auto_score: true });
      ctx.showToast("success", `${t("tdRepairOk")}: ${item.symbol}`);
      await load();
    } catch (err: any) {
      ctx.showToast("error", `${t("tdRepairFailed")}: ${err.message || err}`);
    } finally {
      setRepairingSymbolId(null);
    }
  };

  const repairAllSamples = async (items: DataHealthBarIssue[]) => {
    if (items.length === 0) return;
    setRepairAllLoading(true);
    try {
      const result = (await api.repairAllSymbolMarketData({
        symbol_ids: items.map((item) => item.symbol_id),
        auto_score: true,
      })) as {
        success: boolean;
        total: number;
        ok_count: number;
        empty_count: number;
        failed_count: number;
        missing_count: number;
      };
      ctx.showToast(
        result.success ? "success" : "info",
        template("tdRepairAllSummary", {
          total: result.total,
          ok: result.ok_count,
          empty: result.empty_count,
          failed: result.failed_count,
          missing: result.missing_count,
        }),
      );
      await load();
    } catch (err: any) {
      ctx.showToast("error", `${t("tdRepairAllFailed")}: ${err.message || err}`);
    } finally {
      setRepairAllLoading(false);
    }
  };

  const cleanupExpiredResults = async () => {
    setDiscoveryCleanupLoading(true);
    try {
      const result = (await api.cleanupDiscoveryResults()) as { deleted: number };
      ctx.showToast("success", template("tdCleanupExpiredResultsSummary", { deleted: result.deleted }));
      await load();
    } catch (err: any) {
      ctx.showToast("error", `${t("tdCleanupExpiredResultsFailed")}: ${err.message || err}`);
    } finally {
      setDiscoveryCleanupLoading(false);
    }
  };

  const rescanDiscovery = async () => {
    setDiscoveryRescanLoading(true);
    try {
      await api.createDiscoveryTask({ scope: "cn-stock", min_score: 55, include_news: true });
      ctx.showToast("success", t("tdRescanDiscoveryStarted"));
      await load();
    } catch (err: any) {
      ctx.showToast("error", `${t("tdRescanDiscoveryFailed")}: ${err.message || err}`);
    } finally {
      setDiscoveryRescanLoading(false);
    }
  };

  const conclusion = macroScore === null
    ? t("tdNoData")
    : template("tdConclusion", { market: marketLabel(macroScore).toLowerCase() });

  const openSymbol = async (item: WorkbenchCandidate) => {
    setOpeningSymbolId(item.symbol_id);
    try {
      // WP5.3：今日决策入口跳转，携带 candidate 来源与 candidate_id
      navigateToResearch(
        ctx,
        {
          symbol_id: item.symbol_id,
          source_type: "candidate",
          source_id: item.candidate_id ?? undefined,
          portfolio_id: undefined,
          return_to: "candidate",
        },
      );
    } catch (err: any) {
      ctx.showToast("error", err.message || t("tdLoadDetailFailed"));
    } finally {
      setOpeningSymbolId(null);
    }
  };

  // WP1-FIX.1：徽标点击时复用 openSymbol 逻辑（切换到 investment 标签打开详情）
  // 因 TodayDecision 不在 DetailModal 自动触发列表（portfolio/discovery/opportunity）内，需切换标签
  const openSymbolDetail = (symbolId: number) => {
    openSymbol({ symbol_id: symbolId } as WorkbenchCandidate);
  };

  const explainRows = selectedExplain ? [
    [t("tdFinalScore"), score(opportunityScoreValue(selectedExplain), 1)],
    [t("tdBaseScore"), score(baseOpportunityScoreValue(selectedExplain), 1)],
    [t("tdNewsMultiplier"), `${score(selectedExplain.news_multiplier ?? 1, 3)}x`],
    [t("tdMessageScore"), score(selectedExplain.news_message_score, 1)],
    [t("tdConfidence"), `${confidenceText(selectedExplain.news_confidence)} (${score(Number(selectedExplain.news_confidence ?? 0) * 100, 0)}%)`],
    [t("tdQuality"), score(selectedExplain.quality_score, 1)],
    [t("tdTiming"), score(selectedExplain.timing_score, 1)],
    [t("tdTrend"), score(selectedExplain.trend_score, 1)],
    [t("tdMomentum"), score(selectedExplain.momentum_score, 1)],
    [t("tdVolatility"), score(selectedExplain.volatility_score, 1)],
    [t("tdLiquidity"), score(selectedExplain.liquidity_score, 1)],
    [t("tdBreadth"), score(selectedExplain.breadth_score, 1)],
    [t("tdEvent"), score(selectedExplain.event_score, 1)],
    [t("tdPlan"), `${stageLabel(selectedExplain.stage)} / ${actionLabel(selectedExplain.action)}`],
    [t("tdReasons"), reasonText(selectedExplain)],
    [t("tdFreshness"), selectedExplain.created_at ? formatRelativeTime(selectedExplain.created_at) : "-"],
  ] : [];

  const openFactorSettings = () => {
    window.localStorage.setItem("settings_active_section", "factor-model");
    ctx.setActiveTab("settings");
  };

  const openDataCenter = (tab: "market" | "inputs") => {
    window.localStorage.setItem("settings_active_section", "data-center");
    window.localStorage.setItem("settings_data_center_tab", tab);
    ctx.setActiveTab("settings");
  };

  return (
    <div className="decision-page">
      <section className="decision-hero">
        <div>
          <p className="panel-kicker">{t("tdTitle")}</p>
          <h1>{conclusion}</h1>
          <p>{t("tdSubtitle")}</p>
        </div>
        <Button icon={<ReloadOutlined />} loading={loading} onClick={load}>{t("tdRefresh")}</Button>
      </section>

      {error && <Alert type="error" showIcon message={error} />}

      {/* ===============================================================
       * FR-P0-10 / FR-P1-8a HG1 组合状态门禁横幅（9 状态 × 色彩分级）
       * 非 READY 状态：提示 + 4 权限徽章 + 前往治理诊断按钮
       * READY 状态：绿色生产就绪徽章条（紧凑型，不占视觉主位）
       * =============================================================== */}
      <Alert
        type={bannerCfg.alert_type}
        showIcon
        icon={statusLoading ? <LoadingOutlined spin /> : bannerCfg.icon}
        style={{
          marginBottom: 16,
          borderRadius: 12,
          border: currentState === "ADMIN_PAUSED"
            ? "2px solid #dc2626"
            : currentState === "RECONCILIATION_BLOCKED"
              ? "2px solid #ef4444"
              : undefined,
          background: currentState === "ADMIN_PAUSED"
            ? "linear-gradient(135deg, #fef2f2 0%, #fff1f2 100%)"
            : undefined,
          boxShadow: currentState === "ADMIN_PAUSED" ? "0 0 0 1px rgba(220,38,38,0.15), 0 4px 18px -6px rgba(220,38,38,0.25)" : undefined,
        }}
        message={
          <Space size="large" wrap style={{ width: "100%", justifyContent: "space-between", alignItems: "flex-start" }}>
            <Space direction="vertical" size={2} style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontWeight: 700, fontSize: 14 }}>
                {bannerTitle || HG1_HARDCODED_ZH[currentState]?.title || `组合状态：${currentState}`}
              </div>
              <div style={{ fontSize: 12.5, opacity: 0.85 }}>
                {bannerDescription || HG1_HARDCODED_ZH[currentState]?.desc || "治理 API 未返回该状态的说明，建议前往「治理」Tab 查看诊断明细。"}
              </div>
              {/* 4 列 9×4 允许权限徽章（与治理 Tab 矩阵一致） */}
              <Space size={6} wrap style={{ marginTop: 4 }}>
                <Tag color={perm.allow_new_buys ? "green" : "red"} style={{ marginInlineEnd: 0 }}>
                  {perm.allow_new_buys ? "✅ 新买单：允许" : "❌ 新买单：禁止"}
                </Tag>
                <Tag color={perm.allow_risk_exits ? "green" : "red"} style={{ marginInlineEnd: 0 }}>
                  {perm.allow_risk_exits ? "✅ 风险退出：允许" : "❌ 风险退出：禁止"}
                </Tag>
                <Tag color={perm.allow_auto_recovery ? "blue" : "default"} style={{ marginInlineEnd: 0 }}>
                  {perm.allow_auto_recovery ? "🔄 自动恢复：允许" : "🛑 自动恢复：禁止"}
                </Tag>
                {perm.requires_manual_ack && (
                  <Tag color="orange" icon={<WarningOutlined />} style={{ marginInlineEnd: 0 }}>
                    👤 人工确认：需要
                  </Tag>
                )}
                {portfolioStatus?.is_auto_simulation_eligible === false && (
                  <Tooltip title="HG1 综合预检未通过（Score 新鲜度/对账连续性/门禁任一未达标）">
                    <Tag color="volcano" icon={<StopOutlined />} style={{ marginInlineEnd: 0 }}>
                      🚫 自动推演预检未通过（当前不可用）
                    </Tag>
                  </Tooltip>
                )}
              </Space>
            </Space>
            <Space>
              {portfolioStatus?.last_decision_trade_date && (
                <Tag style={{ marginInlineEnd: 0 }}>
                  上次决策日：<b style={{ marginLeft: 4 }}>{portfolioStatus.last_decision_trade_date}</b>
                </Tag>
              )}
              {portfolioStatus?.last_reconciled_trade_date && (
                <Tag color={portfolioStatus.last_reconciled_trade_date ? "cyan" : "red"} style={{ marginInlineEnd: 0 }}>
                  上次对账：<b style={{ marginLeft: 4 }}>{portfolioStatus.last_reconciled_trade_date || "无"}</b>
                </Tag>
              )}
              <Button
                type={currentState === "READY" ? "default" : "primary"}
                size="small"
                onClick={goGovernance}
                danger={currentState === "ADMIN_PAUSED" || currentState === "RECONCILIATION_BLOCKED"}
              >
                {currentState === "READY" ? "查看治理详情 →" : "前往治理诊断 →"}
              </Button>
            </Space>
          </Space>
        }
      />

      {/* ===============================================================
       * FR-P1-3：三硬门禁预检卡片（HG1 READY / HG2 20:00 时间窗 / HG3 对账连续性）
       * 规则：fail-closed —— 未预检 或 预检返回 proceed=false → 进入 Shadow/自动推演按钮全部禁用
       * =============================================================== */}
      <Card
        size="small"
        style={{
          marginBottom: 16,
          borderRadius: 12,
          border: "1px solid var(--pt-border, #e5e7eb)",
          background: frp13Proceed
            ? preflightResult?.skip_reason === "RECONCILIATION_GAP_WARNING"
              ? "linear-gradient(135deg, #fffbeb 0%, #fef3c7 100%)"
              : "linear-gradient(135deg, #ecfdf5 0%, #d1fae5 100%)"
            : preflightResult
              ? "linear-gradient(135deg, #fef2f2 0%, #fee2e2 100%)"
              : undefined,
        }}
        title={
          <Space size={8}>
            <PlayCircleOutlined style={{ color: "var(--pt-primary)" }} />
            <span style={{ fontWeight: 600 }}>FR-P1-3 三硬门禁 · 自动推演前置预检</span>
            <Tag color="geekblue" style={{ marginInlineEnd: 0 }}>
              HG1/HG2/HG3
            </Tag>
          </Space>
        }
        extra={
          <Space>
            <Button
              size="small"
              icon={<ReloadOutlined />}
              onClick={handlePreflight}
              loading={preflightLoading || statusLoading}
              type={!preflightResult ? "primary" : "default"}
            >
              {preflightLoading ? "预检中..." : !preflightResult ? "执行预检" : "重新预检"}
            </Button>
            <Tooltip title={frp13FailReason || "预检通过（允许进入自动推演 / Shadow 模式）"}>
              <span>
                <Button
                  size="small"
                  type="primary"
                  danger={Boolean(frp13FailReason)}
                  icon={<ExperimentOutlined />}
                  disabled={!frp13Proceed}
                  style={{ opacity: !frp13Proceed ? 0.6 : 1, cursor: !frp13Proceed ? "not-allowed" : undefined }}
                  onClick={() => {
                    if (!frp13Proceed) return;
                    // 进入 Shadow：跳治理 Tab（cron worker 会在预检通过后真正执行 evaluate；
                    // 管理员/QA 在此按钮仅做"查看 auto-simulation 结果"动作）
                    ctx.setActiveTab("portfolio");
                    ctx.setActiveSubTab("governance");
                    ctx.showToast("success", "已允许进入 Shadow 模式；cron worker 将在 20:30 执行 auto_simulation。若需人工介入请前往「治理」Tab。");
                  }}
                >
                  进入自动推演 / Shadow
                </Button>
              </span>
            </Tooltip>
          </Space>
        }
      >
        <Space direction="vertical" size={8} style={{ width: "100%" }}>
          {/* 3 门门禁状态概览 */}
          <Space wrap size={10}>
            {(() => {
              const hg1 = preflightResult ? (preflightResult.skip_reason === "PORTFOLIO_NOT_READY" ? "fail" : "pass") : null;
              const hg2 = preflightResult ? (preflightResult.skip_reason === "SCHEDULE_TOO_EARLY" || preflightResult.skip_reason === "DUAL_EXECUTION_RISK_PROHIBITED" ? "fail" : "pass") : null;
              const hg3 = preflightResult ? (preflightResult.skip_reason === "RECONCILIATION_GAP_WARNING" ? "warn" : "pass") : null;
              const badge = (name: string, status: "pass" | "fail" | "warn" | null, desc: string) => {
                const color = status === "pass" ? "green" : status === "fail" ? "red" : status === "warn" ? "orange" : "default";
                const icon = status === "pass" ? "✅" : status === "fail" ? "❌" : status === "warn" ? "⚠️" : "⏳";
                return <Tag key={name} color={color} style={{ marginInlineEnd: 0, padding: "2px 8px", fontSize: 12 }}>{icon} {name}：{desc}</Tag>;
              };
              return (
                <>
                  {badge("HG1 状态机 READY", hg1, hg1 === "fail" ? "组合非 READY 硬拒" : hg1 === "pass" ? "READY ✔" : "未评估")}
                  {badge("HG2 调度时间/同日重复", hg2, hg2 === "fail" ? "早于20点或已存在 RUNNING/SUCCEEDED" : hg2 === "pass" ? "20:00+ 无同日重复 ✔" : "未评估")}
                  {badge("HG3 对账连续性", hg3, hg3 === "warn" ? "缺口>1（审计 FAILED 告警，proceed 仍 True）" : hg3 === "pass" ? "连续性 OK ✔" : "未评估")}
                </>
              );
            })()}
          </Space>

          {/* 预检 skip_reason / skip_detail / warnings 卡片 */}
          {preflightResult && (preflightResult.skip_reason || (preflightResult.warnings && preflightResult.warnings.length > 0)) && (
            <Alert
              type={preflightResult.proceed === false ? "error" : "warning"}
              showIcon
              style={{ borderRadius: 10 }}
              message={
                <>
                  <b style={{ fontSize: 13 }}>
                    {preflightResult.proceed === false
                      ? "🛑 三硬门禁阻断详情（已写 AUTO_SIMULATION_RESULT=FAILED 审计）"
                      : "⚠️ 预检通过，但存在告警（仍已同步写 AUTO_SIMULATION_RESULT=FAILED 审计追踪）"}
                  </b>
                  {preflightResult.skip_reason && (
                    <div style={{ fontSize: 12.5, marginTop: 4 }}>
                      原因：<Tag color={preflightResult.proceed ? "orange" : "red"} style={{ marginInlineEnd: 0 }}>{governanceCodeLabel(preflightResult.skip_reason)}</Tag>
                    </div>
                  )}
                </>
              }
              description={
                <div style={{ fontSize: 12.5 }}>
                  {preflightResult.skip_detail && <div>{preflightResult.skip_detail}</div>}
                  {preflightResult.warnings && preflightResult.warnings.length > 0 && (
                    <ul style={{ paddingInlineStart: 20, margin: preflightResult.skip_detail ? "6px 0 0 0" : 0 }}>
                      {preflightResult.warnings.map((w, i) => <li key={i}>{w}</li>)}
                    </ul>
                  )}
                  {(preflightResult.from_state || preflightResult.to_state) && (
                    <div style={{ marginTop: 6, color: "var(--pt-muted-foreground)" }}>
                      状态轨迹：{governanceCodeLabel(preflightResult.from_state)} <ArrowRightOutlined /> {governanceCodeLabel(preflightResult.to_state)}
                    </div>
                  )}
                </div>
              }
            />
          )}

          {!preflightResult && (
            <div style={{ fontSize: 12.5, color: "var(--pt-muted-foreground)" }}>
              <InfoCircleOutlined style={{ marginRight: 4 }} />
              为保证 fail-closed 安全，<b>未预检默认视为不通过</b>：进入自动推演/Shadow 按钮禁用。请点击右上角「执行预检」通过 HG1/HG2/HG3 检查后再操作。
            </div>
          )}
        </Space>
      </Card>

      <Row gutter={[12, 12]}>
        <Col xs={24} sm={12} lg={6}><Card className="decision-metric"><BarChartOutlined /><span>{t("tdMarket")}</span><strong style={{ color: scoreColor(macroScore) }}>{marketLabel(macroScore)}</strong><Progress percent={macroScore ? Math.round(macroScore) : 0} strokeColor={scoreColor(macroScore)} showInfo={false} /></Card></Col>
        <Col xs={24} sm={12} lg={6}><Card className="decision-metric"><FireOutlined /><span>{t("tdOpportunities")}</span><strong>{candidates.length}</strong><Button type="link" onClick={() => ctx.setActiveTab("discovery")}>{t("tdGoDiscovery")}</Button></Card></Col>
        <Col xs={24} sm={12} lg={6}><Card className="decision-metric"><AlertOutlined /><span>{t("tdRisks")}</span><strong>{riskEvents.length}</strong><Button type="link" onClick={() => ctx.setActiveTab("news")}>{t("tdGoNews")}</Button></Card></Col>
        <Col xs={24} sm={12} lg={6}><Card className="decision-metric"><SafetyOutlined /><span>{t("tdPosition")}</span><strong>{positionStatus}</strong><Text type="secondary">{pct(investedPct)}</Text></Card></Col>
      </Row>

      <Card className="decision-health-card" title={<Space><DatabaseOutlined />{t("tdHealthTitle")}</Space>} extra={<Tag color={health?.status === "ok" ? "green" : health?.status === "error" ? "red" : "orange"}>{healthLabel(health)}</Tag>}>
        <div className="decision-health-grid">
          <div className="decision-health-score">
            <strong style={{ color: healthColor(health) }}>{health?.score ?? "-"}</strong>
            <Progress percent={health?.score ?? 0} strokeColor={healthColor(health)} showInfo={false} />
          </div>
          <span><b>{t("tdSymbols")}</b>{health?.symbols.total ?? "-"}</span>
          <span><b>{t("tdBarCoverage")}</b>{pct(health?.bars.coverage_pct)}</span>
          <span><b>{t("tdMissingBars")}</b>{health?.bars.missing_symbols ?? "-"}</span>
          <span><b>{t("tdOutdatedBars")}</b>{health?.bars.outdated_symbols ?? "-"}</span>
          <span><b>{t("tdMacroFreshness")}</b>{ageText(health?.macro.latest_age_days)}</span>
          <span><b>{t("tdNews7d")}</b>{health?.market_events.events_7d ?? "-"}</span>
          <span><b>{t("tdDiscoveryFreshness")}</b>{t("tdExpired")}: {health?.discovery.expired_results ?? 0} / {t("tdFrozen")}: {health?.discovery.frozen_results ?? 0}</span>
        </div>
        <div className="decision-health-issues">
          {(health?.issues?.length ? health.issues : [{ level: "ok", message: t("tdNoIssue") }]).map((issue, index) => (
            <Tag key={index} color={issue.level === "error" ? "red" : issue.level === "warn" ? "orange" : "green"}>{issue.message}</Tag>
          ))}
        </div>
        {(needsMarketData || needsFactorData) && (
          <Space direction="vertical" size={8} style={{ width: "100%", marginTop: 12 }}>
            <Text strong>{t("tdHealthGuideTitle")}</Text>
            {needsMarketData && (
              <Alert
                type="warning"
                showIcon
                  message={template("tdHealthMarketGuide", { missing: missingBarCount, outdated: outdatedBarCount })}
                  action={
                    <Space wrap>
                      <Button size="small" onClick={() => openDataCenter("market")}>{t("tdHealthOpenMarket")}</Button>
                    </Space>
                  }
              />
            )}
            {needsFactorData && (
              <Alert
                type="warning"
                showIcon
                message={template("tdHealthFactorGuide", { pct: factorCoveragePct?.toFixed(1) ?? "-", lowCount: lowFactorCount })}
                action={
                  <Space wrap>
                    <Button size="small" onClick={() => openDataCenter("inputs")}>{t("tdHealthOpenFactorInput")}</Button>
                    <Button size="small" onClick={openFactorSettings}>{t("tdHealthOpenFactorModel")}</Button>
                  </Space>
                }
              />
            )}
          </Space>
        )}
        <div className="decision-health-repair">
          <div className="decision-health-repair-head">
            <Space>
              <Text type="secondary">{t("tdRepairSamples")}</Text>
              <Tag>{allRepairSamples.length}</Tag>
            </Space>
            <Space>
              <Text type="secondary">{health?.bars.repair_hint}</Text>
              <Button type="primary" size="small" icon={<ReloadOutlined />} loading={repairAllLoading} disabled={allRepairSamples.length === 0} onClick={() => repairAllSamples(allRepairSamples)}>
                {t("tdRepairAll")}
              </Button>
            </Space>
          </div>
          {allRepairSamples.length === 0 ? (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t("tdNoRepairSamples")} style={{ margin: "16px 0" }} />
          ) : (
            <>
              <div className="decision-health-repair-list">
                {visibleRepairSamples.map((item) => (
                  <div key={`${item.reason}-${item.symbol_id}`} className="decision-health-repair-row">
                    <div>
                      <Tag color={item.tagColor}>{item.repairKind}</Tag>
                      <strong>{item.symbol}</strong>
                      <span>{item.name}</span>
                      <Text type="secondary">
                        {item.latest_trade_date ? `${t("tdLatestBar")}: ${item.latest_trade_date} / ${ageText(item.latest_age_days)}` : t("tdMissingReason")}
                      </Text>
                    </div>
                    <Button size="small" icon={<ReloadOutlined />} loading={repairingSymbolId === item.symbol_id} onClick={() => repairSymbol(item)}>{t("tdRepair")}</Button>
                  </div>
                ))}
              </div>
              {hasMoreRepairSamples && (
                <div style={{ marginTop: 12, textAlign: "center" }}>
                  <Button type="link" onClick={() => setRepairModalOpen(true)}>
                    {template("tdRepairViewMore", { count: allRepairSamples.length - 6 })}
                  </Button>
                </div>
              )}
            </>
          )}
        </div>

        {(!!health?.discovery.expired_results || ["failed", "expired"].includes(health?.discovery.latest_task?.status ?? "")) && (
          <div className="decision-health-repair" style={{ marginTop: 16 }}>
            <div className="decision-health-repair-head">
              <Space>
                <Text type="secondary">{t("tdDiscoveryActions")}</Text>
              </Space>
              <Space>
                {!!health?.discovery.expired_results && (
                  <Button size="small" icon={<ClearOutlined />} loading={discoveryCleanupLoading} onClick={cleanupExpiredResults}>
                    {t("tdCleanupExpiredResults")} ({health.discovery.expired_results})
                  </Button>
                )}
                {["failed", "expired"].includes(health?.discovery.latest_task?.status ?? "") && (
                  <Button type="primary" size="small" icon={<PlayCircleOutlined />} loading={discoveryRescanLoading} onClick={rescanDiscovery}>
                    {t("tdRescanDiscovery")}
                  </Button>
                )}
              </Space>
            </div>
          </div>
        )}
      </Card>

      <Card
        className="decision-factor-card"
        title={<Space><ExperimentOutlined />{t("tdFactorTitle")}</Space>}
        extra={
          <Space>
            <Tag color={factorOverview?.health.warehouse_available ? (factorOverview.health.status === "healthy" ? "green" : "orange") : "red"}>
              {factorOverview?.health.warehouse_available ? factorOverview.health.status : t("tdFactorUnavailable")}
            </Tag>
            <Button type="link" onClick={openFactorSettings}>{t("tdFactorManage")}</Button>
          </Space>
        }
      >
        <div className="decision-factor-grid">
          <span><b>{t("tdFactorMode")}</b><Tag color={factorOverview?.runtime.weight_mode === "ridge" ? "green" : factorOverview?.runtime.weight_mode === "shadow" ? "blue" : "default"}>{enumLabel("factorMode", factorOverview?.runtime.weight_mode ?? "manual")}</Tag></span>
          <span><b>{t("tdFactorScoreSource")}</b><strong>{enumLabel("factorMode", factorOverview?.runtime.score_weight_mode ?? "manual")}</strong></span>
          <span title={factorOverview?.runtime.active_model_run_id ?? undefined}><b>{t("tdFactorModel")}</b><strong>{factorOverview?.runtime.active_model_run_id ? factorOverview.runtime.active_model_run_id.slice(0, 16) : "-"}</strong></span>
          <span><b>{t("tdFactorLatest")}</b><strong>{factorOverview?.latest_trade_date ?? "-"}</strong></span>
          <span><b>{t("tdFactorCoverage")}</b><strong>{factorCoverage == null ? "-" : `${(factorCoverage * 100).toFixed(1)}%`}</strong></span>
        </div>
      </Card>

      <Row gutter={[12, 12]}>
        <Col xs={24} lg={14}>
          <Card
            title={
              <Space>
                {t("tdTopOpportunities")}
                {!perm.allow_new_buys && (
                  <Tooltip title="HG1 门禁：当前组合状态禁止新增买单，请先在「治理」Tab 解除限制。">
                    <Tag color="red" icon={<StopOutlined />} style={{ marginInlineEnd: 0 }}>🚫 新买单暂不可用</Tag>
                  </Tooltip>
                )}
              </Space>
            }
            extra={<Button type="link" onClick={() => ctx.setActiveTab("discovery")}>{t("tdGoDiscovery")}</Button>}
            style={!perm.allow_new_buys ? { opacity: 0.62 } : undefined}
          >
            {candidates.length === 0 ? <Empty description={t("tdEmpty")} /> : (
              <div className="decision-list">
                {candidates.map((item, index) => (
                  <div key={item.symbol_id} className="decision-row decision-row-split">
                    <Tooltip
                      title={
                        perm.allow_new_buys
                          ? undefined
                          : `当前组合状态 ${currentState} 禁止 NEW_BUY 新买单。${perm.requires_manual_ack ? "需人工在治理 Tab 确认或解除限制。" : "数据补齐/自动恢复后解除。"}`
                      }
                      placement="topLeft"
                    >
                      {/* span 包装让 Tooltip 在 button disabled 时依然可触发 */}
                      <span style={{ display: "inline-flex", flex: 1, minWidth: 0 }}>
                        <button
                          type="button"
                          disabled={openingSymbolId === item.symbol_id || !perm.allow_new_buys}
                          onClick={() => openSymbol(item)}
                          style={{ cursor: !perm.allow_new_buys ? "not-allowed" : undefined }}
                        >
                          <span className="decision-rank">{index + 1}</span>
                          <strong>{item.symbol}</strong>
                          <span>{item.name}</span>
                          <Tag>{stageLabel(item.stage)}</Tag>
                          <b>{scoreValue(Number(opportunityScoreValue(item)))}</b>
                          {openingSymbolId === item.symbol_id ? <LoadingOutlined spin /> : <ArrowRightOutlined />}
                        </button>
                      </span>
                    </Tooltip>
                    <Button size="small" icon={<InfoCircleOutlined />} onClick={() => setSelectedExplain(item)}>{t("tdExplain")}</Button>
                    <OpportunityStatusBadges symbolId={item.symbol_id} compact onOpenDetail={openSymbolDetail} />
                  </div>
                ))}
              </div>
            )}
          </Card>
        </Col>
        <Col xs={24} lg={10}>
          <Card title={t("tdTopNews")} extra={<Button type="link" onClick={() => ctx.setActiveTab("news")}>{t("tdGoNews")}</Button>}>
            {events.length === 0 ? <Empty description={t("tdEmpty")} /> : <div className="decision-news">{events.slice(0, 5).map((event) => <button key={event.id} className="decision-news-row" onClick={() => ctx.setActiveTab("news")}><Tag color={eventTone(event)}>{event.importance_level}</Tag><span>{event.title}</span></button>)}</div>}
          </Card>
        </Col>
      </Row>

      {/* FR-P1-8a：风险出口与加仓动作分别对应 allow_risk_exits / allow_new_buys 权限 */}
      <Card
        title={
          <Space>
            {t("tdActions")}
            {(!perm.allow_new_buys || !perm.allow_risk_exits) && (
              <Space size={4}>
                {!perm.allow_new_buys && (
                  <Tooltip title="HG1 门禁：禁止 NEW_BUY 型动作（加仓/新建）">
                    <Tag color="red" icon={<StopOutlined />} style={{ marginInlineEnd: 0 }}>🚫 NEW_BUY</Tag>
                  </Tooltip>
                )}
                {!perm.allow_risk_exits && (
                  <Tooltip title="HG1 门禁：禁止 RISK_EXIT 型动作（止损/减仓/清仓）">
                    <Tag color="orange" icon={<WarningOutlined />} style={{ marginInlineEnd: 0 }}>⚠ RISK_EXIT 暂不可用</Tag>
                  </Tooltip>
                )}
              </Space>
            )}
          </Space>
        }
        extra={<Button type="link" onClick={() => ctx.setActiveTab("investment")}>{t("tdGoInvestment")}</Button>}
        style={(!perm.allow_new_buys && !perm.allow_risk_exits) ? { opacity: 0.62 } : undefined}
      >
        {actions.length === 0 ? <Empty description={t("tdEmpty")} /> : (
          <div className="decision-actions">
            {actions.map((item) => {
              // 识别是否风险出口动作（action=EXIT/REDUCE_POSITION/FORCE_EXIT/STOP_LOSS/TAKE_PROFIT 都走风险出口权限）
              const act = (item.action || "").toUpperCase();
              const isRiskExit = ["EXIT", "REDUCE_POSITION", "FORCE_EXIT", "STOP_LOSS", "TAKE_PROFIT"].includes(act);
              const allowed = isRiskExit ? perm.allow_risk_exits : perm.allow_new_buys;
              const denyReason = allowed
                ? undefined
                : isRiskExit
                  ? `当前组合状态 ${currentState} 禁止 RISK_EXIT（风险退出）。${perm.requires_manual_ack ? "请管理员在治理 Tab 解除刹车/确认差异。" : "自动恢复/对账差异清零后解除。"}`
                  : `当前组合状态 ${currentState} 禁止 NEW_BUY（加仓/新建）。${perm.requires_manual_ack ? "请人工在治理 Tab 完成审查/确认。" : "数据补齐/自动恢复后解除。"}`;
              return (
                <Tooltip key={item.symbol_id} title={denyReason} placement="topLeft">
                  <span style={{ display: "block", width: "100%" }}>
                    <button
                      disabled={openingSymbolId === item.symbol_id || !allowed}
                      onClick={() => openSymbol(item)}
                      style={{ cursor: !allowed ? "not-allowed" : undefined }}
                    >
                      {openingSymbolId === item.symbol_id ? <LoadingOutlined spin /> : <CheckCircleOutlined />}
                      <span>{item.symbol} {item.name}</span>
                      <Tag>{actionLabel(item.action)}</Tag>
                      <Text type="secondary">{item.reason_tags?.slice(0, 2).join(" / ")}</Text>
                      <OpportunityStatusBadges symbolId={item.symbol_id} compact onOpenDetail={openSymbolDetail} />
                    </button>
                  </span>
                </Tooltip>
              );
            })}
          </div>
        )}
      </Card>

      <Modal
        open={!!selectedExplain}
        title={selectedExplain ? `${t("tdExplainTitle")}: ${selectedExplain.symbol} ${selectedExplain.name}` : t("tdExplainTitle")}
        onCancel={() => setSelectedExplain(null)}
        footer={<Button onClick={() => setSelectedExplain(null)}>{t("tdClose")}</Button>}
        width={720}
      >
        <div className="decision-explain-grid">
          {explainRows.map(([label, value]) => (
            <div key={label}>
              <span>{label}</span>
              <strong>{value}</strong>
            </div>
          ))}
        </div>
      </Modal>

      <Modal
        open={repairModalOpen}
        title={t("tdRepairModalTitle")}
        onCancel={() => setRepairModalOpen(false)}
        footer={(
          <Space>
            <Button onClick={() => setRepairModalOpen(false)}>{t("tdClose")}</Button>
            <Button type="primary" icon={<ReloadOutlined />} loading={repairAllLoading} onClick={() => repairAllSamples(allRepairSamples)}>
              {t("tdRepairAll")}
            </Button>
          </Space>
        )}
        width={720}
      >
        <div style={{ maxHeight: 480, overflowY: "auto" }}>
          <div className="decision-health-repair-list">
            {allRepairSamples.map((item) => (
              <div key={`modal-${item.reason}-${item.symbol_id}`} className="decision-health-repair-row">
                <div>
                  <Tag color={item.tagColor}>{item.repairKind}</Tag>
                  <strong>{item.symbol}</strong>
                  <span>{item.name}</span>
                  <Text type="secondary">
                    {item.latest_trade_date ? `${t("tdLatestBar")}: ${item.latest_trade_date} / ${ageText(item.latest_age_days)}` : t("tdMissingReason")}
                  </Text>
                </div>
                <Button size="small" icon={<ReloadOutlined />} loading={repairingSymbolId === item.symbol_id} onClick={() => repairSymbol(item)}>{t("tdRepair")}</Button>
              </div>
            ))}
          </div>
        </div>
      </Modal>
    </div>
  );
}
