// WP1.2：机会中心-已排除子组件（建设状态）
// 第一阶段：显示建设状态占位，不伪造数据
// 后续 WP3 会实现统一的候选/观察/组合状态流转与审计，
// 届时填充真实的已排除候选列表
import { Alert, Empty } from "antd";
import { t } from "../../i18n";

export interface ExcludedPoolProps {
  className?: string;
}

export default function ExcludedPool({ className }: ExcludedPoolProps) {
  return (
    <div
      className={`opportunity-excluded-pool ${className ?? ""}`}
      data-opportunity-tab="excluded"
      data-state="under-construction"
    >
      <Alert
        type="info"
        showIcon
        message={t("opportunityExcludedConstructingTitle")}
        description={t("opportunityExcludedConstructingDesc")}
        style={{ marginBottom: 12 }}
      />
      <Empty description={t("opportunityUnderConstruction")} />
    </div>
  );
}
