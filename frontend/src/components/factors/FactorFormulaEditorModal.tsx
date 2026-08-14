import type { RefObject } from "react";
import type { TextAreaRef } from "antd/es/input/TextArea";
import {
  Alert,
  Button,
  Form,
  Input,
  Modal,
  Select,
  Space,
  Tag,
  Tooltip,
} from "antd";
import {
  CheckCircleFilled,
  CloseCircleFilled,
  RobotOutlined,
  SafetyCertificateOutlined,
  EyeOutlined,
  QuestionCircleOutlined,
  InfoCircleOutlined,
  PlayCircleOutlined,
  FormatPainterOutlined,
  UndoOutlined,
  RedoOutlined,
  DeleteOutlined,
} from "@ant-design/icons";
import { t } from "../../i18n";
import FactorFormulaBuilder from "./FactorFormulaBuilder";
import "./FactorFormulaEditorModal.css";

type Direction = "higher_better" | "lower_better" | "nonlinear";

type FormulaTemplate = {
  key: string;
  name: string;
  formula: string;
  category: string;
};

type FactorFormulaEditorModalProps = {
  open: boolean;
  isZh: boolean;
  factorCode: string | null;
  formulaExpr: string;
  formulaTextAreaRef: RefObject<TextAreaRef>;
  versionDirection: Direction;
  changeNote: string;
  paramsText: string;
  templates: FormulaTemplate[];
  validating: boolean;
  previewing: boolean;
  validationState: "valid" | "invalid" | "idle";
  previewCount: number | null;
  directionOptions: Array<{ value: Direction; label: string }>;
  onCancel: () => void;
  onApply: () => void;
  onAskAi: () => void;
  onValidate: () => void;
  onPreview: () => void;
  onInsert: (snippet: string) => void;
  onUseExample: (formula: string) => void;
  onApplyTemplate: (template: FormulaTemplate) => void;
  onFormulaChange: (value: string) => void;
  onRememberSelection: (textarea: HTMLTextAreaElement) => void;
  onDirectionChange: (value: Direction) => void;
  onChangeNoteChange: (value: string) => void;
  onParamsTextChange: (value: string) => void;
};

