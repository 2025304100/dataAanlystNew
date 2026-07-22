// WP5.1：标的图表面板
//
// 设计说明：
// - 任务清单 #8：K线 + MA10/MA20 + MACD + RSI + BOLL + 绘图 Hook 统一
// - 从 InvestmentCenter.tsx 抽出 renderChartSection JSX 块
// - FuturePlanOverlay 子组件保留在本文件内（与图表强耦合）
// - 图表数据计算（chartData/chartOption/macdOption/rsiOption）保留在 Shell
// - useChartDrawings hook 可在此组件内调用，但当前 InvestmentCenter 未启用绘图功能
//   保留接口（drawingHook）供 WP5.3 接入
//
// 关键约束：
// - 不重写技术指标计算（computeMA/computeMACD/computeRSI/computeBOLL 留在 Shell）
// - 不重写图表 option 构造逻辑
// - 通过 props 接收所有计算好的 option 和回调
import ReactECharts from "echarts-for-react";
import { Button } from "antd";
import { t, DOT } from "../../i18n";
import { score } from "../../utils/format";
import { FUTURE_PLAN_STYLE_MAP } from "../../constants/chartTheme";
import type { FutureBuyPlan } from "../../types";
import type { SymbolChartPanelProps } from "./types";

/* ════════════════════════════════════════════════════════════
   Future Buy Plan SVG Overlay（与 DetailModal 一致）
   ════════════════════════════════════════════════════════════ */
function FuturePlanOverlay({
  plans,
  lastClose,
  priceMin,
  priceMax,
  height,
}: {
  plans: FutureBuyPlan[];
  lastClose: number;
  priceMin: number;
  priceMax: number;
  height: number;
}) {
  const panelWidth = 168;
  const padding = 8;
  const bandPadding = 6;
  const plotTop = 30;
  const plotBottom = 50;
  const legendHeight = 28;
  const plotHeight = height - plotTop - plotBottom - legendHeight;
  const priceSpan = Math.max(priceMax - priceMin, 0.01);

  const priceToY = (price: number) => {
    const ratio = (priceMax - price) / priceSpan;
    return plotTop + ratio * plotHeight;
  };

  const styleMap = FUTURE_PLAN_STYLE_MAP;
  const usablePlans = plans.filter((plan) => plan.zone_min !== null && plan.zone_max !== null);
  const nonAvoidPlans = usablePlans.filter((p) => p.priority !== "avoid").slice(0, 3);
  const svgWidth = panelWidth + padding * 2;

  // 复用 i18n 模块的 futureBuyLabel（已在文件顶部导入）
  // 这里直接通过 t 兜底，避免再次导入
  const labelOf = (v: string | null) => {
    if (!v) return "-";
    const tr = t(v);
    return tr === v ? v : tr;
  };

  return (
    <svg
      style={{
        position: "absolute",
        right: 0,
        top: 0,
        width: svgWidth,
        height,
        pointerEvents: "none",
        overflow: "visible",
      }}
      viewBox={`0 0 ${svgWidth} ${height}`}
    >
      <rect
        x={padding}
        y={plotTop}
        width={panelWidth - padding}
        height={plotHeight}
        rx={8}
        ry={8}
        fill="rgba(31,41,51,0.035)"
        stroke="rgba(31,41,51,0.10)"
        strokeWidth={1}
        strokeDasharray="4,4"
      />
      <text x={padding + 6} y={plotTop + 18} fontSize={11} fill="#6b7280">
        {t("futureBuyPlan")}
      </text>

      {usablePlans.map((plan) => {
        const style = styleMap[plan.priority] ?? styleMap.normal;
        const yMin = priceToY(Math.min(plan.zone_min!, plan.zone_max!));
        const yMax = priceToY(Math.max(plan.zone_min!, plan.zone_max!));
        const bandH = Math.max(Math.abs(yMax - yMin), 12);
        const bandY = Math.min(yMin, yMax);
        return (
          <g key={plan.label}>
            <rect
              x={padding + bandPadding}
              y={bandY}
              width={panelWidth - padding * 2 - bandPadding}
              height={bandH}
              rx={6}
              ry={6}
              fill={style.fill}
              stroke={style.stroke}
              strokeWidth={1}
              strokeDasharray={style.dash || undefined}
            />
            <text
              x={padding + panelWidth - padding - 2}
              y={bandY + bandH / 2}
              fontSize={11}
              fill={style.stroke}
              textAnchor="end"
              dominantBaseline="middle"
            >
              {labelOf(plan.label)}
            </text>
            <text
              x={padding + bandPadding + 3}
              y={bandY + bandH / 2}
              fontSize={10}
              fill={style.stroke}
              dominantBaseline="middle"
            >
              {score(plan.zone_min)}-{score(plan.zone_max)}
            </text>
          </g>
        );
      })}

      {nonAvoidPlans.length > 0 && (() => {
        const startX = padding + 2;
        const startY = priceToY(lastClose);
        const curvePoints = nonAvoidPlans.map((plan) => ({
          x: padding + panelWidth - padding - 20,
          y: priceToY((Number(plan.zone_min) + Number(plan.zone_max)) / 2),
        }));
        const pathParts = [`M ${startX} ${startY}`];
        curvePoints.forEach((pt) => pathParts.push(`L ${pt.x} ${pt.y}`));
        return (
          <>
            <path
              d={pathParts.join(" ")}
              fill="none"
              stroke="#111827"
              strokeWidth={1.4}
              strokeDasharray="5,5"
              opacity={0.75}
            />
            <circle cx={startX} cy={startY} r={3.5} fill="#111827" />
            {curvePoints.map((pt, i) => (
              <circle key={i} cx={pt.x} cy={pt.y} r={3.5} fill="#111827" />
            ))}
          </>
        );
      })()}
    </svg>
  );
}

