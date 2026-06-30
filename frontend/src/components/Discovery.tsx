import { useState, useMemo, useCallback, useEffect } from "react";
import { useApp } from "../context/AppContext";
import { api } from "../api/client";
import { t, template, regionShortLabel, assetTypeLabel, stageLabel, actionLabel } from "../i18n";
import { Input, Select, Checkbox, Button, Tag, Space, InputNumber } from "antd";
import {
  percent,
  score,
  money,
  joinParts,
  badgeClass,
  pnlClass,
  discoveryFreshness,
  withFinalOpportunityScore,
  opportunityScoreValue,
  ageDays,
  clamp,
} from "../utils/format";

const DOT = " | ";

const STEP_ORDER = ["prepare", "sync", "scan", "news", "done"] as const;

const STEP_LABEL_KEYS: Record<string, string> = {
  prepare: "stepPrepare",
  sync: "stepSync",
  scan: "stepScan",
  news: "stepNews",
  done: "stepDone",
};

function scopeLabel(scope: string): string {
  switch (scope) {
    case "cn-stock":
      return t("discoveryScopeCnStock");
    case "cn-etf":
      return t("discoveryScopeCnEtf");
    case "us-stock":
      return t("discoveryScopeUsStock");
    case "us-etf":
      return t("discoveryScopeUsEtf");
    default:
      return scope;
  }
}

function stepClassName(task: { status: string; stage: string } | null, step: string): string {
  if (!task) return "";
  if (task.status === "done") return "done";
  const stageIdx = STEP_ORDER.indexOf(task.stage as (typeof STEP_ORDER)[number]);
  const stepIdx = STEP_ORDER.indexOf(step as (typeof STEP_ORDER)[number]);
  if (stageIdx < 0) return "";
  if (stepIdx < stageIdx) return "done";
  if (stepIdx === stageIdx) return "active";
  return "";
}

