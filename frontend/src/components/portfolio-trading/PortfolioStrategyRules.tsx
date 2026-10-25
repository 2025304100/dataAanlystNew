import React, { useCallback, useEffect, useRef, useState } from "react";
import { Bot, Plus, AlertTriangle, Save, RotateCcw, FileText, Info, ShieldAlert, Snowflake } from "lucide-react";
import { Button, Input, InputNumber, Select } from "antd";
import { api } from "../../api/client";
import type { ScoringModelBrief, ScoringFactorSetBrief, PortfolioFactorUsageState } from "../../api/client";
import { actionLabel, t } from "../../i18n";
import { govStatusCodeForTooltip, govStatusLabel } from "../../utils/govStatusLabel";
import { useApp } from "../../context/AppContext";
import type { PortfolioStatePermissions, PortfolioStatusResponse, SignalRule } from "../../types";
import { openSettingsSection } from "../../utils/navigate";

/**
 * PortfolioStrategyRules — 策略规则子 Tab（Task 6）
 *
 * 一比一还原原型图「应用主框架.html」策略规则区域：
 *   1. 顶部自动接管系统配置卡片（Bot 图标 + 标题 + 描述 + 状态徽章 + 总开关）
 *   2. 左侧「1. 因子模型配置」卡片（模型下拉 + 复制/新建 + 选股池/调仓周期/权重分配下拉 + 激活因子 chips + 总权重进度条）
 *   3. 右侧「2. 条件模型与风控」卡片（择时信号 + 单股持仓上限 + 个股止损 + 组合回撤熔断 + 警告 banner）
 *   4. 底部保存操作（恢复默认 / 保存草稿 / 保存并应用）
 *
 * 数据/API：
 *   - POST /api/v1/portfolios/{id}/rules   → api.upsertPortfolioRule（保存因子/风控策略配置）
 *   - POST /api/v1/portfolios/{id}/signal-rule → api.saveSignalRule（保存择时/止损等信号规则，保留已有字段）
 *   - GET  /api/v1/portfolios/{id}/signal-rule → api.getSignalRule（加载已有信号规则，保存时合并避免覆盖）
 *
 * i18n：使用 t('portfolioTrading.strategy.xxx')，key 由 Task 13 补全，缺失时返回 key 字符串不阻塞。
 */
interface PortfolioStrategyRulesProps {
  portfolioId: number;
  onNavigate?: (tab: string) => void;
  // FR-P1-8a HG1
  portfolioStatus?: PortfolioStatusResponse | null;
  perm?: PortfolioStatePermissions;
  onConfigurationChanged?: (portfolioId: number) => void | Promise<void>;
}

interface FactorItem {
  name: string;
  /** 默认权重（激活时回填） */
  defaultWeight: number;
  /** 当前权重（停用时为 0） */
  weight: number;
  active: boolean;
}

// 默认激活因子：动量 30% / 价值 25% / 低波 20% / 质量 15% / 成长 10%，总权重 100%
const DEFAULT_FACTORS: FactorItem[] = [
  { name: "动量", defaultWeight: 30, weight: 30, active: true },
  { name: "价值", defaultWeight: 25, weight: 25, active: true },
  { name: "低波", defaultWeight: 20, weight: 20, active: true },
  { name: "质量", defaultWeight: 15, weight: 15, active: true },
  { name: "成长", defaultWeight: 10, weight: 10, active: true },
];

const FACTOR_MODELS = ["多因子增强模型", "价值成长平衡", "低波质量", "动量轮动"];

// 因子模型 → 预设权重（打通"选模型"与"下面的因子芯片权重"）
// 模型含义：
//   多因子增强模型 — 默认均匀混合
//   价值成长平衡 — 价值45 + 成长25，双主线
//   低波质量     — 质量40 + 低波35，稳健防御
//   动量轮动     — 动量50 + 规模20，进攻追涨
const FACTOR_MODEL_PRESETS: Record<string, FactorItem[]> = {
  "多因子增强模型": [
    { name: "规模", defaultWeight: 10, weight: 10, active: true },
    { name: "动量", defaultWeight: 30, weight: 30, active: true },
    { name: "价值", defaultWeight: 25, weight: 25, active: true },
    { name: "低波", defaultWeight: 20, weight: 20, active: true },
    { name: "质量", defaultWeight: 15, weight: 15, active: true },
    { name: "成长", defaultWeight: 10, weight: 10, active: true },
  ],
  "价值成长平衡": [
    { name: "规模", defaultWeight: 5, weight: 5, active: true },
    { name: "动量", defaultWeight: 10, weight: 10, active: true },
    { name: "价值", defaultWeight: 45, weight: 45, active: true },
    { name: "低波", defaultWeight: 10, weight: 10, active: true },
    { name: "质量", defaultWeight: 5, weight: 5, active: true },
    { name: "成长", defaultWeight: 25, weight: 25, active: true },
  ],
  "低波质量": [
    { name: "规模", defaultWeight: 5, weight: 5, active: true },
    { name: "动量", defaultWeight: 5, weight: 5, active: false },
    { name: "价值", defaultWeight: 10, weight: 10, active: true },
    { name: "低波", defaultWeight: 35, weight: 35, active: true },
    { name: "质量", defaultWeight: 40, weight: 40, active: true },
    { name: "成长", defaultWeight: 5, weight: 5, active: true },
  ],
  "动量轮动": [
    { name: "规模", defaultWeight: 20, weight: 20, active: true },
    { name: "动量", defaultWeight: 50, weight: 50, active: true },
    { name: "价值", defaultWeight: 5, weight: 5, active: true },
    { name: "低波", defaultWeight: 5, weight: 5, active: false },
    { name: "质量", defaultWeight: 10, weight: 10, active: true },
    { name: "成长", defaultWeight: 10, weight: 10, active: true },
  ],
};
const STOCK_POOLS = ["沪深300", "中证500", "全A", "自定义"];
const REBALANCE_PERIODS = ["周调", "双周调", "月调", "季调"];
const WEIGHTINGS = ["等权", "市值加权", "风险平价", "自定义"];
const MODEL_TIMING_SIGNAL = "模型综合分";
const STOP_LOSSES = ["5%", "8%", "10%", "15%", "关闭"];
const DRAWDOWN_CIRCUITS = ["10%", "15%", "20%", "关闭"];

const sectionTitleStyle: React.CSSProperties = { fontSize: 14, fontWeight: 600, color: "var(--pt-foreground)", margin: 0 };
const sectionSubStyle: React.CSSProperties = { fontSize: 12, color: "var(--pt-muted-foreground)", margin: "2px 0 0 0" };
const fieldLabelStyle: React.CSSProperties = { fontSize: 12, color: "var(--pt-muted-foreground)" };
const selectFullWidthStyle: React.CSSProperties = { width: "100%" };

