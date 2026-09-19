import { useState, useEffect, useCallback } from "react";
import { Button } from "antd";
import { useApp } from "../context/AppContext";
import { api } from "../api/client";
import { t, DOT, regionShortLabel, assetTypeLabel, actionLabel, watchlistTypeLabel } from "../i18n";
import { score, joinParts } from "../utils/format";

interface MetricModalProps {
  type: string;
  onClose: () => void;
}

export default function MetricModal({ type, onClose }: MetricModalProps) {
  const ctx = useApp();
  const [items, setItems] = useState<any[]>([]);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [busyButton, setBusyButton] = useState<string | null>(null);

  // ESC key handler
  useEffect(() => {
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  }, [onClose]);

  // Load items when modal opens
  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      try {
        let loaded: any[] = [];
        if (type === "symbols") {
          loaded = await ctx.fetchVisibleSymbols();
        } else if (type === "watchlists") {
          loaded = ctx.workbench?.watchlists ?? [];
        } else if (type === "candidates") {
          loaded = ctx.workbench?.candidates ?? [];
        }
        if (cancelled) return;
        setItems(loaded);
        if (loaded.length) {
          const firstId = type === "candidates" ? loaded[0].symbol_id : loaded[0].id;
          setSelectedId(firstId);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [type]);

  // Load watchlist items when a watchlist is selected
  useEffect(() => {
    if (type !== "watchlists" || selectedId == null) return;
    ctx.fetchWatchlistItems(selectedId).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [type, selectedId]);

  const handleBackdropClick = useCallback(
    (e: React.MouseEvent<HTMLDivElement>) => {
      if (e.target === e.currentTarget) onClose();
    },
    [onClose]
  );

  const title =
    type === "watchlists"
      ? t("watchlistListTitle")
      : type === "candidates"
      ? t("candidateList")
      : t("symbolList");

  const getItemId = (item: any) => (type === "candidates" ? item.symbol_id : item.id);

  const getItemTitle = (item: any) =>
    type === "watchlists" ? item.name : `${item.symbol}${DOT}${item.name}`;

  const getItemMeta = (item: any) => {
    if (type === "watchlists") {
      return `${watchlistTypeLabel(item.list_type)}${DOT}${item.item_count} ${t("items")}`;
    }
    if (type === "candidates") {
      return joinParts([
        `Rank ${item.rank_no ?? "-"}`,
        regionShortLabel(item.region),
        assetTypeLabel(item.asset_type),
        `${t("quality")} ${score(item.quality_score)}`,
        `${t("timing")} ${score(item.timing_score)}`,
        actionLabel(item.action),
      ]);
    }
    return joinParts([
      regionShortLabel(item.region),
      item.market,
      assetTypeLabel(item.asset_type),
      item.theme,
    ]);
  };

  const selected = items.find((item) => getItemId(item) === selectedId) ?? null;

  const handleViewDetail = useCallback(async () => {
    if (!selected) return;
    const itemId = getItemId(selected);
    onClose();
    await ctx.loadSymbolDetail(itemId, { focus: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected, ctx, onClose]);

  const handleAddToWatchlist = useCallback(async () => {
    if (!selected) return;
    const itemId = getItemId(selected);
    setBusyButton("addToWatchlist");
    try {
      await ctx.addSymbolToPrimaryWatchlist(itemId);
      await ctx.loadWorkbench();
    } catch (error: any) {
      ctx.showToast("error", error.message);
    } finally {
      setBusyButton(null);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected, ctx]);

  const handleSyncSelected = useCallback(async () => {
    if (!selected) return;
    setBusyButton("syncSelected");
    try {
      await api.syncMarketData({
        scope: "symbols",
        symbol_ids: [selected.id ?? selected.symbol_id],
        asset_types: [selected.asset_type],
        adjust: "qfq",
        auto_scan: true,
        portfolio_id: ctx.portfolioId,
        portfolio_rule_id: ctx.workbench?.active_rule?.id ?? null,
      });
      await ctx.loadWorkbench();
    } catch (error: any) {
      ctx.showToast("error", error.message);
    } finally {
      setBusyButton(null);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected, ctx]);

  const handleScanSelected = useCallback(async () => {
    if (!selected) return;
    setBusyButton("scanSelected");
    try {
      await api.createScanRun({
        portfolio_id: ctx.portfolioId,
        portfolio_rule_id: ctx.workbench?.active_rule?.id ?? null,
        run_name: "manual-single-scan",
        scope_snapshot: {
          symbol_ids: [selected.id ?? selected.symbol_id],
          asset_types: [selected.asset_type],
          markets: [selected.market],
        },
      });
      await ctx.loadWorkbench();
    } catch (error: any) {
      ctx.showToast("error", error.message);
    } finally {
      setBusyButton(null);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected, ctx]);

  const handleGeneratePlan = useCallback(async () => {
    if (!selected) return;
    const itemId = getItemId(selected);
    onClose();
    await ctx.loadSymbolDetail(itemId, { focus: true });
    await ctx.generateTradeSetup();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected, ctx, onClose]);

  const handleOpenWatchlist = useCallback(() => {
    onClose();
  }, [onClose]);

  const handleAddActiveSymbol = useCallback(async () => {
    if (selectedId == null || ctx.activeSymbolId == null) return;
    setBusyButton("addActiveSymbol");
    try {
      await ctx.addSymbolToWatchlist(selectedId, ctx.activeSymbolId);
      await ctx.fetchWatchlistItems(selectedId);
    } catch (error: any) {
      ctx.showToast("error", error.message);
    } finally {
      setBusyButton(null);
    }
  }, [selectedId, ctx]);

  const watchlistItems =
    type === "watchlists" && selectedId != null ? ctx.watchlistItems[selectedId] ?? [] : [];

  return (
    <div className="modal-backdrop" onClick={handleBackdropClick}>
      <section className="modal-panel" role="dialog" aria-modal="true">
        <header className="modal-head">
          <div>
            <p className="panel-kicker">{t("listActions")}</p>
            <h2>{title}</h2>
          </div>
          <Button type="text" aria-label={t("close")} onClick={onClose}>
            &times;
          </Button>
        </header>
        <div className="modal-workspace">
          <div className="modal-list">
            {loading ? (
              <div className="empty">{t("syncing")}</div>
            ) : items.length === 0 ? (
              <div className="empty">{t("modalEmpty")}</div>
            ) : (
              items.map((item) => {
                const itemId = getItemId(item);
                return (
                  <Button
                    key={itemId}
                    type="text"
                    block
                    className={`modal-row ${selectedId === itemId ? "active" : ""}`}
                    onClick={() => setSelectedId(itemId)}
                  >
                    <strong>{getItemTitle(item)}</strong>
                    <span className="item-subline">{getItemMeta(item)}</span>
                  </Button>
                );
              })
            )}
          </div>
          <aside className="modal-actions">
            {!selected ? (
              <div className="empty">{t("noSelection")}</div>
            ) : type === "watchlists" ? (
              <>
                <div className="modal-action-title">
                  <strong>{selected.name}</strong>
                  <span className="item-subline">{getItemMeta(selected)}</span>
                </div>
                {watchlistItems.length > 0 && (
                  <div className="modal-mini-list">
                    {watchlistItems.map((wi: any) => (
                      <div key={wi.id} className="item-subline">
                        {wi.symbol
                          ? `${wi.symbol.symbol}${DOT}${wi.symbol.name}`
                          : `#${wi.symbol_id}`}
                      </div>
                    ))}
                  </div>
                )}
                <div className="modal-action-buttons">
                  <Button type="text" onClick={handleOpenWatchlist}>
                    {t("openWatchlist")}
                  </Button>
                  <Button
                    type="text"
                    onClick={handleAddActiveSymbol}
                    disabled={ctx.activeSymbolId == null || busyButton === "addActiveSymbol"}
                  >
                    {busyButton === "addActiveSymbol" ? t("addingSymbol") : t("addActiveSymbol")}
                  </Button>
                </div>
              </>
            ) : (
              <>
                <div className="modal-action-title">
                  <strong>{getItemTitle(selected)}</strong>
                  <span className="item-subline">{getItemMeta(selected)}</span>
                </div>
                <div className="modal-action-buttons">
                  <Button type="text" onClick={handleViewDetail}>
                    {t("viewDetail")}
                  </Button>
                  <Button
                    type="text"
                    onClick={handleAddToWatchlist}
                    disabled={busyButton === "addToWatchlist"}
                  >
                    {busyButton === "addToWatchlist" ? t("addingSymbol") : t("addToWatchlist")}
                  </Button>
                  <Button
                    type="text"
                    onClick={handleSyncSelected}
                    disabled={busyButton === "syncSelected"}
                  >
                    {busyButton === "syncSelected" ? t("syncing") : t("syncSelected")}
                  </Button>
                  <Button
                    type="text"
                    onClick={handleScanSelected}
                    disabled={busyButton === "scanSelected"}
                  >
                    {busyButton === "scanSelected" ? t("scanning") : t("scanSelected")}
                  </Button>
                  <Button type="primary" onClick={handleGeneratePlan}>
                    {t("generatePlan")}
                  </Button>
                </div>
              </>
            )}
          </aside>
        </div>
      </section>
    </div>
  );
}
