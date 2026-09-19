import { describe, it, expect } from "vitest";
import {
  planWithRatio,
  invalidFuturePlan,
  applyFuturePlanTuning,
  buildLongFuturePlan,
  buildCustomFuturePlan,
  getActiveFutureBuyPlan,
} from "../trade-plan";
import type { TradeSetup, FutureBuyPlan, FuturePlanTuning } from "../../types";

function makeSetup(overrides: Partial<TradeSetup> = {}): TradeSetup {
  return {
    entry_min: 10,
    entry_max: 12,
    moving_averages: { ma20: 11 },
    recommended_position_pct: 0.2,
    recommended_position_amount: 2000,
    future_buy_plan: [
      {
        label: "high_priority",
        horizon_days: 3,
        zone_min: 10,
        zone_max: 11,
        priority: "high",
        position_pct: 0.1,
        amount: 1000,
        trigger: "",
      },
      {
        label: "low_priority",
        horizon_days: 30,
        zone_min: 9,
        zone_max: 10,
        priority: "low",
        position_pct: 0.05,
        amount: 500,
        trigger: "",
      },
    ],
    ...overrides,
  } as any;
}

describe("planWithRatio", () => {
  it("按比例缩放 position_pct 和 amount", () => {
    const plan: FutureBuyPlan = {
      label: "test",
      horizon_days: 10,
      zone_min: 10,
      zone_max: 11,
      priority: "normal",
      position_pct: 0.1,
      amount: 1000,
      trigger: "",
    };
    const result = planWithRatio(plan, 0.5);
    expect(result.position_pct).toBe(0.05);
    expect(result.amount).toBe(500);
  });
  it("ratio=1 不变", () => {
    const plan: FutureBuyPlan = {
      label: "test",
      horizon_days: 10,
      zone_min: 10,
      zone_max: 11,
      priority: "normal",
      position_pct: 0.1,
      amount: 1000,
      trigger: "",
    };
    const result = planWithRatio(plan, 1);
    expect(result.position_pct).toBe(0.1);
    expect(result.amount).toBe(1000);
  });
});

describe("invalidFuturePlan", () => {
  it("返回 priority=avoid 的计划", () => {
    const setup = makeSetup({
      future_buy_plan: [
        { label: "valid", horizon_days: 5, zone_min: 10, zone_max: 11, priority: "normal", position_pct: 0.1, amount: 1000, trigger: "" },
        { label: "invalid_below_stop", horizon_days: 0, zone_min: 0, zone_max: 0, priority: "avoid", position_pct: 0, amount: 0, trigger: "" },
      ],
    });
    const result = invalidFuturePlan(setup);
    expect(result).toBeDefined();
    expect(result?.priority).toBe("avoid");
  });
  it("无 avoid 计划返回 undefined", () => {
    const setup = makeSetup();
    const result = invalidFuturePlan(setup);
    expect(result).toBeUndefined();
  });
  it("setup=null 返回 undefined", () => {
    expect(invalidFuturePlan(null)).toBeUndefined();
  });
});

describe("applyFuturePlanTuning", () => {
  const plans: FutureBuyPlan[] = [
    { label: "p1", horizon_days: 10, zone_min: 10, zone_max: 11, priority: "normal", position_pct: 0.1, amount: 1000, trigger: "" },
  ];
  it("无 tuning 返回原数组", () => {
    const result = applyFuturePlanTuning(plans, undefined, 100000, 1);
    expect(result).toEqual(plans);
  });
  it("horizonDays 覆盖", () => {
    const tuning: FuturePlanTuning = { horizonDays: 20 };
    const result = applyFuturePlanTuning(plans, tuning, 100000, 1);
    expect(result[0].horizon_days).toBe(20);
  });
  it("positionPct 覆盖并重算 amount", () => {
    const tuning: FuturePlanTuning = { positionPct: 50 };
    // totalCapital=100000, investableRatio=0.8, positionPct=50% → 0.5
    // amount = 100000 * 0.8 * 0.5 = 40000
    const result = applyFuturePlanTuning(plans, tuning, 100000, 0.8);
    expect(result[0].position_pct).toBe(0.5);
    expect(result[0].amount).toBe(40000);
  });
  it("pullbackPct 调整 zone_max", () => {
    const tuning: FuturePlanTuning = { pullbackPct: 10, bandPct: 5 };
    // anchor=11, pullback=10% → zoneMax = 11 * (1 - 0.1) = 9.9
    // zoneMin = 9.9 * (1 - 0.05) = 9.405
    const result = applyFuturePlanTuning(plans, tuning, 100000, 1);
    expect(result[0].zone_max).toBe(9.9);
    expect(result[0].zone_min).toBeCloseTo(9.41, 1);
  });
  it("avoid 计划仅应用 horizonDays", () => {
    const avoidPlan: FutureBuyPlan = { label: "avoid", horizon_days: 0, zone_min: 0, zone_max: 0, priority: "avoid", position_pct: 0, amount: 0, trigger: "" };
    const tuning: FuturePlanTuning = { horizonDays: 5, positionPct: 50 };
    const result = applyFuturePlanTuning([avoidPlan], tuning, 100000, 1);
    expect(result[0].horizon_days).toBe(5);
    expect(result[0].position_pct).toBe(0); // 不被覆盖
  });
});

