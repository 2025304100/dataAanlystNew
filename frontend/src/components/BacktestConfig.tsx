import { useMemo, useState } from "react";
import { Button, DatePicker, Input, InputNumber, Select, Space, message } from "antd";
import dayjs from "dayjs";
import { api } from "../api/client";
import { t } from "../i18n";
import type { BacktestRun } from "../types";

interface BacktestConfigProps {
  portfolioId: number | null;
  activeSymbolId: number | null;
  onResult: (run: BacktestRun) => void;
}

const DEFAULT_STAGES = ["start", "accel"];
const DEFAULT_ACTIONS = ["open", "hold", "buy_dip"];

export default function BacktestConfig({ portfolioId, activeSymbolId, onResult }: BacktestConfigProps) {
  const [running, setRunning] = useState(false);
  const [runName, setRunName] = useState(t("backtestRunName"));
  const [qualityMin, setQualityMin] = useState(60);
  const [timingMin, setTimingMin] = useState(55);
  const [stages, setStages] = useState<string[]>(DEFAULT_STAGES);
  const [actions, setActions] = useState<string[]>(DEFAULT_ACTIONS);
  const [takeProfitPct, setTakeProfitPct] = useState(15);
  const [stopLossPct, setStopLossPct] = useState(8);
  const [maxHoldDays, setMaxHoldDays] = useState(30);
  const [positionPct, setPositionPct] = useState(5);
  const [maxPositions, setMaxPositions] = useState(5);
  const [commissionRate, setCommissionRate] = useState(0.03);
  const [minCommission, setMinCommission] = useState(5);
  const [stampTaxRate, setStampTaxRate] = useState(0.1);
  const [slippageRate, setSlippageRate] = useState(0.1);
  const [range, setRange] = useState<[dayjs.Dayjs, dayjs.Dayjs]>([
    dayjs().subtract(1, "year"),
    dayjs(),
  ]);

  const disabled = useMemo(() => !portfolioId || !activeSymbolId || !range?.[0] || !range?.[1], [portfolioId, activeSymbolId, range]);

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
        rule_config: {
          buy_conditions: {
            quality_score_min: qualityMin,
            timing_score_min: timingMin,
            stages,
            actions,
          },
          sell_conditions: {
            take_profit_pct: takeProfitPct / 100,
            stop_loss_pct: stopLossPct / 100,
            max_hold_days: maxHoldDays,
          },
          position_config: {
            type: "fixed_pct",
            value: positionPct / 100,
            max_positions: maxPositions,
          },
        },
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

      <div className="backtest-subtitle">{t("backtestBuyRule")}</div>
      <div className="backtest-grid compact">
        <label><span>{t("backtestQualityMin")}</span><InputNumber min={0} max={100} value={qualityMin} onChange={(v) => setQualityMin(Number(v ?? 0))} /></label>
        <label><span>{t("backtestTimingMin")}</span><InputNumber min={0} max={100} value={timingMin} onChange={(v) => setTimingMin(Number(v ?? 0))} /></label>
        <label><span>{t("backtestStages")}</span><Select mode="multiple" value={stages} onChange={setStages} options={["start", "accel", "cooldown", "overheat"].map(v => ({ label: v, value: v }))} /></label>
        <label><span>{t("backtestActions")}</span><Select mode="multiple" value={actions} onChange={setActions} options={["open", "hold", "buy_dip", "reduce", "exit"].map(v => ({ label: v, value: v }))} /></label>
      </div>

      <div className="backtest-subtitle">{t("backtestSellRule")}</div>
      <div className="backtest-grid compact">
        <label><span>{t("backtestTakeProfit")}</span><InputNumber min={0} max={100} value={takeProfitPct} onChange={(v) => setTakeProfitPct(Number(v ?? 0))} /></label>
        <label><span>{t("backtestStopLoss")}</span><InputNumber min={0} max={100} value={stopLossPct} onChange={(v) => setStopLossPct(Number(v ?? 0))} /></label>
        <label><span>{t("backtestMaxHoldDays")}</span><InputNumber min={1} max={365} value={maxHoldDays} onChange={(v) => setMaxHoldDays(Number(v ?? 1))} /></label>
        <label><span>{t("backtestPositionPct")}</span><InputNumber min={0.1} max={100} value={positionPct} onChange={(v) => setPositionPct(Number(v ?? 0))} /></label>
        <label><span>{t("backtestMaxPositions")}</span><InputNumber min={1} max={50} value={maxPositions} onChange={(v) => setMaxPositions(Number(v ?? 1))} /></label>
      </div>

      <div className="backtest-subtitle">{t("backtestCostRule")}</div>
      <div className="backtest-grid compact">
        <label><span>{t("backtestCommissionRate")}</span><InputNumber min={0} max={5} step={0.01} value={commissionRate} onChange={(v) => setCommissionRate(Number(v ?? 0))} /></label>
        <label><span>{t("backtestMinCommission")}</span><InputNumber min={0} value={minCommission} onChange={(v) => setMinCommission(Number(v ?? 0))} /></label>
        <label><span>{t("backtestStampTaxRate")}</span><InputNumber min={0} max={5} step={0.01} value={stampTaxRate} onChange={(v) => setStampTaxRate(Number(v ?? 0))} /></label>
        <label><span>{t("backtestSlippageRate")}</span><InputNumber min={0} max={5} step={0.01} value={slippageRate} onChange={(v) => setSlippageRate(Number(v ?? 0))} /></label>
      </div>

      <Space className="backtest-actions">
        <Button type="primary" loading={running} disabled={disabled} onClick={runBacktest}>{t("runBacktest")}</Button>
        {disabled && <span className="panel-meta">{t("backtestRunHint")}</span>}
      </Space>
    </section>
  );
}
