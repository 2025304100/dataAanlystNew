import React from "react";
import ReactDOM from "react-dom/client";
import { App as AntdApp, ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import enUS from "antd/locale/en_US";
import dayjs from "dayjs";
import "dayjs/locale/zh-cn";
import "dayjs/locale/en";
import WorkbenchApp from "./App";
import { AppProvider, useApp } from "./context/AppContext";
import "./styles/workbench.css";

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
    <ConfigProvider locale={antdLocale} theme={{ token: { colorPrimary: "#0f766e" } }}>
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
