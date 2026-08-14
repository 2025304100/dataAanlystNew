import { useEffect, useRef, useState } from "react";
import type { TextAreaRef } from "antd/es/input/TextArea";
import {
  Alert,
  Button,
  Card,
  Collapse,
  Descriptions,
  Empty,
  Form,
  Input,
  InputNumber,
  Select,
  Space,
  Spin,
  Switch,
  Table,
  Tag,
  Tooltip,
  App,
  Typography,
} from "antd";
import {
  ArrowLeftOutlined,
  CheckCircleFilled,
  CheckCircleOutlined,
  CloseCircleFilled,
  CodeOutlined,
  EditOutlined,
  EyeOutlined,
  FileTextOutlined,
  InfoCircleOutlined,
  SafetyCertificateOutlined,
  SaveOutlined,
  SettingOutlined,
} from "@ant-design/icons";
import { api } from "../../api/client";
import type {
  FactorDefinition,
  FactorPreviewResult,
  FactorValidateResult,
  FactorPreviewValueItem,
} from "../../api/client";
import { useApp } from "../../context/AppContext";
import { t, factorLabel, factorCategoryLabel, factorDirectionLabel } from "../../i18n";
import FactorFormulaEditorModal from "./FactorFormulaEditorModal";
import AiChatDrawer from "../AiChatDrawer";
import "./FactorEditor.css";

type FactorEditorProps = {
  factorCode: string | null; // null = 新建草稿，非 null = 基于已有因子创建新版本
  onSaved: (code: string) => void;
  onBack: () => void;
  /** WP4-05: AI 草案应用到编辑器时的初始数据（仅新建草稿时生效） */
  initialPayload?: {
    code?: string;
    name?: string;
    category?: string;
    formula_expr?: string;
    params?: Record<string, unknown>;
    direction?: Direction;
    factor_kind?: FactorKind;
    risk_level?: RiskLevel;
    description?: string;
    thesis?: string;
    change_note?: string;
  } | null;
};

type Direction = "higher_better" | "lower_better" | "nonlinear";
type FactorKind = "continuous" | "event" | "regime";
type RiskLevel = "low" | "medium" | "high";
type WinsorizeMethod = "none" | "mad" | "quantile";
type NormalizeMethod = "none" | "zscore" | "rank";
type MissingPolicy = "exclude" | "impute_zero" | "ignore";

type PostprocessConfig = {
  winsorizeMethod: WinsorizeMethod;
  madMultiplier: number;
  lowerQ: number;
  upperQ: number;
  normalizeMethod: NormalizeMethod;
  zscoreDdof: number;
  rankAscending: boolean;
  missingPolicy: MissingPolicy;
};

const DEFAULT_POSTPROCESS: PostprocessConfig = {
  winsorizeMethod: "none",
  madMultiplier: 3.0,
  lowerQ: 0.01,
  upperQ: 0.99,
  normalizeMethod: "none",
  zscoreDdof: 0,
  rankAscending: true,
  missingPolicy: "exclude",
};

const FORMULA_TEMPLATES = [
  { key: "ep", name: "EP (盈利收益率)", formula: "1 / pe_ttm", category: "valuation" },
  { key: "turnover_z", name: "换手ZScore", formula: "sma(turnover_rate, 20)", category: "volume" },
  { key: "momentum", name: "动量", formula: "pct_change(close, 20)", category: "momentum" },
];

const DIRECTION_OPTIONS: { value: Direction; label: string }[] = [
  { value: "higher_better", label: t("factorEditorDirHigherBetter") },
  { value: "lower_better", label: t("factorEditorDirLowerBetter") },
  { value: "nonlinear", label: t("factorEditorDirNonlinear") },
];

const FACTOR_KIND_OPTIONS: { value: FactorKind; label: string }[] = [
  { value: "continuous", label: t("factorEditorKindContinuous") },
  { value: "event", label: t("factorEditorKindEvent") },
  { value: "regime", label: t("factorEditorKindRegime") },
];

const RISK_LEVEL_OPTIONS: { value: RiskLevel; label: string }[] = [
  { value: "low", label: t("factorEditorRiskLow") },
  { value: "medium", label: t("factorEditorRiskMedium") },
  { value: "high", label: t("factorEditorRiskHigh") },
];

const WINSORIZE_OPTIONS: { value: WinsorizeMethod; label: string }[] = [
  { value: "none", label: t("factorEditorWinsorNone") },
  { value: "mad", label: t("factorEditorWinsorMad") },
  { value: "quantile", label: t("factorEditorWinsorQuantile") },
];

const NORMALIZE_OPTIONS: { value: NormalizeMethod; label: string }[] = [
  { value: "none", label: t("factorEditorNormNone") },
  { value: "zscore", label: t("factorEditorNormZscore") },
  { value: "rank", label: t("factorEditorNormRank") },
];

const MISSING_POLICY_OPTIONS: { value: MissingPolicy; label: string }[] = [
  { value: "exclude", label: t("factorEditorMissingExclude") },
  { value: "impute_zero", label: t("factorEditorMissingImputeZero") },
  { value: "ignore", label: t("factorEditorMissingIgnore") },
];

function buildPostprocess(config: PostprocessConfig): Record<string, unknown> | null {
  const result: Record<string, unknown> = {};
  if (config.winsorizeMethod !== "none") {
    if (config.winsorizeMethod === "mad") {
      result.winsorize = { method: "mad", mad_multiplier: config.madMultiplier };
    } else {
      result.winsorize = { method: "quantile", lower_quantile: config.lowerQ, upper_quantile: config.upperQ };
    }
  }
  if (config.normalizeMethod === "zscore") {
    result.zscore = { ddof: config.zscoreDdof };
  } else if (config.normalizeMethod === "rank") {
    result.rank = { ascending: config.rankAscending };
  }
  result.missing_policy = config.missingPolicy;
  return Object.keys(result).length > 0 ? result : null;
}

