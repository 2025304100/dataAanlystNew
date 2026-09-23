import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { t } from "../../../../../i18n";
import { Checkbox } from "antd";
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
 * - 字段按分组勾选；**不可用的字段不可勾选**（`blocked_reason` 或
 *   `data_mode=blocked`），系统不自动剔除/降级/填零；
 * - 发起校验后 **5s 轮询**进度（not_do：不引入 WebSocket），到达终态停止；
 * - 阻断处理只给两个出口（重新选择字段 / 去修复数据），不提供逐字段自动修复。
 *
 * 字段列表由 `fields` 注入：P0-2 起 MiningShell 挂载本步时调候选池
 * `filter-fields` 映射注入（真实目录）；组标题优先 `category_label_zh`。
 *
 * 界面上每行是「字段卡」：中文名 + 代码 + 来源表 / 数据模式 / 最新日期 /
 * 可用区间 / 覆盖率进度条，缺项一律显示 "-"（不臆造）。
 * ⚠️ `data-field-*` 系列是测试钩子，不得改动。
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

/**
 * Step3 只列「可进入挖掘公式」的字段（设计 §5：行情/估值/财报/资金流/事件）。
 *
 * 候选池的**筛选条件类**字段不属于公式输入，必须排除：
 * - 设计 §3.2 第 7 类明确：行业「不得用于挖掘期逐日 PIT 筛选、行业中性化或行业横截面填充」；
 *   市场与交易状态/风险标记/上市时间同理，只在 Step1 用于选池。
 * - 工程上：这些字段的物理表是 **MySQL 主数据**（如 `universe_symbols`），
 *   不在 DuckDB 因子仓库里；字段校验按因子仓库取元数据 → 直接抛
 *   `CatalogException: Table with name universe_symbols does not exist`。
 *
 * ⚠️ 该集合是「界面按设计收口」的边界；若后续决定让这些字段也参与校验，
 * 需先让后端校验器能跨库定位物理表（见 .workbuddy/memory/2026-09-21.md 待办）。
 */
const NON_FORMULA_GROUPS = new Set(["universe", "market_status", "risk", "industry", "listing"]);

/** 数据模式 → 展示标签（未知模式原样展示） */
const MODE_LABEL: Record<string, string> = {
  continuous: "miningFieldModeContinuous",
  PIT: "miningFieldModePit",
  pit: "miningFieldModePit",
  event: "miningFieldModeEvent",
  snapshot: "miningFieldModeSnapshot",
  blocked: "miningFieldModeBlocked",
};

/** 覆盖率 < 80% 视为偏低（与看板口径一致） */
const COVERAGE_LOW = 0.8;

