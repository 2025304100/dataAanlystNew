import { useState, useEffect } from "react";
import { Button, Space, Modal } from "antd";
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

type FactorSubTab = "library" | "evaluation" | "shadow" | "model";

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
    // 兼容旧值：detail / editor 回退到 library
    if (stored === "library" || stored === "evaluation" || stored === "shadow" || stored === "model") {
      return stored;
    }
    return "library";
  });

  // —— 弹窗状态 ——
  const [detailModalOpen, setDetailModalOpen] = useState(false);
  const [editorModalOpen, setEditorModalOpen] = useState(false);
  const [selectedFactorCode, setSelectedFactorCode] = useState<string | null>(null);
  const [editorInitialPayload, setEditorInitialPayload] = useState<FactorDraftPayload | null>(null);
  const [editorIsNewDraft, setEditorIsNewDraft] = useState(true);

  // WP4-05: AI 草案确认 Modal 状态
  const [draftModalOpen, setDraftModalOpen] = useState(false);
  const [draftAuditId, setDraftAuditId] = useState<number | null>(null);
  const [draftSuggestedPayload, setDraftSuggestedPayload] = useState<FactorDraftPayload | null>(null);

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

  // 监听外部切换内层 Tab（评估实验室 blockers 的修复超链接 / 其他外部模块跳转）
  useEffect(() => {
    const navHandler = (e: Event) => {
      const detail = (e as CustomEvent).detail as Record<string, unknown> | undefined;
      const target = detail?.target as string | undefined;
      // 兼容旧 target 值
      if (target === "detail") {
        const code = (detail?.factor_code as string) || selectedFactorCode;
        if (code) openDetailModal(code);
      } else if (target === "editor") {
        const code = (detail?.factor_code as string) || null;
        if (code) {
          openEditorModalFromFactor(code);
        } else {
          openEditorModalNew();
        }
      } else {
        const allowed: FactorSubTab[] = ["library", "evaluation", "shadow", "model"];
        if (target && allowed.includes(target as FactorSubTab)) {
          setActiveTab(target as FactorSubTab);
          window.localStorage.setItem("settings_factor_center_subtab", target);
        }
      }
    };
    window.addEventListener("factor-center:navigate", navHandler as EventListener);
    return () => window.removeEventListener("factor-center:navigate", navHandler as EventListener);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedFactorCode]);

  // —— 详情弹窗 ——
  const openDetailModal = (code: string) => {
    setSelectedFactorCode(code);
    setDetailModalOpen(true);
  };

  const closeDetailModal = () => {
    setDetailModalOpen(false);
  };

  // —— 编辑器弹窗 ——
  // 新建草稿
  const openEditorModalNew = () => {
    setSelectedFactorCode(null);
    setEditorInitialPayload(null);
    setEditorIsNewDraft(true);
    setEditorModalOpen(true);
  };

  // 基于已有因子创建新版本
  const openEditorModalFromFactor = (code: string) => {
    setSelectedFactorCode(code);
    setEditorInitialPayload(null);
    setEditorIsNewDraft(false);
    setEditorModalOpen(true);
  };

  const closeEditorModal = () => {
    setEditorModalOpen(false);
  };

  // 从详情页跳转到编辑器（创建新版本）
  const handleDetailOpenEditor = (code: string) => {
    setDetailModalOpen(false);
    setSelectedFactorCode(code);
    setEditorIsNewDraft(false);
    setEditorInitialPayload(null);
    setEditorModalOpen(true);
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
    setEditorIsNewDraft(true);
    setDraftModalOpen(false);
    setEditorModalOpen(true);
  };

  // WP4-05: 直接创建成功后跳转详情
  const handleDraftCreated = (factorCode: string) => {
    setEditorInitialPayload(null);
    setEditorModalOpen(false);
    openDetailModal(factorCode);
  };

  // 编辑器保存成功后
  const handleEditorSaved = (code: string) => {
    setEditorModalOpen(false);
    openDetailModal(code);
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
          <FactorLibrary
            onViewDetail={openDetailModal}
            onEditFactor={openEditorModalFromFactor}
            onNewFactor={openEditorModalNew}
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

      {/* 因子详情弹窗 */}
      <Modal
        title={t("factorCenterTabDetail")}
        open={detailModalOpen}
        onCancel={closeDetailModal}
        footer={null}
        width={960}
        destroyOnHidden
        className="factor-detail-modal"
      >
        {selectedFactorCode && detailModalOpen ? (
          <FactorDetail
            factorCode={selectedFactorCode}
            onOpenEditor={handleDetailOpenEditor}
            onBack={closeDetailModal}
          />
        ) : null}
      </Modal>

      {/* 因子编辑器弹窗 */}
      <Modal
        title={editorIsNewDraft ? t("factorEditorTitleNew") : t("factorCenterTabEditor")}
        open={editorModalOpen}
        onCancel={closeEditorModal}
        footer={null}
        width={1200}
        destroyOnHidden
        className="factor-editor-modal"
      >
        {editorModalOpen ? (
          <FactorEditor
            factorCode={editorIsNewDraft ? null : selectedFactorCode}
            initialPayload={editorInitialPayload}
            onSaved={handleEditorSaved}
            onBack={closeEditorModal}
          />
        ) : null}
      </Modal>

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
