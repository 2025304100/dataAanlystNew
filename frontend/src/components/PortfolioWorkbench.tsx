import { useMemo, useState, useEffect, useCallback } from "react";
import { Input, InputNumber, Button, Tag, Space, Modal, Dropdown, Empty, Skeleton, Table } from "antd";
import type { MenuProps } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useApp } from "../context/AppContext";
import { api } from "../api/client";
import { t, template, DOT, stageLabel, actionLabel } from "../i18n";
import {
  percent,
  score,
  money,
  joinParts,
  badgeClass,
  pnlClass,
  withFinalOpportunityScore,
  opportunityScoreValue,
  inferSymbolPayload,
} from "../utils/format";
import type { Position, AllocationSnapshot, WorkbenchCandidate } from "../types";

interface PortfolioWorkbenchProps {
  openMetricModal: (type: string) => void;
}

function positionsEqual(a: Position[], b: Position[]): boolean {
  if (a.length !== b.length) return false;
  const map = new Map(b.map((p) => [p.symbol_id, p]));
  return a.every((pos) => {
    const other = map.get(pos.symbol_id);
    if (!other) return false;
    return (
      pos.quantity === other.quantity &&
      pos.avg_cost === other.avg_cost &&
      pos.latest_price === other.latest_price &&
      pos.market_value === other.market_value &&
      pos.position_pct === other.position_pct &&
      pos.unrealized_pnl === other.unrealized_pnl &&
      pos.unrealized_pnl_pct === other.unrealized_pnl_pct
    );
  });
}

function ruleValuesEqual(
  rule: any,
  totalCapital: number | null,
  current: {
    ruleTotalCapital: number | null;
    ruleMaxSingle: number | null;
    ruleMaxStock: number | null;
    ruleMaxEtf: number | null;
    ruleMaxSector: number | null;
    ruleMaxLoss: number | null;
    ruleMaxOpenPositions: number | null;
  }
): boolean {
  return (
    (rule.max_single_position_pct != null ? rule.max_single_position_pct * 100 : null) === current.ruleMaxSingle &&
    (rule.max_stock_position_pct != null ? rule.max_stock_position_pct * 100 : null) === current.ruleMaxStock &&
    (rule.max_etf_position_pct != null ? rule.max_etf_position_pct * 100 : null) === current.ruleMaxEtf &&
    (rule.max_sector_position_pct != null ? rule.max_sector_position_pct * 100 : null) === current.ruleMaxSector &&
    (rule.max_loss_per_trade_pct != null ? rule.max_loss_per_trade_pct * 100 : null) === current.ruleMaxLoss &&
    (rule.max_open_positions ?? null) === current.ruleMaxOpenPositions &&
    totalCapital === current.ruleTotalCapital
  );
}

