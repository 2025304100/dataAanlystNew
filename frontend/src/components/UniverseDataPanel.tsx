import { useState, useEffect, useCallback } from "react";
import { Card, Button, Progress, Statistic, Row, Col, InputNumber, Select, Space, Alert, Tag, Tooltip, Checkbox } from "antd";
import { PlayCircleOutlined, ReloadOutlined, StopOutlined, QuestionCircleOutlined } from "@ant-design/icons";
import { api } from "../api/client";
import { t } from "../i18n";
import { useApp } from "../context/AppContext";

// P0.6：初始化 scope 选项
const INIT_SCOPE_OPTIONS = [
  { label: "A股股票", value: "cn-stock" },
  { label: "A股ETF", value: "cn-etf" },
  { label: "美股股票", value: "us-stock" },
  { label: "美股ETF", value: "us-etf" },
];

// 历史K线天数下拉选项
const HISTORY_DAYS_OPTIONS = [
  { label: "近1月", value: 30 },
  { label: "近1年", value: 365 },
  { label: "近3年", value: 1095 },
  { label: "近5年", value: 1825 },
  { label: "近10年", value: 3650 },
];

// 单次同步标的上限（分段同步，避免一口气跑太久）
const SYNC_LIMIT_OPTIONS = [
  { label: "不限（全部）", value: 0 },
  { label: "100 个", value: 100 },
  { label: "300 个", value: 300 },
  { label: "500 个", value: 500 },
  { label: "1000 个", value: 1000 },
  { label: "2000 个", value: 2000 },
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

// scope 中文映射
const SCOPE_LABELS: Record<string, string> = {
  "cn-stock": "A股股票",
  "cn-etf": "A股ETF",
  "us-stock": "美股股票",
  "us-etf": "美股ETF",
};

// 板块中文映射
const BOARD_LABELS: Record<string, string> = {
  main: "主板",
  gem: "创业板",
  star: "科创板",
  bj: "北交所",
};

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
  const map: Record<TaskStatus, { color: string; text: string }> = {
    queued: { color: "default", text: "排队中" },
    running: { color: "processing", text: "运行中" },
    done: { color: "success", text: "已完成" },
    failed: { color: "error", text: "失败" },
    cancelled: { color: "warning", text: "已取消" },
  };
  const cfg = map[status] || map.queued;
  return <Tag color={cfg.color}>{cfg.text}</Tag>;
}

