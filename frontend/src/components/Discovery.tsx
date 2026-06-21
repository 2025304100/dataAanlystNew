import { useState, useMemo, useCallback } from "react";
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
  const [batchSize, setBatchSize] = useState(20);
  const [delay, setDelay] = useState(0.25);
  const [warningDays, setWarningDays] = useState(3);
  const [validDays, setValidDays] = useState(5);
  const [includeNews, setIncludeNews] = useState(true);

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

  const handleStart = useCallback(() => {
    ctx.runDiscoveryMining({
      scope,
      minScore,
      dataMode,
      batchSize,
      delaySeconds: delay,
      warningDays,
      validDays,
      includeNews,
    }).catch((error: any) => {
      ctx.showToast("error", error.message);
    });
  }, [ctx, scope, minScore, dataMode, batchSize, delay, warningDays, validDays, includeNews]);

  const handlePause = useCallback(() => {
    ctx.sendDiscoveryTaskCommand("pause").catch((error: any) => {
      ctx.showToast("error", error.message);
    });
  }, [ctx]);

  const handleResume = useCallback(() => {
    ctx.sendDiscoveryTaskCommand("resume").catch((error: any) => {
      ctx.showToast("error", error.message);
    });
  }, [ctx]);

  const handleCancel = useCallback(() => {
    ctx.sendDiscoveryTaskCommand("cancel").catch((error: any) => {
      ctx.showToast("error", error.message);
    });
  }, [ctx]);

  const handleRefresh = useCallback(() => {
    ctx.refreshDiscoveryTasks().catch((error: any) => {
      ctx.showToast("error", error.message);
    });
  }, [ctx]);

  const handleCleanup = useCallback(() => {
    ctx.cleanupExpiredDiscoveryResults().catch((error: any) => {
      ctx.showToast("error", error.message);
    });
  }, [ctx]);

  const handleAddToWatchlist = useCallback(
    async (symbolId: number) => {
      try {
        await ctx.addSymbolToPrimaryWatchlist(symbolId);
        await ctx.loadWorkbench();
        ctx.showToast("success", t("discoveryAddedWatchlist"));
      } catch (error: any) {
        ctx.showToast("error", error.message);
      }
    },
    [ctx]
  );

  const handleToggleFreeze = useCallback(
    async (resultId: number, isFrozen: boolean) => {
      try {
        await api.updateDiscoveryResult(resultId, { is_frozen: !isFrozen });
        await ctx.loadWorkbench();
        ctx.showToast("success", t("discoveryRowFrozen"));
      } catch (error: any) {
        ctx.showToast("error", error.message);
      }
    },
    [ctx]
  );

  const handleUpdateRow = useCallback(
    async (resultId: number) => {
      try {
        await api.refreshDiscoveryResult(resultId);
        await ctx.loadWorkbench();
        ctx.showToast("success", t("discoveryRowUpdated"));
      } catch (error: any) {
        ctx.showToast("error", error.message);
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
              <Button id="discoveryRunButton" type="primary" onClick={handleStart} disabled={!canStart}>
                {t("startDiscovery")}
              </Button>
              <Button onClick={handlePause} disabled={!canPause}>
                {t("pauseDiscovery")}
              </Button>
              <Button onClick={handleResume} disabled={!canResume}>
                {t("resumeDiscovery")}
              </Button>
              <Button danger onClick={handleCancel} disabled={!canCancel}>
                {t("cancelDiscovery")}
              </Button>
              <Button onClick={handleRefresh}>{t("refreshResults")}</Button>
              <Button onClick={handleCleanup}>{t("cleanupExpired")}</Button>
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
                  <th>{t("stage")}</th>
                  <th>{t("action")}</th>
                  <th>{t("position")}</th>
                  <th>{t("freshness")}</th>
                  <th>{t("operations")}</th>
                </tr>
              </thead>
              <tbody>
                {sortedResults.map((item: any) => {
                const _inWatchlist = ctx.primaryWatchlistSymbolIds.has(item.symbol_id);
                const _resultId = item.scan_result_id ?? item.id;
                const _freshness = discoveryFreshness(item);
                return (
                <tr
                  key={item.symbol_id}
                  className={[
                    "clickable",
                    item.is_frozen ? "discovery-row-frozen" : "",
                    _freshness.className === "warning" ? "discovery-row-warning" : "",
                  ].filter(Boolean).join(" ")}
                  onClick={() => handleRowClick(item.symbol_id)}
                >
                  <td>{item.rank_no ?? '-'}</td>
                  <td>
                    <div className="symbol-title">
                      <span className="symbol-code">{item.symbol}</span>
                      <span className="symbol-name">{item.name}</span>
                    </div>
                    <div className="item-subline">{joinParts([regionShortLabel(item.region), assetTypeLabel(item.asset_type)])}</div>
                  </td>
                  <td>{score(opportunityScoreValue(item))}</td>
                  <td>{score(item.news_message_score)}</td>
                  <td>{score(item.quality_score)}</td>
                  <td>{score(item.timing_score)}</td>
                  <td><Tag className={badgeClass(item.stage)}>{stageLabel(item.stage)}</Tag></td>
                  <td><Tag className={badgeClass(item.action)}>{actionLabel(item.action)}</Tag></td>
                  <td>{percent(item.recommended_position_pct)}</td>
                  <td><span className={`freshness-chip ${_freshness.className}`}>{_freshness.label}</span></td>
                  <td>
                    <Space size="small">
                      <Button
                        size="small"
                        disabled={_inWatchlist}
                        onClick={(e: any) => { e.stopPropagation(); handleAddToWatchlist(item.symbolId); }}
                      >
                        {_inWatchlist ? t("discoveryInWatchlist") : t("discoveryAddWatchlist")}
                      </Button>
                      <Button
                        size="small"
                        onClick={(e: any) => { e.stopPropagation(); if(_resultId) handleToggleFreeze(_resultId, !!item.is_frozen); }}
                      >
                        {item.is_frozen ? t("unfreeze") : t("freeze")}
                      </Button>
                      <Button
                        size="small"
                        onClick={(e: any) => { e.stopPropagation(); if(_resultId) handleUpdateRow(_resultId); }}
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