describe("buildLongFuturePlan", () => {
  it("setup=null 返回空数组", () => {
    expect(buildLongFuturePlan(null)).toEqual([]);
  });
  it("anchor<=0 返回 future_buy_plan 原值", () => {
    const setup = makeSetup({ moving_averages: { ma20: 0 }, entry_min: 0, entry_max: 0 });
    const result = buildLongFuturePlan(setup);
    expect(result).toEqual(setup.future_buy_plan);
  });
  it("正常构建：基于 ma20 锚点", () => {
    const setup = makeSetup();
    const result = buildLongFuturePlan(setup, undefined, 100000, 1);
    expect(result.length).toBeGreaterThan(0);
    expect(result[0].label).toBe("long_accumulate_zone");
    // position_pct = max(0.2 * 0.5, 0) = 0.1
    expect(result[0].position_pct).toBe(0.1);
  });
});

describe("buildCustomFuturePlan", () => {
  it("setup=null 返回空数组", () => {
    expect(buildCustomFuturePlan(null, { horizonDays: 20, pullbackPct: 3, positionPct: 5 }, 100000, 1)).toEqual([]);
  });
  it("正常构建自定义买点", () => {
    const setup = makeSetup();
    const result = buildCustomFuturePlan(setup, { horizonDays: 20, pullbackPct: 3, positionPct: 5 }, 100000, 1);
    expect(result.length).toBeGreaterThan(0);
    expect(result[0].label).toBe("custom_buy_zone");
    expect(result[0].horizon_days).toBe(20);
  });
});

describe("getActiveFutureBuyPlan", () => {
  const setup = makeSetup();
  const custom = { horizonDays: 20, pullbackPct: 3, positionPct: 5 };

  it("scenario=short: 仅保留高优先级/短期/avoid", () => {
    const result = getActiveFutureBuyPlan(setup, "short", custom, 100000, 1);
    // 高优先级 plan 应用 0.7 缩放，avoid 原样保留
    expect(result.length).toBeGreaterThan(0);
  });
  it("scenario=long: 委托给 buildLongFuturePlan", () => {
    const result = getActiveFutureBuyPlan(setup, "long", custom, 100000, 1);
    expect(result.some((p) => p.label === "long_accumulate_zone")).toBe(true);
  });
  it("scenario=custom: 委托给 buildCustomFuturePlan", () => {
    const result = getActiveFutureBuyPlan(setup, "custom", custom, 100000, 1);
    expect(result.some((p) => p.label === "custom_buy_zone")).toBe(true);
  });
  it("scenario=general: 应用 tuning 到原 future_buy_plan", () => {
    const result = getActiveFutureBuyPlan(setup, "general", custom, 100000, 1);
    expect(result.length).toBe(setup.future_buy_plan!.length);
  });
  it("scenario=mid: 保留非高优先级/中长期", () => {
    const result = getActiveFutureBuyPlan(setup, "mid", custom, 100000, 1);
    expect(result.length).toBeGreaterThan(0);
  });
  it("未知 scenario 默认走 general", () => {
    const result = getActiveFutureBuyPlan(setup, "unknown", custom, 100000, 1);
    expect(result.length).toBe(setup.future_buy_plan!.length);
  });
});