export default function PortfolioWorkbench({ openMetricModal }: PortfolioWorkbenchProps) {
  const ctx = useApp();
  const workbench = ctx.workbench;

  const scoredCandidates = useMemo(() => {
    if (!workbench) return [];
    return workbench.candidates
      .map((item) => withFinalOpportunityScore(item, ctx.newsSnapshot))
      .sort((a, b) => opportunityScoreValue(b) - opportunityScoreValue(a));
  }, [workbench, ctx.newsSnapshot]);

  const todayExecutable = useMemo(() => scoredCandidates.slice(0, 5), [scoredCandidates]);

  const watchQueue = useMemo(() => {
    if (!workbench) return [];
    const candidateIds = new Set(workbench.candidates.map((item) => item.symbol_id));
    return workbench.latest_scores
      .filter((item) => !candidateIds.has(item.symbol_id))
      .map((item) => withFinalOpportunityScore(item, ctx.newsSnapshot))
      .sort((a, b) => opportunityScoreValue(b) - opportunityScoreValue(a))
      .slice(0, 5);
  }, [workbench, ctx.newsSnapshot]);

  const todayMessages = useMemo(() => {
    if (!ctx.newsSnapshot) return [];
    return [...ctx.newsSnapshot.symbols]
      .sort((a, b) => Math.abs(b.message_score) - Math.abs(a.message_score))
      .slice(0, 5);
  }, [ctx.newsSnapshot]);

  const filteredCandidates = useMemo(() => {
    if (!workbench) return [];
    const search = ctx.candidateSearch.trim().toLowerCase();
    if (!search) return workbench.candidates;
    return workbench.candidates.filter(
      (item) =>
        item.symbol.toLowerCase().includes(search) ||
        item.name.toLowerCase().includes(search)
    );
  }, [workbench, ctx.candidateSearch]);

  // ── P2: 持仓管理 & 组合暴露 state ──
  const portfolioId = ctx.portfolioId ?? 1;
  const [positions, setPositions] = useState<Position[]>(workbench?.positions ?? []);
  const [positionsLoading, setPositionsLoading] = useState(false);
  const [allocation, setAllocation] = useState<AllocationSnapshot | null>(null);
  const [allocationLoading, setAllocationLoading] = useState(false);
  const [posSymbolCode, setPosSymbolCode] = useState("");
  const [posQuantity, setPosQuantity] = useState<number | null>(null);
  const [posAvgCost, setPosAvgCost] = useState<number | null>(null);
  const [savingPosition, setSavingPosition] = useState(false);
  const [deletingPositionSymbolId, setDeletingPositionSymbolId] = useState<number | null>(null);

  const [ruleTotalCapital, setRuleTotalCapital] = useState<number | null>(null);
  const [ruleMaxSingle, setRuleMaxSingle] = useState<number | null>(null);
  const [ruleMaxStock, setRuleMaxStock] = useState<number | null>(null);
  const [ruleMaxEtf, setRuleMaxEtf] = useState<number | null>(null);
  const [ruleMaxSector, setRuleMaxSector] = useState<number | null>(null);
  const [ruleMaxLoss, setRuleMaxLoss] = useState<number | null>(null);
  const [ruleMaxOpenPositions, setRuleMaxOpenPositions] = useState<number | null>(null);
  const [savingRule, setSavingRule] = useState(false);

  // ── P3: Data management state ──
  const [backingUp, setBackingUp] = useState(false);
  const [restoring, setRestoring] = useState(false);
  const [restoreModalOpen, setRestoreModalOpen] = useState(false);
  const [backupList, setBackupList] = useState<any[]>([]);
  const [loadingBackups, setLoadingBackups] = useState(false);

  const loadPositions = useCallback(async () => {
    setPositionsLoading(true);
    try {
      const data = await api.getPositions(portfolioId);
      setPositions(Array.isArray(data) ? (data as Position[]) : []);
    } catch {
      // ignore load error
    } finally {
      setPositionsLoading(false);
    }
  }, [portfolioId]);

  const loadAllocation = useCallback(async () => {
    setAllocationLoading(true);
    try {
      const data = await api.getAllocation(portfolioId);
      setAllocation(data as AllocationSnapshot);
    } catch {
      // ignore load error
    } finally {
      setAllocationLoading(false);
    }
  }, [portfolioId]);

  useEffect(() => {
    loadPositions();
    loadAllocation();
  }, [loadPositions, loadAllocation]);

  useEffect(() => {
    if (workbench?.positions && !positionsEqual(workbench.positions, positions)) {
      setPositions(workbench.positions);
    }
  }, [workbench, positions]);

  useEffect(() => {
    const rule = workbench?.active_rule;
    const totalCapital = workbench?.portfolio?.total_capital ?? null;
    if (!rule) return;
    if (
      ruleValuesEqual(rule, totalCapital, {
        ruleTotalCapital,
        ruleMaxSingle,
        ruleMaxStock,
        ruleMaxEtf,
        ruleMaxSector,
        ruleMaxLoss,
        ruleMaxOpenPositions,
      })
    ) {
      return;
    }
    setRuleMaxSingle(rule.max_single_position_pct != null ? rule.max_single_position_pct * 100 : null);
    setRuleMaxStock(rule.max_stock_position_pct != null ? rule.max_stock_position_pct * 100 : null);
    setRuleMaxEtf(rule.max_etf_position_pct != null ? rule.max_etf_position_pct * 100 : null);
    setRuleMaxSector(rule.max_sector_position_pct != null ? rule.max_sector_position_pct * 100 : null);
    setRuleMaxLoss(rule.max_loss_per_trade_pct != null ? rule.max_loss_per_trade_pct * 100 : null);
    setRuleMaxOpenPositions(rule.max_open_positions ?? null);
    if (totalCapital !== null) {
      setRuleTotalCapital(totalCapital);
    }
  }, [workbench, ruleTotalCapital, ruleMaxSingle, ruleMaxStock, ruleMaxEtf, ruleMaxSector, ruleMaxLoss, ruleMaxOpenPositions]);

  if (!workbench) {
    return (
      <div className="sub-tab-container" data-sub-content="portfolio-workbench">
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={t("noScanYet")}
          className="workbench-empty"
        />
      </div>
    );
  }

  const overview = workbench.overview;
  const account = workbench.account_summary;
  const latestScan = workbench.latest_scan;
  const activeRule = workbench.active_rule;
  const marketScope = workbench.market_scope;

  const todayMeta = `${t("candidates")}: ${workbench.candidates.length}${DOT}${
    ctx.newsSnapshot ? `${t("todayMessages")}: ${ctx.newsSnapshot.symbols_total}` : t("noNewsYet")
  }`;

  const accountMeta = account
    ? `${t("activeTrades")}: ${account.trade_count_7d}${DOT}${t("lastTrade")}: ${
        account.last_trade_at ? new Date(account.last_trade_at).toLocaleString() : "-"
      }`
    : "-";

  const candidateMeta = latestScan
    ? `${t("latestAutoScan")}: ${latestScan.run_name}${DOT}${latestScan.executable_count} / ${latestScan.total_results}`
    : t("noScanYet");

  const handleSymbolClick = (symbolId: number) => {
    ctx.loadSymbolDetail(symbolId, { focus: true });
  };

  // ── P2: 持仓录入 / 删除 / 规则保存 ──
  const handleUpsertPosition = async () => {
    const code = posSymbolCode.trim();
    if (!code || posQuantity == null || posAvgCost == null) return;
    try {
      setSavingPosition(true);
      const payload = inferSymbolPayload(code);
      let symbolId: number | null = null;
      const symbols = await api.getSymbols(payload.symbol);
      const found = symbols.find((item) => item.symbol.toUpperCase() === payload.symbol.toUpperCase());
      if (found) {
        symbolId = found.id;
      } else {
        const created = await api.createSymbol(payload);
        symbolId = created.id;
      }
      await api.upsertPosition(portfolioId, {
        symbol_id: symbolId,
        quantity: posQuantity,
        avg_cost: posAvgCost,
        asset_type: payload.asset_type,
        theme: payload.theme,
      });
      setPosSymbolCode("");
      setPosQuantity(null);
      setPosAvgCost(null);
      await loadPositions();
      await loadAllocation();
      await ctx.loadWorkbench();
      ctx.showToast("success", t("positionSaved"));
    } catch (error: any) {
      ctx.showToast("error", error?.message || t("positionSaveFailed"));
    } finally {
      setSavingPosition(false);
    }
  };

  const handleDeletePosition = async (symbolId: number) => {
    try {
      setDeletingPositionSymbolId(symbolId);
      await api.deletePosition(portfolioId, symbolId);
      await loadPositions();
      await loadAllocation();
      await ctx.loadWorkbench();
      ctx.showToast("success", t("positionDeleted"));
    } catch (error: any) {
      ctx.showToast("error", error?.message || t("positionSaveFailed"));
    } finally {
      setDeletingPositionSymbolId(null);
    }
  };

  const handleSaveRule = async () => {
    try {
      setSavingRule(true);
      await api.upsertPortfolioRule(portfolioId, {
        max_single_position_pct: ruleMaxSingle != null ? ruleMaxSingle / 100 : null,
        max_stock_position_pct: ruleMaxStock != null ? ruleMaxStock / 100 : null,
        max_etf_position_pct: ruleMaxEtf != null ? ruleMaxEtf / 100 : null,
        max_sector_position_pct: ruleMaxSector != null ? ruleMaxSector / 100 : null,
        max_loss_per_trade_pct: ruleMaxLoss != null ? ruleMaxLoss / 100 : null,
        max_open_positions: ruleMaxOpenPositions,
        total_capital: ruleTotalCapital,
      });
      await ctx.loadWorkbench();
      ctx.showToast("success", t("ruleSavedOk"));
    } catch (error: any) {
      ctx.showToast("error", error?.message || t("ruleSaveFailed"));
    } finally {
      setSavingRule(false);
    }
  };

  // ── P3: Data management handlers ──
  const handleBackupNow = async () => {
    try {
      setBackingUp(true);
      await api.backupDatabase();
      ctx.showToast("success", t("backupCreated"));
    } catch (error: any) {
      ctx.showToast("error", error?.message || t("backupFailed"));
    } finally {
      setBackingUp(false);
    }
  };

  const handleExportData = (dataType: string) => {
    // CSV export: open in browser to trigger download
    window.open(`/api/v1/system/export/${dataType}?portfolio_id=${portfolioId}`);
  };

  const handleOpenRestoreModal = async () => {
    setRestoreModalOpen(true);
    try {
      setLoadingBackups(true);
      const data = await api.listBackups();
      const list = Array.isArray(data) ? data : (data as any)?.backups ?? [];
      setBackupList(list);
    } catch (error: any) {
      ctx.showToast("error", error?.message || t("restoreFailed"));
      setBackupList([]);
    } finally {
      setLoadingBackups(false);
    }
  };

  const handleRestoreBackup = async (backupPath: string) => {
    const confirmed = window.confirm(t("restoreConfirm"));
    if (!confirmed) return;
    try {
      setRestoring(true);
      await api.restoreDatabase(backupPath);
      ctx.showToast("success", t("restoreSuccess"));
      setRestoreModalOpen(false);
      await ctx.loadWorkbench();
    } catch (error: any) {
      ctx.showToast("error", error?.message || t("restoreFailed"));
    } finally {
      setRestoring(false);
    }
  };

  const exportMenuItems: MenuProps["items"] = [
    { key: "journals", label: t("exportJournals"), onClick: () => handleExportData("journals") },
    { key: "positions", label: t("exportPositions"), onClick: () => handleExportData("positions") },
    { key: "scan_results", label: t("exportScans"), onClick: () => handleExportData("scan_results") },
    { key: "trade_setups", label: t("exportTradeSetups"), onClick: () => handleExportData("trade_setups") },
  ];

  // 暴露明细派生值
  const sectorThreshold = (activeRule?.max_sector_position_pct ?? 0.3) * 100;
  const sectorExposureEntries = allocation?.sector_exposure
    ? Object.entries(allocation.sector_exposure)
        .filter(([, pct]) => Number(pct) * 100 > sectorThreshold)
        .sort((a, b) => Number(b[1]) - Number(a[1]))
    : [];
  const topPositionItem = positions.length
    ? positions.reduce((top, p) => ((p.position_pct ?? 0) > (top.position_pct ?? 0) ? p : top), positions[0])
    : null;

  const positionColumns: ColumnsType<Position> = [
    {
      title: t("symbol"),
      key: "symbol",
      render: (_, item) => (
        <div className="symbol-title">
          <span className="symbol-code">{item.symbol}</span>
          <span className="symbol-name">{item.name}</span>
        </div>
      ),
    },
    { title: t("quantity"), dataIndex: "quantity", key: "quantity" },
    { title: t("avgCost"), dataIndex: "avg_cost", key: "avg_cost", render: (v) => score(v) },
    { title: t("currentPrice"), dataIndex: "latest_price", key: "latest_price", render: (v) => score(v) },
    { title: t("marketValue"), dataIndex: "market_value", key: "market_value", render: (v) => money(v) },
    { title: t("position"), dataIndex: "position_pct", key: "position_pct", render: (v) => percent(v) },
    {
      title: t("unrealizedPnl"),
      dataIndex: "unrealized_pnl",
      key: "unrealized_pnl",
      render: (v) => <span className={pnlClass(v)}>{money(v)}</span>,
    },
    {
      title: t("unrealizedPnlPct"),
      dataIndex: "unrealized_pnl_pct",
      key: "unrealized_pnl_pct",
      render: (v) => <span className={pnlClass(v)}>{percent(v)}</span>,
    },
    {
      title: t("operations"),
      key: "operations",
      render: (_, item) => (
        <Button
          className="delete-btn"
          size="small"
          danger
          loading={deletingPositionSymbolId === item.symbol_id}
          onClick={(e) => { e.stopPropagation(); handleDeletePosition(item.symbol_id); }}
          aria-label={template("deletePositionFor", { symbol: item.symbol })}
        >
          {t("deletePosition")}
        </Button>
      ),
    },
  ];

  const candidateColumns: ColumnsType<WorkbenchCandidate> = [
    {
      title: t("rank"),
      key: "rank",
      width: 70,
      render: (_, item, index) => item.rank_no ?? index + 1,
    },
    {
      title: t("symbol"),
      key: "symbol",
      render: (_, item) => (
        <div className="symbol-title">
          <span className="symbol-code">{item.symbol}</span>
          <span className="symbol-name">{item.name}</span>
        </div>
      ),
    },
    { title: t("quality"), dataIndex: "quality_score", key: "quality_score", sorter: (a, b) => a.quality_score - b.quality_score, render: (v) => score(v), width: 90 },
    { title: t("timing"), dataIndex: "timing_score", key: "timing_score", sorter: (a, b) => a.timing_score - b.timing_score, render: (v) => score(v), width: 90 },
    { title: t("stage"), dataIndex: "stage", key: "stage", render: (v) => <Tag className={badgeClass(v)}>{stageLabel(v)}</Tag>, width: 100 },
    { title: t("action"), dataIndex: "action", key: "action", render: (v) => <Tag className={badgeClass(v)}>{actionLabel(v)}</Tag>, width: 100 },
    { title: t("position"), dataIndex: "recommended_position_pct", key: "recommended_position_pct", sorter: (a, b) => (a.recommended_position_pct ?? 0) - (b.recommended_position_pct ?? 0), render: (v) => percent(v), width: 100 },
  ];

  const rowKeyboardHandler = (symbolId: number) => (e: React.KeyboardEvent<HTMLTableRowElement>) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      handleSymbolClick(symbolId);
    }
  };

  return (
    <div className="sub-tab-container" data-sub-content="portfolio-workbench">
      <section className="band today-band">
        <div className="panel wide">
          <div className="panel-head">
            <div>
              <p className="panel-kicker">{ctx.locale === "zh-CN" ? "决策路径" : "Decision Path"}</p>
              <h2>{t("todayOpportunities")}</h2>
            </div>
            <p className="panel-meta">{todayMeta}</p>
          </div>
          <div className="today-grid">
            <div className="today-column">
              <p className="panel-kicker">{t("todayExecutable")}</p>
              <div className="today-list">
                {todayExecutable.length === 0 ? (
                  <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t("noCandidates")} />
                ) : (
                  todayExecutable.map((item) => (
                    <Button
                      key={item.symbol_id}
                      type="text"
                      className="today-item"
                      data-symbol-id={item.symbol_id}
                      onClick={() => handleSymbolClick(item.symbol_id)}
                      aria-label={template("viewSymbolDetail", { symbol: item.symbol })}
                    >
                      <div>
                        <div className="symbol-title">
                          <span className="symbol-code">{item.symbol}</span>
                          <span className="symbol-name">{item.name}</span>
                        </div>
                        <p className="item-subline">
                          {joinParts([stageLabel(item.stage), actionLabel(item.action)])}
                        </p>
                      </div>
                      <span className="today-score">{score(opportunityScoreValue(item))}</span>
                    </Button>
                  ))
                )}
              </div>
            </div>

            <div className="today-column">
              <p className="panel-kicker">{t("todayWatch")}</p>
              <div className="today-list">
                {watchQueue.length === 0 ? (
                  <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t("noScores")} />
                ) : (
                  watchQueue.map((item) => (
                    <Button
                      key={item.symbol_id}
                      type="text"
                      className="today-item"
                      data-symbol-id={item.symbol_id}
                      onClick={() => handleSymbolClick(item.symbol_id)}
                      aria-label={template("viewSymbolDetail", { symbol: item.symbol })}
                    >
                      <div>
                        <div className="symbol-title">
                          <span className="symbol-code">{item.symbol}</span>
                          <span className="symbol-name">{item.name}</span>
                        </div>
                        <p className="item-subline">
                          {joinParts([stageLabel(item.stage), actionLabel(item.action)])}
                        </p>
                      </div>
                      <span className="today-score">{score(opportunityScoreValue(item))}</span>
                    </Button>
                  ))
                )}
              </div>
            </div>

            <div className="today-column">
              <p className="panel-kicker">{t("todayMessages")}</p>
              <div className="today-list">
                {todayMessages.length === 0 ? (
                  <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t("noNewsYet")} />
                ) : (
                  todayMessages.map((item) => (
                    <Button
                      key={item.symbol_id}
                      type="text"
                      className="today-item"
                      data-symbol-id={item.symbol_id}
                      onClick={() => handleSymbolClick(item.symbol_id)}
                      aria-label={template("viewSymbolDetail", { symbol: item.symbol })}
                    >
                      <div>
                        <div className="symbol-title">
                          <span className="symbol-code">{item.symbol}</span>
                          <span className="symbol-name">{item.name}</span>
                        </div>
                        <p className="item-subline">{item.latest_title}</p>
                      </div>
                      <span className="today-score">{score(item.message_score)}</span>
                    </Button>
                  ))
                )}
              </div>
            </div>
          </div>
        </div>
      </section>

      <section className="band metrics-band">
        <div className="metric-grid">
          <Button type="text" className="metric-card metric-action" onClick={() => openMetricModal("symbols")} aria-label={template("metricActionView", { label: t("trackedUniverse") })}>
            <span className="metric-label">{t("trackedUniverse")}</span>
            <span className="metric-value">{marketScope?.filtered_symbols ?? overview.symbols_count}</span>
            <span className="metric-note">{t("items")}</span>
          </Button>
          <Button type="text" className="metric-card metric-action" onClick={() => openMetricModal("watchlists")} aria-label={template("metricActionView", { label: t("watchlists") })}>
            <span className="metric-label">{t("watchlists")}</span>
            <span className="metric-value">{overview.watchlists_count}</span>
            <span className="metric-note">{t("items")}</span>
          </Button>
          <div className="metric-card">
            <span className="metric-label">{t("portfolioUsage")}</span>
            <span className="metric-value">{percent(overview.total_position_pct)}</span>
            <span className="metric-note">{t("allIn")}</span>
          </div>
          <div className="metric-card">
            <span className="metric-label">{t("cash")}</span>
            <span className="metric-value">{percent(overview.cash_pct)}</span>
            <span className="metric-note">{t("reserveLeft")}</span>
          </div>
          <Button type="text" className="metric-card metric-action" onClick={() => openMetricModal("candidates")} aria-label={template("metricActionView", { label: t("candidates") })}>
            <span className="metric-label">{t("candidates")}</span>
            <span className="metric-value">{workbench.candidates.length}</span>
            <span className="metric-note">{t("items")}</span>
          </Button>
          <div className="metric-card">
            <span className="metric-label">{t("maxSingle")}</span>
            <span className="metric-value">{percent(activeRule?.max_single_position_pct)}</span>
            <span className="metric-note">{t("guardrails")}</span>
          </div>
        </div>
      </section>

      <div className="workbench-columns">
        <div className="workbench-main">
          <section className="band account-band">
            <div className="panel">
              <div className="panel-head">
                <div>
                  <p className="panel-kicker">{t("simAccount")}</p>
                  <h2>{t("accountSummary")}</h2>
                </div>
                <p className="panel-meta">{accountMeta}</p>
              </div>
              {account ? (
                <div className="account-grid">
                  <div className="account-item">
                    <span className="metric-label">{t("accountEquity")}</span>
                    <span className="metric-value">{money(account.total_equity)}</span>
                  </div>
                  <div className="account-item">
                    <span className="metric-label">{t("accountCash")}</span>
                    <span className="metric-value">{money(account.available_cash)}</span>
                  </div>
                  <div className="account-item">
                    <span className="metric-label">{t("accountMarketValue")}</span>
                    <span className="metric-value">{money(account.market_value)}</span>
                  </div>
                  <div className="account-item">
                    <span className="metric-label">{t("accountInvested")}</span>
                    <span className="metric-value">{percent(account.invested_pct)}</span>
                  </div>
                  <div className="account-item">
                    <span className="metric-label">{t("accountRealizedPnl")}</span>
                    <span className={`metric-value ${pnlClass(account.realized_pnl)}`}>
                      {money(account.realized_pnl)}
                    </span>
                  </div>
                  <div className="account-item">
                    <span className="metric-label">{t("accountUnrealizedPnl")}</span>
                    <span className={`metric-value ${pnlClass(account.unrealized_pnl)}`}>
                      {money(account.unrealized_pnl)}
                    </span>
                  </div>
                </div>
              ) : (
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t("noTrades")} />
              )}
            </div>
          </section>

          <section className="band holdings-section">
            <div className="panel">
              <div className="panel-head">
                <div>
                  <p className="panel-kicker">{t("holdings")}</p>
                  <h2>{t("holdingsSection")}</h2>
                </div>
                <p className="panel-meta">
                  {joinParts([
                    `${t("positionCountLabel")}: ${positions.length}`,
                    allocation?.position_count != null ? `${t("positionCountLabel")}: ${allocation.position_count}` : null,
                  ])}
                </p>
              </div>
              <div className="holdings-grid">
                <div className="position-form">
                  <p className="form-kicker">{t("addPosition")}</p>
                  <label>
                    <span>{t("symbolCode")}</span>
                    <Input
                      value={posSymbolCode}
                      onChange={(e) => setPosSymbolCode(e.target.value)}
                      placeholder={t("searchSymbolPlaceholder")}
                      allowClear
                    />
                  </label>
                  <label>
                    <span>{t("quantity")}</span>
                    <InputNumber
                      value={posQuantity}
                      onChange={(v) => setPosQuantity(v)}
                      min={0}
                      style={{ width: "100%" }}
                      placeholder="0"
                    />
                  </label>
                  <label>
                    <span>{t("avgCost")}</span>
                    <InputNumber
                      value={posAvgCost}
                      onChange={(v) => setPosAvgCost(v)}
                      min={0}
                      step={0.01}
                      style={{ width: "100%" }}
                      placeholder="0.00"
                    />
                  </label>
                  <div className="position-form-actions">
                    <Space>
                      <Button
                        type="primary"
                        loading={savingPosition}
                        disabled={!posSymbolCode.trim() || posQuantity == null || posAvgCost == null}
                        onClick={handleUpsertPosition}
                      >
                        {t("addPosition")}
                      </Button>
                    </Space>
                  </div>
                </div>

                <div className="position-table">
                  <div className="table-wrap">
                    {positionsLoading ? (
                      <Skeleton active paragraph={{ rows: 4 }} />
                    ) : (
                      <Table<Position>
                        columns={positionColumns}
                        dataSource={positions}
                        rowKey="symbol_id"
                        pagination={false}
                        locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t("positionsTableEmpty")} /> }}
                        onRow={(record) => ({
                          onClick: () => handleSymbolClick(record.symbol_id),
                          onKeyDown: rowKeyboardHandler(record.symbol_id),
                          tabIndex: 0,
                          role: "button",
                          "aria-label": template("viewSymbolDetail", { symbol: record.symbol }),
                        })}
                      />
                    )}
                  </div>
                </div>
              </div>
            </div>
          </section>

          <section className="band">
            <div className="panel">
              <div className="panel-head">
                <div>
                  <p className="panel-kicker">{t("exposureKicker")}</p>
                  <h2>{t("portfolioExposure")}</h2>
                </div>
                <p className="panel-meta">{activeRule ? activeRule.rule_name : t("noActiveRule")}</p>
              </div>
              <div className="exposure-rule-grid">
                <div className="exposure-panel">
                  <p className="exposure-kicker">{t("exposureKicker")}</p>
                  {allocationLoading ? (
                    <Skeleton active paragraph={{ rows: 3 }} />
                  ) : allocation ? (
                    <>
                      <div className="exposure-stats">
                        <div className="exposure-stat">
                          <span className="metric-label">{t("usedPosition")}</span>
                          <span className="metric-value">{percent(allocation.total_position_pct)}</span>
                        </div>
                        <div className="exposure-stat">
                          <span className="metric-label">{t("cashPct")}</span>
                          <span className="metric-value">
                            {percent(allocation.cash_pct ?? (allocation.total_position_pct != null ? 1 - allocation.total_position_pct : null))}
                          </span>
                        </div>
                        <div className="exposure-stat">
                          <span className="metric-label">{t("stockExposure")}</span>
                          <span className="metric-value">{percent(allocation.stock_position_pct)}</span>
                        </div>
                        <div className="exposure-stat">
                          <span className="metric-label">{t("etfExposure")}</span>
                          <span className="metric-value">{percent(allocation.etf_position_pct)}</span>
                        </div>
                      </div>

                      <div className="exposure-block">
                        <p className="exposure-block-title">{t("sectorConcentration")}</p>
                        {sectorExposureEntries.length === 0 ? (
                          <span className="item-subline">{t("noPositions")}</span>
                        ) : (
                          <div className="exposure-chip-list">
                            {sectorExposureEntries.map(([sector, pct]) => (
                              <span
                                key={sector}
                                className={`exposure-chip ${Number(pct) * 100 > (activeRule?.max_sector_position_pct ?? 0.3) * 100 * 1.2 ? "danger" : ""}`}
                              >
                                {sector} {percent(Number(pct))}
                              </span>
                            ))}
                          </div>
                        )}
                      </div>

                      <div className="exposure-block">
                        <p className="exposure-block-title">{t("topPosition")}</p>
                        {topPositionItem ? (
                          <div className="exposure-top-row">
                            <strong>{topPositionItem.symbol}</strong>
                            <span className="symbol-name">{topPositionItem.name}</span>
                            <span className={pnlClass(topPositionItem.position_pct)}>
                              {percent(topPositionItem.position_pct)}
                            </span>
                          </div>
                        ) : (
                          <span className="item-subline">{t("noPositions")}</span>
                        )}
                      </div>
                    </>
                  ) : (
                    <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t("noAllocation")} />
                  )}
                </div>

                <div className="rule-config-panel">
                  <p className="rule-config-kicker">{t("ruleConfigPanel")}</p>
                  <label>
                    <span>{t("totalCapital")}</span>
                    <InputNumber
                      value={ruleTotalCapital}
                      onChange={(v) => setRuleTotalCapital(v)}
                      min={0}
                      style={{ width: "100%" }}
                      placeholder="0"
                    />
                  </label>
                  <div className="rule-config-grid-compact">
                    <label>
                      <span>{t("maxSinglePct")}</span>
                      <InputNumber
                        value={ruleMaxSingle}
                        onChange={(v) => setRuleMaxSingle(v)}
                        min={0}
                        max={100}
                        step={1}
                        style={{ width: "100%" }}
                        placeholder="0"
                      />
                    </label>
                    <label>
                      <span>{t("maxStockPct")}</span>
                      <InputNumber
                        value={ruleMaxStock}
                        onChange={(v) => setRuleMaxStock(v)}
                        min={0}
                        max={100}
                        step={1}
                        style={{ width: "100%" }}
                        placeholder="0"
                      />
                    </label>
                    <label>
                      <span>{t("maxEtfPct")}</span>
                      <InputNumber
                        value={ruleMaxEtf}
                        onChange={(v) => setRuleMaxEtf(v)}
                        min={0}
                        max={100}
                        step={1}
                        style={{ width: "100%" }}
                        placeholder="0"
                      />
                    </label>
                    <label>
                      <span>{t("maxSectorPct")}</span>
                      <InputNumber
                        value={ruleMaxSector}
                        onChange={(v) => setRuleMaxSector(v)}
                        min={0}
                        max={100}
                        step={1}
                        style={{ width: "100%" }}
                        placeholder="0"
                      />
                    </label>
                    <label>
                      <span>{t("maxLossPct")}</span>
                      <InputNumber
                        value={ruleMaxLoss}
                        onChange={(v) => setRuleMaxLoss(v)}
                        min={0}
                        max={100}
                        step={0.5}
                        style={{ width: "100%" }}
                        placeholder="0"
                      />
                    </label>
                    <label>
                      <span>{t("maxOpenPositions")}</span>
                      <InputNumber
                        value={ruleMaxOpenPositions}
                        onChange={(v) => setRuleMaxOpenPositions(v)}
                        min={0}
                        step={1}
                        style={{ width: "100%" }}
                        placeholder="0"
                      />
                    </label>
                  </div>
                  <div className="rule-config-actions">
                    <Button type="primary" loading={savingRule} onClick={handleSaveRule}>
                      {t("saveRule")}
                    </Button>
                  </div>
                </div>
              </div>
            </div>
          </section>

          {/* P3: Data Management */}
          <section className="band">
            <div className="panel">
              <div className="panel-head">
                <div>
                  <p className="panel-kicker">{t("backupExport")}</p>
                  <h2>{t("dataManagement")}</h2>
                </div>
              </div>
              <div className="data-management-section">
                <div className="data-management-actions">
                  <Button type="primary" loading={backingUp} onClick={handleBackupNow}>
                    {t("backupNow")}
                  </Button>
                  <Dropdown menu={{ items: exportMenuItems }} placement="bottomLeft">
                    <Button>{t("exportData")}</Button>
                  </Dropdown>
                  <Button
                    danger
                    loading={restoring}
                    onClick={handleOpenRestoreModal}
                  >
                    {t("restoreBackup")}
                  </Button>
                </div>
              </div>
            </div>
          </section>

          <section className="band">
            <div className="panel">
              <div className="panel-head">
                <div>
                  <p className="panel-kicker">{t("candidates")}</p>
                  <h2>{t("candidateList")}</h2>
                </div>
                <div className="panel-head-actions">
                  <Input
                    className="search-input"
                    placeholder={t("searchCandidates")}
                    allowClear
                    value={ctx.candidateSearch}
                    onChange={(e) => ctx.setCandidateSearch(e.target.value)}
                  />
                  <p className="panel-meta">{candidateMeta}</p>
                </div>
              </div>
              <div className="table-wrap">
                <Table<WorkbenchCandidate>
                  columns={candidateColumns}
                  dataSource={filteredCandidates}
                  rowKey="symbol_id"
                  pagination={{ pageSize: 20, hideOnSinglePage: true }}
                  locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t("candidatesTableEmpty")} /> }}
                  onRow={(record) => ({
                    onClick: () => handleSymbolClick(record.symbol_id),
                    onKeyDown: rowKeyboardHandler(record.symbol_id),
                    tabIndex: 0,
                    role: "button",
                    "aria-label": template("viewSymbolDetail", { symbol: record.symbol }),
                    className: `clickable ${ctx.activeSymbolId === record.symbol_id ? "active" : ""}`,
                  })}
                />
              </div>
            </div>
          </section>
        </div>

        <div className="workbench-side">
          <section className="band">
            <div className="panel">
              <div className="panel-head">
                <div>
                  <p className="panel-kicker">{t("scoreboard")}</p>
                  <h2>{t("latestScores")}</h2>
                </div>
              </div>
              {workbench.latest_scores.length === 0 ? (
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t("noScores")} />
              ) : (
                <div className="list">
                  {workbench.latest_scores.map((item) => (
                    <div
                      key={item.symbol_id}
                      className={`list-item clickable ${ctx.activeSymbolId === item.symbol_id ? "active" : ""}`}
                      onClick={() => handleSymbolClick(item.symbol_id)}
                    >
                      <div className="item-topline">
                        <div className="symbol-title">
                          <span className="symbol-code">{item.symbol}</span>
                          <span className="symbol-name">{item.name}</span>
                        </div>
                        <Tag className={badgeClass(item.stage)}>{stageLabel(item.stage)}</Tag>
                      </div>
                      <div className="item-subline">
                        {joinParts([
                          `${t("quality")}: ${score(item.quality_score)}`,
                          `${t("timing")}: ${score(item.timing_score)}`,
                        ])}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </section>
        </div>
      </div>

      {/* P3: Restore Backup Modal */}
      <Modal
        title={t("restoreBackup")}
        open={restoreModalOpen}
        onCancel={() => setRestoreModalOpen(false)}
        footer={null}
        width={560}
      >
        {loadingBackups ? (
          <Skeleton active paragraph={{ rows: 3 }} />
        ) : backupList.length === 0 ? (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={t("noBackups")} />
        ) : (
          <div className="list">
            {backupList.map((backup: any, idx: number) => {
              const path = typeof backup === "string" ? backup : (backup.path ?? backup.backup_path ?? backup.name ?? "");
              const label = typeof backup === "string" ? backup : (backup.name ?? backup.path ?? backup.backup_path ?? `Backup ${idx + 1}`);
              const createdAt = typeof backup === "string" ? null : (backup.created_at ?? backup.size ?? null);
              return (
                <article key={idx} className="list-item">
                  <div className="item-topline">
                    <strong>{label}</strong>
                    <Button
                      size="small"
                      type="primary"
                      loading={restoring}
                      onClick={() => handleRestoreBackup(path)}
                    >
                      {t("restoreBackup")}
                    </Button>
                  </div>
                  {createdAt && (
                    <div className="item-subline">{String(createdAt)}</div>
                  )}
                </article>
              );
            })}
          </div>
        )}
      </Modal>
    </div>
  );
}
