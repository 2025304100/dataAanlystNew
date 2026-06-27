from pathlib import Path
path = Path('frontend/src/components/InvestmentCenter.tsx')
text = path.read_text(encoding='utf-8')
start = text.index('  const renderTradePlan = () =>')
end = text.index('  /* ════════════════════════════════════════════════════\n     渲染：K线图区块', start)
new_func = r'''  const renderTradePlan = () => {
    const draft = tradePlanDraft;
    const renderPlanEditNumber = (label: string, field: keyof TradePlanDraft, suffix?: string) => (
      <label className="ic__plan-edit-field">
        <span>{label}</span>
        <InputNumber
          size="small"
          min={0}
          value={draft?.[field] ?? null}
          addonAfter={suffix}
          onChange={(value) => updateTradePlanDraft(field, value == null ? null : Number(value))}
        />
      </label>
    );

    const planOverview = setup ? (
      <div className="ic__plan-tab-panel">
        {tradePlanEditing && draft ? (
          <div className="ic__plan-edit-grid">
            {renderPlanEditNumber(t("buyZone") + " 下限", "entry_min")}
            {renderPlanEditNumber(t("buyZone") + " 上限", "entry_max")}
            {renderPlanEditNumber(t("stopLoss"), "stop_loss")}
            {renderPlanEditNumber(t("target"), "target_price")}
            {renderPlanEditNumber(t("recommendedPosition"), "recommended_position_pct", "%")}
            {renderPlanEditNumber(t("positionAmount"), "recommended_position_amount")}
          </div>
        ) : (
          <div className="ic__plan-summary-grid">
            <div className="ic__plan-summary-card">
              <span>{t("buyZone")}</span>
              <strong>{setup.entry_min !== null ? score(setup.entry_min) : "-"} - {setup.entry_max !== null ? score(setup.entry_max) : "-"}</strong>
              <em>{fieldSourceBadge("entry_min")}{fieldSourceBadge("entry_max")}</em>
            </div>
            <div className="ic__plan-summary-card">
              <span>{t("stopLoss")}</span>
              <strong>{score((scenarios as any)?._adjStop ?? setup.stop_loss)}</strong>
              <em>{fieldSourceBadge("stop_loss")}</em>
            </div>
            <div className="ic__plan-summary-card">
              <span>{t("target")}</span>
              <strong>{score((scenarios as any)?._adjTarget ?? setup.target_price)}</strong>
              <em>{fieldSourceBadge("target_price")}</em>
            </div>
            <div className="ic__plan-summary-card">
              <span>{t("recommendedPosition")}</span>
              <strong>{percent(setup.recommended_position_pct)}</strong>
              <em>{money(setup.recommended_position_amount)} {fieldSourceBadge("recommended_position_pct")}{fieldSourceBadge("recommended_position_amount")}</em>
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
          <div><strong>{t("openTrigger")}</strong><span>{formatOpenTrigger(setup)}</span></div>
          <div><strong>{t("addTrigger")}</strong><span>{formatAddTrigger(setup)}</span></div>
          <div><strong>{t("stopTrigger")}</strong><span>{formatStopTrigger(setup)}</span></div>
          <div><strong>{t("trimTrigger")}</strong><span>{formatTrimTrigger(setup)}</span></div>
        </div>
      </div>
    ) : <div className="empty">{t("latestTradeSetupEmpty")}</div>;

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
    ) : <div className="empty">{t("latestTradeSetupEmpty")}</div>;

    const tranchePane = setup?.tranche_plan && setup.tranche_plan.length > 0 ? (
      <div className="detail-card ic__plan-tab-panel">
        <div className="ic__tranche-timeline">
          {setup.tranche_plan.map((tranche, i) => (
            <div key={i} className={`ic__tranche-node ic__tranche--${tranche.label}`}>
              <div className="ic__tranche-dot" />
              {(i < setup.tranche_plan!.length - 1) && <div className="ic__tranche-line" />}
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
      </div>
    ) : <div className="empty">{t("latestTradeSetupEmpty")}</div>;

    const futurePane = activeFutureBuyPlan.length > 0 ? (
      <div className="detail-card ic__plan-tab-panel">
        <div className="future-plan-head">
          <p className="panel-kicker">{t("futureBuyPlan")}</p>
          <div className="future-scenario-tabs">
            {["general", "short", "mid", "long", "custom"].map((sc) => (
              <Button key={sc} size="small"
                type={ctx.futurePlanScenario === sc ? "primary" : "default"}
                onClick={() => ctx.setFuturePlanScenario(sc)}>
                {t(`futureScenario${sc.charAt(0).toUpperCase() + sc.slice(1)}`)}
              </Button>
            ))}
          </div>
        </div>

        {ctx.futurePlanScenario === "custom" && (
          <div className="future-custom-grid">
            <label><span>{t("customHorizon")}</span><input type="number" value={ctx.futurePlanCustom.horizonDays}
              onChange={(e) => ctx.setFuturePlanCustom({ ...ctx.futurePlanCustom, horizonDays: Number(e.target.value) })} /></label>
            <label><span>{t("customPullback")}</span><input type="number" value={ctx.futurePlanCustom.pullbackPct}
              onChange={(e) => ctx.setFuturePlanCustom({ ...ctx.futurePlanCustom, pullbackPct: Number(e.target.value) })} /></label>
            <label><span>{t("customPosition")}</span><input type="number" value={ctx.futurePlanCustom.positionPct}
              onChange={(e) => ctx.setFuturePlanCustom({ ...ctx.futurePlanCustom, positionPct: Number(e.target.value) })} /></label>
          </div>
        )}

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
    ) : <div className="empty">{t("latestTradeSetupEmpty")}</div>;

    return (
      <section className="ic__section ic__trade-plan-section">
        <div className="panel">
          <div className="detail-card-head">
            <h3>{t("tradeSetup")}</h3>
            <div className="detail-actions">
              {setup && (tradePlanEditing ? (
                <>
                  <Button size="small" onClick={handleCancelPlanEdit}>{icText.cancelEdit}</Button>
                  <Button size="small" type="primary" loading={refreshingPlan} onClick={handleApplyPlanOverrides}>{icText.savePlan}</Button>
                </>
              ) : (
                <Button size="small" onClick={handleEditPlan}>{icText.editPlan}</Button>
              ))}
              <Button size="small" loading={refreshingPlan} onClick={handleRefreshPlan}>{t("refreshPlan")}</Button>
            </div>
          </div>
          <Tabs
            className="ic__trade-tabs"
            size="small"
            items={[
              { key: "overview", label: icText.overview, children: planOverview },
              { key: "scenario", label: icText.scenario, children: scenarioPane },
              { key: "tranches", label: icText.tranches, children: tranchePane },
              { key: "future", label: icText.future, children: futurePane },
            ]}
          />
        </div>
      </section>
    );
  };

'''
text = text[:start] + new_func + text[end:]
path.write_text(text, encoding='utf-8')
