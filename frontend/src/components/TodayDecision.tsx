import { useEffect, useMemo, useState } from "react";
import { Alert, Button, Card, Col, Empty, Modal, Progress, Row, Space, Tag, Typography } from "antd";
import {
  AlertOutlined,
  ArrowRightOutlined,
  BarChartOutlined,
  CheckCircleOutlined,
  DatabaseOutlined,
  ExperimentOutlined,
  FireOutlined,
  InfoCircleOutlined,
  LoadingOutlined,
  ReloadOutlined,
  SafetyOutlined,
} from "@ant-design/icons";
import { api, type FactorOverview } from "../api/client";
import { actionLabel, stageLabel } from "../i18n";
import { useApp } from "../context/AppContext";
import { baseOpportunityScoreValue, formatRelativeTime, opportunityScoreValue, score, withFinalOpportunityScore } from "../utils/format";
import type { DataHealth, DataHealthBarIssue, MacroOverview, MarketEvent, WorkbenchCandidate } from "../types";

const { Text } = Typography;

const LABELS = {
  "zh-CN": {
    title: "\u4eca\u65e5\u51b3\u7b56",
    subtitle: "\u5148\u770b\u7ed3\u8bba\uff0c\u518d\u8fdb\u5165\u8be6\u60c5\u3002",
    refresh: "\u5237\u65b0\u51b3\u7b56\u53f0",
    market: "\u5e02\u573a\u73af\u5883",
    opportunities: "\u4eca\u65e5\u673a\u4f1a",
    risks: "\u91cd\u5927\u98ce\u9669",
    position: "\u4ed3\u4f4d\u72b6\u6001",
    actions: "\u4eca\u65e5\u5f85\u529e",
    topOpportunities: "\u673a\u4f1a Top 5",
    topNews: "\u91cd\u5927\u6d88\u606f Top 5",
    noData: "\u6682\u65e0\u6570\u636e\uff0c\u53ef\u4ee5\u5148\u6267\u884c\u673a\u4f1a\u6316\u6398\u3001\u5b8f\u89c2\u66f4\u65b0\u548c\u6d88\u606f\u91c7\u96c6\u3002",
    viewDetail: "\u67e5\u770b",
    explain: "\u89e3\u91ca",
    goDiscovery: "\u53bb\u673a\u4f1a\u6316\u6398",
    goNews: "\u770b\u6d88\u606f",
    goInvestment: "\u770b\u8ba1\u5212",
    active: "\u504f\u79ef\u6781",
    neutral: "\u4e2d\u6027",
    cautious: "\u504f\u8c28\u614e",
    defensive: "\u9632\u5b88",
    safe: "\u5b89\u5168",
    high: "\u504f\u9ad8",
    empty: "\u6682\u65e0",
    healthTitle: "\u6570\u636e\u53ef\u4fe1\u5ea6",
    healthOk: "\u6570\u636e\u6b63\u5e38",
    healthWarn: "\u9700\u5173\u6ce8",
    healthError: "\u9700\u5148\u4fee\u590d",
    symbols: "\u6807\u7684\u5e93",
    barCoverage: "K\u7ebf\u8986\u76d6",
    staleBars: "\u8fc7\u671fK\u7ebf",
    missingBars: "\u7f3a\u5931K\u7ebf",
    outdatedBars: "\u8d85\u671fK\u7ebf",
    repairSamples: "\u4f18\u5148\u4fee\u590d\u6807\u7684",
    repair: "\u4fee\u590d",
    repairOk: "\u884c\u60c5\u5df2\u4fee\u590d",
    repairFailed: "\u4fee\u590d\u5931\u8d25",
    latestBar: "\u6700\u65b0K\u7ebf",
    missingReason: "\u7f3a\u5931",
    macroFreshness: "\u5b8f\u89c2\u65f6\u6548",
    news7d: "7\u5929\u6d88\u606f",
    discoveryFreshness: "\u673a\u4f1a\u65f6\u6548",
    frozen: "\u51bb\u7ed3",
    expired: "\u8fc7\u671f",
    noIssue: "\u6682\u672a\u53d1\u73b0\u660e\u663e\u6570\u636e\u95ee\u9898\u3002",
    loadDetailFailed: "\u52a0\u8f7d\u6807\u7684\u8be6\u60c5\u5931\u8d25",
    explainTitle: "\u673a\u4f1a\u5206\u89e3",
    finalScore: "\u6700\u7ec8\u673a\u4f1a\u5206",
    baseScore: "\u6280\u672f\u57fa\u7840\u5206",
    newsMultiplier: "\u6d88\u606f\u52a0\u6743\u7cfb\u6570",
    messageScore: "\u6d88\u606f\u9762\u5206",
    confidence: "\u6d88\u606f\u4fe1\u5fc3\u5ea6",
    quality: "\u80a1\u8d28",
    timing: "\u62e9\u65f6",
    trend: "\u8d8b\u52bf",
    momentum: "\u52a8\u91cf",
    volatility: "\u6ce2\u52a8",
    liquidity: "\u6d41\u52a8\u6027",
    breadth: "\u5e02\u573a\u5bbd\u5ea6",
    event: "\u4e8b\u4ef6",
    plan: "\u5efa\u8bae\u52a8\u4f5c",
    reasons: "\u89e6\u53d1\u539f\u56e0",
    freshness: "\u7ed3\u679c\u65f6\u6548",
    close: "\u5173\u95ed",
    factorTitle: "\u56e0\u5b50\u5f15\u64ce",
    factorMode: "\u8fd0\u884c\u6a21\u5f0f",
    factorScoreSource: "\u51b3\u7b56\u8bc4\u5206",
    factorModel: "\u6d3b\u52a8\u6a21\u578b",
    factorLatest: "\u6700\u65b0\u672c\u5730\u6570\u636e\u65e5",
    factorCoverage: "\u5e73\u5747\u8986\u76d6",
    factorManage: "\u7ba1\u7406\u56e0\u5b50\u6a21\u578b",
    factorUnavailable: "\u56e0\u5b50\u4ed3\u5e93\u4e0d\u53ef\u7528",
  },
  "en-US": {
    title: "Today Decision",
    subtitle: "Read the conclusion first, then drill into detail.",
    refresh: "Refresh Decision Desk",
    market: "Market Regime",
    opportunities: "Opportunities",
    risks: "Major Risks",
    position: "Position Status",
    actions: "Today To-do",
    topOpportunities: "Opportunity Top 5",
    topNews: "Major News Top 5",
    noData: "No data yet. Run discovery, macro update and news collection first.",
    viewDetail: "View",
    explain: "Explain",
    goDiscovery: "Open Mining",
    goNews: "Open News",
    goInvestment: "Open Plan",
    active: "Active",
    neutral: "Neutral",
    cautious: "Cautious",
    defensive: "Defensive",
    safe: "Safe",
    high: "High",
    empty: "Empty",
    healthTitle: "Data Trust",
    healthOk: "Healthy",
    healthWarn: "Needs Attention",
    healthError: "Fix First",
    symbols: "Symbols",
    barCoverage: "Bar Coverage",
    staleBars: "Stale Bars",
    missingBars: "Missing Bars",
    outdatedBars: "Outdated Bars",
    repairSamples: "Repair First",
    repair: "Repair",
    repairOk: "Market data repaired",
    repairFailed: "Repair failed",
    latestBar: "Latest Bar",
    missingReason: "Missing",
    macroFreshness: "Macro Freshness",
    news7d: "7D News",
    discoveryFreshness: "Discovery Freshness",
    frozen: "Frozen",
    expired: "Expired",
    noIssue: "No obvious data issue found.",
    loadDetailFailed: "Failed to load symbol detail",
    explainTitle: "Opportunity Breakdown",
    finalScore: "Final Score",
    baseScore: "Technical Base",
    newsMultiplier: "News Multiplier",
    messageScore: "News Score",
    confidence: "News Confidence",
    quality: "Quality",
    timing: "Timing",
    trend: "Trend",
    momentum: "Momentum",
    volatility: "Volatility",
    liquidity: "Liquidity",
    breadth: "Breadth",
    event: "Event",
    plan: "Suggested Action",
    reasons: "Reason Tags",
    freshness: "Freshness",
    close: "Close",
    factorTitle: "Factor Engine",
    factorMode: "Runtime Mode",
    factorScoreSource: "Decision Scores",
    factorModel: "Active Model",
    factorLatest: "Latest Local Data",
    factorCoverage: "Average Coverage",
    factorManage: "Manage Factor Models",
    factorUnavailable: "Factor warehouse unavailable",
  },
} as const;

