import { t } from "../../../../i18n";
import { contextComplete } from "./resultTypes";
import type { MiningResultContext } from "./resultTypes";
import { navigate } from "../../../../utils/navigate";

/**
 * C1：结果页 → 因子模型页跳转按钮（4 上下文）。
 *
 * - 复用既有 `settings:navigate` 机制（`utils/navigate` 派发 detail=factor-center）；
 * - **缺任一上下文（factor_set_id/data_cutoff_at/candidate_pool_snapshot_id/
 *   rebalance_frequency）→ 禁用**（设计 pitfalls：跳转必须携带 4 项，缺项禁止）。
 */
export interface ResultGotoFactorModelButtonProps {
  context?: MiningResultContext | null;
  /** 测试可注入覆盖默认 navigate（否则走 utils/navigate 真实派发） */
  onNavigate?: (ctx: MiningResultContext) => void;
}

export default function ResultGotoFactorModelButton({
  context = null,
  onNavigate,
}: ResultGotoFactorModelButtonProps) {
  const canGoto = contextComplete(context);
  return (
    <button
      type="button"
      className="mining-pool-btn"
      data-result-goto-model
      disabled={!canGoto}
      title={canGoto ? undefined : t("miningResultGotoDisabledHint")}
      onClick={() => {
        if (!canGoto || !context) return;
        if (onNavigate) {
          onNavigate(context);
        } else {
          navigate("/factors?next=pipeline");
        }
      }}
    >
      {t("miningResultGotoModel")}
    </button>
  );
}