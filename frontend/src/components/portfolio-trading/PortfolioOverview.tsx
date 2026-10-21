import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Shield, ShieldCheck, Activity, LineChart, Settings2, AlertTriangle, ShieldAlert, type LucideIcon } from "lucide-react";
import { message } from "antd";
import { api } from "../../api/client";
import { actionLabel, t } from "../../i18n";
import { money, percent } from "../../utils/format";
import { govStatusCodeForTooltip, govStatusLabel } from "../../utils/govStatusLabel";
import type { AllocationSnapshot, PortfolioStatePermissions, PortfolioStatusResponse, Position, SignalRule } from "../../types";

/**
 * PortfolioOverview — 账户总览子 Tab（Task 4）
 *
 * 一比一还原原型图「应用主框架.html」账户总览区域：
 *   1. 5 列等分账户摘要带（总资产 / 可用现金 / 持仓市值 / 累计收益 / 最大回撤）
 *   2. 自动接管配置卡片（左）+ 风险规则卡片（右）
 *   3. 收益曲线卡片（2fr）+ 持仓分布卡片（1fr）
 *
 * 数据来源（useEffect + 现有 api client，Promise.allSettled 容错，单接口失败不阻塞 UI）：
 *   - GET /portfolios            → 组合基本信息（auto_trade_enabled 等）
 *   - GET /portfolios/{id}/performance → 绩效（累计收益 / 最大回撤 / 夏普 / 净值曲线）
 *   - GET /portfolios/{id}/allocation  → 现金 / 持仓市值 / 现金占比 / 持仓只数
 *   - GET /portfolios/{id}/positions   → 持仓分布（代码 / 名称 / 占比）
 *
 * i18n：使用 t('portfolioTrading.overview.xxx')，key 由 Task 13 补全，缺失时返回 key 字符串不阻塞。
 */
interface PortfolioOverviewProps {
  portfolioId: number;
  onNavigate?: (tab: string) => void;
  // FR-P1-8a HG1
  portfolioStatus?: PortfolioStatusResponse | null;
  perm?: PortfolioStatePermissions;
}

interface EquityPoint {
  date: string;
  equity: number;
}

interface PerformanceStats {
  total_return?: number | null;
  total_return_pct?: number | null;
  max_drawdown?: number | null;
  max_drawdown_pct?: number | null;
  sharpe_ratio?: number | null;
}

interface PerformanceResult {
  snapshot_count?: number;
  date_range?: { start: string | null; end: string | null };
  equity_curve?: EquityPoint[];
  stats?: PerformanceStats;
}

interface PortfolioMeta {
  id: number;
  total_capital?: number;
  auto_trade_enabled?: number;
}

/** GET /portfolios/{id} 返回的 active_rule，用于总览页配置值回显 */
interface ActiveRule {
  id: number;
  rule_name?: string;
  max_single_position_pct?: number | null;
  max_loss_per_trade_pct?: number | null;
}

type ChartRange = "1m" | "3m" | "1y" | "all";
type RiskKey = "stopLoss" | "drawdown" | "volatility";
const OVERVIEW_RESOURCES = ["account", "performance", "allocation", "positions", "signal", "detail"] as const;
type OverviewResource = (typeof OVERVIEW_RESOURCES)[number];

const CHART_RANGES: { key: ChartRange; label: string }[] = [
  { key: "1m", label: t("portfolioTrading.overview.range1m") },
  { key: "3m", label: t("portfolioTrading.overview.range3m") },
  { key: "1y", label: t("portfolioTrading.overview.range1y") },
  { key: "all", label: t("portfolioTrading.overview.rangeAll") },
];

const RISK_RULE_DEFS: { key: RiskKey; Icon: LucideIcon; tone: "warning" | "error" | "info" }[] = [
  { key: "stopLoss", Icon: Shield, tone: "warning" },
  { key: "drawdown", Icon: ShieldCheck, tone: "error" },
  { key: "volatility", Icon: Activity, tone: "info" },
];

const TONE_VAR: Record<"warning" | "error" | "info", { bg: string; color: string }> = {
  warning: { bg: "var(--pt-state-warning-dim)", color: "var(--pt-state-warning)" },
  error: { bg: "var(--pt-state-error-dim)", color: "var(--pt-state-error)" },
  info: { bg: "var(--pt-state-info-dim)", color: "var(--pt-state-info)" },
};

const sectionTitleStyle: React.CSSProperties = { fontSize: 14, fontWeight: 600, color: "var(--pt-foreground)", margin: 0 };
const sectionSubStyle: React.CSSProperties = { fontSize: 12, color: "var(--pt-muted-foreground)", margin: "2px 0 0 0" };

