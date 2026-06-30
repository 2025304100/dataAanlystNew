import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  Button,
  Collapse,
  DatePicker,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Radio,
  Select,
  Space,
  Tag,
  message,
} from "antd";
import { DeleteOutlined, SaveOutlined } from "@ant-design/icons";
import dayjs from "dayjs";
import { api } from "../api/client";
import { t } from "../i18n";
import type {
  BacktestCoverageWarning,
  BacktestRuleConfigV2,
  BacktestRun,
  ConditionGroup,
  CustomIndicator,
  RuleTemplate,
} from "../types";
import ConditionBuilder from "./ConditionBuilder";
import { DEFAULT_BUY_GROUP, DEFAULT_SELL_GROUP } from "../constants/conditionFields";

interface BacktestConfigProps {
  portfolioId: number | null;
  activeSymbolId: number | null;
  onResult: (run: BacktestRun) => void;
}

const DEFAULT_STAGES = ["start", "accel"];
const DEFAULT_ACTIONS = ["open", "hold", "buy_dip"];

type RuleMode = "standard" | "advanced";

function collectBacktestIndicatorKeys(node: unknown, keys: Set<string>) {
  if (!node) return;
  if (Array.isArray(node)) {
    node.forEach((item) => collectBacktestIndicatorKeys(item, keys));
    return;
  }
  if (typeof node !== "object") return;
  const record = node as Record<string, unknown>;
  if (record.field === "custom_indicator") {
    const params = record.params as Record<string, unknown> | undefined;
    const indicatorKey = typeof params?.indicator_key === "string" ? params.indicator_key : "";
    if (indicatorKey) keys.add(indicatorKey);
  }
  Object.values(record).forEach((value) => collectBacktestIndicatorKeys(value, keys));
}

type BacktestTemplateConfig = Partial<BacktestRuleConfigV2> & {
  position_config?: {
    value?: number;
    max_positions?: number;
  };
  execution_config?: {
    entry_price_field?: "open" | "close";
    exit_price_field?: "open" | "close";
  };
};

function presetLabel(preset: BacktestCoverageWarning["summary"]["recommended_preset"]): string {
  if (preset === "1m") return t("btPreset1m");
  if (preset === "1q") return t("btPreset1q");
  if (preset === "3y") return t("btPreset3y");
  return t("btPreset1y");
}

