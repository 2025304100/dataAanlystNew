import { t } from "../../i18n";
import { ChannelConfig } from "../notifications/ChannelConfig";
import { PolicyEditor } from "../notifications/PolicyEditor";
import { TemplateEditor } from "../notifications/TemplateEditor";
import { DeliveryLog } from "../notifications/DeliveryLog";

export type DeliverySubTab = "channels" | "policies" | "templates" | "deliveries";

const SUB_TABS: Array<{ key: DeliverySubTab; labelKey: string }> = [
  { key: "channels", labelKey: "channelConfig" },
  { key: "policies", labelKey: "policyEditor" },
  { key: "templates", labelKey: "templateEditor" },
  { key: "deliveries", labelKey: "deliveryLog" },
];

/**
 * 消息中心「送达配置」：渠道 / 策略 / 模板 / 发送记录。
 *
 * 从 `Settings.tsx` 原样搬来（DOM 结构与 `sub-tabs`/`sub-tab-container` 契约不变），
 * 状态仍由 `Settings` 持有，因此 `settings_notification_subtab` 持久化与
 * 「铃铛 → 查看全部 → 直达发送记录」的深链行为都保持原样。
 */
export default function DeliverySettings({
  activeTab,
  onTabChange,
}: {
  activeTab: DeliverySubTab;
  onTabChange: (tab: DeliverySubTab) => void;
}) {
  return (
    <div className="settings-indicator-stack" data-delivery-settings>
      <div className="sub-tabs" aria-label={t("mcTabDelivery")}>
        {SUB_TABS.map((tab) => (
          <button
            key={tab.key}
            type="button"
            className={`sub-tab ${activeTab === tab.key ? "active" : ""}`}
            aria-current={activeTab === tab.key ? "page" : undefined}
            onClick={() => onTabChange(tab.key)}
          >
            {t(tab.labelKey)}
          </button>
        ))}
      </div>
      <div className="sub-tab-container" hidden={activeTab !== "channels"}>
        <ChannelConfig />
      </div>
      <div className="sub-tab-container" hidden={activeTab !== "policies"}>
        <PolicyEditor />
      </div>
      <div className="sub-tab-container" hidden={activeTab !== "templates"}>
        <TemplateEditor />
      </div>
      <div className="sub-tab-container" hidden={activeTab !== "deliveries"}>
        <DeliveryLog />
      </div>
    </div>
  );
}
