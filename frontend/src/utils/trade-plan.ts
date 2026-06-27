import type { FutureBuyPlan, FuturePlanTuning, TradeSetup } from "../types";

const clamp = (value: number, min: number, max: number) => Math.min(max, Math.max(min, value));

export function planWithRatio(plan: FutureBuyPlan, ratio: number): FutureBuyPlan {
  return {
    ...plan,
    position_pct: Number((Number(plan.position_pct || 0) * ratio).toFixed(4)),
    amount: Number((Number(plan.amount || 0) * ratio).toFixed(2)),
  };
}

export function invalidFuturePlan(setup: TradeSetup | null): FutureBuyPlan | undefined {
  return (setup?.future_buy_plan ?? []).find(
    (plan) => plan.priority === "avoid" || plan.label === "invalid_below_stop"
  );
}

function tunedPositionAmount(
  positionPct: number,
  totalCapital: number,
  investableRatio: number,
  fallbackAmount: number
) {
  if (!(positionPct >= 0)) return fallbackAmount;
  return Number((Number(totalCapital || 0) * Number(investableRatio || 1) * positionPct).toFixed(2));
}

export function applyFuturePlanTuning(
  plans: FutureBuyPlan[],
  tuning: FuturePlanTuning | undefined,
  totalCapital: number,
  investableRatio: number
): FutureBuyPlan[] {
  if (!tuning) return plans;
  const hasPositionOverride = tuning.positionPct !== undefined;
  const targetPositionPct = clamp(Number(tuning.positionPct ?? 0) / 100, 0, 1);
  const scale = tuning.scalePct !== undefined ? Math.max(0, Number(tuning.scalePct) / 100) : null;
  const pullback = tuning.pullbackPct !== undefined ? Math.max(0, Number(tuning.pullbackPct) / 100) : null;
  const band = tuning.bandPct !== undefined ? clamp(Number(tuning.bandPct) / 100, 0.001, 0.5) : null;
  const horizonDays = tuning.horizonDays !== undefined ? Math.max(1, Number(tuning.horizonDays)) : null;

  return plans.map((plan) => {
    if (plan.priority === "avoid") return horizonDays ? { ...plan, horizon_days: horizonDays } : plan;
    let next = { ...plan };
    if (horizonDays) next.horizon_days = horizonDays;
    if (pullback !== null && next.zone_max !== null) {
      const anchor = Number(next.zone_max);
      const zoneMax = anchor * (1 - pullback);
      next.zone_max = Number(zoneMax.toFixed(2));
      next.zone_min = Number((zoneMax * (1 - (band ?? 0.015))).toFixed(2));
    } else if (band !== null && next.zone_max !== null) {
      next.zone_min = Number((Number(next.zone_max) * (1 - band)).toFixed(2));
    }
    if (hasPositionOverride) {
      next.position_pct = Number(targetPositionPct.toFixed(4));
      next.amount = tunedPositionAmount(targetPositionPct, totalCapital, investableRatio, next.amount);
    } else if (scale !== null) {
      next = planWithRatio(next, scale);
    }
    return next;
  });
}

export function buildLongFuturePlan(
  setup: TradeSetup | null,
  tuning?: FuturePlanTuning,
  totalCapital = 0,
  investableRatio = 1
): FutureBuyPlan[] {
  if (!setup) return [];
  const ma20 = setup.moving_averages?.ma20;
  const anchor = Number(ma20 || setup.entry_min || setup.entry_max || 0);
  if (!(anchor > 0)) return setup.future_buy_plan ?? [];
  const positionPct = Math.max(Number(setup.recommended_position_pct || 0) * 0.5, 0);
  const amount = Number(setup.recommended_position_amount || 0) * 0.5;
  const pullback = Math.max(0, Number(tuning?.pullbackPct ?? 0)) / 100;
  const band = clamp(Number(tuning?.bandPct ?? 5) / 100, 0.001, 0.5);
  const zoneMax = anchor * (1 - pullback + 0.02);
  const plans: FutureBuyPlan[] = [
    {
      label: "long_accumulate_zone",
      horizon_days: Math.max(1, Number(tuning?.horizonDays ?? 30)),
      zone_min: Number((zoneMax * (1 - band)).toFixed(2)),
      zone_max: Number(zoneMax.toFixed(2)),
      priority: "low",
      position_pct: Number(positionPct.toFixed(4)),
      amount: Number(amount.toFixed(2)),
      trigger: "",
    },
  ];
  const tuned = applyFuturePlanTuning(plans, tuning, totalCapital, investableRatio);
  const invalid = invalidFuturePlan(setup);
  if (invalid) tuned.push(invalid);
  return tuned;
}

