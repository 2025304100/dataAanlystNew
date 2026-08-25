import React, { useCallback, useEffect, useState } from "react";
import { Bot, Plus, AlertTriangle, Save, RotateCcw, FileText, Info, ShieldAlert, Snowflake } from "lucide-react";
import { api } from "../../api/client";
import type { FactorModelRun, FactorSet } from "../../api/client";
import { t } from "../../i18n";
import { useApp } from "../../context/AppContext";
import type { PortfolioStatePermissions, PortfolioStatusResponse, SignalRule } from "../../types";

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
const TIMING_SIGNALS = ["MA趋势", "MACD", "RSI", "布林带", "无"];
const STOP_LOSSES = ["5%", "8%", "10%", "15%", "关闭"];
const DRAWDOWN_CIRCUITS = ["10%", "15%", "20%", "关闭"];

const sectionTitleStyle: React.CSSProperties = { fontSize: 14, fontWeight: 600, color: "var(--pt-foreground)", margin: 0 };
const sectionSubStyle: React.CSSProperties = { fontSize: 12, color: "var(--pt-muted-foreground)", margin: "2px 0 0 0" };
const fieldLabelStyle: React.CSSProperties = { fontSize: 12, color: "var(--pt-muted-foreground)" };
const selectFullWidthStyle: React.CSSProperties = { width: "100%" };

