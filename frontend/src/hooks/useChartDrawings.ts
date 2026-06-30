/**
 * useChartDrawings — 图表趋势线绘制 hook
 *
 * 管理用户绘制的趋势线 / 水平线，持久化到 localStorage，
 * 提供 ECharts 点击事件处理和 line series 生成。
 */
import { useState, useEffect, useCallback, useMemo } from "react";

// ── Types ─────────────────────────────────────────────

export interface DrawingPoint {
  date: string;
  price: number;
}

export interface ChartDrawing {
  id: string;
  start: DrawingPoint;
  end: DrawingPoint;
  color: string;
  lineWidth: number;
  lineType: "solid" | "dashed" | "dotted";
}

export type DrawingMode = "none" | "trendline" | "horizontal";

// ── Storage ───────────────────────────────────────────

const STORAGE_PREFIX = "chart_drawings_";

function loadDrawings(symbolId: number): ChartDrawing[] {
  try {
    const raw = localStorage.getItem(`${STORAGE_PREFIX}${symbolId}`);
    return raw ? JSON.parse(raw) : [];
  } catch {
    return [];
  }
}

function saveDrawings(symbolId: number, drawings: ChartDrawing[]): void {
  try {
    localStorage.setItem(
      `${STORAGE_PREFIX}${symbolId}`,
      JSON.stringify(drawings)
    );
  } catch {
    /* localStorage unavailable */
  }
}

// ── Default palette ───────────────────────────────────

const PALETTE = ["#f59e0b", "#3b82f6", "#ec4899", "#10b981", "#8b5cf6", "#ef4444"];

function nextColor(existing: ChartDrawing[]): string {
  const used = new Set(existing.map((d) => d.color));
  return PALETTE.find((c) => !used.has(c)) ?? PALETTE[existing.length % PALETTE.length];
}

// ── Hook ──────────────────────────────────────────────

export function useChartDrawings(symbolId: number | null) {
  const [drawings, setDrawings] = useState<ChartDrawing[]>([]);
  const [drawingMode, setDrawingMode] = useState<DrawingMode>("none");
  const [pendingPoint, setPendingPoint] = useState<DrawingPoint | null>(null);

  // Load when symbol changes
  useEffect(() => {
    if (symbolId == null) {
      setDrawings([]);
      return;
    }
    setDrawings(loadDrawings(symbolId));
    setDrawingMode("none");
    setPendingPoint(null);
  }, [symbolId]);

  // Persist on change
  useEffect(() => {
    if (symbolId != null) saveDrawings(symbolId, drawings);
  }, [symbolId, drawings]);

  // ECharts click handler
  const handleChartClick = useCallback(
    (params: any, chartInstance: any) => {
      if (drawingMode === "none" || !chartInstance) return;
      try {
        const coord = chartInstance.convertFromPixel(
          { seriesIndex: 0 },
          [params.event?.offsetX ?? 0, params.event?.offsetY ?? 0]
        );
        if (!coord) return;
        const dateIdx = Math.round(coord[0]);
        const price = Number(coord[1]);
        if (isNaN(price) || dateIdx < 0) return;

        // Resolve date string from x-axis category data
        const option = chartInstance.getOption();
        const xData: string[] = option?.xAxis?.[0]?.data ?? [];
        if (dateIdx >= xData.length) return;
        const dateStr = xData[dateIdx];
        const point: DrawingPoint = { date: dateStr, price: Number(price.toFixed(2)) };

        if (!pendingPoint) {
          setPendingPoint(point);
        } else {
          setDrawings((prev) => {
            const final: ChartDrawing = {
              id: `d_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`,
              start: pendingPoint,
              end:
                drawingMode === "horizontal"
                  ? { ...point, price: pendingPoint.price }
                  : point,
              color: nextColor(prev),
              lineWidth: 1.5,
              lineType: "solid",
            };
            return [...prev, final];
          });
          setPendingPoint(null);
          setDrawingMode("none");
        }
      } catch {
        /* ignore conversion errors */
      }
    },
    [drawingMode, pendingPoint]
  );

  // ── Actions ──────────────────────────────────────────

  const startDrawing = useCallback((mode: DrawingMode) => {
    setDrawingMode(mode);
    setPendingPoint(null);
  }, []);

  const cancelDrawing = useCallback(() => {
    setDrawingMode("none");
    setPendingPoint(null);
  }, []);

  const removeLastDrawing = useCallback(() => {
    setDrawings((prev) => prev.slice(0, -1));
  }, []);

  const clearAllDrawings = useCallback(() => {
    setDrawings([]);
  }, []);

  const removeDrawing = useCallback((id: string) => {
    setDrawings((prev) => prev.filter((d) => d.id !== id));
  }, []);

  // ── ECharts line series generation ───────────────────

  const drawingLineSeries = useMemo(() => {
    return drawings.map((d) => ({
      type: "line" as const,
      data: [
        { value: [d.start.date, d.start.price] },
        { value: [d.end.date, d.end.price] },
      ],
      lineStyle: {
        color: d.color,
        width: d.lineWidth,
        type: d.lineType,
      },
      itemStyle: { color: d.color },
      symbol: "circle",
      symbolSize: 5,
      smooth: false,
      silent: false,
      z: 10,
      _drawingId: d.id,
    }));
  }, [drawings]);

  // Pending-point indicator (dashed ghost line from first point)
  const pendingSeries = useMemo(() => {
    if (!pendingPoint) return null;
    return {
      type: "line" as const,
      data: [{ value: [pendingPoint.date, pendingPoint.price] }],
      lineStyle: { width: 0 },
      itemStyle: { color: "#f59e0b", borderWidth: 2, borderColor: "#fff" },
      symbol: "circle",
      symbolSize: 10,
      silent: true,
      z: 11,
    };
  }, [pendingPoint]);

  // All price values from drawings (for y-axis range extension)
  const drawingPrices = useMemo(() => {
    const prices: number[] = [];
    drawings.forEach((d) => {
      prices.push(d.start.price, d.end.price);
    });
    if (pendingPoint) prices.push(pendingPoint.price);
    return prices;
  }, [drawings, pendingPoint]);

  return {
    drawings,
    drawingMode,
    pendingPoint,
    handleChartClick,
    startDrawing,
    cancelDrawing,
    removeLastDrawing,
    clearAllDrawings,
    removeDrawing,
    drawingLineSeries,
    pendingSeries,
    drawingPrices,
  };
}
