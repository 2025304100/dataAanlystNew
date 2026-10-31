import { useEffect, useMemo, useRef, useState } from "react";
import { Empty, Modal, Table } from "antd";
import type { ColumnsType } from "antd/es/table";
import { api } from "../api/client";
import { useApp } from "../context/AppContext";
import type { MacroIndicator, MacroOverview } from "../types";
import { t, template } from "../i18n";

// VIZ-0929-16：分维度换算系数必须与后端总分同源（app/services/macro.py 的
// `50 + weighted * 2.25`）。此前前端脉搏/雷达用 ×2.5，同一原始值会出现
// "脉搏 68 / 总分口径 66" 两套数，用户无从判断哪个是真的。
const MACRO_DIM_SCALE = 2.25;

/** 页面支持的区域筛选。历史接口只接受 cn|us，故卡片取历史时必须用指标自身的 region。 */
const REGION_OPTIONS = [
  { value: "all", labelKey: "macro_all" },
  { value: "cn", labelKey: "macro_cn" },
  { value: "us", labelKey: "macro_us" },
] as const;

type RegionValue = (typeof REGION_OPTIONS)[number]["value"];

/** 历史曲线的期数区间（对应原型右上角 12/24/36 分段控件）。 */
const RANGE_OPTIONS = [12, 24, 36] as const;

/** 指标卡 sparkline 取多少期。原型每卡 8 个点。 */
const SPARK_POINTS = 8;
/** sparkline 并发上限，避免一次打出十几个请求。 */
const SPARK_CONCURRENCY = 4;
/** 历史曲线单次取数上限（接口 limit 最大 240，取 36 足够覆盖最大区间）。 */
const HISTORY_LIMIT = 36;

const CHART_W = 1160;
const CHART_H = 280;
const CHART_PAD_LEFT = 20;
const CHART_PAD_RIGHT = 70;
const CHART_PAD_TOP = 30;
const CHART_PAD_BOTTOM = 40;

const SPARK_W = 200;
const SPARK_H = 40;

function indicatorName(row: MacroIndicator) {
  const key = "macro_indicator_" + row.indicator_key;
  const translated = t(key);
  return translated === key ? row.name : translated;
}

function indicatorTip(key: string): { text: string; bench: string } {
  const tipKey = "macro_tip_" + key;
  const benchKey = "macro_bench_" + key;
  const text = t(tipKey);
  const bench = t(benchKey);
  return {
    text: text === tipKey ? "" : text,
    bench: bench === benchKey ? "" : bench,
  };
}

