import React from "react";
import { render, RenderOptions } from "@testing-library/react";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { AppProvider } from "../context/AppContext";
import { setLocale } from "../i18n";

// 默认中文环境
setLocale("zh-CN");

interface AllProvidersProps {
  children: React.ReactNode;
  locale?: "zh-CN" | "en-US";
}

function AllProviders({ children, locale = "zh-CN" }: AllProvidersProps) {
  return (
    <ConfigProvider locale={locale === "zh-CN" ? zhCN : undefined} theme={{ token: { colorPrimary: "#0f766e" } }}>
      <AppProvider>{children}</AppProvider>
    </ConfigProvider>
  );
}

/**
 * 自定义 render：包裹 AppProvider + ConfigProvider，组件内可使用 useApp。
 * 对于不依赖 context 的纯工具/组件，可直接使用 @testing-library/react 的 render。
 */
function renderWithProviders(ui: React.ReactElement, options?: RenderOptions & { locale?: "zh-CN" | "en-US" }) {
  const { locale, ...rest } = options || {};
  return render(<AllProviders locale={locale}>{ui}</AllProviders>, rest);
}

export { renderWithProviders as render, AllProviders };