type DecisionLabels = (typeof LABELS)[keyof typeof LABELS];

const TODO_ACTIONS = new Set(["open", "buy_dip", "hold"]);

function scoreValue(value: number | null | undefined) {
  if (value === null || value === undefined || Number.isNaN(value)) return "-";
  return value.toFixed(1);
}

function pct(value: number | null | undefined) {
  if (value === null || value === undefined || Number.isNaN(value)) return "-";
  return value.toFixed(1) + "%";
}

function marketLabel(value: number | null | undefined, labels: DecisionLabels) {
  if (value === null || value === undefined) return labels.empty;
  if (value >= 68) return labels.active;
  if (value <= 42) return labels.defensive;
  if (value <= 52) return labels.cautious;
  return labels.neutral;
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

function healthLabel(health: DataHealth | null, labels: DecisionLabels) {
  if (!health) return labels.empty;
  if (health.status === "ok") return labels.healthOk;
  if (health.status === "error") return labels.healthError;
  return labels.healthWarn;
}

function healthColor(health: DataHealth | null) {
  if (!health) return "#64748b";
  if (health.status === "ok") return "#0f766e";
  if (health.status === "error") return "#b42318";
  return "#d97706";
}

function ageText(days: number | null | undefined, locale: string) {
  if (days === null || days === undefined) return "-";
  if (days === 0) return locale === "en-US" ? "today" : "\u4eca\u5929";
  return locale === "en-US" ? `${days}d ago` : `${days}\u5929\u524d`;
}

function confidenceText(value: number | null | undefined, locale: string) {
  const n = Number(value ?? 0);
  if (n >= 0.7) return locale === "en-US" ? "high" : "\u9ad8";
  if (n >= 0.4) return locale === "en-US" ? "medium" : "\u4e2d";
  return locale === "en-US" ? "low" : "\u4f4e";
}

function reasonText(item: WorkbenchCandidate, labels: DecisionLabels) {
  return item.reason_tags?.length ? item.reason_tags.join(" / ") : labels.empty;
}

export default function TodayDecision() {
  const ctx = useApp();
  const labels = LABELS[ctx.locale as "zh-CN" | "en-US"] ?? LABELS["zh-CN"];
  const workbench = ctx.workbench;
  const [macro, setMacro] = useState<MacroOverview | null>(null);
  const [events, setEvents] = useState<MarketEvent[]>([]);
  const [health, setHealth] = useState<DataHealth | null>(null);
  const [factorOverview, setFactorOverview] = useState<FactorOverview | null>(null);
  const [selectedExplain, setSelectedExplain] = useState<WorkbenchCandidate | null>(null);
  const [repairingSymbolId, setRepairingSymbolId] = useState<number | null>(null);
  const [openingSymbolId, setOpeningSymbolId] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = async () => {
    setLoading(true);
    setError(null);
    try {
      const [macroData, newsData, healthData, factorData] = await Promise.all([
        api.getMacroOverview("all"),
        api.getMarketEvents({ importance_level_min: 3, limit: 8, sort_by: "importance_level" }),
        api.getDataHealth(),
        api.getFactorOverview().catch(() => null),
      ]);
      setMacro(macroData);
      setEvents(newsData.events ?? []);
      setHealth(healthData);
      setFactorOverview(factorData);
      await ctx.loadWorkbench();
    } catch (err: any) {
      setError(err.message || "Failed to load decision desk");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, []);

  const macroScore = macro?.snapshot?.market_score ?? null;
  const candidates = useMemo(() => {
    const rows = [...(workbench?.candidates ?? [])].map((item) => withFinalOpportunityScore(item, ctx.newsSnapshot) as WorkbenchCandidate);
    return rows.sort((a, b) => Number(opportunityScoreValue(b)) - Number(opportunityScoreValue(a))).slice(0, 5);
  }, [workbench, ctx.newsSnapshot]);

  const riskEvents = useMemo(() => events.filter((item) => item.importance_level >= 4 || item.sentiment === "negative").slice(0, 5), [events]);
  const investedPct = workbench?.account_summary?.invested_pct ?? workbench?.overview?.total_position_pct ?? 0;
  const positionStatus = investedPct >= 75 ? labels.high : labels.safe;
  const actions = useMemo(() => {
    return [...(workbench?.candidates ?? [])]
      .map((item) => withFinalOpportunityScore(item, ctx.newsSnapshot) as WorkbenchCandidate)
      .filter((item) => TODO_ACTIONS.has(String(item.action)))
      .sort((a, b) => Number(opportunityScoreValue(b)) - Number(opportunityScoreValue(a)))
      .slice(0, 5);
  }, [workbench, ctx.newsSnapshot]);

  const repairSamples = useMemo(() => {
    const missing = (health?.bars.missing_samples ?? []).map((item) => ({ ...item, repairKind: labels.missingBars, tagColor: "red" }));
    const stale = (health?.bars.stale_samples ?? []).map((item) => ({ ...item, repairKind: labels.outdatedBars, tagColor: "orange" }));
    return [...missing, ...stale].slice(0, 6);
  }, [health, labels.missingBars, labels.outdatedBars]);

  const factorCoverage = useMemo(() => {
    const rows = factorOverview?.factor_coverage ?? [];
    if (!rows.length) return null;
    return rows.reduce((sum, item) => sum + Number(item.coverage || 0), 0) / rows.length;
  }, [factorOverview]);

  const repairSymbol = async (item: DataHealthBarIssue) => {
    setRepairingSymbolId(item.symbol_id);
    try {
      await api.repairSymbolMarketData(item.symbol_id, { auto_score: true });
      ctx.showToast("success", `${labels.repairOk}: ${item.symbol}`);
      await load();
    } catch (err: any) {
      ctx.showToast("error", `${labels.repairFailed}: ${err.message || err}`);
    } finally {
      setRepairingSymbolId(null);
    }
  };

  const conclusion = macroScore === null
    ? labels.noData
    : (ctx.locale === "en-US"
      ? "Today is " + marketLabel(macroScore, labels).toLowerCase() + ": focus on top opportunities, watch major news and keep position discipline."
      : "\u4eca\u5929\u5e02\u573a\u73af\u5883" + marketLabel(macroScore, labels) + "\uff1a\u5148\u770b Top \u673a\u4f1a\uff0c\u540c\u65f6\u76ef\u4f4f\u91cd\u5927\u6d88\u606f\u548c\u4ed3\u4f4d\u7eaa\u5f8b\u3002");

  const openSymbol = async (item: WorkbenchCandidate) => {
    setOpeningSymbolId(item.symbol_id);
    try {
      await ctx.loadSymbolDetail(item.symbol_id);
      ctx.setActiveTab("investment");
    } catch (err: any) {
      ctx.showToast("error", err.message || labels.loadDetailFailed);
    } finally {
      setOpeningSymbolId(null);
    }
  };

  const explainRows = selectedExplain ? [
    [labels.finalScore, score(opportunityScoreValue(selectedExplain), 1)],
    [labels.baseScore, score(baseOpportunityScoreValue(selectedExplain), 1)],
    [labels.newsMultiplier, `${score(selectedExplain.news_multiplier ?? 1, 3)}x`],
    [labels.messageScore, score(selectedExplain.news_message_score, 1)],
    [labels.confidence, `${confidenceText(selectedExplain.news_confidence, ctx.locale)} (${score(Number(selectedExplain.news_confidence ?? 0) * 100, 0)}%)`],
    [labels.quality, score(selectedExplain.quality_score, 1)],
    [labels.timing, score(selectedExplain.timing_score, 1)],
    [labels.trend, score(selectedExplain.trend_score, 1)],
    [labels.momentum, score(selectedExplain.momentum_score, 1)],
    [labels.volatility, score(selectedExplain.volatility_score, 1)],
    [labels.liquidity, score(selectedExplain.liquidity_score, 1)],
    [labels.breadth, score(selectedExplain.breadth_score, 1)],
    [labels.event, score(selectedExplain.event_score, 1)],
    [labels.plan, `${stageLabel(selectedExplain.stage)} / ${actionLabel(selectedExplain.action)}`],
    [labels.reasons, reasonText(selectedExplain, labels)],
    [labels.freshness, selectedExplain.created_at ? formatRelativeTime(selectedExplain.created_at) : "-"],
  ] : [];

  const openFactorSettings = () => {
    window.localStorage.setItem("settings_active_section", "factor-model");
    ctx.setActiveTab("settings");
  };

  return (
    <div className="decision-page">
      <section className="decision-hero">
        <div>
          <p className="panel-kicker">{labels.title}</p>
          <h1>{conclusion}</h1>
          <p>{labels.subtitle}</p>
        </div>
        <Button icon={<ReloadOutlined />} loading={loading} onClick={load}>{labels.refresh}</Button>
      </section>

      {error && <Alert type="error" showIcon message={error} />}

      <Row gutter={[12, 12]}>
        <Col xs={24} sm={12} lg={6}><Card className="decision-metric"><BarChartOutlined /><span>{labels.market}</span><strong style={{ color: scoreColor(macroScore) }}>{marketLabel(macroScore, labels)}</strong><Progress percent={macroScore ? Math.round(macroScore) : 0} strokeColor={scoreColor(macroScore)} showInfo={false} /></Card></Col>
        <Col xs={24} sm={12} lg={6}><Card className="decision-metric"><FireOutlined /><span>{labels.opportunities}</span><strong>{candidates.length}</strong><Button type="link" onClick={() => ctx.setActiveTab("discovery")}>{labels.goDiscovery}</Button></Card></Col>
        <Col xs={24} sm={12} lg={6}><Card className="decision-metric"><AlertOutlined /><span>{labels.risks}</span><strong>{riskEvents.length}</strong><Button type="link" onClick={() => ctx.setActiveTab("news")}>{labels.goNews}</Button></Card></Col>
        <Col xs={24} sm={12} lg={6}><Card className="decision-metric"><SafetyOutlined /><span>{labels.position}</span><strong>{positionStatus}</strong><Text type="secondary">{pct(investedPct)}</Text></Card></Col>
      </Row>

      <Card className="decision-health-card" title={<Space><DatabaseOutlined />{labels.healthTitle}</Space>} extra={<Tag color={health?.status === "ok" ? "green" : health?.status === "error" ? "red" : "orange"}>{healthLabel(health, labels)}</Tag>}>
        <div className="decision-health-grid">
          <div className="decision-health-score">
            <strong style={{ color: healthColor(health) }}>{health?.score ?? "-"}</strong>
            <Progress percent={health?.score ?? 0} strokeColor={healthColor(health)} showInfo={false} />
          </div>
          <span><b>{labels.symbols}</b>{health?.symbols.total ?? "-"}</span>
          <span><b>{labels.barCoverage}</b>{pct(health?.bars.coverage_pct)}</span>
          <span><b>{labels.missingBars}</b>{health?.bars.missing_symbols ?? "-"}</span>
          <span><b>{labels.outdatedBars}</b>{health?.bars.outdated_symbols ?? "-"}</span>
          <span><b>{labels.macroFreshness}</b>{ageText(health?.macro.latest_age_days, ctx.locale)}</span>
          <span><b>{labels.news7d}</b>{health?.market_events.events_7d ?? "-"}</span>
          <span><b>{labels.discoveryFreshness}</b>{labels.expired}: {health?.discovery.expired_results ?? 0} / {labels.frozen}: {health?.discovery.frozen_results ?? 0}</span>
        </div>
        <div className="decision-health-issues">
          {(health?.issues?.length ? health.issues : [{ level: "ok", message: labels.noIssue }]).map((issue, index) => (
            <Tag key={index} color={issue.level === "error" ? "red" : issue.level === "warn" ? "orange" : "green"}>{issue.message}</Tag>
          ))}
        </div>
        {repairSamples.length > 0 && (
          <div className="decision-health-repair">
            <div className="decision-health-repair-head">
              <Text type="secondary">{labels.repairSamples}</Text>
              <Text type="secondary">{health?.bars.repair_hint}</Text>
            </div>
            <div className="decision-health-repair-list">
              {repairSamples.map((item) => (
                <div key={`${item.reason}-${item.symbol_id}`} className="decision-health-repair-row">
                  <div>
                    <Tag color={item.tagColor}>{item.repairKind}</Tag>
                    <strong>{item.symbol}</strong>
                    <span>{item.name}</span>
                    <Text type="secondary">
                      {item.latest_trade_date ? `${labels.latestBar}: ${item.latest_trade_date} / ${ageText(item.latest_age_days, ctx.locale)}` : labels.missingReason}
                    </Text>
                  </div>
                  <Button size="small" icon={<ReloadOutlined />} loading={repairingSymbolId === item.symbol_id} onClick={() => repairSymbol(item)}>{labels.repair}</Button>
                </div>
              ))}
            </div>
          </div>
        )}
      </Card>

      <Card
        className="decision-factor-card"
        title={<Space><ExperimentOutlined />{labels.factorTitle}</Space>}
        extra={
          <Space>
            <Tag color={factorOverview?.health.warehouse_available ? (factorOverview.health.status === "healthy" ? "green" : "orange") : "red"}>
              {factorOverview?.health.warehouse_available ? factorOverview.health.status : labels.factorUnavailable}
            </Tag>
            <Button type="link" onClick={openFactorSettings}>{labels.factorManage}</Button>
          </Space>
        }
      >
        <div className="decision-factor-grid">
          <span><b>{labels.factorMode}</b><Tag color={factorOverview?.runtime.weight_mode === "ridge" ? "green" : factorOverview?.runtime.weight_mode === "shadow" ? "blue" : "default"}>{factorOverview?.runtime.weight_mode ?? "manual"}</Tag></span>
          <span><b>{labels.factorScoreSource}</b><strong>{factorOverview?.runtime.score_weight_mode ?? "manual"}</strong></span>
          <span title={factorOverview?.runtime.active_model_run_id ?? undefined}><b>{labels.factorModel}</b><strong>{factorOverview?.runtime.active_model_run_id ? factorOverview.runtime.active_model_run_id.slice(0, 16) : "-"}</strong></span>
          <span><b>{labels.factorLatest}</b><strong>{factorOverview?.latest_trade_date ?? "-"}</strong></span>
          <span><b>{labels.factorCoverage}</b><strong>{factorCoverage == null ? "-" : `${(factorCoverage * 100).toFixed(1)}%`}</strong></span>
        </div>
      </Card>

      <Row gutter={[12, 12]}>
        <Col xs={24} lg={14}>
          <Card title={labels.topOpportunities} extra={<Button type="link" onClick={() => ctx.setActiveTab("discovery")}>{labels.goDiscovery}</Button>}>
            {candidates.length === 0 ? <Empty description={labels.empty} /> : <div className="decision-list">{candidates.map((item, index) => <div key={item.symbol_id} className="decision-row decision-row-split"><button type="button" disabled={openingSymbolId === item.symbol_id} onClick={() => openSymbol(item)}><span className="decision-rank">{index + 1}</span><strong>{item.symbol}</strong><span>{item.name}</span><Tag>{stageLabel(item.stage)}</Tag><b>{scoreValue(Number(opportunityScoreValue(item)))}</b>{openingSymbolId === item.symbol_id ? <LoadingOutlined spin /> : <ArrowRightOutlined />}</button><Button size="small" icon={<InfoCircleOutlined />} onClick={() => setSelectedExplain(item)}>{labels.explain}</Button></div>)}</div>}
          </Card>
        </Col>
        <Col xs={24} lg={10}>
          <Card title={labels.topNews} extra={<Button type="link" onClick={() => ctx.setActiveTab("news")}>{labels.goNews}</Button>}>
            {events.length === 0 ? <Empty description={labels.empty} /> : <div className="decision-news">{events.slice(0, 5).map((event) => <button key={event.id} className="decision-news-row" onClick={() => ctx.setActiveTab("news")}><Tag color={eventTone(event)}>{event.importance_level}</Tag><span>{event.title}</span></button>)}</div>}
          </Card>
        </Col>
      </Row>

      <Card title={labels.actions} extra={<Button type="link" onClick={() => ctx.setActiveTab("investment")}>{labels.goInvestment}</Button>}>
        {actions.length === 0 ? <Empty description={labels.empty} /> : <div className="decision-actions">{actions.map((item) => <button key={item.symbol_id} disabled={openingSymbolId === item.symbol_id} onClick={() => openSymbol(item)}>{openingSymbolId === item.symbol_id ? <LoadingOutlined spin /> : <CheckCircleOutlined />}<span>{item.symbol} {item.name}</span><Tag>{actionLabel(item.action)}</Tag><Text type="secondary">{item.reason_tags?.slice(0, 2).join(" / ")}</Text></button>)}</div>}
      </Card>

      <Modal
        open={!!selectedExplain}
        title={selectedExplain ? `${labels.explainTitle}: ${selectedExplain.symbol} ${selectedExplain.name}` : labels.explainTitle}
        onCancel={() => setSelectedExplain(null)}
        footer={<Button onClick={() => setSelectedExplain(null)}>{labels.close}</Button>}
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
    </div>
  );
}
