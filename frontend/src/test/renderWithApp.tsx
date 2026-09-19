import React from "react";
import { render, RenderOptions } from "@testing-library/react";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { AppProvider, AppContext } from "../context/AppContext";
import { AIAssistantProvider } from "../components/ai/AIAssistantContext";
import type { AppContextValue } from "../context/AppContext";
import { makeMockContext } from "./factories";
import { setLocale } from "../i18n";

// 默认中文环境
setLocale("zh-CN");

interface RenderWithAppOptions extends RenderOptions {
  locale?: "zh-CN" | "en-US";
  /**
   * 注入自定义 AppContext 值。传入后使用 makeMockContext 构造完整 mock context，
   * 适用于不依赖真实 AppProvider 副作用、只需 useApp 返回稳定值的组件测试。
   */
  contextValue?: Partial<AppContextValue>;
  /** 是否包裹 AIAssistantProvider（默认 true，ExplainButton 等组件依赖） */
  withAIAssistant?: boolean;
}

/**
 * 统一测试渲染器：默认包裹 ConfigProvider + AppProvider + AIAssistantProvider。
 *
 * - 不传 contextValue：使用真实 AppProvider（适合需要 context 副作用的集成测试，
 *   需配合 mock api/client 使用）。
 * - 传 contextValue：使用 makeMockContext 构造完整 mock context，通过 AppContext.Provider
 *   注入（适合纯组件单测，避免 AppProvider 启动时的 API 调用）。
 *
 * 注意：当测试文件已 vi.mock("../../context/AppContext") 时，AppProvider/useApp 会被
 * 替换为 mock，此时 renderWithApp 仍会包裹 AIAssistantProvider，ExplainButton 等依赖
 * AI 助手上下文的组件可正常渲染。
 */
export function renderWithApp(
  ui: React.ReactElement,
  options: RenderWithAppOptions = {},
) {
  const {
    locale = "zh-CN",
    contextValue,
    withAIAssistant = true,
    ...rest
  } = options;

  const wrapAI = (children: React.ReactNode) =>
    withAIAssistant ? <AIAssistantProvider>{children}</AIAssistantProvider> : children;

  const tree = (
    <ConfigProvider
      locale={locale === "zh-CN" ? zhCN : undefined}
      theme={{ token: { colorPrimary: "#0f766e" } }}
    >
      {contextValue ? (
        <AppContext.Provider value={makeMockContext(contextValue)}>
          {wrapAI(ui)}
        </AppContext.Provider>
      ) : (
        <AppProvider>{wrapAI(ui)}</AppProvider>
      )}
    </ConfigProvider>
  );

  return render(tree, rest);
}
