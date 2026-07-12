import React from "react";
import ReactDOM from "react-dom/client";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { setLocale } from "./i18n";
import UniverseDataPanel from "./components/UniverseDataPanel";

setLocale("zh-CN");

function StandaloneApp() {
  return (
    <div style={{ minHeight: "100vh", background: "#f5f1e8", padding: 24 }}>
      <div style={{ maxWidth: 1400, margin: "0 auto" }}>
        <UniverseDataPanel />
      </div>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ConfigProvider locale={zhCN} theme={{ token: { colorPrimary: "#0f766e" } }}>
      <StandaloneApp />
    </ConfigProvider>
  </React.StrictMode>
);