/** 金额占位：缺失/无效时显示 ¥-- */
const fmtMoney = (v: number | null | undefined): string =>
  v == null || !Number.isFinite(Number(v)) ? "¥--" : money(v, 2);

/** 百分比占位：缺失/无效时显示 --%，可选正号 */
const fmtPct = (v: number | null | undefined, withSign = false): string => {
  if (v == null || !Number.isFinite(Number(v))) return "--%";
  const s = percent(v);
  return withSign && Number(v) > 0 ? "+" + s : s;
};

/** 收益正绿负红，0/缺失 muted */
const pnlColor = (v: number | null | undefined): string => {
  if (v == null || !Number.isFinite(Number(v))) return "var(--pt-muted-foreground)";
  return Number(v) > 0 ? "var(--pt-state-success)" : Number(v) < 0 ? "var(--pt-state-error)" : "var(--pt-muted-foreground)";
};

// 名称沿用后端 signal_rules.PRESETS；mode 表示样本匹配条件，不是调仓频率。
const SIGNAL_MODE_NAMES: Record<string, string> = {
  conservative: "保守模式", balanced: "均衡模式", aggressive: "进取模式", expert: "专家模式",
};

const PortfolioOverview: React.FC<PortfolioOverviewProps> = ({ portfolioId, onNavigate, portfolioStatus, perm }) => {
  // FR-P1-8a HG1：禁止开启自动交易的状态（终态硬禁）；另外 requires_manual_ack 必须手动确认解除
  const currentState = portfolioStatus?.current_state ?? "UNKNOWN";
  const allowAutoRecovery = perm?.allow_auto_recovery ?? false;
  const requiresManualAck = perm?.requires_manual_ack ?? false;
  // 硬禁：ADMIN_PAUSED / RECONCILIATION_BLOCKED / PENDING_INITIAL_REVIEW / MODEL_INACTIVE + requires_manual_ack
  const hardBlockAutoOn = currentState === "ADMIN_PAUSED" || currentState === "RECONCILIATION_BLOCKED" ||
    currentState === "PENDING_INITIAL_REVIEW" || currentState === "MODEL_INACTIVE" || requiresManualAck;
  const softWarningAutoOn = currentState !== "READY";
  const hardBlockHint = (() => {
    switch (currentState) {
      case "ADMIN_PAUSED": return "组合已暂停，请到“检查与对账”查看原因并处理。";
      case "RECONCILIATION_BLOCKED": return "请到“检查与对账”核对差异，处理后再继续。";
      case "PENDING_INITIAL_REVIEW": return "组合还未完成首次检查，请到“检查与对账”确认。";
      case "MODEL_INACTIVE": return "当前策略不能用于交易，请到“策略规则”检查。";
      default:
        return requiresManualAck ? "还有事项需要你确认，请到“检查与对账”处理。" : "请到“检查与对账”查看当前限制。";
    }
  })();

  const [loading, setLoading] = useState(true);
  const [portfolio, setPortfolio] = useState<PortfolioMeta | null>(null);
  const [perf, setPerf] = useState<PerformanceResult | null>(null);
  const [allocation, setAllocation] = useState<AllocationSnapshot | null>(null);
  const [positions, setPositions] = useState<Position[]>([]);
  const [signalRule, setSignalRule] = useState<SignalRule | null>(null);
  const [activeRule, setActiveRule] = useState<ActiveRule | null>(null);
  const [chartRange, setChartRange] = useState<ChartRange>("3m");
  const [autoEnabled, setAutoEnabled] = useState<boolean | null>(null);
  const [autoSaving, setAutoSaving] = useState(false);
  const [loadErrors, setLoadErrors] = useState<Partial<Record<OverviewResource, string>>>({});
  const [retrying, setRetrying] = useState<OverviewResource | null>(null);
  const [loadedPortfolioId, setLoadedPortfolioId] = useState<number | null>(null);
  const currentPortfolioIdRef = useRef(portfolioId);
  currentPortfolioIdRef.current = portfolioId;
  const loadRequestRef = useRef(0);
  const autoRequestRef = useRef(0);
  const mountedRef = useRef(true);

  const load = useCallback(async (retryResource?: OverviewResource) => {
    if (!portfolioId) {
      setLoading(false);
      return;
    }
    const requestId = ++loadRequestRef.current;
    if (!retryResource) {
      setLoading(true);
      setAutoSaving(false);
    }
    setRetrying(retryResource ?? null);
    const resources: readonly OverviewResource[] = retryResource ? [retryResource] : OVERVIEW_RESOURCES;
    const loaders: Record<OverviewResource, () => Promise<unknown>> = {
      account: () => api.getPortfolios(),
      performance: () => api.getPortfolioPerformance(portfolioId, {}),
      allocation: () => api.getAllocation(portfolioId),
      positions: () => api.getPositions(portfolioId),
      signal: () => api.getSignalRule(portfolioId),
      detail: () => api.getPortfolioDetail(portfolioId),
    };
    const results = await Promise.allSettled(resources.map((resource) => loaders[resource]()));
    if (!mountedRef.current || requestId !== loadRequestRef.current || portfolioId !== currentPortfolioIdRef.current) return;
    const errors: Partial<Record<OverviewResource, string>> = {};
    results.forEach((result, index) => {
      const resource = resources[index];
      const value = result.status === "fulfilled" ? result.value : null;
      if (result.status === "rejected") {
        const detail = result.reason instanceof Error ? result.reason.message : String(result.reason ?? "");
        errors[resource] = detail.trim() || t(`portfolioTrading.overview.${resource}LoadFailed`);
      }
      switch (resource) {
        case "account": {
          const found = (Array.isArray(value) ? value as PortfolioMeta[] : []).find((p) => Number(p.id) === Number(portfolioId)) ?? null;
          setPortfolio(found);
          setAutoEnabled(found ? Number(found.auto_trade_enabled) === 1 : null);
          if (!found && result.status === "fulfilled") errors.account = t("portfolioTrading.overview.selectedAccountMissing");
          break;
        }
        case "performance": setPerf(value as PerformanceResult | null); break;
        case "allocation": setAllocation(value as AllocationSnapshot | null); break;
        case "positions": setPositions((value as Position[] | null) ?? []); break;
        case "signal": setSignalRule(value as SignalRule | null); break;
        case "detail": setActiveRule((value as { active_rule?: ActiveRule | null } | null)?.active_rule ?? null); break;
      }
    });
    setLoadErrors((previous) => {
      const next = { ...previous };
      resources.forEach((resource) => { delete next[resource]; });
      return { ...next, ...errors };
    });
    setRetrying(null);
    setLoadedPortfolioId(portfolioId);
    setLoading(false);
  }, [portfolioId]);

  useEffect(() => {
    mountedRef.current = true;
    void load();
    return () => {
      mountedRef.current = false;
      loadRequestRef.current += 1;
      autoRequestRef.current += 1;
    };
  }, [load]);

  const stats = perf?.stats;
  const totalReturnPct = stats?.total_return_pct ?? null;
  const maxDrawdownPct = stats?.max_drawdown_pct ?? null;
  const sharpe = stats?.sharpe_ratio ?? null;
  const cumulativePnL = stats?.total_return ?? null;

  const totalAssetsValue = allocation?.cash_amount != null && allocation.used_amount != null
    ? allocation.cash_amount + allocation.used_amount : null;
  const positionCount = allocation?.position_count ?? null;
  const holdingsTotalPct = allocation?.total_position_pct ?? null;

  // 年化收益：基于绩效日期区间对累计收益年化
  const annualizedPct = useMemo(() => {
    if (totalReturnPct == null) return null;
    const start = perf?.date_range?.start;
    const end = perf?.date_range?.end;
    if (!start || !end) return null;
    const startTime = new Date(start).getTime();
    const endTime = new Date(end).getTime();
    if (!Number.isFinite(startTime) || !Number.isFinite(endTime)) return null;
    const days = Math.max(1, (endTime - startTime) / 86400000);
    const years = days / 365;
    if (years <= 0) return null;
    return Math.pow(1 + totalReturnPct, 1 / years) - 1;
  }, [totalReturnPct, perf]);

  // 收益曲线 SVG 折线（按时间切换过滤可见区间）
  const sparkline = useMemo(() => {
    const curve = perf?.equity_curve ?? [];
    if (!curve.length) return null;
    let visible = curve;
    if (chartRange !== "all") {
      const days = chartRange === "1m" ? 30 : chartRange === "3m" ? 90 : 365;
      const cutoff = Date.now() - days * 86400000;
      const filtered = curve.filter((p) => new Date(p.date).getTime() >= cutoff);
      visible = filtered.length >= 2 ? filtered : curve;
    }
    const values = visible.map((p) => Number(p.equity));
    const min = Math.min(...values);
    const max = Math.max(...values);
    const range = max - min || 1;
    const n = visible.length;
    const pts = visible.map((p, i) => {
      const x = n > 1 ? (i / (n - 1)) * 100 : 0;
      const y = 40 - ((Number(p.equity) - min) / range) * 36 - 2;
      return { x, y };
    });
    const linePath = pts.map((p, i) => `${i === 0 ? "M" : "L"}${p.x.toFixed(2)},${p.y.toFixed(2)}`).join(" ");
    const areaPath = `${linePath} L100,40 L0,40 Z`;
    return { linePath, areaPath };
  }, [perf, chartRange]);

  // 自动接管配置卡片的 4 个配置值：优先 signal-rule，回退 active_rule，再回退占位「—」
  const configValues = useMemo(() => {
    const ruleName = signalRule?.rule_name || activeRule?.rule_name;
    const model = ruleName ? (SIGNAL_MODE_NAMES[ruleName] ?? ruleName) : "—";
    const freq = signalRule?.mode ? (SIGNAL_MODE_NAMES[signalRule.mode] ?? "请到策略规则查看") : "—";
    const maxSingle = activeRule?.max_single_position_pct ?? null;
    const maxLoss = activeRule?.max_loss_per_trade_pct ?? null;
    return {
      currentModel: model,
      rebalanceFreq: freq,
      maxSinglePosition: maxSingle != null ? fmtPct(maxSingle) : "—",
      maxDrawdownThreshold: maxLoss != null ? fmtPct(maxLoss) : "—",
    };
  }, [signalRule, activeRule]);
  const topHoldings = useMemo(
    () =>
      [...positions]
        .sort((a, b) => (b.position_pct ?? 0) - (a.position_pct ?? 0))
        .slice(0, 8),
    [positions],
  );

  const handleDetailConfig = useCallback(() => {
    onNavigate?.("strategy");
  }, [onNavigate]);

  // 自动接管开关：调 api.updatePortfolio 持久化 auto_trade_enabled，失败回滚
  // FR-P1-8a HG1：硬禁状态下禁止从 OFF → ON；其它非 READY 开启前二次确认
  const handleToggleAuto = useCallback(async () => {
    if (autoEnabled === null || autoSaving || loadedPortfolioId !== portfolioId) return;
    const prev = autoEnabled;
    const next = !prev;
    // 从 OFF → ON 必须检查 HG1
    if (!prev && next) {
      if (hardBlockAutoOn) {
        message.error(hardBlockHint);
        return;
      }
      if (softWarningAutoOn) {
        const ok = window.confirm(
          `当前组合还没有准备好：${govStatusLabel(currentState)}。\n\n` +
          `打开开关后，下单仍受以下限制：\n` +
          `  · ${actionLabel("NEW_BUY")}：${perm?.allow_new_buys ? "允许" : "禁止"}\n` +
          `  · ${actionLabel("RISK_EXIT")}：${perm?.allow_risk_exits ? "允许" : "禁止"}\n` +
          `  · 自动恢复：${allowAutoRecovery ? "允许" : "禁止"}\n\n` +
          `如果之后暂停组合或发现对账差异，系统会关闭自动交易并提醒你。\n\n确认打开开关？`,
        );
        if (!ok) return;
      }
    }
    const targetPortfolioId = portfolioId;
    const requestId = ++autoRequestRef.current;
    const isCurrent = () => mountedRef.current && requestId === autoRequestRef.current && targetPortfolioId === currentPortfolioIdRef.current;
    setAutoSaving(true);
    setAutoEnabled(next);
    try {
      await api.updatePortfolio(targetPortfolioId, { auto_trade_enabled: next ? 1 : 0 });
      if (!isCurrent()) return;
      setAutoEnabled(next);
      message.success(next ? t("portfolioTrading.overview.autoTradeOn") : t("portfolioTrading.overview.autoTradeOff"));
    } catch (err: any) {
      if (!isCurrent()) return;
      setAutoEnabled(prev);
      message.error(t("portfolioTrading.overview.autoTradeToggleFailed") + (err?.message ? ": " + err.message : ""));
    } finally {
      if (isCurrent()) setAutoSaving(false);
    }
  }, [autoEnabled, autoSaving, loadedPortfolioId, portfolioId, hardBlockAutoOn, hardBlockHint, softWarningAutoOn, currentState, perm, allowAutoRecovery]);

  const handleViewAllHoldings = useCallback(() => {
    onNavigate?.("members");
  }, [onNavigate]);

  const renderLoadError = (resource: OverviewResource) => loadErrors[resource] ? (
    <div role="alert" aria-label={t(`portfolioTrading.overview.${resource}LoadFailed`)} style={{ padding: 12, color: "var(--pt-state-error)" }}>
      <p>{t(`portfolioTrading.overview.${resource}LoadFailed`)}：{loadErrors[resource]}</p>
      <button type="button" className="pt-btn pt-btn-secondary pt-btn-sm" disabled={retrying !== null} onClick={() => { void load(resource); }}>
        {t(`portfolioTrading.overview.${resource}Retry`)}
      </button>
    </div>
  ) : null;

  if (!portfolioId) return <section role="status" style={{ padding: 16 }}>{t("portfolioTrading.header.noPortfolio")}</section>;

  // 当前组合的数据尚未到达时，在本次渲染就隐藏上个组合的数据与错误。
  if (loading || loadedPortfolioId !== portfolioId) {
    return (
      <section role="status" aria-label={t("portfolioTrading.overview.loadingOverview")} style={{ padding: 16, display: "flex", flexDirection: "column", gap: 16 }}>
        <style>{`@keyframes pt-sk-pulse{0%,100%{opacity:1}50%{opacity:0.4}}.pt-sk{animation:pt-sk-pulse 1.4s ease-in-out infinite;}`}</style>
        <div className="pt-card" style={{ padding: 24 }}>
          <div className="pt-sk" style={{ background: "var(--pt-surface-3)", borderRadius: "var(--pt-radius-md)", height: 18, width: "30%", marginBottom: 20 }} />
          <div style={{ display: "grid", gridTemplateColumns: "repeat(5, 1fr)" }}>
            {[0, 1, 2, 3, 4].map((i) => (
              <div key={i} style={{ padding: 16, borderLeft: i === 0 ? "none" : "1px solid var(--pt-border)" }}>
                <div className="pt-sk" style={{ background: "var(--pt-surface-3)", borderRadius: 4, height: 12, width: "60%", marginBottom: 12 }} />
                <div className="pt-sk" style={{ background: "var(--pt-surface-3)", borderRadius: 4, height: 22, width: "80%", marginBottom: 12 }} />
                <div className="pt-sk" style={{ background: "var(--pt-surface-3)", borderRadius: 4, height: 12, width: "50%" }} />
              </div>
            ))}
          </div>
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
          <div className="pt-card" style={{ padding: 16, height: 200 }} />
          <div className="pt-card" style={{ padding: 16, height: 200 }} />
        </div>
      </section>
    );
  }

  return (
    <section style={{ padding: 16, display: "flex", flexDirection: "column", gap: 16 }}>
      {renderLoadError("allocation")}
      {/* ========== SubTask 4.1: 5 列账户摘要带 ========== */}
      <div className="pt-card" style={{ overflow: "hidden" }}>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(5, 1fr)" }}>
          {/* 1. 总资产 */}
          <SummaryCell
            label={t("portfolioTrading.overview.totalAssets")}
            big
            first
            main={fmtMoney(totalAssetsValue)}
            sub={
              <>
                {t("portfolioTrading.overview.cumulativePnL")}{" "}
                <span style={{ color: pnlColor(cumulativePnL) }}>{fmtMoney(cumulativePnL)}</span>
              </>
            }
          />
          {/* 2. 可用现金 */}
          <SummaryCell
            label={t("portfolioTrading.overview.cash")}
            main={fmtMoney(allocation?.cash_amount ?? null)}
            sub={
              <>
                {t("portfolioTrading.overview.cashRatio")}{" "}
                <span style={{ color: "var(--pt-muted-foreground)" }}>{fmtPct(allocation?.cash_pct ?? null)}</span>
              </>
            }
          />
          {/* 3. 持仓市值 */}
          <SummaryCell
            label={t("portfolioTrading.overview.holdingsValue")}
            main={fmtMoney(allocation?.used_amount ?? null)}
            sub={
              <>
                {t("portfolioTrading.overview.holdingsCount")}{" "}
                <span style={{ color: "var(--pt-muted-foreground)" }}>{positionCount != null ? positionCount : "--"}</span>
              </>
            }
          />
          {/* 4. 累计收益 */}
          <SummaryCell
            label={t("portfolioTrading.overview.totalReturn")}
            main={<span style={{ color: pnlColor(totalReturnPct) }}>{fmtPct(totalReturnPct, true)}</span>}
            sub={
              <>
                {t("portfolioTrading.overview.annualized")}{" "}
                <span style={{ color: pnlColor(annualizedPct) }}>{fmtPct(annualizedPct, true)}</span>
              </>
            }
          />
          {/* 5. 最大回撤 */}
          <SummaryCell
            label={t("portfolioTrading.overview.maxDrawdown")}
            main={
              <span style={{ color: Number(maxDrawdownPct) < 0 ? "var(--pt-state-error)" : "var(--pt-muted-foreground)" }}>
                {fmtPct(maxDrawdownPct)}
              </span>
            }
            sub={
              <>
                {t("portfolioTrading.overview.sharpe")}{" "}
                <span style={{ color: "var(--pt-primary)" }}>{sharpe != null ? sharpe.toFixed(2) : "--"}</span>
              </>
            }
          />
        </div>
      </div>

      {/* ========== SubTask 4.2 + 4.3: 自动接管配置 + 风险规则 ========== */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
        {/* 自动接管配置卡片 */}
        <div className="pt-card" style={{ padding: 16 }}>
          <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", marginBottom: 16 }}>
            <div>
              <h3 style={sectionTitleStyle}>{t("portfolioTrading.overview.autoTradeTitle")}</h3>
              <p style={sectionSubStyle}>{t("portfolioTrading.overview.autoTradeSub")}</p>
              {/* FR-P1-8a: HG1 状态小字提示 */}
              {currentState !== "READY" && (
                <div title={govStatusCodeForTooltip(currentState) ?? undefined} style={{ marginTop: 6, fontSize: 11, color: hardBlockAutoOn ? "var(--pt-state-error, #ef4444)" : "var(--pt-state-warning, #f59e0b)" }}>
                  {hardBlockAutoOn ? <ShieldAlert size={11} style={{ display: "inline", verticalAlign: "-1px", marginRight: 4 }} /> : <AlertTriangle size={11} style={{ display: "inline", verticalAlign: "-1px", marginRight: 4 }} />}
                  {/* currentState 是治理枚举原值（如 RECONCILIATION_BLOCKED），不能直接给用户看 */}
                  {autoEnabled ? (hardBlockAutoOn ? "自动交易开关已打开，当前仍不能下单。" : "自动交易开关已打开，下单仍受当前交易限制。") : (hardBlockAutoOn ? "当前不能开启自动交易。" : "打开开关前，请先查看当前交易限制。")}
                  <div style={{ marginTop: 4 }}>{hardBlockHint}</div>
                  {onNavigate && <button type="button" className="pt-btn pt-btn-secondary pt-btn-sm" style={{ marginTop: 8 }} onClick={() => onNavigate(currentState === "MODEL_INACTIVE" ? "strategy" : "governance")}>
                    {currentState === "MODEL_INACTIVE" ? "检查策略规则" : "查看待处理事项"}
                  </button>}
                </div>
              )}
            </div>
            <button
              type="button"
              className={`pt-toggle${autoEnabled ? " active" : ""}`}
              aria-label={t("portfolioTrading.overview.autoTradeToggle")}
              aria-pressed={autoEnabled ?? undefined}
              aria-busy={autoSaving}
              onClick={handleToggleAuto}
              disabled={autoEnabled === null || autoSaving || (hardBlockAutoOn && !autoEnabled)}
              title={hardBlockAutoOn && !autoEnabled ? hardBlockHint : t("portfolioTrading.overview.autoTradeToggle")}
              style={{
                opacity: hardBlockAutoOn && !autoEnabled ? 0.45 : 1,
                cursor: hardBlockAutoOn && !autoEnabled ? "not-allowed" : undefined,
                boxShadow: (hardBlockAutoOn && !autoEnabled) ? "0 0 0 1px rgba(239,68,68,0.15)" : undefined,
              }}
            />
          </div>
          {autoEnabled === null && <p style={sectionSubStyle}>{t("portfolioTrading.overview.autoStateUnknown")}</p>}
          {renderLoadError("account")}
          {renderLoadError("signal")}
          {renderLoadError("detail")}
          <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            <ConfigRow label={t("portfolioTrading.overview.currentModel")} value={configValues.currentModel} />
            <ConfigRow label={t("portfolioTrading.overview.rebalanceFreq")} value={configValues.rebalanceFreq} />
            <ConfigRow label={t("portfolioTrading.overview.maxSinglePosition")} value={configValues.maxSinglePosition} />
            <ConfigRow label={t("portfolioTrading.overview.maxDrawdownThreshold")} value={configValues.maxDrawdownThreshold} />
          </div>
          <div style={{ marginTop: 16, paddingTop: 16, borderTop: "1px solid var(--pt-border)" }}>
            <button
              type="button"
              className="pt-btn pt-btn-secondary pt-btn-sm"
              style={{ width: "100%" }}
              onClick={handleDetailConfig}
            >
              <Settings2 size={14} />
              {t("portfolioTrading.overview.detailConfig")}
            </button>
          </div>
        </div>

        {/* 仅说明规则含义；未绑定执行的控件不能表示组合已受保护。 */}
        <section className="pt-card" aria-label={t("portfolioTrading.overview.riskRulesRegion")} style={{ padding: 16 }}>
          <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", marginBottom: 16 }}>
            <div>
              <h3 style={sectionTitleStyle}>{t("portfolioTrading.overview.riskRulesTitle")}</h3>
              <p style={sectionSubStyle}>{t("portfolioTrading.overview.riskRulesSub")}</p>
            </div>
            <span className="pt-tag pt-tag-warning">
              {t("portfolioTrading.overview.riskRulesReadOnly")}
            </span>
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            {RISK_RULE_DEFS.map(({ key, Icon, tone }) => (
              <RiskRuleRow
                key={key}
                Icon={Icon}
                tone={tone}
                title={t(`portfolioTrading.overview.risk_${key}`)}
                desc={t(`portfolioTrading.overview.risk_${key}_desc`)}
              />
            ))}
          </div>
          {onNavigate && (
            <button type="button" className="pt-btn pt-btn-secondary pt-btn-sm" style={{ marginTop: 16 }} onClick={handleDetailConfig}>
              {t("portfolioTrading.overview.riskRulesViewConfig")}
            </button>
          )}
        </section>
      </div>

      {/* ========== SubTask 4.4: 收益曲线 + 持仓分布 ========== */}
      <div style={{ display: "grid", gridTemplateColumns: "2fr 1fr", gap: 16 }}>
        {/* 收益曲线卡片 */}
        <div className="pt-card" style={{ padding: 16 }}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 16 }}>
            <div>
              <h3 style={sectionTitleStyle}>{t("portfolioTrading.overview.equityCurveTitle")}</h3>
              <p style={sectionSubStyle}>{t("portfolioTrading.overview.equityCurveSub")}</p>
            </div>
            <div style={{ display: "flex", gap: 4 }}>
              {CHART_RANGES.map((r) => (
                <button
                  key={r.key}
                  type="button"
                  className={`pt-btn pt-btn-sm ${chartRange === r.key ? "pt-btn-secondary" : "pt-btn-ghost"}`}
                  onClick={() => setChartRange(r.key)}
                >
                  {r.label}
                </button>
              ))}
            </div>
          </div>
          <div
            style={{
              height: 224,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              borderRadius: "var(--pt-radius-lg)",
              background: "var(--pt-surface-3)",
            }}
          >
            {loadErrors.performance ? renderLoadError("performance") : sparkline ? (
              <svg viewBox="0 0 100 40" preserveAspectRatio="none" style={{ width: "100%", height: "100%", display: "block" }}>
                <defs>
                  <linearGradient id="pt-equity-fill" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="rgba(6,182,212,0.25)" />
                    <stop offset="100%" stopColor="rgba(6,182,212,0.02)" />
                  </linearGradient>
                </defs>
                <path d={sparkline.areaPath} fill="url(#pt-equity-fill)" stroke="none" />
                <path
                  d={sparkline.linePath}
                  fill="none"
                  stroke="var(--pt-primary)"
                  strokeWidth={1}
                  vectorEffect="non-scaling-stroke"
                />
              </svg>
            ) : (
              <div style={{ textAlign: "center" }}>
                <LineChart size={40} style={{ color: "var(--pt-muted-foreground)", opacity: 0.5, margin: "0 auto 8px", display: "block" }} />
                <p style={{ fontSize: 12, color: "var(--pt-muted-foreground)" }}>
                  {t("portfolioTrading.overview.chartPlaceholder")}
                </p>
              </div>
            )}
          </div>
        </div>

        {/* 持仓分布卡片 */}
        <div className="pt-card" style={{ padding: 16 }}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 16 }}>
            <h3 style={sectionTitleStyle}>{t("portfolioTrading.overview.holdingsTitle")}</h3>
            <button
              type="button"
              className="pt-btn pt-btn-sm pt-btn-ghost"
              style={{ height: "auto", padding: 0, color: "var(--pt-primary)" }}
              onClick={handleViewAllHoldings}
            >
              {t("portfolioTrading.overview.viewAll")}
            </button>
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            {loadErrors.positions ? renderLoadError("positions") : topHoldings.length === 0 ? (
              <p style={{ fontSize: 12, color: "var(--pt-muted-foreground)", textAlign: "center", padding: "24px 0" }}>
                {t("portfolioTrading.overview.noHoldings")}
              </p>
            ) : (
              topHoldings.map((p) => {
                const weightPct = Math.max(0, Math.min(100, (Number(p.position_pct) || 0) * 100));
                return (
                  <HoldingRow
                    key={p.symbol_id}
                    code={p.symbol}
                    name={p.name}
                    weightText={fmtPct(p.position_pct ?? null)}
                    weightPct={weightPct}
                  />
                );
              })
            )}
          </div>
          <div style={{ marginTop: 16, paddingTop: 16, borderTop: "1px solid var(--pt-border)" }}>
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", fontSize: 12 }}>
              <span style={{ color: "var(--pt-muted-foreground)" }}>{t("portfolioTrading.overview.holdingsCount")}</span>
              <span style={{ color: "var(--pt-foreground)" }}>{positionCount != null ? positionCount : "--"}</span>
            </div>
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", fontSize: 12, marginTop: 8 }}>
              <span style={{ color: "var(--pt-muted-foreground)" }}>{t("portfolioTrading.overview.holdingsTotal")}</span>
              <span style={{ color: "var(--pt-foreground)" }}>{fmtPct(holdingsTotalPct)}</span>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
};

