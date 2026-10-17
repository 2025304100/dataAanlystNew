import { t } from "../../../../i18n";
import type { MiningStepKey } from "../../../../types/mining";

/**
 * 步骤引导横幅（强引导，2026-10-14）—— 每一步顶部常驻：
 *   「第 N 步 / 共 5 步」+ 步骤名 + 这步要做什么 + 完成标志。
 *
 * 设计意图（业界 setup wizard 通行做法）：
 * - 用户任何时刻都知道「自己在哪、要做什么、怎样算完成」；
 * - 完成标志与底部「下一步」的解锁条件逐字对应，因果可见；
 * - 只读态（查看历史任务）隐藏「完成标志」，只留说明，不误导。
 */

interface StepMeta {
  descKey: string;
  reqKey: string;
}

const STEP_META: Record<MiningStepKey, StepMeta> = {
  pool: { descKey: "miningGuidePoolDesc", reqKey: "miningGuidePoolReq" },
  "time-target": { descKey: "miningGuideTimeDesc", reqKey: "miningGuideTimeReq" },
  field: { descKey: "miningGuideFieldDesc", reqKey: "miningGuideFieldReq" },
  evolution: { descKey: "miningGuideEvoDesc", reqKey: "miningGuideEvoReq" },
  run: { descKey: "miningGuideRunDesc", reqKey: "miningGuideRunReq" },
};

export interface WizardStepGuideProps {
  stepKey: MiningStepKey;
  /** 0 基步骤下标（展示为 1 基） */
  stepIndex: number;
  total: number;
  /** 本步是否已完成（壳层 stepDone 推导，不臆造） */
  done: boolean;
  /** 步骤名（已走 t() 的标签，由壳层注入，避免重复翻译源） */
  title: string;
  /** 只读态不展示「完成标志」 */
  showReq?: boolean;
}

export default function WizardStepGuide({
  stepKey,
  stepIndex,
  total,
  done,
  title,
  showReq = true,
}: WizardStepGuideProps) {
  const meta = STEP_META[stepKey];
  return (
    <div
      className={`mining-step-guide${done ? " is-done" : ""}`}
      data-mining-step-guide={stepKey}
      data-mining-step-guide-done={done ? "true" : undefined}
    >
      <div className="mining-step-guide-head">
        <span className="mining-step-guide-no">
          {t("miningGuideNo")
            .replace("{n}", String(stepIndex + 1))
            .replace("{total}", String(total))}
        </span>
        <span className="mining-step-guide-title">{title}</span>
        {done && <span className="mining-chip mining-chip--success">{t("miningGuideDone")}</span>}
      </div>
      <p className="mining-step-guide-desc">{t(meta.descKey)}</p>
      {showReq && <p className="mining-step-guide-req">{t(meta.reqKey)}</p>}
    </div>
  );
}
