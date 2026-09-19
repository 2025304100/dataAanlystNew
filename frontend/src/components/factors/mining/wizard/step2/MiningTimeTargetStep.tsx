import { useState } from "react";
import { t } from "../../../../../i18n";
import MirrorGuideBanner from "./MirrorGuideBanner";
import MiningSplitPanel from "./MiningSplitPanel";
import {
  FREQUENCY_FLOOR,
  FREQUENCY_RECOMMENDED,
} from "./splitTypes";
import type {
  MiningFrequency,
  MiningTargetType,
  SplitBudget,
  SplitRatios,
} from "./splitTypes";

/**
 * Step2 时间与目标（向导 §4）—— 编排页：
 * 起止日期 / 调仓频率 / 预测持有期 / 预测目标 + 三段切分面板 + 镜像引导。
 *
 * 口径：
 * - 结束日不得晚于数据截止日（由父级/后端校验，此处只收集）；
 * - 最小样本量硬门槛 日 252 / 周 104 / 月 36，**生产建议** 周 156 / 月 60
 *   （界面同时展示「最低可运行 / 建议样本量」差异）；
 * - 切分与分位数**一律后端计算**（not_do），本页只下发配置、展示后端预算。
 */
export interface MiningTimeTargetStepProps {
  budget?: SplitBudget | null;
  dataCutoffAt?: string | null;
  mirroredFrom?: string | null;
  mirroredTo?: string | null;
  mirroredYears?: number;
  onChange?: (config: {
    start_date: string;
    end_date: string;
    rebalance_frequency: MiningFrequency;
    target_horizon: number;
    target_type: MiningTargetType;
    ratios: SplitRatios;
  }) => void;
  onGoMirror?: () => void;
}

export default function MiningTimeTargetStep({
  budget = null,
  dataCutoffAt = null,
  mirroredFrom = null,
  mirroredTo = null,
  mirroredYears = 5,
  onChange,
  onGoMirror,
}: MiningTimeTargetStepProps) {
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [frequency, setFrequency] = useState<MiningFrequency>("daily");
  const [horizon, setHorizon] = useState(5);
  const [targetType, setTargetType] = useState<MiningTargetType>("simple");
  const [ratios, setRatios] = useState<SplitRatios>({ train: 60, val: 20, test: 20 });

  const emit = (patch: Partial<{
    start_date: string; end_date: string; rebalance_frequency: MiningFrequency;
    target_horizon: number; target_type: MiningTargetType; ratios: SplitRatios;
  }> = {}) => {
    onChange?.({
      start_date: patch.start_date ?? startDate,
      end_date: patch.end_date ?? endDate,
      rebalance_frequency: patch.rebalance_frequency ?? frequency,
      target_horizon: patch.target_horizon ?? horizon,
      target_type: patch.target_type ?? targetType,
      ratios: patch.ratios ?? ratios,
    });
  };

  const floor = FREQUENCY_FLOOR[frequency];
  const recommended = FREQUENCY_RECOMMENDED[frequency];

  return (
    <div className="mining-time-target-step" data-time-target-step>
      <div className="mining-time-grid">
        <label>
          <span>{t("miningTimeStart")}</span>
          <input
            type="date"
            data-time-start
            value={startDate}
            onChange={(e) => {
              setStartDate(e.target.value);
              emit({ start_date: e.target.value });
            }}
          />
        </label>
        <label>
          <span>{t("miningTimeEnd")}</span>
          <input
            type="date"
            data-time-end
            max={dataCutoffAt ?? undefined}
            value={endDate}
            onChange={(e) => {
              setEndDate(e.target.value);
              emit({ end_date: e.target.value });
            }}
          />
        </label>
        <label>
          <span>{t("miningTimeFreq")}</span>
          <select
            data-time-frequency
            value={frequency}
            onChange={(e) => {
              const v = e.target.value as MiningFrequency;
              setFrequency(v);
              emit({ rebalance_frequency: v });
            }}
          >
            <option value="daily">{t("miningFreqDaily")}</option>
            <option value="weekly">{t("miningFreqWeekly")}</option>
            <option value="monthly">{t("miningFreqMonthly")}</option>
          </select>
        </label>
        <label>
          <span>{t("miningTimeHorizon")}</span>
          <input
            type="number"
            data-time-horizon
            min={1}
            max={20}
            value={horizon}
            onChange={(e) => {
              const v = Number.parseInt(e.target.value, 10);
              const next = Number.isNaN(v) ? 5 : v;
              setHorizon(next);
              emit({ target_horizon: next });
            }}
          />
        </label>
        <label>
          <span>{t("miningTimeTarget")}</span>
          <select
            data-time-target-type
            value={targetType}
            onChange={(e) => {
              const v = e.target.value as MiningTargetType;
              setTargetType(v);
              emit({ target_type: v });
            }}
          >
            <option value="simple">{t("miningTargetSimple")}</option>
            <option value="log">{t("miningTargetLog")}</option>
            <option value="excess">{t("miningTargetExcess")}</option>
            <option value="rank">{t("miningTargetRank")}</option>
          </select>
        </label>
      </div>

      <div className="mining-time-floor" data-time-floor>
        {t("miningSplitFloor")}: {floor} / {t("miningSplitTotalPoints")}: {recommended}
      </div>

      <MirrorGuideBanner
        startDate={startDate}
        endDate={endDate}
        frequency={frequency}
        mirroredFrom={mirroredFrom}
        mirroredTo={mirroredTo}
        mirroredYears={mirroredYears}
        onGoMirror={onGoMirror}
      />

      <MiningSplitPanel
        budget={budget}
        ratios={ratios}
        onChange={(next) => {
          setRatios(next);
          emit({ ratios: next });
        }}
      />
    </div>
  );
}
