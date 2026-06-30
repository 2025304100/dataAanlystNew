from pathlib import Path
path = Path('frontend/src/components/InvestmentCenter.tsx')
text = path.read_text(encoding='utf-8')
text = text.replace('import { Button, Input, Progress } from "antd";', 'import { Button, Input, InputNumber, Progress, Switch, Tabs } from "antd";')
text = text.replace('import type { FutureBuyPlan, TradeSetup } from "../types";', 'import type { FutureBuyPlan, TradeSetup, TradeSetupOverrides } from "../types";')
anchor = '''interface InvestmentCenterProps {
  openMetricModal: (type: string) => void;
}
'''
insert = '''interface InvestmentCenterProps {
  openMetricModal: (type: string) => void;
}

type TradePlanDraft = {
  entry_min: number | null;
  entry_max: number | null;
  stop_loss: number | null;
  target_price: number | null;
  recommended_position_pct: number | null;
  recommended_position_amount: number | null;
};

type RiskSettings = {
  atrMultiplier: number;
  concentrationMediumPct: number;
  concentrationHighPct: number;
  maxLossPct: number;
};

type AlertSettings = {
  enableStopLoss: boolean;
  enableTarget: boolean;
  enableRsi: boolean;
  enableMacd: boolean;
  stopNearPct: number;
  targetNearPct: number;
  rsiOverbought: number;
  rsiOversold: number;
};

const DEFAULT_RISK_SETTINGS: RiskSettings = {
  atrMultiplier: 2,
  concentrationMediumPct: 10,
  concentrationHighPct: 20,
  maxLossPct: 2,
};

const DEFAULT_ALERT_SETTINGS: AlertSettings = {
  enableStopLoss: true,
  enableTarget: true,
  enableRsi: true,
  enableMacd: true,
  stopNearPct: 3,
  targetNearPct: 5,
  rsiOverbought: 75,
  rsiOversold: 25,
};

function readStoredObject<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key);
    return raw ? { ...fallback, ...JSON.parse(raw) } : fallback;
  } catch {
    return fallback;
  }
}

function planDraftFromSetup(setup: TradeSetup): TradePlanDraft {
  return {
    entry_min: setup.entry_min,
    entry_max: setup.entry_max,
    stop_loss: setup.stop_loss,
    target_price: setup.target_price,
    recommended_position_pct: Math.round((setup.recommended_position_pct ?? 0) * 10000) / 100,
    recommended_position_amount: setup.recommended_position_amount,
  };
}
'''
if anchor not in text:
    raise SystemExit('props anchor not found')
text = text.replace(anchor, insert)
state_anchor = '''  const [priceAlerts, setPriceAlerts] = useState<Array<{
    id: string; type: string; level: "warning" | "danger" | "info";
    message: string; detail: string; timestamp: number;
  }>>([]);
'''
state_insert = state_anchor + '''
  const [tradePlanEditing, setTradePlanEditing] = useState(false);
  const [tradePlanDraft, setTradePlanDraft] = useState<TradePlanDraft | null>(null);
  const [riskSettingsOpen, setRiskSettingsOpen] = useState(false);
  const [alertSettingsOpen, setAlertSettingsOpen] = useState(false);
  const [riskSettings, setRiskSettings] = useState<RiskSettings>(() => readStoredObject("ic_risk_settings", DEFAULT_RISK_SETTINGS));
  const [alertSettings, setAlertSettings] = useState<AlertSettings>(() => readStoredObject("ic_alert_settings", DEFAULT_ALERT_SETTINGS));
'''
if state_anchor not in text:
    raise SystemExit('state anchor not found')
