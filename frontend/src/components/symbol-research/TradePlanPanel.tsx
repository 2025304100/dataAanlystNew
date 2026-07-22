// WP5.1：交易计划面板
//
// 设计说明：
// - 任务清单 #7：交易计划刷新 + 参数覆盖 + 情景分析 + 分批计划 + 未来买入计划
// - 从 InvestmentCenter.tsx 抽出 renderTradePlan JSX 块（含 4 个 Tab）
// - 交易计划生成、分批保存、未来计划计算逻辑保留在 Shell
// - 不重写 planDraftFromSetup/normalizeTrancheDrafts/getActiveFutureBuyPlan 等工具
//
// 关键约束：
// - 不重写交易计划生成算法、分批保存逻辑、未来买入计划计算
// - 通过 props 接收所有必要数据和回调，状态留在 Shell
import { Button, Input, InputNumber, Tabs } from "antd";
import {
  t,
  template,
  stageLabel,
  actionLabel,
  futureBuyLabel,
  futurePriorityLabel,
  futureTriggerLabel,
  trancheLabel,
  trancheTrigger,
} from "../../i18n";
import { joinParts, money, percent, score } from "../../utils/format";
import type { FuturePlanTuning } from "../../types";
import type { TradePlanPanelProps } from "./types";
import type { TradePlanDraft, TrancheDraft } from "./types";

/**
 * 交易计划面板：包含 4 个 Tab（概览/场景预演/分批/未来计划）。
 * - 所有数据通过 props 传入
 * - 编辑/保存/重置/刷新均通过回调透传到 Shell
 */