const PortfolioStrategyRules: React.FC<PortfolioStrategyRulesProps> = ({ portfolioId, onNavigate, portfolioStatus, perm, onConfigurationChanged }) => {
  const { showToast, portfolios, portfolioId: activeId, loadPortfolios, setActiveTab } = useApp();
  const activePortfolioRef = useRef(portfolioId);
  activePortfolioRef.current = portfolioId;

  // FR-P1-8a HG1：与 PortfolioOverview 一致 —— 硬禁状态禁止从 OFF → ON
  const currentState = portfolioStatus?.current_state ?? "UNKNOWN";
  const requiresManualAck = perm?.requires_manual_ack ?? false;
  const hardBlockAutoOn = currentState === "ADMIN_PAUSED" || currentState === "RECONCILIATION_BLOCKED" ||
    currentState === "PENDING_INITIAL_REVIEW" || currentState === "MODEL_INACTIVE" || requiresManualAck;
  const softWarningAutoOn = currentState !== "READY";
  const hardBlockHint = (() => {
    switch (currentState) {
      case "ADMIN_PAUSED": return "管理员紧急刹车已触发：需管理员在治理 Tab 解除后才可开启自动交易";
      case "RECONCILIATION_BLOCKED": return "对账存在非零差异：需在治理 Tab 单人确认后才可开启自动交易";
      case "PENDING_INITIAL_REVIEW": return "新建组合尚未通过管理员合规审查：需管理员在治理 Tab 确认后才可开启自动交易";
      case "MODEL_INACTIVE": return "绑定因子模型已退役/未激活：请在策略设置中更换/激活模型，或在治理 Tab 修复";
      default:
        return requiresManualAck ? "当前组合状态需要人工确认/解除，请在治理 Tab 处理后再开启自动交易" : "";
    }
  })();

  // P0-FIX: 从当前组合派生真实 auto_trade_enabled（0=关闭，1=开启）
  const currentPortfolio = portfolios.find((p) => p.id === portfolioId) ?? portfolios.find((p) => p.id === activeId) ?? null;
  const realAutoEnabled = currentPortfolio ? Number(currentPortfolio.auto_trade_enabled) === 1 : false;

  // P1-FIX: autoEnabled 默认 null（加载中），useEffect 加载真实配置后回显
  const [autoEnabled, setAutoEnabled] = useState<boolean | null>(null);
  const [factorModel, setFactorModel] = useState(FACTOR_MODELS[0]);
  const [factorModelRuns, setFactorModelRuns] = useState<ScoringModelBrief[]>([]);
  const [factorModelRunId, setFactorModelRunId] = useState<string>("");
  // WP0-8 C-05/C-07：绑定的 FactorSet 只读映射展示 + 用于保存时双写契约
  const [boundFactorSetId, setBoundFactorSetId] = useState<string>("");
  const [currentUsage, setCurrentUsage] = useState<PortfolioFactorUsageState | null>(null);
  const [bindingError, setBindingError] = useState<string | null>(null);
  const [stockPool, setStockPool] = useState(STOCK_POOLS[1]);
  const [rebalancePeriod, setRebalancePeriod] = useState(REBALANCE_PERIODS[0]);
  const [weighting, setWeighting] = useState(WEIGHTINGS[2]);
  const [factors, setFactors] = useState<FactorItem[]>(DEFAULT_FACTORS.map((f) => ({ ...f })));
  const timingSignal = MODEL_TIMING_SIGNAL;
  const [maxSinglePosition, setMaxSinglePosition] = useState<number | null>(null);
  const [stopLoss, setStopLoss] = useState<string | null>(null);
  const [riskBudgetPct, setRiskBudgetPct] = useState(0.05);
  const [drawdownCircuit, setDrawdownCircuit] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [loading, setLoading] = useState(true);
  // 已有信号规则：保存时合并字段，避免覆盖 quality_tolerance 等既有配置
  const [existingSignalRule, setExistingSignalRule] = useState<SignalRule | null>(null);

  // P1-FIX: 加载真实规则数据回显
  useEffect(() => {
    if (!portfolioId) return;
    let cancelled = false;
    setLoading(true);
    setSaving(false);
    setBindingError(null);
    setCurrentUsage(null);
    (async () => {
      try {
        // 并行：刷新组合列表（最佳 effort）+ 读取组合详情(active_rule) + 信号规则
        // loadPortfolios 是异步 setState，不能用返回值；最新值以 detailRes.portfolio 为准（更可靠）
        const [, detailRes, signalRes, optionsRes, usageRes] = await Promise.allSettled([
          loadPortfolios().catch(() => {}),
          api.getPortfolioDetail(portfolioId),
          api.getSignalRule(portfolioId),
          api.getFactorUsageOptions(portfolioId),
          api.getCurrentFactorUsage(portfolioId),
        ]);
        if (cancelled) return;
        const configReadError = detailRes.status === "rejected" || signalRes.status === "rejected" ? "配置读取失败，请重新进入本页重试；当前不能覆盖正式策略。" : null;
        setCurrentUsage(usageRes.status === "fulfilled" ? usageRes.value : null);
        if (optionsRes.status === "fulfilled") {
          const options = optionsRes.value;
          setBindingError([...options.blocking_reasons, configReadError, usageRes.status === "rejected" ? "当前执行绑定读取失败，请重新进入本页重试。" : null].filter(Boolean).join("；") || null);
          setFactorSets(options.factor_sets.map((set) => ({ id: set.factor_set_id, label: set.name, status: set.status, member_count: set.member_count, frozen_at: set.frozen_at ?? null, created_at: null, is_active_for_model_run_ids: [], description: null })));
          setFactorModelRuns(options.factor_models.filter((model) => model.production_eligible === true).map((model) => {
            const set = options.factor_sets.find((item) => item.factor_set_id === model.factor_set_id);
            return { id: model.factor_model_run_id, name: model.model_name || model.display_name || model.factor_model_run_id, status: "validated", validation_ic: model.sample_out_rank_ic ?? null, sample_count: model.sample_count ?? Number.NaN, data_cutoff_at: model.data_cutoff_at ?? null, factorset_id: model.factor_set_id, factorset_label: set?.name ?? null, factorset_member_count: set?.member_count ?? null, created_at: model.trained_at ?? null };
          }));
        } else {
          setFactorModelRuns([]);
          setFactorSets([]);
          setBindingError("正式模型选项读取失败，请重新进入本页重试。当前配置不能应用。");
        }

        // autoEnabled：优先从详情读（真实后端值）
        let detailAuto = realAutoEnabled;
        let activeRule: any = null;
        if (detailRes.status === "fulfilled" && detailRes.value) {
          const d: any = detailRes.value;
          if (d.portfolio?.auto_trade_enabled != null) {
            detailAuto = Number(d.portfolio.auto_trade_enabled) === 1;
          }
          activeRule = d.active_rule ?? null;
        }
        setAutoEnabled(detailAuto);

        // 规则字段回显：ratio->% 转换
        if (activeRule) {
          const r: any = activeRule;
          if (r.max_single_position_pct != null) {
            setMaxSinglePosition(Number((r.max_single_position_pct * 100).toFixed(3)));
          } else {
            setMaxSinglePosition(15);
          }
          const savedRiskBudget = Number(r.max_loss_per_trade_pct);
          setRiskBudgetPct(r.max_loss_per_trade_pct != null && Number.isFinite(savedRiskBudget) ? savedRiskBudget : 0.05);
          const stageObj: any = r.stage_limits_json && typeof r.stage_limits_json === "object"
            ? r.stage_limits_json
            : null;
          if (stageObj?.price_stop_loss_pct != null) {
            const pct = Number((Number(stageObj.price_stop_loss_pct) * 100).toFixed(3));
            setStopLoss(pct > 0 ? `${pct}%` : "关闭");
          } else {
            setStopLoss(STOP_LOSSES[0]);
          }
          if (stageObj?.drawdown_circuit_pct != null) {
            const raw = Number(stageObj.drawdown_circuit_pct);
            // 兼容两种单位：|raw| <= 1 视为 ratio(0.1 = 10%)，否则视为百分比原值(10 = 10%)
            const pct = Math.abs(raw) <= 1 ? Number((raw * 100).toFixed(3)) : Number(raw.toFixed(3));
            setDrawdownCircuit(pct > 0 ? `${pct}%` : "关闭");
          } else {
            setDrawdownCircuit(DRAWDOWN_CIRCUITS[0]);
          }
          if (stageObj?.rebalance_period && REBALANCE_PERIODS.includes(stageObj.rebalance_period)) {
            setRebalancePeriod(stageObj.rebalance_period);
          }
          if (stageObj?.stock_pool && STOCK_POOLS.includes(stageObj.stock_pool)) {
            setStockPool(stageObj.stock_pool);
          }
          if (stageObj?.weighting && WEIGHTINGS.includes(stageObj.weighting)) {
            setWeighting(stageObj.weighting);
          }
          if (Array.isArray(stageObj?.factors) && stageObj.factors.length) {
            setFactors(stageObj.factors.map((f: any) => ({
              name: String(f.name ?? ""),
              defaultWeight: Number(f.defaultWeight ?? f.default_weight ?? f.weight ?? 0),
              weight: Number(f.weight ?? 0),
              active: Boolean(f.active ?? true),
            })));
          }
          // factor_model：优先读 stage_limits_json 显式字段，其次才拆 rule_name
          const fm = (stageObj?.factor_model && FACTOR_MODELS.includes(String(stageObj.factor_model)))
            ? String(stageObj.factor_model)
            : (typeof r.rule_name === "string"
              ? (() => {
                  const parts = String(r.rule_name).split("-");
                  return parts.length >= 1 && FACTOR_MODELS.includes(parts[0]) ? parts[0] : FACTOR_MODELS[0];
                })()
              : FACTOR_MODELS[0]);
          setFactorModel(fm);
          // WP0-8 C-05/C-07 回显：factor_model_run_id + factor_set_id 双溯源
          //   优先级：rule 顶层独立字段(C-07 双写) > stage_limits_json
          const savedRunId = String(r.factor_model_run_id ?? stageObj?.factor_model_run_id ?? "");
          const runId = optionsRes.status === "fulfilled" && optionsRes.value.factor_models.some((m) => m.factor_model_run_id === savedRunId && m.production_eligible === true) ? savedRunId : "";
          const fsId = optionsRes.status === "fulfilled" ? optionsRes.value.factor_models.find((m) => m.factor_model_run_id === runId)?.factor_set_id ?? "" : "";
          setFactorModelRunId(runId);
          setBoundFactorSetId(fsId);
        } else {
          setMaxSinglePosition(15);
          setStopLoss(STOP_LOSSES[0]);
          setRiskBudgetPct(0.05);
          setDrawdownCircuit(DRAWDOWN_CIRCUITS[0]);
          setFactorModel(FACTOR_MODELS[0]);
          setFactors(FACTOR_MODEL_PRESETS[FACTOR_MODELS[0]].map((f) => ({ ...f })));
          setFactorModelRunId("");
          setBoundFactorSetId("");
          setStockPool(STOCK_POOLS[1]);
          setRebalancePeriod(REBALANCE_PERIODS[0]);
          setWeighting(WEIGHTINGS[2]);
        }
        if (signalRes.status === "fulfilled" && signalRes.value) {
          setExistingSignalRule(signalRes.value);
        } else {
          setExistingSignalRule(null);
        }
      } catch (e) {
        if (cancelled) return;
        setBindingError("配置读取失败，请重新进入本页重试；当前不能覆盖正式策略。");
        setMaxSinglePosition(15);
        setStopLoss(STOP_LOSSES[0]);
        setDrawdownCircuit(DRAWDOWN_CIRCUITS[0]);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [portfolioId]);

  // WP0-8 C-05：同步加载 FactorSet，在策略规则页做「模型 → FactorSet」只读映射展示
  const [factorSets, setFactorSets] = useState<ScoringFactorSetBrief[]>([]);

  // —— 汉化字典（避免 UI 直接吐英文枚举值 / 原始字段名） ————————————————
  // 因子模型训练产出的「验证集指标」字段 → 中文标签 + 数值友好格式化
  type MetricMeta = {
    label: string;
    unit?: string;
    percent?: boolean;
    digits?: number;
    integer?: boolean;
    group?: boolean;
  };
  const METRIC_CN_LABEL: Record<string, MetricMeta> = {
    turnover_z20: { label: "20日换手率暴露", unit: "%", percent: true, digits: 0 },
    cluster_exposure: { label: "集中度控制（行业/风格）", unit: "", percent: false, digits: 4 },
    sample_count: { label: "训练样本量", unit: "条", integer: true, group: true },
    train_ic: { label: "训练集 IC", unit: "", digits: 4 },
    validation_date_count: { label: "验证交易日数", unit: "天", integer: true },
    validation_ic: { label: "验证集 IC", unit: "", digits: 4 },
    ic: { label: "IC（信息系数）", unit: "", digits: 4 },
    icir: { label: "ICIR（IC 稳定性）", unit: "", digits: 3 },
    annual_return: { label: "年化收益率", unit: "%", percent: true, digits: 2 },
    sharpe: { label: "夏普比率", unit: "", digits: 3 },
    max_drawdown: { label: "最大回撤", unit: "%", percent: true, digits: 2 },
    turnover: { label: "双边换手率", unit: "%", percent: true, digits: 1 },
    rank_ic: { label: "排序 IC", unit: "", digits: 4 },
    rank_icir: { label: "排序 ICIR", unit: "", digits: 3 },
  };
  const fmtMetric = (key: string, raw: number): { label: string; text: string } => {
    const m = METRIC_CN_LABEL[key];
    const fallbackLabel = key;
    if (!m) {
      // 未知字段：保留原名，整数千分位，小数保留4位
      const isInt = Number.isInteger(raw) && Math.abs(raw) >= 1;
      const text = isInt ? raw.toLocaleString("zh-CN") : Number(raw).toFixed(4);
      return { label: fallbackLabel, text };
    }
    const label = m.label ?? fallbackLabel;
    const v = Number(raw);
    let text: string;
    if (m.integer) {
      const iv = Math.round(v);
      text = m.group ? iv.toLocaleString("zh-CN") : String(iv);
    } else if (m.percent) {
      // percent=true 表示原值是 0~1 的小数，转成 0~100 的百分比显示
      const digits = typeof m.digits === "number" ? m.digits : 2;
      text = (v * 100).toFixed(digits);
    } else {
      const digits = typeof m.digits === "number" ? m.digits : 4;
      text = v.toFixed(digits);
    }
    return { label, text: `${text}${m.unit ?? ""}` };
  };
  // FactorSet 状态枚举 → 中文展示
  const FACTOR_SET_STATUS_CN: Record<string, string> = {
    frozen: "已冻结",
    draft: "草稿",
    deprecated: "已废弃",
    unknown: "未知",
  };
  // FactorSet.name 全问号 / 不可打印字符兜底：这种通常是数据库写入失败时的占位
  const FALLBACK_FSNAME = "（未命名因子集）";
  const safeFactorSetName = (name: string | null | undefined): string => {
    if (!name) return "";
    const s = String(name).trim();
    if (!s) return "";
    // 连续 3 个以上问号基本是编码/写入失败占位
    if (/^\?{3,}(?:\s|$)/.test(s) || /^[?\uFFFD\s]+$/.test(s)) return FALLBACK_FSNAME;
    // Strip encoding placeholders accidentally persisted before the UTF-8
    // migration, while retaining the meaningful suffix (e.g. turnover_z20).
    const cleaned = s.replace(/^[?\uFFFD\s]+/, "").trim();
    return cleaned || FALLBACK_FSNAME;
  };
  const modelDisplayName = (run: ScoringModelBrief): string => {
    const explicit = run.name?.trim();
    if (explicit && !/^[?\uFFFD\s]+$/.test(explicit)) return explicit;
    const setName = safeFactorSetName(run.factorset_label);
    const dateText = run.data_cutoff_at ? String(run.data_cutoff_at).slice(0, 10) : "未标注日期";
    return `${setName || "因子模型"} · ${dateText}`;
  };
  // 因子代码（factor_code）→ 中文因子名
  // 未识别的代码原样保留（避免破坏未知因子可识别性）
  const FACTOR_CODE_CN: Record<string, string> = {
    turnover_z20: "20日换手率",
    turnover_20: "20日换手率",
    turnover: "换手率",
    ret_20: "20日收益率",
    mom_1m: "1个月动量",
    momentum: "动量因子",
    rev_20: "20日反转",
    reversal: "反转因子",
    volatility: "波动率",
    vol_20: "20日波动率",
    volatility_20: "20日波动率",
    beta: "市场贝塔",
    size: "市值因子",
    log_mcap: "对数市值",
    mcap: "总市值",
    pb: "市净率(PB)",
    pe_ttm: "市盈率PE(TTM)",
    pe: "市盈率PE",
    roe: "净资产收益率ROE",
    roa: "资产回报率ROA",
    gross_margin: "毛利率",
    net_margin: "净利率",
    revenue_growth: "营收增速",
    profit_growth: "净利润增速",
    cluster_exposure: "行业/风格暴露",
    ic: "IC（信息系数）",
    rank_ic: "排序IC",
    icir: "ICIR",
    rank_icir: "排序ICIR",
    sharpe: "夏普",
    max_drawdown: "最大回撤",
    annual_return: "年化收益",
  };
  const cnFactorName = (code: string | null | undefined): string => {
    if (!code) return "—";
    const key = String(code).trim();
    return FACTOR_CODE_CN[key] ?? key;
  };
  // 模型类型（model_type）→ 中文
  const MODEL_TYPE_CN: Record<string, string> = {
    ridge: "岭回归",
    lasso: "Lasso",
    elastic_net: "弹性网络",
    linear: "线性回归",
    xgb: "XGBoost",
    xgboost: "XGBoost",
    lgb: "LightGBM",
    lightgbm: "LightGBM",
    catboost: "CatBoost",
    rf: "随机森林",
    random_forest: "随机森林",
    svr: "支持向量回归",
    gbdt: "GBDT",
    ensemble: "集成模型",
    rule: "规则模型",
  };
  const cnModelType = (t: string | null | undefined): string =>
    !t ? "—" : (MODEL_TYPE_CN[String(t).trim()] ?? String(t).trim());
  // 资产类型（asset_type）→ 中文
  const ASSET_TYPE_CN: Record<string, string> = {
    stock: "A股股票",
    cn_stock: "A股股票",
    "cn-stock": "A股股票",
    cn_stock_a: "A股股票",
    etf: "A股ETF",
    cn_etf: "A股ETF",
    "cn-etf": "A股ETF",
    us_stock: "美股股票",
    "us-stock": "美股股票",
    us_etf: "美股ETF",
    "us-etf": "美股ETF",
    index: "指数",
    futures: "期货",
    crypto: "数字币",
    bond: "债券",
    multi: "混合资产",
  };
  const cnAssetType = (t: string | null | undefined): string =>
    !t ? "—" : (ASSET_TYPE_CN[String(t).trim()] ?? String(t).trim());
  // 训练状态（FactorModelRun.status）→ 中文
  const RUN_STATUS_CN: Record<string, string> = {
    validated: "已验证",
    validated_approved: "已验证(已锁定)",
    training: "训练中",
    pending: "排队中",
    running: "运行中",
    failed: "训练失败",
    canceled: "已取消",
    draft: "草稿",
    deprecated: "已废弃",
  };
  const cnRunStatus = (s: string | null | undefined): string =>
    !s ? "" : (RUN_STATUS_CN[String(s).trim()] ?? String(s).trim());
  // WP0-8 C-05：切换已训练模型 → 自动只读绑定该模型对应的 FactorSet（facade 返回 factorset_id）
  //         用户无需再手动选择 FactorSet，保证溯源链闭环。
  useEffect(() => {
    if (!factorModelRunId) {
      setBoundFactorSetId("");
      return;
    }
    const selected = factorModelRuns.find((r) => r.id === factorModelRunId);
    const fs = selected?.factorset_id;
    setBoundFactorSetId(typeof fs === "string" ? fs : "");
  }, [factorModelRunId, factorModelRuns]);

  const totalWeight = factors.reduce((sum, f) => sum + f.weight, 0);

  // 显示用：null 时 fallback 默认值
  const displaySinglePosition = maxSinglePosition ?? 15;
  const displayStopLoss = stopLoss ?? STOP_LOSSES[0];
  const displayDrawdown = drawdownCircuit ?? DRAWDOWN_CIRCUITS[0];
  const displayAutoEnabled = autoEnabled ?? realAutoEnabled;

  const toggleFactor = useCallback((name: string) => {
    setFactors((prev) =>
      prev.map((f) =>
        f.name === name
          ? { ...f, active: !f.active, weight: f.active ? 0 : f.defaultWeight }
          : f,
      ),
    );
  }, []);

  // 构造 POST /portfolios/{id}/rules payload（匹配后端 PortfolioRuleUpsert schema）
  // 前端 UI 字段 → 后端字段映射：
  //   factor_model + timing_signal → rule_name（组合命名）
  //   max_single_position(%) → max_single_position_pct(0.15)  P1-FIX: /100 单位转换
  //   价格止损写入 price_stop_loss_pct；单笔风险预算保留已保存规则的值。
  //   其余风控字段使用合理默认值
  //   isApply=true 时 is_active=true（激活规则），草稿模式 is_active=false
  // WP0-8 C-07 双写契约：factor_set_id / factor_model_run_id 必须同时写入 (a) 顶层独立字段 (b) stage_limits_json，
  //   保证后端 PortfolioRuleUpsert 的 upsert 双写一致性 + 前端提交字段透明。
  const buildRulesPayload = useCallback(
    (isApply = false) => {
      // 解析止损百分比："8%" → 0.08，"关闭" → 0
      const stopLossPct = (() => {
        const n = parseFloat(displayStopLoss);
        return Number.isFinite(n) ? n / 100 : 0;
      })();
      // 解析回撤熔断百分比："10%" → 0.10（ratio），"关闭" → 0
      // P1-FIX: 统一下行 ratio 格式（与 max_single_position_pct / max_loss_per_trade_pct 一致）
      const drawdownPct = (() => {
        const n = parseFloat(displayDrawdown);
        return Number.isFinite(n) ? n / 100 : 0;
      })();
      // stage_limits_json: 显式写入 factor_model / timing_signal 等 UI 字段，避免只能从 rule_name 模糊推断
      const selectedRun = factorModelRuns.find((r) => r.id === factorModelRunId) ?? null;
      // C-05 只读派生：优先用 UI state boundFactorSetId；若未显式回显则从 selectedRun.factorset_id 推导
      const resolvedFactorSetId = boundFactorSetId
        || (typeof selectedRun?.factorset_id === "string" ? selectedRun.factorset_id : "");
      const resolvedModelRunId = factorModelRunId || "";
      const stageLimits: Record<string, unknown> = {
        price_stop_loss_pct: stopLossPct,
        drawdown_circuit_pct: drawdownPct,
        rebalance_period: rebalancePeriod,
        stock_pool: stockPool,
        weighting,
        factors,
        factor_model: factorModel,
        timing_signal: timingSignal,
        // P2 因子模型链路打通：绑定 validated FactorModelRun.id，
        // 后端回测/自动交易会优先读取 Score 里由该模型算出的 model_alpha_score / factor_scores_json
        factor_model_run_id: resolvedModelRunId || null,
        factor_model_run_name: selectedRun
          ? `${selectedRun.name || "model"} (${selectedRun.id.slice(0, 8)})`
          : null,
        // WP0-8 C-07 双写 stage_limits_json 内同步：factor_set_id（与 PortfolioRule.factor_set_id 字段一致）
        factor_set_id: resolvedFactorSetId || null,
      };
      const payload: Record<string, unknown> & { is_active: boolean } = {
        rule_name: `${factorModel}-${timingSignal}`,
        max_single_position_pct: (Number(displaySinglePosition) || 0) / 100,
        max_sector_position_pct: 40 / 100,
        max_stock_position_pct: 80 / 100,
        max_etf_position_pct: 60 / 100,
        max_loss_per_trade_pct: riskBudgetPct,
        max_open_positions: 10,
        stage_limits_json: stageLimits,
        is_active: isApply,
        // WP0-8 C-07 双写顶层独立字段（PortfolioRuleUpsert 契约：双写 factor_set_id / factor_model_run_id）
        factor_set_id: resolvedFactorSetId || null,
        factor_model_run_id: resolvedModelRunId || null,
      };
      return payload;
    },
    [
      factorModel,
      timingSignal,
      displaySinglePosition,
      displayStopLoss,
      riskBudgetPct,
      displayDrawdown,
      rebalancePeriod,
      stockPool,
      weighting,
      factors,
      factorModelRuns,
      factorModelRunId,
      boundFactorSetId,
    ],
  );

  // 构造 signal-rule payload：保留已有 SignalRule 字段，避免覆盖；无则使用默认值
  const buildSignalPayload = useCallback((): Record<string, unknown> => {
    const ruleName = timingSignal;
    const base = existingSignalRule;
    if (base) {
      return {
        rule_name: ruleName,
        mode: base.mode,
        quality_tolerance: base.quality_tolerance,
        timing_tolerance: base.timing_tolerance,
        min_sample_count: base.min_sample_count,
        max_samples: base.max_samples,
        same_region: base.same_region,
        same_asset_type: base.same_asset_type,
        same_stage: base.same_stage,
        same_action: base.same_action,
      };
    }
    return {
      rule_name: ruleName,
      mode: "general",
      quality_tolerance: 70,
      timing_tolerance: 3,
      min_sample_count: 5,
      max_samples: 20,
      same_region: false,
      same_asset_type: false,
      same_stage: false,
      same_action: false,
    };
  }, [existingSignalRule, timingSignal]);

  // FR-P1-8a HG1：保存前二次确认（当 autoEnabled=true 且 currentState 非 READY）
  const confirmAutoBeforeSave = useCallback((): boolean => {
    if (!autoEnabled || realAutoEnabled) return true;
    if (hardBlockAutoOn) {
      showToast("error", "HG1 门禁：" + (hardBlockHint || "当前组合状态禁止开启自动交易"));
      return false;
    }
    if (softWarningAutoOn) {
      return window.confirm(
        `⚠️ HG1 提示：当前组合状态为「${govStatusLabel(currentState)}」，尚未就绪。\n\n` +
        `即将保存并开启自动交易，将受到治理权限约束：${actionLabel("NEW_BUY")}=${perm?.allow_new_buys ? "允许" : "禁止"} / ${actionLabel("RISK_EXIT")}=${perm?.allow_risk_exits ? "允许" : "禁止"}。\n\n` +
        `若在运行中触发管理员紧急暂停或对账差异阻断，系统将自动强制关闭自动交易。\n\n确认仍要保存并开启？`,
      );
    }
    return true;
  }, [autoEnabled, realAutoEnabled, hardBlockAutoOn, hardBlockHint, softWarningAutoOn, currentState, perm, showToast]);

  const handleSaveDraft = useCallback(async () => {
    if (!portfolioId) return;
    if (loading || saving) return;
    setSaving(true);
    try {
      await api.savePortfolioStrategyConfiguration(portfolioId, { rule: buildRulesPayload(false), signal_rule: buildSignalPayload() });
      if (activePortfolioRef.current !== portfolioId) return;
      showToast("success", t("portfolioTrading.strategy.draftSaved"));
    } catch (error: any) {
      if (activePortfolioRef.current !== portfolioId) return;
      showToast("error", error?.message || t("portfolioTrading.strategy.saveFailed"));
    } finally {
      if (activePortfolioRef.current === portfolioId) setSaving(false);
    }
  }, [portfolioId, loading, saving, buildRulesPayload, buildSignalPayload, showToast]);

  const handleSaveApply = useCallback(async () => {
    if (!portfolioId) return;
    if (loading || saving || bindingError) return;
    const model = factorModelRuns.find((item) => item.id === factorModelRunId);
    if (!model?.factorset_id || model.factorset_id !== boundFactorSetId) {
      showToast("error", "正式应用需要选择可执行的真实训练模型。");
      return;
    }
    if (!confirmAutoBeforeSave()) return;
    setSaving(true);
    try {
      const result = await api.savePortfolioStrategyConfiguration(portfolioId, {
        rule: buildRulesPayload(true), signal_rule: buildSignalPayload(),
        binding: { factor_model_run_id: model.id, factor_set_id: model.factorset_id, run_mode: "production_sim", pit_mode: "strict_pit_safe" },
        ...(autoEnabled !== null && autoEnabled !== realAutoEnabled ? { auto_trade_enabled: autoEnabled } : {}),
      });
      if (activePortfolioRef.current !== portfolioId) return;
      if (result.binding_result) setCurrentUsage({ current: result.binding_result.factor_usage, history: [], latest_snapshot_id: result.binding_result.strategy_snapshot_id });
      setAutoEnabled(result.auto_trade_enabled);
      showToast("success", result.requires_confirmation ? "策略已应用，请在待办中由本人确认新模型绑定。" : t("portfolioTrading.strategy.saveSuccess"));
      await Promise.allSettled([loadPortfolios(), onConfigurationChanged?.(portfolioId)]);
    } catch (error: any) {
      if (activePortfolioRef.current !== portfolioId) return;
      showToast("error", error?.message || t("portfolioTrading.strategy.saveFailed"));
    } finally { if (activePortfolioRef.current === portfolioId) setSaving(false); }
  }, [portfolioId, loading, saving, bindingError, factorModelRuns, factorModelRunId, boundFactorSetId, buildRulesPayload, buildSignalPayload, showToast, autoEnabled, realAutoEnabled, loadPortfolios, confirmAutoBeforeSave, onConfigurationChanged]);

  // 切换因子模型时：自动应用预设到因子芯片权重（下方 UI 同步变化）
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => {
    const preset = FACTOR_MODEL_PRESETS[factorModel];
    if (Array.isArray(preset) && preset.length > 0) {
      setFactors(preset.map((f) => ({ ...f })));
    }
  }, [factorModel]);

  const handleResetDefault = useCallback(() => {
    setAutoEnabled(realAutoEnabled);
    const defaultModel = FACTOR_MODELS[0];
    setFactorModel(defaultModel);
    setStockPool(STOCK_POOLS[1]);
    setRebalancePeriod(REBALANCE_PERIODS[0]);
    setWeighting(WEIGHTINGS[2]);
    setFactors(FACTOR_MODEL_PRESETS[defaultModel].map((f) => ({ ...f })));
    setMaxSinglePosition(15);
    setStopLoss(STOP_LOSSES[0]);
    setDrawdownCircuit(DRAWDOWN_CIRCUITS[0]);
    // WP0-8：重置时同步清空溯源绑定
    setFactorModelRunId("");
    setBoundFactorSetId("");
    showToast("info", t("portfolioTrading.strategy.resetDone"));
  }, [showToast, realAutoEnabled]);

  // WP0-8 C-08：移除「复制/新建模型」假控件（原先只弹 toast，没有调任何 API）。
  // 真实新建/训练入口在「因子模型页（FactorModelPage）」，此处保留提示说明即可。
  // 避免给用户造成"已经在策略页创建了新模型"的误导（所见即所执行原则）。

  return (
    <section style={{ padding: 16, display: "flex", flexDirection: "column", gap: 16 }}>
      <div className="pt-card" data-testid="current-strategy-binding" style={{ padding: 16, fontSize: 12 }}>
        <strong>当前执行绑定</strong>
        {loading ? <div>读取中…</div> : <>
          <div>执行模型：{currentUsage?.current?.factor_model_run_id || "尚未应用"}</div>
          <div>因子集：{currentUsage?.current?.factor_set_id || "—"}</div>
          <div>执行快照：{currentUsage?.latest_snapshot_id || "尚未生成"}</div>
          <div style={{ marginTop: 6 }}>下方是待保存配置；正式应用真实模型后，请在待办中由本人确认新绑定。</div>
        </>}
      </div>
      {bindingError && <div role="alert" style={{ color: "var(--pt-state-warning)" }}>{bindingError}</div>}
      {/* ========== SubTask 6.1: 自动接管系统配置卡片 ========== */}
      <div className="pt-card" style={{ padding: 16 }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
            <div
              style={{
                width: 40,
                height: 40,
                borderRadius: "var(--pt-radius-md)",
                background: "var(--pt-state-info-dim)",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                flexShrink: 0,
              }}
            >
              <Bot size={20} style={{ color: "var(--pt-state-info)" }} />
            </div>
            <div>
              <h2 style={{ fontSize: 16, fontWeight: 600, color: "var(--pt-foreground)", margin: 0 }}>
                {t("portfolioTrading.strategy.autoTakeoverTitle")}
              </h2>
              <p style={{ fontSize: 12, color: "var(--pt-muted-foreground)", margin: "2px 0 0 0" }}>
                {t("portfolioTrading.strategy.autoTakeoverDesc")}
              </p>
              {/* FR-P1-8a: HG1 状态小字提示 */}
              {currentState !== "READY" && (
                <div title={govStatusCodeForTooltip(currentState) ?? undefined} style={{ marginTop: 5, fontSize: 11, color: hardBlockAutoOn ? "var(--pt-state-error, #ef4444)" : "var(--pt-state-warning, #f59e0b)" }}>
                  {hardBlockAutoOn ? <ShieldAlert size={11} style={{ display: "inline", verticalAlign: "-1px", marginRight: 4 }} /> : <AlertTriangle size={11} style={{ display: "inline", verticalAlign: "-1px", marginRight: 4 }} />}
                  HG1 状态：「{govStatusLabel(currentState)}」{hardBlockAutoOn ? "（禁止开启自动交易）" : "（保存时若开启将二次确认）"}
                </div>
              )}
            </div>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
            <span className={`pt-tag ${displayAutoEnabled ? "pt-tag-success" : "pt-tag-warning"}`}>
              {displayAutoEnabled
                ? t("portfolioTrading.strategy.statusRunning")
                : t("portfolioTrading.strategy.statusPaused")}
            </span>
            <button
              type="button"
              className={`pt-toggle${displayAutoEnabled ? " active" : ""}`}
              aria-pressed={displayAutoEnabled}
              onClick={() => {
                const prev = displayAutoEnabled;
                // 从 OFF → ON 检查 HG1（保存时会再次做一次，这里也同步拦截）
                if (!prev) {
                  if (hardBlockAutoOn) {
                    showToast("error", "HG1 门禁：" + (hardBlockHint || "当前组合状态禁止开启自动交易"));
                    return;
                  }
                }
                setAutoEnabled((v) => !(v ?? realAutoEnabled));
              }}
              title={hardBlockAutoOn && !displayAutoEnabled ? ("HG1 门禁：" + hardBlockHint) : t("portfolioTrading.strategy.autoTakeoverTitle")}
              disabled={loading || (hardBlockAutoOn && !displayAutoEnabled)}
              style={{
                opacity: (hardBlockAutoOn && !displayAutoEnabled) ? 0.45 : 1,
                cursor: (hardBlockAutoOn && !displayAutoEnabled) ? "not-allowed" : undefined,
                boxShadow: (hardBlockAutoOn && !displayAutoEnabled) ? "0 0 0 1px rgba(239,68,68,0.15)" : undefined,
              }}
            />
          </div>
        </div>
      </div>

      {/* ========== SubTask 6.2 + 6.3: 因子模型配置 + 条件模型与风控 ========== */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
        {/* ---------- 左侧：1. 因子模型配置 ---------- */}
        <div className="pt-card" style={{ overflow: "hidden" }}>
          <div style={{ padding: "12px 16px", borderBottom: "1px solid var(--pt-border)", display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12 }}>
            <div>
              <h3 style={sectionTitleStyle}>{t("portfolioTrading.strategy.factorModelTitle")}</h3>
              <p style={sectionSubStyle}>
                {t("portfolioTrading.strategy.factorModelSub")}
                <span style={{ marginLeft: 4, color: "var(--pt-muted-foreground)" }}>
                  （WP0-8 C-08：训练/新建模型的真实入口在「因子模型页」，此处只做绑定；已移除原假控件避免误导）
                </span>
              </p>
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap", justifyContent: "flex-end" }}>
              {/* 预设模板 Tag：提醒这不是训练出的真实模型 */}
              <span
                style={{
                  padding: "2px 8px",
                  borderRadius: "var(--pt-radius-full)",
                  background: "rgba(14,165,233,0.12)",
                  color: "var(--pt-state-info)",
                  fontSize: 11,
                  fontWeight: 600,
                  border: "1px solid rgba(14,165,233,0.3)",
                  whiteSpace: "nowrap",
                }}
                title="快捷预设：切换它会自动重置下面因子芯片的权重（规模/动量/价值等），属于「手动配权重」路径。"
              >
                快捷预设
              </span>
              <Select
                style={{ width: 160 }}
                value={factorModel}
                onChange={(v) => setFactorModel(v as string)}
                aria-label={t("portfolioTrading.strategy.modelLabel")}
                title="预设名称：切换后自动重置下方「激活因子」芯片为该预设的默认权重（纯UI快捷模板，不做训练溯源）。
4 个内置模板：
· 多因子增强模型（默认）— 动量30 + 价值25 + 低波20 + 质量15 + 成长10 + 规模10，均衡混合
· 价值成长平衡 — 价值45 + 成长25，双主线稳健
· 低波质量       — 质量40 + 低波35，防御低回撤
· 动量轮动       — 动量50 + 规模20，进攻追涨弹性高"
                options={FACTOR_MODELS.map((m) => ({ value: m, label: `${m}（预设模板）` }))}
              />
              <span
                title="快捷预设用于编辑草稿和研究参数。正式执行需选择真实训练模型、应用执行快照，再由本人确认绑定；当前生效模型以页面顶部的执行绑定为准。"
                style={{ cursor: "help", color: "var(--pt-muted-foreground)" }}
              >
                <Info size={14} />
              </span>
              <Button
                size="small"
                icon={<Plus size={14} />}
                onClick={() => {
                  // C-08：引导去真实的训练入口（设置 / 因子模型 Tab：冻结因子集 → 跑训练）
                  showToast(
                    "info",
                    "请选择冻结集合并运行真实训练流水线；训练完成且生产激活后，回到本页应用并确认绑定。",
                  );
                  openSettingsSection("factor-model");
                  setActiveTab("settings");
                }}
                title="去设置中的真实训练流水线"
                aria-label={t("portfolioTrading.strategy.newModel")}
                style={{ whiteSpace: "nowrap" }}
              >
                训练新模型
              </Button>
            </div>
          </div>
          <div style={{ padding: 16, display: "flex", flexDirection: "column", gap: 16 }}>
            {/* —— 两条使用路径说明：消除「快捷预设」vs「训练产出绑定」的混淆 —————————— */}
            <div
              style={{
                fontSize: 12,
                padding: "10px 12px",
                borderRadius: 6,
                background: "linear-gradient(90deg, rgba(16,185,129,0.08), rgba(59,130,246,0.08))",
                border: "1px solid rgba(16,185,129,0.25)",
                lineHeight: 1.85,
                color: "var(--pt-foreground)",
              }}
            >
              <div style={{ fontWeight: 600, marginBottom: 4, color: "var(--pt-primary)" }}>
                草稿参数与正式执行模型
              </div>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
                <div
                  style={{
                    padding: "8px 10px",
                    borderRadius: 6,
                    background: "rgba(14,165,233,0.06)",
                    border: "1px dashed rgba(14,165,233,0.35)",
                  }}
                >
                  <div style={{ fontWeight: 600, color: "var(--pt-state-info)", marginBottom: 2 }}>
                    手工参数：草稿与研究配置
                  </div>
                  <div style={{ color: "var(--pt-muted-foreground)", marginBottom: 4 }}>
                    未选择正式模型时，仅可保存草稿。
                  </div>
                  <div style={{ color: "var(--pt-foreground)" }}>
                    ① 顶部下拉切换<strong>快捷预设模板</strong> → 自动重置下方「激活因子」芯片权重<br/>
                    ② 手动微调各因子的开关和权重 → 保存<br/>
                    ③ 草稿不会改变当前执行绑定与自动接管。
                  </div>
                </div>
                <div
                  style={{
                    padding: "8px 10px",
                    borderRadius: 6,
                    background: "rgba(16,185,129,0.06)",
                    border: "1px dashed rgba(16,185,129,0.35)",
                  }}
                >
                  <div style={{ fontWeight: 600, color: "var(--pt-state-success)", marginBottom: 2 }}>
                    正式执行：真实训练模型
                  </div>
                  <div style={{ color: "var(--pt-muted-foreground)", marginBottom: 4 }}>
                    下方仅显示通过正式运行资格检查的当前模型。
                  </div>
                  <div style={{ color: "var(--pt-foreground)" }}>
                    ① 点击右上角<strong>「训练新模型」</strong> → 去「设置/因子模型」冻结因子集+训练<br/>
                    ② 训练完成并生产激活后，选择模型并保存应用<br/>
                    ③ 在待办中由本人确认新绑定，再按执行快照运行。
                  </div>
                </div>
              </div>
            </div>
            {/* 已验证因子模型（训练产出的实际权重）绑定 —— 选此模型后，回测/实盘会优先复用该模型算出来的 Score.model_alpha_score */}
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              <label style={fieldLabelStyle}>
                {t("portfolioTrading.strategy.validatedFactorModelLabel") || "已验证因子模型（训练产出）"}
                <span style={{ marginLeft: 6, color: "var(--pt-muted-foreground)" }}>
                  正式应用需选择真实训练模型；应用后由本人确认绑定。
                </span>
              </label>
              <Select
                style={selectFullWidthStyle}
                value={factorModelRunId || undefined}
                placeholder="— 未选择模型，仅可保存草稿 —"
                allowClear
                onChange={(v) => setFactorModelRunId((v as string) ?? "")}
                aria-label={t("portfolioTrading.strategy.validatedFactorModelLabel") || "Validated Factor Model Run"}
                options={factorModelRuns.map((run) => {
                  const memberCount = Number(run.factorset_member_count || 0);
                  const summary = memberCount > 0 ? `${memberCount} 因子` : "";
                  const statusCN = cnRunStatus(run.status);
                  const statusBadge = run.status === "validated" || !statusCN ? "" : `[${statusCN}]`;
                  const label = modelDisplayName(run);
                  return {
                    value: run.id,
                    label: `${label}${summary ? ` · ${summary}` : ""} ${statusBadge}`.trim(),
                  };
                })}
              />
              {factorModelRunId && factorModelRuns.find((r) => r.id === factorModelRunId) ? (
                <div
                  style={{
                    fontSize: 12,
                    color: "var(--pt-muted-foreground)",
                    background: "var(--pt-muted)",
                    padding: "6px 10px",
                    borderRadius: 6,
                    lineHeight: 1.6,
                  }}
                >
                  {(() => {
                    const run = factorModelRuns.find((r) => r.id === factorModelRunId)!;
                    const icVal = run.validation_ic;
                    const icText =
                      typeof icVal === "number" && Number.isFinite(icVal)
                        ? Number(icVal).toFixed(4)
                        : "—";
                    const sampleText = Number.isFinite(Number(run.sample_count))
                      ? Number(run.sample_count).toLocaleString()
                      : "—";
                    const cutoffText = run.data_cutoff_at
                      ? String(run.data_cutoff_at).replace("T", " ").slice(0, 16)
                      : "—";
                    const factorSetText = safeFactorSetName(run.factorset_label) || run.factorset_id || "—";
                    const memberCount = Number(run.factorset_member_count || 0);
                    return (
                      <>
                        <div>
                          <strong style={{ color: "var(--pt-foreground)" }}>模型摘要：</strong>
                          {`样本=${sampleText}，数据截止=${cutoffText}，验证IC=${icText}`}
                        </div>
                        <div style={{ marginTop: 4 }}>
                          <strong style={{ color: "var(--pt-foreground)" }}>绑定因子集：</strong>
                          {`${factorSetText}${memberCount > 0 ? `（${memberCount} 成员）` : ""}`}
                        </div>
                      </>
                    );
                  })()}
                </div>
              ) : null}
              {/* WP0-8 C-05：只读因子映射 —— 展示当前绑定 FactorSet id + 状态（frozen/draft/deprecated）+ 成员数 */}
              {boundFactorSetId ? (
                <div
                  style={{
                    fontSize: 12,
                    color: "var(--pt-muted-foreground)",
                    background: "rgba(14, 165, 233, 0.06)",
                    border: "1px dashed rgba(14, 165, 233, 0.35)",
                    padding: "8px 10px",
                    borderRadius: 6,
                    lineHeight: 1.7,
                  }}
                >
                  {(() => {
                    const fs = factorSets.find((s) => s.id === boundFactorSetId);
                    const status = fs?.status ?? "unknown";
                    const statusCN = FACTOR_SET_STATUS_CN[status] ?? status;
                    const statusColor =
                      status === "frozen" ? "var(--pt-state-info)" :
                      status === "deprecated" ? "var(--pt-state-error)" :
                      status === "draft" ? "var(--pt-state-warning)" :
                      "var(--pt-muted-foreground)";
                    const safeName = safeFactorSetName(fs?.label);
                    const frozenAt = fs?.frozen_at;
                    const frozenAtText = frozenAt
                      ? String(frozenAt).replace("T", " ").slice(0, 16)
                      : "";
                    return (
                      <>
                        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                          <Snowflake size={12} style={{ color: "var(--pt-state-info)" }} />
                          <strong style={{ color: "var(--pt-foreground)" }}>溯源链只读映射（C-05）：</strong>
                          <span
                            title="为满足权重契约要求，每一次因子模型训练都会绑定到一个已冻结的因子集；此后若因子集变动或被解锁，训练结果将被视为不可复现"
                            style={{ cursor: "help" }}
                          >
                            当前模型训练 → 已绑定因子集
                          </span>
                        </div>
                        <div style={{ marginTop: 4, lineHeight: 2 }}>
                          <span>因子集 ID：</span>
                          <code style={{ fontFamily: "var(--pt-font-mono)", fontSize: 11 }}>{boundFactorSetId}</code>
                          {safeName ? (
                            <span style={{ marginLeft: 8, opacity: 0.95 }}>
                              · {safeName}
                            </span>
                          ) : null}
                          <span
                            style={{
                              marginLeft: 10,
                              padding: "2px 8px",
                              borderRadius: 999,
                              background: `${statusColor}1A`,
                              color: statusColor,
                              fontSize: 11,
                              fontWeight: 600,
                            }}
                          >
                            状态：{statusCN}
                          </span>
                          {fs?.member_count != null ? (
                            <span style={{ marginLeft: 10 }}>含 {fs.member_count} 个因子</span>
                          ) : null}
                          {frozenAtText ? (
                            <span style={{ marginLeft: 10 }}>冻结时间：{frozenAtText}</span>
                          ) : null}
                        </div>
                        {status !== "frozen" ? (
                          <div style={{ marginTop: 4, color: "var(--pt-state-warning)" }}>
                            ⚠️ 当前绑定的因子集不是「已冻结」状态，无法保证训练权重可复现的 C-06 契约；请在「因子模型页」先冻结因子集再训练模型。
                          </div>
                        ) : null}
                      </>
                    );
                  })()}
                </div>
              ) : factorModelRunId ? (
                <div
                  style={{
                    fontSize: 12,
                    padding: "6px 10px",
                    borderRadius: 6,
                    background: "color-mix(in srgb, var(--pt-state-warning) 12%, transparent)",
                    color: "var(--pt-state-warning)",
                  }}
                >
                  ⚠️ 已选训练模型但无法读到 FactorSet（可能是离线最小模式下的最小模型，或 hyperparameters.factor_set_id 缺省）。请检查训练接口是否已写此字段。
                </div>
              ) : null}
            </div>
            {/* 选股池 */}
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              <label style={fieldLabelStyle}>{t("portfolioTrading.strategy.stockPool")}</label>
              <Select
                style={selectFullWidthStyle}
                value={stockPool}
                onChange={(v) => setStockPool(v as string)}
                options={STOCK_POOLS.map((s) => ({ value: s, label: s }))}
              />
            </div>
            {/* 调仓周期 */}
 <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              <label style={fieldLabelStyle}>{t("portfolioTrading.strategy.rebalancePeriod")}</label>
              <Select
                style={selectFullWidthStyle}
                value={rebalancePeriod}
                onChange={(v) => setRebalancePeriod(v as string)}
                options={REBALANCE_PERIODS.map((r) => ({ value: r, label: r }))}
              />
            </div>
            {/* 权重分配 */}
 <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              <label style={fieldLabelStyle}>{t("portfolioTrading.strategy.weighting")}</label>
              <Select
                style={selectFullWidthStyle}
                value={weighting}
                onChange={(v) => setWeighting(v as string)}
                options={WEIGHTINGS.map((w) => ({ value: w, label: w }))}
              />
            </div>
            {/* 激活因子 chips */}
            <div
              style={{
                display: "flex",
                flexDirection: "column",
                gap: 12,
                paddingTop: 8,
                marginTop: 8,
                borderTop: "1px solid var(--pt-border)",
              }}
            >
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                <label style={fieldLabelStyle}>{t("portfolioTrading.strategy.activeFactors")}</label>
                <span className="pt-mono" style={{ fontSize: 12, color: "var(--pt-primary)" }}>
                  {t("portfolioTrading.strategy.totalWeight")} {totalWeight}%
                </span>
              </div>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
                {factors.map((f) => (
                  <button
                    key={f.name}
                    type="button"
                    className="pt-factor-chip"
                    style={{
                      fontFamily: "var(--pt-font-sans)",
                      ...(f.active
                        ? {
                            borderColor: "var(--pt-primary)",
                            color: "var(--pt-primary)",
                            background: "rgba(6, 182, 212, 0.08)",
                          }
                        : undefined),
                    }}
                    onClick={() => toggleFactor(f.name)}
                  >
                    {f.name}
                    <span style={{ fontFamily: "var(--pt-font-mono)", fontWeight: 500 }}>{f.weight}%</span>
                  </button>
                ))}
              </div>
              <div className="pt-progress">
                <div className="pt-progress-fill" style={{ width: `${Math.min(100, totalWeight)}%` }} />
              </div>
            </div>
          </div>
        </div>

        {/* ---------- 右侧：2. 条件模型与风控 ---------- */}
        <div className="pt-card" style={{ overflow: "hidden" }}>
          {/* VIZ-0929-16：这些控件在加载期是 disabled 的，但原生 disabled 元素不触发 hover，
              Tooltip 挂了也看不见，所以用一行就地说明交代"为什么现在改不了"。 */}
          {loading && (
            <div style={{ padding: "8px 16px", fontSize: 12, color: "var(--pt-muted-foreground, #6b7280)" }}>
              {t("portfolioTrading.strategy.fieldsLoadingHint")}
            </div>
          )}
          <div style={{ padding: "12px 16px", borderBottom: "1px solid var(--pt-border)" }}>
            <h3 style={sectionTitleStyle}>{t("portfolioTrading.strategy.conditionTitle")}</h3>
            <p style={sectionSubStyle}>{t("portfolioTrading.strategy.conditionSub")}</p>
          </div>
          <div style={{ padding: 16, display: "flex", flexDirection: "column", gap: 16 }}>
            {/* 当前仅模型综合分驱动买入，择时指标尚未接入执行。 */}
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              <label style={fieldLabelStyle}>买入依据</label>
              <Input
                aria-label="买入依据"
                style={selectFullWidthStyle}
                value={timingSignal}
                readOnly
              />
              <span style={fieldLabelStyle}>模型综合评分达到策略阈值时买入；MACD等择时指标暂不参与。</span>
            </div>
            {/* 单股持仓上限 */}
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              <label style={fieldLabelStyle}>{t("portfolioTrading.strategy.maxSinglePosition")}</label>
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <InputNumber
                  style={{ flex: 1 }}
                  value={displaySinglePosition}
                  min={0}
                  max={100}
                  addonAfter="%"
                  onChange={(v) => setMaxSinglePosition(Number(v ?? 0))}
                  disabled={loading}
                />
              </div>
            </div>
            {/* 个股止损 */}
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              <label style={fieldLabelStyle}>持仓价格止损</label>
              <Select
                aria-label="持仓价格止损"
                style={selectFullWidthStyle}
                value={displayStopLoss}
                onChange={(v) => setStopLoss(v as string)}
                disabled={loading}
                options={STOP_LOSSES.map((s) => ({ value: s, label: s }))}
              />
              <span style={fieldLabelStyle}>价格相对持仓平均成本下跌达到设定比例时退出；单笔风险预算保持原规则值。</span>
            </div>
            {/* 组合回撤熔断 */}
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              <label style={fieldLabelStyle}>组合回撤暂停新买</label>
              <Select
                aria-label="组合回撤暂停新买"
                style={selectFullWidthStyle}
                value={displayDrawdown}
                onChange={(v) => setDrawdownCircuit(v as string)}
                disabled={loading}
                options={DRAWDOWN_CIRCUITS.map((d) => ({ value: d, label: d }))}
              />
              <span style={fieldLabelStyle}>回撤达到设定比例时暂停新买，保留卖出和风险退出。</span>
            </div>
            {/* 警告 banner */}
            <div
              style={{
                padding: 12,
                borderRadius: "var(--pt-radius-md)",
                background: "var(--pt-state-warning-dim)",
                display: "flex",
                alignItems: "center",
                gap: 8,
                marginTop: 8,
              }}
            >
              <AlertTriangle size={16} style={{ color: "var(--pt-state-warning)", flexShrink: 0 }} />
              <span style={{ fontSize: 12, color: "var(--pt-foreground)" }}>
                {t("portfolioTrading.strategy.warningText")}
              </span>
            </div>
          </div>
        </div>
      </div>

      {/* ========== SubTask 6.4: 底部保存操作 ========== */}
      <div
        className="pt-card"
        style={{ padding: 16, display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12 }}
      >
        <span
          style={{
            fontSize: 12,
            color: "var(--pt-muted-foreground)",
            display: "inline-flex",
            alignItems: "center",
            gap: 4,
          }}
        >
          <Info size={14} style={{ flexShrink: 0 }} />
          保存草稿仅暂存配置；正式应用生成执行快照，随后需本人确认新绑定。
        </span>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <Button
            icon={<RotateCcw size={14} />}
            onClick={handleResetDefault}
            disabled={saving}
          >
            {t("portfolioTrading.strategy.resetDefault")}
          </Button>
          <Button
            icon={<FileText size={14} />}
            onClick={handleSaveDraft}
            disabled={saving || loading}
          >
            {t("portfolioTrading.strategy.saveDraft")}
          </Button>
          <Button
            type="primary"
            icon={<Save size={14} />}
            onClick={handleSaveApply}
            loading={saving}
            disabled={loading || Boolean(bindingError) || !factorModelRunId || !boundFactorSetId}
          >
            {t("portfolioTrading.strategy.saveApply")}
          </Button>
        </div>
      </div>
    </section>
  );
};

export default PortfolioStrategyRules;