/* ------------------------------------------------------------------ */
/* 模块级子组件（避免在组件内部定义组件，符合 re-render 最佳实践）       */
/* ------------------------------------------------------------------ */

const SummaryCell: React.FC<{
  label: string;
  big?: boolean;
  first?: boolean;
  main: React.ReactNode;
  sub: React.ReactNode;
}> = ({ label, big, first, main, sub }) => (
  <div style={{ padding: 16, borderLeft: first ? "none" : "1px solid var(--pt-border)" }}>
    <p style={{ fontSize: 12, color: "var(--pt-muted-foreground)", margin: "0 0 4px 0" }}>{label}</p>
    <p
      className="pt-mono"
      style={{
        fontSize: big ? 24 : 20,
        fontWeight: big ? 600 : 500,
        color: "var(--pt-foreground)",
        letterSpacing: "-0.02em",
        margin: 0,
      }}
    >
      {main}
    </p>
    <p className="pt-mono" style={{ fontSize: 12, color: "var(--pt-muted-foreground)", margin: "4px 0 0 0" }}>
      {sub}
    </p>
  </div>
);

const ConfigRow: React.FC<{ label: string; value: string }> = ({ label, value }) => (
  <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
    <span style={{ fontSize: 12, color: "var(--pt-muted-foreground)" }}>{label}</span>
    <span style={{ fontSize: 12, color: "var(--pt-foreground)", fontWeight: 500 }}>{value}</span>
  </div>
);

