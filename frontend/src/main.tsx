import React from "react";
import ReactDOM from "react-dom/client";
import { App as AntdApp, ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import enUS from "antd/locale/en_US";
import dayjs from "dayjs";
import "dayjs/locale/zh-cn";
import "dayjs/locale/en";
// antd DatePicker（rc-picker/dayjs）依赖这些 dayjs 插件，否则选日期会抛 clone.weekday is not a function
import customParseFormat from "dayjs/plugin/customParseFormat";
import advancedFormat from "dayjs/plugin/advancedFormat";
import weekday from "dayjs/plugin/weekday";
import localeData from "dayjs/plugin/localeData";
import weekOfYear from "dayjs/plugin/weekOfYear";
import weekYear from "dayjs/plugin/weekYear";
dayjs.extend(customParseFormat);
dayjs.extend(advancedFormat);
dayjs.extend(weekday);
dayjs.extend(localeData);
dayjs.extend(weekOfYear);
dayjs.extend(weekYear);
import WorkbenchApp from "./App";
import { AppProvider, useApp } from "./context/AppContext";
import "./styles/workbench.css";
import "./styles/mining.css";
import {startAcceptanceMonitor} from './utils/acceptanceMode';

startAcceptanceMonitor();

function Root() {
  const { locale, setAntdMessageApi } = useApp();
  const antdLocale = locale === "zh-CN" ? zhCN : enUS;
  const { message: messageApi } = AntdApp.useApp();

  React.useEffect(() => {
    dayjs.locale(locale === "zh-CN" ? "zh-cn" : "en");
  }, [locale]);

  React.useEffect(() => {
    // 将 antd App 上下文的 message 实例注入到 AppContext，供 showToast 使用
    // 这样 showToast / message 就走上下文而不是静态调用，消除 "Static function can not consume context" 警告
    setAntdMessageApi(messageApi);
  }, [messageApi, setAntdMessageApi]);

  return (
    <ConfigProvider
      locale={antdLocale}
      theme={{
        token: {
          colorPrimary: "#0f766e",
          colorInfo: "#2563eb",
          colorSuccess: "#16a34a",
          colorWarning: "#d97706",
          colorError: "#dc2626",
          colorText: "#111827",
          colorTextSecondary: "#5f6b7a",
          colorTextTertiary: "#98a2b3",
          colorBgLayout: "#f6f7f9",
          colorBorder: "#dce1e8",
          colorBorderSecondary: "#ebedf2",
          borderRadius: 8,
          controlHeight: 36,
          fontSize: 14,
          fontFamily:
            '-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Helvetica Neue", Arial, sans-serif',
        },
        components: {
          Button: {
            fontWeight: 500,
            primaryShadow: "none",
            defaultShadow: "none",
            borderRadius: 8,
            borderRadiusSM: 8,
            controlHeight: 36,
            controlHeightSM: 28,
            paddingInline: 16,
            paddingInlineSM: 12,
            contentFontSize: 14,
            contentFontSizeSM: 12,
          },
          Card: {
            borderRadiusLG: 10,
            boxShadowTertiary: "0 1px 2px rgba(16, 24, 40, 0.04), 0 1px 3px rgba(16, 24, 40, 0.05)",
          },
          Table: {
            headerBg: "#f8fafc",
            headerColor: "#5f6b7a",
            headerSplitColor: "transparent",
            rowHoverBg: "#f7f9fb",
            borderColor: "#ebedf2",
          },
          Modal: { borderRadiusLG: 12 },
          Drawer: { borderRadiusLG: 12 },
          Input: { activeShadow: "0 0 0 3px rgba(15,118,110,0.12)" },
          Select: { activeOutlineColor: "#0f766e" },
          Tabs: { inkBarColor: "#0f766e", itemActiveColor: "#0f766e", itemSelectedColor: "#0f766e", itemHoverColor: "#0f766e" },
          Menu: { itemSelectedBg: "rgba(15,118,110,0.08)", itemSelectedColor: "#0f766e" },
          Tag: { borderRadiusSM: 6 },
        },
      }}
    >
      <WorkbenchApp />
    </ConfigProvider>
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <AppProvider>
      <AntdApp>
        <Root />
      </AntdApp>
    </AppProvider>
  </React.StrictMode>
);
