import { useCallback, useEffect, useRef, useState } from "react";
import { t } from "../../../../../i18n";
import FieldValidationPanel from "./FieldValidationPanel";
import { createValidation, getValidation } from "./fieldApi";
import {
  POLL_INTERVAL_MS,
  TERMINAL_STATUSES,
} from "./fieldTypes";
import type { FieldGroup, MiningField, ValidationStatus } from "./fieldTypes";

/**
 * Step3 字段与校验（向导 §5，设计 §9.2）。
 *
 * - 字段按分组（行情/估值/财报/资金流/事件）勾选；**不可用的字段不可勾选**
 *   （`blocked_reason` 或 `data_mode=blocked`），系统不自动剔除/降级/填零；
 * - 发起校验后 **5s 轮询**进度（not_do：不引入 WebSocket），到达终态停止；
 * - 阻断处理只给两个出口（重新选择字段 / 去修复数据），不提供逐字段自动修复。
 *
 * 字段列表由 `fields` 注入：**无字段目录接口时不臆造**（当前后端仅有
 * 候选池 `filter-fields`，挖掘字段目录接口待补）。
 */
export interface MiningFieldStepProps {
  fields?: MiningField[];
  selected?: string[];
  onChange?: (selected: string[]) => void;
  onReselectFields?: () => void;
  onRepairData?: () => void;
  onValidated?: (status: ValidationStatus) => void;
}

const GROUP_ORDER: FieldGroup[] = ["quote", "valuation", "financial", "flow", "event"];

const GROUP_LABEL: Record<FieldGroup, string> = {
  quote: "miningFieldGroupQuote",
  valuation: "miningFieldGroupValuation",
  financial: "miningFieldGroupFinancial",
  flow: "miningFieldGroupFlow",
  event: "miningFieldGroupEvent",
};

export default function MiningFieldStep({
  fields = [],
  selected,
  onChange,
  onReselectFields,
  onRepairData,
  onValidated,
}: MiningFieldStepProps) {
  const [internal, setInternal] = useState<string[]>([]);
  const current = selected ?? internal;
  const [status, setStatus] = useState<ValidationStatus | null>(null);
  const [starting, setStarting] = useState(false);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const stopPolling = useCallback(() => {
    if (timerRef.current) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  useEffect(() => stopPolling, [stopPolling]);

  const toggle = (code: string) => {
    const next = current.includes(code)
      ? current.filter((c) => c !== code)
      : [...current, code];
    setInternal(next);
    onChange?.(next);
  };

  const startValidation = async () => {
    if (current.length === 0) return;
    setStarting(true);
    try {
      const created = await createValidation({ selected_fields: current });
      const taskId = String(created?.task_id ?? "");
      const first = await getValidation(taskId);
      setStatus(first);
      onValidated?.(first);
      stopPolling();
      timerRef.current = setInterval(async () => {
        try {
          const next = await getValidation(taskId);
          setStatus(next);
          onValidated?.(next);
          if ((TERMINAL_STATUSES as readonly string[]).includes(next?.status)) {
            stopPolling();
          }
        } catch {
          stopPolling();
        }
      }, POLL_INTERVAL_MS);
    } finally {
      setStarting(false);
    }
  };

  return (
    <div className="mining-field-step" data-field-step>
      <div className="mining-field-head">
        <span>{t("miningFieldSelected")}: </span>
        <span data-field-selected-count>{current.length}</span>
      </div>

      {GROUP_ORDER.map((group) => {
        const items = fields.filter((f) => f.group === group);
        if (items.length === 0) return null;
        return (
          <div key={group} className="mining-field-group" data-field-group={group}>
            <div className="mining-field-group-title">{t(GROUP_LABEL[group])}</div>
            {items.map((f) => {
              const disabled = Boolean(f.blocked_reason) || f.data_mode === "blocked";
              return (
                <label key={f.code} className="mining-field-row">
                  <input
                    type="checkbox"
                    data-field-checkbox={f.code}
                    disabled={disabled}
                    checked={current.includes(f.code)}
                    onChange={() => toggle(f.code)}
                  />
                  <span>{f.name_zh}</span>
                  <span className="mining-field-code">{f.code}</span>
                  <span>{f.coverage != null ? `${(f.coverage * 100).toFixed(1)}%` : "-"}</span>
                  {f.blocked_reason ? (
                    <span className="mining-field-blocked-tag">{f.blocked_reason}</span>
                  ) : null}
                </label>
              );
            })}
          </div>
        );
      })}

      <button
        type="button"
        className="mining-pool-btn primary"
        data-field-start
        disabled={starting || current.length === 0}
        onClick={startValidation}
      >
        {starting ? t("miningFieldStarting") : t("miningFieldStart")}
      </button>

      <FieldValidationPanel
        status={status}
        onReselectFields={onReselectFields}
        onRepairData={onRepairData}
      />
    </div>
  );
}
