import { useState, useEffect, useCallback, useMemo } from "react";
import { Card, Button, Progress, Statistic, Row, Col, InputNumber, Select, Space, Alert, Tag, Tooltip, Checkbox, Tabs, message } from "antd";
import { PlayCircleOutlined, ReloadOutlined, StopOutlined, QuestionCircleOutlined, ThunderboltOutlined, HistoryOutlined, ToolOutlined } from "@ant-design/icons";
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
  daily: "smart",
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
    } catch {
      // ignore
    }
  }, []);

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
    const timer = setInterval(() => {
      refreshIncrTask();
      refreshStats();
    }, 5000);
    return () => clearInterval(timer);
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
    setIncrStarting(true);
    try {
      await api.startUniverseIncrementalSync(maxWorkers);
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
        key: "smart",
        titleKey: "universeQuickSmartTitle",
        descKey: "universeQuickDailySmart",
        badgeKey: "universeSmartRecommended",
        badgeColor: "gold",
      },
      {
        key: "incremental",
        titleKey: "universeQuickIncrementalTitle",
        descKey: "universeQuickDailyIncremental",
        badgeKey: "universeIncrementalBadge",
        badgeColor: "blue",
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
                {incrTask.errors && incrTask.errors.length > 0 && (
                  <Alert
                    message={t("universeRecentErrors").replace("{n}", String(incrTask.errors.length))}
                    description={incrTask.errors.slice(-3).map((e, i) => (
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
              <p style={{ color: "var(--text-muted, #888)" }}>{t("universeIncrementalNoTask")}</p>
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
                  {!incrIsRunning && (
                    <Button
                      type="default"
                      icon={<PlayCircleOutlined />}
                      onClick={handleIncrStart}
                      loading={incrStarting}
                      disabled={anyRunning}
                    >
                      {t("universeIncrementalStart")}
                    </Button>
                  )}
                  {incrIsRunning && (
                    <Button
                      danger
                      icon={<StopOutlined />}
                      onClick={handleIncrCancel}
                      loading={incrCancelling}
                    >
                      {incrCancelling ? t("universeCanceling") : t("universeIncrementalCancel")}
                    </Button>
                  )}
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