const PortfolioStrategyRules: React.FC<PortfolioStrategyRulesProps> = ({ portfolioId, onNavigate, portfolioStatus, perm }) => {
  const { showToast, portfolios, portfolioId: activeId, loadPortfolios } = useApp();

  // FR-P1-8a HG1：与 PortfolioOverview 一致 —— 硬禁状态禁止从 OFF → ON
  const currentState = portfolioStatus?.current_state ?? "UNKNOWN";
  const requiresManualAck = perm?.requires_manual_ack ?? false;
  const hardBlockAutoOn = currentState === "ADMIN_PAUSED" || currentState === "RECONCILIATION_BLOCKED" ||
    currentState === "PENDING_INITIAL_REVIEW" || currentState === "MODEL_INACTIVE" || requiresManualAck;
  const softWarningAutoOn = currentState !== "READY";
  const hardBlockHint = (() => {
    switch (currentState) {
      case "ADMIN_PAUSED": return "管理员紧急刹车已触发（ADMIN_PAUSED）：需管理员在治理 Tab 解除后才可开启自动交易";
      case "RECONCILIATION_BLOCKED": return "对账存在非零差异（RECONCILIATION_BLOCKED）：需在治理 Tab 单人确认后才可开启自动交易";
      case "PENDING_INITIAL_REVIEW": return "新建组合尚未通过管理员合规审查（PENDING_INITIAL_REVIEW）：需管理员在治理 Tab 确认后才可开启自动交易";
      case "MODEL_INACTIVE": return "绑定因子模型已退役/未激活（MODEL_INACTIVE）：请在策略设置中更换/激活模型，或在治理 Tab 修复";
      default:
        return requiresManualAck ? "当前组合状态需要人工确认/解除（requires_manual_ack=true），请在治理 Tab 处理后再开启自动交易" : "";
    }
  })();

  // P0-FIX: 从当前组合派生真实 auto_trade_enabled（0=关闭，1=开启）
  const currentPortfolio = portfolios.find((p) => p.id === portfolioId) ?? portfolios.find((p) => p.id === activeId) ?? null;
  const realAutoEnabled = currentPortfolio ? Number(currentPortfolio.auto_trade_enabled) === 1 : false;

  // P1-FIX: autoEnabled 默认 null（加载中），useEffect 加载真实配置后回显
  const [autoEnabled, setAutoEnabled] = useState<boolean | null>(null);
  const [factorModel, setFactorModel] = useState(FACTOR_MODELS[0]);
  const [factorModelRuns, setFactorModelRuns] = useState<FactorModelRun[]>([]);
  const [factorModelRunId, setFactorModelRunId] = useState<string>("");
  // WP0-8 C-05/C-07：绑定的 FactorSet 只读映射展示 + 用于保存时双写契约
  const [boundFactorSetId, setBoundFactorSetId] = useState<string>("");
  const [stockPool, setStockPool] = useState(STOCK_POOLS[1]);
  const [rebalancePeriod, setRebalancePeriod] = useState(REBALANCE_PERIODS[0]);
  const [weighting, setWeighting] = useState(WEIGHTINGS[2]);
  const [factors, setFactors] = useState<FactorItem[]>(DEFAULT_FACTORS.map((f) => ({ ...f })));
  const [timingSignal, setTimingSignal] = useState(TIMING_SIGNALS[1]);
  const [maxSinglePosition, setMaxSinglePosition] = useState<number | null>(null);
  const [stopLoss, setStopLoss] = useState<string | null>(null);
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
    (async () => {
      try {
        // 并行：刷新组合列表（最佳 effort）+ 读取组合详情(active_rule) + 信号规则
        // loadPortfolios 是异步 setState，不能用返回值；最新值以 detailRes.portfolio 为准（更可靠）
        const [, detailRes, signalRes] = await Promise.allSettled([
          loadPortfolios().catch(() => {}),
          api.getPortfolioDetail(portfolioId),
          api.getSignalRule(portfolioId),
        ]);
        if (cancelled) return;

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
          if (r.max_loss_per_trade_pct != null) {
            const pct = Number((r.max_loss_per_trade_pct * 100).toFixed(3));
            setStopLoss(pct > 0 ? `${pct}%` : "关闭");
          } else {
            setStopLoss(STOP_LOSSES[1]);
          }
          const stageObj: any = r.stage_limits_json && typeof r.stage_limits_json === "object"
            ? r.stage_limits_json
            : null;
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
          const runId = String(
            r.factor_model_run_id ?? stageObj?.factor_model_run_id ?? "",
          );
          const fsId = String(
            r.factor_set_id ?? stageObj?.factor_set_id ?? "",
          );
          setFactorModelRunId(runId);
          setBoundFactorSetId(fsId);
          // timing_signal：stageObj → rule_name split → 默认
          const ts = (stageObj?.timing_signal && TIMING_SIGNALS.includes(String(stageObj.timing_signal)))
            ? String(stageObj.timing_signal)
            : (typeof r.rule_name === "string"
              ? (() => {
                  const parts = String(r.rule_name).split("-");
                  return parts.length >= 2 && TIMING_SIGNALS.includes(parts[1]) ? parts[1] : TIMING_SIGNALS[1];
                })()
              : TIMING_SIGNALS[1]);
          setTimingSignal(ts);
        } else {
          setMaxSinglePosition(15);
          setStopLoss(STOP_LOSSES[1]);
          setDrawdownCircuit(DRAWDOWN_CIRCUITS[0]);
          setFactorModel(FACTOR_MODELS[0]);
          setTimingSignal(TIMING_SIGNALS[1]);
          setFactors(FACTOR_MODEL_PRESETS[FACTOR_MODELS[0]].map((f) => ({ ...f })));
          setFactorModelRunId("");
          setBoundFactorSetId("");
        }
        if (signalRes.status === "fulfilled" && signalRes.value) {
          setExistingSignalRule(signalRes.value);
        } else {
          setExistingSignalRule(null);
        }
      } catch (e) {
        setMaxSinglePosition(15);
        setStopLoss(STOP_LOSSES[1]);
        setDrawdownCircuit(DRAWDOWN_CIRCUITS[0]);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [portfolioId]);

  // P2 因子模型打通：加载已验证（validated）的 FactorModelRun，供用户绑定策略规则
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const list = await api.getFactorModels("validated", 50);
        if (!cancelled) setFactorModelRuns(list?.items ?? []);
      } catch (_e) {
        // best-effort，不阻塞策略规则页
      }
    })();
    return () => { cancelled = true; };
  }, [portfolioId]);

  // WP0-8 C-05：同步加载 FactorSet，在策略规则页做「FactorModelRun → FactorSet」只读映射展示
  const [factorSets, setFactorSets] = useState<FactorSet[]>([]);
  useEffect(() => {
    let cancelled = false;
    api
      .listFactorSets(undefined, 100)
      .then((list) => { if (!cancelled) setFactorSets(Array.isArray(list) ? list : []); })
      .catch(() => { /* best-effort */ });
    return () => { cancelled = true; };
  }, [portfolioId]);

  // WP0-8 C-05：切换已训练模型 → 自动只读绑定该模型对应的 FactorSet（hyperparameters.factor_set_id）
  //         用户无需再手动选择 FactorSet，保证溯源链闭环。
  useEffect(() => {
    if (!factorModelRunId) {
      setBoundFactorSetId((v) => v); // 不主动清空（保留回显值）
      return;
    }
    const selected = factorModelRuns.find((r) => r.id === factorModelRunId);
    const fs = (selected?.hyperparameters as { factor_set_id?: unknown } | undefined)?.factor_set_id;
    if (typeof fs === "string" && fs) {
      setBoundFactorSetId(fs);
    }
  }, [factorModelRunId, factorModelRuns]);

  // 加载已有信号规则（best-effort，失败不阻塞 UI）—— 上面已并行加载，保留作兜底
  useEffect(() => {
    if (!portfolioId) return;
    let cancelled = false;
    api
      .getSignalRule(portfolioId)
      .then((rule) => {
        if (!cancelled) setExistingSignalRule((prev) => prev ?? rule);
      })
      .catch(() => {
        // ignore
      });
    return () => {
      cancelled = true;
    };
  }, [portfolioId]);

  const totalWeight = factors.reduce((sum, f) => sum + f.weight, 0);

  // 显示用：null 时 fallback 默认值
  const displaySinglePosition = maxSinglePosition ?? 15;
  const displayStopLoss = stopLoss ?? STOP_LOSSES[1];
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
  //   stop_loss "8%" → max_loss_per_trade_pct(0.08)  P1-FIX: /100 单位转换
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
      // C-05 只读派生：优先用 UI state boundFactorSetId；若未显式回显则从 selectedRun.hyperparameters 推导
      const resolvedFactorSetId = boundFactorSetId
        || (typeof (selectedRun?.hyperparameters as { factor_set_id?: unknown } | undefined)?.factor_set_id === "string"
          ? String((selectedRun!.hyperparameters as { factor_set_id: string }).factor_set_id)
          : "");
      const resolvedModelRunId = factorModelRunId || "";
      const stageLimits: Record<string, unknown> = {
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
          ? `${selectedRun.model_type}/${selectedRun.asset_type} (${selectedRun.id.slice(0, 8)})`
          : null,
        // WP0-8 C-07 双写 stage_limits_json 内同步：factor_set_id（与 PortfolioRule.factor_set_id 字段一致）
        factor_set_id: resolvedFactorSetId || null,
      };
      const payload: Record<string, unknown> = {
        rule_name: `${factorModel}-${timingSignal}`,
        max_single_position_pct: (Number(displaySinglePosition) || 0) / 100,
        max_sector_position_pct: 40 / 100,
        max_stock_position_pct: 80 / 100,
        max_etf_position_pct: 60 / 100,
        max_loss_per_trade_pct: stopLossPct,
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
    const willEnableAuto = autoEnabled !== null ? autoEnabled : realAutoEnabled;
    if (!willEnableAuto) return true; // 保存为关闭则不用确认
    if (hardBlockAutoOn) {
      showToast("error", "HG1 门禁：" + (hardBlockHint || "当前组合状态禁止开启自动交易"));
      return false;
    }
    if (softWarningAutoOn) {
      return window.confirm(
        `⚠️ HG1 提示：当前组合状态为「${currentState}」，并非生产就绪态（READY）。\n\n` +
        `即将保存并开启自动交易，将受到治理权限约束：NEW_BUY=${perm?.allow_new_buys ? "允许" : "禁止"} / RISK_EXIT=${perm?.allow_risk_exits ? "允许" : "禁止"}。\n\n` +
        `若在运行中触发 ADMIN_PAUSED / RECONCILIATION_BLOCKED，系统将自动强制关闭自动交易。\n\n确认仍要保存并开启？`,
      );
    }
    return true;
  }, [autoEnabled, realAutoEnabled, hardBlockAutoOn, hardBlockHint, softWarningAutoOn, currentState, perm, showToast]);

  const handleSaveDraft = useCallback(async () => {
    if (!portfolioId) return;
    if (!confirmAutoBeforeSave()) return;
    setSaving(true);
    try {
      await api.upsertPortfolioRule(portfolioId, buildRulesPayload(false));
      // P1-FIX: 同步持久化自动接管开关
      if (autoEnabled !== null && autoEnabled !== realAutoEnabled) {
        await api.updatePortfolio(portfolioId, { auto_trade_enabled: autoEnabled ? 1 : 0 });
        await loadPortfolios();
      }
      showToast("success", t("portfolioTrading.strategy.draftSaved"));
    } catch (error: any) {
      showToast("error", error?.message || t("portfolioTrading.strategy.saveFailed"));
    } finally {
      setSaving(false);
    }
  }, [portfolioId, buildRulesPayload, showToast, autoEnabled, realAutoEnabled, loadPortfolios, confirmAutoBeforeSave]);

  const handleSaveApply = useCallback(async () => {
    if (!portfolioId) return;
    if (!confirmAutoBeforeSave()) return;
    setSaving(true);
    // P1-FIX: 同步持久化自动接管开关
    const saveAutoPromise = (autoEnabled !== null && autoEnabled !== realAutoEnabled)
      ? api.updatePortfolio(portfolioId, { auto_trade_enabled: autoEnabled ? 1 : 0 }).then(async (r) => { await loadPortfolios(); return r; })
      : Promise.resolve(null);
    // rules 与 signal-rule 并行保存，各自容错：单一失败不阻塞另一接口
    const [rulesRes, signalRes] = await Promise.allSettled([
      Promise.all([api.upsertPortfolioRule(portfolioId, buildRulesPayload(true)), saveAutoPromise]),
      api.saveSignalRule(portfolioId, buildSignalPayload()),
    ]);
    setSaving(false);
    if (rulesRes.status === "fulfilled") {
      showToast("success", t("portfolioTrading.strategy.saveSuccess"));
    } else {
      const reason = (rulesRes as PromiseRejectedResult).reason;
      showToast("error", reason?.message || t("portfolioTrading.strategy.saveFailed"));
    }
    if (signalRes.status === "rejected") {
      console.warn("signal-rule save failed", (signalRes as PromiseRejectedResult).reason);
    }
  }, [portfolioId, buildRulesPayload, buildSignalPayload, showToast, autoEnabled, realAutoEnabled, loadPortfolios, confirmAutoBeforeSave]);

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
    setTimingSignal(TIMING_SIGNALS[1]);
    setMaxSinglePosition(15);
    setStopLoss(STOP_LOSSES[1]);
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
                <div style={{ marginTop: 5, fontSize: 11, color: hardBlockAutoOn ? "var(--pt-state-error, #ef4444)" : "var(--pt-state-warning, #f59e0b)" }}>
                  {hardBlockAutoOn ? <ShieldAlert size={11} style={{ display: "inline", verticalAlign: "-1px", marginRight: 4 }} /> : <AlertTriangle size={11} style={{ display: "inline", verticalAlign: "-1px", marginRight: 4 }} />}
                  HG1 状态：「{currentState}」{hardBlockAutoOn ? "（禁止开启自动交易）" : "（保存时若开启将二次确认）"}
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
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <select
                className="pt-select"
                style={{ width: 160 }}
                value={factorModel}
                onChange={(e) => setFactorModel(e.target.value)}
                aria-label={t("portfolioTrading.strategy.modelLabel")}
                title="预设名称：仅作为下方因子芯片权重的快捷模板，不作为真实模型溯源依据"
              >
                {FACTOR_MODELS.map((m) => (
                  <option key={m} value={m}>{m}（预设模板）</option>
                ))}
              </select>
              <button
                type="button"
                className="pt-btn pt-btn-outline pt-btn-sm"
                onClick={() => {
                  // C-08：规划中指引，而非可交互假控件
                  showToast(
                    "info",
                    "请在「设置 / 因子模型」Tab 完成 FactorSet 冻结 → 训练模型（离线最小/仓库）后，在此处下拉选择已验证模型绑定。",
                  );
                  // 指引：通知外层 shell 切换到 settings/factor-model（若 onNavigate 提供则跳转）
                  onNavigate?.("factor-model");
                }}
                title="去因子模型页创建并训练真实模型（规划中：onNavigate 打通后将自动跳转）"
                aria-label={t("portfolioTrading.strategy.newModel")}
              >
                <Plus size={14} />
              </button>
            </div>
          </div>
          <div style={{ padding: 16, display: "flex", flexDirection: "column", gap: 16 }}>
            {/* 已验证因子模型（训练产出的实际权重）绑定 —— 选此模型后，回测/实盘会优先复用该模型算出来的 Score.model_alpha_score */}
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              <label style={fieldLabelStyle}>
                {t("portfolioTrading.strategy.validatedFactorModelLabel") || "已验证因子模型（训练产出）"}
                <span style={{ marginLeft: 6, color: "var(--pt-muted-foreground)" }}>
                  {t("portfolioTrading.strategy.validatedFactorModelHint") ||
                    "不选则默认只用 UI 因子权重；选中后会覆盖为该模型已训练出的真实权重"}
                </span>
              </label>
              <select
                className="pt-select"
                style={selectFullWidthStyle}
                value={factorModelRunId}
                onChange={(e) => setFactorModelRunId(e.target.value)}
                aria-label={t("portfolioTrading.strategy.validatedFactorModelLabel") || "Validated Factor Model Run"}
              >
                <option value="">
                  {t("portfolioTrading.strategy.validatedFactorModelNone") || "— 不绑定，使用 UI 因子权重 —"}
                </option>
                {factorModelRuns.map((run) => {
                  const idTail = run.id.length > 10 ? `${run.id.slice(0, 8)}…${run.id.slice(-4)}` : run.id;
                  const summary =
                    (Array.isArray(run.weights) ? run.weights.length : 0) > 0
                      ? `${(Array.isArray(run.weights) ? run.weights.length : 0)} 因子`
                      : "";
                  const statusBadge = run.status === "validated" ? "" : `[${run.status}]`;
                  return (
                    <option key={run.id} value={run.id}>
                      {`${run.model_type} / ${run.asset_type} — ${idTail} — ${summary} ${statusBadge}`.trim()}
                    </option>
                  );
                })}
              </select>
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
                    const icList = (run.weights || [])
                      .map((w) => ({
                        code: w.factor_code,
                        ic: w.validation_ic ?? w.train_ic ?? null,
                        w: w.normalized_weight,
                      }))
                      .filter((x) => x.ic != null)
                      .sort((a, b) => Math.abs(b.ic as number) - Math.abs(a.ic as number))
                      .slice(0, 5);
                    return (
                      <>
                        <div>
                          <strong style={{ color: "var(--pt-foreground)" }}>权重构成（前 5）：</strong>
                          {icList.length
                            ? icList
                                .map((x) => `${x.code} ${Math.round(Number(x.w) * 100)}% (IC${Number(x.ic).toFixed(3)})`)
                                .join("，")
                            : "（暂无因子权重数据）"}
                        </div>
                        {run.metrics && typeof run.metrics === "object" ? (
                          <div style={{ marginTop: 4 }}>
                            <strong style={{ color: "var(--pt-foreground)" }}>验证集指标：</strong>
                            {Object.entries(run.metrics as Record<string, unknown>)
                              .filter(([, v]) => typeof v === "number" && Number.isFinite(v))
                              .slice(0, 5)
                              .map(([k, v]) => `${k}=${Number(v).toFixed(4)}`)
                              .join("，") || "（无）"}
                          </div>
                        ) : null}
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
                    const statusColor =
                      status === "frozen" ? "var(--pt-state-info)" :
                      status === "deprecated" ? "var(--pt-state-error)" :
                      status === "draft" ? "var(--pt-state-warning)" :
                      "var(--pt-muted-foreground)";
                    return (
                      <>
                        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                          <Snowflake size={12} style={{ color: "var(--pt-state-info)" }} />
                          <strong style={{ color: "var(--pt-foreground)" }}>溯源链只读映射（C-05）：</strong>
                          <span>FactorModelRun → FactorSet</span>
                        </div>
                        <div style={{ marginTop: 4 }}>
                          FactorSet ID：<code style={{ fontFamily: "var(--pt-font-mono)" }}>{boundFactorSetId}</code>
                          {fs?.name ? ` · ${fs.name}` : ""}
                          <span
                            style={{
                              marginLeft: 8,
                              padding: "1px 7px",
                              borderRadius: 999,
                              background: `${statusColor}1A`,
                              color: statusColor,
                              fontSize: 11,
                            }}
                          >
                            status={status}
                          </span>
                          {fs?.n_members != null ? (
                            <span style={{ marginLeft: 8 }}>成员 {fs.n_members} 个</span>
                          ) : null}
                          {fs?.frozen_at ? (
                            <span style={{ marginLeft: 8 }}>frozen_at={String(fs.frozen_at).replace("T", " ").slice(0, 16)}</span>
                          ) : null}
                        </div>
                        {status !== "frozen" ? (
                          <div style={{ marginTop: 4, color: "var(--pt-state-warning)" }}>
                            ⚠️ 当前绑定的 FactorSet 不是 frozen 状态，无法保证 C-06 权重溯源契约；请在「因子模型页」先冻结 FactorSet 再训练模型。
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
              <select
                className="pt-select"
                style={selectFullWidthStyle}
                value={stockPool}
                onChange={(e) => setStockPool(e.target.value)}
              >
                {STOCK_POOLS.map((s) => <option key={s} value={s}>{s}</option>)}
              </select>
            </div>
            {/* 调仓周期 */}
 <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              <label style={fieldLabelStyle}>{t("portfolioTrading.strategy.rebalancePeriod")}</label>
              <select
                className="pt-select"
                style={selectFullWidthStyle}
                value={rebalancePeriod}
                onChange={(e) => setRebalancePeriod(e.target.value)}
              >
                {REBALANCE_PERIODS.map((r) => <option key={r} value={r}>{r}</option>)}
              </select>
            </div>
            {/* 权重分配 */}
 <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              <label style={fieldLabelStyle}>{t("portfolioTrading.strategy.weighting")}</label>
              <select
                className="pt-select"
                style={selectFullWidthStyle}
                value={weighting}
                onChange={(e) => setWeighting(e.target.value)}
              >
                {WEIGHTINGS.map((w) => <option key={w} value={w}>{w}</option>)}
              </select>
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
          <div style={{ padding: "12px 16px", borderBottom: "1px solid var(--pt-border)" }}>
            <h3 style={sectionTitleStyle}>{t("portfolioTrading.strategy.conditionTitle")}</h3>
            <p style={sectionSubStyle}>{t("portfolioTrading.strategy.conditionSub")}</p>
          </div>
          <div style={{ padding: 16, display: "flex", flexDirection: "column", gap: 16 }}>
            {/* 择时信号 */}
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              <label style={fieldLabelStyle}>{t("portfolioTrading.strategy.timingSignal")}</label>
              <select
                className="pt-select"
                style={selectFullWidthStyle}
                value={timingSignal}
                onChange={(e) => setTimingSignal(e.target.value)}
              >
                {TIMING_SIGNALS.map((s) => <option key={s} value={s}>{s}</option>)}
              </select>
            </div>
            {/* 单股持仓上限 */}
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              <label style={fieldLabelStyle}>{t("portfolioTrading.strategy.maxSinglePosition")}</label>
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <input
                  type="number"
                  className="pt-input"
                  style={{ flex: 1 }}
                  value={displaySinglePosition}
                  min={0}
                  max={100}
                  onChange={(e) => setMaxSinglePosition(Number(e.target.value))}
                  disabled={loading}
                />
                <span style={fieldLabelStyle}>%</span>
              </div>
            </div>
            {/* 个股止损 */}
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              <label style={fieldLabelStyle}>{t("portfolioTrading.strategy.stopLoss")}</label>
              <select
                className="pt-select"
                style={selectFullWidthStyle}
                value={displayStopLoss}
                onChange={(e) => setStopLoss(e.target.value)}
                disabled={loading}
              >
                {STOP_LOSSES.map((s) => <option key={s} value={s}>{s}</option>)}
              </select>
            </div>
            {/* 组合回撤熔断 */}
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              <label style={fieldLabelStyle}>{t("portfolioTrading.strategy.drawdownCircuit")}</label>
              <select
                className="pt-select"
                style={selectFullWidthStyle}
                value={displayDrawdown}
                onChange={(e) => setDrawdownCircuit(e.target.value)}
                disabled={loading}
              >
                {DRAWDOWN_CIRCUITS.map((d) => <option key={d} value={d}>{d}</option>)}
              </select>
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
          {t("portfolioTrading.strategy.saveTip")}
        </span>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <button
            type="button"
            className="pt-btn pt-btn-ghost"
            onClick={handleResetDefault}
            disabled={saving}
          >
            <RotateCcw size={14} />
            {t("portfolioTrading.strategy.resetDefault")}
          </button>
          <button
            type="button"
            className="pt-btn pt-btn-outline"
            onClick={handleSaveDraft}
            disabled={saving}
          >
            <FileText size={14} />
            {t("portfolioTrading.strategy.saveDraft")}
          </button>
          <button
            type="button"
            className="pt-btn pt-btn-primary"
            onClick={handleSaveApply}
            disabled={saving}
          >
            <Save size={14} />
            {t("portfolioTrading.strategy.saveApply")}
          </button>
        </div>
      </div>
    </section>
  );
};

export default PortfolioStrategyRules;