/** 频率英文值 → 本地化文案。后端只给 monthly/quarterly/... 这类枚举。 */
function frequencyLabel(frequency: string | null | undefined): string {
  switch ((frequency ?? "").toLowerCase()) {
    case "monthly":
      return t("macro_freq_monthly");
    case "quarterly":
      return t("macro_freq_quarterly");
    case "weekly":
      return t("macro_freq_weekly");
    case "daily":
      return t("macro_freq_daily");
    default:
      return t("macro_freq_irregular");
  }
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

/** 数值与单位拆开渲染：原型把单位做成小字跟随主数值。 */
function splitValue(row: MacroIndicator): { text: string; unit: string } {
  const value = row.value;
  const unit = row.unit ?? "";
  if (!isFiniteNumber(value)) return { text: "-", unit: "" };
  if (unit === "CNY") return { text: (value / 100000000).toFixed(2), unit: t("macro_unit_yi_yuan") };
  if (unit === "CNY 100M") return { text: value.toFixed(0), unit: t("macro_unit_yi_yuan") };
  if (unit === "10k people") return { text: value.toFixed(1), unit: t("macro_unit_wan_ren") };
  const abs = Math.abs(value);
  if (abs >= 10000 && !unit) return { text: (value / 10000).toFixed(2), unit: t("macro_unit_wan") };
  return { text: value.toFixed(abs >= 100 ? 0 : 2), unit };
}

/** 变化值带正负号；百分数类指标用「个百分点」，其余直接跟随单位。 */
function deltaText(delta: number | null | undefined, unit: string | null | undefined): string {
  if (!isFiniteNumber(delta)) return "-";
  const abs = Math.abs(delta);
  const digits = abs >= 100 ? 0 : abs >= 1 ? 2 : 3;
  const sign = delta > 0 ? "+" : delta < 0 ? "-" : "";
  const body = Math.abs(delta).toFixed(digits);
  const suffix = unit === "%" ? t("macro_delta_pp") : unit ? ` ${unit}` : "";
  return `${sign}${body}${suffix}`;
}

function deltaClass(delta: number | null | undefined): string {
  if (!isFiniteNumber(delta) || delta === 0) return "is-flat";
  return delta > 0 ? "is-up" : "is-down";
}

/** 维度分档：只由分数推导，不编造业务解读。 */
function dimensionLevel(score: number): { label: string; hint: string } {
  if (score >= 68) return { label: t("macro_dim_strong"), hint: t("macro_dim_hint_strong") };
  if (score <= 42) return { label: t("macro_dim_weak"), hint: t("macro_dim_hint_weak") };
  return { label: t("macro_dim_neutral"), hint: t("macro_dim_hint_neutral") };
}

function clampScore(value: number): number {
  return Math.max(0, Math.min(100, value));
}

function pickSeries(rows: MacroIndicator[]): MacroIndicator[] {
  return rows.filter((row) => isFiniteNumber(row.value));
}

/** sparkline 几何：只画线 + 极淡面积，不带坐标轴。 */
function buildSpark(rows: MacroIndicator[]) {
  const series = pickSeries(rows);
  if (series.length < 2) return null;
  const values = series.map((row) => row.value as number);
  let min = Math.min(...values);
  let max = Math.max(...values);
  if (min === max) {
    min -= 1;
    max += 1;
  }
  const padY = 4;
  const x0 = 4;
  const x1 = SPARK_W - 4;
  const y0 = padY;
  const y1 = SPARK_H - padY;
  const xOf = (index: number) => x0 + (index / (series.length - 1)) * (x1 - x0);
  const yOf = (value: number) => y1 - ((value - min) / (max - min)) * (y1 - y0);
  const coords = series.map((row, index) => [xOf(index), yOf(row.value as number)] as const);
  const points = coords.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  const area = `M${coords.map(([x, y]) => `${x.toFixed(1)} ${y.toFixed(1)}`).join(" L")} L${x1.toFixed(1)} ${y1} L${x0.toFixed(1)} ${y1} Z`;
  return { points, area, series };
}

/** 历史曲线几何：网格、坐标轴、折线、面积、均值参考线、末端标注、时间刻度。 */
function buildChart(rows: MacroIndicator[]) {
  const series = pickSeries(rows);
  if (series.length < 2) return null;
  const values = series.map((row) => row.value as number);
  let min = Math.min(...values);
  let max = Math.max(...values);
  if (min === max) {
    min -= 1;
    max += 1;
  }
  const span = max - min;
  min -= span * 0.12;
  max += span * 0.12;

  const x0 = CHART_PAD_LEFT;
  const x1 = CHART_W - CHART_PAD_RIGHT;
  const y0 = CHART_PAD_TOP;
  const y1 = CHART_H - CHART_PAD_BOTTOM;

  const xOf = (index: number) => x0 + (index / (series.length - 1)) * (x1 - x0);
  const yOf = (value: number) => y1 - ((value - min) / (max - min)) * (y1 - y0);

  const coords = series.map((row, index) => [xOf(index), yOf(row.value as number)] as const);
  const points = coords.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  const area = `M${coords.map(([x, y]) => `${x.toFixed(1)} ${y.toFixed(1)}`).join(" L")} L${x1.toFixed(1)} ${y1} L${x0.toFixed(1)} ${y1} Z`;

  const mean = values.reduce((sum, value) => sum + value, 0) / values.length;

  const gridRows = 6;
  const gridLines = Array.from({ length: gridRows }, (_, index) => {
    const y = y0 + ((y1 - y0) / (gridRows - 1)) * index;
    return y;
  });

  // 时间刻度取首、末与两个中间点，避免标签重叠
  const tickIndexes = [0, Math.round((series.length - 1) / 3), Math.round(((series.length - 1) * 2) / 3), series.length - 1];
  const ticks = Array.from(new Set(tickIndexes)).map((index) => ({
    x: xOf(index),
    label: series[index].period,
  }));

  const last = coords[coords.length - 1];
  const lastValue = series[series.length - 1].value as number;

  return {
    series,
    points,
    area,
    coords,
    gridLines,
    ticks,
    x0,
    x1,
    y0,
    y1,
    yOf,
    mean,
    meanY: yOf(mean),
    lastX: last[0],
    lastY: last[1],
    lastValue,
    maxLabel: max.toFixed(2),
    minLabel: min.toFixed(2),
  };
}

function csvEscape(value: unknown): string {
  const text = value === null || value === undefined ? "" : String(value);
  if (/[",\n\r]/.test(text)) return `"${text.replace(/"/g, '""')}"`;
  return text;
}

type FailureDetail = {
  key: string;
  name: string;
  scope: string;
  detail: string;
};

function toDisplayText(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function normalizeFailureDetails(items: unknown, fallbackScope = "macro", fallbackMessage?: string): FailureDetail[] {
  if (!Array.isArray(items) || items.length === 0) {
    if (!fallbackMessage) return [];
    return [{
      key: `${fallbackScope}-fallback`,
      name: fallbackScope,
      scope: fallbackScope,
      detail: fallbackMessage,
    }];
  }
  return items.map((item, index) => {
    if (item && typeof item === "object") {
      const row = item as Record<string, unknown>;
      const name =
        toDisplayText(row.name) ||
        toDisplayText(row.indicator_key) ||
        toDisplayText(row.scope) ||
        toDisplayText(row.source) ||
        `${fallbackScope}-${index + 1}`;
      const scope =
        toDisplayText(row.scope) ||
        toDisplayText(row.source) ||
        toDisplayText(row.region) ||
        fallbackScope;
      const detail =
        toDisplayText(row.error) ||
        toDisplayText(row.message) ||
        toDisplayText(row.reason) ||
        toDisplayText(row.detail) ||
        toDisplayText(row.status) ||
        fallbackMessage ||
        "-";
      const key =
        toDisplayText(row.indicator_key) ||
        toDisplayText(row.key) ||
        toDisplayText(row.name) ||
        `${fallbackScope}-${index}`;
      return { key: `${key}-${index}`, name, scope, detail };
    }
    const text = toDisplayText(item) || fallbackMessage || "-";
    return { key: `${fallbackScope}-${index}`, name: text, scope: fallbackScope, detail: text };
  });
}

function failurePreview(items: FailureDetail[]) {
  return items.slice(0, 3).map((item) => (
    item.detail && item.detail !== item.name ? `${item.name}: ${item.detail}` : item.name
  )).join(" / ");
}

function localizeIndicatorTaskName(value: string | null | undefined): string {
  if (!value) return "";
  const key = "macro_indicator_" + value;
  const translated = t(key);
  return translated === key ? value : translated;
}

function formatTaskProgressTitle(
  task: { processed?: number | null; total?: number | null },
): string {
  const processed = task.processed ?? 0;
  const total = task.total ?? 0;
  if (total <= 0) return t("macro_syncing");
  return template("macro_task_progress_title", { processed, total });
}

function formatTaskProgressSummary(
  task: { processed?: number | null; total?: number | null; ok_count?: number | null; failed_count?: number | null; current_item?: string | null },
): string {
  const processed = task.processed ?? 0;
  const total = task.total ?? 0;
  const okCount = task.ok_count ?? 0;
  const failedCount = task.failed_count ?? 0;
  const currentItem = localizeIndicatorTaskName(task.current_item);
  const params = { processed, total, okCount, failedCount, currentItem };
  if (!currentItem) return template("macro_task_progress_summary", params);
  return template("macro_task_progress_summary_with_current", params);
}

/* ------------------------------- 图标 ------------------------------- */

function IconInfo() {
  return (
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="8" cy="8" r="5.8" />
      <path d="M8 7.2v4" />
      <path d="M8 4.9h.01" />
    </svg>
  );
}

function IconWarning() {
  return (
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M8 2.2 14.4 13H1.6L8 2.2Z" />
      <path d="M8 6.4v3.2" />
      <path d="M8 11.6h.01" />
    </svg>
  );
}

function IconDownload() {
  return (
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M8 2.6v7.2" />
      <path d="M4.8 6.6 8 9.8l3.2-3.2" />
      <path d="M2.6 12.4h10.8" />
    </svg>
  );
}

function IconArrowUp() {
  return (
    <svg viewBox="0 0 12 12" aria-hidden="true">
      <path d="M6 2.4 10.2 9.4H1.8L6 2.4Z" fill="currentColor" />
    </svg>
  );
}

function IconArrowDown() {
  return (
    <svg viewBox="0 0 12 12" aria-hidden="true">
      <path d="M6 9.6 1.8 2.6h8.4L6 9.6Z" fill="currentColor" />
    </svg>
  );
}

function IconEmptyBox() {
  return (
    <svg className="macro-empty-icon" viewBox="0 0 32 32" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M4 11.5 16 5l12 6.5v9L16 27 4 20.5v-9Z" />
      <path d="M4 11.5 16 18l12-6.5" />
      <path d="M16 18v9" />
    </svg>
  );
}

export default function MacroData() {
  const ctx = useApp();
  const [region, setRegion] = useState<RegionValue>("all");
  const [overview, setOverview] = useState<MacroOverview | null>(null);
  const [booting, setBooting] = useState(true);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [updateTask, setUpdateTask] = useState<any | null>(null);
  const pollRef = useRef<number | null>(null);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [range, setRange] = useState<number>(36);
  const [history, setHistory] = useState<MacroIndicator[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [taskNotice, setTaskNotice] = useState<string | null>(null);
  const [taskNoticeType, setTaskNoticeType] = useState<"success" | "info" | "warning" | "error" | null>(null);
  const [taskFailureDetails, setTaskFailureDetails] = useState<FailureDetail[]>([]);
  const [failureDetailOpen, setFailureDetailOpen] = useState(false);
  const terminalNoticeRef = useRef<string | null>(null);

  // sparkline 缓存：key = `${region}:${indicator_key}`。用 ref 存数据、用 state 触发重渲染，
  // 避免把缓存本身放进 effect 依赖导致循环取数。
  const sparkCacheRef = useRef<Record<string, MacroIndicator[]>>({});
  const sparkInflightRef = useRef<Set<string>>(new Set());
  const [sparkVersion, setSparkVersion] = useState(0);

  const stopPolling = () => {
    if (pollRef.current) {
      window.clearInterval(pollRef.current);
      pollRef.current = null;
    }
  };

  const notifyTaskOutcome = (taskId: string, status: string, type: "success" | "info" | "warning" | "error", text: string) => {
    const key = `${taskId}:${status}:${text}`;
    if (!text || terminalNoticeRef.current === key) return;
    terminalNoticeRef.current = key;
    if (type === "success") ctx.showToast("success", text);
    else if (type === "error") ctx.showToast("error", text);
    else ctx.showToast("info", text);
  };

  const load = async (nextRegion: RegionValue = region) => {
    setError(null);
    try {
      const data = await api.getMacroOverview(nextRegion);
      setOverview(data);
    } catch (err: any) {
      setError(err.message);
    } finally {
      setBooting(false);
    }
  };

  useEffect(() => {
    load(region);
  }, [region]);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const task = await api.getLatestMacroUpdateTask();
        if (!alive || !task || !["queued", "running"].includes(task.status)) return;
        setUpdateTask(task);
        setLoading(true);
      } catch {
        // ignore resume failure
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  useEffect(() => {
    if (!updateTask || !["queued", "running"].includes(updateTask.status)) {
      stopPolling();
      return;
    }
    pollRef.current = window.setInterval(async () => {
      try {
        const next = await api.getMacroUpdateTask(updateTask.id);
        setUpdateTask(next);
        if (!["queued", "running"].includes(next.status)) {
          stopPolling();
        }
        if (next.status === "done") {
          const failures = normalizeFailureDetails(next.result?.failed, next.result?.region ?? "macro");
          const notice = failures.length ? `${t("macro_task_partial")} (${failures.length})` : t("macro_task_done");
          setTaskFailureDetails(failures);
          setTaskNotice(notice);
          setTaskNoticeType(failures.length ? "warning" : "success");
          setError(null);
          notifyTaskOutcome(next.id, next.status, failures.length ? "warning" : "success", notice);
          await load(region);
          setLoading(false);
        } else if (next.status === "failed") {
          const failures = normalizeFailureDetails(next.errors, "macro", next.message || t("macro_task_failed"));
          const notice = next.message || t("macro_task_failed");
          setTaskFailureDetails(failures);
          setTaskNotice(notice);
          setTaskNoticeType("error");
          setError(null);
          notifyTaskOutcome(next.id, next.status, "error", notice);
          setLoading(false);
        } else if (next.status === "cancelled") {
          setTaskFailureDetails([]);
          setTaskNotice(t("macro_task_cancelled"));
          setTaskNoticeType("info");
          setError(null);
          notifyTaskOutcome(next.id, next.status, "info", t("macro_task_cancelled"));
          setLoading(false);
        }
      } catch (err: any) {
        stopPolling();
        setError(err.message);
        setLoading(false);
      }
    }, 2500);
    return () => {
      stopPolling();
    };
  }, [ctx.locale, region, updateTask?.id, updateTask?.status]);

  const refresh = async () => {
    setLoading(true);
    setError(null);
    setTaskNotice(null);
    setTaskNoticeType(null);
    setTaskFailureDetails([]);
    setFailureDetailOpen(false);
    terminalNoticeRef.current = null;
    // 刷新后历史序列可能新增数据点，清空 sparkline 缓存强制重取
    sparkCacheRef.current = {};
    setSparkVersion((version) => version + 1);
    try {
      const task = await api.startMacroUpdateTask({ region });
      setUpdateTask(task);
      if (task.status === "done") {
        const failures = normalizeFailureDetails(task.result?.failed, task.result?.region ?? "macro");
        const notice = failures.length ? `${t("macro_task_partial")} (${failures.length})` : t("macro_task_done");
        setTaskFailureDetails(failures);
        setTaskNotice(notice);
        setTaskNoticeType(failures.length ? "warning" : "success");
        notifyTaskOutcome(task.id, task.status, failures.length ? "warning" : "success", notice);
        await load(region);
        setLoading(false);
      }
    } catch (err: any) {
      setError(err.message);
      setLoading(false);
    }
  };

  const cancelRefresh = async () => {
    if (!updateTask?.id) return;
    try {
      const task = await api.cancelMacroUpdateTask(updateTask.id);
      setUpdateTask(task);
      stopPolling();
      setTaskFailureDetails([]);
      setTaskNotice(t("macro_task_cancelled"));
      setTaskNoticeType("info");
      setError(null);
      notifyTaskOutcome(task.id, task.status, "info", t("macro_task_cancelled"));
      setLoading(false);
    } catch (err: any) {
      setError(err.message);
    }
  };

  const snapshot = overview?.snapshot ?? null;
  const indicators = useMemo(() => overview?.indicators ?? [], [overview]);
  const running = Boolean(updateTask && ["queued", "running"].includes(updateTask.status));

  const overviewFailureDetails = useMemo(
    () => normalizeFailureDetails(overview?.failed, region),
    [overview?.failed, region],
  );
  const activeFailureDetails = taskFailureDetails.length ? taskFailureDetails : overviewFailureDetails;

  /* ------------------------- 市场脉搏五维 ------------------------- */
  const dimensionRows = useMemo(() => {
    if (!snapshot) return [];
    return [
      { key: "growth", raw: snapshot.growth_score },
      { key: "inflation", raw: snapshot.inflation_score },
      { key: "liquidity", raw: snapshot.liquidity_score },
      { key: "credit", raw: snapshot.credit_score },
      { key: "risk", raw: snapshot.risk_score },
    ].map((row) => {
      const score = clampScore(50 + row.raw * MACRO_DIM_SCALE);
      return { ...row, score, level: dimensionLevel(score) };
    });
  }, [snapshot]);

  /* --------------------------- 页头副标题 --------------------------- */
  const subtitle = useMemo(() => {
    const parts: string[] = [];
    if (indicators.length) parts.push(template("macro_indicators_count", { count: indicators.length }));
    const periods = indicators.map((row) => row.period).filter(Boolean).sort();
    const latestPeriod = periods.length ? periods[periods.length - 1] : "";
    if (latestPeriod) parts.push(template("macro_updated_through", { period: latestPeriod }));
    const sources = Array.from(new Set(indicators.map((row) => row.source).filter(Boolean))).slice(0, 3);
    if (sources.length) parts.push(template("macro_sources", { sources: sources.join(" / ") }));
    return parts.length ? parts.join(" · ") : t("macro_subtitle");
  }, [indicators]);

  /* ------------------------ sparkline 取数 ------------------------ */
  useEffect(() => {
    if (!indicators.length) return;
    const tasks = indicators
      .map((row) => ({
        id: `${row.region}:${row.indicator_key}`,
        region: row.region,
        key: row.indicator_key,
      }))
      .filter((task) => !(task.id in sparkCacheRef.current) && !sparkInflightRef.current.has(task.id));
    if (!tasks.length) return;

    let cancelled = false;

    const runOne = async (task: { id: string; region: string; key: string }) => {
      sparkInflightRef.current.add(task.id);
      try {
        const rows = await api.getMacroIndicatorHistory(task.region, task.key, SPARK_POINTS);
        if (cancelled) return;
        sparkCacheRef.current[task.id] = Array.isArray(rows) ? rows : [];
      } catch {
        if (cancelled) return;
        sparkCacheRef.current[task.id] = [];
      } finally {
        sparkInflightRef.current.delete(task.id);
        if (!cancelled) setSparkVersion((version) => version + 1);
      }
    };

    (async () => {
      for (let index = 0; index < tasks.length; index += SPARK_CONCURRENCY) {
        if (cancelled) return;
        await Promise.all(tasks.slice(index, index + SPARK_CONCURRENCY).map(runOne));
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [indicators]);

  /* --------------------------- 选中指标 --------------------------- */
  const selectedRow = useMemo(() => {
    if (!indicators.length) return null;
    if (selectedKey) {
      const hit = indicators.find((row) => `${row.region}:${row.indicator_key}` === selectedKey);
      if (hit) return hit;
    }
    // 默认落在制造业 PMI（原型的主曲线），没有则退回第一条
    return indicators.find((row) => row.indicator_key === "cn_pmi") ?? indicators[0];
  }, [indicators, selectedKey]);

  /* ------------------------- 历史曲线取数 ------------------------- */
  useEffect(() => {
    if (!selectedRow) {
      setHistory([]);
      return;
    }
    let cancelled = false;
    setHistoryLoading(true);
    api
      .getMacroIndicatorHistory(selectedRow.region, selectedRow.indicator_key, HISTORY_LIMIT)
      .then((rows) => {
        if (cancelled) return;
        setHistory(Array.isArray(rows) ? rows : []);
      })
      .catch(() => {
        if (cancelled) return;
        setHistory([]);
      })
      .finally(() => {
        if (cancelled) return;
        setHistoryLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedRow?.region, selectedRow?.indicator_key, overview]);

  const chartRows = useMemo(() => {
    const rows = pickSeries(history);
    if (rows.length <= range) return rows;
    return rows.slice(rows.length - range);
  }, [history, range]);

  const chart = useMemo(() => buildChart(chartRows), [chartRows]);

  /* --------------------------- 导出快照 --------------------------- */
  const exportSnapshot = () => {
    if (!indicators.length) return;
    const header = [
      t("macro_category"),
      t("macro_indicator"),
      t("macro_period"),
      t("macro_value"),
      t("macro_previous"),
      t("macro_delta"),
      t("macro_unit_label"),
      t("macro_score"),
      t("macro_status"),
      t("macro_source"),
    ];
    const lines = indicators.map((row) => [
      t("macro_" + row.category),
      indicatorName(row),
      row.period,
      row.value ?? "",
      row.previous_value ?? "",
      row.delta ?? "",
      row.unit ?? "",
      row.score,
      t("macro_" + row.status),
      row.source,
    ]);
    const csv = "\ufeff" + [header, ...lines].map((cells) => cells.map(csvEscape).join(",")).join("\r\n");
    const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    const stamp = new Date().toISOString().slice(0, 10);
    anchor.href = url;
    anchor.download = `macro-${region}-${stamp}.csv`;
    document.body.appendChild(anchor);
    anchor.click();
    document.body.removeChild(anchor);
    URL.revokeObjectURL(url);
    ctx.showToast("success", t("macro_export_snapshot"));
  };

  const failureColumns: ColumnsType<FailureDetail> = [
    { title: t("macro_failure_item"), dataIndex: "name", width: 220 },
    { title: t("macro_failure_scope"), dataIndex: "scope", width: 140 },
    {
      title: t("macro_failure_detail"),
      dataIndex: "detail",
      render: (value: string) => <span style={{ whiteSpace: "pre-wrap", wordBreak: "break-word" }}>{value || "-"}</span>,
    },
  ];

  const alertClass = (type: "success" | "info" | "warning" | "error") => `macro-alert macro-alert--${type}`;

  return (
    <div className="macro-page">
      {/* ------------------------------ 页头 ------------------------------ */}
      <header className="macro-head">
        <div className="macro-head-copy">
          <p className="macro-kicker">{t("macro_kicker")}</p>
          <h1 className="macro-title">{t("macro_title")}</h1>
          <p className="macro-sub">{subtitle}</p>
        </div>
        <div className="macro-actions">
          <button
            type="button"
            className="macro-btn macro-btn--secondary"
            onClick={exportSnapshot}
            disabled={!indicators.length}
          >
            <IconDownload />
            {t("macro_export_snapshot")}
          </button>
          <button
            type="button"
            className="macro-btn macro-btn--primary"
            onClick={refresh}
            disabled={running}
          >
            {running ? t("macro_syncing") : t("macro_update")}
          </button>
          {running ? (
            <button type="button" className="macro-btn macro-btn--ghost" onClick={cancelRefresh}>
              {t("macro_task_cancel")}
            </button>
          ) : null}
        </div>
      </header>

      {/* ---------------------------- 任务进度 ---------------------------- */}
      {running ? (
        <div className={alertClass("info")} role="status">
          <IconInfo />
          <div className="macro-alert-copy">
            <p className="macro-alert-title">{t("macro_task_running")}</p>
            <p className="macro-alert-body">{formatTaskProgressTitle(updateTask)}</p>
            <div className="macro-progress">
              <div
                className="macro-progress-fill"
                style={{ width: `${Math.max(0, Math.min(100, Math.round(updateTask?.percent ?? 0)))}%` }}
              />
            </div>
            <p className="macro-alert-body">{formatTaskProgressSummary(updateTask)}</p>
          </div>
        </div>
      ) : null}

      {/* ---------------------------- 任务结果 ---------------------------- */}
      {taskNotice && taskNoticeType ? (
        <div className={alertClass(taskNoticeType === "success" ? "success" : taskNoticeType === "error" ? "error" : taskNoticeType === "warning" ? "warning" : "info")}>
          <IconWarning />
          <div className="macro-alert-copy">
            <p className="macro-alert-title">{taskNotice}</p>
            {taskFailureDetails.length ? (
              <>
                <p className="macro-alert-body">{failurePreview(taskFailureDetails)}</p>
                <div className="macro-alert-actions">
                  <button type="button" className="macro-btn macro-btn--secondary" onClick={() => setFailureDetailOpen(true)}>
                    {t("macro_view_failures")}
                  </button>
                </div>
              </>
            ) : null}
          </div>
        </div>
      ) : null}

      {/* ------------------------------ 错误 ------------------------------ */}
      {error ? (
        <div className={alertClass("error")} role="alert">
          <IconWarning />
          <div className="macro-alert-copy">
            <p className="macro-alert-title">{error}</p>
          </div>
        </div>
      ) : null}

      {/* ------------------------------ 主体 ------------------------------ */}
      {booting ? (
        <section className="macro-card macro-empty" aria-busy="true">
          <span className="macro-spinner" aria-hidden="true" />
          <p className="macro-empty-title">{t("loading")}</p>
        </section>
      ) : !snapshot ? (
        <section className="macro-card macro-empty">
          <IconEmptyBox />
          <p className="macro-empty-title">{t("macro_empty")}</p>
          <p className="macro-empty-desc">{t("macro_subtitle")}</p>
          <button type="button" className="macro-btn macro-btn--primary" onClick={refresh} disabled={loading}>
            {t("macro_update")}
          </button>
        </section>
      ) : (
        <>
          {/* ① 市场脉搏：综合分 + 五个维度 */}
          <section className="macro-card">
            <div className="macro-card-head">
              <div className="macro-card-head-copy">
                <h2 className="macro-card-title">{t("macro_market_pulse")}</h2>
                <span className="macro-card-sub">{t("macro_pulse_sub")}</span>
              </div>
              <div className="macro-seg" role="tablist" aria-label={t("macro_region")}>
                {REGION_OPTIONS.map((option) => (
                  <button
                    key={option.value}
                    type="button"
                    role="tab"
                    aria-selected={region === option.value}
                    className={`macro-seg-item${region === option.value ? " is-active" : ""}`}
                    onClick={() => setRegion(option.value)}
                  >
                    {t(option.labelKey)}
                  </button>
                ))}
              </div>
            </div>
            <div className="macro-card-body">
              <div className="macro-kpi-row">
                <div className="macro-kpi macro-kpi--composite">
                  <p className="macro-kpi-label">{t("macro_market_score")}</p>
                  <div className="macro-kpi-value">
                    {snapshot.market_score.toFixed(1)}
                    <span className="macro-kpi-unit">{t("macro_score_unit")}</span>
                  </div>
                  <div className="macro-bar">
                    <div className="macro-bar-fill" style={{ width: `${clampScore(snapshot.market_score)}%` }} />
                  </div>
                  <p className="macro-kpi-hint">{t("macro_" + snapshot.stance)}</p>
                </div>
                {dimensionRows.map((dim) => (
                  <div key={dim.key} className="macro-kpi">
                    <p className="macro-kpi-label">{t("macro_" + dim.key)}</p>
                    <div className="macro-kpi-value">
                      {dim.score.toFixed(0)}
                      <span className="macro-kpi-unit">{t("macro_score_unit")}</span>
                    </div>
                    <div className="macro-bar">
                      <div className="macro-bar-fill" style={{ width: `${dim.score}%` }} />
                    </div>
                    <p className="macro-kpi-hint">{`${dim.level.label} · ${dim.level.hint}`}</p>
                  </div>
                ))}
              </div>
            </div>
          </section>

          {/* ② 立场横幅 */}
          <div className={alertClass("info")}>
            <IconInfo />
            <div className="macro-alert-copy">
              <p className="macro-alert-title">
                {t("macro_stance")}：{t("macro_" + snapshot.stance)}
              </p>
              <p className="macro-alert-body">
                {[snapshot.summary, t("macro_stance_hint_" + snapshot.stance)].filter(Boolean).join(" ")}
              </p>
            </div>
          </div>

          {/* ③ 指标卡片网格 */}
          <div>
            <div className="macro-card-head" style={{ borderBottom: "none", padding: 0, marginBottom: 8 }}>
              <div className="macro-card-head-copy">
                <h2 className="macro-card-title">{t("macro_table")}</h2>
                <span className="macro-card-sub">{t("macro_click_card_hint")}</span>
              </div>
            </div>
            <div className="macro-grid">
              {indicators.map((row) => {
                const id = `${row.region}:${row.indicator_key}`;
                const value = splitValue(row);
                const spark = buildSpark(sparkCacheRef.current[id] ?? []);
                const spanRows = sparkCacheRef.current[id] ?? [];
                const active = selectedRow ? `${selectedRow.region}:${selectedRow.indicator_key}` === id : false;
                const tip = indicatorTip(row.indicator_key);
                return (
                  <button
                    key={id}
                    type="button"
                    className={`macro-ind-card${active ? " is-active" : ""}`}
                    aria-pressed={active}
                    title={tip.text || indicatorName(row)}
                    onClick={() => setSelectedKey(id)}
                    data-spark-version={sparkVersion}
                  >
                    <div className="macro-ind-head">
                      <p className="macro-ind-label">{indicatorName(row)}</p>
                      <span className="macro-ind-tag">{frequencyLabel(row.frequency)}</span>
                    </div>
                    <div className="macro-ind-value">
                      {value.text}
                      {value.unit ? <span className="macro-ind-unit">{value.unit}</span> : null}
                    </div>
                    <div className={`macro-ind-delta ${deltaClass(row.delta)}`}>
                      {isFiniteNumber(row.delta) && row.delta !== 0 ? (row.delta > 0 ? <IconArrowUp /> : <IconArrowDown />) : null}
                      {deltaText(row.delta, row.unit)}
                    </div>
                    {spark ? (
                      <div className="macro-ind-spark">
                        <svg viewBox={`0 0 ${SPARK_W} ${SPARK_H}`} role="img" aria-label={`${indicatorName(row)} ${t("macro_history_chart")}`}>
                          <path className="macro-svg-spark-area" d={spark.area} />
                          <polyline className="macro-svg-spark" points={spark.points} />
                        </svg>
                      </div>
                    ) : (
                      <p className="macro-ind-empty">{t("macro_no_spark")}</p>
                    )}
                    <p className="macro-ind-span">
                      {spanRows.length
                        ? template("macro_span_recent", { count: spanRows.length, period: row.period })
                        : row.period}
                    </p>
                  </button>
                );
              })}
            </div>
          </div>

          {/* ④ 历史曲线 */}
          <section className="macro-chart">
            <div className="macro-chart-head">
              <div>
                <h2 className="macro-chart-title">
                  {selectedRow ? indicatorName(selectedRow) : t("macro_history_chart")} · {t("macro_history_chart")}
                </h2>
                <p className="macro-chart-sub">
                  {[
                    selectedRow ? frequencyLabel(selectedRow.frequency) : "",
                    selectedRow ? selectedRow.source : "",
                    selectedRow ? indicatorTip(selectedRow.indicator_key).bench : "",
                  ]
                    .filter(Boolean)
                    .join(" · ")}
                </p>
              </div>
              <div className="macro-chart-tools">
                <div className="macro-chart-legend">
                  <span>
                    <i className="macro-legend-dot macro-legend-dot--series" aria-hidden="true" />
                    {selectedRow ? indicatorName(selectedRow) : t("macro_indicator")}
                  </span>
                  {chart ? (
                    <span>
                      <i className="macro-legend-dot macro-legend-dot--ref" aria-hidden="true" />
                      {template("macro_reference_mean", { value: chart.mean.toFixed(2) })}
                    </span>
                  ) : null}
                </div>
                <div className="macro-seg" role="tablist" aria-label={t("macro_history_chart")}>
                  {RANGE_OPTIONS.map((option) => (
                    <button
                      key={option}
                      type="button"
                      role="tab"
                      aria-selected={range === option}
                      className={`macro-seg-item${range === option ? " is-active" : ""}`}
                      onClick={() => setRange(option)}
                    >
                      {template("macro_range_periods", { count: option })}
                    </button>
                  ))}
                </div>
              </div>
            </div>

            {historyLoading ? (
              <div className="macro-chart-loading" aria-busy="true">
                <span className="macro-spinner" aria-hidden="true" />
                {t("loading")}
              </div>
            ) : chart ? (
              <div className="macro-chart-body">
                <svg viewBox={`0 0 ${CHART_W} ${CHART_H}`} role="img" aria-label={`${selectedRow ? indicatorName(selectedRow) : ""} ${t("macro_history_chart")}`}>
                  {chart.gridLines.map((y, index) => (
                    <line key={index} className="macro-svg-grid" x1={chart.x0} y1={y.toFixed(1)} x2={chart.x1} y2={y.toFixed(1)} />
                  ))}
                  <line className="macro-svg-axis" x1={chart.x0} y1={chart.y1} x2={chart.x1} y2={chart.y1} />
                  <path className="macro-svg-area" d={chart.area} />
                  <polyline className="macro-svg-line" points={chart.points} />
                  <line
                    className="macro-svg-ref"
                    x1={chart.x0}
                    y1={chart.meanY.toFixed(1)}
                    x2={chart.x1}
                    y2={chart.meanY.toFixed(1)}
                  />
                  <circle className="macro-svg-dot" cx={chart.lastX.toFixed(1)} cy={chart.lastY.toFixed(1)} r="3" />
                  <text className="macro-svg-label-strong" x={chart.lastX + 8} y={chart.lastY + 4}>
                    {chart.lastValue.toFixed(2)}
                  </text>
                  <text className="macro-svg-label" x={chart.x1 + 8} y={chart.meanY + 4}>
                    {template("macro_reference_mean", { value: chart.mean.toFixed(2) })}
                  </text>
                  <text className="macro-svg-label" x={chart.x0} y={chart.y0 - 10}>
                    {chart.maxLabel}
                  </text>
                  <text className="macro-svg-label" x={chart.x0} y={chart.y1 + 16}>
                    {chart.minLabel}
                  </text>
                  {chart.ticks.map((tick) => (
                    <text
                      key={`${tick.label}-${tick.x}`}
                      className="macro-svg-label"
                      x={Math.min(tick.x, chart.x1 - 40).toFixed(1)}
                      y={chart.y1 + 34}
                    >
                      {tick.label}
                    </text>
                  ))}
                </svg>
              </div>
            ) : (
              <div className="macro-chart-empty">{t("macro_no_history")}</div>
            )}
          </section>
        </>
      )}

      {/* -------------------------- 失败详情弹窗 -------------------------- */}
      <Modal
        open={failureDetailOpen}
        title={t("macro_failure_details")}
        onCancel={() => setFailureDetailOpen(false)}
        footer={null}
        width={860}
        destroyOnHidden
      >
        {activeFailureDetails.length ? (
          <Table
            rowKey="key"
            columns={failureColumns}
            dataSource={activeFailureDetails}
            pagination={{ pageSize: 8, size: "small" }}
            scroll={{ x: 720 }}
            size="small"
          />
        ) : (
          <Empty description={t("macro_failure_empty")} />
        )}
      </Modal>
    </div>
  );
}
