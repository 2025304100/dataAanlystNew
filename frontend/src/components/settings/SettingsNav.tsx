import type { ReactNode } from "react";
import {
  ApiOutlined, AppstoreOutlined, BellOutlined, CalendarOutlined, DatabaseOutlined,
  ExperimentOutlined, FunctionOutlined, MedicineBoxOutlined, NotificationOutlined,
  RobotOutlined, SettingOutlined, SyncOutlined, TrophyOutlined, UnorderedListOutlined,
} from "@ant-design/icons";
import { t } from "../../i18n";

/**
 * 设置页侧边导航（T39 从 `Settings.tsx` 拆出，**纯重构、行为不变**）。
 *
 * 等价性契约（`Settings.test.tsx` 依赖，不得改动）：
 * - 容器：`<nav className="settings-sidebar" aria-label={t("ariaSettingsCategories")}>`
 * - 每项：`button.settings-nav-item`，`active` 类 + `aria-current="page"` 由 `active` 决定，
 *   点击调用 `onSelect(key)`，内部结构为 `span.settings-nav-icon` + `span.settings-nav-copy`；
 * - **顺序**与拆分前完全一致（视觉顺序即 tab 顺序）。
 *
 * 注：「数据中心」一项原为硬编码中文（未走 t()），本次拆分**原样保留**——
 * 本卡 not_do「不改任何设置项的行为与默认值」，文案国际化属另一议题。
 */

export type SettingsSection =
  | "rules" | "indicators" | "history" | "diagnostic" | "tasks" | "schedules"
  | "alerts" | "scoring" | "factor-model" | "factor-center" | "data-center"
  | "api-mgmt" | "db" | "ai" | "factor-mining" | "notifications" | "factor-basic";

interface NavItem {
  key: SettingsSection;
  icon: ReactNode;
  /** i18n key；`label` 存在时优先用字面量（等价保留原硬编码项） */
  labelKey?: string;
  label?: string;
}

const NAV_ITEMS: NavItem[] = [
  { key: "rules", icon: <SettingOutlined />, labelKey: "tabRules" },
  { key: "indicators", icon: <FunctionOutlined />, labelKey: "tabIndicators" },
  { key: "history", icon: <SyncOutlined />, labelKey: "histSettingsNav" },
  { key: "diagnostic", icon: <MedicineBoxOutlined />, labelKey: "diagTitle" },
  { key: "tasks", icon: <UnorderedListOutlined />, labelKey: "taskCenter" },
  { key: "schedules", icon: <CalendarOutlined />, labelKey: "scheduledTaskManager" },
  { key: "alerts", icon: <BellOutlined />, labelKey: "alertCenter" },
  { key: "scoring", icon: <TrophyOutlined />, labelKey: "scTabTitle" },
  { key: "factor-model", icon: <ExperimentOutlined />, labelKey: "factorModelTabTitle" },
  { key: "factor-center", icon: <AppstoreOutlined />, labelKey: "factorCenterTabTitle" },
  { key: "factor-mining", icon: <ExperimentOutlined />, labelKey: "factorMiningTabTitle" },
  { key: "factor-basic", icon: <SettingOutlined />, labelKey: "factorBasicTabTitle" },
  { key: "data-center", icon: <DatabaseOutlined />, label: "数据中心" },
  { key: "api-mgmt", icon: <ApiOutlined />, labelKey: "apiMgmtTabTitle" },
  { key: "db", icon: <DatabaseOutlined />, labelKey: "dbTabTitle" },
  { key: "ai", icon: <RobotOutlined />, labelKey: "aiConfigTabTitle" },
  { key: "notifications", icon: <NotificationOutlined />, labelKey: "messageManagement" },
];

export interface SettingsNavProps {
  active: SettingsSection;
  onSelect: (section: SettingsSection) => void;
}

export default function SettingsNav({ active, onSelect }: SettingsNavProps) {
  return (
    <nav className="settings-sidebar" aria-label={t("ariaSettingsCategories")}>
      {NAV_ITEMS.map((item) => (
        <button
          key={item.key}
          type="button"
          className={`settings-nav-item ${active === item.key ? "active" : ""}`}
          aria-current={active === item.key ? "page" : undefined}
          onClick={() => onSelect(item.key)}
        >
          <span className="settings-nav-icon">{item.icon}</span>
          <span className="settings-nav-copy">
            {item.label ?? t(item.labelKey as string)}
          </span>
        </button>
      ))}
    </nav>
  );
}
