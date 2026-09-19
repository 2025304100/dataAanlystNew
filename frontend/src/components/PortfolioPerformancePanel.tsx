import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { DatePicker, Button, Empty, Skeleton, Tooltip, Tabs, Alert, Table, Input } from "antd";
import { QuestionCircleOutlined } from "@ant-design/icons";
import dayjs, { Dayjs } from "dayjs";
import ReactECharts from "echarts-for-react";
import { useApp } from "../context/AppContext";
import { api } from "../api/client";
import { t, template } from "../i18n";
import { money, percent, pnlClass } from "../utils/format";
import type { AttributionReport, AttributionDimension, Review } from "../types";

const { RangePicker } = DatePicker;

interface EquityPoint {
  date: string;
  equity: number;
}

interface PerformanceStats {
  total_return: number;
  total_return_pct: number;
  max_drawdown: number;
  max_drawdown_pct: number;
  sharpe_ratio: number;
  win_rate: number;
  profit_factor: number;
  trade_count: number;
  avg_holding_days: number;
}

interface PerformanceResult {
  portfolio_id: number;
  initial_capital: number;
  snapshot_count: number;
  date_range: { start: string | null; end: string | null };
  equity_curve: EquityPoint[];
  // P3+ 预留：后端 benchmark 基础设施就绪后返回；当前后端未返回，前端自动降级为单 series
  benchmark_curve?: EquityPoint[];
  benchmark_name?: string;
  stats: PerformanceStats;
}

/**
 * P1-2：组合绩效面板（净值曲线 + 绩效指标卡片）。
 *
 * 数据来源：GET /portfolios/{id}/performance
 * - equity_curve 来自 PortfolioEquitySnapshot 时序
 * - stats 来自 metrics 共享模块（与 backtest 算法一致）
 *
 * 交互：
 * - 日期范围筛选（默认最近 90 天）
 * - 手动刷新（组合切换或下单后调用）
 * - 空状态引导（无 snapshot 时提示等待定时任务）
 *
 * 注意：历史数据无法回溯，仅有系统上线后写入的 snapshot。
 */
