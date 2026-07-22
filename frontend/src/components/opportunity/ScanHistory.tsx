// WP1.2：机会中心-扫描记录子组件（建设状态）
// 第一阶段：显示建设状态占位，不伪造数据
// 后续 WP-P/WP9 会接入扫描历史快照与摘要，
// 届时填充真实的 ScanRun 历史
import { Alert, Empty } from "antd";
import { t } from "../../i18n";

export interface ScanHistoryProps {
  className?: string;
}

export default function ScanHistory({ className }: ScanHistoryProps) {
  return (
    <div
      className={`opportunity-scan-history ${className ?? ""}`}
      data-opportunity-tab="scan-history"
      data-state="under-construction"
    >
      <Alert
        type="info"
        showIcon
        message={t("opportunityScanHistoryConstructingTitle")}
        description={t("opportunityScanHistoryConstructingDesc")}
        style={{ marginBottom: 12 }}
      />
      <Empty description={t("opportunityUnderConstruction")} />
    </div>
  );
}