export default function Discovery() {
  const ctx = useApp();
  const workbench = ctx.workbench;
  const task = ctx.discoveryTask;
  const scopeStats = ctx.discoveryScopeStats;

  const [scope, setScope] = useState("cn-stock");
  const [minScore, setMinScore] = useState(55);
  const [dataMode, setDataMode] = useState("cached");
  const [coverageHint, setCoverageHint] = useState<string | null>(null);

  // 加载数据覆盖率提示（P0-4.3）
  useEffect(() => {
    api.getDataHealth().then((data: any) => {
      const bars = data?.bars;
      if (bars) {
        const pct = bars.coverage_pct ?? 100;
        if (pct < 60) {
          setCoverageHint(ctx.locale === "zh-CN"
            ? `行情覆盖率仅 ${pct.toFixed(0)}%，扫描结果可能不完整`
            : `Bar coverage only ${pct.toFixed(0)}%, scan results may be incomplete`);
        } else if (pct < 85) {
          setCoverageHint(ctx.locale === "zh-CN"
            ? `行情覆盖率 ${pct.toFixed(0)}%，部分标的数据可能缺失`
            : `Bar coverage ${pct.toFixed(0)}%, some symbols may lack data`);
        } else {
          setCoverageHint(null);
        }
      }
    }).catch(() => {});
  }, [ctx.locale]);
  const [batchSize, setBatchSize] = useState(20);
  const [delay, setDelay] = useState(0.25);
  const [warningDays, setWarningDays] = useState(3);
  const [validDays, setValidDays] = useState(5);
  const [includeNews, setIncludeNews] = useState(true);

  // 本地 loading 状态（避免与全局 loading 冲突）
  const [starting, setStarting] = useState(false);
  const [pausing, setPausing] = useState(false);
  const [resuming, setResuming] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [cleaning, setCleaning] = useState(false);
  const [rowActionLoading, setRowActionLoading] = useState<Record<number, { watchlist?: boolean; freeze?: boolean; update?: boolean }>>({});

  const isTaskActive = !!task && ["queued", "running"].includes(task.status);
  const canPause = isTaskActive;
  const canResume = task?.status === "paused" && !!task?.can_resume;
  const canCancel = !!task && ["queued", "running", "paused"].includes(task.status);
  const canStart = !isTaskActive;

  const sortedResults = useMemo(() => {
    if (!workbench) return [];
    return workbench.candidates
      .map((item) => withFinalOpportunityScore(item, ctx.newsSnapshot))
      .sort((a, b) => {
        const aFrozen = a.is_frozen ? 1 : 0;
        const bFrozen = b.is_frozen ? 1 : 0;
        if (aFrozen !== bFrozen) return bFrozen - aFrozen;
        const aScore = opportunityScoreValue(a);
        const bScore = opportunityScoreValue(b);
        if (bScore !== aScore) return bScore - aScore;
        return String(b.created_at ?? "").localeCompare(String(a.created_at ?? ""));
      });
  }, [workbench, ctx.newsSnapshot]);

  // ════════════════════════════════════════════════
  // 多视角候选池（P1-3）
  // ════════════════════════════════════════════════
  const candidatePools = useMemo(() => {
    const all = sortedResults;
    const isZh = ctx.locale === "zh-CN";

    // 1. 高股质候选池：quality_score >= 70
    const highQuality = all.filter((item: any) => {
      const qScore = item.quality_score ?? item.latest_score?.quality_score ?? 0;
      return qScore >= 70;
    });

    // 2. 高时点候选池：timing_score >= 65 + stage in ['start', 'accel']
    const highTiming = all.filter((item: any) => {
      const tScore = item.timing_score ?? item.latest_score?.timing_score ?? 0;
      const stage = item.stage ?? item.latest_score?.stage ?? "";
      return tScore >= 65 && ["start", "accel"].includes(stage);
    });

    // 3. 组合可执行候选池：priority_score >= 60 + action = 'open'
    const actionable = all.filter((item: any) => {
      const pScore = item.priority_score ?? item.latest_score?.priority_score ?? 0;
      const action = item.action ?? item.latest_score?.action ?? "";
      return pScore >= 60 && action === "open";
    });

    // 4. 过热风险候选池：stage = 'overheat' + action = 'reduce'
    const overheatRisk = all.filter((item: any) => {
      const stage = item.stage ?? item.latest_score?.stage ?? "";
      const action = item.action ?? item.latest_score?.action ?? "";
      return stage === "overheat" && action === "reduce";
    });

    // 5. 低可信度候选池：数据过期（> 7天未更新）
    const lowCredibility = all.filter((item: any) => {
      const createdAt = item.created_at ?? item.latest_score?.created_at ?? "";
      if (!createdAt) return true; // 无时间信息 = 低可信度
      try {
        const age = Math.floor((Date.now() - new Date(createdAt).getTime()) / 86400000);
        return age > 7;
      } catch { return true; }
    });

    return {
      all,
      highQuality,
      highTiming,
      actionable,
      overheatRisk,
      lowCredibility,
    };
  }, [sortedResults, ctx.locale]);

  // 候选池 Tab 状态
  const [poolTab, setPoolTab] = useState<"all" | "highQuality" | "highTiming" | "actionable" | "overheatRisk" | "lowCredibility">("actionable");

  const poolLabels: Record<string, { zh: string; en: string; color: string }> = {
    all: { zh: "全部候选", en: "All Candidates", color: "#6b7280" },
    highQuality: { zh: "高股质候选", en: "High Quality", color: "#0f766e" },
    highTiming: { zh: "高时点候选", en: "High Timing", color: "#2563eb" },
    actionable: { zh: "组合可执行", en: "Actionable", color: "#d97706" },
    overheatRisk: { zh: "过热风险", en: "Overheat Risk", color: "#b42318" },
    lowCredibility: { zh: "低可信度", en: "Low Credibility", color: "#9ca3af" },
  };

  const currentPool = candidatePools[poolTab] ?? candidatePools.all;

  const handleStart = useCallback(async () => {
    setStarting(true);
    try {
      await ctx.runDiscoveryMining({
        scope,
        minScore,
        dataMode,
        batchSize,
        delaySeconds: delay,
        warningDays,
        validDays,
        includeNews,
      });
    } catch (error: any) {
      ctx.showToast("error", error?.message || t("discoveryCommandFailed"));
    } finally {
      setStarting(false);
    }
  }, [ctx, scope, minScore, dataMode, batchSize, delay, warningDays, validDays, includeNews]);

  const handlePause = useCallback(async () => {
    setPausing(true);
    try {
      await ctx.sendDiscoveryTaskCommand("pause");
    } catch (error: any) {
      ctx.showToast("error", error?.message || t("discoveryCommandFailed"));
    } finally {
      setPausing(false);
    }
  }, [ctx]);

  const handleResume = useCallback(async () => {
    setResuming(true);
    try {
      await ctx.sendDiscoveryTaskCommand("resume");
    } catch (error: any) {
      ctx.showToast("error", error?.message || t("discoveryCommandFailed"));
    } finally {
      setResuming(false);
    }
  }, [ctx]);

  const handleCancel = useCallback(async () => {
    setCancelling(true);
    try {
      await ctx.sendDiscoveryTaskCommand("cancel");
    } catch (error: any) {
      ctx.showToast("error", error?.message || t("discoveryCommandFailed"));
    } finally {
      setCancelling(false);
    }
  }, [ctx]);

  const handleRefresh = useCallback(async () => {
    setRefreshing(true);
    try {
      await ctx.refreshDiscoveryTasks();
    } catch (error: any) {
      ctx.showToast("error", error?.message || t("discoveryCommandFailed"));
    } finally {
      setRefreshing(false);
    }
  }, [ctx]);

  const handleCleanup = useCallback(async () => {
    setCleaning(true);
    try {
      await ctx.cleanupExpiredDiscoveryResults();
    } catch (error: any) {
      ctx.showToast("error", error?.message || t("discoveryCommandFailed"));
    } finally {
      setCleaning(false);
    }
  }, [ctx]);

  const setRowLoading = (symbolId: number, key: "watchlist" | "freeze" | "update", value: boolean) => {
    setRowActionLoading((prev) => ({
      ...prev,
      [symbolId]: { ...prev[symbolId], [key]: value },
    }));
  };

  const handleAddToWatchlist = useCallback(
    async (symbolId: number) => {
      setRowLoading(symbolId, "watchlist", true);
      try {
        await ctx.addSymbolToPrimaryWatchlist(symbolId);
        await ctx.loadWorkbench();
        ctx.showToast("success", t("discoveryAddedWatchlist"));
      } catch (error: any) {
        ctx.showToast("error", error?.message || t("discoveryCommandFailed"));
      } finally {
        setRowLoading(symbolId, "watchlist", false);
      }
    },
    [ctx]
  );

  const handleToggleFreeze = useCallback(
    async (resultId: number, isFrozen: boolean, symbolId: number) => {
      setRowLoading(symbolId, "freeze", true);
      try {
        await api.updateDiscoveryResult(resultId, { is_frozen: !isFrozen });
        await ctx.loadWorkbench();
        ctx.showToast("success", t("discoveryRowFrozen"));
      } catch (error: any) {
        ctx.showToast("error", error?.message || t("discoveryCommandFailed"));
      } finally {
        setRowLoading(symbolId, "freeze", false);
      }
    },
    [ctx]
  );

  const handleUpdateRow = useCallback(
    async (resultId: number, symbolId: number) => {
      setRowLoading(symbolId, "update", true);
      try {
        await api.refreshDiscoveryResult(resultId);
        await ctx.loadWorkbench();
        ctx.showToast("success", t("discoveryRowUpdated"));
      } catch (error: any) {
        ctx.showToast("error", error?.message || t("discoveryCommandFailed"));
      } finally {
        setRowLoading(symbolId, "update", false);
      }
    },
    [ctx]
  );

  const handleRowClick = useCallback(
    (symbolId: number) => {
      ctx.loadSymbolDetail(symbolId, { focus: true });
    },
    [ctx]
  );

  const percentValue = task?.percent ?? 0;
  const progressTitle = task?.message || t("discoveryIdle");
  const progressPct = `${percentValue}%${DOT}${template("totalProgress", {
    total: task?.total ?? 0,
    processed: task?.processed ?? 0,
  })}`;
  const meta = task
    ? `${task.status}${DOT}${template("scanCounters", {
        ok: task.ok_count,
        empty: task.empty_count,
        failed: task.failed_count,
        scored: task.scored_count,
      })}`
    : "-";

  const resultMeta = workbench
    ? `${t("candidates")}: ${workbench.candidates.length}`
    : "-";

  return (
    <div className="tab-container" data-tab-content="discovery">
      <section className="band discovery-band">
        <div className="panel wide">
          <div className="panel-head">
            <div>
              <p className="panel-kicker">{t("discoveryKicker")}</p>
              <h2>{t("discoveryTitle")}</h2>
            </div>
            <p className="panel-meta">
              {meta}
              {ctx.discoveryPolling ? ` ${DOT} ...` : ""}
            </p>
          </div>

          <div className="discovery-control-bar">
            <label className="inline-control">
              <span>{t("discoveryScope")}</span>
              <Select
                value={scope}
                onChange={(value) => setScope(value)}
                options={[
                  { value: "cn-stock", label: t("discoveryScopeCnStock") },
                  { value: "cn-etf", label: t("discoveryScopeCnEtf") },
                  { value: "us-stock", label: t("discoveryScopeUsStock") },
                  { value: "us-etf", label: t("discoveryScopeUsEtf") },
                ]}
              />
            </label>

            <label className="inline-control">
              <span>{t("minOpportunityScore")}</span>
              <InputNumber
                min={0}
                max={100}
                value={minScore}
                onChange={(value) => setMinScore(value ?? 0)}
              />
            </label>

            <label className="inline-control">
              <span>{t("discoveryDataMode")}</span>
              <Select
                value={dataMode}
                onChange={(value) => setDataMode(value)}
                options={[
                  { value: "cached", label: t("discoveryModeCached") },
                  { value: "sync", label: t("discoveryModeSync") },
                ]}
              />
            </label>

            <label className="inline-control">
              <span>{t("discoveryBatchSize")}</span>
              <InputNumber
                min={1}
                value={batchSize}
                onChange={(value) => setBatchSize(value ?? 1)}
              />
            </label>

            <label className="inline-control">
              <span>{t("discoveryDelay")}</span>
              <InputNumber
                min={0}
                step={0.05}
                value={delay}
                onChange={(value) => setDelay(value ?? 0)}
              />
            </label>

            <label className="inline-control">
              <span>{t("warningDays")}</span>
              <InputNumber
                min={1}
                value={warningDays}
                onChange={(value) => setWarningDays(value ?? 1)}
              />
            </label>

            <label className="inline-control">
              <span>{t("validDays")}</span>
              <InputNumber
                min={1}
                value={validDays}
                onChange={(value) => setValidDays(value ?? 1)}
              />
            </label>

            <label className="discovery-check">
              <Checkbox
                checked={includeNews}
                onChange={(e) => setIncludeNews(e.target.checked)}
              >
                {t("includeNewsScore")}
              </Checkbox>
            </label>

            <div className="discovery-actions">
              <Button id="discoveryRunButton" type="primary" loading={starting} onClick={handleStart} disabled={!canStart}>
                {t("startDiscovery")}
              </Button>
              <Button loading={pausing} onClick={handlePause} disabled={!canPause}>
                {t("pauseDiscovery")}
              </Button>
              <Button loading={resuming} onClick={handleResume} disabled={!canResume}>
                {t("resumeDiscovery")}
              </Button>
              <Button danger loading={cancelling} onClick={handleCancel} disabled={!canCancel}>
                {t("cancelDiscovery")}
              </Button>
              <Button loading={refreshing} onClick={handleRefresh}>{t("refreshResults")}</Button>
              <Button loading={cleaning} onClick={handleCleanup}>{t("cleanupExpired")}</Button>
            </div>
          </div>

          <div className="discovery-progress">
            <div className="discovery-progress-head">
              <strong>{progressTitle}</strong>
              <span>{progressPct}</span>
            </div>
            <div className="progress-track"><div className="progress-fill" style={{width: percentValue + "%"}} /></div>
            <div className="discovery-steps">
              {STEP_ORDER.map((step) => (
                <span
                  key={step}
                  data-discovery-step={step}
                  className={stepClassName(task, step)}
                >
                  {t(STEP_LABEL_KEYS[step])}
                </span>
              ))}
            </div>
          </div>
        </div>
      </section>

      <section className="band metrics-band">
        <div className="metric-grid discovery-metrics">
          <div className="metric-card">
            <span className="metric-label">{t("discoveryScope")}</span>
            <span className="metric-value">{scopeLabel(scope)}</span>
            <span className="metric-note">{scope}</span>
          </div>
          <div className="metric-card">
            <span className="metric-label">{t("discoveryUniverseTotal")}</span>
            <span className="metric-value">{scopeStats?.total_symbols ?? "-"}</span>
            <span className="metric-note">{t("items")}</span>
          </div>
          <div className="metric-card">
            <span className="metric-label">{t("discoveryCachedPool")}</span>
            <span className="metric-value">{scopeStats?.cached_symbols ?? "-"}</span>
            <span className="metric-note">{t("items")}</span>
          </div>
          <div className="metric-card">
            <span className="metric-label">{t("candidates")}</span>
            <span className="metric-value">{workbench?.candidates.length ?? 0}</span>
            <span className="metric-note">{t("items")}</span>
          </div>
          <div className="metric-card">
            <span className="metric-label">{t("discoveryCurrentRun")}</span>
            <span className="metric-value">
              {task ? `${task.processed}/${task.total}` : "0/0"}
            </span>
            <span className="metric-note">{t("items")}</span>
          </div>
          <div className="metric-card">
            <span className="metric-label">{t("messageScore")}</span>
            <span className="metric-value">{ctx.newsSnapshot?.symbols_total ?? 0}</span>
            <span className="metric-note">{t("items")}</span>
          </div>
        </div>
      </section>

      <section className="band">
        <div className="panel wide">
          <div className="panel-head">
            <div>
              <p className="panel-kicker">{t("discoveryResultKicker")}</p>
              <h2>{t("discoveryResults")}</h2>
            </div>
            <p className="panel-meta">{resultMeta}</p>
          </div>

          {/* 候选池 Tab 切换 */}
          {coverageHint && (
            <div className="coverage-hint">{coverageHint}</div>
          )}
          <div className="pool-tabs">
            {Object.entries(poolLabels).map(([key, cfg]) => (
              <button
                key={key}
                className={`pool-tab ${poolTab === key ? "pool-tab--active" : ""}`}
                style={{ borderColor: poolTab === key ? cfg.color : "transparent" }}
                onClick={() => setPoolTab(key as any)}
              >
                <span className="pool-tab-label">{ctx.locale === "zh-CN" ? cfg.zh : cfg.en}</span>
                <span className="pool-tab-count" style={{ backgroundColor: cfg.color }}>
                  {(candidatePools as any)[key]?.length ?? 0}
                </span>
              </button>
            ))}
          </div>

          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>{t("rank")}</th>
                  <th>{t("symbol")}</th>
                  <th>{t("finalOpportunityScore")}</th>
                  <th>{t("messageScore")}</th>
                  <th>{t("quality")}</th>
                  <th>{t("timing")}</th>
                  <th>{t("priority")}</th>
                  <th>{t("stage")}</th>
                  <th>{t("action")}</th>
                  <th>{t("position")}</th>
                  <th>{t("freshness")}</th>
                  <th>{t("operations")}</th>
                </tr>
              </thead>
              <tbody>
                {currentPool.map((item: any, index: number) => {
                const _inWatchlist = ctx.primaryWatchlistSymbolIds.has(item.symbol_id);
                const _resultId = item.scan_result_id ?? item.id;
                const _freshness = discoveryFreshness(item);
                const _ageDays = ageDays(item.created_at);
                const _isLowCredibility = _ageDays > 7 || !item.created_at;
                const _reasonTags: string[] = item.reason_tags ?? [];
                return (
                <tr
                  key={item.symbol_id}
                  className={[
                    "clickable",
                    item.is_frozen ? "discovery-row-frozen" : "",
                    _freshness.className === "warning" ? "discovery-row-warning" : "",
                    _isLowCredibility ? "discovery-row-low-credibility" : "",
                  ].filter(Boolean).join(" ")}
                  onClick={() => handleRowClick(item.symbol_id)}
                >
                  <td>{index + 1}</td>
                  <td>
                    <div className="symbol-title">
                      <span className="symbol-code">{item.symbol}</span>
                      <span className="symbol-name">{item.name}</span>
                    </div>
                    <div className="item-subline">{joinParts([regionShortLabel(item.region), assetTypeLabel(item.asset_type)])}</div>
                    {_reasonTags.length > 0 && (
                      <div className="item-reason-tags">
                        {_reasonTags.slice(0, 3).map((tag: string, i: number) => (
                          <span key={i} className="reason-tag">{tag}</span>
                        ))}
                      </div>
                    )}
                  </td>
                  <td>{score(opportunityScoreValue(item))}</td>
                  <td>{score(item.news_message_score)}</td>
                  <td>{score(item.quality_score)}</td>
                  <td>{score(item.timing_score)}</td>
                  <td>{score(item.priority_score)}</td>
                  <td><Tag className={badgeClass(item.stage)}>{stageLabel(item.stage)}</Tag></td>
                  <td><Tag className={badgeClass(item.action)}>{actionLabel(item.action)}</Tag></td>
                  <td>{percent(item.recommended_position_pct)}</td>
                  <td>
                    <span className={`freshness-chip ${_freshness.className}`}>
                      {_freshness.label}
                    </span>
                    {_isLowCredibility && (
                      <span className="credibility-badge credibility-low">
                        {ctx.locale === "zh-CN" ? "低可信" : "Low"}
                      </span>
                    )}
                  </td>
                  <td>
                    <Space size="small">
                      <Button
                        size="small"
                        loading={rowActionLoading[item.symbol_id]?.watchlist}
                        disabled={_inWatchlist}
                        onClick={(e: any) => { e.stopPropagation(); handleAddToWatchlist(item.symbol_id); }}
                      >
                        {_inWatchlist ? t("discoveryInWatchlist") : t("discoveryAddWatchlist")}
                      </Button>
                      <Button
                        size="small"
                        loading={rowActionLoading[item.symbol_id]?.freeze}
                        onClick={(e: any) => { e.stopPropagation(); if(_resultId) handleToggleFreeze(_resultId, !!item.is_frozen, item.symbol_id); }}
                      >
                        {item.is_frozen ? t("unfreeze") : t("freeze")}
                      </Button>
                      <Button
                        size="small"
                        loading={rowActionLoading[item.symbol_id]?.update}
                        onClick={(e: any) => { e.stopPropagation(); if(_resultId) handleUpdateRow(_resultId, item.symbol_id); }}
                      >
                        {t("updateCurrent")}
                      </Button>
                    </Space>
                  </td>
                </tr>
                );
                })}
              </tbody>
            </table>
          </div>
        </div>
      </section>
    </div>
  );
}