export default function PortfolioPerformancePanel() {
  const ctx = useApp();
  const portfolioId = ctx.portfolioId;

  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<PerformanceResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  // 默认最近 90 天
  const [dateRange, setDateRange] = useState<[Dayjs, Dayjs]>([
    dayjs().subtract(89, "day"),
    dayjs(),
  ]);

  // WP8.3：归因报告状态
  const [attribution, setAttribution] = useState<AttributionReport | null>(null);
  const [attrLoading, setAttrLoading] = useState(false);
  const [attrError, setAttrError] = useState<string | null>(null);
  const [activeAttrTab, setActiveAttrTab] = useState("by_member");

  // WP8.3：复盘记录状态
  const [reviews, setReviews] = useState<Review[]>([]);
  const [reviewsLoading, setReviewsLoading] = useState(false);
  const [reviewFormOpen, setReviewFormOpen] = useState(false);
  const [reviewNote, setReviewNote] = useState("");
  const [reviewSubmitting, setReviewSubmitting] = useState(false);

  const loadPerformance = useCallback(async () => {
    if (!portfolioId) return;
    setLoading(true);
    setError(null);
    try {
      const params = {
        startDate: dateRange[0].format("YYYY-MM-DD"),
        endDate: dateRange[1].format("YYYY-MM-DD"),
        snapshotLimit: 1000,
      };
      const data = await api.getPortfolioPerformance(portfolioId, params);
      setResult(data as PerformanceResult);
    } catch (err: any) {
      setError(err?.message || String(err));
      setResult(null);
    } finally {
      setLoading(false);
    }
  }, [portfolioId, dateRange]);

  // WP8.3：加载归因报告
  const loadAttribution = useCallback(async () => {
    if (!portfolioId) return;
    setAttrLoading(true);
    setAttrError(null);
    try {
      const data = await api.getAttributionReport(
        portfolioId,
        dateRange[0].format("YYYY-MM-DD"),
        dateRange[1].format("YYYY-MM-DD"),
      );
      setAttribution(data as AttributionReport);
    } catch (err: any) {
      setAttrError(err?.message || String(err));
      setAttribution(null);
    } finally {
      setAttrLoading(false);
    }
  }, [portfolioId, dateRange]);

  // WP8.3：加载复盘记录列表
  const loadReviews = useCallback(async () => {
    if (!portfolioId) return;
    setReviewsLoading(true);
    try {
      const data = await api.getReviews(portfolioId);
      setReviews(data as Review[]);
    } catch (err) {
      // 复盘历史加载失败不阻断主流程，仅静默清空
      setReviews([]);
    } finally {
      setReviewsLoading(false);
    }
  }, [portfolioId]);

  // WP8.3：创建复盘记录（自动附归因快照）
  const handleCreateReview = useCallback(async () => {
    if (!portfolioId || !reviewNote.trim()) return;
    setReviewSubmitting(true);
    try {
      const snapshot = attribution ? JSON.stringify(attribution) : undefined;
      await api.createReview(portfolioId, { note: reviewNote.trim(), attribution_snapshot: snapshot });
      setReviewNote("");
      setReviewFormOpen(false);
      await loadReviews();
    } catch (err: any) {
      // 创建失败提示通过 error 文案展示
      setAttrError(err?.message || t("portfolioAttribution.reviewCreateFailed"));
    } finally {
      setReviewSubmitting(false);
    }
  }, [portfolioId, reviewNote, attribution, loadReviews]);

  // 刷新全部（绩效 + 归因 + 复盘）
  const refreshAll = useCallback(() => {
    loadPerformance();
    loadAttribution();
    loadReviews();
  }, [loadPerformance, loadAttribution, loadReviews]);

  // 组合切换或日期变更时重新加载
  useEffect(() => {
    if (portfolioId) {
      loadPerformance();
      loadAttribution();
      loadReviews();
    } else {
      setResult(null);
      setAttribution(null);
      setReviews([]);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [portfolioId]);

  const chartOption = useMemo(() => {
    if (!result || result.equity_curve.length === 0) return null;
    const dates = result.equity_curve.map((p) => p.date);
    const equities = result.equity_curve.map((p) => p.equity);

    // P3+ 预留：benchmark 数据对齐（后端未返回时降级为单 series）
    const benchmarkCurve = result.benchmark_curve;
    const benchmarkName = result.benchmark_name || t("perfBenchmark");
    const benchmarkMap = new Map<string, number>();
    benchmarkCurve?.forEach((p) => benchmarkMap.set(p.date, p.equity));
    const hasBenchmark = !!benchmarkCurve && benchmarkCurve.length > 0;
    const benchmarkData = hasBenchmark
      ? dates.map((d) => {
          const v = benchmarkMap.get(d);
          return v == null ? null : v;
        })
      : [];

    const series: any[] = [
      {
        name: t("perfEquity"),
        type: "line",
        data: equities,
        smooth: false,
        symbol: "none",
        lineStyle: { width: 2, color: "#0d9488" },
        areaStyle: {
          color: {
            type: "linear",
            x: 0, y: 0, x2: 0, y2: 1,
            colorStops: [
              { offset: 0, color: "rgba(13, 148, 136, 0.25)" },
              { offset: 1, color: "rgba(13, 148, 136, 0.02)" },
            ],
          },
        },
      },
    ];
    if (hasBenchmark) {
      series.push({
        name: benchmarkName,
        type: "line",
        data: benchmarkData,
        smooth: false,
        symbol: "none",
        lineStyle: { width: 1.5, color: "#94a3b8", type: "dashed" },
      });
    }

    return {
      tooltip: {
        trigger: "axis",
        formatter: (params: any) => {
          if (!params || params.length === 0) return "";
          const lines = [params[0].axisValue];
          params.forEach((p: any) => {
            if (p.value == null) return;
            lines.push(`${p.marker} ${p.seriesName}: ${money(p.value)}`);
          });
          return lines.join("<br/>");
        },
      },
      legend: hasBenchmark
        ? {
            data: [t("perfEquity"), benchmarkName],
            top: 0,
            textStyle: { fontSize: 11 },
          }
        : undefined,
      grid: { left: 60, right: 20, top: hasBenchmark ? 30 : 20, bottom: 30 },
      xAxis: {
        type: "category",
        data: dates,
        axisLabel: { fontSize: 11 },
      },
      yAxis: {
        type: "value",
        axisLabel: {
          fontSize: 11,
          formatter: (val: number) => {
            if (Math.abs(val) >= 10000) return (val / 10000).toFixed(1) + "万";
            return val.toFixed(0);
          },
        },
        scale: true,
      },
      series,
    };
  }, [result]);

  const stats = result?.stats;
  const hasData = result && result.snapshot_count > 0;

  // WP8.3：归因维度表格列定义
  const attrColumns = useMemo(
    () => [
      { title: t("portfolioAttribution.label"), dataIndex: "label", key: "label" },
      {
        title: t("portfolioAttribution.contribution"),
        dataIndex: "contribution_pct",
        key: "contribution_pct",
        render: (v: number | null) => (v == null ? "-" : percent(v)),
      },
      {
        title: t("portfolioAttribution.pnl"),
        dataIndex: "pnl",
        key: "pnl",
        render: (v: number | null) => <span className={pnlClass(v)}>{money(v)}</span>,
      },
      {
        title: t("portfolioAttribution.tradeCount"),
        dataIndex: "trade_count",
        key: "trade_count",
        render: (v: number | null) => (v == null ? "-" : v),
      },
    ],
    [],
  );

  // 计算维度的样本数提示文案（后端未返回 sample_size 时用 items 推断）
  const sampleSizeText = (dim: AttributionDimension | undefined): string => {
    if (!dim) return "";
    const groups = dim.group_count ?? dim.items.length;
    const trades = dim.sample_size ?? dim.items.reduce((s, it) => s + (it.trade_count ?? 0), 0);
    return template("portfolioAttribution.basedOn", { trades, groups });
  };

  // 任一维度存在样本不足告警时，隐藏可能具有误导性的稳定结论（summary）
  const hasAnySampleWarning = !!(
    attribution &&
    (attribution.by_member?.sample_warning ||
      attribution.by_execution_mode?.sample_warning ||
      attribution.by_source?.sample_warning ||
      attribution.by_rule_signal?.sample_warning)
  );

  // 回测 vs 模拟 偏差行
  const backtestDiffRows = useMemo(() => {
    const diff = attribution?.backtest_vs_sim?.diff;
    if (!diff) return [];
    const labelMap: Record<string, string> = {
      total_return_pct: t("perfTotalReturn"),
      max_drawdown_pct: t("perfMaxDrawdown"),
      sharpe_ratio: t("perfSharpe"),
      win_rate: t("perfWinRate"),
      trade_count: t("perfTradeCount"),
    };
    return Object.keys(diff)
      .filter((k) => labelMap[k] && diff[k] != null)
      .map((k) => ({ key: k, metric: labelMap[k], value: diff[k] as number }));
  }, [attribution]);

  return (
    <section className="band perf-band">
      <div className="panel">
        <div className="panel-head">
          <div>
            <p className="panel-kicker">{t("perfKicker")}</p>
            <h2>
              {t("perfTitle")}
              <Tooltip title={t("perfHelp")}>
                <QuestionCircleOutlined style={{ marginLeft: 8, fontSize: 14, color: "#94a3b8" }} />
              </Tooltip>
            </h2>
          </div>
          <div className="panel-meta" style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <RangePicker
              size="small"
              value={dateRange}
              onChange={(range) => {
                if (range && range[0] && range[1]) {
                  setDateRange([range[0], range[1]]);
                }
              }}
              allowClear={false}
            />
            <Button size="small" onClick={refreshAll} loading={loading}>
              {t("refresh")}
            </Button>
          </div>
        </div>

        {loading && <Skeleton active paragraph={{ rows: 4 }} />}

        {!loading && error && (
          <div className="empty" style={{ color: "#dc2626" }}>
            {t("perfLoadFailed")}: {error}
          </div>
        )}

        {!loading && !error && !hasData && (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={
              <div>
                <p>{t("perfNoSnapshot")}</p>
                <p style={{ fontSize: 12, color: "#94a3b8" }}>{t("perfNoSnapshotHint")}</p>
              </div>
            }
          />
        )}

        {!loading && !error && hasData && stats && (
          <>
            {/* 绩效指标卡片 */}
            <div className="account-grid" style={{ marginBottom: 16 }}>
              <div className="account-item">
                <span className="metric-label">{t("perfTotalReturn")}</span>
                <span className={`metric-value ${pnlClass(stats.total_return)}`}>
                  {money(stats.total_return)}
                  <span className={`pnl-pct ${pnlClass(stats.total_return_pct)}`} style={{ marginLeft: 6, fontSize: 12 }}>
                    ({percent(stats.total_return_pct)})
                  </span>
                </span>
              </div>
              <div className="account-item">
                <span className="metric-label">{t("perfMaxDrawdown")}</span>
                <span className="metric-value" style={{ color: "#dc2626" }}>
                  {money(stats.max_drawdown)}
                  <span className="pnl-pct" style={{ marginLeft: 6, fontSize: 12, color: "#dc2626" }}>
                    ({percent(stats.max_drawdown_pct)})
                  </span>
                </span>
              </div>
              <div className="account-item">
                <span className="metric-label">
                  {t("perfSharpe")}
                  <Tooltip title={t("perfSharpeHelp")}>
                    <QuestionCircleOutlined style={{ marginLeft: 4, fontSize: 11, color: "#94a3b8" }} />
                  </Tooltip>
                </span>
                <span className="metric-value">{stats.sharpe_ratio.toFixed(2)}</span>
              </div>
              <div className="account-item">
                <span className="metric-label">{t("perfWinRate")}</span>
                <span className="metric-value">{percent(stats.win_rate)}</span>
              </div>
              <div className="account-item">
                <span className="metric-label">{t("perfProfitFactor")}</span>
                <span className="metric-value">{stats.profit_factor.toFixed(2)}</span>
              </div>
              <div className="account-item">
                <span className="metric-label">{t("perfTradeCount")}</span>
                <span className="metric-value">{stats.trade_count}</span>
              </div>
              <div className="account-item">
                <span className="metric-label">{t("perfAvgHoldDays")}</span>
                <span className="metric-value">{stats.avg_holding_days.toFixed(1)}</span>
              </div>
              <div className="account-item">
                <span className="metric-label">{t("perfSnapshotCount")}</span>
                <span className="metric-value">{result.snapshot_count}</span>
              </div>
            </div>

            {/* 净值曲线 */}
            {chartOption ? (
              <ReactECharts option={chartOption} style={{ height: 320, width: "100%" }} />
            ) : (
              <div className="empty">{t("perfNoEquityCurve")}</div>
            )}

            <p className="panel-meta" style={{ marginTop: 8, fontSize: 11 }}>
              {t("perfDateRange")}: {result.date_range.start} ~ {result.date_range.end}
              <span style={{ marginLeft: 12 }}>{t("perfInitialCapital")}: {money(result.initial_capital)}</span>
            </p>
          </>
        )}
      </div>

      {/* WP8.3：绩效归因分区 */}
      {portfolioId && (
        <div className="panel" data-testid="attribution-panel">
          <div className="panel-head">
            <div>
              <p className="panel-kicker">{t("perfKicker")}</p>
              <h2>
                {t("portfolioAttribution.title")}
                <Tooltip title={t("portfolioAttribution.help")}>
                  <QuestionCircleOutlined style={{ marginLeft: 8, fontSize: 14, color: "#94a3b8" }} />
                </Tooltip>
              </h2>
            </div>
          </div>

          {attrLoading && <Skeleton active paragraph={{ rows: 4 }} />}

          {!attrLoading && attrError && (
            <div className="empty" style={{ color: "#dc2626" }}>
              {t("portfolioAttribution.loadFailed")}: {attrError}
            </div>
          )}

          {!attrLoading && !attrError && !attribution && (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={t("portfolioAttribution.noData")}
            />
          )}

          {!attrLoading && !attrError && attribution && (
            <>
              {/* 样本不足时不展示可能具有误导性的稳定结论（summary） */}
              {!hasAnySampleWarning && attribution.summary && (
                <Alert
                  type="info"
                  showIcon
                  message={t("portfolioAttribution.summary")}
                  description={attribution.summary}
                  style={{ marginBottom: 12 }}
                />
              )}

              <Tabs
                activeKey={activeAttrTab}
                onChange={setActiveAttrTab}
                size="small"
                items={[
                  {
                    key: "by_member",
                    label: t("portfolioAttribution.byMember"),
                    children: (
                      <DimensionView
                        dim={attribution.by_member}
                        columns={attrColumns}
                        sampleText={sampleSizeText(attribution.by_member)}
                      />
                    ),
                  },
                  {
                    key: "by_execution_mode",
                    label: t("portfolioAttribution.byExecutionMode"),
                    children: (
                      <DimensionView
                        dim={attribution.by_execution_mode}
                        columns={attrColumns}
                        sampleText={sampleSizeText(attribution.by_execution_mode)}
                      />
                    ),
                  },
                  {
                    key: "by_source",
                    label: t("portfolioAttribution.bySource"),
                    children: (
                      <DimensionView
                        dim={attribution.by_source}
                        columns={attrColumns}
                        sampleText={sampleSizeText(attribution.by_source)}
                      />
                    ),
                  },
                  {
                    key: "by_rule_signal",
                    label: t("portfolioAttribution.byRuleSignal"),
                    children: (
                      <DimensionView
                        dim={attribution.by_rule_signal}
                        columns={attrColumns}
                        sampleText={sampleSizeText(attribution.by_rule_signal)}
                      />
                    ),
                  },
                  {
                    key: "backtest_vs_sim",
                    label: t("portfolioAttribution.backtestVsSim"),
                    children: (
                      <div data-testid="backtest-vs-sim-view">
                        {backtestDiffRows.length > 0 ? (
                          <Table
                            size="small"
                            pagination={false}
                            rowKey="key"
                            dataSource={backtestDiffRows}
                            columns={[
                              { title: t("portfolioAttribution.diff"), dataIndex: "metric", key: "metric" },
                              {
                                title: t("portfolioAttribution.label"),
                                dataIndex: "value",
                                key: "value",
                                render: (v: number) => (typeof v === "number" ? v.toFixed(4) : String(v)),
                              },
                            ]}
                          />
                        ) : (
                          <div className="empty">{t("portfolioAttribution.noData")}</div>
                        )}
                        {attribution.backtest_vs_sim?.explanation && (
                          <p className="panel-meta" style={{ marginTop: 8, fontSize: 11 }}>
                            {t("portfolioAttribution.explanation")}: {attribution.backtest_vs_sim.explanation}
                          </p>
                        )}
                      </div>
                    ),
                  },
                  {
                    key: "cost_impact",
                    label: t("portfolioAttribution.costImpact"),
                    children: (
                      <div className="account-grid" data-testid="cost-impact-view">
                        <div className="account-item">
                          <span className="metric-label">{t("portfolioAttribution.totalCost")}</span>
                          <span className="metric-value">{money(attribution.cost_impact?.total_cost)}</span>
                        </div>
                        <div className="account-item">
                          <span className="metric-label">{t("portfolioAttribution.slippageCost")}</span>
                          <span className="metric-value">{money(attribution.cost_impact?.slippage_cost)}</span>
                        </div>
                        <div className="account-item">
                          <span className="metric-label">{t("portfolioAttribution.rejectedCount")}</span>
                          <span className="metric-value">{attribution.cost_impact?.rejected_count ?? "-"}</span>
                        </div>
                        <div className="account-item">
                          <span className="metric-label">{t("portfolioAttribution.riskBlocked")}</span>
                          <span className="metric-value">{attribution.cost_impact?.risk_blocked_count ?? "-"}</span>
                        </div>
                        <div className="account-item">
                          <span className="metric-label">{t("portfolioAttribution.impactPct")}</span>
                          <span className="metric-value">{percent(attribution.cost_impact?.impact_pct)}</span>
                        </div>
                      </div>
                    ),
                  },
                ]}
              />

              {/* 基准对比区块 */}
              <div style={{ marginTop: 16 }} data-testid="benchmark-block">
                <h3 style={{ fontSize: 13, marginBottom: 8 }}>{t("portfolioAttribution.benchmark")}</h3>
                {attribution.benchmark ? (
                  <div className="account-grid">
                    <div className="account-item">
                      <span className="metric-label">{t("portfolioAttribution.benchmark")}</span>
                      <span className="metric-value">{attribution.benchmark.name}</span>
                    </div>
                    <div className="account-item">
                      <span className="metric-label">{t("portfolioAttribution.excessReturn")}</span>
                      <span className={`metric-value ${pnlClass(attribution.benchmark.excess_return)}`}>
                        {percent(attribution.benchmark.excess_return)}
                      </span>
                    </div>
                    <div className="account-item">
                      <span className="metric-label">{t("portfolioAttribution.trackingError")}</span>
                      <span className="metric-value">{percent(attribution.benchmark.tracking_error)}</span>
                    </div>
                    <div className="account-item">
                      <span className="metric-label">{t("portfolioAttribution.informationRatio")}</span>
                      <span className="metric-value">{(attribution.benchmark.information_ratio ?? 0).toFixed(2)}</span>
                    </div>
                  </div>
                ) : (
                  <Alert type="warning" showIcon message={t("portfolioAttribution.noBenchmark")} />
                )}
              </div>

              {/* 复盘记录区块 */}
              <div style={{ marginTop: 16 }} data-testid="review-section">
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
                  <h3 style={{ fontSize: 13, margin: 0 }}>{t("portfolioAttribution.reviewHistory")}</h3>
                  <Button
                    size="small"
                    onClick={() => setReviewFormOpen((v) => !v)}
                    data-testid="create-review-button"
                  >
                    {t("portfolioAttribution.createReview")}
                  </Button>
                </div>

                {reviewFormOpen && (
                  <div style={{ marginBottom: 12 }} data-testid="review-form">
                    <p style={{ fontSize: 11, color: "#94a3b8", marginBottom: 4 }}>
                      {t("portfolioAttribution.attachSnapshot")}
                    </p>
                    <Input.TextArea
                      rows={3}
                      value={reviewNote}
                      onChange={(e) => setReviewNote(e.target.value)}
                      placeholder={t("portfolioAttribution.reviewNote")}
                      data-testid="review-note-input"
                    />
                    <div style={{ marginTop: 8, display: "flex", gap: 8 }}>
                      <Button
                        type="primary"
                        size="small"
                        loading={reviewSubmitting}
                        onClick={handleCreateReview}
                        disabled={!reviewNote.trim()}
                        data-testid="review-submit-button"
                      >
                        {t("portfolioAttribution.submit")}
                      </Button>
                      <Button size="small" onClick={() => { setReviewFormOpen(false); setReviewNote(""); }}>
                        {t("portfolioAttribution.cancel")}
                      </Button>
                    </div>
                  </div>
                )}

                {reviewsLoading ? (
                  <Skeleton active paragraph={{ rows: 2 }} />
                ) : reviews.length > 0 ? (
                  <div data-testid="review-list">
                    {reviews.map((rv) => (
                      <div
                        key={rv.id}
                        style={{ padding: "6px 0", borderBottom: "1px solid #f0f0f0", fontSize: 12 }}
                      >
                        <div style={{ display: "flex", justifyContent: "space-between" }}>
                          <span>{rv.note}</span>
                          <span style={{ color: "#94a3b8" }}>{rv.created_at}</span>
                        </div>
                        {rv.attribution_snapshot && (
                          <span style={{ color: "#94a3b8", fontSize: 11 }}>
                            {t("portfolioAttribution.attachSnapshot")}
                          </span>
                        )}
                      </div>
                    ))}
                  </div>
                ) : (
                  <div className="empty" style={{ fontSize: 12 }}>{t("portfolioAttribution.noReviews")}</div>
                )}
              </div>
            </>
          )}
        </div>
      )}
    </section>
  );
}

/**
 * 归因维度视图：样本数提示 + 样本不足告警 + 分组表格。
 * 样本不足时不展示具有误导性的稳定结论（由父级控制 summary，此处仅展示原始数据）。
 */
function DimensionView({
  dim,
  columns,
  sampleText,
}: {
  dim: AttributionDimension | undefined;
  columns: Array<{ title: string; dataIndex: string; key: string; render?: (v: any) => ReactNode }>;
  sampleText: string;
}) {
  if (!dim) {
    return <div className="empty">{t("portfolioAttribution.noData")}</div>;
  }
  return (
    <div>
      {sampleText && (
        <p className="panel-meta" style={{ fontSize: 11, marginBottom: 4 }}>
          {t("portfolioAttribution.sampleSize")}: {sampleText}
        </p>
      )}
      {dim.sample_warning && (
        <Alert
          type="warning"
          showIcon
          message={t("portfolioAttribution.sampleWarning")}
          description={dim.sample_warning}
          style={{ marginBottom: 8 }}
          data-testid="sample-warning-alert"
        />
      )}
      {dim.items.length > 0 ? (
        <Table
          size="small"
          pagination={false}
          rowKey="label"
          dataSource={dim.items}
          columns={columns as any}
        />
      ) : (
        <div className="empty">{t("portfolioAttribution.noData")}</div>
      )}
    </div>
  );
}
