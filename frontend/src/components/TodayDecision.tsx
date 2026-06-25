import { useEffect, useMemo, useState } from "react";
import { Alert, Button, Card, Col, Empty, Progress, Row, Tag, Typography } from "antd";
import { AlertOutlined, ArrowRightOutlined, BarChartOutlined, CheckCircleOutlined, FireOutlined, ReloadOutlined, SafetyOutlined } from "@ant-design/icons";
import { api } from "../api/client";
import { actionLabel, stageLabel } from "../i18n";
import { useApp } from "../context/AppContext";
import type { MacroOverview, MarketEvent, WorkbenchCandidate } from "../types";

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
    goDiscovery: "\u53bb\u673a\u4f1a\u6316\u6398",
    goMacro: "\u770b\u5b8f\u89c2",
    goNews: "\u770b\u6d88\u606f",
    goInvestment: "\u770b\u8ba1\u5212",
    active: "\u504f\u79ef\u6781",
    neutral: "\u4e2d\u6027",
    cautious: "\u504f\u8c28\u614e",
    defensive: "\u9632\u5b88",
    safe: "\u5b89\u5168",
    high: "\u504f\u9ad8",
    empty: "\u6682\u65e0",
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
    goDiscovery: "Open Mining",
    goMacro: "Open Macro",
    goNews: "Open News",
    goInvestment: "Open Plan",
    active: "Active",
    neutral: "Neutral",
    cautious: "Cautious",
    defensive: "Defensive",
    safe: "Safe",
    high: "High",
    empty: "Empty",
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

function marketLabel(score: number | null | undefined, labels: DecisionLabels) {
  if (score === null || score === undefined) return labels.empty;
  if (score >= 68) return labels.active;
  if (score <= 42) return labels.defensive;
  if (score <= 52) return labels.cautious;
  return labels.neutral;
}

function scoreColor(score: number | null | undefined) {
  if (score === null || score === undefined) return "#64748b";
  if (score >= 68) return "#0f766e";
  if (score <= 42) return "#b42318";
  if (score <= 52) return "#d97706";
  return "#2563eb";
}

function eventTone(event: MarketEvent) {
  if (event.importance_level >= 5) return "red";
  if (event.sentiment === "negative") return "volcano";
  if (event.sentiment === "positive") return "green";
  if (event.importance_level >= 4) return "orange";
  return "blue";
}

export default function TodayDecision() {
  const ctx = useApp();
  const labels = LABELS[ctx.locale as "zh-CN" | "en-US"] ?? LABELS["zh-CN"];
  const workbench = ctx.workbench;
  const [macro, setMacro] = useState<MacroOverview | null>(null);
  const [events, setEvents] = useState<MarketEvent[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = async () => {
    setLoading(true);
    setError(null);
    try {
      const [macroData, newsData] = await Promise.all([
        api.getMacroOverview("all"),
        api.getMarketEvents({ importance_level_min: 3, limit: 8, sort_by: "importance_level" }),
      ]);
      setMacro(macroData);
      setEvents(newsData.events ?? []);
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
    const rows = [...(workbench?.candidates ?? [])];
    return rows.sort((a, b) => Number(b.final_opportunity_score ?? b.priority_score ?? 0) - Number(a.final_opportunity_score ?? a.priority_score ?? 0)).slice(0, 5);
  }, [workbench]);

  const riskEvents = useMemo(() => events.filter((item) => item.importance_level >= 4 || item.sentiment === "negative").slice(0, 5), [events]);
  const investedPct = workbench?.account_summary?.invested_pct ?? workbench?.overview?.total_position_pct ?? 0;
  const positionStatus = investedPct >= 75 ? labels.high : labels.safe;
  const actions = useMemo(() => {
    return [...(workbench?.candidates ?? [])]
      .filter((item) => TODO_ACTIONS.has(String(item.action)))
      .sort((a, b) => Number(b.final_opportunity_score ?? b.priority_score ?? 0) - Number(a.final_opportunity_score ?? a.priority_score ?? 0))
      .slice(0, 5);
  }, [workbench]);

  const conclusion = macroScore === null
    ? labels.noData
    : (ctx.locale === "en-US"
      ? "Today is " + marketLabel(macroScore, labels).toLowerCase() + ": focus on top opportunities, watch major news and keep position discipline."
      : "\u4eca\u5929\u5e02\u573a\u73af\u5883" + marketLabel(macroScore, labels) + "\uff1a\u5148\u770b Top \u673a\u4f1a\uff0c\u540c\u65f6\u76ef\u4f4f\u91cd\u5927\u6d88\u606f\u548c\u4ed3\u4f4d\u7eaa\u5f8b\u3002");

  const openSymbol = async (item: WorkbenchCandidate) => {
    await ctx.loadSymbolDetail(item.symbol_id, { focus: true });
    ctx.setActiveTab("investment");
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

      <Row gutter={[12, 12]}>
        <Col xs={24} lg={14}>
          <Card title={labels.topOpportunities} extra={<Button type="link" onClick={() => ctx.setActiveTab("discovery")}>{labels.goDiscovery}</Button>}>
            {candidates.length === 0 ? <Empty description={labels.empty} /> : <div className="decision-list">{candidates.map((item, index) => <button key={item.symbol_id} className="decision-row" onClick={() => openSymbol(item)}><span className="decision-rank">{index + 1}</span><strong>{item.symbol}</strong><span>{item.name}</span><Tag>{stageLabel(item.stage)}</Tag><b>{scoreValue(Number(item.final_opportunity_score ?? item.priority_score ?? 0))}</b><ArrowRightOutlined /></button>)}</div>}
          </Card>
        </Col>
        <Col xs={24} lg={10}>
          <Card title={labels.topNews} extra={<Button type="link" onClick={() => ctx.setActiveTab("news")}>{labels.goNews}</Button>}>
            {events.length === 0 ? <Empty description={labels.empty} /> : <div className="decision-news">{events.slice(0, 5).map((event) => <button key={event.id} className="decision-news-row" onClick={() => ctx.setActiveTab("news")}><Tag color={eventTone(event)}>{event.importance_level}</Tag><span>{event.title}</span></button>)}</div>}
          </Card>
        </Col>
      </Row>

      <Card title={labels.actions} extra={<Button type="link" onClick={() => ctx.setActiveTab("investment")}>{labels.goInvestment}</Button>}>
        {actions.length === 0 ? <Empty description={labels.empty} /> : <div className="decision-actions">{actions.map((item) => <button key={item.symbol_id} onClick={() => openSymbol(item)}><CheckCircleOutlined /><span>{item.symbol} {item.name}</span><Tag>{actionLabel(item.action)}</Tag><Text type="secondary">{item.reason_tags?.slice(0, 2).join(" / ")}</Text></button>)}</div>}
      </Card>
    </div>
  );
}
