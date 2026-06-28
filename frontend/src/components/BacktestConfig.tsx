import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Button, Collapse, DatePicker, Input, InputNumber, Modal,
  Popconfirm, Radio, Select, Space, message,
} from "antd";
import { SaveOutlined, DeleteOutlined } from "@ant-design/icons";
import dayjs from "dayjs";
import { api } from "../api/client";
import { t } from "../i18n";
import type { BacktestRun, ConditionGroup } from "../types";
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

export default function BacktestConfig({ portfolioId, activeSymbolId, onResult }: BacktestConfigProps) {
  const [running, setRunning] = useState(false);
  const [runName, setRunName] = useState(t("backtestRunName"));
  const [ruleMode, setRuleMode] = useState<RuleMode>("standard");
  const [range, setRange] = useState<[dayjs.Dayjs, dayjs.Dayjs]>([
    dayjs().subtract(1, "year"),
    dayjs(),
  ]);

  // ── v1 standard mode state ──
  const [qualityMin, setQualityMin] = useState(60);
  const [timingMin, setTimingMin] = useState(55);
  const [stages, setStages] = useState<string[]>(DEFAULT_STAGES);
  const [actions, setActions] = useState<string[]>(DEFAULT_ACTIONS);
  const [takeProfitPct, setTakeProfitPct] = useState(15);
  const [stopLossPct, setStopLossPct] = useState(8);
  const [maxHoldDays, setMaxHoldDays] = useState(30);

  // ── v2 advanced mode state ──
  const [buyConditions, setBuyConditions] = useState<ConditionGroup>(structuredClone(DEFAULT_BUY_GROUP));
  const [sellConditions, setSellConditions] = useState<ConditionGroup>(structuredClone(DEFAULT_SELL_GROUP));

  // ── shared state ──
  const [positionPct, setPositionPct] = useState(5);
  const [maxPositions, setMaxPositions] = useState(5);
  const [entryPriceField, setEntryPriceField] = useState<"open" | "close">("close");
  const [exitPriceField, setExitPriceField] = useState<"open" | "close">("close");
  const [commissionRate, setCommissionRate] = useState(0.03);
  const [minCommission, setMinCommission] = useState(5);
  const [stampTaxRate, setStampTaxRate] = useState(0.1);
  const [slippageRate, setSlippageRate] = useState(0.1);

  // ── template state ──
  const [templates, setTemplates] = useState<any[]>([]);
  const [selectedTemplateId, setSelectedTemplateId] = useState<number | null>(null);
  const [saveModalOpen, setSaveModalOpen] = useState(false);
  const [templateName, setTemplateName] = useState("");
  const [templateDesc, setTemplateDesc] = useState("");

  const disabled = useMemo(() => !portfolioId || !activeSymbolId || !range?.[0] || !range?.[1], [portfolioId, activeSymbolId, range]);

  // Load templates on mount
  useEffect(() => {
    api.getBacktestTemplates().then(setTemplates).catch(() => {});
  }, []);

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
  }, [ruleMode, buyConditions, sellConditions, qualityMin, timingMin, stages, actions,
      takeProfitPct, stopLossPct, maxHoldDays, positionPct, maxPositions, entryPriceField, exitPriceField]);

  const runBacktest = async () => {
    if (disabled || !portfolioId || !activeSymbolId) return;
    setRunning(true);
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
    } catch (err: any) {
      message.error(err?.message || t("backtestFailed"));
    } finally {
      setRunning(false);
    }
  };

  // ── template handlers ──
  const loadTemplate = useCallback((id: number) => {
    const tpl = templates.find((t: any) => t.id === id);
    if (!tpl) return;
    const cfg = tpl.rule_config;
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
    message.success(`已加载模板: ${tpl.name}`);
  }, [templates]);

  const saveTemplate = useCallback(async () => {
    if (!templateName.trim()) { message.warning("请输入模板名称"); return; }
    try {
      await api.createBacktestTemplate({
        name: templateName.trim(),
        description: templateDesc,
        rule_config: buildRuleConfig(),
      });
      message.success("模板已保存");
      setSaveModalOpen(false);
      setTemplateName("");
      setTemplateDesc("");
      const updated = await api.getBacktestTemplates();
      setTemplates(updated);
    } catch (err: any) {
      message.error(err?.message || "保存失败");
    }
  }, [templateName, templateDesc, buildRuleConfig]);

  const updateTemplate = useCallback(async () => {
    if (!selectedTemplateId) return;
    try {
      await api.updateBacktestTemplate(selectedTemplateId, { rule_config: buildRuleConfig() });
      message.success("模板已更新");
      const updated = await api.getBacktestTemplates();
      setTemplates(updated);
    } catch (err: any) {
      message.error(err?.message || "更新失败");
    }
  }, [selectedTemplateId, buildRuleConfig]);

  const deleteTemplate = useCallback(async (id: number) => {
    try {
      await api.deleteBacktestTemplate(id);
      message.success("模板已删除");
      if (selectedTemplateId === id) setSelectedTemplateId(null);
      const updated = await api.getBacktestTemplates();
      setTemplates(updated);
    } catch (err: any) {
      message.error(err?.message || "删除失败");
    }
  }, [selectedTemplateId]);

  // ── render ──
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
            onChange={(v) => v?.[0] && v?.[1] && setRange([v[0], v[1]])}
            allowClear={false}
          />
        </label>
      </div>

      {/* 规则模式切换 */}
      <div style={{ display: "flex", alignItems: "center", gap: 12, margin: "12px 0 8px" }}>
        <Radio.Group
          value={ruleMode}
          onChange={(e) => setRuleMode(e.target.value)}
          optionType="button"
          buttonStyle="solid"
          size="small"
        >
          <Radio.Button value="standard">标准模式</Radio.Button>
          <Radio.Button value="advanced">高级模式</Radio.Button>
        </Radio.Group>
      </div>

      {/* 高级模式：模板管理 */}
      {ruleMode === "advanced" && (
        <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 12, flexWrap: "wrap" }}>
          <Select
            size="small"
            style={{ minWidth: 180 }}
            placeholder="选择模板..."
            value={selectedTemplateId}
            onChange={(v) => { setSelectedTemplateId(v); loadTemplate(v); }}
            options={templates.map((tpl: any) => ({ label: tpl.name, value: tpl.id }))}
            allowClear
            onClear={() => setSelectedTemplateId(null)}
          />
          <Button size="small" icon={<SaveOutlined />} onClick={() => setSaveModalOpen(true)}>
            保存为模板
          </Button>
          {selectedTemplateId && (
            <>
              <Button size="small" type="primary" onClick={updateTemplate}>更新模板</Button>
              <Popconfirm title="确定删除此模板？" onConfirm={() => deleteTemplate(selectedTemplateId)}>
                <Button size="small" danger icon={<DeleteOutlined />} />
              </Popconfirm>
            </>
          )}
        </div>
      )}

      {ruleMode === "standard" ? (
        <>
          <div className="backtest-subtitle">{t("backtestBuyRule")}</div>
          <div className="backtest-grid compact">
            <label><span>{t("backtestQualityMin")}</span><InputNumber min={0} max={100} value={qualityMin} onChange={(v) => setQualityMin(Number(v ?? 0))} /></label>
            <label><span>{t("backtestTimingMin")}</span><InputNumber min={0} max={100} value={timingMin} onChange={(v) => setTimingMin(Number(v ?? 0))} /></label>
            <label><span>{t("backtestStages")}</span><Select mode="multiple" value={stages} onChange={setStages} options={["start", "accel", "cooldown", "overheat"].map(v => ({ label: t(`stage_${v}`), value: v }))} /></label>
            <label><span>{t("backtestActions")}</span><Select mode="multiple" value={actions} onChange={setActions} options={["open", "hold", "buy_dip", "reduce", "exit"].map(v => ({ label: t(`action_${v}`), value: v }))} /></label>
          </div>

          <div className="backtest-subtitle">{t("backtestSellRule")}</div>
          <div className="backtest-grid compact">
            <label><span>{t("backtestTakeProfit")}</span><InputNumber min={0} max={100} value={takeProfitPct} onChange={(v) => setTakeProfitPct(Number(v ?? 0))} /></label>
            <label><span>{t("backtestStopLoss")}</span><InputNumber min={0} max={100} value={stopLossPct} onChange={(v) => setStopLossPct(Number(v ?? 0))} /></label>
            <label><span>{t("backtestMaxHoldDays")}</span><InputNumber min={1} max={365} value={maxHoldDays} onChange={(v) => setMaxHoldDays(Number(v ?? 1))} /></label>
          </div>
        </>
      ) : (
        <>
          <div className="backtest-subtitle">买入规则</div>
          <ConditionBuilder value={buyConditions} onChange={setBuyConditions} side="buy" />
          <div className="backtest-subtitle">卖出规则</div>
          <ConditionBuilder value={sellConditions} onChange={setSellConditions} side="sell" />
        </>
      )}

      {/* 仓位 + 成交口径 (两种模式共享) */}
      <Collapse
        ghost
        size="small"
        defaultActiveKey={["position"]}
        items={[{
          key: "position",
          label: <span className="backtest-subtitle" style={{ margin: 0 }}>{t("backtestPositionPct")} & {t("backtestExecutionRule")}</span>,
          children: (
            <div className="backtest-grid compact">
              <label><span>{t("backtestPositionPct")}</span><InputNumber min={0.1} max={100} value={positionPct} onChange={(v) => setPositionPct(Number(v ?? 0))} /></label>
              <label><span>{t("backtestMaxPositions")}</span><InputNumber min={1} max={50} value={maxPositions} onChange={(v) => setMaxPositions(Number(v ?? 1))} /></label>
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

      {/* 交易成本 */}
      <Collapse
        ghost
        size="small"
        items={[{
          key: "cost",
          label: <span className="backtest-subtitle" style={{ margin: 0 }}>{t("backtestCostRule")}</span>,
          children: (
            <div className="backtest-grid compact">
              <label><span>{t("backtestCommissionRate")}</span><InputNumber min={0} max={5} step={0.01} value={commissionRate} onChange={(v) => setCommissionRate(Number(v ?? 0))} /></label>
              <label><span>{t("backtestMinCommission")}</span><InputNumber min={0} value={minCommission} onChange={(v) => setMinCommission(Number(v ?? 0))} /></label>
              <label><span>{t("backtestStampTaxRate")}</span><InputNumber min={0} max={5} step={0.01} value={stampTaxRate} onChange={(v) => setStampTaxRate(Number(v ?? 0))} /></label>
              <label><span>{t("backtestSlippageRate")}</span><InputNumber min={0} max={5} step={0.01} value={slippageRate} onChange={(v) => setSlippageRate(Number(v ?? 0))} /></label>
            </div>
          ),
        }]}
      />

      <Space className="backtest-actions">
        <Button type="primary" loading={running} disabled={disabled} onClick={runBacktest}>{t("runBacktest")}</Button>
        {disabled && <span className="panel-meta">{t("backtestRunHint")}</span>}
      </Space>

      {/* 保存模板弹窗 */}
      <Modal
        title="保存为回测模板"
        open={saveModalOpen}
        onOk={saveTemplate}
        onCancel={() => setSaveModalOpen(false)}
        okText="保存"
        cancelText="取消"
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <label>
            <span>模板名称</span>
            <Input value={templateName} onChange={(e) => setTemplateName(e.target.value)} placeholder="如：稳健买入策略" />
          </label>
          <label>
            <span>描述（可选）</span>
            <Input.TextArea value={templateDesc} onChange={(e) => setTemplateDesc(e.target.value)} rows={2} placeholder="模板说明..." />
          </label>
        </div>
      </Modal>
    </section>
  );
}
