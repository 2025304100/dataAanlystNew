/** 因子模型评估分级纯函数单元测试（utils/factorRating.ts）。
 *  设计原则：
 *   1) 边界优先：每个阈值的 = 与 刚好小于 都各覆盖一个用例（例如 IC=0.02 "优"、IC=0.01999 "良"）。
 *   2) 关联场景：样本少但 IC 高 → 文案命中"高 IC 陷阱"分支；样本多 + IC 正常 → 文案正常。
 *   3) 异常输入：null / undefined / NaN / Infinity / 负数 / 字符串格式错 → 走 missing 或 warning 兜底。
 *   4) 日期特殊：跨月、闰年、UTC+8 含时分秒偏移 的格式化/滞后计算。
 */
import { describe, expect, it } from "vitest";
import {
  LEVEL_COLOR,
  LEVEL_TEXT,
  daysFromToday,
  formatDateTime,
  rateCoverage,
  rateDataCutoff,
  rateLatestTradeDate,
  rateModeConsistency,
  rateSampleCount,
  rateValidationIC,
  ratedColorFg,
  type RatingLevel,
} from "../factorRating";

describe("factorRating 颜色与等级常量", () => {
  it("LEVEL_COLOR / LEVEL_TEXT 对 5 个等级都有定义，且映射稳定", () => {
    const levels: RatingLevel[] = ["excellent", "good", "warning", "danger", "missing"];
    expect(levels.every((l) => typeof LEVEL_COLOR[l] === "string")).toBe(true);
    expect(levels.every((l) => typeof LEVEL_TEXT[l] === "string")).toBe(true);
    expect(LEVEL_TEXT.excellent).toBe("优");
    expect(LEVEL_TEXT.missing).toBe("缺失");
    expect(LEVEL_COLOR.danger).toBe("red");
  });

  it("ratedColorFg 返回与等级对应的前景色（十六进制），missing 走 CSS 变量", () => {
    expect(ratedColorFg("excellent")).toMatch(/^#/);
    expect(ratedColorFg("good")).toMatch(/^#/);
    expect(ratedColorFg("warning")).toMatch(/^#/);
    expect(ratedColorFg("danger")).toMatch(/^#/);
    expect(ratedColorFg("missing")).toContain("var(--pt-muted-foreground)");
    // 防色盲回归：good 必须是蓝系（避免 good/warning 都黄）
    expect(ratedColorFg("good")).toBe("#1d4ed8");
  });
});

describe("rateValidationIC（验证 IC 分级）", () => {
  const cases: Array<{ input: number | null | undefined; level: RatingLevel; desc: string; textCheck?: RegExp }> = [
    { input: null, level: "missing", desc: "null → missing" },
    { input: undefined, level: "missing", desc: "undefined → missing" },
    { input: Number.NaN, level: "missing", desc: "NaN → missing" },
    { input: Number.POSITIVE_INFINITY, level: "missing", desc: "+Infinity 非有限 → missing" },
    { input: -0.05, level: "danger", desc: "IC 负数（反指）→ danger" },
    { input: 0, level: "danger", desc: "IC = 0 → danger（无预测力）" },
    { input: 0.0001, level: "danger", desc: "IC 正但 < 0.005 → danger" },
    { input: 0.005, level: "warning", desc: "IC = 0.005（下边界）→ warning" },
    { input: 0.00999, level: "warning", desc: "IC < 0.01 且 ≥ 0.005 → warning" },
    { input: 0.01, level: "good", desc: "IC = 0.01（下边界）→ good" },
    { input: 0.015, level: "good", desc: "IC 0.01 ~ 0.02 → good" },
    { input: 0.02, level: "excellent", desc: "IC = 0.02（下边界）→ excellent" },
    { input: 0.03, level: "excellent", desc: "IC > 0.02 → excellent", textCheck: /0\.0300/ },
  ];
  it.each(cases)("$desc", ({ input, level, textCheck }) => {
    const r = rateValidationIC(input);
    expect(r.level).toBe(level);
    if (level === "missing") {
      expect(r.text).toBe("-");
      expect(r.hint).toMatch(/未产出验证 IC/);
    } else {
      // 数值文本 4 位小数
      expect(r.text).toMatch(/^-?\d+\.\d{4}$/);
    }
    if (textCheck) expect(r.text).toMatch(textCheck);
    // 每个 level 的 hint 都要非空，且是中文解释句子
    expect(r.hint.length).toBeGreaterThan(10);
  });
});

describe("rateSampleCount（样本数量分级 + 高 IC 陷阱联动）", () => {
  it("异常值 / 0 样本 → missing 文本 0，提示'因子缺失'", () => {
    expect(rateSampleCount(null, null).level).toBe("missing");
    expect(rateSampleCount(null, null).text).toBe("0");
    expect(rateSampleCount(undefined, 0.01).level).toBe("missing");
    expect(rateSampleCount(0, 0.01).level).toBe("missing");
    expect(rateSampleCount(NaN, 0.01).level).toBe("missing");
    expect(rateSampleCount(-5, 0.01).level).toBe("missing");
  });

  it("千分位格式：645085 → '645,085'（zh-CN）", () => {
    expect(rateSampleCount(645_085, 0.0171).text).toBe("645,085");
  });

  it("优 / 良 / 注意 / 差 的数值边界", () => {
    expect(rateSampleCount(500_000, 0.01).level).toBe("excellent");
    expect(rateSampleCount(500_001, 0.01).level).toBe("excellent");
    expect(rateSampleCount(499_999, 0.01).level).toBe("good");
    expect(rateSampleCount(100_000, 0.01).level).toBe("good");
    expect(rateSampleCount(99_999, 0.01).level).toBe("warning");
    expect(rateSampleCount(10_000, 0.01).level).toBe("warning");
    expect(rateSampleCount(9_999, 0.01).level).toBe("danger");
    expect(rateSampleCount(1_000, 0.01).level).toBe("danger");
    expect(rateSampleCount(999, 0.01).level).toBe("danger");
    expect(rateSampleCount(999, 0.01).hint).toMatch(/< 1000/);
  });

  it("关联场景：样本 1000 + IC≥0.02 → danger 文案命中'少 + IC 高' 高 IC 陷阱", () => {
    const r = rateSampleCount(1_000, 0.03);
    expect(r.level).toBe("danger");
    expect(r.hint).toContain("少 + IC 高");
    expect(r.hint).toContain("高 IC 陷阱");
  });

  it("关联场景：样本 1000 + IC 正常 → danger 文案为'过少'（不触发高 IC 陷阱扩展）", () => {
    const r = rateSampleCount(1_000, 0.008);
    expect(r.level).toBe("danger");
    expect(r.hint).toContain("过少");
    expect(r.hint).not.toContain("少 + IC 高");
  });
});

describe("daysFromToday / formatDateTime 日期工具", () => {
  it("daysFromToday 基本差值", () => {
    expect(daysFromToday("2026-08-29", "2026-08-29")).toBe(0);
    expect(daysFromToday("2026-08-28", "2026-08-29")).toBe(1);
    expect(daysFromToday("2026-08-20", "2026-08-29")).toBe(9);
    // 空输入
    expect(daysFromToday(null, "2026-08-29")).toBeNull();
    expect(daysFromToday(undefined, "2026-08-29")).toBeNull();
    // 没 latestTradeDate 时 base = today，但这里我们都传 latestTradeDate，所以无需断言 today 的值
  });

  it("daysFromToday 跨月、闰年", () => {
    // 跨月：2026-02-28 → 2026-03-02 = 2 天（2026 非闰年）
    expect(daysFromToday("2026-02-28", "2026-03-02")).toBe(2);
    // 闰年：2024-02-28 → 2024-03-02 = 3 天
    expect(daysFromToday("2024-02-28", "2024-03-02")).toBe(3);
  });

  it("daysFromToday 自动截断到 yyyy-mm-dd，忽略时分秒", () => {
    expect(daysFromToday("2026-08-29T08:00:00", "2026-08-29")).toBe(0);
    expect(daysFromToday("2026-08-29T23:59:59", "2026-08-29")).toBe(0);
  });

  it("formatDateTime 格式化日期时间（日期部分稳定、时分秒部分按本地时区显示但数值合法）", () => {
    // 无时区后缀 → 按 UTC 解释再转本地时区显示（China UTC+8 会 +8h）
    const a = formatDateTime("2026-08-14T08:00:00");
    expect(a).toMatch(/^2026-08-14 \d{2}:\d{2}:\d{2}$/);
    // 有 Z → 日期部分同样稳定（2026-07-01）
    const b = formatDateTime("2026-07-01T00:00:00Z");
    expect(b.startsWith("2026-07-")).toBe(true);
    // 空 / 非法 → "-"
    expect(formatDateTime(null)).toBe("-");
    expect(formatDateTime("not-a-date")).toBe("-");
  });
});

describe("rateDataCutoff（数据截止新鲜度，5 等级）", () => {
  const LATEST = "2026-08-29";
  it("v 为空 → missing", () => {
    const r = rateDataCutoff(null, LATEST);
    expect(r.level).toBe("missing");
    expect(r.text).toBe("-");
  });

  it("v 非法 latestTradeDate 导致 d 为 null → warning", () => {
    const r = rateDataCutoff("2026-08-29T08:00:00", null);
    // 当 latestTradeDate 是 null 时走 new Date() 兜底，所以 d 不会是 null；
    // 构造 NaN 触发：latestTradeDate 传明显非法字符串
    const r2 = rateDataCutoff("2026-08-29T08:00:00", "not-a-date");
    expect(r2.level).toBe("warning");
    expect(r2.hint).toMatch(/无法对比/);
  });

  it("边界 d=0/3/10/20/21 → 对应级别", () => {
    expect(rateDataCutoff("2026-08-29", LATEST).level).toBe("excellent"); // 0
    expect(rateDataCutoff("2026-08-26", LATEST).level).toBe("excellent"); // 3
    expect(rateDataCutoff("2026-08-25", LATEST).level).toBe("good"); // 4
    expect(rateDataCutoff("2026-08-19", LATEST).level).toBe("good"); // 10
    expect(rateDataCutoff("2026-08-18", LATEST).level).toBe("warning"); // 11
    expect(rateDataCutoff("2026-08-09", LATEST).level).toBe("warning"); // 20
    expect(rateDataCutoff("2026-08-08", LATEST).level).toBe("danger"); // 21
  });

  it("danger 级别 hint 必须强约束：'禁止直接启用'", () => {
    expect(rateDataCutoff("2026-07-01", LATEST).hint).toMatch(/禁止直接启用/);
  });
});

describe("rateCoverage（覆盖率分级，0-1 小数转百分比文本）", () => {
  it("空/NaN/Inf → missing", () => {
    expect(rateCoverage(null).level).toBe("missing");
    expect(rateCoverage(undefined).level).toBe("missing");
    expect(rateCoverage(NaN).level).toBe("missing");
    expect(rateCoverage(Infinity).level).toBe("missing");
  });

  it("文本格式 x.x%（保留 1 位小数）", () => {
    expect(rateCoverage(0.50).text).toBe("50.0%");
    expect(rateCoverage(0.341).text).toBe("34.1%");
    expect(rateCoverage(0).text).toBe("0.0%");
    expect(rateCoverage(1).text).toBe("100.0%");
  });

  it("边界 50% / 35% / 20%", () => {
    expect(rateCoverage(0.50).level).toBe("excellent");
    expect(rateCoverage(0.4999).level).toBe("good");
    expect(rateCoverage(0.35).level).toBe("good");
    expect(rateCoverage(0.3499).level).toBe("warning");
    expect(rateCoverage(0.20).level).toBe("warning");
    expect(rateCoverage(0.1999).level).toBe("danger");
    expect(rateCoverage(0).level).toBe("danger");
  });
});

describe("rateModeConsistency（决策模式 vs 实际评分来源一致性）", () => {
  it("一致 → excellent，hint 包含'一致'与'闭环'", () => {
    const r = rateModeConsistency("ridge", "ridge", (m) => m.toUpperCase());
    expect(r.level).toBe("excellent");
    expect(r.text).toBe("RIDGE");
    expect(r.hint).toMatch(/一致/);
    expect(r.hint).toMatch(/闭环/);
  });

  it("不一致 → danger，hint 包含'不一致'与'立刻修复'", () => {
    const r = rateModeConsistency("ridge", "shadow");
    expect(r.level).toBe("danger");
    expect(r.hint).toMatch(/不一致/);
    expect(r.hint).toMatch(/立刻修复/);
  });

  it("undefined → 走 manual 默认值；两者都 undefined → 一致 excellent", () => {
    expect(rateModeConsistency(undefined, undefined).level).toBe("excellent");
    expect(rateModeConsistency(undefined, "shadow").level).toBe("danger");
    expect(rateModeConsistency("ridge" as const, undefined).level).toBe("danger");
  });
});

describe("rateLatestTradeDate（因子仓库最新交易日）", () => {
  const TODAY = "2026-08-29";
  it("空 → missing，hint 含 daily bar", () => {
    const r = rateLatestTradeDate(null, TODAY);
    expect(r.level).toBe("missing");
    expect(r.hint).toMatch(/daily bar/);
  });

  it("边界 d=0/1 → excellent，d=2/3/4 → good，d≥5 → warning", () => {
    expect(rateLatestTradeDate("2026-08-29", TODAY).level).toBe("excellent");
    expect(rateLatestTradeDate("2026-08-28", TODAY).level).toBe("excellent");
    expect(rateLatestTradeDate("2026-08-27", TODAY).level).toBe("good"); // 2
    expect(rateLatestTradeDate("2026-08-25", TODAY).level).toBe("good"); // 4
    expect(rateLatestTradeDate("2026-08-24", TODAY).level).toBe("warning"); // 5
    expect(rateLatestTradeDate("2026-08-01", TODAY).level).toBe("warning"); // 28
  });

  it("warning 级 hint 必须提醒'行情数据同步可能卡住'", () => {
    expect(rateLatestTradeDate("2026-07-15", TODAY).hint).toMatch(/行情数据同步可能卡住/);
  });

  it("当 latestTradeDate（第一个参数）明显非法时 d 为 null → warning 兜底", () => {
    // todayStr 合法、v 非法字符串 → d 无法计算 → warning
    const r = rateLatestTradeDate("not-a-date", TODAY);
    expect(r.level).toBe("warning");
  });
});
