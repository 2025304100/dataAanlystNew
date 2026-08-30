/** 因子模型评估分级（纯函数，无 React 依赖）。
 *  level = excellent / good / warning / danger / missing  统一颜色与等级文案。
 *  本文件仅包含“输入数值/字符串 → RatedCell”的纯计算，便于单测与复用。
 */
import type { FactorWeightMode } from "../api/client";

export type RatingLevel = "excellent" | "good" | "warning" | "danger" | "missing";

export const LEVEL_COLOR: Record<RatingLevel, string> = {
  excellent: "green",
  good: "geekblue",
  warning: "gold",
  danger: "red",
  missing: "default",
};

export const LEVEL_TEXT: Record<RatingLevel, string> = {
  excellent: "优",
  good: "良",
  warning: "注意",
  danger: "差",
  missing: "缺失",
};

export type RatedCell = {
  /** 给组件显示的文本（已格式化，如百分比/千分位） */
  text: string;
  /** 分级枚举，驱动颜色与等级胶囊 */
  level: RatingLevel;
  /** 浮到 ? 上的中文解释（为什么给这个级别 + 改进建议） */
  hint: string;
};

export function ratedColorFg(level: RatingLevel): string {
  switch (level) {
    case "excellent":
      return "#15803d";
    case "good":
      return "#1d4ed8";
    case "warning":
      return "#a16207";
    case "danger":
      return "#b91c1c";
    default:
      return "var(--pt-muted-foreground)";
  }
}

/** 工具：把 ISO/yyyy-mm-dd 字符串 → 与 latestTradeDate 的天数差（正数=滞后）。
 *  独立出来方便单测。 */
export function daysFromToday(dateStr: string | null | undefined, latestTradeDate: string | null | undefined): number | null {
  if (!dateStr) return null;
  const base = latestTradeDate || new Date().toISOString().slice(0, 10);
  const a = new Date(`${base}T00:00:00Z`);
  const b = new Date(`${String(dateStr).slice(0, 10)}T00:00:00Z`);
  const diff = Math.round((a.getTime() - b.getTime()) / 86400000);
  return Number.isFinite(diff) ? diff : null;
}

/** 工具：格式化日期时间（和组件一致口径，便于断言）。 */
function pad(n: number) {
  return String(n).padStart(2, "0");
}
function parseDateTime(value: string | null | undefined) {
  if (!value) return null;
  const hasTimeZone = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(value);
  const parsed = new Date(hasTimeZone ? value : `${value}Z`);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}
export function formatDateTime(value: string | null | undefined) {
  const parsed = parseDateTime(value);
  if (!parsed) return "-";
  return [
    `${parsed.getFullYear()}-${pad(parsed.getMonth() + 1)}-${pad(parsed.getDate())}`,
    `${pad(parsed.getHours())}:${pad(parsed.getMinutes())}:${pad(parsed.getSeconds())}`,
  ].join(" ");
}

// ---------- 各字段评级函数 ----------

export function rateValidationIC(v: number | null | undefined): RatedCell {
  if (v == null || !Number.isFinite(v)) {
    return {
      text: "-",
      level: "missing",
      hint: "本次训练未产出验证 IC，通常是样本不足或训练异常中断；该模型必然处于已拒绝状态，不能启用。",
    };
  }
  const text = v.toFixed(4);
  if (v >= 0.02) {
    return {
      text,
      level: "excellent",
      hint: "验证 IC ≥ 0.02：单因子/模型在 A 股上属于强可用水平。但必须配合「样本 ≥ 30 万、数据新鲜」才能直接启用，否则容易是窄样本过拟合。",
    };
  }
  if (v >= 0.01) {
    return {
      text,
      level: "good",
      hint: "验证 IC 0.01 ~ 0.02：实盘常见稳定区间。配合 ≥ 50 万样本可放心进入影子运行/正式启用。",
    };
  }
  if (v >= 0.005) {
    return {
      text,
      level: "warning",
      hint: "验证 IC 0.005 ~ 0.01：边缘合格。建议先影子运行 10 个交易日，同时看覆盖率与 G5 动作匹配率，不要立刻替换当前在役模型。",
    };
  }
  if (v > 0) {
    return {
      text,
      level: "danger",
      hint: "验证 IC 虽为正但 < 0.005：几乎没有稳定的排名预测力，通常会被拒绝门禁。请改训练窗口或扩充因子集后重跑。",
    };
  }
  return {
    text,
    level: "danger",
    hint: "验证 IC ≤ 0：模型是反指或无预测力。拒绝；不能影子运行也不能启用。",
  };
}