text = text.replace(state_anchor, state_insert)
setup_anchor = '''  const setup = detail?.latest_trade_setup ?? null;
'''
setup_insert = '''  const setup = detail?.latest_trade_setup ?? null;
  const zh = ctx.locale === "zh-CN";
  const icText = {
    editPlan: zh ? "编辑计划" : "Edit plan",
    exitEdit: zh ? "退出编辑" : "Exit edit",
    savePlan: zh ? "重新计算" : "Recalculate",
    cancelEdit: zh ? "取消" : "Cancel",
    manual: zh ? "手动" : "Manual",
    system: zh ? "系统" : "System",
    overview: zh ? "计划概览" : "Overview",
    scenario: zh ? "收益预演" : "Scenarios",
    tranches: zh ? "分批执行" : "Tranches",
    future: zh ? "未来计划" : "Future plan",
    riskSettings: zh ? "风险参数" : "Risk settings",
    alertSettings: zh ? "预警设置" : "Alert settings",
    atrMultiplier: zh ? "ATR倍数" : "ATR multiplier",
    mediumPosition: zh ? "中等仓位%" : "Medium position %",
    highPosition: zh ? "高仓位%" : "High position %",
    maxLossPct: zh ? "单笔最大亏损%" : "Max loss %",
    stopNearPct: zh ? "止损接近%" : "Near stop %",
    targetNearPct: zh ? "目标接近%" : "Near target %",
    rsiOverbought: zh ? "RSI超买" : "RSI overbought",
    rsiOversold: zh ? "RSI超卖" : "RSI oversold",
    enableStopLoss: zh ? "止损" : "Stop",
    enableTarget: zh ? "目标" : "Target",
    enableRsi: zh ? "RSI" : "RSI",
    enableMacd: zh ? "MACD" : "MACD",
    invalidPlan: zh ? "买入区间下限不能大于上限" : "Buy zone min cannot exceed max",
  };

  useEffect(() => {
    if (setup && !tradePlanEditing) setTradePlanDraft(planDraftFromSetup(setup));
  }, [setup?.id, setup?.entry_min, setup?.entry_max, setup?.stop_loss, setup?.target_price, setup?.recommended_position_pct, setup?.recommended_position_amount, tradePlanEditing]);

  useEffect(() => {
    try { localStorage.setItem("ic_risk_settings", JSON.stringify(riskSettings)); } catch {}
  }, [riskSettings]);

  useEffect(() => {
    try { localStorage.setItem("ic_alert_settings", JSON.stringify(alertSettings)); } catch {}
  }, [alertSettings]);

  const updateTradePlanDraft = useCallback((field: keyof TradePlanDraft, value: number | null) => {
    setTradePlanDraft((prev) => ({ ...(prev ?? {} as TradePlanDraft), [field]: value }));
  }, []);

  const handleEditPlan = useCallback(() => {
    if (!setup) return;
    setTradePlanDraft(planDraftFromSetup(setup));
    setTradePlanEditing(true);
  }, [setup]);

  const handleCancelPlanEdit = useCallback(() => {
    if (setup) setTradePlanDraft(planDraftFromSetup(setup));
    setTradePlanEditing(false);
  }, [setup]);

  const handleApplyPlanOverrides = useCallback(async () => {
    if (!tradePlanDraft) return;
    if (tradePlanDraft.entry_min != null && tradePlanDraft.entry_max != null && tradePlanDraft.entry_min > tradePlanDraft.entry_max) {
      ctx.showToast("error", icText.invalidPlan);
      return;
    }
    const overrides: TradeSetupOverrides = {
      entry_min: tradePlanDraft.entry_min,
      entry_max: tradePlanDraft.entry_max,
      stop_loss: tradePlanDraft.stop_loss,
      target_price: tradePlanDraft.target_price,
      recommended_position_pct: tradePlanDraft.recommended_position_pct != null ? tradePlanDraft.recommended_position_pct / 100 : null,
      recommended_position_amount: tradePlanDraft.recommended_position_amount,
    };
    setRefreshingPlan(true);
    try {
      await ctx.generateTradeSetup(overrides);
      setTradePlanEditing(false);
    } finally {
      setRefreshingPlan(false);
    }
  }, [ctx, icText.invalidPlan, tradePlanDraft]);

  const fieldSourceBadge = useCallback((field: string) => {
    if (setup?.field_sources?.[field] !== "manual") return null;
    return <span className="ic__manual-badge">{icText.manual}</span>;
  }, [setup?.field_sources, icText.manual]);
'''
if setup_anchor not in text:
    raise SystemExit('setup anchor not found')
text = text.replace(setup_anchor, setup_insert)
path.write_text(text, encoding='utf-8')
