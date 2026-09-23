import { t } from "../../../../../i18n";
import { formatEtaMinutes } from "./evoTypes";
import type { MiningLockStatus } from "./evoTypes";

/**
 * 资源确认弹窗（向导 §7）—— 提交前的最后一道闸门。
 *
 * **锁状态三态文案**（任务卡 pitfalls 第一条）：
 * | 情况 | 文案 | 提交 |
 * |------|------|------|
 * | 无冲突 | 当前无其他写任务，可立即开始 | 可用 |
 * | `mining_domain` 冲突 | 已有挖掘任务进行中（ID: xxx，第 N 代） | **禁用**（不排队） |
 * | `duckdb_write` 冲突 | 当前 XX 任务正在执行，已为你排队，前面还有 N 个 | 可用（排队） |
 *
 * **ETA 必须标注「估算」**（pitfalls 第二条 + §7 实现要点）。
 * **不暴露并行度**（not_do）：弹窗内不得出现并行/进程/worker 等字样。
 * 资源不足（磁盘 <20% 或内存 <2GB）→ **阻断**并给出调整建议。
 */
export interface ResourceConfirmModalProps {
  lockStatus?: MiningLockStatus | null;
  etaSeconds?: number | null;
  resourcesOk?: boolean;
  /** E2：磁盘剩余百分比（≥20% 达标）或缺省 null */
  diskFreePercent?: number | null;
  /** E2：可用内存 GB（≥2 达标）或缺省 null */
  memoryFreeGb?: number | null;
  onConfirm?: () => void;
  onClose?: () => void;
}

export default function ResourceConfirmModal({
  lockStatus = null,
  etaSeconds = null,
  resourcesOk = true,
  diskFreePercent = null,
  memoryFreeGb = null,
  onConfirm,
  onClose,
}: ResourceConfirmModalProps) {
  const domainBusy = Boolean(lockStatus?.miningDomain?.busy);
  const writeBusy = Boolean(lockStatus?.duckdbWrite?.busy);
  // P1-8：后端返回排队任务 id 列表，前面排队数 = queue.length（1 表示队首在等）
  const queuePosition = lockStatus?.duckdbWrite?.queue?.length ?? 0;
  const etaMinutes = formatEtaMinutes(etaSeconds);
  // E3：磁盘/内存不达标 → 资源不足阻断（与 mining_domain 冲突并列）
  const diskOk = diskFreePercent == null || diskFreePercent >= 20;
  const memOk = memoryFreeGb == null || memoryFreeGb >= 2;
  const resourceInsufficient = !resourcesOk || !diskOk || !memOk;
  // mining_domain 冲突 → 禁用提交（不排队）；资源不足同样阻断
  const blocked = domainBusy || resourceInsufficient;

  return (
    <div className="mining-resource-mask" role="dialog" aria-modal="true">
      <div className="mining-resource-modal" data-resource-modal>
        <header>
          <h3>{t("miningResTitle")}</h3>
        </header>

        {!domainBusy && !writeBusy && (
          <div data-lock-none>{t("miningResLockNone")}</div>
        )}

        {domainBusy && (
          <div data-lock-domain className="mining-res-domain">
            {t("miningResLockDomain")
              .replace("{task_id}", String(lockStatus?.miningDomain?.taskId ?? "-"))}
          </div>
        )}

        {writeBusy && !domainBusy && (
          <div data-lock-write className="mining-res-write">
            {t("miningResLockWrite").replace(
              "{task_id}", String(lockStatus?.duckdbWrite?.taskId ?? "-"),
            )}
            {queuePosition > 0 && (
              <span data-queue-position>
                {t("miningResQueue").replace("{position}", String(queuePosition))}
              </span>
            )}
          </div>
        )}

        {etaMinutes != null && (
          <div data-eta>
            {t("miningResEta").replace("{minutes}", String(etaMinutes))}
            <span className="mining-res-eta-note">（{t("miningResEtaEstimateNote")}）</span>
          </div>
        )}

        {/* E2：资源卡片（磁盘/内存剩余；缺省不展示） */}
        {(diskFreePercent != null || memoryFreeGb != null) && (
          <div className="mining-res-resource" data-resource-card>
            {diskFreePercent != null && (
              <span data-resource-disk className={diskOk ? "" : "risk"}>
                {t("miningResDisk").replace("{percent}", String(Math.round(diskFreePercent)))}
              </span>
            )}
            {memoryFreeGb != null && (
              <span data-resource-mem className={memOk ? "" : "risk"}>
                {t("miningResMem").replace("{gb}", String(memoryFreeGb.toFixed(1)))}
              </span>
            )}
          </div>
        )}

        {resourceInsufficient && (
          <div data-resource-blocked className="mining-res-blocked" role="alert">
            <div>{t("miningResResourceBlocked")}</div>
            <div>{t("miningResSuggestion")}</div>
          </div>
        )}

        <footer>
          <button type="button" data-resource-close onClick={onClose}>
            {t("miningResClose")}
          </button>
          <button
            type="button"
            data-evo-confirm-submit
            disabled={blocked}
            onClick={onConfirm}
          >
            {t("miningEvoConfirm")}
          </button>
        </footer>
      </div>
    </div>
  );
}
