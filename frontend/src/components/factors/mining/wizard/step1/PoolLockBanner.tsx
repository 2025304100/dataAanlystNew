import { t } from "../../../../../i18n";

/** 黄色锁定提示条（向导 §3.7.3）：生成挖掘物料后全程可见。 */
export default function PoolLockBanner() {
  return (
    <div className="mining-pool-lock-banner" data-pool-lock-banner role="status">
      {t("miningPoolLockBanner")}
    </div>
  );
}