export default function TradePlanPanel(props: TradePlanPanelProps) {
  const {
    setup,
    scenarios,
    entryPrice,
    quantity,
    chartEntryPrice,
    tradePlanEditing,
    tradePlanDraft,
    trancheEditing,
    trancheDrafts,
    refreshingPlan,
    futurePlanScenario,
    futurePlanTunings,
    activeFutureBuyPlan,
    onEditPlan,
    onCancelPlanEdit,
    onApplyPlanOverrides,
    onRefreshPlan,
    onUpdateTradePlanDraft,
    onEditTranches,
    onCancelTranches,
    onSaveTranches,
    onResetTranches,
    onUpdateTrancheDraft,
    onAddTranche,
    onRemoveTranche,
    onUpdateFutureTuning,
    onSetFuturePlanScenario,
    onJumpToPortfolioTrade,
  } = props;

  const draft = tradePlanDraft;
  const currentFutureTuning: FuturePlanTuning = {
    ...(futurePlanTunings[futurePlanScenario] ?? {}),
  };

  const renderPlanEditNumber = (
    label: string,
    field: keyof TradePlanDraft,
    suffix?: string,
  ) => (
    <label className="ic__plan-edit-field">
      <span>{label}</span>
      <InputNumber
        size="small"
        min={0}
        value={draft?.[field] ?? null}
        addonAfter={suffix}
        onChange={(value) => onUpdateTradePlanDraft(field, value == null ? null : Number(value))}
      />
    </label>
  );

  const renderScenarioBox = (label: string, exitPrice: number) => {
    if (!(exitPrice > 0) || !(entryPrice > 0)) return null;
    const returnPct = (exitPrice - entryPrice) / entryPrice;
    const projectedPnl = (exitPrice - entryPrice) * quantity;
    const projectedValue = exitPrice * quantity;
    return (
      <div className="scenario-box" key={label}>
        <strong>{label}</strong>
        <span className="metric-value">{percent(returnPct)}</span>
        <span className={`item-subline pnl-positive`}>{money(projectedPnl)}</span>
        <span className="item-subline">
          {t("projectedValue")}: {money(projectedValue)}
        </span>
        <span className="item-subline">
          {t("target")}: {score(exitPrice)}
        </span>
      </div>
    );
  };

  const formatOpenTrigger = (item: typeof setup) => {
    if (!item || item.entry_min === null || item.entry_max === null) return "-";
    return template("icOpenTrigger", { min: item.entry_min, max: item.entry_max });
  };
  const formatAddTrigger = (item: typeof setup) => {
    if (!item || !item.allow_add_position) return "-";
    return t("icAddTrigger");
  };
  const formatStopTrigger = (item: typeof setup) => {
    if (!item || item.stop_loss === null) return "-";
    return template("icStopTrigger", { price: item.stop_loss });
  };
  const formatTrimTrigger = (item: typeof setup) => {
    if (!item || item.target_price === null) return "-";
    return template("icTrimTrigger", { price: item.target_price });
  };

  const planOverview = setup ? (
    <div className="ic__plan-tab-panel">
      {tradePlanEditing && draft ? (
        <div className="ic__plan-edit-grid">
          {renderPlanEditNumber(t("buyZoneLower"), "entry_min")}
          {renderPlanEditNumber(t("buyZoneUpper"), "entry_max")}
          {renderPlanEditNumber(t("stopLoss"), "stop_loss")}
          {renderPlanEditNumber(t("target"), "target_price")}
          {renderPlanEditNumber(t("recommendedPosition"), "recommended_position_pct", "%")}
          {renderPlanEditNumber(t("positionAmount"), "recommended_position_amount")}
        </div>
      ) : (
        <div className="ic__plan-summary-grid">
          <div className="ic__plan-summary-card">
            <span>{t("buyZone")}</span>
            <strong>
              {setup.entry_min !== null ? score(setup.entry_min) : "-"} -{" "}
              {setup.entry_max !== null ? score(setup.entry_max) : "-"}
            </strong>
            <em />
          </div>
          <div className="ic__plan-summary-card">
            <span>{t("stopLoss")}</span>
            <strong>{score(scenarios?._adjStop ?? setup.stop_loss)}</strong>
            <em />
          </div>
          <div className="ic__plan-summary-card">
            <span>{t("target")}</span>
            <strong>{score(scenarios?._adjTarget ?? setup.target_price)}</strong>
            <em />
          </div>
          <div className="ic__plan-summary-card">
            <span>{t("recommendedPosition")}</span>
            <strong>{percent(setup.recommended_position_pct)}</strong>
            <em>{money(setup.recommended_position_amount)}</em>
          </div>
        </div>
      )}

      <div className="item-subline ic__plan-meta-line">
        {joinParts([
          `${t("riskReward")}: ${setup.risk_reward_ratio != null ? score(setup.risk_reward_ratio) : "-"}`,
          `${t("allowAdd")}: ${setup.allow_add_position ? t("yes") : t("no")}`,
          `${t("stage")}: ${stageLabel(setup.stage)}`,
          `${t("action")}: ${actionLabel(setup.action)}`,
        ])}
      </div>

      <div className="item-subline ic__plan-meta-line">
        {joinParts([
          `${t("stageCap")}: ${setup.stage_cap_pct != null ? percent(setup.stage_cap_pct) : "-"}`,
          `${t("stageRoom")}: ${setup.remaining_stage_pct != null ? percent(setup.remaining_stage_pct) : "-"}`,
          `${t("currentPosition")}: ${setup.current_position_pct != null ? percent(setup.current_position_pct) : "-"}`,
          `${t("riskBudget")}: ${setup.risk_budget_amount != null ? money(setup.risk_budget_amount) : "-"}`,
          `${t("riskShare")}: ${setup.risk_per_share != null ? score(setup.risk_per_share) : "-"}`,
        ])}
      </div>

      <div className="ic__trigger-grid">
        <div>
          <strong>{t("openTrigger")}</strong>
          <span>{formatOpenTrigger(setup)}</span>
        </div>
        <div>
          <strong>{t("addTrigger")}</strong>
          <span>{formatAddTrigger(setup)}</span>
        </div>
        <div>
          <strong>{t("stopTrigger")}</strong>
          <span>{formatStopTrigger(setup)}</span>
        </div>
        <div>
          <strong>{t("trimTrigger")}</strong>
          <span>{formatTrimTrigger(setup)}</span>
        </div>
      </div>
    </div>
  ) : (
    <div className="empty">{t("latestTradeSetupEmpty")}</div>
  );

  const scenarioPane = scenarios ? (
    <div id="orderScenarioPreview" className="scenario-preview-card ic__plan-tab-panel">
      <div className="scenario-preview-head">
        <span className="scenario-preview-meta">
          {joinParts([
            `${t("plannedOrder")}: ${quantity}`,
            `${t("referencePrice")}: ${score(entryPrice)}${chartEntryPrice ? ` (${t("chartEntry")})` : ""}`,
            `${t("estimateConfidence")}: ${percent(scenarios.confidence_pct)}`,
            `${t("estimateHorizon")}: ${scenarios.horizon_days}${t("daysUnit")}`,
          ])}
        </span>
      </div>
      <div className="scenario-grid">
        {renderScenarioBox(t("expectedCase"), scenarios.expected.exit_price)}
        {renderScenarioBox(t("optimisticCase"), scenarios.optimistic.exit_price)}
        {renderScenarioBox(t("pessimisticCase"), scenarios.pessimistic.exit_price)}
      </div>
    </div>
  ) : (
    <div className="empty">{t("latestTradeSetupEmpty")}</div>
  );

  const tranchePane = setup ? (
    <div className="detail-card ic__plan-tab-panel">
      <div className="ic__tab-toolbar">
        <span className="panel-meta">
          {setup.manual_tranche_plan_json ? t("icManual") : t("icSystem")}
        </span>
        <div>
          {trancheEditing ? (
            <>
              <Button
                size="small"
                onClick={() => {
                  onCancelTranches();
                }}
              >
                {t("icCancelEdit")}
              </Button>
              <Button
                size="small"
                type="primary"
                loading={refreshingPlan}
                onClick={() => onSaveTranches()}
              >
                {t("icSaveTranches")}
              </Button>
            </>
          ) : (
            <Button size="small" onClick={onEditTranches}>
              {t("icEditTranches")}
            </Button>
          )}
          {setup.manual_tranche_plan_json && (
            <Button size="small" loading={refreshingPlan} onClick={() => onResetTranches()}>
              {t("icResetTranches")}
            </Button>
          )}
        </div>
      </div>

      {trancheEditing ? (
        <div className="ic__tranche-editor">
          {trancheDrafts.map((tranche: TrancheDraft, i: number) => (
            <div key={i} className="ic__tranche-edit-row">
              <label>
                <span>{t("icTrancheLabel")}</span>
                <Input
                  size="small"
                  value={tranche.label}
                  onChange={(e) => onUpdateTrancheDraft(i, "label", e.target.value)}
                />
              </label>
              <label>
                <span>{t("tranchePct")}</span>
                <InputNumber
                  size="small"
                  min={0}
                  max={100}
                  value={tranche.position_pct}
                  addonAfter="%"
                  onChange={(v) => onUpdateTrancheDraft(i, "position_pct", Number(v ?? 0))}
                />
              </label>
              <label>
                <span>{t("positionAmount")}</span>
                <InputNumber
                  size="small"
                  min={0}
                  value={tranche.amount}
                  onChange={(v) => onUpdateTrancheDraft(i, "amount", Number(v ?? 0))}
                />
              </label>
              <label>
                <span>{t("trigger")}</span>
                <Input
                  size="small"
                  value={tranche.trigger}
                  onChange={(e) => onUpdateTrancheDraft(i, "trigger", e.target.value)}
                />
              </label>
              <Button size="small" danger onClick={() => onRemoveTranche(i)}>
                {t("delete")}
              </Button>
            </div>
          ))}
          <Button
            size="small"
            onClick={() =>
              onAddTranche()
            }
          >
            {t("icAddTranche")}
          </Button>
          <div className="item-subline">
            {joinParts([
              `${t("recommendedPosition")}: ${percent(setup.recommended_position_pct)}`,
            ])}
          </div>
        </div>
      ) : setup.tranche_plan && setup.tranche_plan.length > 0 ? (
        <div className="ic__tranche-timeline">
          {setup.tranche_plan.map((tranche, i) => (
            <div key={i} className={`ic__tranche-node ic__tranche--${tranche.label}`}>
              <div className="ic__tranche-dot" />
              {i < setup.tranche_plan!.length - 1 && <div className="ic__tranche-line" />}
              <div className="ic__tranche-content">
                <strong style={{ color: "#0f766e" }}>{trancheLabel(tranche.label)}</strong>
                <span className="ic__tranche-meta">
                  {joinParts([
                    `${t("tranchePct")}: ${percent(tranche.position_pct)}`,
                    `${t("positionAmount")}: ${money(tranche.amount)}`,
                    `${t("trigger")}: ${trancheTrigger(tranche.trigger ?? "")}`,
                  ])}
                </span>
              </div>
            </div>
          ))}
        </div>
      ) : (
        <div className="empty">{t("latestTradeSetupEmpty")}</div>
      )}
    </div>
  ) : (
    <div className="empty">{t("latestTradeSetupEmpty")}</div>
  );

  const futurePane = activeFutureBuyPlan.length > 0 ? (
    <div className="detail-card ic__plan-tab-panel">
      <div className="future-plan-head">
        <p className="panel-kicker">{t("futureBuyPlan")}</p>
        <div className="future-scenario-tabs">
          {["general", "short", "mid", "long", "custom"].map((sc) => (
            <Button
              key={sc}
              size="small"
              type={futurePlanScenario === sc ? "primary" : "default"}
              onClick={() => onSetFuturePlanScenario(sc)}
            >
              {t(`futureScenario${sc.charAt(0).toUpperCase() + sc.slice(1)}`)}
            </Button>
          ))}
        </div>
      </div>

      <div className="future-custom-grid ic__future-tuning-grid">
        <label>
          <span>{t("icHorizonDays")}</span>
          <InputNumber
            size="small"
            min={1}
            max={365}
            value={currentFutureTuning.horizonDays as number | undefined}
            onChange={(v) => onUpdateFutureTuning("horizonDays", v == null ? null : Number(v))}
          />
        </label>
        <label>
          <span>{t("icPullbackPct")}</span>
          <InputNumber
            size="small"
            min={0}
            max={50}
            step={0.5}
            value={currentFutureTuning.pullbackPct as number | undefined}
            onChange={(v) => onUpdateFutureTuning("pullbackPct", v == null ? null : Number(v))}
          />
        </label>
        <label>
          <span>{t("icPositionPct")}</span>
          <InputNumber
            size="small"
            min={0}
            max={100}
            step={0.5}
            value={currentFutureTuning.positionPct as number | undefined}
            onChange={(v) => onUpdateFutureTuning("positionPct", v == null ? null : Number(v))}
          />
        </label>
        <label>
          <span>{t("icBandPct")}</span>
          <InputNumber
            size="small"
            min={0.1}
            max={50}
            step={0.1}
            value={currentFutureTuning.bandPct as number | undefined}
            onChange={(v) => onUpdateFutureTuning("bandPct", v == null ? null : Number(v))}
          />
        </label>
        <label>
          <span>{t("icScalePct")}</span>
          <InputNumber
            size="small"
            min={0}
            max={300}
            step={5}
            value={currentFutureTuning.scalePct as number | undefined}
            onChange={(v) => onUpdateFutureTuning("scalePct", v == null ? null : Number(v))}
          />
        </label>
      </div>

      {activeFutureBuyPlan.map((plan, i) => (
        <div key={i} className="item-subline">
          {joinParts([
            futureBuyLabel(plan.label),
            `${t("futureZone")}: ${plan.zone_min !== null ? score(plan.zone_min) : "-"} - ${plan.zone_max !== null ? score(plan.zone_max) : "-"}`,
            `${t("futureHorizon")}: ${plan.horizon_days}${t("daysUnit")}`,
            `${t("futurePriority")}: ${futurePriorityLabel(plan.priority)}`,
            `${t("recommendedPosition")}: ${percent(plan.position_pct)}`,
            `${t("positionAmount")}: ${money(plan.amount)}`,
            `${t("trigger")}: ${futureTriggerLabel(plan)}`,
          ])}
        </div>
      ))}
    </div>
  ) : (
    <div className="empty">{t("latestTradeSetupEmpty")}</div>
  );

  return (
    <section className="ic__section ic__trade-plan-section symbol-research-trade-plan">
      <div className="panel">
        <div className="detail-card-head">
          <h3>{t("tradeSetup")}</h3>
          <div className="detail-actions">
            {setup &&
              (tradePlanEditing ? (
                <>
                  <Button size="small" onClick={onCancelPlanEdit}>
                    {t("icCancelEdit")}
                  </Button>
                  <Button
                    size="small"
                    type="primary"
                    loading={refreshingPlan}
                    onClick={() => onApplyPlanOverrides()}
                  >
                    {t("icSavePlan")}
                  </Button>
                </>
              ) : (
                <Button size="small" onClick={onEditPlan}>
                  {t("icEditPlan")}
                </Button>
              ))}
            <Button size="small" loading={refreshingPlan} onClick={() => onRefreshPlan()}>
              {t("refreshPlan")}
            </Button>
            <Button
              size="small"
              type="primary"
              onClick={onJumpToPortfolioTrade}
              disabled={!onJumpToPortfolioTrade}
              data-testid="jump-to-portfolio-trade-button"
            >
              {t("symbolResearchJumpToPortfolioTrade")}
            </Button>
          </div>
        </div>
        <Tabs
          className="ic__trade-tabs"
          size="small"
          items={[
            { key: "overview", label: t("icOverview"), children: planOverview },
            { key: "scenario", label: t("icScenario"), children: scenarioPane },
            { key: "tranches", label: t("icTranches"), children: tranchePane },
            { key: "future", label: t("icFuture"), children: futurePane },
          ]}
        />
      </div>
    </section>
  );
}
