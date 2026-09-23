import { useState } from "react";
import { t } from "../../../../../i18n";
import { DatePicker, InputNumber } from "antd";
import type { SplitBudget, SplitMode, SplitRatios } from "./splitTypes";

/**
 * 三段切分面板（向导 §4，设计 §7.2）—— **只收集配置 + 原样展示预算**。
 *
 * **not_do：不在前端计算分位数或切分** —— 段边界、各段点数、purge/embargo
 * 全部由后端 `compute_split_budget()` 算出后经 `budget` 下发；前端改比例只
 * 上抛 `onChange`（由后端重算），**不本地推算边界**（有测试断言守着）。
 *
 * pitfalls 落点：
 * - purge / embargo **同时展示「调仓点数」与「折算交易日数」** —— 否则用户会把
 *   `purge=5` 误读成 5 个交易日；
 * - 月频（`statistically_degraded`）→ 显示「最高 B 级」降级提示；
 * - 未达最低样本量（`meets_floor=false`）→ 显示门槛值与调整方向。
 */
export interface MiningSplitPanelProps {
  budget?: SplitBudget | null;
  ratios?: SplitRatios;
  onChange?: (ratios: SplitRatios) => void;
}

const DEFAULT_RATIOS: SplitRatios = { train: 60, val: 20, test: 20 };

const SEG_COLOR_CLASS: Record<"train" | "val" | "test", string> = {
  train: "mining-split-bar-seg--train",
  val: "mining-split-bar-seg--val",
  test: "mining-split-bar-seg--test",
};

