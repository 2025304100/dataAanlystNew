import { useState, useEffect, useCallback, useMemo, useRef } from "react";
import { Card, Button, Progress, Statistic, Row, Col, InputNumber, Select, Space, Alert, Tag, Tooltip, Checkbox, Tabs, Table, message, Input, Popconfirm } from "antd";
import type { IndexSyncTaskRead } from "../api/client";
import {
  PlayCircleOutlined, ReloadOutlined, StopOutlined, QuestionCircleOutlined,
  ThunderboltOutlined, HistoryOutlined, ToolOutlined, LineChartOutlined,
  CloudSyncOutlined, PlusOutlined, CloseCircleOutlined,
} from "@ant-design/icons";
import { api } from "../api/client";
import { t } from "../i18n";

// P0.6：初始化 scope 选项
const INIT_SCOPE_OPTIONS = [
  { labelKey: "universeScopeCnStock", value: "cn-stock" },
  { labelKey: "universeScopeCnEtf", value: "cn-etf" },
  { labelKey: "universeScopeUsStock", value: "us-stock" },
  { labelKey: "universeScopeUsEtf", value: "us-etf" },
];

// 历史K线天数下拉选项
const HISTORY_DAYS_OPTIONS = [
  { labelKey: "universeHistory1m", value: 30 },
  { labelKey: "universeHistory1y", value: 365 },
  { labelKey: "universeHistory3y", value: 1095 },
  { labelKey: "universeHistory5y", value: 1825 },
  { labelKey: "universeHistory10y", value: 3650 },
];

// 单次同步标的上限（分段同步，避免一口气跑太久）
// Repair chunk size options for safer mid-history replay
const REPAIR_CHUNK_OPTIONS = [
  { labelKey: "universeRepairChunk30", value: 30 },
  { labelKey: "universeRepairChunk60", value: 60 },
  { labelKey: "universeRepairChunk90", value: 90 },
  { labelKey: "universeRepairChunk180", value: 180 },
  { labelKey: "universeRepairChunk365", value: 365 },
];

const SYNC_LIMIT_OPTIONS = [
  { labelKey: "universeSyncLimitAll", value: 0 },
  { label: "100" , value: 100 },
  { label: "300" , value: 300 },
  { label: "500" , value: 500 },
  { label: "1000", value: 1000 },
  { label: "2000", value: 2000 },
];

type TaskStatus = "queued" | "running" | "done" | "failed" | "cancelled";

interface UniverseTask {
  id: string;
  status: TaskStatus;
  stage: string;
  percent: number;
  message: string;
  total: number;
  processed: number;
  ok_count: number;
  failed_count: number;
  result: Record<string, unknown> | null;
  errors: Array<Record<string, unknown>>;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
}

interface ScopeStat {
  total: number;
  synced: number;
  coverage: number;
}

interface UniverseStats {
  total_symbols: number;
  synced_symbols: number;
  failed_symbols: number;
  circuit_broken_symbols: number;
  total_bars: number;
  by_type: Record<string, { total: number; synced: number }>;
  by_scope?: Record<string, ScopeStat>;
  by_board?: Record<string, ScopeStat>;
  freshness?: {
    today: number;
    "1_3_days": number;
    "3_7_days": number;
    over_7_days: number;
    no_data: number;
  };
  latest_synced_at: string | null;
  is_empty: boolean;
}

// scope 映射（i18n key）
const SCOPE_LABEL_KEYS: Record<string, string> = {
  "cn-stock": "universeScopeCnStock",
  "cn-etf": "universeScopeCnEtf",
  "us-stock": "universeScopeUsStock",
  "us-etf": "universeScopeUsEtf",
};

function scopeLabel(scope: string): string {
  return t(SCOPE_LABEL_KEYS[scope] || "universeNoTask");
}

// 板块映射（i18n key）
const BOARD_LABEL_KEYS: Record<string, string> = {
  main: "universeBoardMain",
  gem: "universeBoardGem",
  star: "universeBoardStar",
  bj: "universeBoardBj",
};

function boardLabel(board: string): string {
  return t(BOARD_LABEL_KEYS[board] || board);
}

const STAGE_LABEL_KEY: Record<string, string> = {
  queued: "universeNoTask",
  refresh_stock: "universeStageRefreshStock",
  refresh_etf: "universeStageRefreshEtf",
  refresh_us_stock: "universeStageRefreshUsStock",
  refresh_us_etf: "universeStageRefreshUsEtf",
  sync_stock: "universeStageSyncStock",
  sync_etf: "universeStageSyncEtf",
  sync_us_stock: "universeStageSyncUsStock",
  sync_us_etf: "universeStageSyncUsEtf",
  done: "universeStageSyncUsEtf",
  cancelled: "universeNoTask",
  failed: "universeNoTask",
};

function stageLabel(stage: string): string {
  return t(STAGE_LABEL_KEY[stage] || "universeNoTask");
}

function statusTag(status: TaskStatus) {
  const map: Record<TaskStatus, { color: string; key: string }> = {
    queued: { color: "default", key: "universeStatusQueued" },
    running: { color: "processing", key: "universeStatusRunning" },
    done: { color: "success", key: "universeStatusDone" },
    failed: { color: "error", key: "universeStatusFailed" },
    cancelled: { color: "warning", key: "universeStatusCancelled" },
  };
  const cfg = map[status] || map.queued;
  return <Tag color={cfg.color}>{t(cfg.key)}</Tag>;
}

/** Turn task error payloads into an actionable message instead of exposing API JSON. */
function getUniverseTaskErrorText(taskError: Record<string, unknown>): string {
  const rawError = taskError.error ?? taskError;
  let payload: Record<string, unknown> | null = null;

  if (rawError && typeof rawError === "object" && !Array.isArray(rawError)) {
    payload = rawError as Record<string, unknown>;
  } else if (typeof rawError === "string") {
    try {
      const parsed = JSON.parse(rawError);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
        payload = parsed as Record<string, unknown>;
      }
    } catch {
      // Plain-text errors are displayed below without parsing.
    }
  }

  if (payload?.code === "BACKEND_RESTART_INTERRUPTED") {
    const correlationId = payload.correlation_id;
    const correlationText = typeof correlationId === "string" && correlationId
      ? ` ${t("universeErrorCorrelation").replace("{id}", correlationId)}`
      : "";
    return `${t("universeBackendRestartInterrupted")}${correlationText}`;
  }

  const detail = payload?.detail_zh ?? payload?.message ?? payload?.error ?? rawError;
  return typeof detail === "string" ? detail : JSON.stringify(detail);
}

// 配置 localStorage 持久化
type RepairScopeSummary = {
  key: string;
  scope: string;
  total: number;
  processed: number;
  ok: number;
  skipped: number;
  failed: number;
  inserted: number;
  updated: number;
  emptyChunks: number;
};

type SyncTabKey = "daily" | "history" | "advanced";
type SyncPanelKey = "smart" | "incremental" | "init" | "backfill" | "repair";

const DEFAULT_SYNC_PANEL_SELECTION: Record<SyncTabKey, SyncPanelKey> = {
  daily: "incremental",
  history: "init",
  advanced: "repair",
};