export default function BacktestConfig({ portfolioId, activeSymbolId, onResult }: BacktestConfigProps) {
  const [running, setRunning] = useState(false);
  const [runName, setRunName] = useState(t("backtestRunName"));
  const [ruleMode, setRuleMode] = useState<RuleMode>("standard");
  const [range, setRange] = useState<[dayjs.Dayjs, dayjs.Dayjs]>([
    dayjs().subtract(1, "year"),
    dayjs(),
  ]);

  const [qualityMin, setQualityMin] = useState(60);
  const [timingMin, setTimingMin] = useState(55);
  const [stages, setStages] = useState<string[]>(DEFAULT_STAGES);
  const [actions, setActions] = useState<string[]>(DEFAULT_ACTIONS);
  const [takeProfitPct, setTakeProfitPct] = useState(15);
  const [stopLossPct, setStopLossPct] = useState(8);
  const [maxHoldDays, setMaxHoldDays] = useState(30);

  const [buyConditions, setBuyConditions] = useState<ConditionGroup>(structuredClone(DEFAULT_BUY_GROUP));
  const [sellConditions, setSellConditions] = useState<ConditionGroup>(structuredClone(DEFAULT_SELL_GROUP));

  const [positionPct, setPositionPct] = useState(5);
  const [maxPositions, setMaxPositions] = useState(5);
  const [entryPriceField, setEntryPriceField] = useState<"open" | "close">("close");
  const [exitPriceField, setExitPriceField] = useState<"open" | "close">("close");
  const [commissionRate, setCommissionRate] = useState(0.03);
  const [minCommission, setMinCommission] = useState(5);
  const [stampTaxRate, setStampTaxRate] = useState(0.1);
  const [slippageRate, setSlippageRate] = useState(0.1);

  const [templates, setTemplates] = useState<RuleTemplate[]>([]);
  const [customIndicators, setCustomIndicators] = useState<CustomIndicator[]>([]);
  const [selectedTemplateId, setSelectedTemplateId] = useState<number | null>(null);
  const [saveModalOpen, setSaveModalOpen] = useState(false);
  const [templateName, setTemplateName] = useState("");
  const [templateDesc, setTemplateDesc] = useState("");
  const [coverageWarning, setCoverageWarning] = useState<BacktestCoverageWarning | null>(null);

  const disabled = useMemo(
    () => !portfolioId || !activeSymbolId || !range?.[0] || !range?.[1],
    [portfolioId, activeSymbolId, range],
  );
  const selectedTemplate = useMemo(
    () => templates.find((tpl) => tpl.id === selectedTemplateId) ?? null,
    [selectedTemplateId, templates],
  );
  const selectedTemplateIndicators = useMemo(() => {
    if (!selectedTemplate) return [] as CustomIndicator[];
    const keys = new Set<string>();
    collectBacktestIndicatorKeys(selectedTemplate.rule_config ?? {}, keys);
    return Array.from(keys)
      .map((key) => customIndicators.find((item) => item.key === key))
      .filter(Boolean) as CustomIndicator[];
  }, [customIndicators, selectedTemplate]);

  useEffect(() => {
    api.getBacktestTemplates().then(setTemplates).catch(() => {});
    api.getCustomIndicators({ scope: "backtest", enabled: true })
      .then((rows) => setCustomIndicators(rows as CustomIndicator[]))
      .catch(() => {});
  }, []);

  useEffect(() => {
    setCoverageWarning(null);
  }, [activeSymbolId, range]);

  const buildRuleConfig = useCallback(() => {
    if (ruleMode === "advanced") {
      return {
        version: 2,
        buy_conditions: buyConditions,
        sell_conditions: sellConditions,
        position_config: { type: "fixed_pct", value: positionPct / 100, max_positions: maxPositions },
        execution_config: { entry_price_field: entryPriceField, exit_price_field: exitPriceField },
      };
    }
    return {
      buy_conditions: { quality_score_min: qualityMin, timing_score_min: timingMin, stages, actions },
      sell_conditions: { take_profit_pct: takeProfitPct / 100, stop_loss_pct: stopLossPct / 100, max_hold_days: maxHoldDays },
      position_config: { type: "fixed_pct", value: positionPct / 100, max_positions: maxPositions },
      execution_config: { entry_price_field: entryPriceField, exit_price_field: exitPriceField },
    };
  }, [
    actions,
    buyConditions,
    entryPriceField,
    exitPriceField,
    maxHoldDays,
    maxPositions,
    positionPct,
    qualityMin,
    ruleMode,
    sellConditions,
    slippageRate,
    stages,
    stopLossPct,
    takeProfitPct,
    timingMin,
  ]);

  const runBacktest = async () => {
    if (disabled || !portfolioId || !activeSymbolId) return;
    setRunning(true);
    setCoverageWarning(null);
    try {
      const result = await api.runBacktest({
        portfolio_id: portfolioId,
        symbol_ids: [activeSymbolId],
        start_date: range[0].format("YYYY-MM-DD"),
        end_date: range[1].format("YYYY-MM-DD"),
        run_name: runName || t("backtestRunName"),
        rule_config: buildRuleConfig(),
        cost_config: {
          commission_rate: commissionRate / 100,
          min_commission: minCommission,
          stamp_tax_rate: stampTaxRate / 100,
          slippage_rate: slippageRate / 100,
        },
      });
      const detail = await api.getBacktestRun(result.id);
      onResult(detail);
      message.success(t("backtestRunSuccess"));
    } catch (err: any) {
      const detail = err?.detail as BacktestCoverageWarning | undefined;
      if (detail?.code === "BACKTEST_SCORE_COVERAGE_INSUFFICIENT") {
        setCoverageWarning(detail);
        message.warning(t("btCoverageInsufficient"));
      } else {
        message.error(err?.message || t("backtestFailed"));
      }
    } finally {
      setRunning(false);
    }
  };

  const loadTemplate = useCallback((id: number) => {
    const tpl = templates.find((item) => item.id === id);
    if (!tpl) return;
    const cfg = tpl.rule_config as BacktestTemplateConfig;
    if (cfg?.version === 2) {
      setRuleMode("advanced");
      setBuyConditions(cfg.buy_conditions || structuredClone(DEFAULT_BUY_GROUP));
      setSellConditions(cfg.sell_conditions || structuredClone(DEFAULT_SELL_GROUP));
      if (cfg.position_config) {
        if (cfg.position_config.value != null) setPositionPct(Number(cfg.position_config.value) * 100);
        if (cfg.position_config.max_positions != null) setMaxPositions(Number(cfg.position_config.max_positions));
      }
      if (cfg.execution_config) {
        setEntryPriceField((cfg.execution_config.entry_price_field as "open" | "close") || "close");
        setExitPriceField((cfg.execution_config.exit_price_field as "open" | "close") || "close");
      }
    }
    setSelectedTemplateId(id);
    message.success(`${t("backtestTemplateLoaded")}: ${tpl.name}`);
  }, [templates]);

  const saveTemplate = useCallback(async () => {
    if (!templateName.trim()) {
      message.warning(t("backtestTemplateNameRequired"));
      return;
    }
    try {
      await api.createBacktestTemplate({
        name: templateName.trim(),
        description: templateDesc,
        rule_config: buildRuleConfig(),
      });
      message.success(t("backtestTemplateSaved"));
      setSaveModalOpen(false);
      setTemplateName("");
      setTemplateDesc("");
      const updated = await api.getBacktestTemplates();
      setTemplates(updated);
    } catch (err: any) {
      message.error(err?.message || t("backtestTemplateSaveFailed"));
    }
  }, [buildRuleConfig, templateDesc, templateName]);

  const updateTemplate = useCallback(async () => {
    if (!selectedTemplateId) return;
    try {
      await api.updateBacktestTemplate(selectedTemplateId, { rule_config: buildRuleConfig() });
      message.success(t("backtestTemplateUpdated"));
      const updated = await api.getBacktestTemplates();
      setTemplates(updated);
    } catch (err: any) {
      message.error(err?.message || t("backtestTemplateUpdateFailed"));
    }
  }, [buildRuleConfig, selectedTemplateId]);

  const deleteTemplate = useCallback(async (id: number) => {
    try {
      await api.deleteBacktestTemplate(id);
      message.success(t("backtestTemplateDeleted"));
      if (selectedTemplateId === id) setSelectedTemplateId(null);
      const updated = await api.getBacktestTemplates();
      setTemplates(updated);
    } catch (err: any) {
      message.error(err?.message || t("backtestTemplateDeleteFailed"));
    }
  }, [selectedTemplateId]);

  return (
    <section className="backtest-config">
      <div className="backtest-grid">
        <label>
          <span>{t("backtestRunName")}</span>
          <Input value={runName} onChange={(e) => setRunName(e.target.value)} />
        </label>
        <label>
          <span>{t("backtestDateRange")}</span>
          <DatePicker.RangePicker
            value={range}
            onChange={(value) => value?.[0] && value?.[1] && setRange([value[0], value[1]])}
            allowClear={false}
          />
        </label>
      </div>

      <div style={{ display: "flex", alignItems: "center", gap: 12, margin: "12px 0 8px" }}>
        <Radio.Group
          value={ruleMode}
          onChange={(e) => setRuleMode(e.target.value)}
          optionType="button"
          buttonStyle="solid"
          size="small"
        >
          <Radio.Button value="standard">{t("btModeStandard")}</Radio.Button>
          <Radio.Button value="advanced">{t("btModeAdvanced")}</Radio.Button>
        </Radio.Group>
      </div>

      {ruleMode === "advanced" && (
        <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 12, flexWrap: "wrap" }}>
          <Select
            size="small"
            style={{ minWidth: 180 }}
            placeholder={t("backtestSelectTemplate")}
            value={selectedTemplateId ?? undefined}
            onChange={(value) => {
              if (typeof value === "number") loadTemplate(value);
            }}
            options={templates.map((tpl) => ({ label: tpl.name, value: tpl.id }))}
            allowClear
            onClear={() => setSelectedTemplateId(null)}
          />
          <Button size="small" icon={<SaveOutlined />} onClick={() => setSaveModalOpen(true)}>
            {t("backtestSaveTemplate")}
          </Button>
          {selectedTemplateId && (
            <>
              <Button size="small" type="primary" onClick={updateTemplate}>{t("backtestUpdateTemplate")}</Button>
              <Popconfirm title={t("backtestDeleteTemplateConfirm")} onConfirm={() => deleteTemplate(selectedTemplateId)}>
                <Button size="small" danger icon={<DeleteOutlined />} />
              </Popconfirm>
            </>
          )}
        </div>
      )}

      {ruleMode === "advanced" && selectedTemplate && (
        <div className="panel-meta" style={{ marginBottom: 12 }}>
          <Space wrap>
            <span>{selectedTemplateIndicators.length ? t("backtestTemplateIndicators") : t("backtestTemplateIndicatorsEmpty")}</span>
            {selectedTemplateIndicators.map((indicator) => (
              <Tag key={indicator.key} color={indicator.value_type === "number" ? "gold" : "blue"}>
                {indicator.name}
              </Tag>
            ))}
          </Space>
        </div>
      )}

      {ruleMode === "standard" ? (
        <>
          <div className="backtest-subtitle">{t("backtestBuyRule")}</div>
          <div className="backtest-grid compact">
            <label><span>{t("backtestQualityMin")}</span><InputNumber min={0} max={100} value={qualityMin} onChange={(value) => setQualityMin(Number(value ?? 0))} /></label>
            <label><span>{t("backtestTimingMin")}</span><InputNumber min={0} max={100} value={timingMin} onChange={(value) => setTimingMin(Number(value ?? 0))} /></label>
            <label><span>{t("backtestStages")}</span><Select mode="multiple" value={stages} onChange={setStages} options={["start", "accel", "cooldown", "overheat"].map((value) => ({ label: t(`stage_${value}`), value }))} /></label>
            <label><span>{t("backtestActions")}</span><Select mode="multiple" value={actions} onChange={setActions} options={["open", "hold", "buy_dip", "reduce", "exit"].map((value) => ({ label: t(`action_${value}`), value }))} /></label>
          </div>

          <div className="backtest-subtitle">{t("backtestSellRule")}</div>
          <div className="backtest-grid compact">
            <label><span>{t("backtestTakeProfit")}</span><InputNumber min={0} max={100} value={takeProfitPct} onChange={(value) => setTakeProfitPct(Number(value ?? 0))} /></label>
            <label><span>{t("backtestStopLoss")}</span><InputNumber min={0} max={100} value={stopLossPct} onChange={(value) => setStopLossPct(Number(value ?? 0))} /></label>
            <label><span>{t("backtestMaxHoldDays")}</span><InputNumber min={1} max={365} value={maxHoldDays} onChange={(value) => setMaxHoldDays(Number(value ?? 1))} /></label>
          </div>
        </>
      ) : (
        <>
          <div className="backtest-subtitle">{t("backtestAdvancedBuyConditions")}</div>
          <ConditionBuilder value={buyConditions} onChange={setBuyConditions} side="buy" customIndicators={customIndicators} />
          <div className="backtest-subtitle">{t("backtestAdvancedSellConditions")}</div>
          <ConditionBuilder value={sellConditions} onChange={setSellConditions} side="sell" customIndicators={customIndicators} />
        </>
      )}

      <Collapse
        ghost
        size="small"
        defaultActiveKey={["position"]}
        items={[{
          key: "position",
          label: <span className="backtest-subtitle" style={{ margin: 0 }}>{t("backtestPositionPct")} / {t("backtestExecutionRule")}</span>,
          children: (
            <div className="backtest-grid compact">
              <label><span>{t("backtestPositionPct")}</span><InputNumber min={0.1} max={100} value={positionPct} onChange={(value) => setPositionPct(Number(value ?? 0))} /></label>
              <label><span>{t("backtestMaxPositions")}</span><InputNumber min={1} max={50} value={maxPositions} onChange={(value) => setMaxPositions(Number(value ?? 1))} /></label>
              <label>
                <span>{t("backtestEntryPriceField")}</span>
                <Select value={entryPriceField} onChange={setEntryPriceField} options={[{ label: t("btOpenPrice"), value: "open" }, { label: t("btClosePrice"), value: "close" }]} />
              </label>
              <label>
                <span>{t("backtestExitPriceField")}</span>
                <Select value={exitPriceField} onChange={setExitPriceField} options={[{ label: t("btOpenPrice"), value: "open" }, { label: t("btClosePrice"), value: "close" }]} />
              </label>
            </div>
          ),
        }]}
      />

      <Collapse
        ghost
        size="small"
        items={[{
          key: "cost",
          label: <span className="backtest-subtitle" style={{ margin: 0 }}>{t("backtestCostRule")}</span>,
          children: (
            <div className="backtest-grid compact">
              <label><span>{t("backtestCommissionRate")}</span><InputNumber min={0} max={5} step={0.01} value={commissionRate} onChange={(value) => setCommissionRate(Number(value ?? 0))} /></label>
              <label><span>{t("backtestMinCommission")}</span><InputNumber min={0} value={minCommission} onChange={(value) => setMinCommission(Number(value ?? 0))} /></label>
              <label><span>{t("backtestStampTaxRate")}</span><InputNumber min={0} max={5} step={0.01} value={stampTaxRate} onChange={(value) => setStampTaxRate(Number(value ?? 0))} /></label>
              <label><span>{t("backtestSlippageRate")}</span><InputNumber min={0} max={5} step={0.01} value={slippageRate} onChange={(value) => setSlippageRate(Number(value ?? 0))} /></label>
            </div>
          ),
        }]}
      />

      {coverageWarning && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message={t("btCoverageWarningTitle")}
          description={(
            <div style={{ display: "grid", gap: 8 }}>
              <span>
                {t("btCoverageRecommend")}
                {presetLabel(coverageWarning.summary.recommended_preset)}
                {". "}
                {t("btCoverageCurrentMin")}
                {coverageWarning.summary.min_coverage_pct ?? 0}%
                {"。"}
              </span>
              {coverageWarning.issues.slice(0, 3).map((issue) => (
                <span key={issue.symbol_id} className="panel-meta">
                  {(issue.symbol || issue.symbol_id)} {issue.name ? `(${issue.name})` : ""}
                  {t("btCoverageIssueScore")}
                  {issue.score_days}
                  {t("btCoverageIssueBar")}
                  {issue.bar_days}
                  {t("btCoverageIssueMissing")}
                  {issue.missing_days}
                  {t("btCoverageIssueDays")}
                </span>
              ))}
            </div>
          )}
        />
      )}

      <Space className="backtest-actions">
        <Button type="primary" loading={running} disabled={disabled} onClick={runBacktest}>{t("runBacktest")}</Button>
        {disabled && <span className="panel-meta">{t("backtestRunHint")}</span>}
      </Space>

      <Modal
        title={t("backtestTemplateModalTitle")}
        open={saveModalOpen}
        onOk={saveTemplate}
        onCancel={() => setSaveModalOpen(false)}
        okText={t("save")}
        cancelText={t("cancel")}
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <label>
            <span>{t("backtestTemplateName")}</span>
            <Input value={templateName} onChange={(e) => setTemplateName(e.target.value)} placeholder={t("backtestTemplateNamePlaceholder")} />
          </label>
          <label>
            <span>{t("backtestTemplateDescription")}</span>
            <Input.TextArea value={templateDesc} onChange={(e) => setTemplateDesc(e.target.value)} rows={2} placeholder={t("backtestTemplateDescriptionPlaceholder")} />
          </label>
        </div>
      </Modal>
    </section>
  );
}