export default function MiningSplitPanel({
  budget = null,
  ratios: external,
  onChange,
}: MiningSplitPanelProps) {
  const [ratios, setRatios] = useState<SplitRatios>(external ?? DEFAULT_RATIOS);
  const [mode, setMode] = useState<SplitMode>("ratio");

  const current = external ?? ratios;
  const sum = current.train + current.val + current.test;
  const invalid = sum !== 100;
  const totalForWidth = sum > 0 ? sum : 1;

  const update = (key: keyof SplitRatios, value: string) => {
    const parsed = Number.parseInt(value, 10);
    const next = { ...current, [key]: Number.isNaN(parsed) ? 0 : parsed };
    setRatios(next);
    onChange?.(next);
  };

  /** 各段边界只取自后端 budget（缺失时留空，不臆造） */
  const segRange: Record<"train" | "val" | "test", string> = {
    train:
      budget?.train_start || budget?.train_end
        ? `${budget.train_start ?? "?"} ~ ${budget.train_end ?? "?"}`
        : "",
    val: budget?.val_end ? `~ ${budget.val_end}` : "",
    test: budget?.val_end ? `${budget.val_end} ~` : "",
  };

  const segLabel: Record<"train" | "val" | "test", string> = {
    train: t("miningSplitTrain"),
    val: t("miningSplitVal"),
    test: t("miningSplitTest"),
  };

  return (
    <div className="mining-split-panel" data-split-panel>
      <div className="mining-split-head">
        <span className="mining-card-title mining-card-title--plain">
          {t("miningSplitTitle")}
        </span>
        <div className="mining-segmented">
          <button
            type="button"
            className={`sub-tab ${mode === "ratio" ? "active" : ""}`}
            data-split-mode-ratio
            onClick={() => setMode("ratio")}
          >
            {t("miningSplitModeRatio")}
          </button>
          <button
            type="button"
            className={`sub-tab ${mode === "custom" ? "active" : ""}`}
            data-split-mode-custom
            onClick={() => setMode("custom")}
          >
            {t("miningSplitModeCustom")}
          </button>
        </div>
      </div>

      {/* 三段比例可视化条（宽度取自当前比例；段内日期只来自后端 budget） */}
      <div className="mining-split-bar" data-split-bar aria-label={t("miningSplitBarTitle")}>
        {(["train", "val", "test"] as const).map((key) =>
          current[key] <= 0 ? null : (
            <div
              key={key}
              className={`mining-split-bar-seg ${SEG_COLOR_CLASS[key]}`}
              style={{ flex: `${current[key]} 0 0%` }}
              data-split-bar-seg={key}
              title={`${segLabel[key]} ${current[key]}%${segRange[key] ? ` · ${segRange[key]}` : ""}`}
            >
              <span>
                {segLabel[key]} · {Math.round((current[key] / totalForWidth) * 100)}%
              </span>
              {segRange[key] ? (
                <span className="mining-split-bar-seg-date">{segRange[key]}</span>
              ) : null}
            </div>
          ),
        )}
        {(["train", "val", "test"] as const).every((k) => current[k] <= 0) && (
          <div className="mining-split-bar-seg mining-split-bar-seg--empty">
            {t("miningSplitBarEmpty")}
          </div>
        )}
      </div>

      {mode === "ratio" ? (
        <div className="mining-split-ratios">
          {(["train", "val", "test"] as const).map((key) => (
            <label key={key}>
              <span>{segLabel[key]}</span>
              <InputNumber
                data-split-ratio={key}
                min={0}
                max={100}
                value={current[key]}
                style={{ width: 104 }}
                onChange={(v) => update(key, v == null ? "0" : String(v))}
              />
            </label>
          ))}
          <span
            className={`mining-split-sum ${invalid ? "is-invalid" : ""}`}
            data-split-sum
          >
            {t("miningSplitSum")}: {sum}%
          </span>
          {invalid && (
            <span className="mining-split-error" data-split-error role="alert">
              {t("miningSplitSumError").replace("{sum}", String(sum))}
            </span>
          )}
        </div>
      ) : (
        <div className="mining-split-custom">
          <label>
            <span>{t("miningSplitCustomTrainEnd")}</span>
            <DatePicker
              data-split-custom-train-end
              format="YYYY-MM-DD"
              allowClear
            />
          </label>
          <label>
            <span>{t("miningSplitCustomValEnd")}</span>
            <DatePicker
              data-split-custom-val-end
              format="YYYY-MM-DD"
              allowClear
            />
          </label>
        </div>
      )}

      {!budget && (
        <p className="mining-split-nobudget">{t("miningSplitNoBudget")}</p>
      )}

      {budget && (
        <div className="mining-split-budget" data-split-budget>
          <div>
            {t("miningSplitTotalPoints")}
            <strong>{budget.total_points}</strong>
          </div>
          <div>
            {t("miningSplitTrain")}
            <strong>{budget.train_points}</strong>
          </div>
          <div>
            {t("miningSplitVal")}
            <strong>{budget.val_points}</strong>
          </div>
          <div>
            {t("miningSplitTest")}
            <strong>{budget.test_points}</strong>
          </div>
          <div>
            {t("miningSplitFloor")}
            <strong>
              {budget.frequency_floor} {t("miningSplitPointsUnit")}
            </strong>
          </div>
          <div>
            {t("miningSplitTailLoss")}
            <strong>
              {budget.tail_loss} {t("miningSplitPointsUnit")}
            </strong>
          </div>

          {/* purge / embargo：点数与交易日数**同时**展示（防误读） */}
          <div data-split-purge-item>
            {t("miningSplitPurgePoints")}
            <strong>
              <span data-split-purge-points>{budget.purge_points}</span>{" "}
              {t("miningSplitPointsUnit")}
            </strong>
            <span>
              {t("miningSplitPurgeDays")}:{" "}
              <span data-split-purge-days>{budget.purge_trading_days}</span>{" "}
              {t("miningSplitDaysUnit")}
            </span>
          </div>
          <div data-split-embargo-item>
            {t("miningSplitEmbargoPoints")}
            <strong>
              <span data-split-embargo-points>{budget.embargo_points}</span>{" "}
              {t("miningSplitPointsUnit")}
            </strong>
            <span>
              {t("miningSplitEmbargoDays")}:{" "}
              <span data-split-embargo-days>{budget.embargo_trading_days}</span>{" "}
              {t("miningSplitDaysUnit")}
            </span>
          </div>

          {budget.train_start || budget.train_end ? (
            <div data-split-boundary>
              {t("miningSplitBoundary")}: {budget.train_start ?? "-"} ~{" "}
              {budget.train_end ?? "-"} / {budget.val_end ?? "-"}
            </div>
          ) : null}

          {budget.statistically_degraded && (
            <div className="mining-split-degraded" data-split-degraded role="status">
              {t("miningSplitDegraded")}
            </div>
          )}

          {!budget.meets_floor && (
            <div className="mining-split-floor-fail" data-split-floor-fail role="alert">
              {t("miningSplitFloorFail").replace(
                "{floor}", String(budget.frequency_floor),
              )}
            </div>
          )}
        </div>
      )}

      {budget && (
        <p className="mining-hint">{t("miningSplitPurgeNote")}</p>
      )}
    </div>
  );
}
