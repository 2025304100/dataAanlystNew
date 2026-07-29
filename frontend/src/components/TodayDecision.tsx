import { useEffect, useMemo, useState } from "react";
import { Alert, Button, Card, Col, Empty, Modal, Progress, Row, Space, Tag, Typography } from "antd";
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
  PlayCircleOutlined,
  ReloadOutlined,
  SafetyOutlined,
} from "@ant-design/icons";
import { api, type FactorOverview } from "../api/client";
import { actionLabel, enumLabel, stageLabel, t, template } from "../i18n";
import { useApp } from "../context/AppContext";
import { navigateToResearch } from "../utils/sourceContext";
import { baseOpportunityScoreValue, formatRelativeTime, opportunityScoreValue, score, withFinalOpportunityScore } from "../utils/format";
import type { DataHealth, DataHealthBarIssue, MacroOverview, MarketEvent, WorkbenchCandidate } from "../types";
// WP1-FIX.1：今日决策接入 OpportunityStatusBadges（compact 模式）
import { OpportunityStatusBadges } from "./opportunity/OpportunityStatusBadges";

const { Text } = Typography;

const TODO_ACTIONS = new Set(["open", "buy_dip", "hold"]);

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

export default function TodayDecision() {
  const ctx = useApp();
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
  const [repairAllLoading, setRepairAllLoading] = useState(false);
  const [repairModalOpen, setRepairModalOpen] = useState(false);
  const [discoveryCleanupLoading, setDiscoveryCleanupLoading] = useState(false);
  const [discoveryRescanLoading, setDiscoveryRescanLoading] = useState(false);

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
      setError(err.message || t("tdLoadFailed"));
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
          <Card title={t("tdTopOpportunities")} extra={<Button type="link" onClick={() => ctx.setActiveTab("discovery")}>{t("tdGoDiscovery")}</Button>}>
            {candidates.length === 0 ? <Empty description={t("tdEmpty")} /> : <div className="decision-list">{candidates.map((item, index) => <div key={item.symbol_id} className="decision-row decision-row-split"><button type="button" disabled={openingSymbolId === item.symbol_id} onClick={() => openSymbol(item)}><span className="decision-rank">{index + 1}</span><strong>{item.symbol}</strong><span>{item.name}</span><Tag>{stageLabel(item.stage)}</Tag><b>{scoreValue(Number(opportunityScoreValue(item)))}</b>{openingSymbolId === item.symbol_id ? <LoadingOutlined spin /> : <ArrowRightOutlined />}</button><Button size="small" icon={<InfoCircleOutlined />} onClick={() => setSelectedExplain(item)}>{t("tdExplain")}</Button><OpportunityStatusBadges symbolId={item.symbol_id} compact onOpenDetail={openSymbolDetail} /></div>)}</div>}
          </Card>
        </Col>
        <Col xs={24} lg={10}>
          <Card title={t("tdTopNews")} extra={<Button type="link" onClick={() => ctx.setActiveTab("news")}>{t("tdGoNews")}</Button>}>
            {events.length === 0 ? <Empty description={t("tdEmpty")} /> : <div className="decision-news">{events.slice(0, 5).map((event) => <button key={event.id} className="decision-news-row" onClick={() => ctx.setActiveTab("news")}><Tag color={eventTone(event)}>{event.importance_level}</Tag><span>{event.title}</span></button>)}</div>}
          </Card>
        </Col>
      </Row>

      <Card title={t("tdActions")} extra={<Button type="link" onClick={() => ctx.setActiveTab("investment")}>{t("tdGoInvestment")}</Button>}>
        {actions.length === 0 ? <Empty description={t("tdEmpty")} /> : <div className="decision-actions">{actions.map((item) => <button key={item.symbol_id} disabled={openingSymbolId === item.symbol_id} onClick={() => openSymbol(item)}>{openingSymbolId === item.symbol_id ? <LoadingOutlined spin /> : <CheckCircleOutlined />}<span>{item.symbol} {item.name}</span><Tag>{actionLabel(item.action)}</Tag><Text type="secondary">{item.reason_tags?.slice(0, 2).join(" / ")}</Text><OpportunityStatusBadges symbolId={item.symbol_id} compact onOpenDetail={openSymbolDetail} /></button>)}</div>}
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
