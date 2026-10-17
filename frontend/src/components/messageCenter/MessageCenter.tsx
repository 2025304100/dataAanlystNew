import { t } from "../../i18n";
import AlertCenter from "../AlertCenter";
import MessageInbox from "./MessageInbox";
import DeliverySettings, { type DeliverySubTab } from "./DeliverySettings";

export type MessageCenterTab = "alerts" | "inbox" | "delivery";

const TABS: Array<{ key: MessageCenterTab; labelKey: string }> = [
  { key: "alerts", labelKey: "mcTabAlerts" },
  { key: "inbox", labelKey: "mcTabInbox" },
  { key: "delivery", labelKey: "mcTabDelivery" },
];

/**
 * 消息中心：把原先并列的两个入口（「告警中心」与「消息管理」）合成一个。
 *
 * 两者不是重复实现，而是上下游：`alert_rules`+`EVALUATORS` 负责识别越界并写
 * `alert_events`，通知侧负责按策略投递到渠道并留投递审计（`alerts.py` 单向
 * `emit_alert_event`）。所以这里只合并在入口与阅读动线上，不合并数据模型：
 * - 告警：规则、事件、确认、恢复（复用 `AlertCenter`，API 与状态不变）
 * - 收件箱：站内消息与已读（`/notifications/inbox`，与顶部铃铛同源）
 * - 送达配置：渠道 / 策略 / 模板 / 发送记录（复用原四个子页）
 */
export default function MessageCenter({
  tab,
  onTabChange,
  deliverySubTab,
  onDeliverySubTabChange,
}: {
  tab: MessageCenterTab;
  onTabChange: (tab: MessageCenterTab) => void;
  deliverySubTab: DeliverySubTab;
  onDeliverySubTabChange: (tab: DeliverySubTab) => void;
}) {
  return (
    <div className="settings-indicator-stack" data-message-center>
      <div className="sub-tabs" aria-label={t("messageCenter")}>
        {TABS.map((item) => (
          <button
            key={item.key}
            type="button"
            className={`sub-tab ${tab === item.key ? "active" : ""}`}
            aria-current={tab === item.key ? "page" : undefined}
            onClick={() => onTabChange(item.key)}
          >
            {t(item.labelKey)}
          </button>
        ))}
      </div>
      <div className="sub-tab-container" hidden={tab !== "alerts"}>
        <AlertCenter />
      </div>
      <div className="sub-tab-container" hidden={tab !== "inbox"}>
        <MessageInbox onOpenDeliveries={() => onTabChange("delivery")} />
      </div>
      <div className="sub-tab-container" hidden={tab !== "delivery"}>
        <DeliverySettings activeTab={deliverySubTab} onTabChange={onDeliverySubTabChange} />
      </div>
    </div>
  );
}