/** 取数组最后一个非空值并格式化 */
function lastMAValue(arr: (number | null)[]): string {
  const v = arr[arr.length - 1];
  return v !== null && v !== undefined ? score(v) : "-";
}

/**
 * 标的图表面板：主 K 线 + MACD 副图 + RSI 副图 + FuturePlanOverlay。
 * - 所有 option 由 Shell 计算并传入
 * - 图表点击事件通过 onChartClick 回调到 Shell（设置入场价）
 */
export default function SymbolChartPanel({
  chartData,
  chartOption,
  macdOption,
  rsiOption,
  showMACD,
  showRSI,
  chartExpanded,
  chartTimeframe,
  chartWindowSize,
  lastBar,
  activeFutureBuyPlan,
  onToggleMACD,
  onToggleRSI,
  onSetChartTimeframe,
  onSetChartWindowSize,
  onResetChart,
  onChartClick,
}: SymbolChartPanelProps) {
  return (
    <section className="ic__section ic__chart-section symbol-research-chart">
      <div className="panel">
        <div className="detail-card-head">
          <h3>{t("recentBars")}</h3>
          <div className="chart-toolbar">
            <p className="panel-meta" id="chartMeta">
              {lastBar ? `${lastBar.trade_date}${DOT}${t("close")}: ${score(lastBar.close)}` : "-"}
            </p>
            <div className="detail-actions chart-actions">
              <div className="chart-timeframe-group">
                <Button
                  size="small"
                  type={chartTimeframe === "daily" ? "primary" : "default"}
                  onClick={() => onSetChartTimeframe("daily")}
                >
                  {t("chartDaily")}
                </Button>
                <Button
                  size="small"
                  type={chartTimeframe === "weekly" ? "primary" : "default"}
                  onClick={() => onSetChartTimeframe("weekly")}
                >
                  {t("chartWeekly")}
                </Button>
              </div>
              <span className="chart-window-pill">
                {chartWindowSize}
                {t("barsUnit")}
              </span>
              <Button size="small" onClick={() => onSetChartWindowSize(Math.max(20, chartWindowSize - 20))}>
                {t("zoomIn")}
              </Button>
              <Button size="small" onClick={() => onSetChartWindowSize(Math.min(500, chartWindowSize + 20))}>
                {t("zoomOut")}
              </Button>
              <Button size="small" onClick={onResetChart}>
                {t("resetZoom")}
              </Button>
            </div>
          </div>
          {/* 指标面板开关 */}
          <div className="ic__indicator-toggles">
            <button
              type="button"
              className={`ic__toggle-btn${showMACD ? " active" : ""}`}
              onClick={onToggleMACD}
            >
              MACD
            </button>
            <button
              type="button"
              className={`ic__toggle-btn${showRSI ? " active" : ""}`}
              onClick={onToggleRSI}
            >
              RSI
            </button>
          </div>
        </div>

        <div className={`chart-surface${chartExpanded ? " expanded" : ""}`} id="detailChart">
          {chartOption ? (
            <div className="ic__charts-stack">
              {/* 主K线图 */}
              <div style={{ position: "relative" }}>
                <ReactECharts
                  option={chartOption}
                  style={{ height: chartExpanded ? 520 : 400, width: "100%" }}
                  onEvents={{
                    click: (params: unknown) => onChartClick(params),
                  }}
                />
                {chartData && activeFutureBuyPlan.length > 0 && (
                  <FuturePlanOverlay
                    plans={activeFutureBuyPlan}
                    lastClose={chartData.closes[chartData.closes.length - 1]}
                    priceMin={(() => {
                      const ap = chartData.candlestick.flatMap((c) => [c[1], c[2], c[3], c[4]]);
                      const mv = [...chartData.ma10, ...chartData.ma20].filter(
                        (v): v is number => v != null,
                      );
                      const all = [...ap, ...mv].filter(
                        (v): v is number => v != null && !isNaN(v),
                      );
                      return all.length > 0 ? Math.min(...all) * 0.985 : 0;
                    })()}
                    priceMax={(() => {
                      const ap = chartData.candlestick.flatMap((c) => [c[1], c[2], c[3], c[4]]);
                      const mv = [...chartData.ma10, ...chartData.ma20].filter(
                        (v): v is number => v != null,
                      );
                      const all = [...ap, ...mv].filter(
                        (v): v is number => v != null && !isNaN(v),
                      );
                      return all.length > 0 ? Math.max(...all) * 1.015 : 100;
                    })()}
                    height={chartExpanded ? 520 : 400}
                  />
                )}
              </div>
              {/* MACD 副图 */}
              {macdOption && (
                <div className="ic__sub-chart">
                  <ReactECharts option={macdOption} style={{ height: 140, width: "100%" }} />
                </div>
              )}
              {/* RSI 副图 */}
              {rsiOption && (
                <div className="ic__sub-chart">
                  <ReactECharts option={rsiOption} style={{ height: 140, width: "100%" }} />
                </div>
              )}
            </div>
          ) : (
            <div className="empty">{t("noChart")}</div>
          )}
          {chartData && (
            <div className="chart-legend">
              {lastBar && (
                <span>
                  {t("chartOpen")}:{score(lastBar.open)} {t("chartHigh")}:{score(lastBar.high)}{" "}
                  {t("chartLow")}:{score(lastBar.low)} {t("chartClose")}:{score(lastBar.close)}
                </span>
              )}
              <span>
                {t("ma10")}: {lastMAValue(chartData.ma10)}
              </span>
              <span>
                {t("ma20")}: {lastMAValue(chartData.ma20)}
              </span>
              {chartData.macd && (
                <span>
                  DIF: {lastMAValue(chartData.macd.dif)} DEA: {lastMAValue(chartData.macd.dea)}
                </span>
              )}
              {chartData.rsi && <span>RSI: {lastMAValue(chartData.rsi)}</span>}
            </div>
          )}
        </div>
      </div>
    </section>
  );
}
