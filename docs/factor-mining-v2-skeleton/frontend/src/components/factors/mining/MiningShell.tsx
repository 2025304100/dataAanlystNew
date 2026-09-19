/**
 * 因子挖掘壳层（设计文档 §9.1 / §9.2）。
 *
 * 落位：`frontend/src/components/factors/mining/MiningShell.tsx`
 * 挂载：`Settings.tsx` 的 `activeSection === "factor-mining"` 分支
 *
 * 范式对齐既有 `FactorCenter.tsx`（本项目**没有**路由/ tab 注册表）：
 *   手写 sub-tab 按钮 + `hidden` 切换 + localStorage 持久化
 *
 * ⚠️ 新增本组件后，`Settings.tsx` 必须同步改 **6 处**（设计文档 §9.1）：
 *   1. L34  activeSection 联合类型加 "factor-mining" / "factor-base-config"
 *   2. L38  localStorage 校验条件加这两个值
 *   3. L76  settings:navigate 白名单加这两个值   ← 最易漏，不加则跳转无效
 *   4. L295 后新增 2 个导航按钮
 *   5. L565 后新增 2 个内容块（data-settings-content="settings-factor-mining"）
 *   6. L1-25 import 本组件
 * 另需在 `i18n/zh-CN.ts` 与 `i18n/en-US.ts` **同步**新增 key：
 *   factorMiningTabTitle / factorBaseConfigTabTitle
 *   （只加一份会让 i18n/__tests__/translations.test.ts 变红）
 */
import { useState, useEffect } from "react";
import { Button } from "antd";
import { ExperimentOutlined } from "@ant-design/icons";
import { t } from "../../../i18n";

/** 子页签白名单（新增页签必须同时改这里与 localStorage 校验） */
const SUB_TABS = ["wizard", "runs", "result", "locks"] as const;
type MiningSubTab = (typeof SUB_TABS)[number];

const STORAGE_KEY = "settings_factor_mining_subtab";

function readStoredTab(): MiningSubTab {
  if (typeof window === "undefined") return "wizard";
  const stored = window.localStorage.getItem(STORAGE_KEY);
  return (SUB_TABS as readonly string[]).includes(stored ?? "")
    ? (stored as MiningSubTab)
    : "wizard";
}

export default function MiningShell() {
  const [activeTab, setActiveTab] = useState<MiningSubTab>(readStoredTab);

  useEffect(() => {
    if (typeof window !== "undefined") {
      window.localStorage.setItem(STORAGE_KEY, activeTab);
    }
  }, [activeTab]);

  // 支持外部深链：window.dispatchEvent(new CustomEvent("factor-mining:navigate", { detail: "wizard" }))
  useEffect(() => {
    const handler = (e: Event) => {
      const target = (e as CustomEvent<string>).detail;
      if (target && (SUB_TABS as readonly string[]).includes(target)) {
        setActiveTab(target as MiningSubTab);
      }
    };
    window.addEventListener("factor-mining:navigate", handler as EventListener);
    return () => window.removeEventListener("factor-mining:navigate", handler as EventListener);
  }, []);

  return (
    <div className="mining-shell" data-settings-content="settings-factor-mining">
      <div className="mining-header">
        <h2>
          <ExperimentOutlined /> {t("factorMiningTabTitle")}
        </h2>
        <p className="mining-subtitle">{t("mining.researchDisclaimer")}</p>
      </div>

      <div className="sub-tabs">
        <button
          type="button"
          className={`sub-tab ${activeTab === "wizard" ? "active" : ""}`}
          onClick={() => setActiveTab("wizard")}
        >
          {t("mining.tab.wizard")}
        </button>
        <button
          type="button"
          className={`sub-tab ${activeTab === "runs" ? "active" : ""}`}
          onClick={() => setActiveTab("runs")}
        >
          {t("mining.tab.runs")}
        </button>
        <button
          type="button"
          className={`sub-tab ${activeTab === "result" ? "active" : ""}`}
          onClick={() => setActiveTab("result")}
        >
          {t("mining.tab.result")}
        </button>
        <button
          type="button"
          className={`sub-tab ${activeTab === "locks" ? "active" : ""}`}
          onClick={() => setActiveTab("locks")}
        >
          {t("mining.tab.locks")}
        </button>
      </div>

      <div className="sub-tab-container">
        {/*
          本项目范式：全部常驻挂载 + hidden 切换（不做懒加载），
          与 FactorCenter.tsx:217-234 保持一致。
        */}
        <div hidden={activeTab !== "wizard"}>
          {/* TODO(M15): <MiningWizard /> */}
        </div>
        <div hidden={activeTab !== "runs"}>
          {/* TODO(M5): <MiningRunList /> */}
        </div>
        <div hidden={activeTab !== "result"}>
          {/* TODO(M7/M13): <MiningResultOverview /> */}
        </div>
        <div hidden={activeTab !== "locks"}>
          {/* TODO(M5): <LockStatusCard /> + 排队取消 */}
          <Button type="link" size="small" disabled>
            {t("mining.tab.locks")}
          </Button>
        </div>
      </div>
    </div>
  );
}
