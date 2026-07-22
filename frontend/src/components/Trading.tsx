import { useMemo, useCallback, useState } from "react";
import { InputNumber, Button, Table } from "antd";
import type { TableColumnsType } from "antd";
import { useApp } from "../context/AppContext";
import { t, sideLabel } from "../i18n";
import {
  percent,
  score,
  money,
  joinParts,
  pnlClass,
  sideBadgeClass,
  computeSuggestedPrice,
  computeSuggestedBuyQuantity,
  computeDefaultSellQuantity,
  lotSizeForDetail,
} from "../utils/format";
import PortfolioPerformancePanel from "./PortfolioPerformancePanel";
import AutoTradePanel from "./AutoTradePanel";
import PortfolioBacktestPanel from "./PortfolioBacktestPanel";
// WP1-FIX.3：组合页交易子页持仓表接入 OpportunityStatusBadges
import { OpportunityStatusBadges } from "./opportunity/OpportunityStatusBadges";

const SEP = " | ";

export default function Trading() {
  const ctx = useApp();
  const workbench = ctx.workbench;
  const detail = ctx.detail;
  const account = workbench?.account_summary ?? null;
  const positions = workbench?.positions ?? [];
  const recentTrades = workbench?.recent_trades ?? [];
  const [buying, setBuying] = useState(false);
  const [selling, setSelling] = useState(false);

  const accountMeta = account
    ? `${t("activeTrades")}: ${account.trade_count_7d}${SEP}${t("lastTrade")}: ${
        account.last_trade_at ? new Date(account.last_trade_at).toLocaleString() : "-"
      }`
    : "-";

  const lastBar = detail?.bars?.[detail.bars.length - 1];
  const currentPrice = lastBar?.close ?? detail?.position?.latest_price ?? null;
  const position = detail?.position ?? null;

  const orderTitle = detail
    ? `${detail.symbol.symbol}${SEP}${detail.symbol.name}`
    : t("selectSymbol");

  const orderPreview = useMemo(() => {
    const quantity = Number(ctx.simQuantity) || 0;
    const price = Number(ctx.simPrice) || 0;
    if (!(quantity > 0) || !(price > 0)) return null;
    const totalCost = quantity * price;
    const cash = workbench?.account_summary?.available_cash ?? 0;
    const remaining = cash - totalCost;
    return { totalCost, remaining };
  }, [ctx.simQuantity, ctx.simPrice, workbench]);

  const handleQuickQty = useCallback(
    (pct: number) => {
      if (!detail) return;
      const price = Number(ctx.simPrice) || computeSuggestedPrice(detail);
      if (!(price > 0)) return;
      const lotSize = lotSizeForDetail(detail);
      const cash = workbench?.account_summary?.available_cash ?? 0;
      const budget = cash * (pct / 100);
      const qty = Math.floor(budget / price / lotSize) * lotSize;
      if (qty > 0) ctx.setSimQuantity(String(qty));
    },
    [detail, ctx.simPrice, workbench, ctx]
  );

  const handleBuy = useCallback(async () => {
    setBuying(true);
    try {
      await ctx.submitSimOrder("buy");
    } catch (error: any) {
      ctx.showToast("error", `${t("orderFailed")}: ${error?.message || error}`);
    } finally {
      setBuying(false);
    }
  }, [ctx]);

  const handleSell = useCallback(async () => {
    setSelling(true);
    try {
      await ctx.submitSimOrder("sell");
    } catch (error: any) {
      ctx.showToast("error", `${t("orderFailed")}: ${error?.message || error}`);
    } finally {
      setSelling(false);
    }
  }, [ctx]);

  const handlePositionClick = useCallback(
    (symbolId: number) => {
      ctx.loadSymbolDetail(symbolId, { focus: true });
    },
    [ctx]
  );

  if (!workbench) {
    return (
      <div className="sub-tab-container" data-sub-content="portfolio-trading">
        <div className="empty">{t("noScanYet")}</div>
      </div>
    );
  }

  return (
    <div className="sub-tab-container" data-sub-content="portfolio-trading">
      {/* Account Summary */}
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
            <div className="empty">{t("noTrades")}</div>
          )}
        </div>
      </section>

      {/* P1-2：组合绩效面板（净值曲线 + 绩效指标卡片） */}
      <PortfolioPerformancePanel />

      {/* P2-3：自动交易执行面板（dry_run 预览 + 实际下单） */}
      <AutoTradePanel />

      {/* P2-2：组合整体回测面板（基于 Score.action 信号 + 自动推导标的） */}
      <PortfolioBacktestPanel />

      <div className="trading-columns">
        {/* Order Panel */}
        <div className="trading-order">
          <section className="band">
            <div className="panel trading-order-panel">
              <div className="trading-order-head">
                <p className="panel-kicker">{t("orderTitle")}</p>
                <h2 id="tradingDetailTitle">{orderTitle}</h2>
                <p className="trading-current-price" id="tradingCurrentPrice">
                  {currentPrice !== null ? score(currentPrice) : "-"}
                </p>
              </div>

              <div className="trading-symbol-info" id="tradingSymbolInfo">
                {position && (
                  <>
                    <div className="info-item">
                      <span>{t("holdingQty")}</span>
                      <strong>{position.quantity}</strong>
                    </div>
                    <div className="info-item">
                      <span>{t("avgCost")}</span>
                      <strong>{score(position.avg_cost)}</strong>
                    </div>
                    <div className="info-item">
                      <span>{t("weight")}</span>
                      <strong>{percent(position.position_pct)}</strong>
                    </div>
                    <div className="info-item">
                      <span>{t("currentPrice")}</span>
                      <strong>{currentPrice !== null ? score(currentPrice) : "-"}</strong>
                    </div>
                  </>
                )}
                {detail && (
                  <>
                    <div className="info-item">
                      <span>{t("simBuy")}</span>
                      <strong>{computeSuggestedBuyQuantity(detail)}</strong>
                    </div>
                    {position && (
                      <div className="info-item">
                        <span>{t("simSell")}</span>
                        <strong>{computeDefaultSellQuantity(detail)}</strong>
                      </div>
                    )}
                  </>
                )}
              </div>

              <div className="order-form">
                <label>
                  <span>{t("orderQty")}</span>
                  <InputNumber
                    id="simQuantityInput"
                    min={1}
                    step={1}
                    value={Number(ctx.simQuantity) || undefined}
                    onChange={(value) => ctx.setSimQuantity(String(value ?? ""))}
                  />
                </label>
                <label>
                  <span>{t("orderPrice")}</span>
                  <InputNumber
                    id="simPriceInput"
                    min={0.01}
                    step={0.01}
                    precision={2}
                    value={Number(ctx.simPrice) || undefined}
                    onChange={(value) => ctx.setSimPrice(String(value ?? ""))}
                  />
                </label>
              </div>

              <div className="trading-quick-qty" id="tradingQuickQty">
                <Button size="small" className="quick-qty-btn" data-pct="25" onClick={() => handleQuickQty(25)}>
                  25%
                </Button>
                <Button size="small" className="quick-qty-btn" data-pct="33" onClick={() => handleQuickQty(33)}>
                  33%
                </Button>
                <Button size="small" className="quick-qty-btn" data-pct="50" onClick={() => handleQuickQty(50)}>
                  50%
                </Button>
                <Button size="small" className="quick-qty-btn" data-pct="100" onClick={() => handleQuickQty(100)}>
                  All
                </Button>
              </div>

              <div className="trading-order-preview" id="tradingOrderPreview">
                {orderPreview && (
                  <>
                    <div className="preview-row">
                      <span className="label">{t("totalCost")}</span>
                      <span className="value">{money(orderPreview.totalCost)}</span>
                    </div>
                    <div className="preview-row">
                      <span className="label">{t("remainingCash")}</span>
                      <span className={`value ${pnlClass(orderPreview.remaining)}`}>
                        {money(orderPreview.remaining)}
                      </span>
                    </div>
                  </>
                )}
              </div>

              <div className="trading-order-actions">
                <Button
                  id="simBuyButton"
                  type="primary"
                  className="ghost-button detail-action buy-action"
                  loading={buying}
                  onClick={handleBuy}
                >
                  {t("simBuy")}
                </Button>
                <Button
                  id="simSellButton"
                  className="ghost-button detail-action sell-action"
                  loading={selling}
                  onClick={handleSell}
                >
                  {t("simSell")}
                </Button>
              </div>
            </div>
          </section>
        </div>

        {/* Holdings Table */}
        <div className="trading-positions">
          <section className="band">
            <div className="panel">
              <div className="panel-head">
                <div>
                  <p className="panel-kicker">{t("holdings")}</p>
                  <h2>{t("holdings")}</h2>
                </div>
              </div>
              <div className="table-wrap">
                <Table
                  size="small"
                  rowKey="symbol_id"
                  dataSource={positions}
                  pagination={false}
                  scroll={{ x: "max-content" }}
                  rowClassName={(record: any) =>
                    `clickable ${ctx.activeSymbolId === record.symbol_id ? "active" : ""}`
                  }
                  onRow={(record: any) => ({
                    onClick: () => handlePositionClick(record.symbol_id),
                  })}
                  columns={[
                    {
                      title: t("symbol"),
                      dataIndex: "symbol",
                      render: (_: unknown, record: any) => (
                        <div className="symbol-title">
                          <span className="symbol-code">{record.symbol}</span>
                          <span className="symbol-name">{record.name}</span>
                        </div>
                      ),
                    },
                    { title: t("orderQty"), dataIndex: "quantity" },
                    { title: t("avgCost"), dataIndex: "avg_cost", render: (v: number) => score(v) },
                    { title: t("currentPrice"), dataIndex: "latest_price", render: (v: number) => score(v) },
                    { title: t("accountMarketValue"), dataIndex: "market_value", render: (v: number) => money(v) },
                    { title: t("weight"), dataIndex: "position_pct", render: (v: number) => percent(v) },
                    {
                      title: t("accountUnrealizedPnl"),
                      render: (_: unknown, record: any) => (
                        <>
                          <div className={pnlClass(record.unrealized_pnl)}>{money(record.unrealized_pnl)}</div>
                          <div className={`pnl-pct ${pnlClass(record.unrealized_pnl_pct)}`}>
                            {percent(record.unrealized_pnl_pct)}
                          </div>
                        </>
                      ),
                    },
                    // WP1-FIX.3：关联状态列（持仓会显示"持仓 active"，但不会把持仓显示成观察项）
                    {
                      title: t("opportunityObservationColStatus"),
                      key: "status_badges",
                      render: (_: unknown, record: any) => (
                        <OpportunityStatusBadges
                          symbolId={record.symbol_id}
                          onOpenDetail={handlePositionClick}
                        />
                      ),
                    },
                  ] as TableColumnsType<any>}
                />
              </div>
            </div>
          </section>
        </div>
      </div>

      {/* Trade History */}
      <section className="band">
        <div className="panel">
          <div className="panel-head">
            <div>
              <p className="panel-kicker">{t("recentTrades")}</p>
              <h2>{t("tradeHistory")}</h2>
            </div>
          </div>
          {recentTrades.length === 0 ? (
            <div className="empty">{t("noTrades")}</div>
          ) : (
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(auto-fill, minmax(260px, 1fr))",
                gap: "10px",
              }}
            >
              {recentTrades.map((trade) => (
                <div key={trade.id} className="detail-card">
                  <div className="item-topline">
                    <div className="symbol-title">
                      <span className="symbol-code">{trade.symbol}</span>
                      <span className="symbol-name">{trade.name}</span>
                    </div>
                    <span className={sideBadgeClass(trade.side)}>{sideLabel(trade.side)}</span>
                  </div>
                  <div className="item-subline">
                    {joinParts([
                      `${t("orderQty")}: ${trade.quantity}`,
                      `${t("orderPrice")}: ${score(trade.price)}`,
                      `${t("accountMarketValue")}: ${money(trade.amount)}`,
                    ])}
                  </div>
                  <div className="item-subline">
                    <span className={pnlClass(trade.realized_pnl)}>{money(trade.realized_pnl)}</span>
                    {SEP}
                    {new Date(trade.created_at).toLocaleString()}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </section>
    </div>
  );
}