// 配置 localStorage 持久化
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
  const ctx = useApp();
  const _savedCfg = loadUniverseConfig();
  const [stats, setStats] = useState<UniverseStats | null>(null);
  const [task, setTask] = useState<UniverseTask | null>(null);
  const [incrTask, setIncrTask] = useState<UniverseTask | null>(null);
  const [loading, setLoading] = useState(false);
  const [starting, setStarting] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [incrStarting, setIncrStarting] = useState(false);
  const [incrCancelling, setIncrCancelling] = useState(false);
  const [maxWorkers, setMaxWorkers] = useState<number>(_savedCfg?.maxWorkers ?? 5);
  const [historyDays, setHistoryDays] = useState<number>(_savedCfg?.historyDays ?? 365);
  const [syncLimit, setSyncLimit] = useState<number>(_savedCfg?.syncLimit ?? 0);
  // P0.6：初始化 scope 选择，默认全部4个
  const [initScopes, setInitScopes] = useState<string[]>(_savedCfg?.initScopes ?? ["cn-stock"]);
  // 历史回补
  const [bfTask, setBfTask] = useState<UniverseTask | null>(null);
  const [bfStarting, setBfStarting] = useState(false);
  const [bfCancelling, setBfCancelling] = useState(false);
  const [bfHistoryDays, setBfHistoryDays] = useState<number>(_savedCfg?.bfHistoryDays ?? 1095);
  const [bfScopes, setBfScopes] = useState<string[]>(_savedCfg?.bfScopes ?? ["cn-stock"]);
  const [bfSyncLimit, setBfSyncLimit] = useState<number>(_savedCfg?.bfSyncLimit ?? 0);

  // 配置变更时持久化到 localStorage
  useEffect(() => {
    saveUniverseConfig({ maxWorkers, historyDays, syncLimit, initScopes, bfHistoryDays, bfScopes, bfSyncLimit });
  }, [maxWorkers, historyDays, syncLimit, initScopes, bfHistoryDays, bfScopes, bfSyncLimit]);

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

  // 初始加载
  useEffect(() => {
    refreshStats();
    refreshTask();
    refreshIncrTask();
    refreshBfTask();
  }, [refreshStats, refreshTask, refreshIncrTask, refreshBfTask]);

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

  const handleStart = async () => {
    if (initScopes.length === 0) {
      ctx.showToast("error", "请至少选择一个初始化范围");
      return;
    }
    setStarting(true);
    try {
      // 选满4个时传 null（后端默认全部），否则传具体列表
      const scopes = initScopes.length === 4 ? null : initScopes;
      await api.startUniverseInit(maxWorkers, historyDays, scopes, syncLimit);
      await refreshTask();
      ctx.showToast("success", "初始化同步已启动");
    } catch (e: any) {
      ctx.showToast("error", e.message || "启动失败");
    } finally {
      setStarting(false);
    }
  };

  const handleRetry = async () => {
    if (initScopes.length === 0) {
      ctx.showToast("error", "请至少选择一个初始化范围");
      return;
    }
    setStarting(true);
    try {
      const scopes = initScopes.length === 4 ? null : initScopes;
      await api.retryUniverseInit(maxWorkers, historyDays, scopes, syncLimit);
      await refreshTask();
      ctx.showToast("success", "重试同步已启动（断点续传）");
    } catch (e: any) {
      ctx.showToast("error", e.message || "重试失败");
    } finally {
      setStarting(false);
    }
  };

  const handleCancel = async () => {
    setCancelling(true);
    try {
      await api.cancelUniverseInit();
      await refreshTask();
      ctx.showToast("success", "任务已取消，已同步进度已保留");
    } catch (e: any) {
      ctx.showToast("error", e.message || "取消失败");
    } finally {
      setCancelling(false);
    }
  };

  // P2：增量同步处理函数
  const handleIncrStart = async () => {
    setIncrStarting(true);
    try {
      await api.startUniverseIncrementalSync(maxWorkers);
      await refreshIncrTask();
      ctx.showToast("success", t("universeIncrementalStart"));
    } catch (e: any) {
      ctx.showToast("error", e.message || "启动失败");
    } finally {
      setIncrStarting(false);
    }
  };

  const handleIncrCancel = async () => {
    setIncrCancelling(true);
    try {
      await api.cancelUniverseIncrementalSync();
      await refreshIncrTask();
      ctx.showToast("success", "增量同步已取消");
    } catch (e: any) {
      ctx.showToast("error", e.message || "取消失败");
    } finally {
      setIncrCancelling(false);
    }
  };

  const isRunning = task?.status === "running" || task?.status === "queued";
  const canRetry = task?.status === "failed" || task?.status === "cancelled" || task?.status === "done";
  const incrIsRunning = incrTask?.status === "running" || incrTask?.status === "queued";

  // 历史回补处理函数
  const handleBfStart = async () => {
    if (bfScopes.length === 0) {
      ctx.showToast("error", "请至少选择一个回补范围");
      return;
    }
    setBfStarting(true);
    try {
      const scopes = bfScopes.length === 4 ? null : bfScopes;
      await api.startUniverseBackfill(maxWorkers, bfHistoryDays, scopes, bfSyncLimit);
      await refreshBfTask();
      ctx.showToast("success", "历史回补已启动");
    } catch (e: any) {
      ctx.showToast("error", e.message || "启动失败");
    } finally {
      setBfStarting(false);
    }
  };

  const handleBfCancel = async () => {
    setBfCancelling(true);
    try {
      await api.cancelUniverseBackfill();
      await refreshBfTask();
      ctx.showToast("success", "历史回补已取消");
    } catch (e: any) {
      ctx.showToast("error", e.message || "取消失败");
    } finally {
      setBfCancelling(false);
    }
  };

  const bfIsRunning = bfTask?.status === "running" || bfTask?.status === "queued";
  // 任意同步任务运行中时，禁用其他启动按钮
  const anyRunning = isRunning || incrIsRunning || bfIsRunning;

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
                            <div style={{ fontSize: 12, color: "var(--text-muted, #888)" }}>{SCOPE_LABELS[scope] || scope}</div>
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
                            <div style={{ fontSize: 12, color: "var(--text-muted, #888)" }}>{BOARD_LABELS[board] || board}</div>
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
                          title={type === "stock" ? "股票" : type === "etf" ? "ETF" : type}
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

          {/* 初始化同步任务 */}
          <Card
            title={
              <Space>
                <span>{t("universeTaskTitle")}</span>
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
                    已处理 {task.processed} / {task.total}，成功 {task.ok_count}，失败 {task.failed_count}
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
                          {isBars ? "K线同步" : "列表拉取"} {key.replace(/_(universe|bars)$/, "")}：
                          {isBars
                            ? `待同步 ${v.total ?? 0}，成功 ${v.ok ?? 0}，失败 ${v.failed ?? 0}`
                            : `见 ${v.seen ?? 0}，新建 ${v.created ?? 0}，跳过停牌 ${v.skipped_suspended ?? 0}`}
                        </div>
                      );
                    })}
                  </div>
                )}
                {task.errors && task.errors.length > 0 && (
                  <Alert
                    message={`最近 ${task.errors.length} 条错误`}
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
                    disabled={isRunning}
                    style={{ width: 80 }}
                  />
                  <span>{t("universeHistoryDays")}：</span>
                  <Select
                    value={historyDays}
                    onChange={(v) => setHistoryDays(v)}
                    options={HISTORY_DAYS_OPTIONS}
                    disabled={isRunning}
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
                    options={SYNC_LIMIT_OPTIONS}
                    disabled={isRunning}
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
                    options={INIT_SCOPE_OPTIONS}
                    value={initScopes}
                    onChange={(values) => setInitScopes(values as string[])}
                    disabled={isRunning}
                  />
                </Space>
                <Space wrap>
                  {!isRunning && !canRetry && (
                    <Button
                      type="primary"
                      icon={<PlayCircleOutlined />}
                      onClick={handleStart}
                      loading={starting}
                      disabled={initScopes.length === 0}
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
                      disabled={initScopes.length === 0}
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
                <Alert
                  message={t("universeTip")}
                  type="info"
                  showIcon
                  style={{ fontSize: 12 }}
                />
              </Space>
            </div>
          </Card>

          {/* P2：增量同步任务 */}
          <Card
            title={
              <Space>
                <span>{t("universeIncrementalTitle")}</span>
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
                    已处理 {incrTask.processed} / {incrTask.total}，成功 {incrTask.ok_count}，失败 {incrTask.failed_count}
                  </p>
                )}
                {incrTask.errors && incrTask.errors.length > 0 && (
                  <Alert
                    message={`最近 ${incrTask.errors.length} 条错误`}
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
                    disabled={incrIsRunning}
                    style={{ width: 80 }}
                  />
                  {!incrIsRunning && (
                    <Button
                      type="primary"
                      icon={<PlayCircleOutlined />}
                      onClick={handleIncrStart}
                      loading={incrStarting}
                      disabled={isRunning}
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
                <Alert
                  message={t("universeIncrementalTip")}
                  type="info"
                  showIcon
                  style={{ fontSize: 12 }}
                />
              </Space>
            </div>
          </Card>

          {/* 历史回补任务 */}
          <Card
            title={
              <Space>
                <span>{t("universeBackfillTitle")}</span>
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
                    已处理 {bfTask.processed} / {bfTask.total}，成功 {bfTask.ok_count}，失败 {bfTask.failed_count}
                  </p>
                ) : (
                  bfTask.status === "running" && (
                    <p style={{ color: "var(--text-muted, #888)", fontSize: 12, marginBottom: 8 }}>
                      {t("universeBackfillPreparing")}
                    </p>
                  )
                )}
                {bfTask.status === "done" && bfTask.result && (
                  <div style={{ color: "var(--text-muted, #888)", fontSize: 12, marginBottom: 8 }}>
                    {Object.entries(bfTask.result).map(([key, val]) => {
                      const v = val as Record<string, number>;
                      if (typeof v !== "object" || v === null) return null;
                      return (
                        <div key={key} style={{ marginBottom: 2 }}>
                          回补 {key.replace(/_backfill$/, "")}：
                          待回补 {v.total ?? 0}，成功 {v.ok ?? 0}，失败 {v.failed ?? 0}
                        </div>
                      );
                    })}
                  </div>
                )}
                {bfTask.errors && bfTask.errors.length > 0 && (
                  <Alert
                    message={`最近 ${bfTask.errors.length} 条错误`}
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
                    disabled={bfIsRunning}
                    style={{ width: 120 }}
                    options={HISTORY_DAYS_OPTIONS}
                  />
                  <Tooltip title={t("universeHistoryDaysHint")}>
                    <QuestionCircleOutlined style={{ marginLeft: 4, color: "var(--text-muted, #888)" }} />
                  </Tooltip>
                </Space>
                <Space wrap>
                  <span>{t("universeScopes")}：</span>
                  <Checkbox.Group
                    options={INIT_SCOPE_OPTIONS}
                    value={bfScopes}
                    onChange={(vals) => setBfScopes(vals as string[])}
                    disabled={bfIsRunning}
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
                    disabled={bfIsRunning}
                    style={{ width: 140 }}
                    options={SYNC_LIMIT_OPTIONS}
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
                    disabled={bfIsRunning}
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
                <Alert
                  message={t("universeBackfillTip")}
                  type="info"
                  showIcon
                  style={{ fontSize: 12 }}
                />
              </Space>
            </div>
          </Card>
        </div>
      </section>
    </div>
  );
}
