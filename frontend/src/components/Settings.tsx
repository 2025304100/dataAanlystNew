import { useState, useEffect } from "react";
import { useApp } from "../context/AppContext";
import { t, DOT } from "../i18n";
import { statPct, pnlClass, clamp } from "../utils/format";
import { Input, InputNumber, Checkbox, Button, Space, Card, Form } from "antd";

export default function Settings() {
  const ctx = useApp();
  const rule = ctx.signalRule;
  const presets = ctx.signalRulePresets;
  const preview = ctx.signalRulePreview;
  const activeSymbolId = ctx.activeSymbolId;
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (rule && activeSymbolId) {
      ctx.loadSignalRulePreview().catch(() => {});
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeSymbolId]);

  const applyPreset = (mode: string) => {
    const preset = presets.find((p) => p.mode === mode);
    if (!preset) return;
    ctx.updateSignalRule({
      rule_name: preset.rule_name,
      mode: preset.mode,
      quality_tolerance: preset.quality_tolerance,
      timing_tolerance: preset.timing_tolerance,
      min_sample_count: preset.min_sample_count,
      max_samples: preset.max_samples,
      same_region: preset.same_region,
      same_asset_type: preset.same_asset_type,
      same_stage: preset.same_stage,
      same_action: preset.same_action,
    });
  };

  const updateNumberField = (
    key: "quality_tolerance" | "timing_tolerance" | "min_sample_count" | "max_samples",
    rawValue: number,
    min: number,
    max: number
  ) => {
    const value = clamp(rawValue, min, max);
    ctx.updateSignalRule({ [key]: value, mode: "expert", rule_name: t("expertMode") });
  };

  const updateBooleanField = (
    key: "same_region" | "same_asset_type" | "same_stage" | "same_action",
    value: boolean
  ) => {
    ctx.updateSignalRule({ [key]: value, mode: "expert", rule_name: t("expertMode") });
  };

  const handleSave = () => {
    setSaving(true);
    ctx
      .saveSignalRule()
      .catch((error: any) => {
        ctx.showToast("error", error.message);
      })
      .finally(() => setSaving(false));
  };

  const summary = rule ? `${rule.rule_name}${DOT}${rule.mode}` : "-";

  const renderPreview = () => {
    if (!activeSymbolId) {
      return <div className="item-subline">{t("rulePreviewIdle")}</div>;
    }
    if (!preview) {
      return <div className="item-subline">{t("rulePreview")}...</div>;
    }
    if (!preview.stats) {
      return (
        <div className="modal-action-title">
          <strong>{preview.symbol ? `${preview.symbol}${DOT}${preview.name}` : t("rulePreview")}</strong>
          <span className="item-subline">{preview.message}</span>
        </div>
      );
    }
    const stats = preview.stats;
    const minSamples = stats.min_sample_count ?? stats.scope?.min_sample_count ?? "-";
    return (
      <div>
        <div className="modal-action-title">
          <strong>{t("rulePreview")}{DOT}{preview.symbol}{DOT}{preview.name}</strong>
          <span className="item-subline">{t("previewDiagnosis")}: {preview.message}</span>
        </div>
        <div className="rule-preview-grid">
          <div className="rule-preview-stat">
            <span className="metric-label">{t("previewSamples")}</span>
            <strong>{stats.sample_count ?? 0}/{minSamples}</strong>
          </div>
          <div className="rule-preview-stat">
            <span className="metric-label">{t("previewMatched")}</span>
            <strong>{stats.matched_count ?? 0}</strong>
          </div>
          <div className="rule-preview-stat">
            <span className="metric-label">{t("previewWin20d")}</span>
            <strong>{statPct(stats.win_rate_20d)}</strong>
          </div>
          <div className="rule-preview-stat">
            <span className="metric-label">{t("previewAvg20d")}</span>
            <strong className={pnlClass(stats.avg_return_20d)}>{statPct(stats.avg_return_20d)}</strong>
          </div>
          <div className="rule-preview-stat">
            <span className="metric-label">{t("previewDrawdown")}</span>
            <strong className={pnlClass(stats.avg_max_drawdown_20d)}>{statPct(stats.avg_max_drawdown_20d)}</strong>
          </div>
        </div>
      </div>
    );
  };

  return (
    <div className="tab-container" data-tab-content="settings">
      <div className="settings-layout">
        <nav className="settings-sidebar" aria-label="Settings categories">
          <button type="button" className="settings-nav-item active">{t("tabRules")}</button>
        </nav>
        <div className="settings-content">
          <div className="settings-tab-container" data-settings-content="settings-rules">
            <section className="band rule-config-band">
              <div className="panel">
                <div className="panel-head">
                  <div>
                    <p className="panel-kicker">{t("signalRuleKicker")}</p>
                    <h2>{t("ruleConfig")}</h2>
                  </div>
                  <p className="panel-meta">{summary}</p>
                </div>
                <div className="rule-config-grid">
                  <div className="preset-column">
                    <div className="preset-list">
                      {presets.map((preset) => (
                        <Button
                          key={preset.mode}
                          type={rule?.mode === preset.mode ? "primary" : "default"}
                          className={`preset-button ${rule?.mode === preset.mode ? "active" : ""}`}
                          onClick={() => applyPreset(preset.mode)}
                        >
                          <span className="preset-title">{preset.rule_name}</span>
                        </Button>
                      ))}
                    </div>
                  </div>
                  <form
                    className="rule-form"
                    onSubmit={(e) => {
                      e.preventDefault();
                      handleSave();
                    }}
                  >
                    <div className="rule-form-grid">
                      <label>
                        <span>
                          <span>{t("qualityTolerance")}</span>
                          <button type="button" className="tip-icon" data-tip={t("qualityToleranceTip")}>?</button>
                        </span>
                        <InputNumber
                          min={1}
                          max={40}
                          step={1}
                          value={rule?.quality_tolerance ?? 0}
                          onChange={(value) => updateNumberField("quality_tolerance", value ?? 0, 1, 40)}
                        />
                      </label>
                      <label>
                        <span>
                          <span>{t("timingTolerance")}</span>
                          <button type="button" className="tip-icon" data-tip={t("timingToleranceTip")}>?</button>
                        </span>
                        <InputNumber
                          min={1}
                          max={40}
                          step={1}
                          value={rule?.timing_tolerance ?? 0}
                          onChange={(value) => updateNumberField("timing_tolerance", value ?? 0, 1, 40)}
                        />
                      </label>
                      <label>
                        <span>
                          <span>{t("minSamples")}</span>
                          <button type="button" className="tip-icon" data-tip={t("minSamplesTip")}>?</button>
                        </span>
                        <InputNumber
                          min={1}
                          max={50}
                          step={1}
                          value={rule?.min_sample_count ?? 0}
                          onChange={(value) => updateNumberField("min_sample_count", value ?? 0, 1, 50)}
                        />
                      </label>
                      <label>
                        <span>
                          <span>{t("maxSamples")}</span>
                          <button type="button" className="tip-icon" data-tip={t("maxSamplesTip")}>?</button>
                        </span>
                        <InputNumber
                          min={5}
                          max={240}
                          step={5}
                          value={rule?.max_samples ?? 0}
                          onChange={(value) => updateNumberField("max_samples", value ?? 0, 5, 240)}
                        />
                      </label>
                    </div>
                    <div className="rule-check-grid">
                      <label>
                        <Checkbox
                          checked={rule?.same_region ?? false}
                          onChange={(e) => updateBooleanField("same_region", e.target.checked)}
                        />
                        <span>{t("sameRegion")}</span>
                        <button type="button" className="tip-icon" data-tip={t("sameRegionTip")}>?</button>
                      </label>
                      <label>
                        <Checkbox
                          checked={rule?.same_asset_type ?? false}
                          onChange={(e) => updateBooleanField("same_asset_type", e.target.checked)}
                        />
                        <span>{t("sameAsset")}</span>
                        <button type="button" className="tip-icon" data-tip={t("sameAssetTip")}>?</button>
                      </label>
                      <label>
                        <Checkbox
                          checked={rule?.same_stage ?? false}
                          onChange={(e) => updateBooleanField("same_stage", e.target.checked)}
                        />
                        <span>{t("sameStage")}</span>
                        <button type="button" className="tip-icon" data-tip={t("sameStageTip")}>?</button>
                      </label>
                      <label>
                        <Checkbox
                          checked={rule?.same_action ?? false}
                          onChange={(e) => updateBooleanField("same_action", e.target.checked)}
                        />
                        <span>{t("sameAction")}</span>
                        <button type="button" className="tip-icon" data-tip={t("sameActionTip")}>?</button>
                      </label>
                    </div>
                    <div className="detail-actions">
                      <Button type="primary" className="ghost-button detail-action" disabled={!rule || saving} loading={saving}>
                        {t("saveRule")}
                      </Button>
                    </div>
                    <div className={`rule-preview ${preview?.status || ""}`.trim()}>
                      {renderPreview()}
                    </div>
                  </form>
                </div>
              </div>
            </section>
          </div>
        </div>
      </div>
    </div>
  );
}
