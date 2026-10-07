// 对账差异类型（后端 DiffKind）→ 用户可读名称、含义与结论分诊。
//
// 背景：拟真走查在「组合风控状态机总控 → ① 对账守恒」看到维度列直接打印后端枚举，
// 用户读到的是「自定义维度 (DECISION_RUN_NOT_FOUND)」——既看不懂这是什么，也不知道
// 下一步该干什么；同时顶栏「守恒校验失败：加减和 ≠ 0」把「当天压根没跑决策、无账可对」
// 说成「算出来不等于零」，把人往「数据算错」的方向带。
// 取值集合以 app/services/portfolio_reconciliation.py 的 DiffKind 字面量为准。
import { t } from "../i18n";

const RECON_DIM_LABEL_KEYS: Record<string, string> = {
  DECISION_RUN_NOT_FOUND: "reconDimDecisionRunNotFound",
  MISSING_ORDER_PLAN: "reconDimMissingOrderPlan",
  UNFILLED_PLAN: "reconDimUnfilledPlan",
  POSITION_MISMATCH: "reconDimPositionMismatch",
  NAV_BROKEN: "reconDimNavBroken",
  NOT_CHECKED: "reconDimNotChecked",
};

const RECON_DIM_WHY_KEYS: Record<string, string> = {
  DECISION_RUN_NOT_FOUND: "reconWhyDecisionRunNotFound",
  MISSING_ORDER_PLAN: "reconWhyMissingOrderPlan",
  UNFILLED_PLAN: "reconWhyUnfilledPlan",
  POSITION_MISMATCH: "reconWhyPositionMismatch",
  NAV_BROKEN: "reconWhyNavBroken",
  NOT_CHECKED: "reconWhyNotChecked",
};

/** 已收录的对账差异类型，供测试与守护使用。 */
export const KNOWN_RECON_DIFF_KINDS: readonly string[] = Object.keys(RECON_DIM_LABEL_KEYS);

/**
 * 对账差异类型的中文/英文名称。
 * @returns 可读名称；未收录时返回通用文案，原值交由 tooltip 承载
 */
export function reconDimLabel(kind?: string | null): string {
  const value = (kind ?? "").trim();
  if (!value) return t("reconDimUnknown");
  const key = RECON_DIM_LABEL_KEYS[value];
  return key ? t(key) : t("reconDimUnknown");
}

/** 未收录类型下给运维看的原值（命中映射时返回 null，不无谓暴露内部枚举）。 */
export function reconDimCodeForTooltip(kind?: string | null): string | null {
  const value = (kind ?? "").trim();
  if (!value) return null;
  return RECON_DIM_LABEL_KEYS[value] ? null : value;
}

/** 该差异类型意味着什么（人话解释；未收录返回 null，由界面回退到后端说明）。 */
export function reconDimWhy(kind?: string | null): string | null {
  const value = (kind ?? "").trim();
  if (!value) return null;
  const key = RECON_DIM_WHY_KEYS[value];
  return key ? t(key) : null;
}

/**
 * 对账结论分诊。
 *
 * 四态各自对应完全不同的用户动作，不能都收敛成一句「守恒失败」：
 *  - passed       ：账实相符，组合可正常参与自动仿真。
 *  - no_decision  ：当日没有跑过 auto_simulation 决策，没有基准可比 ——
 *                   不是数据错误，不该提示用户去「核对数字」。
 *  - unavailable  ：组合未接入订单/成交数据源，本次只做了部分核对（NOT_CHECKED 专用）。
 *  - broken       ：真的存在账实不符，需要人工排查并修复。
 */
export type ReconVerdict = "passed" | "no_decision" | "unavailable" | "broken";

/** 分诊只需要这两个字段，刻意不依赖完整响应类型，便于单测构造。 */
export interface ReconVerdictInput {
  zero_sum_check_passed?: boolean | null;
  items?: ReadonlyArray<{ dimension?: string | null } | null> | null;
}

export function classifyReconVerdict(input: ReconVerdictInput): ReconVerdict {
  if (input.zero_sum_check_passed === true) return "passed";

  const kinds = (input.items ?? [])
    .map((item) => String(item?.dimension ?? "").trim())
    .filter((kind) => kind.length > 0);

  // NOT_CHECKED 是「没接数据源」，不是差异；它单独出现时后端仍会进 items，
  // 但组合状态并不会被阻断，所以不能报成 broken 吓人。
  const blocking = kinds.filter((kind) => kind !== "NOT_CHECKED");

  if (blocking.length === 0) return kinds.length > 0 ? "unavailable" : "passed";
  if (blocking.every((kind) => kind === "DECISION_RUN_NOT_FOUND")) return "no_decision";
  return "broken";
}
