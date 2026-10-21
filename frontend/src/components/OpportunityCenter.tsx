// WP1.1：机会中心主入口（信息架构壳层）
// 提供四个页签：候选池 / 观察池 / 已排除 / 扫描记录
// 未完成页签显示建设状态，不伪造数据
//
// 设计说明：
// - 第一版组合现有 Discovery 数据，不改扫描算法
// - 候选池直接复用 Discovery 组件
// - 观察池只读展示现有 watchlist_items（WP2 会替换为正式观察池）
// - 已排除/扫描记录显示建设状态占位
// - 数据来源：AppContext + useSymbolRelationships hook
import { useState, useCallback } from "react";
import { Tabs } from "antd";
import { useApp } from "../context/AppContext";
import { t } from "../i18n";
import CandidatePool from "./opportunity/CandidatePool";
import ObservationPool from "./opportunity/ObservationPool";
import ExcludedPool from "./opportunity/ExcludedPool";
import ScanHistory from "./opportunity/ScanHistory";
import { LocalFavoritesMigration } from "./opportunity/LocalFavoritesMigration";
import { DataPrepActions } from "./opportunity/DataPrepActions";
import type { DiscoveryRefreshRequest } from "./Discovery";

export const OPPORTUNITY_TAB_KEYS = ["candidate", "observation", "excluded", "scan-history"] as const;
export type OpportunityTabKey = (typeof OPPORTUNITY_TAB_KEYS)[number];

export default function OpportunityCenter() {
  const ctx = useApp();
  const [activeKey, setActiveKey] = useState<OpportunityTabKey>("candidate");
  const [candidateScope, setCandidateScope] = useState("cn-stock");
  const [refreshRequest, setRefreshRequest] = useState<DiscoveryRefreshRequest | null>(null);
  const handlePrepared = useCallback((scope: string, taskId: string) => {
    if (scope !== candidateScope) return;
    setRefreshRequest((previous) => previous?.scope === scope && previous.taskId === taskId ? previous : { scope, taskId });
  }, [candidateScope]);

  const handleChange = useCallback((key: string) => {
    setActiveKey(key as OpportunityTabKey);
  }, []);

  return (
    <div className="opportunity-center" data-active-tab={activeKey}>
      {/* WP2.5：本地收藏迁移工具（首次检测到 ic_favorites 时弹窗提示） */}
      <LocalFavoritesMigration />

      <header style={{ marginBottom: 12 }}>
        <h2 style={{ margin: "0 0 4px" }}>{t("opportunityCenterTitle")}</h2>
        <p style={{ margin: 0 }}>{t("opportunityCenterDesc")}</p>
      </header>

      <div hidden={activeKey !== "candidate"}>
        <DataPrepActions scope={candidateScope} onPrepared={handlePrepared} simpleMode />
      </div>

      <Tabs
        activeKey={activeKey}
        onChange={handleChange}
        size="small"
        items={[
          {
            key: "candidate",
            label: t("opportunityTabCandidate"),
            children: <CandidatePool refreshRequest={refreshRequest} onScopeChange={setCandidateScope} simpleMode />,
          },
          {
            key: "observation",
            label: t("opportunityTabObservation"),
            children: <ObservationPool />,
          },
          {
            key: "excluded",
            label: t("opportunityTabExcluded"),
            children: <ExcludedPool />,
          },
          {
            key: "scan-history",
            label: t("opportunityTabScanHistory"),
            children: <ScanHistory />,
          },
        ]}
      />

      {/* 隐藏 span 用于调试/可观测性：标记当前 portfolio 上下文 */}
      <span style={{ display: "none" }} data-portfolio-id={ctx.portfolioId ?? ""} />
    </div>
  );
}
