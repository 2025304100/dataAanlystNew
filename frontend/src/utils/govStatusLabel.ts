// 治理阻断状态（DecisionBlockingStatus）→ 用户可读名称。
//
// 拟真走查在组合交易各处（根页/总览/持仓成员/策略规则/治理）检出界面上出现
// `RECONCILIATION_BLOCKED` —— 那是 DecisionEvidenceDrawer 把 run.blocking_status
// 原值当标题/提示语打印，用户看到「门禁阻断：RECONCILIATION_BLOCKED」这种句子。
// 取值集合以 client.ts 的 `export type DecisionBlockingStatus` 为准，
// 由守护 test_decision_blocking_status_has_labels 保证不漏配。
import { t } from "../i18n";

const GOV_STATUS_LABEL_KEYS: Record<string, string> = {
  READY: "govStatusReady",
  DATA_INCOMPLETE_PAUSED: "govStatusDataIncompletePaused",
  RECONCILIATION_BLOCKED: "govStatusReconciliationBlocked",
  MODEL_INACTIVE: "govStatusModelInactive",
  SCORE_STALE: "govStatusScoreStale",
};

/** 已收录的治理状态，供测试与守护使用。 */
export const KNOWN_GOV_STATUSES: readonly string[] = Object.keys(GOV_STATUS_LABEL_KEYS);

/**
 * 治理状态的可见名称。
 * @returns 中文/英文名；未收录时返回通用文案，并把原值留在 tooltip（由调用方决定）
 */
export function govStatusLabel(status?: string | null): string {
  const value = (status ?? "").trim();
  if (!value) return t("govStatusUnknown");
  const key = GOV_STATUS_LABEL_KEYS[value];
  return key ? t(key) : t("govStatusUnknown");
}

/** 未收录状态下给运维看的原值（命中映射时返回 null，不无谓暴露内部枚举）。 */
export function govStatusCodeForTooltip(status?: string | null): string | null {
  const value = (status ?? "").trim();
  if (!value) return null;
  return GOV_STATUS_LABEL_KEYS[value] ? null : value;
}
