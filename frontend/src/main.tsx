import React from "react";
import ReactDOM from "react-dom/client";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import enUS from "antd/locale/en_US";
import dayjs from "dayjs";
import "dayjs/locale/zh-cn";
import "dayjs/locale/en";
import App from "./App";
import { AppProvider, useApp } from "./context/AppContext";
import "./styles/workbench.css";

function Root() {
  const { locale } = useApp();
  const antdLocale = locale === "zh-CN" ? zhCN : enUS;

  React.useEffect(() => {
    dayjs.locale(locale === "zh-CN" ? "zh-cn" : "en");
  }, [locale]);

  return (
    <ConfigProvider locale={antdLocale} theme={{ token: { colorPrimary: "#0f766e" } }}>
      <App />
    </ConfigProvider>
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <AppProvider>
      <Root />
    </AppProvider>
  </React.StrictMode>
);
