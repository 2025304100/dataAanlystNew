import { useCallback, useEffect, useMemo, useState } from "react";
import { DatePicker, Button, Empty, Skeleton, Tooltip } from "antd";
import { QuestionCircleOutlined } from "@ant-design/icons";
import dayjs, { Dayjs } from "dayjs";
import ReactECharts from "echarts-for-react";
import { useApp } from "../context/AppContext";
import { api } from "../api/client";
import { t } from "../i18n";
import { money, percent, pnlClass } from "../utils/format";

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

  // 组合切换或日期变更时重新加载
  useEffect(() => {
    if (portfolioId) {
      loadPerformance();
    } else {
      setResult(null);
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
            <Button size="small" onClick={loadPerformance} loading={loading}>
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
    </section>
  );
}
