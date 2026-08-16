import { useEffect, useMemo, useRef, useState } from "react";
import type { RefObject } from "react";
import type { TextAreaRef } from "antd/es/input/TextArea";
import {
  Alert,
  Button,
  Descriptions,
  Empty,
  Form,
  Input,
  Modal,
  Select,
  Space,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import {
  CheckCircleFilled,
  CloseCircleFilled,
  DeleteOutlined,
  EyeOutlined,
  FormatPainterOutlined,
  InfoCircleOutlined,
  PlayCircleOutlined,
  QuestionCircleOutlined,
  RedoOutlined,
  RobotOutlined,
  SafetyCertificateOutlined,
  UndoOutlined,
} from "@ant-design/icons";
import type {
  FactorFormulaCatalog,
  FactorFormulaDiagnostic,
  FactorPreviewResult,
  FactorValidateResult,
} from "../../api/client";
import { t } from "../../i18n";
import FactorFormulaBuilder from "./FactorFormulaBuilder";
import type { FormulaInspectorItem } from "./FactorFormulaBuilder";
import "./FactorFormulaEditorModal.css";

type Direction = "higher_better" | "lower_better" | "nonlinear";

type FactorFormulaEditorModalProps = {
  open: boolean;
  isZh: boolean;
  factorCode: string | null;
  formulaExpr: string;
  formulaTextAreaRef: RefObject<TextAreaRef>;
  versionDirection: Direction;
  changeNote: string;
  paramsText: string;
  catalog: FactorFormulaCatalog | null;
  catalogLoading: boolean;
  catalogError: string | null;
  validateResult: FactorValidateResult | null;
  previewResult: FactorPreviewResult | null;
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
  onRetryCatalog: () => void;
  onFormulaChange: (value: string) => void;
  onRememberSelection: (textarea: HTMLTextAreaElement) => void;
  onDirectionChange: (value: Direction) => void;
  onChangeNoteChange: (value: string) => void;
  onParamsTextChange: (value: string) => void;
};

function arrayValue(value: unknown): string[] {
  return Array.isArray(value) ? value.map(String) : [];
}

function numberValue(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function fieldAvailabilityTag(
  availability: FactorFormulaCatalog["fields"][number]["availability"],
  isZh: boolean,
) {
  const meta: Record<string, { color: string; zh: string; en: string }> = {
    available: { color: "success", zh: "可评价", en: "Ready" },
    limited: { color: "warning", zh: "覆盖受限", en: "Limited" },
    event: { color: "processing", zh: "仅事件模式", en: "Event only" },
    snapshot: { color: "purple", zh: "仅快照模式", en: "Snapshot only" },
    blocked: { color: "error", zh: "数据阻断", en: "Data blocked" },
    unknown: { color: "default", zh: "待检查", en: "Unknown" },
  };
  const item = meta[availability] ?? meta.unknown;
  return <Tag color={item.color}>{isZh ? item.zh : item.en}</Tag>;
}

function formatDiagnosticLocation(item: FactorFormulaDiagnostic, isZh: boolean) {
  if (item.line !== null && item.column !== null) {
    return isZh ? `第 ${item.line} 行，第 ${item.column} 列` : `Line ${item.line}, column ${item.column}`;
  }
  if (item.start !== null) return isZh ? `字符 ${item.start}` : `Character ${item.start}`;
  return isZh ? "公式级问题" : "Formula-level issue";
}

export default function FactorFormulaEditorModal({
  open,
  isZh,
  factorCode,
  formulaExpr,
  formulaTextAreaRef,
  versionDirection,
  changeNote,
  paramsText,
  catalog,
  catalogLoading,
  catalogError,
  validateResult,
  previewResult,
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
  onRetryCatalog,
  onFormulaChange,
  onRememberSelection,
  onDirectionChange,
  onChangeNoteChange,
  onParamsTextChange,
}: FactorFormulaEditorModalProps) {
  const [inspectorItem, setInspectorItem] = useState<FormulaInspectorItem | null>(null);
  const [cursorOffset, setCursorOffset] = useState(0);
  const [historyVersion, setHistoryVersion] = useState(0);
  const undoStack = useRef<string[]>([]);
  const redoStack = useRef<string[]>([]);
  const lastFormula = useRef(formulaExpr);
  const replayingHistory = useRef(false);

  useEffect(() => {
    if (!open) return;
    undoStack.current = [];
    redoStack.current = [];
    lastFormula.current = formulaExpr;
    replayingHistory.current = false;
    setCursorOffset(formulaExpr.length);
    setInspectorItem(null);
    setHistoryVersion((value) => value + 1);
    // Opening the dialog starts a new local edit-history session.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  useEffect(() => {
    if (!open || formulaExpr === lastFormula.current) return;
    if (!replayingHistory.current) {
      undoStack.current.push(lastFormula.current);
      if (undoStack.current.length > 100) undoStack.current.shift();
      redoStack.current = [];
    }
    replayingHistory.current = false;
    lastFormula.current = formulaExpr;
    setHistoryVersion((value) => value + 1);
  }, [formulaExpr, open]);

  const dependencies = validateResult?.data_dependencies ?? previewResult?.data_dependencies ?? null;
  const dependencyFields = arrayValue(dependencies?.fields);
  const dependencyFunctions = arrayValue(dependencies?.functions);
  const maxLookback = numberValue(dependencies?.max_lookback, 1);
  const diagnostics = validateResult?.errors ?? previewResult?.errors ?? [];

  const cursor = useMemo(() => {
    const before = formulaExpr.slice(0, Math.min(cursorOffset, formulaExpr.length));
    const lines = before.split("\n");
    return { line: lines.length, column: (lines[lines.length - 1]?.length ?? 0) + 1 };
  }, [cursorOffset, formulaExpr]);

  const lineCount = Math.max(formulaExpr.split("\n").length, 1);
  const lineNumbers = Array.from({ length: Math.max(lineCount, 15) }, (_, index) => index + 1);

  const validationTag = validationState === "valid" ? (
    <Tag icon={<CheckCircleFilled />} color="success">{t("factorEditorValid")}</Tag>
  ) : validationState === "invalid" ? (
    <Tag icon={<CloseCircleFilled />} color="error">{t("factorEditorInvalid")}</Tag>
  ) : (
    <Tag>{t("factorFormulaModalNeedsValidation")}</Tag>
  );

  const updateCursor = (textarea: HTMLTextAreaElement) => {
    setCursorOffset(textarea.selectionStart ?? textarea.value.length);
    onRememberSelection(textarea);
  };

  const undo = () => {
    const previous = undoStack.current.pop();
    if (previous === undefined) return;
    redoStack.current.push(formulaExpr);
    replayingHistory.current = true;
    onFormulaChange(previous);
  };

  const redo = () => {
    const next = redoStack.current.pop();
    if (next === undefined) return;
    undoStack.current.push(formulaExpr);
    replayingHistory.current = true;
    onFormulaChange(next);
  };

  const formatFormula = () => {
    const formatted = formulaExpr
      .split("\n")
      .map((line) => line.trimEnd())
      .join("\n")
      .trim();
    if (formatted !== formulaExpr) onFormulaChange(formatted);
  };

  const inspector = inspectorItem ? (
    <div className="formula-inspector-content">
      <Tag color={inspectorItem.kind === "field" ? "blue" : inspectorItem.kind === "function" ? "purple" : "default"}>
        {inspectorItem.kind}
      </Tag>
      {inspectorItem.kind === "field" ? (
        <>
          <Typography.Title level={5}>{isZh ? inspectorItem.label_zh : inspectorItem.label_en}</Typography.Title>
          <Space wrap>
            <Typography.Text code>{inspectorItem.key}</Typography.Text>
            {fieldAvailabilityTag(inspectorItem.availability, isZh)}
          </Space>
          <Typography.Paragraph>{inspectorItem.description}</Typography.Paragraph>
          <Alert
            type={inspectorItem.availability === "blocked" ? "error" : inspectorItem.availability === "available" ? "success" : "warning"}
            showIcon
            message={inspectorItem.status_reason}
          />
          <Descriptions size="small" column={1} colon={false}>
            <Descriptions.Item label={isZh ? "类型" : "Type"}>{inspectorItem.dtype}</Descriptions.Item>
            <Descriptions.Item label={isZh ? "数据源" : "Source"}>{inspectorItem.source_table}</Descriptions.Item>
            <Descriptions.Item label={isZh ? "数据层" : "Layer"}>{inspectorItem.layer}</Descriptions.Item>
            <Descriptions.Item label={isZh ? "时点数据" : "Point-in-time"}>{inspectorItem.point_in_time ? (isZh ? "是" : "Yes") : (isZh ? "否" : "No")}</Descriptions.Item>
            <Descriptions.Item label={isZh ? "真实非空记录" : "Non-null rows"}>{inspectorItem.nonnull_rows.toLocaleString()}</Descriptions.Item>
            <Descriptions.Item label={isZh ? "标的/日期" : "Symbols/dates"}>{inspectorItem.distinct_symbols > 0 ? inspectorItem.distinct_symbols.toLocaleString() : "-"} / {inspectorItem.distinct_dates > 0 ? inspectorItem.distinct_dates.toLocaleString() : "-"}</Descriptions.Item>
            <Descriptions.Item label={isZh ? "覆盖区间" : "Coverage range"}>{inspectorItem.first_date || inspectorItem.latest_date ? `${inspectorItem.first_date ?? "?"} → ${inspectorItem.latest_date ?? "?"}` : "-"}</Descriptions.Item>
            <Descriptions.Item label={isZh ? "正式评价" : "Formal evaluation"}>{inspectorItem.evaluation_enabled ? (isZh ? "允许（仅限真实覆盖区间）" : "Allowed in covered range") : (isZh ? "暂不允许" : "Not allowed")}</Descriptions.Item>
            {inspectorItem.derived ? <Descriptions.Item label={isZh ? "派生来源" : "Derived from"}>{inspectorItem.derived_from.join(", ")}</Descriptions.Item> : null}
          </Descriptions>
        </>
      ) : null}
      {inspectorItem.kind === "function" ? (
        <>
          <Typography.Title level={5}>{isZh ? inspectorItem.label_zh : inspectorItem.label_en}</Typography.Title>
          <Typography.Text code>{inspectorItem.signature}</Typography.Text>
          <Typography.Paragraph>{inspectorItem.enabled ? inspectorItem.description : inspectorItem.disabled_reason}</Typography.Paragraph>
          {inspectorItem.params?.length ? (
            <div className="formula-inspector-params">
              <span>{isZh ? "参数" : "Parameters"}</span>
              <Space wrap>{inspectorItem.params.map((param) => <Tag key={param}>{param}</Tag>)}</Space>
            </div>
          ) : null}
          <Typography.Text type="secondary">{isZh ? "插入示例：" : "Insert example: "}<code>{inspectorItem.snippet}</code></Typography.Text>
        </>
      ) : null}
      {inspectorItem.kind === "operator" ? (
        <>
          <Typography.Title level={5}>{inspectorItem.label} · {inspectorItem.description}</Typography.Title>
          <Typography.Text code>{inspectorItem.snippet}</Typography.Text>
        </>
      ) : null}
      {inspectorItem.kind === "template" ? (
        <>
          <Typography.Title level={5}>{isZh ? inspectorItem.name_zh : inspectorItem.name_en}</Typography.Title>
          <Typography.Paragraph code copyable>{inspectorItem.formula}</Typography.Paragraph>
        </>
      ) : null}
    </div>
  ) : (
    <Empty
      image={Empty.PRESENTED_IMAGE_SIMPLE}
      description={isZh ? "将鼠标移到字段、函数或运算符上查看用法" : "Hover a field, function, or operator to inspect it"}
    />
  );

  return (
    <Modal
      title={
        <div className="formula-modal-title">
          <span className="formula-modal-title-text">{t("factorFormulaModalTitle")}</span>
          <Tag className="formula-modal-factor-tag">{factorCode || t("factorEditorTitleNew")}</Tag>
          {validationTag}
        </div>
      }
      open={open}
      onCancel={onCancel}
      width="min(1680px, 96vw)"
      className="factor-formula-editor-modal formula-editor-redesign"
      maskClosable={false}
      keyboard={!validating && !previewing}
      closable={!validating && !previewing}
      destroyOnHidden
      footer={
        <Space wrap>
          <Button onClick={onCancel} disabled={validating || previewing}>{t("cancel")}</Button>
          <Button icon={<SafetyCertificateOutlined />} loading={validating} disabled={!formulaExpr.trim() || previewing} onClick={onValidate}>
            {t("factorEditorValidate")}
          </Button>
          <Button icon={<EyeOutlined />} loading={previewing} disabled={!formulaExpr.trim() || validating} onClick={onPreview}>
            {t("factorEditorPreview")}
          </Button>
          <Button type="primary" onClick={onApply} disabled={validating || previewing}>
            {t("factorFormulaModalApply")}
          </Button>
        </Space>
      }
    >
      <div className="factor-formula-modal-layout">
        <aside className="formula-left-panel" aria-label={t("factorFormulaModalExpressionPanel")}>
          <div className="formula-panel-heading">
            <div>
              <strong>{isZh ? "公式能力库" : "Formula catalog"}</strong>
              <span>{isZh ? "仅展示项目真实支持能力" : "Only executable project capabilities"}</span>
            </div>
            <Tooltip title={t("factorFormulaModalExpressionHint")}><InfoCircleOutlined /></Tooltip>
          </div>
          <FactorFormulaBuilder
            isZh={isZh}
            catalog={catalog}
            loading={catalogLoading}
            error={catalogError}
            onRetry={onRetryCatalog}
            onInsert={onInsert}
            onUseExample={onUseExample}
            onInspect={setInspectorItem}
          />
        </aside>

        <main className="formula-main-panel">
          <div className="formula-editor-toolbar">
            <div className="formula-editor-title-section">
              <div className="formula-editor-title">{t("factorEditorFormulaExpr")}</div>
              <div className="formula-editor-subtitle">
                {isZh ? "受控 DSL：支持项目函数、&&、||、! 与 if(condition, a, b)" : "Controlled DSL with project functions, &&, ||, !, and if(condition, a, b)"}
              </div>
            </div>
            <Space size="small" wrap>
              <Tooltip title={isZh ? "AI 只生成受控 DSL 草稿；插入后仍需人工校验" : "AI only drafts controlled DSL; manual validation remains required"}>
                <Button icon={<RobotOutlined />} onClick={onAskAi} className="formula-ask-ai-btn" data-testid="factor-modal-ask-ai">
                  {t("aiAskButton")}
                </Button>
              </Tooltip>
              <Button icon={<FormatPainterOutlined />} disabled={!formulaExpr} onClick={formatFormula}>
                {isZh ? "格式化" : "Format"}
              </Button>
              <Button type="primary" icon={<PlayCircleOutlined />} onClick={onPreview} disabled={!formulaExpr.trim() || validating} loading={previewing} className="formula-compile-btn">
                {isZh ? "编译并预览" : "Compile & Preview"}
              </Button>
            </Space>
          </div>

          <div className="formula-code-card">
            <div className="formula-code-card-header">
              <div className="formula-code-filename">
                <span className={`formula-code-filename-dot ${validationState}`} />
                {factorCode || "new_factor"}
                <span className="formula-code-version">· {isZh ? "当前草稿" : "current draft"}</span>
              </div>
              <Space size="small" className="formula-code-toolbar">
                <Button size="small" type="text" icon={<UndoOutlined />} disabled={undoStack.current.length === 0} onClick={undo} data-history-version={historyVersion}>
                  {isZh ? "撤销" : "Undo"}
                </Button>
                <Button size="small" type="text" icon={<RedoOutlined />} disabled={redoStack.current.length === 0} onClick={redo}>
                  {isZh ? "重做" : "Redo"}
                </Button>
                <Button size="small" type="text" danger icon={<DeleteOutlined />} disabled={!formulaExpr} onClick={() => onFormulaChange("")}>
                  {isZh ? "清空" : "Clear"}
                </Button>
              </Space>
            </div>

            <div className="formula-code-editor-wrapper">
              <div className="formula-code-line-numbers">
                {lineNumbers.map((number) => <div key={number} className="formula-code-line-num">{number}</div>)}
              </div>
              <Input.TextArea
                ref={formulaTextAreaRef}
                value={formulaExpr}
                onChange={(event) => {
                  onFormulaChange(event.target.value);
                  updateCursor(event.currentTarget);
                }}
                onClick={(event) => updateCursor(event.currentTarget)}
                onKeyUp={(event) => updateCursor(event.currentTarget)}
                onSelect={(event) => updateCursor(event.currentTarget)}
                onKeyDown={(event) => {
                  if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
                    event.preventDefault();
                    onPreview();
                  }
                }}
                className="formula-code-textarea"
                placeholder={isZh ? "例如：if(pe_ttm > 0 && roe_ttm > 0.1, 1 / pe_ttm, 0)" : "Example: if(pe_ttm > 0 && roe_ttm > 0.1, 1 / pe_ttm, 0)"}
                autoSize={{ minRows: 15, maxRows: 25 }}
              />
            </div>

            <div className="formula-code-statusbar">
              <Space size="middle" wrap>
                <span className="formula-statusbar-lncol">Ln {cursor.line}, Col {cursor.column}</span>
                <span className="formula-statusbar-version">DSL {catalog?.dsl_version ?? "-"}</span>
                {validationTag}
                {previewCount !== null ? <span>{t("factorFormulaModalPreviewRows")}: {previewCount}</span> : null}
              </Space>
              <Space size="middle" wrap>
                <span>{isZh ? "依赖" : "Deps"}: {dependencyFields.length} {isZh ? "个字段" : "fields"}</span>
                <span>{isZh ? "最大回看" : "Max lookback"}: {maxLookback} {isZh ? "个交易日" : "trading days"}</span>
              </Space>
            </div>
          </div>

          {diagnostics.length ? (
            <Alert
              type="error"
              showIcon
              className="formula-diagnostics"
              message={isZh ? `发现 ${diagnostics.length} 个公式问题` : `${diagnostics.length} formula issue(s)`}
              description={
                <div className="formula-diagnostic-list">
                  {diagnostics.slice(0, 5).map((item, index) => (
                    <button
                      type="button"
                      key={`${item.error_code}-${index}`}
                      onClick={() => {
                        const start = item.start ?? 0;
                        const end = item.end ?? start;
                        requestAnimationFrame(() => {
                          const textarea = formulaTextAreaRef.current?.resizableTextArea?.textArea;
                          textarea?.focus();
                          textarea?.setSelectionRange(start, end);
                        });
                      }}
                    >
                      <code>{item.error_code}</code>
                      <span>{item.message}</span>
                      <small>{formatDiagnosticLocation(item, isZh)}</small>
                    </button>
                  ))}
                </div>
              }
            />
          ) : null}

          {previewResult?.is_valid ? (
            <section className="formula-preview-summary">
              <div className="formula-section-title">
                <strong>{isZh ? "预览摘要" : "Preview summary"}</strong>
                <Tag color={previewResult.coverage_rate >= 0.8 ? "success" : "warning"}>
                  {isZh ? "覆盖率" : "Coverage"} {(previewResult.coverage_rate * 100).toFixed(1)}%
                </Tag>
              </div>
              <Descriptions size="small" column={4}>
                <Descriptions.Item label={isZh ? "交易日" : "Trade date"}>{previewResult.selected_trade_date || "-"}</Descriptions.Item>
                <Descriptions.Item label={isZh ? "有效/尝试" : "Valid/attempted"}>{previewResult.valid_count}/{previewResult.attempted_count}</Descriptions.Item>
                <Descriptions.Item label={isZh ? "异常值" : "Outliers"}>{previewResult.outlier_count}</Descriptions.Item>
                <Descriptions.Item label={isZh ? "耗时" : "Elapsed"}>{previewResult.elapsed_ms.toFixed(0)} ms</Descriptions.Item>
              </Descriptions>
              {!previewResult.evaluation_supported ? (
                <Alert
                  type="warning"
                  showIcon
                  message={isZh ? "当前公式不能进入连续正式评价" : "This formula cannot enter continuous formal evaluation"}
                  description={isZh ? `需要使用 ${previewResult.evaluation_mode} 模式或先补齐阻断数据。` : `Use ${previewResult.evaluation_mode} mode or complete the blocked data first.`}
                />
              ) : null}
              {previewResult.blocking_fields?.length ? (
                <div className="formula-readiness-list">
                  {previewResult.blocking_fields.map((item) => (
                    <Tag color="error" key={item.field}>{item.field}: {item.reason ?? item.status_reason}</Tag>
                  ))}
                </div>
              ) : null}
              {previewResult.readiness_warnings?.length ? (
                <div className="formula-readiness-list">
                  {previewResult.readiness_warnings.map((item) => (
                    <Tag color="warning" key={`${item.field}-${item.availability}`}>{item.field}: {item.availability}</Tag>
                  ))}
                </div>
              ) : null}
            </section>
          ) : null}

          <Alert
            type="warning"
            showIcon
            message={isZh ? "编辑器预览只用于验证公式，不作为正式评价结论。" : "Editor preview validates the formula and is not a formal evaluation result."}
            className="formula-editor-notice"
          />

          <div className="formula-meta-section">
            <Form layout="vertical" size="small">
              <div className="formula-meta-grid">
                <Form.Item
                  label={<Space size={4}><span>{t("factorEditorDirection")}</span><Tooltip title={t("factorEditorDirectionTooltip")}><QuestionCircleOutlined /></Tooltip></Space>}
                  className="formula-meta-item"
                >
                  <Select value={versionDirection} onChange={onDirectionChange} options={directionOptions} />
                </Form.Item>
                <Form.Item label={t("factorEditorChangeNote")} className="formula-meta-item">
                  <Input value={changeNote} onChange={(event) => onChangeNoteChange(event.target.value)} placeholder={t("factorEditorChangeNotePlaceholder")} />
                </Form.Item>
              </div>
              <Form.Item label={t("factorEditorParams")} help={t("factorEditorParamsHelp")} style={{ marginBottom: 0 }}>
                <Input.TextArea value={paramsText} onChange={(event) => onParamsTextChange(event.target.value)} rows={3} className="formula-params-textarea" placeholder="{}" />
              </Form.Item>
            </Form>
          </div>
        </main>

        <aside className="formula-inspector-panel" aria-label={isZh ? "公式上下文帮助" : "Formula context help"}>
          <div className="formula-panel-heading">
            <div>
              <strong>{isZh ? "上下文帮助" : "Context help"}</strong>
              <span>{isZh ? "悬浮或聚焦即可查看用法" : "Hover or focus to inspect usage"}</span>
            </div>
          </div>
          {inspector}
          <div className="formula-inspector-runtime">
            <strong>{isZh ? "当前公式" : "Current formula"}</strong>
            <Descriptions size="small" column={1} colon={false}>
              <Descriptions.Item label={isZh ? "字段" : "Fields"}>{dependencyFields.length ? dependencyFields.join(", ") : "-"}</Descriptions.Item>
              <Descriptions.Item label={isZh ? "函数" : "Functions"}>{dependencyFunctions.length ? dependencyFunctions.join(", ") : "-"}</Descriptions.Item>
              <Descriptions.Item label={isZh ? "回看" : "Lookback"}>{maxLookback}</Descriptions.Item>
              <Descriptions.Item label={isZh ? "编译器" : "Compiler"}>{catalog?.compiler_version ?? "-"}</Descriptions.Item>
            </Descriptions>
          </div>
        </aside>
      </div>
    </Modal>
  );
}
