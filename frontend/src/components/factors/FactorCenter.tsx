import { useState, useEffect } from "react";
import { Button, Space } from "antd";
import { ExperimentOutlined } from "@ant-design/icons";
import { useApp } from "../../context/AppContext";
import { t } from "../../i18n";
import FactorLibrary from "./FactorLibrary";
import FactorDetail from "./FactorDetail";
import FactorEditor from "./FactorEditor";
import FactorDraftConfirmModal from "./FactorDraftConfirmModal";
import FactorEvaluationLab from "./FactorEvaluationLab";
import FactorShadowLab from "./FactorShadowLab";
import FactorModelPage from "./FactorModelPage";

type FactorSubTab = "library" | "detail" | "editor" | "evaluation" | "shadow" | "model";

/** AI 草案 payload 类型（与 FactorDraftConfirmModal 对齐） */
type FactorDraftPayload = {
  code: string;
  name: string;
  category: string;
  formula_expr: string;
  params: Record<string, unknown>;
  direction: "higher_better" | "lower_better" | "nonlinear";
  factor_kind: "continuous" | "event" | "regime";
  risk_level: "low" | "medium" | "high";
  description?: string;
  thesis?: string;
  postprocess?: Record<string, unknown> | null;
  change_note?: string;
};

export default function FactorCenter() {
  const ctx = useApp();
  const [activeTab, setActiveTab] = useState<FactorSubTab>(() => {
    if (typeof window === "undefined") return "library";
    const stored = window.localStorage.getItem("settings_factor_center_subtab");
    return stored === "library" || stored === "detail" || stored === "editor" || stored === "evaluation" || stored === "shadow" || stored === "model"
      ? stored
      : "library";
  });
  const [selectedFactorCode, setSelectedFactorCode] = useState<string | null>(null);

  // WP4-05: AI 草案确认 Modal 状态
  const [draftModalOpen, setDraftModalOpen] = useState(false);
  const [draftAuditId, setDraftAuditId] = useState<number | null>(null);
  const [draftSuggestedPayload, setDraftSuggestedPayload] = useState<FactorDraftPayload | null>(null);
  const [editorInitialPayload, setEditorInitialPayload] = useState<FactorDraftPayload | null>(null);

  useEffect(() => {
    window.localStorage.setItem("settings_factor_center_subtab", activeTab);
  }, [activeTab]);

  // WP4-05: 监听 AI 聊天派发的 open-factor-draft 事件
  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent).detail;
      if (detail?.payload) {
        openDraftModal(null, detail.payload as FactorDraftPayload);
      }
    };
    window.addEventListener("open-factor-draft", handler as EventListener);
    return () => window.removeEventListener("open-factor-draft", handler as EventListener);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 打开因子详情
  const openDetail = (code: string) => {
    setSelectedFactorCode(code);
    setActiveTab("detail");
  };

  // 打开编辑器（新建草稿）
  const openEditor = () => {
    setSelectedFactorCode(null);
    setEditorInitialPayload(null);
    setActiveTab("editor");
  };

  // 基于此因子创建草稿
  const openEditorFromFactor = (code: string) => {
    setSelectedFactorCode(code);
    setEditorInitialPayload(null);
    setActiveTab("editor");
  };

  // WP4-05: 打开 AI 草案确认 Modal（外部可通过 window 事件触发）
  const openDraftModal = (auditId?: number | null, payload?: FactorDraftPayload | null) => {
    setDraftAuditId(auditId ?? null);
    setDraftSuggestedPayload(payload ?? null);
    setDraftModalOpen(true);
  };

  // WP4-05: 应用到编辑器
  const handleApplyToEditor = (payload: FactorDraftPayload) => {
    setSelectedFactorCode(null);
    setEditorInitialPayload(payload);
    setActiveTab("editor");
  };

  // WP4-05: 直接创建成功后跳转详情
  const handleDraftCreated = (factorCode: string) => {
    setEditorInitialPayload(null);
    openDetail(factorCode);
  };

  return (
    <div className="factor-center">
      <div className="factor-center-header">
        <h2>{t("factorCenterTitle")}</h2>
        <p className="factor-center-subtitle">{t("factorCenterSubtitle")}</p>
      </div>
      <div className="sub-tabs">
        <button
          type="button"
          className={`sub-tab ${activeTab === "library" ? "active" : ""}`}
          onClick={() => setActiveTab("library")}
        >
          {t("factorCenterTabLibrary")}
        </button>
        <button
          type="button"
          className={`sub-tab ${activeTab === "detail" ? "active" : ""}`}
          onClick={() => selectedFactorCode && setActiveTab("detail")}
          disabled={!selectedFactorCode}
        >
          {t("factorCenterTabDetail")}
        </button>
        <button
          type="button"
          className={`sub-tab ${activeTab === "editor" ? "active" : ""}`}
          onClick={() => setActiveTab("editor")}
        >
          {t("factorCenterTabEditor")}
        </button>
        <button
          type="button"
          className={`sub-tab ${activeTab === "evaluation" ? "active" : ""}`}
          onClick={() => setActiveTab("evaluation")}
        >
          {t("factorCenterTabEvaluation")}
        </button>
        <button
          type="button"
          className={`sub-tab ${activeTab === "shadow" ? "active" : ""}`}
          onClick={() => setActiveTab("shadow")}
        >
          {t("factorCenterTabShadow")}
        </button>
        <button
          type="button"
          className={`sub-tab ${activeTab === "model" ? "active" : ""}`}
          onClick={() => setActiveTab("model")}
        >
          {t("factorCenterTabModel")}
        </button>
        <Space style={{ marginLeft: "auto" }}>
          <Button
            size="small"
            icon={<ExperimentOutlined />}
            onClick={() => openDraftModal()}
            title={t("aiDraftFactorTitle")}
          >
            {t("aiDraftFactorTitle")}
          </Button>
        </Space>
      </div>
      <div className="sub-tab-container">
        <div hidden={activeTab !== "library"}>
          <FactorLibrary onOpenDetail={openDetail} onOpenEditor={openEditor} />
        </div>
        <div hidden={activeTab !== "detail"}>
          {selectedFactorCode ? (
            <FactorDetail
              factorCode={selectedFactorCode}
              onOpenEditor={openEditorFromFactor}
              onBack={() => setActiveTab("library")}
            />
          ) : null}
        </div>
        <div hidden={activeTab !== "editor"}>
          <FactorEditor
            factorCode={selectedFactorCode}
            initialPayload={editorInitialPayload}
            onSaved={(code) => openDetail(code)}
            onBack={() => setActiveTab(selectedFactorCode ? "detail" : "library")}
          />
        </div>
        <div hidden={activeTab !== "evaluation"}>
          <FactorEvaluationLab />
        </div>
        <div hidden={activeTab !== "shadow"}>
          <FactorShadowLab />
        </div>
        <div hidden={activeTab !== "model"}>
          <FactorModelPage />
        </div>
      </div>

      {/* WP4-05: AI 草案确认 Modal */}
      <FactorDraftConfirmModal
        open={draftModalOpen}
        auditId={draftAuditId}
        suggestedPayload={draftSuggestedPayload}
        onClose={() => setDraftModalOpen(false)}
        onApplyToEditor={handleApplyToEditor}
        onCreated={handleDraftCreated}
      />
    </div>
  );
}