export default function FactorFormulaEditorModal({
  open,
  isZh,
  factorCode,
  formulaExpr,
  formulaTextAreaRef,
  versionDirection,
  changeNote,
  paramsText,
  templates,
  validating,
  previewing,
  validationState,
  previewCount,
  directionOptions,
  onCancel,
  onApply,
  onAskAi,
  onValidate,
  onPreview,
  onInsert,
  onUseExample,
  onApplyTemplate,
  onFormulaChange,
  onRememberSelection,
  onDirectionChange,
  onChangeNoteChange,
  onParamsTextChange,
}: FactorFormulaEditorModalProps) {
  const validationTag = validationState === "valid" ? (
    <Tag icon={<CheckCircleFilled />} color="success" className="formula-validation-tag">
      {t("factorEditorValid")}
    </Tag>
  ) : validationState === "invalid" ? (
    <Tag icon={<CloseCircleFilled />} color="error" className="formula-validation-tag">
      {t("factorEditorInvalid")}
    </Tag>
  ) : (
    <Tag className="formula-validation-tag">{t("factorFormulaModalNeedsValidation")}</Tag>
  );

  // 模拟行号
  const lineCount = formulaExpr ? formulaExpr.split("\n").length : 1;
  const lineNumbers = Array.from({ length: Math.max(lineCount, 15) }, (_, i) => i + 1);

  return (
    <Modal
      title={
        <div className="formula-modal-title">
          <span className="formula-modal-title-text">{t("factorFormulaModalTitle")}</span>
          <Tag className="formula-modal-factor-tag">{factorCode || t("factorEditorTitleNew")}</Tag>
          <Tag color="warning">{isZh ? "待重新校验" : "Needs Re-validation"}</Tag>
        </div>
      }
      open={open}
      onCancel={onCancel}
      width={1400}
      className="factor-formula-editor-modal formula-editor-redesign"
      maskClosable={false}
      keyboard={!validating && !previewing}
      closable={!validating && !previewing}
      destroyOnHidden
      footer={
        <Space wrap>
          <Button onClick={onCancel} disabled={validating || previewing}>{t("cancel")}</Button>
          <Button icon={<SafetyCertificateOutlined />} loading={validating} onClick={onValidate}>
            {t("factorEditorValidate")}
          </Button>
          <Button icon={<EyeOutlined />} loading={previewing} onClick={onPreview}>
            {t("factorEditorPreview")}
          </Button>
          <Button type="primary" onClick={onApply} disabled={validating || previewing}>
            {t("factorFormulaModalApply")}
          </Button>
        </Space>
      }
    >
      <div className="factor-formula-modal-layout">
        {/* 左侧：表达式助手 */}
        <aside className="factor-formula-modal-expression formula-left-panel" aria-label={t("factorFormulaModalExpressionPanel")}>
          <div className="formula-left-header">
            <div className="formula-left-title">
              <strong>{t("factorFormulaModalExpressionPanel")}</strong>
            </div>
            <Tooltip title={t("factorFormulaModalExpressionHint")}>
              <InfoCircleOutlined className="formula-left-help" />
            </Tooltip>
          </div>
          <FactorFormulaBuilder
            isZh={isZh}
            onInsert={onInsert}
            onUseExample={onUseExample}
          />
        </aside>

        {/* 右侧：代码编辑器 */}
        <section className="factor-formula-modal-editor formula-right-panel">
          {/* 顶部工具栏 */}
          <div className="formula-editor-toolbar">
            <div className="formula-editor-title-section">
              <div className="formula-editor-title">{t("factorEditorFormulaExpr")}</div>
              <div className="formula-editor-subtitle">{t("factorFormulaModalEditorHint")}</div>
            </div>
            <div className="formula-editor-actions">
              <Space size="small">
                <Button
                  type="primary"
                  icon={<RobotOutlined />}
                  onClick={onAskAi}
                  className="formula-ask-ai-btn"
                  data-testid="factor-modal-ask-ai"
                >
                  {t("aiAskButton")}
                </Button>
                <Button icon={<FormatPainterOutlined />}>
                  {isZh ? "格式化" : "Format"}
                </Button>
                <Button
                  type="primary"
                  icon={<PlayCircleOutlined />}
                  onClick={onPreview}
                  loading={previewing}
                  className="formula-compile-btn"
                >
                  {isZh ? "编译并预览" : "Compile & Preview"}
                </Button>
              </Space>
            </div>
          </div>

          {/* 代码编辑器卡片 */}
          <div className="formula-code-card">
            {/* 卡片头部：文件名 + 撤销/重做/清空 */}
            <div className="formula-code-card-header">
              <div className="formula-code-filename">
                <span className="formula-code-filename-dot" />
                {factorCode || "new_factor"}
                <span className="formula-code-version"> · {isZh ? "草稿" : "draft"}</span>
              </div>
              <Space size="small" className="formula-code-toolbar">
                <Button size="small" type="text" icon={<UndoOutlined />} disabled>
                  {isZh ? "撤销" : "Undo"}
                </Button>
                <Button size="small" type="text" icon={<RedoOutlined />} disabled>
                  {isZh ? "重做" : "Redo"}
                </Button>
                <Button size="small" type="text" danger icon={<DeleteOutlined />} disabled={!formulaExpr}>
                  {isZh ? "清空" : "Clear"}
                </Button>
              </Space>
            </div>

            {/* 代码编辑区：行号 + 输入框 */}
            <div className="formula-code-editor-wrapper">
              <div className="formula-code-line-numbers">
                {lineNumbers.map((n) => (
                  <div key={n} className="formula-code-line-num">{n}</div>
                ))}
              </div>
              <Input.TextArea
                ref={formulaTextAreaRef}
                value={formulaExpr}
                onChange={(event) => {
                  onFormulaChange(event.target.value);
                  onRememberSelection(event.currentTarget);
                }}
                onClick={(event) => onRememberSelection(event.currentTarget)}
                onKeyUp={(event) => onRememberSelection(event.currentTarget)}
                onSelect={(event) => onRememberSelection(event.currentTarget)}
                className="formula-code-textarea"
                placeholder={t("factorEditorFormulaPlaceholder")}
                autoSize={{ minRows: 15, maxRows: 25 }}
              />
            </div>

            {/* 底部状态栏 */}
            <div className="formula-code-statusbar">
              <Space size="middle" className="formula-statusbar-left">
                <span className="formula-statusbar-lncol">
                  Ln {lineCount}, Col {formulaExpr?.length || 0}
                </span>
                <span className="formula-statusbar-version">DSL v2</span>
                <span className="formula-statusbar-validation">
                  {validationTag}
                </span>
                {previewCount !== null ? (
                  <span className="formula-statusbar-preview">
                    {t("factorFormulaModalPreviewRows")}: {previewCount}
                  </span>
                ) : null}
              </Space>
              <Space size="middle" className="formula-statusbar-right">
                <span className="formula-statusbar-deps">
                  {isZh ? "依赖" : "Deps"}: {formulaExpr ? "1" : "0"} {isZh ? "个字段" : "field(s)"}
                </span>
                <span className="formula-statusbar-warmup">
                  {isZh ? "需要" : "Need"} 19 {isZh ? "个热身交易日" : "warmup days"}
                </span>
              </Space>
            </div>
          </div>

          {/* 提示信息 */}
          <Alert
            type="warning"
            showIcon
            icon={<InfoCircleOutlined />}
            message={isZh
              ? "正式评价会重新计算完整股票池、历史窗口和真实目标标签；编辑器预览不作为正式结论。"
              : "Formal evaluation recalculates full universe, history window and real target labels. Editor preview is not a formal conclusion."}
            className="formula-editor-notice"
          />

          {/* 元数据区：方向 + 版本说明 + 参数 */}
          <div className="formula-meta-section">
            <Form layout="vertical" size="small">
              <div className="formula-meta-grid">
                <Form.Item
                  label={
                    <Space size={4}>
                      <span>{t("factorEditorDirection")}</span>
                      <Tooltip title={t("factorEditorDirectionTooltip")}>
                        <QuestionCircleOutlined className="factor-formula-help-icon" />
                      </Tooltip>
                    </Space>
                  }
                  className="formula-meta-item"
                >
                  <Select
                    value={versionDirection}
                    onChange={onDirectionChange}
                    options={directionOptions}
                    className="formula-meta-select"
                  />
                </Form.Item>
                <Form.Item label={t("factorEditorChangeNote")} className="formula-meta-item">
                  <Input
                    value={changeNote}
                    onChange={(event) => onChangeNoteChange(event.target.value)}
                    placeholder={t("factorEditorChangeNotePlaceholder")}
                  />
                </Form.Item>
              </div>
              <Form.Item
                label={t("factorEditorParams")}
                help={t("factorEditorParamsHelp")}
                style={{ marginBottom: 0 }}
                className="formula-meta-item"
              >
                <Input.TextArea
                  value={paramsText}
                  onChange={(event) => onParamsTextChange(event.target.value)}
                  rows={4}
                  className="formula-params-textarea"
                  placeholder="{}"
                />
              </Form.Item>
            </Form>
          </div>

          {/* 兼容性提示 */}
          <Alert
            type="info"
            showIcon
            message={t("factorFormulaModalCompatibilityTitle")}
            description={t("factorFormulaModalCompatibilityDescription")}
            className="formula-compat-alert"
          />
        </section>
      </div>
    </Modal>
  );
}
