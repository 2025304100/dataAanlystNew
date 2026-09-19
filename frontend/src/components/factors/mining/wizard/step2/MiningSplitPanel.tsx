import { useState } from "react";
import { t } from "../../../../../i18n";
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

  const update = (key: keyof SplitRatios, value: string) => {
    const parsed = Number.parseInt(value, 10);
    const next = { ...current, [key]: Number.isNaN(parsed) ? 0 : parsed };
    setRatios(next);
    onChange?.(next);
  };

  return (
    <div className="mining-split-panel" data-split-panel>
      <div className="mining-split-head">
        <span>{t("miningSplitTitle")}</span>
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

      {mode === "ratio" ? (
        <div className="mining-split-ratios">
          {(["train", "val", "test"] as const).map((key) => (
            <label key={key}>
              <span>
                {key === "train"
                  ? t("miningSplitTrain")
                  : key === "val"
                    ? t("miningSplitVal")
                    : t("miningSplitTest")}
              </span>
              <input
                type="number"
                data-split-ratio={key}
                value={current[key]}
                onChange={(e) => update(key, e.target.value)}
              />
            </label>
          ))}
          <span data-split-sum>
            {t("miningSplitSum")}: {sum}
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
            <input type="date" data-split-custom-train-end />
          </label>
          <label>
            <span>{t("miningSplitCustomValEnd")}</span>
            <input type="date" data-split-custom-val-end />
          </label>
        </div>
      )}

      {!budget && (
        <p className="mining-split-nobudget">{t("miningSplitNoBudget")}</p>
      )}

      {budget && (
        <div className="mining-split-budget" data-split-budget>
          <div>
            {t("miningSplitTotalPoints")}: {budget.total_points}
          </div>
          <div>
            {t("miningSplitTrain")}: {budget.train_points} /{" "}
            {t("miningSplitVal")}: {budget.val_points} /{" "}
            {t("miningSplitTest")}: {budget.test_points}
          </div>
          <div>
            {t("miningSplitFloor")}: {budget.frequency_floor}
          </div>

          {/* purge / embargo：点数与交易日数**同时**展示（防误读） */}
          <div>
            {t("miningSplitPurgePoints")}:{" "}
            <span data-split-purge-points>{budget.purge_points}</span> （
            {t("miningSplitPurgeDays")}:{" "}
            <span data-split-purge-days>{budget.purge_trading_days}</span>）
          </div>
          <div>
            {t("miningSplitEmbargoPoints")}:{" "}
            <span data-split-embargo-points>{budget.embargo_points}</span> （
            {t("miningSplitEmbargoDays")}:{" "}
            <span data-split-embargo-days>{budget.embargo_trading_days}</span>）
          </div>
          <div>
            {t("miningSplitTailLoss")}: {budget.tail_loss}
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
    </div>
  );
}