export function rateSampleCount(v: number | null | undefined, ic: number | null | undefined): RatedCell {
  const n = typeof v === "number" ? v : NaN;
  if (!Number.isFinite(n) || n <= 0) {
    return {
      text: "0",
      level: "missing",
      hint: "样本=0：训练时根本没拉出 股票×交易日 的打分矩阵（通常是因子缺失），IC=0 也无意义。",
    };
  }
  const text = n.toLocaleString("zh-CN");
  const strongIC = typeof ic === "number" && ic >= 0.02;
  if (n >= 500_000) {
    return {
      text,
      level: "excellent",
      hint: "样本 ≥ 50 万：覆盖足够多的日期×股票横截面，过拟合风险低。即便 IC 高也能比较放心启用。",
    };
  }
  if (n >= 100_000) {
    return {
      text,
      level: "good",
      hint: "样本 10 万 ~ 50 万：A 股中等训练规模。IC ≥ 0.01 可正常走影子运行后启用。",
    };
  }
  if (n >= 10_000) {
    return {
      text,
      level: "warning",
      hint: "样本 1 万 ~ 10 万：覆盖面偏小。如果 IC 偏高（≥0.02）要警惕小样本过拟合，优先走 G5 双跑验证。",
    };
  }
  if (n >= 1_000) {
    return {
      text,
      level: "danger",
      hint: `样本 ${strongIC ? "少 + IC 高" : "过少"}：典型的高 IC 陷阱，十有八九是在几十只股票或几天切片上"记住了数据"，不能启用。`,
    };
  }
  return {
    text,
    level: "danger",
    hint: "样本 < 1000：基本等于一次手工试算，不具备评估价值。",
  };
}

export function rateDataCutoff(v: string | null | undefined, latestTradeDate: string | null | undefined): RatedCell {
  if (!v) {
    return {
      text: "-",
      level: "missing",
      hint: "数据截止缺失：说明训练时没能读到最新行情/因子。这版不要启用。",
    };
  }
  const d = daysFromToday(v, latestTradeDate);
  const text = formatDateTime(v);
  if (d == null) {
    return { text, level: "warning", hint: "无法对比最新交易日，请先刷新因子仓库健康状态。" };
  }
  if (d <= 3) {
    return {
      text,
      level: "excellent",
      hint: `数据截止距离最新交易日仅 ${d} 天：模型能吸收最近行情结构变化，启用更安全。`,
    };
  }
  if (d <= 10) {
    return {
      text,
      level: "good",
      hint: `滞后 ${d} 天（≤ 2 周）：正常可用。若有新版本建议刷新流水线再用。`,
    };
  }
  if (d <= 20) {
    return {
      text,
      level: "warning",
      hint: `已滞后 ${d} 天（2~4 周）。启用前建议先跑一次"全量重算关"的增量流水线，避免模型对最新风格切换失效。`,
    };
  }
  return {
    text,
    level: "danger",
    hint: `已滞后 ${d} 天（> 20 个自然日）：IC 再好看也很可能是对旧行情拟合出来的。禁止直接启用，强制先刷新流水线。`,
  };
}