function numText(v: unknown): string {
  if (v === null || v === undefined) return "-";
  if (typeof v === "number") return String(v);
  if (typeof v === "string") return v;
  try {
    return JSON.stringify(v);
  } catch {
    return String(v);
  }
}

// 返回有意义的字符串值，null/undefined 返回空串以便 || 回退
function valOrEmpty(v: unknown): string {
  if (v === null || v === undefined) return "";
  if (typeof v === "string") return v;
  if (typeof v === "number") return String(v);
  try {
    return JSON.stringify(v);
  } catch {
    return String(v);
  }
}

export default function FactorEditor({ factorCode, onSaved, onBack, initialPayload }: FactorEditorProps) {
  const ctx = useApp();
  const { message } = App.useApp();
  const isZh = ctx.locale.startsWith("zh");
  const isNewDraft = factorCode === null;

  const [loadingFactor, setLoadingFactor] = useState(false);
  const [factor, setFactor] = useState<FactorDefinition | null>(null);

  // 基本信息（仅新建草稿时使用）
  const [code, setCode] = useState("");
  const [name, setName] = useState("");
  const [category, setCategory] = useState("");
  const [direction, setDirection] = useState<Direction>("higher_better");
  const [factorKind, setFactorKind] = useState<FactorKind>("continuous");
  const [riskLevel, setRiskLevel] = useState<RiskLevel>("low");
  const [description, setDescription] = useState("");
  const [thesis, setThesis] = useState("");

  // 公式编辑
  const [formulaExpr, setFormulaExpr] = useState("");
  const [aiFormulaOpen, setAiFormulaOpen] = useState(false);
  const [formulaEditorOpen, setFormulaEditorOpen] = useState(false);
  const formulaTextAreaRef = useRef<TextAreaRef>(null);
  const formulaSelectionRef = useRef({ start: Number.MAX_SAFE_INTEGER, end: Number.MAX_SAFE_INTEGER });
  const formulaEditorSnapshotRef = useRef<{
    formulaExpr: string;
    versionDirection: Direction;
    changeNote: string;
    paramsText: string;
    validateResult: FactorValidateResult | null;
    previewResult: FactorPreviewResult | null;
  } | null>(null);
  const [versionDirection, setVersionDirection] = useState<Direction>("higher_better");
  const [changeNote, setChangeNote] = useState("");
  const [paramsText, setParamsText] = useState("{}");

  // 后处理
  const [postprocess, setPostprocess] = useState<PostprocessConfig>(DEFAULT_POSTPROCESS);

  // 校验
  const [validating, setValidating] = useState(false);
  const [validateResult, setValidateResult] = useState<FactorValidateResult | null>(null);

  // 预览
  const [previewing, setPreviewing] = useState(false);
  const [previewResult, setPreviewResult] = useState<FactorPreviewResult | null>(null);

  // 保存
  const [saving, setSaving] = useState(false);

  // 加载已有因子基本信息（只读展示 + 默认值预填）
  useEffect(() => {
    if (factorCode === null) {
      setFactor(null);
      return;
    }
    let cancelled = false;
    setLoadingFactor(true);
    Promise.all([
      api.getFactorDefinition(factorCode),
      api.listFactorVersions(factorCode),
    ])
      .then(([def, versions]) => {
        if (cancelled) return;
        const latestVersion = versions.find((version) => version.is_latest) ?? versions[versions.length - 1];
        setFactor(def);
        if (
          latestVersion?.direction === "higher_better" ||
          latestVersion?.direction === "lower_better" ||
          latestVersion?.direction === "nonlinear"
        ) {
          setVersionDirection(latestVersion.direction as Direction);
        } else if (
          def.direction === "higher_better" ||
          def.direction === "lower_better" ||
          def.direction === "nonlinear"
        ) {
          setVersionDirection(def.direction as Direction);
        }
        const currentFormula = latestVersion?.formula_expr || def.formula_expr;
        if (currentFormula) {
          setFormulaExpr(currentFormula);
        }
        if (latestVersion?.params) {
          setParamsText(JSON.stringify(latestVersion.params, null, 2));
        }
        if (
          def.default_missing_policy === "exclude" ||
          def.default_missing_policy === "impute_zero" ||
          def.default_missing_policy === "ignore"
        ) {
          setPostprocess((prev) => ({
            ...prev,
            missingPolicy: def.default_missing_policy as MissingPolicy,
          }));
        }
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        const msg = err instanceof Error ? err.message : String(err);
        message.error(isZh ? `加载因子失败: ${msg}` : `Failed to load factor: ${msg}`);
      })
      .finally(() => {
        if (!cancelled) setLoadingFactor(false);
      });
    return () => {
      cancelled = true;
    };
  }, [factorCode, isZh, message]);

  // WP4-05: AI 草案应用到编辑器时预填表单（仅新建草稿时生效）
  useEffect(() => {
    if (factorCode !== null || !initialPayload) return;
    if (initialPayload.code) setCode(initialPayload.code);
    if (initialPayload.name) setName(initialPayload.name);
    if (initialPayload.category) setCategory(initialPayload.category);
    if (initialPayload.formula_expr) setFormulaExpr(initialPayload.formula_expr);
    if (initialPayload.params) {
      try {
        setParamsText(JSON.stringify(initialPayload.params, null, 2));
      } catch {
        // ignore
      }
    }
    if (initialPayload.direction) {
      setDirection(initialPayload.direction);
      setVersionDirection(initialPayload.direction);
    }
    if (initialPayload.factor_kind) setFactorKind(initialPayload.factor_kind);
    if (initialPayload.risk_level) setRiskLevel(initialPayload.risk_level);
    if (initialPayload.description) setDescription(initialPayload.description);
    if (initialPayload.thesis) setThesis(initialPayload.thesis);
    if (initialPayload.change_note) setChangeNote(initialPayload.change_note);
    // 仅在首次应用时提示
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialPayload]);

  const updatePostprocess = <K extends keyof PostprocessConfig>(
    key: K,
    value: PostprocessConfig[K],
  ) => {
    setPostprocess((prev) => ({ ...prev, [key]: value }));
  };

  // 解析 params JSON
  const parseParams = (): Record<string, unknown> | null => {
    try {
      const parsed = JSON.parse(paramsText || "{}");
      if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
        message.error(isZh ? "params 必须是 JSON 对象" : "params must be a JSON object");
        return null;
      }
      return parsed as Record<string, unknown>;
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      message.error(isZh ? `params JSON 解析失败: ${msg}` : `params JSON parse failed: ${msg}`);
      return null;
    }
  };

  // 校验公式
  const handleValidate = () => {
    if (!formulaExpr.trim()) {
      message.warning(isZh ? "请输入公式" : "Please enter formula");
      return;
    }
    const params = parseParams();
    if (params === null) return;
    const postprocessConfig = buildPostprocess(postprocess);
    setValidating(true);
    api
      .validateFactorFormula({
        formula_expr: formulaExpr,
        params,
        direction: versionDirection,
        postprocess: postprocessConfig,
      })
      .then((res) => {
        setValidateResult(res);
        if (res.is_valid) {
          message.success(isZh ? "校验通过" : "Validation passed");
        } else {
          message.warning(
            isZh
              ? `校验未通过，共 ${res.errors.length} 个错误`
              : `Validation failed with ${res.errors.length} error(s)`,
          );
        }
      })
      .catch((err: unknown) => {
        const msg = err instanceof Error ? err.message : String(err);
        message.error(isZh ? `校验请求失败: ${msg}` : `Validate request failed: ${msg}`);
      })
      .finally(() => setValidating(false));
  };

  // 预览
  const handlePreview = () => {
    if (!formulaExpr.trim()) {
      message.warning(isZh ? "请输入公式" : "Please enter formula");
      return;
    }
    const params = parseParams();
    if (params === null) return;
    const postprocessConfig = buildPostprocess(postprocess);
    setPreviewing(true);
    api
      .previewFactorFormula({
        formula_expr: formulaExpr,
        params,
        direction: versionDirection,
        postprocess: postprocessConfig,
      })
      .then((res) => {
        setPreviewResult(res);
        if (!res.is_valid) {
          message.warning(
            isZh
              ? `预览校验未通过，共 ${res.errors.length} 个错误`
              : `Preview validation failed with ${res.errors.length} error(s)`,
          );
        } else {
          message.success(
            isZh ? `预览完成，共 ${res.values.length} 条` : `Preview done, ${res.values.length} rows`,
          );
        }
      })
      .catch((err: unknown) => {
        const msg = err instanceof Error ? err.message : String(err);
        message.error(isZh ? `预览请求失败: ${msg}` : `Preview request failed: ${msg}`);
      })
      .finally(() => setPreviewing(false));
  };

  // 保存
  const handleSave = () => {
    if (!formulaExpr.trim()) {
      message.warning(isZh ? "请输入公式" : "Please enter formula");
      return;
    }
    if (!validateResult || !validateResult.is_valid) {
      message.error(
        isZh ? "请先点击“校验公式”并修复全部错误，再保存可运行版本" : "Validate and fix all formula errors before saving a runnable version",
      );
      return;
    }
    const params = parseParams();
    if (params === null) return;
    const postprocessConfig = buildPostprocess(postprocess);

    setSaving(true);

    const versionPayload = {
      formula_expr: formulaExpr,
      params: params ?? {},
      direction: versionDirection,
      postprocess: postprocessConfig,
      change_note: changeNote || undefined,
      created_via: "manual" as const,
    };

    const finish = (targetCode: string) =>
      api
        .createFactorVersion(targetCode, versionPayload)
        .then(() => {
          message.success(isZh ? "保存成功" : "Saved successfully");
          onSaved(targetCode);
        });

    if (isNewDraft) {
      if (!code.trim() || !name.trim() || !category.trim()) {
        message.warning(isZh ? "请填写 code / name / category" : "Please fill in code / name / category");
        setSaving(false);
        return;
      }
      const draftPayload = {
        code: code.trim(),
        name: name.trim(),
        category: category.trim(),
        direction,
        factor_kind: factorKind,
        risk_level: riskLevel,
        description: description || undefined,
        thesis: thesis || undefined,
        asset_scope: ["cn-stock"],
        default_missing_policy: postprocess.missingPolicy,
      };
      // 草稿创建成功后再创建版本，失败时明确提示是"版本"失败还是"草稿"失败，避免误导
      let draftCreated = false;
      api
        .createFactorDraft(draftPayload)
        .then((def) => {
          draftCreated = true;
          return finish(def.code);
        })
        .catch((err: unknown) => {
          const msg = err instanceof Error ? err.message : String(err);
          if (draftCreated) {
            // 草稿已经建成功，是创建版本时失败；提示用户草稿已存在可进入详情再追加版本
            message.error(
              isZh
                ? `草稿创建成功，但创建版本失败: ${msg}（可在因子详情中追加版本）`
                : `Draft created, but version creation failed: ${msg}`,
            );
          } else {
            message.error(isZh ? `创建因子失败: ${msg}` : `Create factor failed: ${msg}`);
          }
        })
        .finally(() => setSaving(false));
    } else {
      finish(factorCode as string)
        .catch((err: unknown) => {
          const msg = err instanceof Error ? err.message : String(err);
          message.error(isZh ? `创建版本失败: ${msg}` : `Create version failed: ${msg}`);
        })
        .finally(() => setSaving(false));
    }
  };

  // 应用模板
  const applyTemplate = (tmpl: (typeof FORMULA_TEMPLATES)[number]) => {
    setFormulaExpr(tmpl.formula);
    if (isNewDraft) {
      setCategory(tmpl.category);
    }
    setValidateResult(null);
    setPreviewResult(null);
    message.info(isZh ? `已应用模板: ${tmpl.name}` : `Applied template: ${tmpl.name}`);
  };

  const handleFormulaChange = (value: string) => {
    setFormulaExpr(value);
    // 校验结果只对应当时的公式文本；编辑后必须重新校验，不能复用旧绿灯。
    setValidateResult(null);
    setPreviewResult(null);
  };

  const openFormulaEditor = () => {
    formulaEditorSnapshotRef.current = {
      formulaExpr,
      versionDirection,
      changeNote,
      paramsText,
      validateResult,
      previewResult,
    };
    formulaSelectionRef.current = { start: formulaExpr.length, end: formulaExpr.length };
    setFormulaEditorOpen(true);
  };

  const cancelFormulaEditor = () => {
    const snapshot = formulaEditorSnapshotRef.current;
    if (snapshot) {
      setFormulaExpr(snapshot.formulaExpr);
      setVersionDirection(snapshot.versionDirection);
      setChangeNote(snapshot.changeNote);
      setParamsText(snapshot.paramsText);
      setValidateResult(snapshot.validateResult);
      setPreviewResult(snapshot.previewResult);
    }
    formulaEditorSnapshotRef.current = null;
    setFormulaEditorOpen(false);
  };

  const applyFormulaEditor = () => {
    formulaEditorSnapshotRef.current = null;
    setFormulaEditorOpen(false);
    message.success(
      isZh
        ? "公式修改已应用到当前草稿，请校验后再保存版本"
        : "Formula changes applied to the draft. Validate before saving the version.",
    );
  };

  // 预览表格列
  const rememberFormulaSelection = (textarea: HTMLTextAreaElement) => {
    formulaSelectionRef.current = {
      start: textarea.selectionStart ?? textarea.value.length,
      end: textarea.selectionEnd ?? textarea.value.length,
    };
  };

  const focusFormulaSelection = (start: number, end: number = start) => {
    requestAnimationFrame(() => {
      const textarea = formulaTextAreaRef.current?.resizableTextArea?.textArea;
      if (!textarea) return;
      textarea.focus();
      textarea.setSelectionRange(start, end);
      formulaSelectionRef.current = { start, end };
    });
  };

  const insertFormulaSnippet = (snippet: string) => {
    const currentSelection = formulaSelectionRef.current;
    let start = Math.min(currentSelection.start, formulaExpr.length);
    const end = Math.min(Math.max(currentSelection.end, start), formulaExpr.length);
    let selectedExpression = formulaExpr.slice(start, end);

    // 函数默认以 close 作为示例参数；若用户选中了表达式，或光标刚好位于字段后，自动用它替换示例参数。
    if (!selectedExpression && snippet.includes("close")) {
      const precedingIdentifier = formulaExpr.slice(0, start).match(/[A-Za-z_][A-Za-z0-9_]*$/)?.[0];
      if (precedingIdentifier) {
        selectedExpression = precedingIdentifier;
        start -= precedingIdentifier.length;
      }
    }

    const insertion = selectedExpression && snippet.includes("close")
      ? snippet.replace("close", selectedExpression)
      : snippet;
    const nextFormula = `${formulaExpr.slice(0, start)}${insertion}${formulaExpr.slice(end)}`;
    handleFormulaChange(nextFormula);

    const placeholderStart = insertion.indexOf("close");
    if (!selectedExpression && placeholderStart >= 0 && insertion !== "close") {
      focusFormulaSelection(start + placeholderStart, start + placeholderStart + "close".length);
    } else if (insertion === "()") {
      focusFormulaSelection(start + 1);
    } else {
      focusFormulaSelection(start + insertion.length);
    }
  };

  const useFormulaExample = (formula: string) => {
    handleFormulaChange(formula);
    formulaSelectionRef.current = { start: formula.length, end: formula.length };
    focusFormulaSelection(formula.length);
    message.success(t("factorFormulaBuilderApplied"));
  };

  const previewColumns = [
    {
      title: "symbol",
      dataIndex: "symbol",
      key: "symbol",
      width: 110,
    },
    {
      title: "trade_date",
      dataIndex: "trade_date",
      key: "trade_date",
      width: 110,
    },
    {
      title: "raw_value",
      dataIndex: "raw_value",
      key: "raw_value",
      width: 110,
      render: (v: number | null) => (v === null ? "-" : v.toFixed(6)),
    },
    {
      title: "winsorized_value",
      dataIndex: "winsorized_value",
      key: "winsorized_value",
      width: 130,
      render: (v: number | null) => (v === null ? "-" : v.toFixed(6)),
    },
    {
      title: "normalized_value",
      dataIndex: "normalized_value",
      key: "normalized_value",
      width: 140,
      render: (v: number | null) => (v === null ? "-" : v.toFixed(6)),
    },
    {
      title: "eligible",
      dataIndex: "eligible",
      key: "eligible",
      width: 80,
      render: (v: boolean) =>
        v ? <Tag color="green">✓</Tag> : <Tag color="red">✗</Tag>,
    },
  ];

  // 校验结果中 data_dependencies 渲染
  const renderDataDependencies = (dep: Record<string, unknown> | null) => {
    if (!dep) return <Empty description={isZh ? "无依赖信息" : "No dependencies"} />;
    const entries = Object.entries(dep);
    if (entries.length === 0) {
      return <Empty description={isZh ? "无依赖信息" : "No dependencies"} />;
    }
    // 若存在数组型 dependencies，按表格渲染
    const arr = Array.isArray(dep.dependencies)
      ? (dep.dependencies as Array<Record<string, unknown>>)
      : null;
    if (arr && arr.length > 0) {
      return (
        <Table
          size="small"
          rowKey={(_, idx) => String(idx)}
          pagination={false}
          dataSource={arr}
          columns={[
            { title: "field", dataIndex: "field", key: "field", render: (v: unknown) => numText(v) },
            { title: "source_table", dataIndex: "source_table", key: "source_table", render: (v: unknown) => numText(v) },
            { title: "pit_field", dataIndex: "pit_field", key: "pit_field", render: (v: unknown) => numText(v) },
            { title: "max_lookback", dataIndex: "max_lookback", key: "max_lookback", render: (v: unknown) => numText(v) },
          ]}
        />
      );
    }
    return (
      <Descriptions size="small" column={1} bordered>
        {entries.map(([k, v]) => (
          <Descriptions.Item key={k} label={k}>
            {numText(v)}
          </Descriptions.Item>
        ))}
      </Descriptions>
    );
  };

  // execution_plan 渲染
  const renderExecutionPlan = (plan: Record<string, unknown> | null) => {
    if (!plan) return <Empty description={isZh ? "无执行计划" : "No execution plan"} />;
    const knownKeys = ["compiler_version", "execution_plan_hash", "complexity_score"];
    return (
      <Descriptions size="small" column={1} bordered>
        {knownKeys.map((k) => (
          <Descriptions.Item key={k} label={k}>
            {numText((plan as Record<string, unknown>)[k])}
          </Descriptions.Item>
        ))}
        {Object.entries(plan)
          .filter(([k]) => !knownKeys.includes(k))
          .map(([k, v]) => (
            <Descriptions.Item key={k} label={k}>
              {numText(v)}
            </Descriptions.Item>
          ))}
      </Descriptions>
    );
  };

  return (
    <div className="factor-editor-redesign">
      <Spin spinning={loadingFactor}>
        {/* 顶部操作栏 */}
        <div className="factor-editor-header">
          <div className="factor-editor-header-left">
            <Button
              className="factor-editor-back-btn"
              icon={<ArrowLeftOutlined />}
              onClick={onBack}
              type="text"
            >
              {t("factorEditorBack")}
            </Button>
            <div className="factor-editor-title-block">
              <h2 className="factor-editor-title">
                {isNewDraft
                  ? t("factorEditorTitleNew")
                  : `${t("factorEditorTitleVersion")} · ${factorCode}`}
              </h2>
              <span className="factor-editor-subtitle">
                {isNewDraft
                  ? t("factorEditorSubtitleNew") || "创建新的因子表达式"
                  : factorLabel(factor?.code, factor?.name)}
              </span>
            </div>
          </div>
          <Button
            type="primary"
            icon={<SaveOutlined />}
            loading={saving}
            onClick={handleSave}
            className="factor-editor-save-btn"
          >
            {t("factorEditorSave")}
          </Button>
        </div>

        {/* 已有因子只读信息（非新建草稿时显示） */}
        {!isNewDraft && factor ? (
          <div className="factor-editor-info-card">
            <div className="factor-editor-info-header">
              <span className="factor-editor-info-title">{t("factorEditorFactorInfo") || "因子信息"}</span>
              <Tag icon={<InfoCircleOutlined />} color="blue">{factor.factor_kind ?? "alpha"}</Tag>
            </div>
            <Descriptions size="small" column={3} bordered className="factor-editor-info-desc">
              <Descriptions.Item label={t("factorColCode")}>{factor.code}</Descriptions.Item>
              <Descriptions.Item label={t("factorColName")}>{factorLabel(factor.code, factor.name)}</Descriptions.Item>
              <Descriptions.Item label={t("factorColCategory")}>{factorCategoryLabel(factor.category)}</Descriptions.Item>
              <Descriptions.Item label={t("factorColDirection")}>{factorDirectionLabel(factor.direction)}</Descriptions.Item>
              <Descriptions.Item label={t("factorColKind")}>{factor.factor_kind ?? "-"}</Descriptions.Item>
              <Descriptions.Item label={t("factorColRiskLevel")}>{factor.risk_level ?? "-"}</Descriptions.Item>
              <Descriptions.Item label={t("factorColDescription")} span={3}>
                {factor.description ?? "-"}
              </Descriptions.Item>
              <Descriptions.Item label={t("factorColThesis")} span={3}>
                {factor.thesis ?? "-"}
              </Descriptions.Item>
            </Descriptions>
          </div>
        ) : null}

        <div className="factor-editor-layout">
          {/* 左栏：基本信息 + 公式编辑 */}
          <div className="factor-editor-main">
            {/* 基本信息（仅新建草稿时显示） */}
            {isNewDraft ? (
              <div className="factor-editor-card factor-editor-basic-card">
                <div className="factor-editor-card-header">
                  <div className="factor-editor-card-icon factor-editor-card-icon--purple">
                    <FileTextOutlined />
                  </div>
                  <div className="factor-editor-card-title-wrap">
                    <h3 className="factor-editor-card-title">{t("factorEditorBasicInfo")}</h3>
                    <span className="factor-editor-card-desc">{t("factorEditorBasicInfoDesc") || "填写因子的基础属性"}</span>
                  </div>
                </div>
                <div className="factor-editor-card-body">
                  <Form layout="vertical" size="small">
                  <div className="factor-editor-grid">
                    <Form.Item label={t("factorEditorCode")} required>
                      <Input
                        value={code}
                        onChange={(e) => setCode(e.target.value.toLowerCase().replace(/[^a-z0-9_]/g, "_"))}
                        placeholder="ep_ttm"
                      />
                    </Form.Item>
                    <Form.Item label={t("factorEditorName")} required>
                      <Input value={name} onChange={(e) => setName(e.target.value)} placeholder={t("factorEditorNamePlaceholder")} />
                    </Form.Item>
                    <Form.Item label={t("factorEditorCategory")} required>
                      <Input
                        value={category}
                        onChange={(e) => setCategory(e.target.value)}
                        placeholder={t("factorEditorCategoryPlaceholder")}
                      />
                    </Form.Item>
                    <Form.Item label={t("factorEditorDirection")}>
                      <Select
                        value={direction}
                        onChange={(v: Direction) => setDirection(v)}
                        options={DIRECTION_OPTIONS}
                      />
                    </Form.Item>
                    <Form.Item label={t("factorEditorFactorKind")}>
                      <Select
                        value={factorKind}
                        onChange={(v: FactorKind) => setFactorKind(v)}
                        options={FACTOR_KIND_OPTIONS}
                      />
                    </Form.Item>
                    <Form.Item label={t("factorEditorRiskLevel")}>
                      <Select
                        value={riskLevel}
                        onChange={(v: RiskLevel) => setRiskLevel(v)}
                        options={RISK_LEVEL_OPTIONS}
                      />
                    </Form.Item>
                  </div>
                  <Form.Item label={t("factorEditorDescription")}>
                    <Input.TextArea
                      value={description}
                      onChange={(e) => setDescription(e.target.value)}
                      rows={2}
                      autoSize={{ minRows: 2, maxRows: 4 }}
                    />
                  </Form.Item>
                  <Form.Item label={t("factorEditorThesis")} style={{ marginBottom: 0 }}>
                    <Input.TextArea
                      value={thesis}
                      onChange={(e) => setThesis(e.target.value)}
                      rows={2}
                      autoSize={{ minRows: 2, maxRows: 4 }}
                    />
                  </Form.Item>
                </Form>
              </div>
              </div>
            ) : null}

            {/* 公式摘要：完整编辑迁移到弹窗，避免与校验/预览/后处理纵向堆叠 */}
            <div className="factor-editor-card factor-editor-formula-card">
              <div className="factor-editor-card-header">
                <div className="factor-editor-card-icon factor-editor-card-icon--indigo">
                  <CodeOutlined />
                </div>
                <div className="factor-editor-card-title-wrap">
                  <h3 className="factor-editor-card-title">{t("factorEditorFormula")}</h3>
                  <span className="factor-editor-card-desc">{t("factorEditorFormulaDesc") || "定义因子的计算表达式"}</span>
                </div>
                <Button
                  size="middle"
                  type="primary"
                  icon={<EditOutlined />}
                  onClick={openFormulaEditor}
                  data-testid="factor-open-formula-modal"
                  className="factor-editor-edit-formula-btn"
                >
                  {t("factorFormulaModalOpen")}
                </Button>
              </div>
              <div className="factor-editor-card-body">
                <div className="factor-formula-summary">
                  <div className="factor-formula-summary__code">
                    <div className="metric-label">{t("factorEditorFormulaExpr")}</div>
                    <code>{formulaExpr || t("factorEditorFormulaPlaceholder")}</code>
                  </div>
                  <div className="factor-formula-tags">
                    <Tag className="factor-formula-tag--direction">{factorDirectionLabel(versionDirection)}</Tag>
                    {validateResult?.is_valid ? (
                      <Tag icon={<CheckCircleFilled />} color="success" className="factor-formula-tag">{t("factorEditorValid")}</Tag>
                    ) : validateResult ? (
                      <Tag icon={<CloseCircleFilled />} color="error" className="factor-formula-tag">{t("factorEditorInvalid")}</Tag>
                    ) : (
                      <Tag className="factor-formula-tag">{t("factorFormulaModalNeedsValidation")}</Tag>
                    )}
                    {previewResult ? (
                      <Tag color="blue" className="factor-formula-tag">{previewResult.values.length} {t("factorFormulaModalPreviewRows")}</Tag>
                    ) : null}
                  </div>
                  <Alert
                    type="info"
                    showIcon
                    message={t("factorFormulaModalCompatibilityTitle")}
                    description={t("factorFormulaModalCompatibilityDescription")}
                    className="factor-formula-compat-alert"
                  />
                </div>
                <div className="factor-formula-actions">
                  <Button
                    icon={<SafetyCertificateOutlined />}
                    loading={validating}
                    onClick={handleValidate}
                    className="factor-formula-action-btn"
                  >
                    {t("factorEditorValidate")}
                  </Button>
                  <Button icon={<EyeOutlined />} loading={previewing} onClick={handlePreview} className="factor-formula-action-btn">
                    {t("factorEditorPreview")}
                  </Button>
                  <Button type="primary" ghost icon={<EditOutlined />} onClick={openFormulaEditor} className="factor-formula-action-btn">
                    {t("factorFormulaModalOpen")}
                  </Button>
                </div>
              </div>
            </div>
          </div>
          {/* 右栏：校验结果 + 预览结果 + 后处理配置 */}
          <div className="factor-editor-side">
            {/* 校验结果 */}
            <div className="factor-editor-card factor-editor-validate-card">
              <div className="factor-editor-card-header factor-editor-card-header--compact">
                <div className="factor-editor-card-icon factor-editor-card-icon--green">
                  <CheckCircleOutlined />
                </div>
                <div className="factor-editor-card-title-wrap">
                  <h3 className="factor-editor-card-title">{t("factorEditorValidateResult")}</h3>
                </div>
                {validateResult ? (
                  validateResult.is_valid ? (
                    <Tag icon={<CheckCircleFilled />} color="success">{t("factorEditorValid")}</Tag>
                  ) : (
                    <Tag icon={<CloseCircleFilled />} color="error">{t("factorEditorInvalid")}</Tag>
                  )
                ) : null}
              </div>
              <div className="factor-editor-card-body">
                {validateResult ? (
                  <div>
                    {validateResult.errors && validateResult.errors.length > 0 ? (
                      <div className="factor-editor-errors">
                        {validateResult.errors.map((err, idx) => (
                          <Alert
                            key={idx}
                            type="error"
                            showIcon
                            className="factor-editor-error-alert"
                            message={valOrEmpty(err.error_code) || valOrEmpty(err.code) || `Error ${idx + 1}`}
                            description={valOrEmpty(err.message) || valOrEmpty(err.detail) || numText(err)}
                          />
                        ))}
                      </div>
                    ) : null}
                    <Collapse
                      size="small"
                      className="factor-editor-collapse"
                      items={[
                        {
                          key: "dep",
                          label: t("factorEditorDataDependencies"),
                          children: renderDataDependencies(validateResult.data_dependencies ?? null),
                        },
                        {
                          key: "plan",
                          label: t("factorEditorExecutionPlan"),
                          children: renderExecutionPlan(validateResult.execution_plan ?? null),
                        },
                      ]}
                    />
                  </div>
                ) : (
                  <Empty description={t("factorEditorNoValidateResult")} className="factor-editor-empty" />
                )}
              </div>
            </div>

            {/* 预览结果 */}
            <div className="factor-editor-card factor-editor-preview-card">
              <div className="factor-editor-card-header factor-editor-card-header--compact">
                <div className="factor-editor-card-icon factor-editor-card-icon--blue">
                  <EyeOutlined />
                </div>
                <div className="factor-editor-card-title-wrap">
                  <h3 className="factor-editor-card-title">{t("factorEditorPreviewResult")}</h3>
                </div>
                {previewResult ? (
                  <Tag color="blue">{previewResult.values.length} 行</Tag>
                ) : null}
              </div>
              <div className="factor-editor-card-body">
                {previewResult ? (
                  <div>
                    <Descriptions size="small" column={1} bordered className="factor-editor-preview-meta">
                      <Descriptions.Item label="selected_trade_date">
                        {previewResult.selected_trade_date ?? "-"}
                      </Descriptions.Item>
                      <Descriptions.Item label="data_cutoff_at">
                        {previewResult.data_cutoff_at ?? "-"}
                      </Descriptions.Item>
                      {previewResult.complete_trade_day_evidence ? (
                        <Descriptions.Item label="complete_trade_day_evidence">
                          {numText(previewResult.complete_trade_day_evidence)}
                        </Descriptions.Item>
                      ) : null}
                    </Descriptions>
                    {previewResult.errors && previewResult.errors.length > 0 ? (
                      <div className="factor-editor-errors">
                        {previewResult.errors.map((err, idx) => (
                          <Alert
                            key={idx}
                            type="error"
                            showIcon
                            className="factor-editor-error-alert"
                            message={valOrEmpty(err.error_code) || valOrEmpty(err.code) || `Error ${idx + 1}`}
                            description={valOrEmpty(err.message) || valOrEmpty(err.detail) || numText(err)}
                          />
                        ))}
                      </div>
                    ) : null}
                    <div className="factor-editor-preview-values-header">
                      <span>{t("factorEditorPreviewValues")}</span>
                      <Tag color="blue">{previewResult.values.length}</Tag>
                    </div>
                    <Table<FactorPreviewValueItem>
                      size="small"
                      rowKey={(r) => `${r.symbol}-${r.trade_date}`}
                      pagination={{ pageSize: 8, size: "small" }}
                      dataSource={previewResult.values}
                      columns={previewColumns}
                      scroll={{ x: "max-content" }}
                      className="factor-editor-preview-table"
                    />
                  {previewResult.missing_reasons &&
                  Object.keys(previewResult.missing_reasons).length > 0 ? (
                    <div style={{ marginTop: 8 }}>
                      <Typography.Text strong style={{ display: "block", marginBottom: 4 }}>
                        {t("factorEditorMissingReasons")}
                      </Typography.Text>
                      <Descriptions size="small" column={1} bordered>
                        {Object.entries(previewResult.missing_reasons).map(([k, v]) => (
                          <Descriptions.Item key={k} label={k}>
                            {v}
                          </Descriptions.Item>
                        ))}
                      </Descriptions>
                    </div>
                  ) : null}
                </div>
              ) : (
                <Empty description={t("factorEditorNoPreviewResult")} className="factor-editor-empty" />
              )}
              </div>
            </div>

            {/* 后处理配置 */}
            <div className="factor-editor-card factor-editor-postprocess-card">
              <div className="factor-editor-card-header factor-editor-card-header--compact">
                <div className="factor-editor-card-icon factor-editor-card-icon--orange">
                  <SettingOutlined />
                </div>
                <div className="factor-editor-card-title-wrap">
                  <h3 className="factor-editor-card-title">{t("factorEditorPostprocess")}</h3>
                </div>
              </div>
              <div className="factor-editor-card-body">
                <Collapse
                  size="small"
                  defaultActiveKey={["postprocess"]}
                  className="factor-editor-collapse factor-editor-postprocess-collapse"
                  items={[
                    {
                      key: "postprocess",
                      label: t("factorEditorPostprocessConfig") || "后处理参数配置",
                      children: (
                      <Form layout="vertical" size="small">
                        <Form.Item label={t("factorEditorWinsorMethod")}>
                          <Select
                            value={postprocess.winsorizeMethod}
                            onChange={(v: WinsorizeMethod) => updatePostprocess("winsorizeMethod", v)}
                            options={WINSORIZE_OPTIONS}
                          />
                        </Form.Item>
                        {postprocess.winsorizeMethod === "mad" ? (
                          <Form.Item label={t("factorEditorWinsorMadMultiplier")}>
                            <InputNumber
                              value={postprocess.madMultiplier}
                              onChange={(v) => updatePostprocess("madMultiplier", Number(v ?? 3.0))}
                              step={0.1}
                              min={0}
                              style={{ width: "100%" }}
                            />
                          </Form.Item>
                        ) : null}
                        {postprocess.winsorizeMethod === "quantile" ? (
                          <div className="factor-editor-grid">
                            <Form.Item label={t("factorEditorWinsorLowerQ")}>
                              <InputNumber
                                value={postprocess.lowerQ}
                                onChange={(v) => updatePostprocess("lowerQ", Number(v ?? 0.01))}
                                step={0.01}
                                min={0}
                                max={1}
                                style={{ width: "100%" }}
                              />
                            </Form.Item>
                            <Form.Item label={t("factorEditorWinsorUpperQ")}>
                              <InputNumber
                                value={postprocess.upperQ}
                                onChange={(v) => updatePostprocess("upperQ", Number(v ?? 0.99))}
                                step={0.01}
                                min={0}
                                max={1}
                                style={{ width: "100%" }}
                              />
                            </Form.Item>
                          </div>
                        ) : null}
                        <Form.Item label={t("factorEditorNormMethod")}>
                          <Select
                            value={postprocess.normalizeMethod}
                            onChange={(v: NormalizeMethod) => updatePostprocess("normalizeMethod", v)}
                            options={NORMALIZE_OPTIONS}
                          />
                        </Form.Item>
                        {postprocess.normalizeMethod === "zscore" ? (
                          <Form.Item label={t("factorEditorZscoreDdof")}>
                            <InputNumber
                              value={postprocess.zscoreDdof}
                              onChange={(v) => updatePostprocess("zscoreDdof", Number(v ?? 0))}
                              step={1}
                              min={0}
                              style={{ width: "100%" }}
                            />
                          </Form.Item>
                        ) : null}
                        {postprocess.normalizeMethod === "rank" ? (
                          <Form.Item label={t("factorEditorRankAscending")}>
                            <Switch
                              checked={postprocess.rankAscending}
                              onChange={(v) => updatePostprocess("rankAscending", v)}
                            />
                          </Form.Item>
                        ) : null}
                        <Form.Item label={t("factorEditorMissingPolicy")} style={{ marginBottom: 0 }}>
                          <Select
                            value={postprocess.missingPolicy}
                            onChange={(v: MissingPolicy) => updatePostprocess("missingPolicy", v)}
                            options={MISSING_POLICY_OPTIONS}
                          />
                        </Form.Item>
                      </Form>
                    ),
                  },
                ]}
                />
              </div>
            </div>
          </div>
        </div>
      </Spin>
      <FactorFormulaEditorModal
        open={formulaEditorOpen}
        isZh={isZh}
        factorCode={factorCode}
        formulaExpr={formulaExpr}
        formulaTextAreaRef={formulaTextAreaRef}
        versionDirection={versionDirection}
        changeNote={changeNote}
        paramsText={paramsText}
        templates={FORMULA_TEMPLATES}
        validating={validating}
        previewing={previewing}
        validationState={validateResult ? (validateResult.is_valid ? "valid" : "invalid") : "idle"}
        previewCount={previewResult ? previewResult.values.length : null}
        directionOptions={DIRECTION_OPTIONS}
        onCancel={cancelFormulaEditor}
        onApply={applyFormulaEditor}
        onAskAi={() => setAiFormulaOpen(true)}
        onValidate={handleValidate}
        onPreview={handlePreview}
        onInsert={insertFormulaSnippet}
        onUseExample={useFormulaExample}
        onApplyTemplate={applyTemplate}
        onFormulaChange={handleFormulaChange}
        onRememberSelection={rememberFormulaSelection}
        onDirectionChange={(value) => {
          setVersionDirection(value);
          setValidateResult(null);
          setPreviewResult(null);
        }}
        onChangeNoteChange={setChangeNote}
        onParamsTextChange={(value) => {
          setParamsText(value);
          setValidateResult(null);
          setPreviewResult(null);
        }}
      />
      <AiChatDrawer
        open={aiFormulaOpen}
        formula={formulaExpr}
        formulaMode="factor"
        factorContext={{
          direction: factorDirectionLabel(versionDirection),
          changeNote: changeNote || undefined,
          paramsText: paramsText !== "{}" ? paramsText : undefined,
        }}
        onClose={() => setAiFormulaOpen(false)}
        onInsertFormula={(formula) => {
          handleFormulaChange(formula);
          formulaSelectionRef.current = { start: formula.length, end: formula.length };
          setAiFormulaOpen(false);
          focusFormulaSelection(formula.length);
          message.success(isZh ? "AI 公式已填入，请校验后再保存" : "AI formula inserted. Validate it before saving.");
        }}
      />
    </div>
  );
}