function toSafeNumber(value: unknown): number {
  if (typeof value === "number" && Number.isFinite(value)) {
    return value;
  }
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

function getRangeRepairSummaries(result: Record<string, unknown> | null | undefined): RepairScopeSummary[] {
  if (!result) {
    return [];
  }

  const scopeOrder = ["cn-stock", "cn-etf", "us-stock", "us-etf"];
  return Object.entries(result)
    .filter(([key, value]) => key.endsWith("_repair") && typeof value === "object" && value !== null)
    .map(([key, value]) => {
      const payload = value as Record<string, unknown>;
      return {
        key,
        scope: key.replace(/_repair$/, ""),
        total: toSafeNumber(payload.total),
        processed: toSafeNumber(payload.processed),
        ok: toSafeNumber(payload.ok),
        skipped: toSafeNumber(payload.skipped),
        failed: toSafeNumber(payload.failed),
        inserted: toSafeNumber(payload.inserted),
        updated: toSafeNumber(payload.updated),
        emptyChunks: toSafeNumber(payload.empty_chunks),
      };
    })
    .sort((a, b) => scopeOrder.indexOf(a.scope) - scopeOrder.indexOf(b.scope));
}

function getBackfillSummaries(result: Record<string, unknown> | null | undefined): RepairScopeSummary[] {
  if (!result) {
    return [];
  }

  const scopeOrder = ["cn-stock", "cn-etf", "us-stock", "us-etf"];
  return Object.entries(result)
    .filter(([key, value]) => key.endsWith("_backfill") && typeof value === "object" && value !== null)
    .map(([key, value]) => {
      const payload = value as Record<string, unknown>;
      return {
        key,
        scope: key.replace(/_backfill$/, ""),
        total: toSafeNumber(payload.total),
        processed: toSafeNumber(payload.processed),
        ok: toSafeNumber(payload.ok),
        skipped: toSafeNumber(payload.skipped),
        failed: toSafeNumber(payload.failed),
        inserted: 0,
        updated: 0,
        emptyChunks: 0,
      };
    })
    .sort((a, b) => scopeOrder.indexOf(a.scope) - scopeOrder.indexOf(b.scope));
}

const UNIVERSE_CONFIG_KEY = "universe_config_v1";

function loadUniverseConfig() {
  try {
    const raw = localStorage.getItem(UNIVERSE_CONFIG_KEY);
    if (!raw) return null;
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

function saveUniverseConfig(cfg: Record<string, unknown>) {
  try {
    localStorage.setItem(UNIVERSE_CONFIG_KEY, JSON.stringify(cfg));
  } catch {
    // ignore
  }
}

// ─── 基准指数数据同步类型 ───
interface IndexStatusItem {
  symbol: string; name: string; bar_count: number;
  first_date: string | null; last_date: string | null;
  freshness_days: number | null; linearity_dev_pct: number | null;
  is_custom?: boolean;  // 是否用户自定义
  last_sync_error?: string | null;
  last_sync_result?: {
    received?: number; written?: number; skipped?: number;
    error?: string | null;
  } | null;
}
const INDEX_HISTORY_DAYS_OPTIONS = [
  { label: "近1年", value: 365 },
  { label: "近2年", value: 730 },
  { label: "近3年", value: 1095 },
  { label: "近5年 (推荐)", value: 1825 },
  { label: "近10年", value: 3650 },
];
const CUSTOM_INDEX_KEY = "benchmark.custom_indices.v1";
interface CustomIndexEntry { symbol: string; name: string }
const DEFAULT_5_SYMBOLS = ["000300", "000905", "399006", "000016", "000688"];
const DEFAULT_5_NAMES: Record<string, string> = {
  "000300": "沪深300", "000905": "中证500", "399006": "创业板指",
  "000016": "上证50", "000688": "科创50",
};
function loadCustomIndices(): CustomIndexEntry[] {
  try {
    const raw = localStorage.getItem(CUSTOM_INDEX_KEY);
    if (!raw) return [];
    const arr = JSON.parse(raw);
    return Array.isArray(arr) ? arr.filter((x) => x && typeof x.symbol === "string") : [];
  } catch { return []; }
}
function saveCustomIndices(list: CustomIndexEntry[]) {
  try { localStorage.setItem(CUSTOM_INDEX_KEY, JSON.stringify(list || [])); } catch {}
}

export default function UniverseDataPanel() {
  const _savedCfg = loadUniverseConfig();
  const [stats, setStats] = useState<UniverseStats | null>(null);
  const [task, setTask] = useState<UniverseTask | null>(null);
  const [smartTask, setSmartTask] = useState<UniverseTask | null>(null);
  const [incrTask, setIncrTask] = useState<UniverseTask | null>(null);
  const [loading, setLoading] = useState(false);
  const [starting, setStarting] = useState(false);
  const [smartStarting, setSmartStarting] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [smartCancelling, setSmartCancelling] = useState(false);
  const [incrStarting, setIncrStarting] = useState(false);
  const [incrCancelling, setIncrCancelling] = useState(false);
  const [maxWorkers, setMaxWorkers] = useState<number>(_savedCfg?.maxWorkers ?? 5);
  const [historyDays, setHistoryDays] = useState<number>(_savedCfg?.historyDays ?? 365);
  const [syncLimit, setSyncLimit] = useState<number>(_savedCfg?.syncLimit ?? 0);
  const [syncTab, setSyncTab] = useState<SyncTabKey>("daily");
  const [syncPanelSelection, setSyncPanelSelection] = useState<Record<SyncTabKey, SyncPanelKey>>(DEFAULT_SYNC_PANEL_SELECTION);
  // P0.6：初始化 scope 选择，默认全部4个
  const [initScopes, setInitScopes] = useState<string[]>(_savedCfg?.initScopes ?? ["cn-stock"]);
  // 历史回补
  const [bfTask, setBfTask] = useState<UniverseTask | null>(null);
  const [bfStarting, setBfStarting] = useState(false);
  const [bfCancelling, setBfCancelling] = useState(false);
  const [bfHistoryDays, setBfHistoryDays] = useState<number>(_savedCfg?.bfHistoryDays ?? 1095);
  const [bfScopes, setBfScopes] = useState<string[]>(_savedCfg?.bfScopes ?? ["cn-stock"]);
  const [bfSyncLimit, setBfSyncLimit] = useState<number>(_savedCfg?.bfSyncLimit ?? 0);
  const [repairTask, setRepairTask] = useState<UniverseTask | null>(null);
  const [repairStarting, setRepairStarting] = useState(false);
  const [repairCancelling, setRepairCancelling] = useState(false);
  const [repairHistoryDays, setRepairHistoryDays] = useState<number>(_savedCfg?.repairHistoryDays ?? 365);
  const [repairChunkDays, setRepairChunkDays] = useState<number>(_savedCfg?.repairChunkDays ?? 90);
  const [repairScopes, setRepairScopes] = useState<string[]>(_savedCfg?.repairScopes ?? ["cn-stock"]);
  const [repairSyncLimit, setRepairSyncLimit] = useState<number>(_savedCfg?.repairSyncLimit ?? 0);

  // ─── 基准指数同步状态 ───
  const [indexStatusList, setIndexStatusList] = useState<IndexStatusItem[]>([]);
  const [indexStatusLoading, setIndexStatusLoading] = useState(false);
  const [indexSyncRunning, setIndexSyncRunning] = useState(false);
  const [indexSyncTask, setIndexSyncTask] = useState<IndexSyncTaskRead | null>(null);
  const indexSyncPollRef = useRef<number | null>(null);  // 心跳轮询 interval id，用于卸载/重新开始时清理
  const indexSyncPollInFlightRef = useRef(false);
  const [indexSelectedSymbols, setIndexSelectedSymbols] = useState<string[]>([]);
  const [indexHistoryDays, setIndexHistoryDays] = useState<number>(1825);
  // 自定义指数（localStorage）
  const [customIndices, setCustomIndices] = useState<CustomIndexEntry[]>(() => loadCustomIndices());
  const [addSymbol, setAddSymbol] = useState("");
  const [addName, setAddName] = useState("");
  const [addLoading, setAddLoading] = useState(false);

  const effectiveSymbolsToQuery = useMemo(() => {
    const set = new Map<string, { symbol: string; name: string; is_custom: boolean }>();
    DEFAULT_5_SYMBOLS.forEach((s) => set.set(s, { symbol: s, name: DEFAULT_5_NAMES[s] || s, is_custom: false }));
    customIndices.forEach((c) => set.set(c.symbol, { symbol: c.symbol, name: c.name || c.symbol, is_custom: true }));
    return Array.from(set.values());
  }, [customIndices]);

  const loadIndexStatus = useCallback(async (silent = true) => {
    if (!silent) setIndexStatusLoading(true);
    try {
      const symbolsParam = effectiveSymbolsToQuery.map((e) => e.symbol).join(",");
      const res = await api.getIndexPricesStatus(symbolsParam);
      // 补全自定义标记 + 自定义名称
      const customMap = new Map<string, string>();
      effectiveSymbolsToQuery.forEach((e) => {
        if (e.is_custom) customMap.set(e.symbol, e.name);
      });
      const base = (res?.items || []).map((it) => ({
        ...it,
        name: customMap.get(it.symbol) || it.name || DEFAULT_5_NAMES[it.symbol] || it.symbol,
        is_custom: customMap.has(it.symbol),
      }));
      // 若有 customIndices 不在 base 里（后端尚未写入时的占位条目），补占位
      customIndices.forEach((c) => {
        if (!base.find((b) => b.symbol === c.symbol)) {
          base.push({
            symbol: c.symbol, name: c.name || c.symbol, bar_count: 0,
            first_date: null, last_date: null, freshness_days: null,
            linearity_dev_pct: null, is_custom: true,
          });
        }
      });
      setIndexStatusList(base);
      // 只在当前无选中项（首次加载 / 用户从未勾选时）智能预选：仅选中需要同步的（无数据或疑似直线）
      // 修复：之前如果全部健康会 fallback 到"全选"，导致一打开5个基准都被勾选染绿背景，误以为无法取消
      if (indexSelectedSymbols.length === 0) {
        const pick = base
          .filter((it) => it.bar_count === 0 || (it.linearity_dev_pct ?? 0) < 0.1)
          .map((it) => it.symbol);
        // 注意：即便 pick 为空（全部健康），也保持空数组，不做全选
        setIndexSelectedSymbols(pick);
      }
    } catch (e: any) {
      if (!silent) message.error("基准指数状态加载失败: " + (e?.message ?? String(e)));
    } finally {
      if (!silent) setIndexStatusLoading(false);
    }
  }, [effectiveSymbolsToQuery, indexSelectedSymbols.length, customIndices]);

  useEffect(() => {
    loadIndexStatus(true).catch(() => {});
    const t = setInterval(() => loadIndexStatus(true).catch(() => {}), 60_000);
    return () => clearInterval(t);
  }, [loadIndexStatus]);

  // 同步心跳只在组件真正卸载时终止；不能依赖 loadIndexStatus 的变化清理，
  // 否则用户切换勾选项会意外停止已提交的后台任务轮询。
  useEffect(() => () => {
    if (indexSyncPollRef.current != null) {
      clearInterval(indexSyncPollRef.current);
      indexSyncPollRef.current = null;
    }
  }, []);

  const addCustomIndex = async () => {
    const sym = (addSymbol || "").trim();
    if (!sym) {
      message.warning("请输入指数代码，例如 000001（上证指数）、000010（上证180）、399005（中小板指）");
      return;
    }
    // 不允许加默认 5 （避免重复）
    if (DEFAULT_5_SYMBOLS.includes(sym)) {
      message.warning("该基准已存在于默认 5 大基准中，无需重复添加");
      return;
    }
    if (customIndices.find((c) => c.symbol === sym)) {
      message.warning("该指数已存在于自定义列表中");
      return;
    }
    setAddLoading(true);
    try {
      const name = (addName || "").trim() || sym;
      const next = [...customIndices, { symbol: sym, name }];
      setCustomIndices(next);
      saveCustomIndices(next);
      message.success(`已添加：${name}（${sym}）。可以勾选后点击「同步选中」拉取历史行情。`);
      setAddSymbol("");
      setAddName("");
      await loadIndexStatus(false);
    } finally {
      setAddLoading(false);
    }
  };

  const removeCustomIndex = (symbol: string) => {
    const next = customIndices.filter((c) => c.symbol !== symbol);
    setCustomIndices(next);
    saveCustomIndices(next);
    setIndexSelectedSymbols((prev) => prev.filter((s) => s !== symbol));
    setIndexStatusList((prev) => prev.filter((p) => p.symbol !== symbol || DEFAULT_5_SYMBOLS.includes(p.symbol)));
    message.success(`已从自定义列表移除：${symbol}`);
  };

  // 指数同步进度 Toast 渲染（带 Progress + 文字状态）
  const renderIndexSyncProgress = (t: IndexSyncTaskRead, extra?: string) => {
    const percent = Math.max(0, Math.min(100, Math.round(t.percent ?? 0)));
    const total = t.total ?? 0;
    const processed = t.processed ?? 0;
    const ok = t.ok_count ?? 0;
    const failed = t.failed_count ?? 0;
    return (
      <div style={{ minWidth: 320 }}>
        <div style={{ fontWeight: 600, marginBottom: 6 }}>
          {t.message || "同步中..."}
          {t.current_item ? <span style={{ color: "var(--text-muted, #888)", fontSize: 12, marginLeft: 8 }}>[当前 {t.current_item}]</span> : null}
        </div>
        <Progress percent={percent} status={t.status === "failed" ? "exception" : undefined} showInfo style={{ marginBottom: 6 }} />
        <div style={{ fontSize: 12, color: "var(--text-muted, #888)", display: "flex", justifyContent: "space-between" }}>
          <span>进度：{processed}/{total > 0 ? total : "-"}</span>
          <span>
            <span style={{ color: "var(--color-success, #16a34a)" }}>成功 {ok}</span>
            {failed > 0 ? <span style={{ color: "var(--color-danger, #dc2626)", marginLeft: 8 }}>失败 {failed}</span> : null}
          </span>
        </div>
        {extra ? <div style={{ marginTop: 4, fontSize: 12, color: "var(--color-primary, #1677ff)" }}>{extra}</div> : null}
      </div>
    );
  };

  const handleSyncIndexPrices = async (all = false, symbolsOverride?: string[]) => {
    // 表格单行同步必须使用显式入参。不能依赖刚 setState 的选中项，
    // 否则 React 尚未提交状态时会把上一轮勾选的指数提交到后台。
    const symbols = all ? undefined : (symbolsOverride ?? indexSelectedSymbols);
    if (!all && (!symbols || symbols.length === 0)) {
      message.warning("请至少选择一个指数");
      return;
    }
    // 清理之前的轮询，防止并发多跑
    if (indexSyncPollRef.current != null) {
      clearInterval(indexSyncPollRef.current);
      indexSyncPollRef.current = null;
    }
    indexSyncPollInFlightRef.current = false;
    setIndexSyncRunning(true);
    const toastKey = `index-sync-${Date.now()}`;

    try {
      // Step 1: 异步提交，立即返回 task_id，不再阻塞 HTTP
      const submitted: IndexSyncTaskRead = all
        ? await api.syncAllBenchmarkIndices()
        : await api.syncIndexPrices({
            symbols: symbols && symbols.length > 0 ? symbols : undefined,
            history_days: indexHistoryDays,
          });
      setIndexSyncTask(submitted);

      message.open({
        key: toastKey,
        type: "loading",
        duration: 0, // 0 = 常驻，心跳刷新；到终态再手动关闭/替换
        content: renderIndexSyncProgress(submitted, "已提交任务，后台 worker 启动中..."),
      });

      // Step 2: 2 秒心跳轮询进度（Experience #835588 模式：失败重试5次兜底）
      const taskId = submitted.id;
      let consecPollFails = 0;

      indexSyncPollRef.current = window.setInterval(async () => {
        // 心跳请求还未返回时，不再并发发起下一次请求，避免慢网络下旧响应覆盖新进度。
        if (indexSyncPollInFlightRef.current) return;
        indexSyncPollInFlightRef.current = true;
        try {
          const latest: IndexSyncTaskRead = await api.getIndexPricesSyncTask(taskId);
          consecPollFails = 0;
          setIndexSyncTask(latest);

          const isTerminal = ["done", "failed", "cancelled"].includes(latest.status);
          if (!isTerminal) {
            // 非终态：只更新动态 Progress Toast，不做别的
            message.open({
              key: toastKey,
              type: "loading",
              duration: 0,
              content: renderIndexSyncProgress(latest),
            });
            return;
          }

          // ── 到达终态：清理轮询 + 更新行状态 + 结果 Toast ──
          if (indexSyncPollRef.current != null) {
            clearInterval(indexSyncPollRef.current);
            indexSyncPollRef.current = null;
          }

          // 把结果写回到每行 last_sync_result / last_sync_error（与原逻辑一致，字段兼容）
          const resultPayload = latest.result;
          if (resultPayload?.items) {
            setIndexStatusList((prev) => prev.map((row) => {
              const it = resultPayload.items.find((i) => i.symbol === row.symbol);
              if (!it) return row;
              return {
                ...row,
                last_sync_error: it.error ?? null,
                last_sync_result: {
                  received: (it as any).received,
                  written: (it as any).written,
                  skipped: (it as any).skipped,
                  error: it.error ?? null,
                },
              };
            }));
          }

          const total = latest.total ?? resultPayload?.total ?? 0;
          const ok = latest.ok_count ?? resultPayload?.success ?? 0;
          const failed = latest.failed_count ?? resultPayload?.failed ?? 0;
          const total_written = (resultPayload?.items || []).reduce(
            (s, i) => s + ((i as any).written ?? 0), 0,
          );

          if (latest.status === "done") {
            if (failed > 0) {
              message.open({
                key: toastKey,
                type: "error",
                duration: 10,
                content: (
                  <div>
                    <Progress percent={100} status="exception" showInfo={false} style={{ marginBottom: 6 }} />
                    {`同步完成：${ok}/${total} 成功，累计写入 ${total_written} 条，失败 ${failed}。可点击每行左侧"+"展开查看错误详情。`}
                  </div>
                ),
              });
            } else {
              message.open({
                key: toastKey,
                type: "success",
                duration: 5,
                content: (
                  <div>
                    <Progress percent={100} status="success" showInfo={false} style={{ marginBottom: 6 }} />
                    {`同步完成：${ok}/${total} 个指数，累计写入 ${total_written} 条`}
                  </div>
                ),
              });
            }
          } else if (latest.status === "cancelled") {
            message.open({ key: toastKey, type: "warning", duration: 5, content: latest.message || "同步已取消" });
          } else {
            // failed
            const errText = latest.message || (latest.errors?.[0] as any)?.error || "未知错误";
            message.open({
              key: toastKey, type: "error", duration: 10,
              content: `同步失败：${errText}`,
            });
          }

          setIndexSyncRunning(false);
          await loadIndexStatus(false);
        } catch (pollErr: any) {
          // 轮询失败：连续5次才兜底停止，单/偶发网络抖动继续尝试
          consecPollFails += 1;
          if (consecPollFails >= 5) {
            if (indexSyncPollRef.current != null) {
              clearInterval(indexSyncPollRef.current);
              indexSyncPollRef.current = null;
            }
            setIndexSyncRunning(false);
            message.open({
              key: toastKey,
              type: "warning",
              duration: 8,
              content: `进度查询连续失败：${pollErr?.message ?? String(pollErr)}。任务仍可能在后台继续，稍后可点"刷新状态"查看最新结果。`,
            });
          }
        } finally {
          indexSyncPollInFlightRef.current = false;
        }
      }, 2000);
    } catch (e: any) {
      // 提交阶段就失败（还没拿到 task_id）
      setIndexSyncRunning(false);
      setIndexSyncTask(null);
      if (indexSyncPollRef.current != null) {
        clearInterval(indexSyncPollRef.current);
        indexSyncPollRef.current = null;
      }
      message.error("提交同步任务失败: " + (e?.message ?? String(e)));
      console.error(e);
    }
  };

  const showToast = useCallback((type: "success" | "error", text: string) => {
    if (!text) return;
    if (type === "success") {
      message.success(text);
      return;
    }
    message.error(text);
  }, []);

  // 将 labelKey 形式的 options 翻译为带 label 的 options（语言切换时自动更新）
  const i18nInitScopeOptions = useMemo(() => INIT_SCOPE_OPTIONS.map((o) => ({ label: t(o.labelKey), value: o.value })), []);
  const i18nHistoryDaysOptions = useMemo(() => HISTORY_DAYS_OPTIONS.map((o) => ({ label: t(o.labelKey), value: o.value })), []);
  const i18nRepairChunkOptions = useMemo(() => REPAIR_CHUNK_OPTIONS.map((o) => ({ label: t(o.labelKey), value: o.value })), []);
  const i18nSyncLimitOptions = useMemo(() => SYNC_LIMIT_OPTIONS.map((o) => ("labelKey" in o ? { label: o.labelKey === "universeSyncLimitAll" ? t("universeSyncLimitAll") : o.label!, value: o.value } : { label: o.label! + t("universeSyncLimitN"), value: o.value })), []);

  // 配置变更时持久化到 localStorage
  useEffect(() => {
    saveUniverseConfig({
      maxWorkers,
      historyDays,
      syncLimit,
      syncTab,
      initScopes,
      bfHistoryDays,
      bfScopes,
      bfSyncLimit,
      repairHistoryDays,
      repairChunkDays,
      repairScopes,
      repairSyncLimit,
    });
  }, [
    maxWorkers,
    historyDays,
    syncLimit,
    syncTab,
    initScopes,
    bfHistoryDays,
    bfScopes,
    bfSyncLimit,
    repairHistoryDays,
    repairChunkDays,
    repairScopes,
    repairSyncLimit,
  ]);

  const refreshStats = useCallback(async () => {
    try {
      const data = await api.getUniverseStats();
      setStats(data as UniverseStats);
    } catch {
      // ignore
    }
  }, []);

  const refreshTask = useCallback(async () => {
    try {
      const data = await api.getUniverseInitStatus();
      setTask(data as UniverseTask | null);
    } catch {
      // ignore
    }
  }, []);

  const refreshSmartTask = useCallback(async () => {
    try {
      const data = await api.getUniverseSmartSyncStatus();
      setSmartTask(data as UniverseTask | null);
    } catch {
      // ignore
    }
  }, []);

  const refreshIncrTask = useCallback(async () => {
    try {
      const data = await api.getUniverseIncrementalSyncStatus();
      setIncrTask(data as UniverseTask | null);
      if (data && !["running", "queued"].includes(data.status)) {
        void refreshStats();
      }
    } catch {
      // ignore
    }
  }, [refreshStats]);

  const refreshBfTask = useCallback(async () => {
    try {
      const data = await api.getUniverseBackfillStatus();
      setBfTask(data as UniverseTask | null);
    } catch {
      // ignore
    }
  }, []);

  const refreshRepairTask = useCallback(async () => {
    try {
      const data = await api.getUniverseRangeRepairStatus();
      setRepairTask(data as UniverseTask | null);
    } catch {
      // ignore
    }
  }, []);

  // 初始加载
  useEffect(() => {
    refreshStats();
    refreshTask();
    refreshSmartTask();
    refreshIncrTask();
    refreshBfTask();
    refreshRepairTask();
  }, [refreshStats, refreshTask, refreshSmartTask, refreshIncrTask, refreshBfTask, refreshRepairTask]);

  // 任务运行中时轮询
  useEffect(() => {
    if (!task || (task.status !== "running" && task.status !== "queued")) {
      return;
    }
    const timer = setInterval(() => {
      refreshTask();
      refreshStats();
    }, 5000);
    return () => clearInterval(timer);
  }, [task?.status, task?.id, refreshTask, refreshStats]);

  // 智能同步任务轮询
  useEffect(() => {
    if (!smartTask || (smartTask.status !== "running" && smartTask.status !== "queued")) {
      return;
    }
    const timer = setInterval(() => {
      refreshSmartTask();
      refreshStats();
    }, 5000);
    return () => clearInterval(timer);
  }, [smartTask?.status, smartTask?.id, refreshSmartTask, refreshStats]);

  // P2：增量同步任务轮询
  useEffect(() => {
    if (!incrTask || (incrTask.status !== "running" && incrTask.status !== "queued")) {
      return;
    }
    const statusTimer = setInterval(refreshIncrTask, 1000);
    const statsTimer = setInterval(refreshStats, 10000);
    return () => {
      clearInterval(statusTimer);
      clearInterval(statsTimer);
    };
  }, [incrTask?.status, incrTask?.id, refreshIncrTask, refreshStats]);

  // 历史回补任务轮询
  useEffect(() => {
    if (!bfTask || (bfTask.status !== "running" && bfTask.status !== "queued")) {
      return;
    }
    const timer = setInterval(() => {
      refreshBfTask();
      refreshStats();
    }, 5000);
    return () => clearInterval(timer);
  }, [bfTask?.status, bfTask?.id, refreshBfTask, refreshStats]);

  // 区间修复任务轮询
  useEffect(() => {
    if (!repairTask || (repairTask.status !== "running" && repairTask.status !== "queued")) {
      return;
    }
    const timer = setInterval(() => {
      refreshRepairTask();
      refreshStats();
    }, 5000);
    return () => clearInterval(timer);
  }, [repairTask?.status, repairTask?.id, refreshRepairTask, refreshStats]);

  const handleStart = async () => {
    if (initScopes.length === 0) {
      showToast("error", t("universeNeedInitScope"));
      return;
    }
    setStarting(true);
    try {
      // 选满4个时传 null（后端默认全部），否则传具体列表
      const scopes = initScopes.length === 4 ? null : initScopes;
      await api.startUniverseInit(maxWorkers, historyDays, scopes, syncLimit);
      await refreshTask();
      showToast("success", t("universeInitStarted"));
    } catch (e: any) {
      showToast("error", e.message || t("universeStartFailed"));
    } finally {
      setStarting(false);
    }
  };

  const handleRetry = async () => {
    if (initScopes.length === 0) {
      showToast("error", t("universeNeedInitScope"));
      return;
    }
    setStarting(true);
    try {
      const scopes = initScopes.length === 4 ? null : initScopes;
      await api.retryUniverseInit(maxWorkers, historyDays, scopes, syncLimit);
      await refreshTask();
      showToast("success", t("universeRetryStarted"));
    } catch (e: any) {
      showToast("error", e.message || t("universeRetryFailed"));
    } finally {
      setStarting(false);
    }
  };

  const handleCancel = async () => {
    setCancelling(true);
    try {
      await api.cancelUniverseInit();
      await refreshTask();
      showToast("success", t("universeCancelOk"));
    } catch (e: any) {
      showToast("error", e.message || t("universeCancelFailed"));
    } finally {
      setCancelling(false);
    }
  };

  const handleSmartStart = async () => {
    if (initScopes.length === 0) {
      showToast("error", t("universeSmartNeedScope"));
      return;
    }
    setSmartStarting(true);
    try {
      const scopes = initScopes.length === 4 ? null : initScopes;
      await api.startUniverseSmartSync(maxWorkers, historyDays, scopes, syncLimit);
      await refreshSmartTask();
      showToast("success", t("universeSmartStarted"));
    } catch (e: any) {
      showToast("error", e.message || t("universeSmartStartFailed"));
    } finally {
      setSmartStarting(false);
    }
  };

  const handleSmartCancel = async () => {
    setSmartCancelling(true);
    try {
      await api.cancelUniverseSmartSync();
      await refreshSmartTask();
      showToast("success", t("universeSmartCancelled"));
    } catch (e: any) {
      showToast("error", e.message || t("universeSmartCancelFailed"));
    } finally {
      setSmartCancelling(false);
    }
  };

  // P2：增量同步处理函数
  const handleIncrStart = async () => {
    if (initScopes.length === 0) {
      showToast("error", t("universeIncrementalNeedScope"));
      return;
    }
    setIncrStarting(true);
    try {
      const scopes = initScopes.length === 4 ? null : initScopes;
      await api.startUniverseIncrementalSync(maxWorkers, scopes);
      await refreshIncrTask();
      showToast("success", t("universeIncrementalStart"));
    } catch (e: any) {
      showToast("error", e.message || t("universeStartFailed"));
    } finally {
      setIncrStarting(false);
    }
  };

  const handleIncrCancel = async () => {
    setIncrCancelling(true);
    try {
      await api.cancelUniverseIncrementalSync();
      await refreshIncrTask();
      showToast("success", t("universeIncrCancelled"));
    } catch (e: any) {
      showToast("error", e.message || t("universeCancelFailed"));
    } finally {
      setIncrCancelling(false);
    }
  };

  const smartIsRunning = smartTask?.status === "running" || smartTask?.status === "queued";
  const isRunning = task?.status === "running" || task?.status === "queued";
  const canRetry = task?.status === "failed" || task?.status === "cancelled" || task?.status === "done";
  const incrIsRunning = incrTask?.status === "running" || incrTask?.status === "queued";

  // 历史回补处理函数
  const handleBfStart = async () => {
    if (bfScopes.length === 0) {
      showToast("error", t("universeNeedBfScope"));
      return;
    }
    setBfStarting(true);
    try {
      const scopes = bfScopes.length === 4 ? null : bfScopes;
      await api.startUniverseBackfill(maxWorkers, bfHistoryDays, scopes, bfSyncLimit);
      await refreshBfTask();
      showToast("success", t("universeBfStarted"));
    } catch (e: any) {
      showToast("error", e.message || t("universeStartFailed"));
    } finally {
      setBfStarting(false);
    }
  };

  const handleBfCancel = async () => {
    setBfCancelling(true);
    try {
      await api.cancelUniverseBackfill();
      await refreshBfTask();
      showToast("success", t("universeBfCancelled"));
    } catch (e: any) {
      showToast("error", e.message || t("universeCancelFailed"));
    } finally {
      setBfCancelling(false);
    }
  };


  const handleRepairStart = async () => {
    if (repairScopes.length === 0) {
      showToast("error", t("universeRangeRepairNeedScope"));
      return;
    }
    setRepairStarting(true);
    try {
      const scopes = repairScopes.length === 4 ? null : repairScopes;
      await api.startUniverseRangeRepair(maxWorkers, repairHistoryDays, repairChunkDays, scopes, repairSyncLimit);
      await refreshRepairTask();
      showToast("success", t("universeRangeRepairStarted"));
    } catch (e: any) {
      showToast("error", e.message || t("universeRangeRepairStartFailed"));
    } finally {
      setRepairStarting(false);
    }
  };

  const handleRepairCancel = async () => {
    setRepairCancelling(true);
    try {
      await api.cancelUniverseRangeRepair();
      await refreshRepairTask();
      showToast("success", t("universeRangeRepairCancelled"));
    } catch (e: any) {
      showToast("error", e.message || t("universeRangeRepairCancelFailed"));
    } finally {
      setRepairCancelling(false);
    }
  };
  const bfIsRunning = bfTask?.status === "running" || bfTask?.status === "queued";
  const repairIsRunning = repairTask?.status === "running" || repairTask?.status === "queued";
  // 任意同步任务运行中时，禁用其他启动按钮
  const anyRunning = smartIsRunning || isRunning || incrIsRunning || bfIsRunning || repairIsRunning;
  const incrAttempts = toSafeNumber(incrTask?.result?.attempts);
  const incrAverageFetch = toSafeNumber(incrTask?.result?.average_fetch_seconds);
  const incrAverageDatabase = toSafeNumber(incrTask?.result?.average_database_seconds);
  const backfillScopeSummaries = getBackfillSummaries(bfTask?.result);
  const backfillSummary = backfillScopeSummaries.reduce(
    (acc, item) => ({
      scopes: acc.scopes + 1,
      processed: acc.processed + (item.processed > 0 ? item.processed : item.total),
      ok: acc.ok + item.ok,
      skipped: acc.skipped + item.skipped,
      failed: acc.failed + item.failed,
    }),
    { scopes: 0, processed: 0, ok: 0, skipped: 0, failed: 0 }
  );
  const repairScopeSummaries = getRangeRepairSummaries(repairTask?.result);
  const repairSummary = repairScopeSummaries.reduce(
    (acc, item) => ({
      scopes: acc.scopes + 1,
      inserted: acc.inserted + item.inserted,
      updated: acc.updated + item.updated,
      emptyChunks: acc.emptyChunks + item.emptyChunks,
    }),
    { scopes: 0, inserted: 0, updated: 0, emptyChunks: 0 }
  );
  const syncTabMeta: Record<
    SyncTabKey,
    {
      tipKey: string;
      guideTitleKey: string;
      guideDescKey: string;
      alertType: "info" | "warning";
    }
  > = {
    daily: {
      tipKey: "universeTabDailyTip",
      guideTitleKey: "universeTabDailyTitle",
      guideDescKey: "universeTabDailyGuide",
      alertType: "info",
    },
    history: {
      tipKey: "universeTabHistoryTip",
      guideTitleKey: "universeTabHistoryTitle",
      guideDescKey: "universeTabHistoryGuide",
      alertType: "info",
    },
    advanced: {
      tipKey: "universeTabAdvancedTip",
      guideTitleKey: "universeTabAdvancedTitle",
      guideDescKey: "universeTabAdvancedGuide",
      alertType: "warning",
    },
  };
  const syncTabItems = [
    {
      key: "daily",
      label: (
        <Space size={6}>
          <ThunderboltOutlined />
          <span>{t("universeTabDaily")}</span>
          <Tag color="gold">{t("universeSmartRecommended")}</Tag>
        </Space>
      ),
    },
    {
      key: "history",
      label: (
        <Space size={6}>
          <HistoryOutlined />
          <span>{t("universeTabHistory")}</span>
          <Tag color="blue">{t("universeTabHistoryBadge")}</Tag>
        </Space>
      ),
    },
    {
      key: "advanced",
      label: (
        <Space size={6}>
          <ToolOutlined />
          <span>{t("universeTabAdvanced")}</span>
          <Tag color="orange">{t("universeTabAdvancedBadge")}</Tag>
        </Space>
      ),
    },
  ];
  const syncTabQuickGuides: Record<
    SyncTabKey,
    Array<{
      key: SyncPanelKey;
      titleKey: string;
      descKey: string;
      badgeKey: string;
      badgeColor: string;
    }>
  > = {
    daily: [
      {
        key: "incremental",
        titleKey: "universeQuickIncrementalTitle",
        descKey: "universeQuickDailyIncremental",
        badgeKey: "universeIncrementalBadge",
        badgeColor: "blue",
      },
      {
        key: "smart",
        titleKey: "universeQuickSmartTitle",
        descKey: "universeQuickDailySmart",
        badgeKey: "universeSmartRecommended",
        badgeColor: "gold",
      },
    ],
    history: [
      {
        key: "init",
        titleKey: "universeQuickInitTitle",
        descKey: "universeQuickHistoryInit",
        badgeKey: "universeInitBadge",
        badgeColor: "cyan",
      },
      {
        key: "backfill",
        titleKey: "universeQuickBackfillTitle",
        descKey: "universeQuickHistoryBackfill",
        badgeKey: "universeBackfillBadge",
        badgeColor: "blue",
      },
    ],
    advanced: [
      {
        key: "repair",
        titleKey: "universeQuickRepairTitle",
        descKey: "universeQuickAdvancedRepair",
        badgeKey: "universeRangeRepairBadge",
        badgeColor: "orange",
      },
    ],
  };
  const activeSyncTab = syncTabMeta[syncTab];
  const activeSyncGuideItems = syncTabQuickGuides[syncTab];
  const activeSyncPanel = syncPanelSelection[syncTab];

  const handleSyncPanelSelect = useCallback((panel: SyncPanelKey) => {
    setSyncPanelSelection((prev) => ({
      ...prev,
      [syncTab]: panel,
    }));
  }, [syncTab]);

  const indexProgressPercent = Math.max(0, Math.min(100, Math.round(indexSyncTask?.percent ?? 0)));
  const indexSyncActive = Boolean(indexSyncTask && ["queued", "running"].includes(indexSyncTask.status));
  const indexTaskUpdatedAt = indexSyncTask?.heartbeat_at ?? indexSyncTask?.updated_at ?? indexSyncTask?.last_progress_at;

  return (
    <div className="settings-tab-container" data-settings-content="settings-universe">
      <section className="band">
        <div className="panel">
          <div className="panel-head">
            <div>
              <p className="panel-kicker">{t("universeKicker")}</p>
              <h2>{t("universeTitle")}</h2>
            </div>
          </div>
          <p style={{ color: "var(--text-muted, #888)", marginBottom: 16 }}>{t("universeDesc")}</p>

          {/* ─── 复权口径说明 ─── */}
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 16 }}
            message={
              <Space size={10} wrap style={{ fontSize: 13 }}>
                <strong style={{ fontSize: 13 }}>复权口径说明：</strong>
                <Tag color="cyan">
                  <Space size={4}>
                    <strong>前复权</strong>
                    <Tooltip title="以最新收盘价为基准，把历史除权除息缺口（分红/送股/配股）向前逐步抹平，让历史 K 线整体向下平移。优点是最新价与真实盘面一致，看 K 线形态、计算日涨跌幅、做技术分析时一般都用前复权，也是绝大多数行情软件的默认模式。">
                      <QuestionCircleOutlined style={{ color: "var(--text-muted, #8c8c8c)" }} />
                    </Tooltip>
                  </Space>
                </Tag>
                <Tag color="blue">
                  <Space size={4}>
                    <strong>后复权</strong>
                    <Tooltip title="以上市首日的原始价格为基准，把后续历次分红派息全部向后累加到价格上（相当于假设分红立刻再买入该股）。优点是价格曲线真实反映长期总收益率走势，适合做 3 年以上的持有收益对比和回测验证。">
                      <QuestionCircleOutlined style={{ color: "var(--text-muted, #8c8c8c)" }} />
                    </Tooltip>
                  </Space>
                </Tag>
                <Tag color="orange">
                  <Space size={4}>
                    <strong>不复权</strong>
                    <Tooltip title="直接展示交易所每日原始行情，不做任何除权修正。遇到股票除权除息日，K 线图上会出现一道道明显的向下跳空缺口。适合需要精确核对当日真实成交价格、核对分红到账金额的场景，不适合直接做技术指标和涨跌幅分析。">
                      <QuestionCircleOutlined style={{ color: "var(--text-muted, #8c8c8c)" }} />
                    </Tooltip>
                  </Space>
                </Tag>
                <Space size={4}>
                  <Tag color="success" style={{ fontWeight: 600 }}>系统默认</Tag>
                  <span style={{ color: "var(--text-secondary, #555)", fontSize: 12 }}>
                    行情/因子底座统一存储为<strong>前复权</strong>口径，保证涨跌幅计算与 K 线形态一致；基准指数 <code>index_prices</code> 表默认写入<strong>后复权</strong>，用于长期收益率对比。
                  </span>
                </Space>
              </Space>
            }
            description={
              <Space direction="vertical" size={2} style={{ fontSize: 12, color: "var(--text-muted, #666)" }}>
                <Space wrap size={16}>
                  <span>• <strong>前复权</strong>：以现在股价为基准，往前把历史除权缺口抹平。看 K 线、看涨跌幅一般用前复权。</span>
                  <span>• <strong>后复权</strong>：以最早历史价格为基准，把后面分红全部算回去，看真实长期总收益用。</span>
                  <span>• <strong>不复权</strong>：原始行情，K 线会看到一道道向下跳空的除权缺口。</span>
                </Space>
              </Space>
            }
          />

          {/* 数据健康度 */}
          <Card title={t("universeStatsTitle")} size="small" style={{ marginBottom: 16 }} loading={loading && !stats}>
            {stats ? (
              <>
                {stats.is_empty && (
                  <Alert
                    message={t("universeEmpty")}
                    type="info"
                    showIcon
                    style={{ marginBottom: 12 }}
                  />
                )}
                <Row gutter={16}>
                  <Col span={6}>
                    <Statistic title={t("universeTotalSymbols")} value={stats.total_symbols} />
                  </Col>
                  <Col span={6}>
                    <Statistic title={t("universeSyncedSymbols")} value={stats.synced_symbols} valueStyle={{ color: "#52c41a" }} />
                  </Col>
                  <Col span={6}>
                    <Statistic title={t("universeFailedSymbols")} value={stats.failed_symbols} valueStyle={{ color: stats.failed_symbols > 0 ? "#ff4d4f" : undefined }} />
                  </Col>
                  <Col span={6}>
                    <Statistic title={t("universeTotalBars")} value={stats.total_bars} />
                  </Col>
                </Row>

                {/* 按 scope 覆盖率 */}
                {stats.by_scope && Object.keys(stats.by_scope).length > 0 && (
                  <div style={{ marginTop: 16 }}>
                    <p style={{ fontWeight: 500, marginBottom: 8, color: "var(--text-primary, #333)" }}>
                      {t("universeHealthByScope")}
                    </p>
                    <Row gutter={[8, 8]}>
                      {Object.entries(stats.by_scope).map(([scope, data]) => (
                        <Col key={scope} xs={12} sm={6}>
                          <div style={{ padding: "8px 12px", background: "var(--bg-elevated, #fafafa)", borderRadius: 6, border: "1px solid var(--border-color, #f0f0f0)" }}>
                            <div style={{ fontSize: 12, color: "var(--text-muted, #888)" }}>{scopeLabel(scope)}</div>
                            <div style={{ fontSize: 18, fontWeight: 600, marginTop: 2 }}>
                              {data.synced}<span style={{ fontSize: 12, color: "var(--text-muted, #888)", fontWeight: 400 }}> / {data.total}</span>
                            </div>
                            <Progress
                              percent={data.coverage}
                              size="small"
                              status={data.coverage >= 100 ? "success" : data.coverage >= 50 ? "active" : "exception"}
                              format={(p) => `${p}%`}
                              style={{ marginTop: 4, marginBottom: 0 }}
                            />
                          </div>
                        </Col>
                      ))}
                    </Row>
                  </div>
                )}

                {/* 数据新鲜度分布 */}
                {stats.freshness && (
                  <div style={{ marginTop: 16 }}>
                    <p style={{ fontWeight: 500, marginBottom: 8, color: "var(--text-primary, #333)" }}>
                      {t("universeHealthFreshness")}
                      <Tooltip title={t("universeHealthFreshnessTip")}>
                        <QuestionCircleOutlined style={{ marginLeft: 6, color: "var(--text-muted, #bfbfbf)", fontSize: 12 }} />
                      </Tooltip>
                    </p>
                    <Row gutter={[8, 8]}>
                      {[
                        { key: "today", label: t("universeFreshnessToday"), color: "#52c41a" },
                        { key: "1_3_days", label: t("universeFreshness1_3"), color: "#1890ff" },
                        { key: "3_7_days", label: t("universeFreshness3_7"), color: "#faad14" },
                        { key: "over_7_days", label: t("universeFreshnessOver7"), color: "#ff4d4f" },
                        { key: "no_data", label: t("universeFreshnessNoData"), color: "#8c8c8c" },
                      ].map(({ key, label, color }) => (
                        <Col key={key} xs={12} sm={8} md={4} xl={4}>
                          <div style={{ padding: "6px 10px", background: "var(--bg-elevated, #fafafa)", borderRadius: 6, border: "1px solid var(--border-color, #f0f0f0)" }}>
                            <div style={{ fontSize: 12, color: "var(--text-muted, #888)" }}>{label}</div>
                            <div style={{ fontSize: 18, fontWeight: 600, marginTop: 2, color }}>{stats.freshness![key as keyof typeof stats.freshness]}</div>
                          </div>
                        </Col>
                      ))}
                    </Row>
                  </div>
                )}

                {/* 按板块统计（仅 A股股票有 board） */}
                {stats.by_board && Object.keys(stats.by_board).length > 0 && (
                  <div style={{ marginTop: 16 }}>
                    <p style={{ fontWeight: 500, marginBottom: 8, color: "var(--text-primary, #333)" }}>
                      {t("universeHealthByBoard")}
                      <Tooltip title={t("universeHealthByBoardTip")}>
                        <QuestionCircleOutlined style={{ marginLeft: 6, color: "var(--text-muted, #bfbfbf)", fontSize: 12 }} />
                      </Tooltip>
                    </p>
                    <Row gutter={[8, 8]}>
                      {Object.entries(stats.by_board).map(([board, data]) => (
                        <Col key={board} xs={12} sm={6}>
                          <div style={{ padding: "8px 12px", background: "var(--bg-elevated, #fafafa)", borderRadius: 6, border: "1px solid var(--border-color, #f0f0f0)" }}>
                            <div style={{ fontSize: 12, color: "var(--text-muted, #888)" }}>{boardLabel(board)}</div>
                            <div style={{ fontSize: 18, fontWeight: 600, marginTop: 2 }}>
                              {data.synced}<span style={{ fontSize: 12, color: "var(--text-muted, #888)", fontWeight: 400 }}> / {data.total}</span>
                            </div>
                            <Progress
                              percent={data.coverage}
                              size="small"
                              status={data.coverage >= 100 ? "success" : data.coverage >= 50 ? "active" : "exception"}
                              format={(p) => `${p}%`}
                              style={{ marginTop: 4, marginBottom: 0 }}
                            />
                          </div>
                        </Col>
                      ))}
                    </Row>
                  </div>
                )}

                {stats.by_type && Object.keys(stats.by_type).length > 0 && (
                  <Row gutter={16} style={{ marginTop: 12 }}>
                    {Object.entries(stats.by_type).map(([type, data]) => (
                      <Col key={type} span={8}>
                        <Statistic
                          title={type === "stock" ? t("universeTypeStock") : type === "etf" ? t("universeTypeEtf") : type}
                          value={data.synced}
                          suffix={`/ ${data.total}`}
                        />
                      </Col>
                    ))}
                  </Row>
                )}
                {stats.latest_synced_at && (
                  <p style={{ marginTop: 12, color: "var(--text-muted, #888)", fontSize: 12 }}>
                    {t("universeLatestSync")}：{new Date(stats.latest_synced_at).toLocaleString()}
                  </p>
                )}
              </>
            ) : null}
          </Card>

          {/* ─── 基准指数数据同步（回测基准曲线 + 板块同步） ─── */}
          <Card
            title={
              <Space wrap>
                <LineChartOutlined style={{ color: "#0f766e" }} />
                <span>基准指数 / 板块同步</span>
                <Tag color="blue">回测基准曲线数据源</Tag>
                <Tooltip title="写入 index_prices 表时默认采用「后复权」口径：以上市首日价格为基准，把后续所有分红派息、拆股配股全部累加到价格上。这样画出来的基准曲线才能反映持有指数长期不动的真实总收益率，和前复权的个股 K 线同屏比较时要注意两者口径不同。">
                  <QuestionCircleOutlined style={{ color: "var(--text-muted, #8c8c8c)", fontSize: 12 }} />
                </Tooltip>
                {customIndices.length > 0 ? (
                  <Tag color="purple">{`含 ${customIndices.length} 个自定义`}</Tag>
                ) : null}
              </Space>
            }
            size="small"
            style={{ marginBottom: 16 }}
            extra={
              <Space size="small" wrap>
                <span style={{ fontSize: 12, color: "var(--text-muted, #888)" }}>历史范围:</span>
                <Select
                  size="small"
                  value={indexHistoryDays}
                  onChange={(v) => setIndexHistoryDays(v)}
                  disabled={indexSyncRunning}
                  style={{ width: 130 }}
                  options={INDEX_HISTORY_DAYS_OPTIONS}
                />
                <Button
                  size="small"
                  icon={<ReloadOutlined />}
                  onClick={() => loadIndexStatus(false)}
                  loading={indexStatusLoading}
                  disabled={indexSyncRunning}
                >
                  刷新状态
                </Button>
                <Button
                  size="small"
                  icon={<CloudSyncOutlined />}
                  onClick={() => handleSyncIndexPrices(false)}
                  loading={indexSyncRunning}
                  type="primary"
                >
                  同步选中
                </Button>
                <Button
                  size="small"
                  icon={<PlayCircleOutlined />}
                  onClick={() => handleSyncIndexPrices(true)}
                  loading={indexSyncRunning}
                >
                  一键同步全部5大基准
                </Button>
              </Space>
            }
          >
            <Alert
              type="info"
              showIcon
              style={{ marginBottom: 12 }}
              message={'板块/基准指数日线数据 (index_prices) 是回测中心基准曲线的数据源，默认按「后复权」写入。若基准线呈直线，点击「一键同步」或先添加自定义指数再同步。'}
              description={'支持 Akshare 能解析的全部指数：默认 5 大基准（沪深300、中证500、创业板指、上证50、科创50）+ 自定义（如上证指数 000001 / 上证180 000010 / 中小板指 399005 / 深证成指 399001 等）。数据源：东财 / 新浪 / 腾讯 三源容灾 fallback。复权口径：以上市首日价格为基准向后累加分红，真实反映长期总收益率。'}
            />

            {indexSyncTask ? (
              <div
                data-testid="index-sync-total-progress"
                style={{
                  marginBottom: 12,
                  padding: "12px 14px",
                  border: `1px solid ${indexSyncActive ? "#91caff" : "var(--border-color, #d9d9d9)"}`,
                  borderRadius: 8,
                  background: indexSyncActive ? "#f0f7ff" : "var(--settings-block, #fafafa)",
                }}
              >
                <Space direction="vertical" size={6} style={{ width: "100%" }}>
                  <Space wrap style={{ justifyContent: "space-between", width: "100%" }}>
                    <Space size={8} wrap>
                      <strong>同步总进度</strong>
                      <Tag color={indexSyncTask.status === "failed" ? "error" : indexSyncTask.status === "cancelled" ? "warning" : indexSyncTask.status === "done" ? "success" : "processing"}>
                        {indexSyncTask.status === "queued" ? "排队中" : indexSyncTask.status === "running" ? "同步中" : indexSyncTask.status === "done" ? "已完成" : indexSyncTask.status === "failed" ? "同步失败" : "已取消"}
                      </Tag>
                      {indexSyncTask.current_item ? <Tag>{`当前：${indexSyncTask.current_item}`}</Tag> : null}
                    </Space>
                    {indexTaskUpdatedAt ? (
                      <span style={{ color: "var(--text-muted, #888)", fontSize: 12 }}>
                        心跳更新：{new Date(indexTaskUpdatedAt).toLocaleTimeString("zh-CN", { hour12: false })}
                      </span>
                    ) : null}
                  </Space>
                  <Progress
                    percent={indexProgressPercent}
                    status={indexSyncTask.status === "failed" ? "exception" : indexSyncTask.status === "done" ? "success" : "active"}
                    strokeColor={indexSyncTask.status === "failed" ? undefined : "#0f766e"}
                  />
                  <Space wrap size={[16, 4]} style={{ color: "var(--text-muted, #666)", fontSize: 12 }}>
                    <span>已处理 {indexSyncTask.processed ?? 0} / {indexSyncTask.total || "—"} 个指数</span>
                    <span style={{ color: "#16a34a" }}>成功 {indexSyncTask.ok_count ?? 0}</span>
                    {(indexSyncTask.failed_count ?? 0) > 0 ? <span style={{ color: "#dc2626" }}>失败 {indexSyncTask.failed_count}</span> : null}
                    <span>{indexSyncTask.current_step_description || indexSyncTask.message || "正在等待后台任务更新…"}</span>
                  </Space>
                </Space>
              </div>
            ) : null}

            {/* 添加自定义指数行 */}
            <div
              style={{
                marginBottom: 12,
                padding: 12,
                border: "1px dashed var(--border-color, #d9d9d9)",
                borderRadius: 6,
                background: "var(--settings-block, #fafafa)",
                display: "flex",
                flexWrap: "wrap",
                gap: 8,
                alignItems: "center",
              }}
            >
              <strong style={{ fontSize: 12, color: "var(--text-muted, #666)" }}>＋ 添加自定义指数：</strong>
              <Input
                size="small"
                value={addSymbol}
                onChange={(e) => setAddSymbol(e.target.value)}
                onPressEnter={addCustomIndex}
                placeholder="指数代码，如 000001 / 399001 / 000010"
                style={{ width: 200 }}
                disabled={indexSyncRunning || addLoading}
              />
              <Input
                size="small"
                value={addName}
                onChange={(e) => setAddName(e.target.value)}
                onPressEnter={addCustomIndex}
                placeholder="名称（可选，默认使用代码）"
                style={{ width: 180 }}
                disabled={indexSyncRunning || addLoading}
              />
              <Button
                size="small"
                type="primary"
                icon={<PlusOutlined />}
                onClick={addCustomIndex}
                loading={addLoading}
                disabled={indexSyncRunning}
              >
                添加到列表
              </Button>
              <Tooltip title={"常见指数参考：上证指数(000001)、深证成指(399001)、上证180(000010)、沪深300(000300，默认已有)、中证500(000905，默认已有)、中小板指(399005)、创业板指(399006，默认已有)、上证50(000016，默认已有)、深证100(399330)、中证1000(000852)、科创50(000688，默认已有)"}>
                <QuestionCircleOutlined style={{ color: "var(--text-muted, #888)" }} />
              </Tooltip>
            </div>

            <Table<IndexStatusItem>
              className="benchmark-index-table"
              size="small"
              rowKey="symbol"
              loading={indexStatusLoading}
              dataSource={indexStatusList}
              pagination={false}
              scroll={{ x: 860 }}
              rowSelection={{
                columnWidth: 50,
                selectedRowKeys: indexSelectedSymbols,
                onChange: (keys) => setIndexSelectedSymbols(keys as string[]),
                getCheckboxProps: () => ({ disabled: indexSyncRunning }),
                checkStrictly: false,
              }}
              expandable={{
                expandedRowKeys: indexStatusList.filter((r) => r.last_sync_error != null).map((r) => r.symbol),
                expandedRowRender: (r) => {
                  const res = r.last_sync_result;
                  const err = r.last_sync_error;
                  return (
                    <div style={{ padding: "4px 20px" }}>
                      {res ? (
                        <Row gutter={16}>
                          <Col><Statistic title="收到" value={res.received ?? 0} /></Col>
                          <Col><Statistic title="写入" value={res.written ?? 0} valueStyle={{ color: "#16a34a" }} /></Col>
                          <Col><Statistic title="跳过" value={res.skipped ?? 0} /></Col>
                        </Row>
                      ) : null}
                      {err ? (
                        <Alert
                          style={{ marginTop: 8 }}
                          type="error"
                          showIcon
                          message="同步错误详情"
                          description={
                            <pre style={{
                              whiteSpace: "pre-wrap",
                              wordBreak: "break-word",
                              margin: 0,
                              fontSize: 12,
                              color: "#7f1d1d",
                              background: "#fff1f2",
                              padding: 8,
                              borderRadius: 4,
                            }}>{err}</pre>
                          }
                        />
                      ) : null}
                    </div>
                  );
                },
              }}
              columns={[
                {
                  title: "指数", dataIndex: "name", key: "name", width: 150, fixed: "left" as const,
                  render: (_, r) => (
                    <Space direction="vertical" size={0} style={{ minWidth: 120 }}>
                      <Space size={4} wrap>
                        <strong>{r.name}</strong>
                        {r.is_custom ? (
                          <Tag color="purple" style={{ margin: 0 }}>自定义</Tag>
                        ) : (
                          <Tag color="geekblue" style={{ margin: 0 }}>默认</Tag>
                        )}
                      </Space>
                      <span style={{ fontSize: 11, color: "var(--text-muted, #888)", fontFamily: "monospace" }}>{r.symbol}</span>
                    </Space>
                  ),
                },
                {
                  title: "状态", dataIndex: "bar_count", key: "status", width: 130,
                  render: (_, r) => {
                    const result = r.last_sync_result;
                    const resultTag = (() => {
                      if (result?.error != null) return <Tag color="red" style={{ marginLeft: 0 }}>同步失败</Tag>;
                      if (result && (result.written ?? 0) > 0) return <Tag color="green" style={{ marginLeft: 0 }}>已写入{result.written}</Tag>;
                      if (result && (result.received ?? 0) > 0 && (result.written ?? 0) === 0) return <Tag color="blue" style={{ marginLeft: 0 }}>已跳过{result.skipped}</Tag>;
                      return null;
                    })();
                    let tag: any;
                    if ((r.bar_count ?? 0) === 0) {
                      tag = <Tag color="red">无数据，需同步</Tag>;
                    } else if ((r.linearity_dev_pct ?? 0) < 0.1) {
                      tag = <Tag color="orange">疑似直线，需同步</Tag>;
                    } else if ((r.freshness_days ?? 9999) > 5) {
                      tag = <Tag color="gold">有数据，但偏旧</Tag>;
                    } else {
                      tag = <Tag color="green">已就绪</Tag>;
                    }
                    return (
                      <Space direction="vertical" size={2} style={{ minWidth: 120 }}>
                        {tag}
                        {resultTag}
                      </Space>
                    );
                  },
                },
                {
                  title: "K线条数", dataIndex: "bar_count", key: "count", width: 80,
                  render: (v: number) => v?.toLocaleString?.() ?? v,
                  align: "right" as const,
                },
                {
                  title: "覆盖区间", key: "range", width: 210,
                  render: (_, r) => (
                    <Space direction="vertical" size={0} style={{ minWidth: 190 }}>
                      <span style={{ fontSize: 12 }}>{r.first_date ?? "—"} 起</span>
                      <span style={{ fontSize: 12, color: "var(--text-muted, #888)" }}>
                        {r.last_date ?? "—"} 止
                        {r.freshness_days != null ? (
                          <span style={{ marginLeft: 6 }}>
                            ({r.freshness_days}d前)
                          </span>
                        ) : null}
                      </span>
                    </Space>
                  ),
                },
                {
                  title: (
                    <Space size={4}>
                      <span>线性偏离度</span>
                      <Tooltip
                        title={
                          <div style={{ maxWidth: 280, fontSize: 12, lineHeight: 1.7 }}>
                            <div style={{ fontWeight: 600, marginBottom: 4 }}>用途：检测基准曲线是否为「假直线」</div>
                            算法：取最近K线首尾两点画一条理想直线，求每天收盘价相对这条直线的最大偏离百分比。
                            <div style={{ marginTop: 6, fontWeight: 600 }}>颜色阈值：</div>
                            <div><span style={{ color: "var(--color-danger, #dc2626)" }}>● 红色 ＜ 0.1%</span>：疑似直线 → 数据源静默失败/全值填充，回测基准会失真，<strong>必须重新同步</strong></div>
                            <div><span style={{ color: "var(--color-warning, #d97706)" }}>● 橙色 0.1%~1%</span>：波动偏弱 → 建议检查同步是否完整</div>
                            <div><span style={{ color: "var(--color-success, #16a34a)" }}>● 绿色 ≥ 1%</span>：有真实波动，数据健康（宽基通常 20%~40%，高波动成长指数可达 50%+）</div>
                            <div style={{ marginTop: 6, color: "var(--text-muted, #999)" }}>※ 首次进入本页时，＜ 0.1% 的指数会被自动勾选为待同步。</div>
                          </div>
                        }
                      >
                        <QuestionCircleOutlined style={{ color: "var(--text-muted, #8c8c8c)", fontSize: 12 }} />
                      </Tooltip>
                    </Space>
                  ),
                  key: "dev", width: 110, align: "right" as const,
                  render: (_, r) => {
                    const pct = r.linearity_dev_pct;
                    if (pct == null) return <span style={{ color: "var(--text-muted, #999)" }}>—</span>;
                    const color = pct >= 1 ? "var(--color-success, #16a34a)" : (pct >= 0.1 ? "var(--color-warning, #d97706)" : "var(--color-danger, #dc2626)");
                    return (
                      <span style={{ color, fontWeight: 600 }}>{pct.toFixed(2)}%</span>
                    );
                  },
                },
                {
                  title: "操作", key: "action", width: 150, fixed: "right" as const,
                  render: (_, r) => (
                    <Space size={2}>
                      <Button
                        size="small"
                        icon={<CloudSyncOutlined />}
                        onClick={() => {
                          setIndexSelectedSymbols([r.symbol]);
                          void handleSyncIndexPrices(false, [r.symbol]);
                        }}
                        loading={indexSyncRunning}
                        disabled={indexSyncRunning}
                      >
                        同步
                      </Button>
                      {r.is_custom ? (
                        <Popconfirm
                          title={`确定移除自定义指数 ${r.name} (${r.symbol})？`}
                          description={"只会从列表移除，数据库中已同步的数据会保留（下次再加回来仍可见）"}
                          onConfirm={() => removeCustomIndex(r.symbol)}
                          okText="移除"
                          cancelText="取消"
                        >
                          <Button size="small" danger icon={<CloseCircleOutlined />}>
                            移除
                          </Button>
                        </Popconfirm>
                      ) : null}
                    </Space>
                  ),
                },
              ]}
            />
          </Card>

          {/* 智能同步任务 */}
          <Tabs
            size="small"
            activeKey={syncTab}
            onChange={(key) => setSyncTab(key as SyncTabKey)}
            items={syncTabItems}
            style={{ marginBottom: 12 }}
          />
          <Alert
            message={t(activeSyncTab.guideTitleKey)}
            description={
              <div>
                <div style={{ marginBottom: 4 }}>{t(activeSyncTab.guideDescKey)}</div>
                <div style={{ fontSize: 12, color: "var(--text-muted, #888)" }}>{t(activeSyncTab.tipKey)}</div>
              </div>
            }
            type={activeSyncTab.alertType}
            showIcon
            style={{ marginBottom: 12 }}
          />
          <div style={{ marginBottom: 8, fontSize: 12, color: "var(--text-muted, #888)" }}>
            {t("universeViewingNow")}<strong style={{ color: "var(--text-primary, #333)", marginLeft: 4 }}>{t(activeSyncGuideItems.find((item) => item.key === activeSyncPanel)?.titleKey || activeSyncGuideItems[0].titleKey)}</strong>
          </div>
          <Row gutter={[12, 12]} style={{ marginBottom: 16 }}>
            {activeSyncGuideItems.map((item) => {
              const isActive = activeSyncPanel === item.key;
              return (
                <Col key={item.key} xs={24} md={activeSyncGuideItems.length === 1 ? 24 : 12}>
                  <div
                    role="button"
                    tabIndex={0}
                    aria-pressed={isActive}
                    onClick={() => handleSyncPanelSelect(item.key)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault();
                        handleSyncPanelSelect(item.key);
                      }
                    }}
                    style={{
                      padding: "12px 14px",
                      background: isActive ? "rgba(15, 118, 110, 0.08)" : "var(--bg-elevated, #fafafa)",
                      border: isActive ? "1px solid #0f766e" : "1px solid var(--border-color, #f0f0f0)",
                      borderRadius: 8,
                      height: "100%",
                      cursor: "pointer",
                      boxShadow: isActive ? "0 0 0 2px rgba(15, 118, 110, 0.12)" : "none",
                      transition: "all 0.2s ease",
                    }}
                  >
                    <Space size={8} wrap style={{ marginBottom: 6 }}>
                      <strong>{t(item.titleKey)}</strong>
                      <Tag color={item.badgeColor}>{t(item.badgeKey)}</Tag>
                      {isActive ? <Tag color="green">{t("universeCurrentSelection")}</Tag> : null}
                    </Space>
                    <div style={{ fontSize: 12, color: "var(--text-muted, #888)" }}>{t(item.descKey)}</div>
                  </div>
                </Col>
              );
            })}
          </Row>

          {syncTab === "daily" && (
            <>
          {activeSyncPanel === "smart" && (
          <Card
            title={
              <Space>
                <span>{t("universeSmartTitle")}</span>
                <Tag color="gold">{t("universeSmartRecommended")}</Tag>
                {smartTask ? statusTag(smartTask.status) : null}
              </Space>
            }
            size="small"
            style={{ marginBottom: 16 }}
          >
            {smartTask ? (
              <>
                <div style={{ marginBottom: 12 }}>
                  <Progress
                    percent={Math.round(smartTask.percent)}
                    status={
                      smartTask.status === "running" ? "active" :
                      smartTask.status === "done" ? "success" :
                      smartTask.status === "failed" ? "exception" :
                      smartTask.status === "cancelled" ? "exception" : "normal"
                    }
                  />
                </div>
                <p style={{ marginBottom: 8 }}>
                  <strong>{t("universeSmartStage")}</strong>
                  {smartTask.message ? ` — ${smartTask.message}` : ""}
                </p>
                {smartTask.total > 0 ? (
                  <p style={{ color: "var(--text-muted, #888)", fontSize: 12, marginBottom: 8 }}>
                    {t("universeProcessed")} {smartTask.processed} / {smartTask.total}，{t("universeSuccess")} {smartTask.ok_count}，{t("universeFail")} {smartTask.failed_count}
                  </p>
                ) : (
                  smartTask.status === "running" && (
                    <p style={{ color: "var(--text-muted, #888)", fontSize: 12, marginBottom: 8 }}>
                      {t("universeSmartPreparing")}
                    </p>
                  )
                )}
                {smartTask.status === "done" && smartTask.result && (
                  <div style={{ color: "var(--text-muted, #888)", fontSize: 12, marginBottom: 8 }}>
                    {Object.entries(smartTask.result).map(([key, val]) => {
                      const v = val as Record<string, number>;
                      if (typeof v !== "object" || v === null) return null;
                      if (key.endsWith("_universe")) {
                        const scope = key.replace(/_universe$/, "");
                        return (
                          <div key={key} style={{ marginBottom: 2 }}>
                            {t("universeResultList")} {scopeLabel(scope)}：{t("universeResultSeen")} {v.seen ?? 0}，{t("universeResultCreated")} {v.created ?? 0}
                          </div>
                        );
                      }
                      if (key.endsWith("_init")) {
                        const scope = key.replace(/_init$/, "");
                        return (
                          <div key={key} style={{ marginBottom: 2 }}>
                            {t("universeResultInit")} {scopeLabel(scope)}：{t("universeResultToSync")} {v.total ?? 0}，{t("universeSuccess")} {v.ok ?? 0}，{t("universeFail")} {v.failed ?? 0}
                          </div>
                        );
                      }
                      if (key.endsWith("_backfill")) {
                        const scope = key.replace(/_backfill$/, "");
                        return (
                          <div key={key} style={{ marginBottom: 2 }}>
                            {t("universeResultBackfill")} {scopeLabel(scope)}：{t("universeResultToBackfill")} {v.total ?? 0}，{t("universeSuccess")} {v.ok ?? 0}，{t("universeFail")} {v.failed ?? 0}
                          </div>
                        );
                      }
                      if (key === "incremental") {
                        return (
                          <div key={key} style={{ marginBottom: 2 }}>
                            {t("universeResultIncr")}：{t("universeResultToProcess")} {v.total ?? 0}，{t("universeResultUpdated")} {v.ok ?? 0}，{t("universeResultUptodate")} {v.uptodate ?? 0}，{t("universeFail")} {v.failed ?? 0}
                          </div>
                        );
                      }
                      return null;
                    })}
                  </div>
                )}
                {smartTask.errors && smartTask.errors.length > 0 && (
                  <Alert
                    message={t("universeRecentErrors").replace("{n}", String(smartTask.errors.length))}
                    description={smartTask.errors.slice(-3).map((e, i) => (
                      <div key={i} style={{ fontSize: 12, color: "#ff4d4f" }}>
                        {String(e.stage || "")} {String(e.error || JSON.stringify(e))}
                      </div>
                    ))}
                    type="warning"
                    showIcon
                    style={{ marginBottom: 12 }}
                  />
                )}
              </>
            ) : (
              <p style={{ color: "var(--text-muted, #888)" }}>{t("universeSmartNoTask")}</p>
            )}

            <div style={{ marginTop: 16, borderTop: "1px solid var(--border-color, #f0f0f0)", paddingTop: 16 }}>
              <Space direction="vertical" style={{ width: "100%" }} size="middle">
                <Space wrap>
                  <span>{t("universeWorkers")}：</span>
                  <InputNumber
                    min={1}
                    max={8}
                    value={maxWorkers}
                    onChange={(v) => setMaxWorkers(v ?? 5)}
                    disabled={anyRunning}
                    style={{ width: 80 }}
                  />
                  <span>{t("universeHistoryDays")}：</span>
                  <Select
                    value={historyDays}
                    onChange={(v) => setHistoryDays(v)}
                    options={i18nHistoryDaysOptions}
                    disabled={anyRunning}
                    style={{ width: 120 }}
                  />
                </Space>
                <Space wrap align="center">
                  <span>
                    {t("universeSyncLimit")}：
                    <Tooltip title={t("universeSyncLimitHint")}>
                      <QuestionCircleOutlined style={{ marginLeft: 4, color: "var(--text-muted, #888)" }} />
                    </Tooltip>
                  </span>
                  <Select
                    value={syncLimit}
                    onChange={(v) => setSyncLimit(v)}
                    options={i18nSyncLimitOptions}
                    disabled={anyRunning}
                    style={{ width: 140 }}
                  />
                </Space>
                <Space wrap align="center">
                  <span>
                    {t("universeInitScopes")}：
                    <Tooltip title={t("universeSmartScopesHint")}>
                      <QuestionCircleOutlined style={{ marginLeft: 4, color: "var(--text-muted, #888)" }} />
                    </Tooltip>
                  </span>
                  <Checkbox.Group
                    options={i18nInitScopeOptions}
                    value={initScopes}
                    onChange={(values) => setInitScopes(values as string[])}
                    disabled={anyRunning}
                  />
                </Space>
                <Space wrap>
                  {!smartIsRunning && (
                    <Button
                      type="primary"
                      icon={<PlayCircleOutlined />}
                      onClick={handleSmartStart}
                      loading={smartStarting}
                      disabled={anyRunning || initScopes.length === 0}
                    >
                      {t("universeSmartStart")}
                    </Button>
                  )}
                  {smartIsRunning && (
                    <Button
                      danger
                      icon={<StopOutlined />}
                      onClick={handleSmartCancel}
                      loading={smartCancelling}
                    >
                      {smartCancelling ? t("universeCanceling") : t("universeSmartCancel")}
                    </Button>
                  )}
                  <Tooltip title={t("universeSmartTip")}>
                    <QuestionCircleOutlined style={{ color: "var(--text-muted, #999)" }} />
                  </Tooltip>
                </Space>
              </Space>
            </div>
          </Card>
          )}

          {activeSyncPanel === "incremental" && (
          <Card
            title={
              <Space>
                <span>{t("universeIncrementalTitle")}</span>
                <Tag color="blue">{t("universeIncrementalBadge")}</Tag>
                {incrTask ? statusTag(incrTask.status) : null}
              </Space>
            }
            extra={
              incrIsRunning ? (
                <Button
                  danger
                  icon={<StopOutlined />}
                  onClick={handleIncrCancel}
                  loading={incrCancelling}
                >
                  {incrCancelling ? t("universeCanceling") : t("universeIncrementalCancel")}
                </Button>
              ) : (
                <Button
                  type="primary"
                  icon={<PlayCircleOutlined />}
                  onClick={handleIncrStart}
                  loading={incrStarting}
                  disabled={anyRunning || initScopes.length === 0}
                >
                  {t("universeIncrementalStart")}
                </Button>
              )
            }
            size="small"
            style={{ marginBottom: 16 }}
          >
            {incrTask ? (
              <>
                <div style={{ marginBottom: 12 }}>
                  <Progress
                    percent={Math.round(incrTask.percent)}
                    status={
                      incrTask.status === "running" ? "active" :
                      incrTask.status === "done" ? "success" :
                      incrTask.status === "failed" ? "exception" :
                      incrTask.status === "cancelled" ? "exception" : "normal"
                    }
                  />
                </div>
                <p style={{ marginBottom: 8 }}>
                  <strong>{t("universeIncrementalStageSync")}</strong>
                  {incrTask.message ? ` — ${incrTask.message}` : ""}
                </p>
                {incrTask.total > 0 && (
                  <p style={{ color: "var(--text-muted, #888)", fontSize: 12, marginBottom: 8 }}>
                    {t("universeProcessed")} {incrTask.processed} / {incrTask.total}，{t("universeSuccess")} {incrTask.ok_count}，{t("universeFail")} {incrTask.failed_count}
                  </p>
                )}
                {incrAttempts > 0 && (
                  <p style={{ color: "var(--text-muted, #888)", fontSize: 12, marginBottom: 8 }}>
                    {t("universeIncrementalTimingBreakdown")
                      .replace("{fetch}", incrAverageFetch.toFixed(2))
                      .replace("{database}", incrAverageDatabase.toFixed(2))}
                  </p>
                )}
                {incrTask.errors && incrTask.errors.length > 0 && (
                  <Alert
                    message={t("universeRecentErrors").replace("{n}", String(incrTask.errors.length))}
                    description={incrTask.errors.slice(-3).map((e, i) => (
                      <div key={i} style={{ fontSize: 12, color: "#ff4d4f" }}>
                        {String(e.stage || "")} {getUniverseTaskErrorText(e)}
                      </div>
                    ))}
                    type="warning"
                    showIcon
                    style={{ marginBottom: 12 }}
                  />
                )}
              </>
            ) : (
              <p style={{ color: "var(--text-muted, #888)" }}>{t("universeIncrementalNoTask")}</p>
            )}

            {/* 控制区 */}
            <div style={{ marginTop: 16, borderTop: "1px solid var(--border-color, #f0f0f0)", paddingTop: 16 }}>
              <Space direction="vertical" style={{ width: "100%" }} size="middle">
                <Space wrap>
                  <span>
                    {t("universeInitScopes")}：
                    <Tooltip title={t("universeIncrementalScopesHint")}>
                      <QuestionCircleOutlined style={{ marginLeft: 4, color: "var(--text-muted, #999)" }} />
                    </Tooltip>
                  </span>
                  <Checkbox.Group
                    options={i18nInitScopeOptions}
                    value={initScopes}
                    onChange={(values) => setInitScopes(values as string[])}
                    disabled={anyRunning}
                  />
                </Space>
                <Space wrap>
                  <span>{t("universeWorkers")}：</span>
                  <InputNumber
                    min={1}
                    max={8}
                    value={maxWorkers}
                    onChange={(v) => setMaxWorkers(v ?? 5)}
                    disabled={anyRunning}
                    style={{ width: 80 }}
                  />
                  <Tooltip title={t("universeIncrementalTip")}>
                    <QuestionCircleOutlined style={{ color: "var(--text-muted, #999)" }} />
                  </Tooltip>
                </Space>
              </Space>
            </div>
          </Card>
          )}
            </>
          )}

          {syncTab === "history" && (
            <>
          <Alert
            message={t("universeHistoryFlowTitle")}
            description={t("universeHistoryFlowTip")}
            type="info"
            showIcon
            style={{ marginBottom: 16 }}
          />
          {activeSyncPanel === "init" && (
          <Card
            title={
              <Space>
                <span>{t("universeTaskTitle")}</span>
                <Tag color="cyan">{t("universeHistoryPath1Badge")}</Tag>
                <Tag color="cyan">{t("universeInitBadge")}</Tag>
                {task ? statusTag(task.status) : null}
              </Space>
            }
            size="small"
            style={{ marginBottom: 16 }}
          >
            {task ? (
              <>
                <div style={{ marginBottom: 12 }}>
                  <Progress
                    percent={Math.round(task.percent)}
                    status={
                      task.status === "running" ? "active" :
                      task.status === "done" ? "success" :
                      task.status === "failed" ? "exception" :
                      task.status === "cancelled" ? "exception" : "normal"
                    }
                  />
                </div>
                <p style={{ marginBottom: 8 }}>
                  <strong>{stageLabel(task.stage)}</strong>
                  {task.message ? ` — ${task.message}` : ""}
                </p>
                {task.total > 0 && (
                  <p style={{ color: "var(--text-muted, #888)", fontSize: 12, marginBottom: 8 }}>
                    {t("universeProcessed")} {task.processed} / {task.total}，{t("universeSuccess")} {task.ok_count}，{t("universeFail")} {task.failed_count}
                  </p>
                )}
                {task.status === "done" && task.result && (
                  <div style={{ color: "var(--text-muted, #888)", fontSize: 12, marginBottom: 8 }}>
                    {Object.entries(task.result).map(([key, val]) => {
                      const v = val as Record<string, number>;
                      if (typeof v !== "object" || v === null) return null;
                      const isBars = key.endsWith("_bars");
                      return (
                        <div key={key} style={{ marginBottom: 2 }}>
                          {isBars ? t("universeResultBarsSync") : t("universeResultListPull")} {key.replace(/_(universe|bars)$/, "")}：
                          {isBars
                            ? `${t("universeResultToSync")} ${v.total ?? 0}，${t("universeSuccess")} ${v.ok ?? 0}，${t("universeFail")} ${v.failed ?? 0}`
                            : `${t("universeResultSeen")} ${v.seen ?? 0}，${t("universeResultCreated")} ${v.created ?? 0}，${t("universeResultSkippedSuspended")} ${v.skipped_suspended ?? 0}`}
                        </div>
                      );
                    })}
                  </div>
                )}
                {task.errors && task.errors.length > 0 && (
                  <Alert
                    message={t("universeRecentErrors").replace("{n}", String(task.errors.length))}
                    description={task.errors.slice(-3).map((e, i) => (
                      <div key={i} style={{ fontSize: 12, color: "#ff4d4f" }}>
                        {String(e.stage || "")} {String(e.error || JSON.stringify(e))}
                      </div>
                    ))}
                    type="warning"
                    showIcon
                    style={{ marginBottom: 12 }}
                  />
                )}
              </>
            ) : (
              <p style={{ color: "var(--text-muted, #888)" }}>{t("universeNoTask")}</p>
            )}

            {/* 控制区 */}
            <div style={{ marginTop: 16, borderTop: "1px solid var(--border-color, #f0f0f0)", paddingTop: 16 }}>
              <Space direction="vertical" style={{ width: "100%" }} size="middle">
                <Space wrap>
                  <span>{t("universeWorkers")}：</span>
                  <InputNumber
                    min={1}
                    max={8}
                    value={maxWorkers}
                    onChange={(v) => setMaxWorkers(v ?? 5)}
                    disabled={anyRunning}
                    style={{ width: 80 }}
                  />
                  <span>{t("universeHistoryDays")}：</span>
                  <Select
                    value={historyDays}
                    onChange={(v) => setHistoryDays(v)}
                    options={i18nHistoryDaysOptions}
                    disabled={anyRunning}
                    style={{ width: 120 }}
                  />
                </Space>
                <Space wrap align="center">
                  <span>
                    {t("universeSyncLimit")}：
                    <Tooltip title={t("universeSyncLimitHint")}>
                      <QuestionCircleOutlined style={{ marginLeft: 4, color: "var(--text-muted, #888)" }} />
                    </Tooltip>
                  </span>
                  <Select
                    value={syncLimit}
                    onChange={(v) => setSyncLimit(v)}
                    options={i18nSyncLimitOptions}
                    disabled={anyRunning}
                    style={{ width: 140 }}
                  />
                </Space>
                <Space wrap align="center">
                  <span>
                    {t("universeInitScopes")}：
                    <Tooltip title={t("universeInitScopesHint")}>
                      <QuestionCircleOutlined style={{ marginLeft: 4, color: "var(--text-muted, #888)" }} />
                    </Tooltip>
                  </span>
                  <Checkbox.Group
                    options={i18nInitScopeOptions}
                    value={initScopes}
                    onChange={(values) => setInitScopes(values as string[])}
                    disabled={anyRunning}
                  />
                </Space>
                <Space wrap>
                  {!isRunning && !canRetry && (
                    <Button
                      type="primary"
                      icon={<PlayCircleOutlined />}
                      onClick={handleStart}
                      loading={starting}
                      disabled={anyRunning || initScopes.length === 0}
                    >
                      {t("universeStart")}
                    </Button>
                  )}
                  {canRetry && (
                    <Button
                      type="primary"
                      icon={<ReloadOutlined />}
                      onClick={handleRetry}
                      loading={starting}
                      disabled={anyRunning || initScopes.length === 0}
                    >
                      {t("universeRetry")}
                    </Button>
                  )}
                  {isRunning && (
                    <Button
                      danger
                      icon={<StopOutlined />}
                      onClick={handleCancel}
                      loading={cancelling}
                    >
                      {cancelling ? t("universeCanceling") : t("universeCancel")}
                    </Button>
                  )}
                  <Tooltip title={t("universeTip")}>
                    <QuestionCircleOutlined style={{ color: "var(--text-muted, #999)" }} />
                  </Tooltip>
                </Space>
              </Space>
            </div>
          </Card>
          )}

          {activeSyncPanel === "backfill" && (
          <Card
            title={
              <Space>
                <span>{t("universeBackfillTitle")}</span>
                <Tag color="blue">{t("universeHistoryPath2Badge")}</Tag>
                <Tag color="blue">{t("universeBackfillBadge")}</Tag>
                {bfTask ? statusTag(bfTask.status) : null}
              </Space>
            }
            size="small"
            style={{ marginBottom: 16 }}
          >
            {bfTask ? (
              <>
                <div style={{ marginBottom: 12 }}>
                  <Progress
                    percent={Math.round(bfTask.percent)}
                    status={
                      bfTask.status === "running" ? "active" :
                      bfTask.status === "done" ? "success" :
                      bfTask.status === "failed" ? "exception" :
                      bfTask.status === "cancelled" ? "exception" : "normal"
                    }
                  />
                </div>
                <p style={{ marginBottom: 8 }}>
                  <strong>{t("universeBackfillStageSync")}</strong>
                  {bfTask.message ? ` — ${bfTask.message}` : ""}
                </p>
                {bfTask.total > 0 ? (
                  <p style={{ color: "var(--text-muted, #888)", fontSize: 12, marginBottom: 8 }}>
                    {t("universeProcessed")} {bfTask.processed} / {bfTask.total}，{t("universeSuccess")} {bfTask.ok_count}，{t("universeFail")} {bfTask.failed_count}
                  </p>
                ) : (
                  bfTask.status === "running" && (
                    <p style={{ color: "var(--text-muted, #888)", fontSize: 12, marginBottom: 8 }}>
                      {t("universeBackfillPreparing")}
                    </p>
                  )
                )}
                {backfillScopeSummaries.length > 0 && (
                  <>
                    <Row gutter={[8, 8]} style={{ marginBottom: 12 }}>
                      <Col xs={12} sm={8} md={4}>
                        <div style={{ padding: "8px 12px", background: "var(--bg-elevated, #fafafa)", borderRadius: 6, border: "1px solid var(--border-color, #f0f0f0)" }}>
                          <div style={{ fontSize: 12, color: "var(--text-muted, #888)" }}>{t("universeBackfillSummaryScopes")}</div>
                          <div style={{ fontSize: 18, fontWeight: 600, marginTop: 2 }}>{backfillSummary.scopes}</div>
                        </div>
                      </Col>
                      <Col xs={12} sm={8} md={4}>
                        <div style={{ padding: "8px 12px", background: "var(--bg-elevated, #fafafa)", borderRadius: 6, border: "1px solid var(--border-color, #f0f0f0)" }}>
                          <div style={{ fontSize: 12, color: "var(--text-muted, #888)" }}>{t("universeBackfillSummaryProcessed")}</div>
                          <div style={{ fontSize: 18, fontWeight: 600, marginTop: 2 }}>{backfillSummary.processed}</div>
                        </div>
                      </Col>
                      <Col xs={12} sm={8} md={4}>
                        <div style={{ padding: "8px 12px", background: "var(--bg-elevated, #fafafa)", borderRadius: 6, border: "1px solid var(--border-color, #f0f0f0)" }}>
                          <div style={{ fontSize: 12, color: "var(--text-muted, #888)" }}>{t("universeBackfillSummaryOk")}</div>
                          <div style={{ fontSize: 18, fontWeight: 600, marginTop: 2, color: "#52c41a" }}>{backfillSummary.ok}</div>
                        </div>
                      </Col>
                      <Col xs={12} sm={8} md={4}>
                        <div style={{ padding: "8px 12px", background: "var(--bg-elevated, #fafafa)", borderRadius: 6, border: "1px solid var(--border-color, #f0f0f0)" }}>
                          <div style={{ fontSize: 12, color: "var(--text-muted, #888)" }}>{t("universeBackfillSummarySkipped")}</div>
                          <div style={{ fontSize: 18, fontWeight: 600, marginTop: 2 }}>{backfillSummary.skipped}</div>
                        </div>
                      </Col>
                      <Col xs={12} sm={8} md={4}>
                        <div style={{ padding: "8px 12px", background: "var(--bg-elevated, #fafafa)", borderRadius: 6, border: "1px solid var(--border-color, #f0f0f0)" }}>
                          <div style={{ fontSize: 12, color: "var(--text-muted, #888)" }}>{t("universeBackfillSummaryFailed")}</div>
                          <div style={{ fontSize: 18, fontWeight: 600, marginTop: 2, color: backfillSummary.failed > 0 ? "#ff4d4f" : undefined }}>{backfillSummary.failed}</div>
                        </div>
                      </Col>
                    </Row>
                    <div style={{ color: "var(--text-muted, #888)", fontSize: 12, marginBottom: 8 }}>
                      {backfillScopeSummaries.map((item) => (
                        <div key={item.key} style={{ marginBottom: 4 }}>
                          {scopeLabel(item.scope)}：
                          {t("universeBackfillSummaryProcessed")} {item.processed > 0 ? item.processed : item.total}，
                          {t("universeBackfillSummaryOk")} {item.ok}，
                          {t("universeBackfillSummarySkipped")} {item.skipped}，
                          {t("universeBackfillSummaryFailed")} {item.failed}
                        </div>
                      ))}
                    </div>
                  </>
                )}
                {bfTask.errors && bfTask.errors.length > 0 && (
                  <Alert
                    message={t("universeRecentErrors").replace("{n}", String(bfTask.errors.length))}
                    description={bfTask.errors.slice(-3).map((e, i) => (
                      <div key={i} style={{ fontSize: 12, color: "#ff4d4f" }}>
                        {String(e.stage || "")} {String(e.error || JSON.stringify(e))}
                      </div>
                    ))}
                    type="warning"
                    showIcon
                    style={{ marginBottom: 12 }}
                  />
                )}
              </>
            ) : (
              <p style={{ color: "var(--text-muted, #888)" }}>{t("universeBackfillNoTask")}</p>
            )}

            {/* 控制区 */}
            <div style={{ marginTop: 16, borderTop: "1px solid var(--border-color, #f0f0f0)", paddingTop: 16 }}>
              <Space direction="vertical" style={{ width: "100%" }} size="middle">
                <Space wrap>
                  <span>{t("universeHistoryDays")}：</span>
                  <Select
                    value={bfHistoryDays}
                    onChange={setBfHistoryDays}
                    disabled={anyRunning}
                    style={{ width: 120 }}
                    options={i18nHistoryDaysOptions}
                  />
                  <Tooltip title={t("universeHistoryDaysHint")}>
                    <QuestionCircleOutlined style={{ marginLeft: 4, color: "var(--text-muted, #888)" }} />
                  </Tooltip>
                </Space>
                <Space wrap>
                  <span>{t("universeScopes")}：</span>
                  <Checkbox.Group
                    options={i18nInitScopeOptions}
                    value={bfScopes}
                    onChange={(vals) => setBfScopes(vals as string[])}
                    disabled={anyRunning}
                  />
                  <Tooltip title={t("universeInitScopesHint")}>
                    <QuestionCircleOutlined style={{ marginLeft: 4, color: "var(--text-muted, #888)" }} />
                  </Tooltip>
                </Space>
                <Space wrap>
                  <span>{t("universeSyncLimit")}：</span>
                  <Select
                    value={bfSyncLimit}
                    onChange={setBfSyncLimit}
                    disabled={anyRunning}
                    style={{ width: 140 }}
                    options={i18nSyncLimitOptions}
                  />
                  <Tooltip title={t("universeSyncLimitHint")}>
                    <QuestionCircleOutlined style={{ marginLeft: 4, color: "var(--text-muted, #888)" }} />
                  </Tooltip>
                </Space>
                <Space wrap>
                  <span>{t("universeWorkers")}：</span>
                  <InputNumber
                    min={1}
                    max={8}
                    value={maxWorkers}
                    onChange={(v) => setMaxWorkers(v ?? 5)}
                    disabled={anyRunning}
                    style={{ width: 80 }}
                  />
                  {!bfIsRunning && (
                    <Button
                      type="primary"
                      icon={<PlayCircleOutlined />}
                      onClick={handleBfStart}
                      loading={bfStarting}
                      disabled={anyRunning}
                    >
                      {t("universeBackfillStart")}
                    </Button>
                  )}
                  {bfIsRunning && (
                    <Button
                      danger
                      icon={<StopOutlined />}
                      onClick={handleBfCancel}
                      loading={bfCancelling}
                    >
                      {bfCancelling ? t("universeCanceling") : t("universeBackfillCancel")}
                    </Button>
                  )}
                  <Tooltip title={t("universeBackfillTip")}>
                    <QuestionCircleOutlined style={{ color: "var(--text-muted, #999)" }} />
                  </Tooltip>
                </Space>
              </Space>
            </div>
          </Card>
          )}
            </>
          )}

          {syncTab === "advanced" && (
            <>
          {activeSyncPanel === "repair" && (
          <Card
            title={
              <Space>
                <span>{t("universeRangeRepairTitle")}</span>
                <Tag color="orange">{t("universeRangeRepairBadge")}</Tag>
                {repairTask ? statusTag(repairTask.status) : null}
              </Space>
            }
            size="small"
            style={{ marginBottom: 16 }}
          >
            {repairTask ? (
              <>
                <div style={{ marginBottom: 12 }}>
                  <Progress
                    percent={Math.round(repairTask.percent)}
                    status={
                      repairTask.status === "running" ? "active" :
                      repairTask.status === "done" ? "success" :
                      repairTask.status === "failed" ? "exception" :
                      repairTask.status === "cancelled" ? "exception" : "normal"
                    }
                  />
                </div>
                <p style={{ marginBottom: 8 }}>
                  <strong>{t("universeRangeRepairStageSync")}</strong>
                  {repairTask.message ? ` - ${repairTask.message}` : ""}
                </p>
                {repairTask.total > 0 ? (
                  <p style={{ color: "var(--text-muted, #888)", fontSize: 12, marginBottom: 8 }}>
                    {t("universeProcessed")} {repairTask.processed} / {repairTask.total}，{t("universeSuccess")} {repairTask.ok_count}，{t("universeFail")} {repairTask.failed_count}
                  </p>
                ) : (
                  repairTask.status === "running" && (
                    <p style={{ color: "var(--text-muted, #888)", fontSize: 12, marginBottom: 8 }}>
                      {t("universeRangeRepairPreparing")}
                    </p>
                  )
                )}
                {repairScopeSummaries.length > 0 && (
                  <>
                    <Row gutter={[8, 8]} style={{ marginBottom: 12 }}>
                      <Col xs={12} sm={6}>
                        <div style={{ padding: "8px 12px", background: "var(--bg-elevated, #fafafa)", borderRadius: 6, border: "1px solid var(--border-color, #f0f0f0)" }}>
                          <div style={{ fontSize: 12, color: "var(--text-muted, #888)" }}>{t("universeRangeRepairSummaryScopes")}</div>
                          <div style={{ fontSize: 18, fontWeight: 600, marginTop: 2 }}>{repairSummary.scopes}</div>
                        </div>
                      </Col>
                      <Col xs={12} sm={6}>
                        <div style={{ padding: "8px 12px", background: "var(--bg-elevated, #fafafa)", borderRadius: 6, border: "1px solid var(--border-color, #f0f0f0)" }}>
                          <div style={{ fontSize: 12, color: "var(--text-muted, #888)" }}>{t("universeRangeRepairSummaryInserted")}</div>
                          <div style={{ fontSize: 18, fontWeight: 600, marginTop: 2, color: "#52c41a" }}>{repairSummary.inserted}</div>
                        </div>
                      </Col>
                      <Col xs={12} sm={6}>
                        <div style={{ padding: "8px 12px", background: "var(--bg-elevated, #fafafa)", borderRadius: 6, border: "1px solid var(--border-color, #f0f0f0)" }}>
                          <div style={{ fontSize: 12, color: "var(--text-muted, #888)" }}>{t("universeRangeRepairSummaryUpdated")}</div>
                          <div style={{ fontSize: 18, fontWeight: 600, marginTop: 2, color: "#1890ff" }}>{repairSummary.updated}</div>
                        </div>
                      </Col>
                      <Col xs={12} sm={6}>
                        <div style={{ padding: "8px 12px", background: "var(--bg-elevated, #fafafa)", borderRadius: 6, border: "1px solid var(--border-color, #f0f0f0)" }}>
                          <div style={{ fontSize: 12, color: "var(--text-muted, #888)" }}>{t("universeRangeRepairSummaryEmptyChunks")}</div>
                          <div style={{ fontSize: 18, fontWeight: 600, marginTop: 2 }}>{repairSummary.emptyChunks}</div>
                        </div>
                      </Col>
                    </Row>
                    <div style={{ color: "var(--text-muted, #888)", fontSize: 12, marginBottom: 8 }}>
                      {repairScopeSummaries.map((item) => (
                        <div key={item.key} style={{ marginBottom: 4 }}>
                          {scopeLabel(item.scope)}：
                          {t("universeRangeRepairSummaryProcessed")} {item.processed > 0 ? item.processed : item.total}，
                          {t("universeRangeRepairSummaryOk")} {item.ok}，
                          {t("universeRangeRepairSummarySkipped")} {item.skipped}，
                          {t("universeRangeRepairSummaryFailed")} {item.failed}，
                          {t("universeRangeRepairSummaryInserted")} {item.inserted}，
                          {t("universeRangeRepairSummaryUpdated")} {item.updated}，
                          {t("universeRangeRepairSummaryEmptyChunks")} {item.emptyChunks}
                        </div>
                      ))}
                    </div>
                  </>
                )}
                {repairTask.errors && repairTask.errors.length > 0 && (
                  <Alert
                    message={t("universeRecentErrors").replace("{n}", String(repairTask.errors.length))}
                    description={repairTask.errors.slice(-3).map((e, i) => (
                      <div key={i} style={{ fontSize: 12, color: "#ff4d4f" }}>
                        {String(e.stage || "")} {String(e.error || JSON.stringify(e))}
                      </div>
                    ))}
                    type="warning"
                    showIcon
                    style={{ marginBottom: 12 }}
                  />
                )}
              </>
            ) : (
              <p style={{ color: "var(--text-muted, #888)" }}>{t("universeRangeRepairNoTask")}</p>
            )}

            <div style={{ marginTop: 16, borderTop: "1px solid var(--border-color, #f0f0f0)", paddingTop: 16 }}>
              <Space direction="vertical" style={{ width: "100%" }} size="middle">
                <Space wrap>
                  <span>{t("universeHistoryDays")}：</span>
                  <Select
                    value={repairHistoryDays}
                    onChange={setRepairHistoryDays}
                    disabled={anyRunning}
                    style={{ width: 120 }}
                    options={i18nHistoryDaysOptions}
                  />
                  <Tooltip title={t("universeHistoryDaysHint")}>
                    <QuestionCircleOutlined style={{ marginLeft: 4, color: "var(--text-muted, #888)" }} />
                  </Tooltip>
                </Space>
                <Space wrap>
                  <span>
                    {t("universeRangeRepairChunkDays")}：
                    <Tooltip title={t("universeRangeRepairChunkHint")}>
                      <QuestionCircleOutlined style={{ marginLeft: 4, color: "var(--text-muted, #888)" }} />
                    </Tooltip>
                  </span>
                  <Select
                    value={repairChunkDays}
                    onChange={setRepairChunkDays}
                    disabled={anyRunning}
                    style={{ width: 140 }}
                    options={i18nRepairChunkOptions}
                  />
                </Space>
                <Space wrap>
                  <span>{t("universeScopes")}：</span>
                  <Checkbox.Group
                    options={i18nInitScopeOptions}
                    value={repairScopes}
                    onChange={(vals) => setRepairScopes(vals as string[])}
                    disabled={anyRunning}
                  />
                </Space>
                <Space wrap>
                  <span>{t("universeSyncLimit")}：</span>
                  <Select
                    value={repairSyncLimit}
                    onChange={setRepairSyncLimit}
                    disabled={anyRunning}
                    style={{ width: 140 }}
                    options={i18nSyncLimitOptions}
                  />
                  <Tooltip title={t("universeSyncLimitHint")}>
                    <QuestionCircleOutlined style={{ marginLeft: 4, color: "var(--text-muted, #888)" }} />
                  </Tooltip>
                </Space>
                <Space wrap>
                  <span>{t("universeWorkers")}：</span>
                  <InputNumber
                    min={1}
                    max={8}
                    value={maxWorkers}
                    onChange={(v) => setMaxWorkers(v ?? 5)}
                    disabled={anyRunning}
                    style={{ width: 80 }}
                  />
                  {!repairIsRunning && (
                    <Button
                      type="default"
                      icon={<PlayCircleOutlined />}
                      onClick={handleRepairStart}
                      loading={repairStarting}
                      disabled={anyRunning || repairScopes.length === 0}
                    >
                      {t("universeRangeRepairStart")}
                    </Button>
                  )}
                  {repairIsRunning && (
                    <Button
                      danger
                      icon={<StopOutlined />}
                      onClick={handleRepairCancel}
                      loading={repairCancelling}
                    >
                      {repairCancelling ? t("universeCanceling") : t("universeRangeRepairCancel")}
                    </Button>
                  )}
                  <Tooltip title={t("universeRangeRepairTip")}>
                    <QuestionCircleOutlined style={{ color: "var(--text-muted, #999)" }} />
                  </Tooltip>
                </Space>
              </Space>
            </div>
          </Card>
          )}
            </>
          )}

        </div>
      </section>
    </div>
  );
}