function modeLabel(mode: string | null | undefined): string {
  if (!mode) return "-";
  const key = MODE_LABEL[mode];
  return key ? t(key) : mode;
}

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
  /** 发起校验失败时的可读原因（**失败必须有回显**，不允许静默无响应） */
  const [error, setError] = useState<string | null>(null);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  /** 只保留可进入挖掘公式的字段（筛掉候选池筛选条件类，见 NON_FORMULA_GROUPS） */
  const formulaFields = useMemo(
    () => fields.filter((f) => !NON_FORMULA_GROUPS.has(String(f.group))),
    [fields],
  );
  const excludedCount = fields.length - formulaFields.length;

  // 分组顺序：已知组（GROUP_ORDER）在前，其余（估值/流动性/盈利质量等）按出现顺序跟随
  const groups: FieldGroup[] = [
    ...GROUP_ORDER.filter((g) => formulaFields.some((f) => f.group === g)),
    ...Array.from(new Set(formulaFields.map((f) => f.group))).filter(
      (g) => !GROUP_ORDER.includes(g),
    ),
  ];

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

  /**
   * 从 ApiError / 结构化 detail 中提取可读原因。
   *
   * 背景：本项目后端对业务错误用 `{error_code, detail_zh}`，而 FastAPI 入参校验
   * 失败（422）返回的是 `detail: [{loc, msg, type}]` 数组 —— 两种都要能读出来，
   * 否则用户只会看到"点了没反应"。
   */
  const describeError = (e: unknown): string => {
    const detail = (e as { detail?: unknown })?.detail;
    if (detail && typeof detail === "object" && !Array.isArray(detail)) {
      const rec = detail as { detail_zh?: string; user_message?: string };
      if (rec.detail_zh) return rec.detail_zh;
      if (rec.user_message) return rec.user_message;
    }
    if (Array.isArray(detail)) {
      const parts = detail.slice(0, 3).map((item) => {
        const rec = item as { loc?: unknown[]; msg?: string };
        const loc = Array.isArray(rec?.loc)
          ? rec.loc.filter((x) => x !== "body").join(".")
          : "";
        return loc ? `${loc}: ${rec?.msg ?? ""}` : String(rec?.msg ?? "");
      });
      const merged = parts.filter(Boolean).join("；");
      if (merged) return merged;
    }
    const msg = (e as { message?: string })?.message;
    return msg && msg.trim() ? msg : t("miningFieldStartFailedUnknown");
  };

  const startValidation = async () => {
    if (current.length === 0) return;
    setStarting(true);
    setError(null);
    try {
      const created = await createValidation({ selected_fields: current });
      const taskId = String(created?.task_id ?? "");
      if (!taskId) throw new Error(t("miningFieldStartFailedNoTask"));
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
        } catch (e) {
          stopPolling();
          setError(describeError(e));
        }
      }, POLL_INTERVAL_MS);
    } catch (e) {
      stopPolling();
      setError(describeError(e));
    } finally {
      setStarting(false);
    }
  };

  return (
    <div className="mining-field-step" data-field-step>
      <div className="mining-field-head">
        <span>{t("miningFieldSelected")}</span>
        <span data-field-selected-count>{current.length}</span>
        <span className="mining-card-sub">
          {t("miningFieldSummary")
            .replace("{total}", String(formulaFields.length))
            .replace("{selected}", String(current.length))}
        </span>
      </div>

      {excludedCount > 0 && (
        <p className="mining-hint" data-field-non-formula-note>
          {t("miningFieldNonFormulaNote").replace("{n}", String(excludedCount))}
        </p>
      )}

      {/* 校验前说明：覆盖率/最新日期/可用区间要等校验跑完才有（接口不提供） */}
      {(!status || !status.field_meta || Object.keys(status.field_meta).length === 0) && (
        <p className="mining-hint" data-field-meta-pending-note>
          {t("miningFieldMetaPending")}
        </p>
      )}

      {formulaFields.length === 0 && (
        <p className="mining-field-empty" data-field-empty role="status">
          {t("miningFieldEmpty")}
        </p>
      )}

      {groups.map((group) => {
        const items = formulaFields.filter((f) => f.group === group);
        if (items.length === 0) return null;
        const groupSelected = items.filter((f) => current.includes(f.code)).length;
        return (
          <div key={group} className="mining-field-group" data-field-group={group}>
            <div className="mining-field-group-title">
              <span>{items[0]?.category_label_zh ?? t(GROUP_LABEL[group] ?? group)}</span>
              <span className="mining-chip mining-chip--brand">
                {t("miningFieldGroupSelected").replace("{n}", String(groupSelected))}
              </span>
            </div>
            {items.map((f) => {
              const disabled = Boolean(f.blocked_reason) || f.data_mode === "blocked";
              const checked = current.includes(f.code);
              /**
               * 实测元数据只来自校验报告（`status.field_meta`）：
               * 字段目录接口 `filter-fields` 不含覆盖率/最新日期/可用区间，
               * 所以校验前这些位置显示「校验后显示」，跑完校验后回填真值。
               */
              const meta = status?.field_meta?.[f.code] ?? null;
              // 优先校验实测值；若后端将来在字段目录里直接给 coverage，则回退使用
              const cov = meta?.coverage ?? f.coverage ?? null;
              const covPct = cov != null ? cov * 100 : null;
              const covLow = cov != null && cov < COVERAGE_LOW;
              const latestDate = meta?.max_date ?? f.latest_date ?? null;
              const rangeFrom = meta?.min_date ?? f.available_from ?? null;
              const rangeTo = meta?.max_date ?? f.available_to ?? null;
              return (
                <div
                  key={f.code}
                  className={`mining-field-row ${checked ? "is-selected" : ""} ${
                    disabled ? "is-blocked" : ""
                  }`}
                  data-field-row={f.code}
                >
                  <Checkbox
                    data-field-checkbox={f.code}
                    disabled={disabled}
                    checked={checked}
                    onChange={() => toggle(f.code)}
                  >
                    <span className="mining-field-name">
                      <span>{f.name_zh}</span>
                      <code className="mining-field-code">{f.code}</code>
                    </span>
                  </Checkbox>

                  {/* 元数据：来源表 / 数据模式 / 最新日期 / 可用区间 / 覆盖率 */}
                  <div className="mining-field-meta">
                    {f.source_table ? (
                      <span className="mining-field-meta-item">
                        <b>{t("miningFieldMetaSource")}</b>
                        <code>{f.source_table}</code>
                      </span>
                    ) : null}
                    <span className="mining-field-meta-item">
                      <b>{t("miningFieldMetaMode")}</b>
                      <span>{modeLabel(f.data_mode)}</span>
                    </span>
                    {latestDate ? (
                      <span className="mining-field-meta-item">
                        <b>{t("miningFieldMetaLatest")}</b>
                        <span data-field-latest={f.code}>{latestDate}</span>
                      </span>
                    ) : null}
                    {rangeFrom || rangeTo ? (
                      <span className="mining-field-meta-item">
                        <b>{t("miningFieldMetaRange")}</b>
                        <span data-field-range={f.code}>
                          {rangeFrom ?? "-"} ~ {rangeTo ?? "-"}
                        </span>
                      </span>
                    ) : null}
                    <span className="mining-field-meta-item mining-field-cov">
                      <b>{t("miningFieldMetaCoverage")}</b>
                      {covPct != null ? (
                        <>
                          <span className="mining-meter mining-meter--sm mining-field-cov-bar">
                            <span
                              className={`mining-meter-fill ${
                                covLow ? "mining-meter-fill--danger" : "mining-meter-fill--success"
                              }`}
                              style={{ width: `${Math.max(0, Math.min(100, covPct))}%` }}
                            />
                          </span>
                          <span
                            className={`mining-field-cov-pct ${covLow ? "is-low" : ""}`}
                            data-field-coverage={f.code}
                          >
                            {covPct.toFixed(1)}%
                          </span>
                        </>
                      ) : (
                        <span className="mining-chip mining-chip--muted" data-field-coverage-pending={f.code}>
                          {t("miningFieldMetaAfterValidation")}
                        </span>
                      )}
                    </span>
                  </div>

                  {f.blocked_reason ? (
                    // 正文只用用户句；表名/实测口径走 title 悬浮（不污染界面）
                    <span
                      className="mining-field-blocked-tag"
                      data-field-blocked-reason
                      title={f.blocked_detail ?? undefined}
                    >
                      <b>{t("miningFieldMetaBlocked")}</b>
                      {f.blocked_reason}
                    </span>
                  ) : null}
                </div>
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

      {/* 发起失败必须有回显（此前无 catch → 422 时界面毫无反应） */}
      {error && (
        <div
          className="mining-banner mining-banner--danger"
          data-field-validate-error
          role="alert"
        >
          <div className="mining-banner-body">
            <span className="mining-banner-title">{t("miningFieldStartFailed")}</span>
            <span>{error}</span>
          </div>
        </div>
      )}

      <FieldValidationPanel
        status={status}
        onReselectFields={onReselectFields}
        onRepairData={onRepairData}
      />
    </div>
  );
}