const RiskRuleRow: React.FC<{
  Icon: LucideIcon;
  tone: "warning" | "error" | "info";
  title: string;
  desc: string;
}> = ({ Icon, tone, title, desc }) => {
  const toneVar = TONE_VAR[tone];
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 12,
        padding: 10,
        borderRadius: "var(--pt-radius-lg)",
        background: "var(--pt-surface-3)",
      }}
    >
      <div
        style={{
          width: 32,
          height: 32,
          borderRadius: "var(--pt-radius-md)",
          background: toneVar.bg,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          flexShrink: 0,
        }}
      >
        <Icon size={16} style={{ color: toneVar.color }} />
      </div>
      <div style={{ flex: 1, minWidth: 0 }}>
        <p style={{ fontSize: 13, fontWeight: 500, color: "var(--pt-foreground)", margin: 0 }}>{title}</p>
        <p style={{ fontSize: 12, color: "var(--pt-muted-foreground)", margin: 0 }}>{desc}</p>
      </div>
      <span style={{ fontSize: 12, color: "var(--pt-muted-foreground)", flexShrink: 0 }}>
        {t("portfolioTrading.overview.riskRuleNotConnected")}
      </span>
    </div>
  );
};

const HoldingRow: React.FC<{ code: string; name: string; weightText: string; weightPct: number }> = ({
  code,
  name,
  weightText,
  weightPct,
}) => (
  <div>
    <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 6 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <span style={{ fontSize: 13, fontWeight: 500, color: "var(--pt-foreground)" }}>{code}</span>
        <span style={{ fontSize: 12, color: "var(--pt-muted-foreground)" }}>{name}</span>
      </div>
      <span className="pt-mono" style={{ fontSize: 13, color: "var(--pt-foreground)" }}>{weightText}</span>
    </div>
    <div className="pt-progress">
      <div className="pt-progress-fill" style={{ width: `${weightPct}%` }} />
    </div>
  </div>
);

export default PortfolioOverview;
