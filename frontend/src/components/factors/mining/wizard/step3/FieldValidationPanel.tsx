import { t } from "../../../../../i18n";
import { Progress } from "antd";
import type { BlockedItem, ValidationStatus } from "./fieldTypes";

/**
 * 校验进度与阻断详情面板（向导 §5.1，设计 §9.2）。
 *
 * - 进度按**分片**展示「已完成 / 总数」（校验可达数小时，支持离开页面与断点续跑）；
 * - 结果三态：通过 / 警告（可继续但需确认）/ 阻断（不能进入下一步）；
 * - **阻断只给两个出口**（任务卡 pitfalls）：重新选择字段 / 去修复数据；
 *   **不提供逐字段自动修复**（not_do）—— 出口按钮在阻断项**之外**，
 *   阻断项内不含任何操作按钮（有测试断言守着）。
 */
export interface FieldValidationPanelProps {
  status: ValidationStatus | null;
  onReselectFields?: () => void;
  onRepairData?: () => void;
}

function BlockedRow({ item }: { item: BlockedItem }) {
  return (
    <div className="mining-field-blocked-item" data-field-blocked-item>
      <span>{item.field}</span>
      {item.name_zh ? <span>{item.name_zh}</span> : null}
      <span className="mining-chip mining-chip--danger">{item.issue ?? "-"}</span>
      <span>
        {t("miningFieldCurrent")}: {String(item.current ?? "-")} /{" "}
        {t("miningFieldRequired")}: {String(item.required ?? "-")}
      </span>
    </div>
  );
}

export default function FieldValidationPanel({
  status, onReselectFields, onRepairData,
}: FieldValidationPanelProps) {
  if (!status) return null;

  const blocked = status.blocked ?? [];
  const isBlocked = status.status === "blocked" || blocked.length > 0;
  const isFailed = status.status === "failed";
  const done = status.progress?.done ?? 0;
  const total = status.progress?.total ?? 0;
  const percent = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;

  /** 后端结论标签（通过/警告/阻断）；无则按本地区分 */
  const verdictLabel =
    status.verdict_label_zh ??
    (status.verdict === "pass"
      ? t("miningFieldVerdictPass")
      : status.verdict === "warn"
        ? t("miningFieldVerdictWarn")
        : status.verdict === "block"
          ? t("miningFieldVerdictBlock")
          : null);
  const verdictTone =
    status.verdict === "block" ? "danger" : status.verdict === "warn" ? "warn" : "success";

  return (
    <div className="mining-field-validation">
      {status.progress ? (
        <div className="mining-field-progress" data-field-progress>
          <span>
            {t("miningFieldProgress")}:{" "}
            <span data-field-progress-done>{done}</span> /{" "}
            <span data-field-progress-total>{total}</span>
            <span className="mining-card-sub">({percent}%)</span>
          </span>
          <Progress percent={percent} showInfo={false} size="small" />
        </div>
      ) : null}

      {verdictLabel && (
        <div className="mining-card-head" data-field-verdict>
          <span className={`mining-chip mining-chip--${verdictTone}`}>
            {t("miningFieldVerdictLabel")}: {verdictLabel}
          </span>
          {status.valid_until && (
            <span className="mining-card-sub">
              {t("miningFieldValidUntil")}: {status.valid_until}
            </span>
          )}
        </div>
      )}

      {isFailed && (
        <div className="mining-banner mining-banner--danger" data-field-failed role="alert">
          <div className="mining-banner-body">
            <span className="mining-banner-title">{t("miningFieldFailed")}</span>
            <span>{status.summary_zh ?? status.message ?? "-"}</span>
          </div>
        </div>
      )}

      {status.passed && (
        <div className="mining-field-passed" data-field-passed role="status">
          <span>{t("miningFieldPassed")}</span>
          <span className="mining-card-sub">
            {status.summary_zh ?? t("miningFieldPassedHint")}
          </span>
        </div>
      )}

      {(status.warnings ?? []).length > 0 && (
        <div className="mining-field-warnings" data-field-warnings>
          {status.warnings.map((w, i) => (
            <div key={i}>{w}</div>
          ))}
        </div>
      )}

      {isBlocked && (
        <div className="mining-field-blocked" data-field-blocked>
          <div className="mining-field-blocked-head">
            {t("miningFieldBlocked")}（{blocked.length}）
          </div>
          <span className="mining-card-sub">{t("miningFieldValidationTable")}</span>
          {blocked.map((item) => (
            <BlockedRow key={item.field} item={item} />
          ))}
          {/* 两个出口：**位于阻断项之外**，阻断项内不放任何按钮 */}
          <div className="mining-field-exits">
            <button
              type="button"
              data-field-exit-reselect
              onClick={onReselectFields}
            >
              {t("miningFieldExitReselect")}
            </button>
            <button
              type="button"
              data-field-exit-repair
              onClick={onRepairData}
            >
              {t("miningFieldExitRepair")}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
