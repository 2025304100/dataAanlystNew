import { useMemo } from "react";
import { Input, Button, Tag } from "antd";
import { useApp } from "../context/AppContext";
import { t, DOT, stageLabel, actionLabel } from "../i18n";
import {
  percent,
  score,
  money,
  joinParts,
  badgeClass,
  pnlClass,
  withFinalOpportunityScore,
  opportunityScoreValue,
} from "../utils/format";

interface PortfolioWorkbenchProps {
  openMetricModal: (type: string) => void;
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

  if (!workbench) {
    return (
      <div className="sub-tab-container" data-sub-content="portfolio-workbench">
        <div className="empty">{t("noScanYet")}</div>
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


  return (
    <div className="sub-tab-container" data-sub-content="portfolio-workbench">
      <section className="band today-band">
        <div className="panel wide">
          <div className="panel-head">
            <div>
              <p className="panel-kicker">Decision Path</p>
              <h2>{t("todayOpportunities")}</h2>
            </div>
            <p className="panel-meta">{todayMeta}</p>
          </div>
          <div className="today-grid">
            <div className="today-column">
              <p className="panel-kicker">{t("todayExecutable")}</p>
              <div className="today-list">
                {todayExecutable.length === 0 ? (
                  <div className="empty">{t("noCandidates")}</div>
                ) : (
                  todayExecutable.map((item) => (
                    <Button
                      key={item.symbol_id}
                      type="text"
                      className="today-item"
                      data-symbol-id={item.symbol_id}
                      onClick={() => handleSymbolClick(item.symbol_id)}
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
                  <div className="empty">{t("noScores")}</div>
                ) : (
                  watchQueue.map((item) => (
                    <Button
                      key={item.symbol_id}
                      type="text"
                      className="today-item"
                      data-symbol-id={item.symbol_id}
                      onClick={() => handleSymbolClick(item.symbol_id)}
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
                  <div className="empty">{t("noNewsYet")}</div>
                ) : (
                  todayMessages.map((item) => (
                    <Button
                      key={item.symbol_id}
                      type="text"
                      className="today-item"
                      data-symbol-id={item.symbol_id}
                      onClick={() => handleSymbolClick(item.symbol_id)}
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
          <Button type="text" className="metric-card metric-action" onClick={() => openMetricModal("symbols")}>
            <span className="metric-label">{t("trackedUniverse")}</span>
            <span className="metric-value">{marketScope?.filtered_symbols ?? overview.symbols_count}</span>
            <span className="metric-note">{t("items")}</span>
          </Button>
          <Button type="text" className="metric-card metric-action" onClick={() => openMetricModal("watchlists")}>
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
          <Button type="text" className="metric-card metric-action" onClick={() => openMetricModal("candidates")}>
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
                <div className="empty">{t("noTrades")}</div>
              )}
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
                              <table>
                                <thead>
                                  <tr>
                                    <th>{t("rank")}</th>
                                    <th>{t("symbol")}</th>
                                    <th>{t("quality")}</th>
                                    <th>{t("timing")}</th>
                                    <th>{t("stage")}</th>
                                    <th>{t("action")}</th>
                                    <th>{t("position")}</th>
                                  </tr>
                                </thead>
                                <tbody>
                                  {filteredCandidates.map((item, idx) => (
                                  <tr
                                    key={item.symbol_id}
                                    className={`clickable ${ctx.activeSymbolId === item.symbol_id ? "active" : ""}`}
                                    onClick={() => handleSymbolClick(item.symbol_id)}
                                  >
                                    <td>{item.rank_no ?? idx + 1}</td>
                                    <td><div className="symbol-title"><span className="symbol-code">{item.symbol}</span><span className="symbol-name">{item.name}</span></div></td>
                                    <td>{score(item.quality_score)}</td>
                                    <td>{score(item.timing_score)}</td>
                                    <td><Tag className={badgeClass(item.stage)}>{stageLabel(item.stage)}</Tag></td>
                                    <td><Tag className={badgeClass(item.action)}>{actionLabel(item.action)}</Tag></td>
                                    <td>{percent(item.recommended_position_pct)}</td>
                                  </tr>
                                  ))}
                                </tbody>
                              </table>
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
                <div className="empty">{t("noScores")}</div>
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
    </div>
  );
}
