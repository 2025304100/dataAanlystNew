import { useEffect, useId, useRef, useState } from "react";
import { Popover } from "antd";
import { QuestionCircleOutlined } from "@ant-design/icons";
import { t } from "../../../../../i18n";

const METRICS: Record<string, string> = {
  market: "Markets", markets: "Markets", board: "Boards", boards: "Boards",
  total_market_cap: "TotalCap", circulating_market_cap: "FloatCap",
  pe_ttm: "Pe", pb: "Pb", dividend_yield: "Dividend",
  avg_amount: "Amount", avg_volume: "Volume", avg_turnover_rate: "Turnover",
  roe_ttm: "Roe", exclude_st: "St", exclude_delisting: "Delisting",
  exclude_suspended: "Suspended", index_member: "Index",
  min_listed_trading_days: "Listing", listing_time: "Listing", listing: "Listing",
  loss: "Loss", net_profit_yoy: "ProfitGrowth", industry: "Industry",
  market_cap: "CapPresets", valuation: "ValuationPresets", liquidity: "LiquidityPresets",
};

const RANGES = new Set(["total_market_cap", "circulating_market_cap", "pe_ttm", "pb",
  "dividend_yield", "avg_amount", "avg_volume", "avg_turnover_rate", "roe_ttm"]);

export function metricDisplayLabel(metric: string, fallbackLabel?: string): string {
  const key = METRICS[metric];
  return key ? t(`miningMetric${key}Label`) : fallbackLabel || metric;
}

export default function PoolMetricHelp({ metric, fallbackLabel }: {
  metric: string; fallbackLabel?: string;
}) {
  const [open, setOpen] = useState(false);
  const id = useId();
  const trigger = useRef<HTMLButtonElement>(null);
  const key = METRICS[metric];
  const label = metricDisplayLabel(metric, fallbackLabel);

  useEffect(() => {
    if (!open) return;
    const dismiss = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        setOpen(false);
        trigger.current?.focus();
      }
    };
    document.addEventListener("keydown", dismiss, true);
    return () => document.removeEventListener("keydown", dismiss, true);
  }, [open]);

  const content = (
    <div className="mining-metric-help-content" id={id}>
      <strong>{label}</strong>
      <div><span>{t("miningMetricHelpMeaning")}</span>
        <p>{key ? t(`miningMetric${key}Meaning`) : t("miningMetricHelpGeneric")}</p></div>
      <div><span>{t("miningMetricHelpUsage")}</span>
        <p>{key ? t(`miningMetric${key}Usage`) : t("miningMetricHelpGenericUsage")}</p></div>
      {RANGES.has(metric) && <div><span>{t("miningMetricHelpRange")}</span>
        <p>{t("miningMetricHelpRangeBody")}</p></div>}
    </div>
  );

  return <Popover content={content} trigger={["hover"]}
    open={open} onOpenChange={setOpen} placement="topLeft"
    overlayClassName="mining-metric-popover">
    <button type="button" ref={trigger} className="mining-metric-help"
      aria-label={t("miningMetricHelpAria").replace("{metric}", label)}
      aria-expanded={open} aria-describedby={open ? id : undefined}
      onFocus={() => setOpen(true)} onBlur={() => setOpen(false)}
      onClick={(event) => { event.stopPropagation(); setOpen(true); }}
      onKeyDown={(event) => event.stopPropagation()}>
      <QuestionCircleOutlined aria-hidden="true" />
    </button>
  </Popover>;
}
