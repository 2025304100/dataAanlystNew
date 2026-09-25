import { useEffect, useState } from "react";
import dayjs from "dayjs";
import { t } from "../../../../../i18n";
import { DatePicker, InputNumber, Select } from "antd";
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
 * 控件统一 antd（DatePicker / Select / InputNumber）。
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
  /** DEF-10：草稿回填入口——此前本组件只有 onChange（单向写），
   * 壳层 wizardConfig 更新（如载入草稿）无法反向同步到内部 state，
   * Step2 日期/频率/比例永远显示默认值。传入引用变化时同步一次。 */
  initial?: {
    start_date?: string;
    end_date?: string;
    rebalance_frequency?: MiningFrequency;
    target_horizon?: number;
    ratios?: SplitRatios;
  } | null;
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
  initial = null,
  onChange,
  onGoMirror,
}: MiningTimeTargetStepProps) {
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [frequency, setFrequency] = useState<MiningFrequency>("daily");
  const [horizon, setHorizon] = useState(5);
  const [targetType, setTargetType] = useState<MiningTargetType>("simple");
  const [ratios, setRatios] = useState<SplitRatios>({ train: 60, val: 20, test: 20 });

  // DEF-10：initial 引用变化（载入草稿）时同步内部受控态；同值回写无害。
  useEffect(() => {
    if (!initial) return;
    if (initial.start_date !== undefined) setStartDate(initial.start_date);
    if (initial.end_date !== undefined) setEndDate(initial.end_date);
    if (initial.rebalance_frequency !== undefined) setFrequency(initial.rebalance_frequency);
    if (initial.target_horizon !== undefined) setHorizon(initial.target_horizon);
    if (initial.ratios !== undefined) setRatios(initial.ratios);
  }, [initial]);

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
  // 后端降级响应（available=false / 缺 total_points）不透传给切分面板，避免展示 undefined
  const effectiveBudget =
    budget != null && typeof budget === "object" && "total_points" in budget
      ? budget
      : null;

  const startDayjs = startDate ? dayjs(startDate) : null;
  const endDayjs = endDate ? dayjs(endDate) : null;

  return (
    <div className="mining-time-target-step" data-time-target-step>
      <div className="mining-time-grid">
        <label>
          <span>{t("miningTimeStart")}</span>
          <DatePicker
            data-time-start
            value={startDayjs}
            format="YYYY-MM-DD"
            allowClear
            onChange={(d) => {
              const v = d ? d.format("YYYY-MM-DD") : "";
              setStartDate(v);
              emit({ start_date: v });
            }}
          />
        </label>
        <label>
          <span>{t("miningTimeEnd")}</span>
          <DatePicker
            data-time-end
            value={endDayjs}
            format="YYYY-MM-DD"
            allowClear
            disabledDate={(d) => (dataCutoffAt ? d.isAfter(dayjs(dataCutoffAt), "day") : false)}
            onChange={(d) => {
              const v = d ? d.format("YYYY-MM-DD") : "";
              setEndDate(v);
              emit({ end_date: v });
            }}
          />
          <span className="mining-time-hint">
            {t("miningPoolBoardAsOf")}:{" "}
            {String(dataCutoffAt ?? "").split("T")[0] || "-"}
          </span>
        </label>
        <label>
          <span>{t("miningTimeFreq")}</span>
          <Select
            data-time-frequency
            value={frequency}
            options={[
              { value: "daily", label: t("miningFreqDaily") },
              { value: "weekly", label: t("miningFreqWeekly") },
              { value: "monthly", label: t("miningFreqMonthly") },
            ]}
            onChange={(v: MiningFrequency) => {
              setFrequency(v);
              emit({ rebalance_frequency: v });
            }}
          />
          <span className="mining-time-hint">{t("miningTimeHintFreq")}</span>
        </label>
        <label>
          <span>{t("miningTimeHorizon")}</span>
          <InputNumber
            data-time-horizon
            min={1}
            max={20}
            value={horizon}
            onChange={(v) => {
              const next = v == null || Number.isNaN(v) ? 5 : v;
              setHorizon(next);
              emit({ target_horizon: next });
            }}
          />
          <span className="mining-time-hint">{t("miningTimeHintHorizon")}</span>
        </label>
        <label>
          <span>{t("miningTimeTarget")}</span>
          <Select
            data-time-target-type
            value={targetType}
            options={[
              { value: "simple", label: t("miningTargetSimple") },
              { value: "log", label: t("miningTargetLog") },
              { value: "excess", label: t("miningTargetExcess") },
              { value: "rank", label: t("miningTargetRank") },
            ]}
            onChange={(v: MiningTargetType) => {
              setTargetType(v);
              emit({ target_type: v });
            }}
          />
          <span className="mining-time-hint">{t("miningTimeHintTarget")}</span>
        </label>
      </div>

      {/* 最低可运行 / 建议样本量对比（§4：生产建议高于硬门槛，界面必须显示差异） */}
      <div className="mining-time-floor" data-time-floor>
        <span className="mining-chip mining-chip--warn">
          {t("miningTimeFloorMinimum")}: {floor}
        </span>
        <span className="mining-chip mining-chip--brand">
          {t("miningTimeFloorRecommended")}: {recommended}
        </span>
        <span className="mining-hint">
          {t("miningTimeFloorCompare")} · {t("miningTimeFreqNote")}
        </span>
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
        budget={effectiveBudget}
        ratios={ratios}
        onChange={(next) => {
          setRatios(next);
          emit({ ratios: next });
        }}
      />
    </div>
  );
}