export function buildCustomFuturePlan(
  setup: TradeSetup | null,
  custom: { horizonDays: number; pullbackPct: number; positionPct: number },
  totalCapital: number,
  investableRatio: number,
  tuning?: FuturePlanTuning
): FutureBuyPlan[] {
  if (!setup) return [];
  const base = Number(setup.entry_max || setup.entry_min || setup.moving_averages?.ma20 || 0);
  if (!(base > 0)) return setup.future_buy_plan ?? [];
  const merged = {
    horizonDays: tuning?.horizonDays ?? custom.horizonDays,
    pullbackPct: tuning?.pullbackPct ?? custom.pullbackPct,
    positionPct: tuning?.positionPct ?? custom.positionPct,
    bandPct: tuning?.bandPct ?? 1.5,
  };
  const pullback = Math.max(0, Number(merged.pullbackPct || 0)) / 100;
  const band = clamp(Number(merged.bandPct || 1.5) / 100, 0.001, 0.5);
  const zoneMax = base * (1 - pullback);
  const positionPct = clamp(Number(merged.positionPct || 0) / 100, 0, 1);
  const plans: FutureBuyPlan[] = [
    {
      label: "custom_buy_zone",
      horizon_days: Math.max(1, Number(merged.horizonDays || 20)),
      zone_min: Number((zoneMax * (1 - band)).toFixed(2)),
      zone_max: Number(zoneMax.toFixed(2)),
      priority: "normal",
      position_pct: Number(positionPct.toFixed(4)),
      amount: tunedPositionAmount(positionPct, totalCapital, investableRatio, 0),
      trigger: "",
    },
  ];
  const invalid = invalidFuturePlan(setup);
  if (invalid) plans.push(invalid);
  return plans;
}

export function getActiveFutureBuyPlan(
  setup: TradeSetup | null,
  scenario: string,
  custom: { horizonDays: number; pullbackPct: number; positionPct: number },
  totalCapital: number,
  investableRatio: number,
  tuning?: FuturePlanTuning
): FutureBuyPlan[] {
  const basePlan = setup?.future_buy_plan ?? [];

  switch (scenario) {
    case "short":
      return applyFuturePlanTuning(
        basePlan
          .filter((plan) => plan.priority === "high" || plan.horizon_days <= 5 || plan.priority === "avoid")
          .map((plan) => (plan.priority === "avoid" ? plan : planWithRatio(plan, 0.7))),
        tuning,
        totalCapital,
        investableRatio
      );

    case "long":
      return buildLongFuturePlan(setup, tuning, totalCapital, investableRatio);

    case "custom":
      return buildCustomFuturePlan(setup, custom, totalCapital, investableRatio, tuning);

    case "mid": {
      const midPlans = basePlan
        .filter((plan) => plan.priority === "avoid" || plan.priority !== "high" || plan.horizon_days >= 5)
        .map((plan) => (plan.priority === "avoid" ? plan : planWithRatio(plan, 0.9)));
      return applyFuturePlanTuning(midPlans.length ? midPlans : basePlan, tuning, totalCapital, investableRatio);
    }

    case "general":
    default:
      return applyFuturePlanTuning(basePlan, tuning, totalCapital, investableRatio);
  }
}
