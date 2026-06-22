/* ════════════════════════════════════════════════════════════
   交易计划引擎 - 未来买入计划场景计算
   供 InvestmentCenter / DetailModal 共用
   ════════════════════════════════════════════════════════════ */

import type { FutureBuyPlan, TradeSetup } from "../types";

// ─── 场景比例调整 ───

/** 按比例缩放未来计划的仓位和金额 */
export function planWithRatio(plan: FutureBuyPlan, ratio: number): FutureBuyPlan {
  return {
    ...plan,
    position_pct: Number((Number(plan.position_pct || 0) * ratio).toFixed(4)),
    amount: Number((Number(plan.amount || 0) * ratio).toFixed(2)),
  };
}

/** 获取"跌破无效"计划（止损线以下的计划） */
export function invalidFuturePlan(setup: TradeSetup | null): FutureBuyPlan | undefined {
  return (setup?.future_buy_plan ?? []).find(
    (plan) => plan.priority === "avoid" || plan.label === "invalid_below_stop"
  );
}

// ─── 场景构建器 ───

/**
 * 长期场景: 在MA20附近构建长期积累区间
 * 使用推荐仓位的50%，时间窗口30天，价格区间 MA20 ±3%
 */
export function buildLongFuturePlan(setup: TradeSetup | null): FutureBuyPlan[] {
  if (!setup) return [];
  const ma20 = setup.moving_averages?.ma20;
  const anchor = Number(ma20 || setup.entry_min || setup.entry_max || 0);
  if (!(anchor > 0)) return setup.future_buy_plan ?? [];
  const positionPct = Math.max(Number(setup.recommended_position_pct || 0) * 0.5, 0);
  const amount = Number(setup.recommended_position_amount || 0) * 0.5;
  const plans: FutureBuyPlan[] = [
    {
      label: "long_accumulate_zone",
      horizon_days: 30,
      zone_min: Number((anchor * 0.97).toFixed(2)),
      zone_max: Number((anchor * 1.02).toFixed(2)),
      priority: "low",
      position_pct: Number(positionPct.toFixed(4)),
      amount: Number(amount.toFixed(2)),
      trigger: "",
    },
  ];
  const invalid = invalidFuturePlan(setup);
  if (invalid) plans.push(invalid);
  return plans;
}

/**
 * 自定义场景: 用户指定回撤比例、观察天数、仓位比例
 * 基于当前入场上限构建买区
 */
export function buildCustomFuturePlan(
  setup: TradeSetup | null,
  custom: { horizonDays: number; pullbackPct: number; positionPct: number },
  totalCapital: number,
  investableRatio: number
): FutureBuyPlan[] {
  if (!setup) return [];
  const base = Number(setup.entry_max || setup.entry_min || setup.moving_averages?.ma20 || 0);
  if (!(base > 0)) return setup.future_buy_plan ?? [];
  const pullback = Math.max(0, Number(custom.pullbackPct || 0)) / 100;
  const zoneMax = base * (1 - pullback);
  const zoneMin = zoneMax * 0.985;
  const positionPct = Math.max(0, Number(custom.positionPct || 0)) / 100;
  const amount = Number(totalCapital || 0) * Number(investableRatio || 1) * positionPct;
  const plans: FutureBuyPlan[] = [
    {
      label: "custom_buy_zone",
      horizon_days: Math.max(1, Number(custom.horizonDays || 20)),
      zone_min: Number(zoneMin.toFixed(2)),
      zone_max: Number(zoneMax.toFixed(2)),
      priority: "normal",
      position_pct: Number(positionPct.toFixed(4)),
      amount: Number(amount.toFixed(2)),
      trigger: "",
    },
  ];
  const invalid = invalidFuturePlan(setup);
  if (invalid) plans.push(invalid);
  return plans;
}

// ─── 场景路由器 ───

/**
 * 根据场景名称返回对应的未来买入计划列表
 *
 * 场景说明:
 *   general  - 标准场景：使用原始计划，不做调整
 *   short    - 短期场景：只保留高优先级+短期计划，仓位缩至70%
 *   mid      - 中期场景：排除高优先级短期计划，仓位缩至90%
 *   long     - 长期场景：在MA20附近构建长期积累区
 *   custom   - 自定义场景：用户指定参数
 */
export function getActiveFutureBuyPlan(
  setup: TradeSetup | null,
  scenario: string,
  custom: { horizonDays: number; pullbackPct: number; positionPct: number },
  totalCapital: number,
  investableRatio: number
): FutureBuyPlan[] {
  const basePlan = setup?.future_buy_plan ?? [];

  switch (scenario) {
    case "short":
      return basePlan
        .filter((plan) => plan.priority === "high" || plan.horizon_days <= 5 || plan.priority === "avoid")
        .map((plan) => (plan.priority === "avoid" ? plan : planWithRatio(plan, 0.7)));

    case "long":
      return buildLongFuturePlan(setup);

    case "custom":
      return buildCustomFuturePlan(setup, custom, totalCapital, investableRatio);

    case "mid": {
      const midPlans = basePlan
        .filter((plan) => plan.priority === "avoid" || plan.priority !== "high" || plan.horizon_days >= 5)
        .map((plan) => (plan.priority === "avoid" ? plan : planWithRatio(plan, 0.9)));
      return midPlans.length ? midPlans : basePlan;
    }

    case "general":
    default:
      return basePlan;
  }
}
