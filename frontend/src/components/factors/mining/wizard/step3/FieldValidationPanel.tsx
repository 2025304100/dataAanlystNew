import { t } from "../../../../../i18n";
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
      <span>{item.issue ?? "-"}</span>
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

  return (
    <div className="mining-field-validation">
      {status.progress ? (
        <div className="mining-field-progress" data-field-progress>
          <span>
            {t("miningFieldProgress")}:{" "}
            <span data-field-progress-done>{status.progress.done}</span> /{" "}
            <span data-field-progress-total>{status.progress.total}</span>
          </span>
        </div>
      ) : null}

      {status.passed && (
        <div className="mining-field-passed" data-field-passed role="status">
          {t("miningFieldPassed")}
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
          <div className="mining-field-blocked-head">{t("miningFieldBlocked")}</div>
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
