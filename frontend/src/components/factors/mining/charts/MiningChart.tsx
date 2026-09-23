import { useEffect, useState } from "react";
import ReactECharts from "echarts-for-react";
import type { EChartsOption } from "echarts";

/**
 * 挖掘模块统一图表容器（暖白主题 / echarts）。
 *
 * 为什么要包一层：
 * 1. **统一视觉**：配色、字号、网格、tooltip、legend 全部取自 mining 令牌，
 *    避免每个图表各写一套 option 导致风格漂移；
 * 2. **jsdom 友好**：与 MacroData / DetailModal 同款「延迟挂载」策略
 *    （`setTimeout(..., 0)` 后才渲染 echarts），单测里 jsdom 的 canvas
 *    不可用时也不会抛错，断言只落在容器与文字上；
 * 3. **空态一致**：无数据时渲染统一占位，不留白、不报错。
 */

/** 暖白主题下的图表配色（与 mining.css 令牌同源，勿在业务组件里另写十六进制） */
export const MINING_CHART_COLORS = {
  brand: "#0f766e",
  brandSoft: "rgba(15, 118, 110, 0.16)",
  accent2: "#1d6fd8",
  accent2Soft: "rgba(29, 111, 216, 0.14)",
  warn: "#b45309",
  danger: "#b42318",
  series: ["#0f766e", "#1d6fd8", "#b45309", "#7c5cbf", "#c2410c", "#0e7490", "#4d7c0f", "#9d174d"],
  axis: "rgba(31, 41, 51, 0.14)",
  axisLabel: "#7b8794",
  grid: "rgba(31, 41, 51, 0.06)",
  text: "#1f2933",
} as const;

/** 图表通用基础 option（网格/坐标轴/tooltip 字体），业务侧只覆盖数据部分 */
export function miningBaseOption(): EChartsOption {
  const c = MINING_CHART_COLORS;
  return {
    color: [...c.series],
    textStyle: {
      fontFamily: '"Segoe UI", "Helvetica Neue", sans-serif',
      fontSize: 12,
      color: c.text,
    },
    grid: { left: 8, right: 12, top: 16, bottom: 4, containLabel: true },
    tooltip: {
      trigger: "axis",
      backgroundColor: "rgba(255, 254, 248, 0.98)",
      borderColor: c.axis,
      borderWidth: 1,
      textStyle: { color: c.text, fontSize: 12 },
      extraCssText: "box-shadow: 0 6px 22px rgba(31,41,51,.14); border-radius: 8px;",
      axisPointer: { type: "shadow", shadowStyle: { color: "rgba(15,118,110,.06)" } },
    },
    legend: { show: false },
  };
}

export interface MiningChartProps {
  /** echarts option（**不含**基础样式时请自行 merge；本组件只做兜底） */
  option: EChartsOption;
  /** 容器高度（px），默认 220 */
  height?: number;
  /** 无数据时展示的文案；缺省渲染空态占位 */
  emptyText?: string;
  /** 是否隐藏空态（数据为空但由父级自行处理提示） */
  hideEmpty?: boolean;
  /** 额外的容器类名（如 mining-pool-board-chart） */
  className?: string;
  /** 数据就绪标记（避免每帧重绘） */
  testId?: string;
}

/**
 * 统一的挖掘图表容器。数据为空 → 渲染 `mining-chart-empty` 占位，
 * 不渲染 echarts（避免 jsdom 报错与真实环境空白画布）。
 */
export default function MiningChart({
  option,
  height = 220,
  emptyText,
  hideEmpty = false,
  className,
  testId,
}: MiningChartProps) {
  const [ready, setReady] = useState(false);

  useEffect(() => {
    const timer = setTimeout(() => setReady(true), 0);
    return () => clearTimeout(timer);
  }, []);

  const series = option.series as unknown;
  const hasSeries =
    Array.isArray(series) && series.length > 0
      ? series.some((s) => {
          const data = (s as { data?: unknown[] })?.data;
          return Array.isArray(data) && data.length > 0;
        })
      : series != null;

  const wrapClass = className ? `mining-chart ${className}` : "mining-chart";

  if (!hasSeries && !hideEmpty) {
    return (
      <div
        className={wrapClass}
        data-mining-chart-empty={testId}
        style={{ minHeight: Math.min(height, 88) }}
      >
        <div className="mining-chart-empty">{emptyText ?? "-"}</div>
      </div>
    );
  }

  return (
    <div className={wrapClass} data-mining-chart={testId}>
      {ready ? (
        <ReactECharts option={option} style={{ height, width: "100%" }} notMerge lazyUpdate />
      ) : (
        <div className="mining-chart-skeleton" style={{ height }} />
      )}
    </div>
  );
}
