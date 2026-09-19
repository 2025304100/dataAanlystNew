import { t } from "../../../../../i18n";
import type { MiningFrequency } from "./splitTypes";

/**
 * 已镜像范围提示 / 月频 10 年引导 / 幸存者偏差告警（向导 §4.1）。
 *
 | 条件 | 提示样式 |
 |------|---------|
 | 起点 < 2021-01-01 | 黄色告警：幸存者偏差风险 |
 | 涉及 2016–2020 | 灰色提示：低覆盖年份，默认不参与 S/A 级评定 |
 | 月频 + 未镜像 10 年 | 蓝色引导：test 段不足，建议镜像 10 年 + 「去镜像」 |
 | 超出已镜像范围 | 阻断：请先镜像或调整区间 |
 */
export interface MirrorGuideBannerProps {
  startDate?: string | null;
  endDate?: string | null;
  frequency?: MiningFrequency | null;
  mirroredFrom?: string | null;
  mirroredTo?: string | null;
  mirroredYears?: number;
  onGoMirror?: () => void;
}

const EARLY_COVERAGE_START = "2016-01-01";
const EARLY_COVERAGE_END = "2020-12-31";
const SURVIVORSHIP_BEFORE = "2021-01-01";
const MONTHLY_MIRROR_YEARS = 10;

export default function MirrorGuideBanner({
  startDate, endDate, frequency, mirroredFrom, mirroredTo,
  mirroredYears = 5, onGoMirror,
}: MirrorGuideBannerProps) {
  const outOfRange = Boolean(
    (mirroredFrom && startDate && startDate < mirroredFrom) ||
    (mirroredTo && endDate && endDate > mirroredTo),
  );
  const survivorship = Boolean(startDate && startDate < SURVIVORSHIP_BEFORE);
  const lowCoverage = Boolean(
    startDate && endDate &&
    startDate <= EARLY_COVERAGE_END && endDate >= EARLY_COVERAGE_START,
  );
  const monthlyNeedsMirror =
    frequency === "monthly" && mirroredYears < MONTHLY_MIRROR_YEARS;

  const rangeText = `${mirroredFrom ?? "?"} ~ ${mirroredTo ?? "?"}`;

  return (
    <div className="mining-mirror-guide">
      {outOfRange && (
        <div className="mining-mirror-block" data-mirror-out-of-range role="alert">
          {t("miningMirrorOutOfRange").replace("{range}", rangeText)}
          {onGoMirror && (
            <button type="button" onClick={onGoMirror}>{t("miningMirrorGo")}</button>
          )}
        </div>
      )}
      {survivorship && (
        <div className="mining-mirror-warn" data-mirror-survivorship role="status">
          {t("miningMirrorSurvivorship")}
        </div>
      )}
      {lowCoverage && (
        <div className="mining-mirror-low" data-mirror-low-coverage>
          {t("miningMirrorLowCoverage")}
        </div>
      )}
      {monthlyNeedsMirror && (
        <div className="mining-mirror-guide-tip" data-mirror-monthly-guide role="status">
          {t("miningMirrorMonthlyGuide")}
          {onGoMirror && (
            <button type="button" onClick={onGoMirror}>{t("miningMirrorGo")}</button>
          )}
        </div>
      )}
    </div>
  );
}