export function rateCoverage(v: number | null | undefined): RatedCell {
  if (v == null || !Number.isFinite(v)) {
    return {
      text: "-",
      level: "missing",
      hint: "平均覆盖率缺失：通常是因子仓库未就绪或流水线尚未产出评分快照。当前活动模型无法对任何股票可靠打分。",
    };
  }
  const pct = v * 100;
  const text = `${pct.toFixed(1)}%`;
  if (pct >= 50) {
    return {
      text,
      level: "excellent",
      hint: "覆盖率 ≥ 50%：模型对全市场一半以上股票能给分，组合换手与收益会更贴合模型 IC。",
    };
  }
  if (pct >= 35) {
    return {
      text,
      level: "good",
      hint: "覆盖率 35% ~ 50%：A 股常见合理区间，对宽基 / 大中盘组合足够。小市值组合需要继续扩充因子。",
    };
  }
  if (pct >= 20) {
    return {
      text,
      level: "warning",
      hint: "覆盖率 20% ~ 35%：偏低。大量股票会落到「无评分 → 只能 HOLD」，组合表现与模型纸面 IC 的偏差会放大。",
    };
  }
  return {
    text,
    level: "danger",
    hint: "覆盖率 < 20%：模型打分几乎覆盖不到足够股票，治理页 G7 readiness 会持续掉 blocker；不建议进入正式启用。",
  };
}

/** 决策模式 ↔ 实际评分来源 一致性评估。 */
export function rateModeConsistency(
  runtimeMode: FactorWeightMode | string | undefined,
  scoreMode: FactorWeightMode | string | undefined,
  labelFn: (m: string) => string = (m) => m,
): RatedCell {
  const rm = runtimeMode ?? "manual";
  const sm = scoreMode ?? "manual";
  const text = labelFn(sm);
  if (rm === sm) {
    return {
      text,
      level: "excellent",
      hint: `「决策模式 = ${labelFn(rm)}」与「实际评分来源 = ${text}」一致，治理/回放/实盘都会走同一套 Ridge 打分，链路闭环。`,
    };
  }
  return {
    text,
    level: "danger",
    hint: `「决策模式 = ${labelFn(rm)}」但「实际评分来源 = ${text}」不一致。常见原因：正式启用失败、影子/回退切换落库不完整。必须立刻修复；否则治理页 G7 readiness 会一直报警。`,
  };
}

/** 因子仓库最新交易日新鲜度。todayStr 传 yyyy-mm-dd；单测时可固定。 */
export function rateLatestTradeDate(
  v: string | null | undefined,
  todayStr: string = new Date().toISOString().slice(0, 10),
): RatedCell {
  if (!v) {
    return {
      text: "-",
      level: "missing",
      hint: "因子仓库 latest_bar_date 为空：说明 daily bar 没同步到；训练/打分均不可信。",
    };
  }
  const d = daysFromToday(v, todayStr); // today - v = 滞后天数（≥0），负数表示 v 超前于 today
  const text = v;
  if (d == null) return { text, level: "warning", hint: "无法计算与今天的差值。" };
  // 特殊：v 超前于 today（时差/时区漂移/数据写入错误），记 warning 并提示校对系统时间
  if (d < 0) {
    return {
      text,
      level: "warning",
      hint: `最新交易日超前今天 ${Math.abs(d)} 天，行情数据同步可能卡住。请先在数据中心/同步任务触发"日行情补齐"后再运行流水线。`,
    };
  }
  if (d <= 1) {
    return {
      text,
      level: "excellent",
      hint: `最新交易日与今天只差 ${d} 天：行情同步正常。流水线会把最近风格变化纳入训练。`,
    };
  }
  if (d <= 4) {
    return {
      text,
      level: "good",
      hint: `滞后 ${d} 个自然日（含周末/节假日属正常）。若连交易日超 2 天未更新则需刷新同步任务。`,
    };
  }
  return {
    text,
    level: "warning",
    hint: `最新交易日距今天 ${d} 天，行情数据同步可能卡住。请先在数据中心/同步任务触发"日行情补齐"后再运行流水线。`,
  };
}
