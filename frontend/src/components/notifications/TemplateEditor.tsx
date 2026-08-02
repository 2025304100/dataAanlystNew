// WP-MSG.6：消息模板页签
//
// 数据来源：
// - GET    /api/v1/notifications/templates               列表
// - POST   /api/v1/notifications/templates               创建
// - PATCH  /api/v1/notifications/templates/{id}          更新
// - DELETE /api/v1/notifications/templates/{id}          删除
// - POST   /api/v1/notifications/templates/{id}/preview  预览渲染
//
// 关键约束：
// - 接口失败降级不抛异常，显示错误信息 + 重试按钮
// - 模板变量转义，防止 Markdown/Webhook 注入（后端处理，前端仅展示）
// - 不伪造数据：列表为空显示 Empty，接口失败显示 Alert
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Button,
  Col,
  Divider,
  Empty,
  Form,
  Input,
  Modal,
  Popconfirm,
  Row,
  Segmented,
  Select,
  Space,
  Spin,
  Switch,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import {
  PlusOutlined,
  ReloadOutlined,
  EditOutlined,
  EyeOutlined,
  DeleteOutlined,
  FileTextOutlined,
} from "@ant-design/icons";
import { requestJson } from "../../api/client";
import { t } from "../../i18n";

// 消息模板（对齐 app.models.notification.NotificationTemplate）
interface Template {
  id: number;
  name: string;
  title_template: string;
  body_template: string;
  body_text_template: string | null;
  variables_json: string | null;
  version: number;
  is_active: boolean;
  created_at?: string | null;
  updated_at?: string | null;
}

// 变量定义
interface TemplateVariable {
  name: string;
  description?: string;
  example?: string;
}

type TemplateFieldName =
  | "title_template"
  | "body_template"
  | "body_text_template";

interface SystemVariable {
  name: string;
  labelKey: string;
  descriptionKey: string;
  example?: string;
  exampleKey?: string;
}

interface SystemVariableGroup {
  key: string;
  labelKey: string;
  titleKey: string;
  bodyKey: string;
  variables: SystemVariable[];
}

// 仅展示已有真实业务调用点的事件及其 template_variables。
export const SYSTEM_VARIABLE_GROUPS: SystemVariableGroup[] = [
  {
    key: "alert_event",
    labelKey: "templateEventAlert",
    titleKey: "templateExampleAlertTitle",
    bodyKey: "templateExampleAlertBody",
    variables: [
      { name: "alert_type", labelKey: "templateVarAlertType", descriptionKey: "templateVarAlertTypeDesc", example: "score_drop" },
      { name: "symbol", labelKey: "templateVarSymbol", descriptionKey: "templateVarSymbolDesc", example: "000001.SZ" },
      { name: "title", labelKey: "templateVarOriginalTitle", descriptionKey: "templateVarOriginalTitleDesc", exampleKey: "templateExampleValueAlertTitle" },
    ],
  },
  {
    key: "trade_executed",
    labelKey: "sourceEventTradeExecuted",
    titleKey: "templateExampleTradeTitle",
    bodyKey: "templateExampleTradeBody",
    variables: [
      { name: "symbol", labelKey: "templateVarSymbol", descriptionKey: "templateVarSymbolDesc", example: "000001.SZ" },
      { name: "action", labelKey: "templateVarAction", descriptionKey: "templateVarActionDesc", example: "buy" },
      { name: "quantity", labelKey: "templateVarQuantity", descriptionKey: "templateVarQuantityDesc", example: "100" },
      { name: "price", labelKey: "templateVarPrice", descriptionKey: "templateVarPriceDesc", example: "12.34" },
    ],
  },
  {
    key: "discovery_new",
    labelKey: "sourceEventDiscoveryNew",
    titleKey: "templateExampleDiscoveryTitle",
    bodyKey: "templateExampleDiscoveryBody",
    variables: [
      { name: "symbol", labelKey: "templateVarSymbol", descriptionKey: "templateVarSymbolDesc", example: "000001.SZ" },
      { name: "score", labelKey: "templateVarScore", descriptionKey: "templateVarScoreDesc", example: "82.50" },
      { name: "reason", labelKey: "templateVarReason", descriptionKey: "templateVarReasonDesc", exampleKey: "templateExampleValueDiscoveryReason" },
    ],
  },
  {
    key: "auto_trade_blocked",
    labelKey: "sourceEventAutoTradeBlocked",
    titleKey: "templateExampleBlockedTitle",
    bodyKey: "templateExampleBlockedBody",
    variables: [
      { name: "symbol", labelKey: "templateVarSymbol", descriptionKey: "templateVarSymbolDesc", example: "000001.SZ" },
      { name: "reason", labelKey: "templateVarReason", descriptionKey: "templateVarReasonDesc", exampleKey: "templateExampleValueBlockedReason" },
      { name: "rule_name", labelKey: "templateVarRuleName", descriptionKey: "templateVarRuleNameDesc", exampleKey: "templateExampleValueBlockedRule" },
    ],
  },
  {
    key: "drawdown_warning",
    labelKey: "sourceEventDrawdownWarning",
    titleKey: "templateExampleDrawdownTitle",
    bodyKey: "templateExampleDrawdownBody",
    variables: [
      { name: "drawdown_pct", labelKey: "templateVarDrawdownPct", descriptionKey: "templateVarDrawdownPctDesc", example: "8.50" },
      { name: "threshold_pct", labelKey: "templateVarThresholdPct", descriptionKey: "templateVarThresholdPctDesc", example: "5.00" },
    ],
  },
];

function getSystemVariableExample(variable: SystemVariable): string {
  return variable.exampleKey ? t(variable.exampleKey) : variable.example || '';
}

// 解析并清洗历史变量 JSON，数据库字段仍保持字符串契约。
export function parseVariables(json: string | null): TemplateVariable[] {
  if (!json) return [];
  try {
    const parsed = JSON.parse(json);
    if (Array.isArray(parsed)) {
      return parsed
        .filter((item) => item && typeof item.name === "string")
        .map((item) => ({
          name: item.name.trim(),
          description: typeof item.description === "string" ? item.description : "",
          example: typeof item.example === "string" ? item.example : "",
        }))
        .filter((item) => item.name);
    }
  } catch {
    // 解析失败返回空数组
  }
  return [];
}

function serializeVariables(variables: TemplateVariable[] | undefined): string | null {
  const cleaned = (variables || [])
    .map((variable) => ({
      name: String(variable?.name || "").trim(),
      description: String(variable?.description || "").trim(),
      example: String(variable?.example || "").trim(),
    }))
    .filter((variable) => variable.name);
  return cleaned.length > 0 ? JSON.stringify(cleaned) : null;
}

export function extractVariableNames(template: string): string[] {
  const names = new Set<string>();
  const pattern = /\{\{\s*([\p{L}\p{N}_]+)\s*\}\}|\{([\p{L}\p{N}_]+)\}/gu;
  for (const match of template.matchAll(pattern)) {
    names.add(match[1] || match[2]);
  }
  return [...names];
}

export function escapeTemplateValue(value: string): string {
  let escaped = value.replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g, '');
  for (const char of ['\\', '*', '_', '`', '[', ']', '#', '<', '>', '&']) {
    escaped = escaped.split(char).join(`\\${char}`);
  }
  return escaped;
}

// 与后端 render_template 保持一致，预览展示渠道收到的转义后文本。
export function renderTemplate(template: string, vars: Record<string, string>): string {
  return template.replace(
    /\{\{\s*([\p{L}\p{N}_]+)\s*\}\}|\{([\p{L}\p{N}_]+)\}/gu,
    (placeholder, doubleBraceName, singleBraceName) => {
      const name = doubleBraceName || singleBraceName;
      return vars[name] === undefined
        ? placeholder
        : escapeTemplateValue(String(vars[name]));
    },
  );
}

export const TemplateEditor: React.FC = () => {
  const [templates, setTemplates] = useState<Template[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [createModalVisible, setCreateModalVisible] = useState(false);
  const [editModalVisible, setEditModalVisible] = useState(false);
  const [editingTemplate, setEditingTemplate] = useState<Template | null>(null);
  const [previewModalVisible, setPreviewModalVisible] = useState(false);
  const [previewingTemplate, setPreviewingTemplate] = useState<Template | null>(null);
  const [previewVars, setPreviewVars] = useState<Record<string, string>>({});
  const [submitting, setSubmitting] = useState(false);
  const [createForm] = Form.useForm();
  const [editForm] = Form.useForm();

  const fetchTemplates = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await requestJson<Template[]>("/api/v1/notifications/templates");
      setTemplates(Array.isArray(data) ? data : []);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      setError(msg || t("templateLoadFailed"));
      setTemplates([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchTemplates();
  }, [fetchTemplates]);

  // 创建模板
  const handleCreateSubmit = useCallback(async () => {
    try {
      const values = await createForm.validateFields();
      setSubmitting(true);
      await requestJson<Template>("/api/v1/notifications/templates", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: values.name,
          title_template: values.title_template,
          body_template: values.body_template,
          body_text_template: values.body_text_template || null,
          variables_json: serializeVariables(values.variables),
          is_active: Boolean(values.is_active ?? true),
        }),
      });
      message.success(t("templateCreated"));
      setCreateModalVisible(false);
      createForm.resetFields();
      fetchTemplates();
    } catch (err: unknown) {
      const anyErr = err as { errorFields?: unknown };
      if (anyErr.errorFields) return;
      const msg = err instanceof Error ? err.message : "";
      message.error(msg || t("createFailed"));
    } finally {
      setSubmitting(false);
    }
  }, [createForm, fetchTemplates]);

  // 编辑模板
  const handleEditSubmit = useCallback(async () => {
    if (!editingTemplate) return;
    try {
      const values = await editForm.validateFields();
      setSubmitting(true);
      await requestJson<Template>(`/api/v1/notifications/templates/${editingTemplate.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: values.name,
          title_template: values.title_template,
          body_template: values.body_template,
          body_text_template: values.body_text_template || null,
          variables_json: serializeVariables(values.variables),
          is_active: Boolean(values.is_active ?? true),
        }),
      });
      message.success(t("templateUpdated"));
      setEditModalVisible(false);
      setEditingTemplate(null);
      editForm.resetFields();
      fetchTemplates();
    } catch (err: unknown) {
      const anyErr = err as { errorFields?: unknown };
      if (anyErr.errorFields) return;
      const msg = err instanceof Error ? err.message : "";
      message.error(msg || t("updateFailed"));
    } finally {
      setSubmitting(false);
    }
  }, [editForm, editingTemplate, fetchTemplates]);

  // 切换激活状态
  const handleToggleActive = useCallback(
    async (templateId: number, isActive: boolean) => {
      try {
        await requestJson<Template>(`/api/v1/notifications/templates/${templateId}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ is_active: isActive }),
        });
        message.success(isActive ? t("templateActivated") : t("templateDeactivated"));
        fetchTemplates();
      } catch (err: unknown) {
        const msg = err instanceof Error ? err.message : "";
        message.error(msg || t("updateFailed"));
      }
    },
    [fetchTemplates],
  );

  // 删除模板
  const handleDelete = useCallback(
    async (templateId: number) => {
      try {
        await requestJson<{ ok: boolean }>(`/api/v1/notifications/templates/${templateId}`, {
          method: "DELETE",
        });
        message.success(t("templateDeleted"));
        fetchTemplates();
      } catch (err: unknown) {
        const msg = err instanceof Error ? err.message : "";
        message.error(msg || t("deleteFailed"));
      }
    },
    [fetchTemplates],
  );

  // 打开编辑弹窗
  const handleEdit = useCallback(
    (template: Template) => {
      setEditingTemplate(template);
      editForm.setFieldsValue({
        name: template.name,
        title_template: template.title_template,
        body_template: template.body_template,
        body_text_template: template.body_text_template || "",
        variables: parseVariables(template.variables_json),
        is_active: template.is_active,
      });
      setEditModalVisible(true);
    },
    [editForm],
  );

  // 打开预览弹窗
  const handlePreview = useCallback((template: Template) => {
    setPreviewingTemplate(template);
    // 初始化变量值
    const vars = parseVariables(template.variables_json);
    const initVars: Record<string, string> = {};
    vars.forEach((v) => {
      initVars[v.name] = v.example || "";
    });
    setPreviewVars(initVars);
    setPreviewModalVisible(true);
  }, []);

  const columns: ColumnsType<Template> = [
    {
      title: t("templateColumnName"),
      dataIndex: "name",
      key: "name",
      width: 160,
    },
    {
      title: t("templateTitle"),
      dataIndex: "title_template",
      key: "title_template",
      width: 240,
      ellipsis: true,
    },
    {
      title: t("templateVersion"),
      dataIndex: "version",
      key: "version",
      width: 80,
      render: (v: number) => <Tag>v{v}</Tag>,
    },
    {
      title: t("templateActive"),
      key: "is_active",
      width: 90,
      render: (_v: unknown, record: Template) => (
        <Switch
          size="small"
          checked={record.is_active}
          onChange={(checked) => handleToggleActive(record.id, checked)}
        />
      ),
    },
    {
      title: t("templateVariables"),
      key: "variables",
      width: 200,
      render: (_v: unknown, record: Template) => {
        const vars = parseVariables(record.variables_json);
        if (vars.length === 0) {
          return <span style={{ color: "var(--muted)" }}>-</span>;
        }
        return (
          <Space size={[2, 2]} wrap>
            {vars.slice(0, 4).map((v) => (
              <Tag key={v.name} color="blue">
                {`{${v.name}}`}
              </Tag>
            ))}
            {vars.length > 4 && <Tag>+{vars.length - 4}</Tag>}
          </Space>
        );
      },
    },
    {
      title: t("channelColumnActions"),
      key: "actions",
      width: 240,
      render: (_v: unknown, record: Template) => (
        <Space size="small">
          <Button
            size="small"
            icon={<EyeOutlined />}
            onClick={() => handlePreview(record)}
          >
            {t("templatePreview")}
          </Button>
          <Button
            size="small"
            icon={<EditOutlined />}
            onClick={() => handleEdit(record)}
          >
            {t("edit")}
          </Button>
          <Popconfirm
            title={t("deleteConfirm")}
            onConfirm={() => handleDelete(record.id)}
          >
            <Button size="small" danger>
              {t("delete")}
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <div className="notification-template-editor" data-tab-content="templates">
      <div
        style={{
          marginBottom: 12,
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          flexWrap: "wrap",
          gap: 8,
        }}
      >
        <Space size="small" wrap>
          <Button icon={<ReloadOutlined />} onClick={fetchTemplates} loading={loading}>
            {t("refresh")}
          </Button>
          <Button
            type="primary"
            icon={<PlusOutlined />}
            onClick={() => {
              createForm.resetFields();
              createForm.setFieldsValue({ is_active: true, variables: [] });
              setCreateModalVisible(true);
            }}
          >
            {t("addTemplate")}
          </Button>
        </Space>
        <span style={{ color: "var(--muted)", fontSize: 12 }}>
          {t("templateCount")}: {templates.length}
        </span>
      </div>

      {error && (
        <Alert
          type="error"
          showIcon
          message={t("templateLoadFailed")}
          description={error}
          action={
            <Button size="small" onClick={fetchTemplates}>
              {t("refresh")}
            </Button>
          }
          style={{ marginBottom: 12 }}
        />
      )}

      {loading && templates.length === 0 ? (
        <div style={{ textAlign: "center", padding: "40px 0" }}>
          <Spin tip={t("templateLoading")} />
        </div>
      ) : templates.length === 0 ? (
        <Empty description={t("templatesEmpty")} />
      ) : (
        <Table<Template>
          rowKey="id"
          dataSource={templates}
          columns={columns}
          size="small"
          pagination={{ pageSize: 20, showSizeChanger: false }}
          scroll={{ x: "max-content" }}
          loading={loading}
          locale={{ emptyText: <Empty description={t("templatesEmpty")} /> }}
        />
      )}

      {/* 创建模板弹窗 */}
      <Modal
        open={createModalVisible}
        title={t("createTemplate")}
        onCancel={() => {
          setCreateModalVisible(false);
          createForm.resetFields();
        }}
        onOk={handleCreateSubmit}
        confirmLoading={submitting}
        okText={t("create")}
        cancelText={t("cancel")}
        width={1040}
      >
        <TemplateFormFields form={createForm} />
      </Modal>

      {/* 编辑模板弹窗 */}
      <Modal
        open={editModalVisible}
        title={t("editTemplate")}
        onCancel={() => {
          setEditModalVisible(false);
          setEditingTemplate(null);
          editForm.resetFields();
        }}
        onOk={handleEditSubmit}
        confirmLoading={submitting}
        okText={t("save")}
        cancelText={t("cancel")}
        width={1040}
      >
        <TemplateFormFields form={editForm} />
      </Modal>

      {/* 预览弹窗 */}
      <Modal
        open={previewModalVisible}
        title={t("templatePreview")}
        onCancel={() => {
          setPreviewModalVisible(false);
          setPreviewingTemplate(null);
          setPreviewVars({});
        }}
        footer={null}
        width={640}
      >
        {previewingTemplate && (
          <PreviewContent
            template={previewingTemplate}
            vars={previewVars}
            onVarsChange={setPreviewVars}
          />
        )}
      </Modal>
    </div>
  );
};

// 模板表单字段（创建/编辑共用）
interface TemplateFormFieldsProps {
  form: ReturnType<typeof Form.useForm>[0];
}

const TemplateFormFields: React.FC<TemplateFormFieldsProps> = ({ form }) => {
  const addVariableRef = useRef<
    ((defaultValue?: TemplateVariable) => void) | null
  >(null);
  const [activeTarget, setActiveTarget] =
    useState<TemplateFieldName>("title_template");
  const [selectedGroupKey, setSelectedGroupKey] = useState(
    SYSTEM_VARIABLE_GROUPS[0].key,
  );
  const titleTemplate = Form.useWatch("title_template", form) || "";
  const bodyTemplate = Form.useWatch("body_template", form) || "";
  const bodyTextTemplate = Form.useWatch("body_text_template", form) || "";
  const variables =
    (Form.useWatch("variables", form) as TemplateVariable[] | undefined) || [];
  const selectedGroup =
    SYSTEM_VARIABLE_GROUPS.find((group) => group.key === selectedGroupKey) ||
    SYSTEM_VARIABLE_GROUPS[0];

  const previewVars = useMemo(
    () =>
      Object.fromEntries(
        variables
          .filter((variable) => variable?.name)
          .map((variable) => [
            variable.name.trim(),
            variable.example || `{${variable.name.trim()}}`,
          ]),
      ),
    [variables],
  );
  const referencedVariables = useMemo(
    () =>
      extractVariableNames(
        `${titleTemplate}\n${bodyTemplate}\n${bodyTextTemplate}`,
      ),
    [bodyTemplate, bodyTextTemplate, titleTemplate],
  );
  const definedNames = useMemo(
    () => new Set(variables.map((variable) => variable?.name?.trim()).filter(Boolean)),
    [variables],
  );
  const missingVariables = referencedVariables.filter(
    (name) => !definedNames.has(name),
  );
  const hasExistingTemplateContent = [
    titleTemplate,
    bodyTemplate,
    bodyTextTemplate,
  ].some((value) => value.trim().length > 0);

  const toTemplateVariable = useCallback(
    (variable: SystemVariable): TemplateVariable => ({
      name: variable.name,
      description: t(variable.descriptionKey),
      example: getSystemVariableExample(variable),
    }),
    [],
  );

  const ensureVariablesDefined = useCallback(
    (systemVariables: SystemVariable[]) => {
      const current =
        (form.getFieldValue("variables") as TemplateVariable[] | undefined) || [];
      const existing = new Set(
        current.map((variable) => variable?.name?.trim()).filter(Boolean),
      );
      const additions = systemVariables
        .filter((variable) => !existing.has(variable.name))
        .map(toTemplateVariable);
      if (additions.length > 0) {
        if (addVariableRef.current) {
          additions.forEach((variable) => addVariableRef.current?.(variable));
        } else {
          form.setFieldsValue({ variables: [...current, ...additions] });
        }
      }
    },
    [form, toTemplateVariable],
  );

  const insertVariable = useCallback(
    (variable: SystemVariable) => {
      const current = String(form.getFieldValue(activeTarget) || "");
      const separator = current && !/\s$/.test(current) ? " " : "";
      form.setFieldValue(
        activeTarget,
        `${current}${separator}{${variable.name}}`,
      );
      ensureVariablesDefined([variable]);
    },
    [activeTarget, ensureVariablesDefined, form],
  );

  const applyExample = useCallback(() => {
    form.setFieldsValue({
      title_template: t(selectedGroup.titleKey),
      body_template: t(selectedGroup.bodyKey),
      body_text_template: t(selectedGroup.bodyKey),
    });
    ensureVariablesDefined(selectedGroup.variables);
  }, [ensureVariablesDefined, form, selectedGroup]);

  return (
    <Form
      form={form}
      layout="vertical"
      initialValues={{ is_active: true, variables: [] }}
    >
      <Row gutter={16} style={{ marginInline: 0 }}>
        <Col xs={24} md={17}>
          <Form.Item
            name="name"
            label={t("templateColumnName")}
            rules={[{ required: true, message: t("templateNameRequired") }]}
          >
            <Input
              placeholder={t("templateColumnNamePlaceholder")}
              autoComplete="off"
            />
          </Form.Item>
        </Col>
        <Col xs={24} md={7}>
          <Form.Item
            name="is_active"
            label={t("templateActive")}
            valuePropName="checked"
          >
            <Switch
              checkedChildren={t("enabled")}
              unCheckedChildren={t("disabled")}
            />
          </Form.Item>
        </Col>
      </Row>

      <div
        style={{
          border: "1px solid var(--border, #d9d9d9)",
          borderRadius: 6,
          padding: 12,
          marginBottom: 16,
        }}
      >
        <Space
          align="start"
          size={12}
          wrap
          style={{ width: "100%", justifyContent: "space-between" }}
        >
          <div style={{ flex: "1 1 320px" }}>
            <Typography.Text strong>{t("templateSystemVariables")}</Typography.Text>
            <div style={{ color: "var(--muted)", fontSize: 12, marginTop: 2 }}>
              {t("templateSystemVariablesHelp")}
            </div>
          </div>
          <Space wrap>
            <Select
              aria-label={t("templateMessageScenario")}
              value={selectedGroupKey}
              style={{ width: 180 }}
              onChange={setSelectedGroupKey}
              options={SYSTEM_VARIABLE_GROUPS.map((group) => ({
                value: group.key,
                label: t(group.labelKey),
              }))}
            />
            <Popconfirm
              title={t("templateApplyExampleConfirmTitle")}
              description={t("templateApplyExampleConfirmDescription")}
              okText={t("templateApplyExampleConfirmOk")}
              cancelText={t("cancel")}
              disabled={!hasExistingTemplateContent}
              onConfirm={applyExample}
            >
              <Button
                icon={<FileTextOutlined />}
                data-testid="apply-template-example"
                onClick={() => {
                  if (!hasExistingTemplateContent) applyExample();
                }}
              >
                {t("templateApplyExample")}
              </Button>
            </Popconfirm>
          </Space>
        </Space>

        <Divider style={{ margin: "12px 0" }} />

        <Space size={[8, 8]} wrap>
          {selectedGroup.variables.map((variable) => (
            <Tooltip
              key={variable.name}
              title={`${t(variable.descriptionKey)} · ${t("templateExampleValue")}: ${getSystemVariableExample(variable)}`}
            >
              <Button
                size="small"
                icon={<PlusOutlined />}
                data-testid={`system-variable-${variable.name}`}
                onClick={() => insertVariable(variable)}
              >
                {t(variable.labelKey)} <Typography.Text code>{`{${variable.name}}`}</Typography.Text>
              </Button>
            </Tooltip>
          ))}
        </Space>

        <div style={{ marginTop: 12 }}>
          <Space size={8} wrap>
            <Typography.Text type="secondary">
              {t("templateInsertTarget")}
            </Typography.Text>
            <Segmented
              size="small"
              value={activeTarget}
              onChange={(value) => setActiveTarget(value as TemplateFieldName)}
              options={[
                { value: "title_template", label: t("templateTargetTitle") },
                { value: "body_template", label: t("templateTargetBody") },
                { value: "body_text_template", label: t("templateTargetBodyText") },
              ]}
            />
          </Space>
        </div>
      </div>

      <Row gutter={20} style={{ marginInline: 0 }}>
        <Col xs={24} lg={15}>
          <Form.Item
            name="title_template"
            label={t("templateTitle")}
            rules={[{ required: true, message: t("templateTitleRequired") }]}
            tooltip={t("templateTitleTooltip")}
          >
            <Input
              data-testid="template-title-input"
              placeholder={t("templateTitlePlaceholder")}
              autoComplete="off"
              onFocus={() => setActiveTarget("title_template")}
            />
          </Form.Item>
          <Form.Item
            name="body_template"
            label={t("templateBody")}
            rules={[{ required: true, message: t("templateBodyRequired") }]}
            tooltip={t("templateBodyTooltip")}
          >
            <Input.TextArea
              data-testid="template-body-input"
              rows={7}
              placeholder={t("templateBodyPlaceholder")}
              onFocus={() => setActiveTarget("body_template")}
            />
          </Form.Item>
          <Form.Item
            name="body_text_template"
            label={t("templateBodyText")}
            tooltip={t("templateBodyTextTooltip")}
          >
            <Input.TextArea
              data-testid="template-body-text-input"
              rows={4}
              placeholder={t("templateBodyTextPlaceholder")}
              onFocus={() => setActiveTarget("body_text_template")}
            />
          </Form.Item>
        </Col>
        <Col xs={24} lg={9}>
          <TemplateDraftPreview
            titleTemplate={titleTemplate}
            bodyTemplate={bodyTemplate}
            bodyTextTemplate={bodyTextTemplate}
            variables={variables}
            previewVars={previewVars}
            missingVariables={missingVariables}
          />
        </Col>
      </Row>

      <Divider orientation="left" style={{ marginTop: 4 }}>
        {t("templateVariables")}
      </Divider>
      <Typography.Paragraph type="secondary" style={{ marginTop: -8 }}>
        {t("templateVariablesHelp")}
      </Typography.Paragraph>

      <Form.List
        name="variables"
        rules={[
          {
            validator: async (_, rows: TemplateVariable[] | undefined) => {
              const names = (rows || [])
                .map((row) => row?.name?.trim())
                .filter(Boolean);
              if (new Set(names).size !== names.length) {
                throw new Error(t("templateVariableDuplicate"));
              }
            },
          },
        ]}
      >
        {(fields, { add, remove }, { errors }) => {
          addVariableRef.current = add;
          return (
            <>
            {fields.length === 0 && (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description={t("templateVariablesEmpty")}
                style={{ marginBlock: 8 }}
              />
            )}
            {fields.map(({ key, name: fieldName, ...restField }) => (
              <Row
                key={key}
                gutter={8}
                align="bottom"
                style={{
                  borderBottom: "1px solid var(--border, #f0f0f0)",
                  marginBottom: 8,
                  marginInline: 0,
                }}
              >
                <Col xs={24} sm={6}>
                  <Form.Item
                    {...restField}
                    name={[fieldName, "name"]}
                    label={t("templateVariableName")}
                    rules={[
                      { required: true, message: t("templateVariableNameRequired") },
                      {
                        pattern: /^[\p{L}\p{N}_]+$/u,
                        message: t("templateVariableNameInvalid"),
                      },
                    ]}
                  >
                    <Input
                      data-testid="template-variable-name"
                      placeholder="symbol"
                      autoComplete="off"
                    />
                  </Form.Item>
                </Col>
                <Col xs={24} sm={8}>
                  <Form.Item
                    {...restField}
                    name={[fieldName, "description"]}
                    label={t("templateVariableDescription")}
                  >
                    <Input
                      placeholder={t("templateVariableDescriptionPlaceholder")}
                      autoComplete="off"
                    />
                  </Form.Item>
                </Col>
                <Col xs={21} sm={8}>
                  <Form.Item
                    {...restField}
                    name={[fieldName, "example"]}
                    label={t("templateVariableExample")}
                  >
                    <Input
                      data-testid="template-variable-example"
                      placeholder={t("templateVariableExamplePlaceholder")}
                      autoComplete="off"
                    />
                  </Form.Item>
                </Col>
                <Col xs={3} sm={2}>
                  <Form.Item label=" ">
                    <Tooltip title={t("templateRemoveVariable")}>
                      <Button
                        aria-label={t("templateRemoveVariable")}
                        icon={<DeleteOutlined />}
                        danger
                        onClick={() => remove(fieldName)}
                      />
                    </Tooltip>
                  </Form.Item>
                </Col>
              </Row>
            ))}
            <Form.ErrorList errors={errors} />
            <Button type="dashed" icon={<PlusOutlined />} onClick={() => add()}>
              {t("templateAddCustomVariable")}
            </Button>
            </>
          );
        }}
      </Form.List>
    </Form>
  );
};

interface TemplateDraftPreviewProps {
  titleTemplate: string;
  bodyTemplate: string;
  bodyTextTemplate: string;
  variables: TemplateVariable[];
  previewVars: Record<string, string>;
  missingVariables: string[];
}

const TemplateDraftPreview: React.FC<TemplateDraftPreviewProps> = ({
  titleTemplate,
  bodyTemplate,
  bodyTextTemplate,
  variables,
  previewVars,
  missingVariables,
}) => {
  const previewBlockStyle = {
    padding: 10,
    background: "var(--bg-alt, #f5f5f5)",
    borderRadius: 4,
    minHeight: 40,
    whiteSpace: "pre-wrap" as const,
    wordBreak: "break-word" as const,
  };

  return (
    <div
      style={{
        borderLeft: "3px solid #1677ff",
        paddingLeft: 14,
        marginBottom: 16,
      }}
    >
      <Typography.Title level={5} style={{ marginTop: 0 }}>
        {t("templateLivePreview")}
      </Typography.Title>
      <Typography.Paragraph type="secondary">
        {t("templateLivePreviewHelp")}
      </Typography.Paragraph>

      {missingVariables.length > 0 && (
        <Alert
          type="warning"
          showIcon
          message={t("templateUndefinedVariables")}
          description={missingVariables.map((name) => `{${name}}`).join(", ")}
          style={{ marginBottom: 12 }}
        />
      )}

      {variables.length > 0 && (
        <Space size={[4, 4]} wrap style={{ marginBottom: 12 }}>
          {variables
            .filter((variable) => variable?.name)
            .map((variable) => (
              <Tooltip
                key={variable.name}
                title={variable.description || variable.name}
              >
                <Tag color={variable.example ? "green" : "default"}>
                  {`{${variable.name}}`} = {variable.example || t("templateNoExample")}
                </Tag>
              </Tooltip>
            ))}
        </Space>
      )}

      <Typography.Text strong>{t("templateTitle")}</Typography.Text>
      <div style={{ ...previewBlockStyle, marginTop: 4, marginBottom: 12 }}>
        {renderTemplate(titleTemplate, previewVars) || (
          <Typography.Text type="secondary">
            {t("templatePreviewTitleEmpty")}
          </Typography.Text>
        )}
      </div>

      <Typography.Text strong>{t("templatePreviewMarkdownBody")}</Typography.Text>
      <div style={{ ...previewBlockStyle, marginTop: 4, marginBottom: 12 }}>
        {renderTemplate(bodyTemplate, previewVars) || (
          <Typography.Text type="secondary">
            {t("templatePreviewBodyEmpty")}
          </Typography.Text>
        )}
      </div>

      {bodyTextTemplate && (
        <>
          <Typography.Text strong>{t("templateBodyText")}</Typography.Text>
          <div style={{ ...previewBlockStyle, marginTop: 4 }}>
            {renderTemplate(bodyTextTemplate, previewVars)}
          </div>
        </>
      )}
    </div>
  );
};

// 预览内容
interface PreviewContentProps {
  template: Template;
  vars: Record<string, string>;
  onVarsChange: (vars: Record<string, string>) => void;
}

const PreviewContent: React.FC<PreviewContentProps> = ({
  template,
  vars,
  onVarsChange,
}) => {
  const variables = parseVariables(template.variables_json);
  const renderedTitle = renderTemplate(template.title_template, vars);
  const renderedBody = renderTemplate(template.body_template, vars);
  const renderedBodyText = template.body_text_template
    ? renderTemplate(template.body_text_template, vars)
    : "";

  return (
    <div>
      {variables.length > 0 && (
        <div style={{ marginBottom: 16 }}>
          <div style={{ fontWeight: 500, marginBottom: 8 }}>
            {t("templatePreviewVars")}
          </div>
          <Space direction="vertical" size="small" style={{ width: "100%" }}>
            {variables.map((v) => (
              <div key={v.name} style={{ display: "flex", gap: 8, alignItems: "center" }}>
                <span style={{ minWidth: 120, fontFamily: "monospace" }}>
                  {`{${v.name}}`}
                </span>
                <Input
                  value={vars[v.name] || ""}
                  onChange={(e) => {
                    const next = { ...vars, [v.name]: e.target.value };
                    onVarsChange(next);
                  }}
                  placeholder={v.description || v.name}
                  autoComplete="off"
                />
              </div>
            ))}
          </Space>
        </div>
      )}
      <div style={{ marginBottom: 16 }}>
        <div style={{ fontWeight: 500, marginBottom: 4 }}>{t("templateTitle")}</div>
        <div
          style={{
            padding: 8,
            background: "var(--bg-alt, #f5f5f5)",
            borderRadius: 4,
            minHeight: 32,
          }}
        >
          {renderedTitle || <span style={{ color: "var(--muted)" }}>...</span>}
        </div>
      </div>
      <div style={{ marginBottom: 16 }}>
        <div style={{ fontWeight: 500, marginBottom: 4 }}>
          {t("templatePreviewMarkdownBody")}
        </div>
        <pre
          style={{
            padding: 8,
            background: "var(--bg-alt, #f5f5f5)",
            borderRadius: 4,
            minHeight: 60,
            whiteSpace: "pre-wrap",
            wordBreak: "break-word",
            margin: 0,
            fontFamily: "inherit",
            fontSize: 13,
          }}
        >
          {renderedBody || "..."}
        </pre>
      </div>
      {renderedBodyText && (
        <div>
          <div style={{ fontWeight: 500, marginBottom: 4 }}>{t("templateBodyText")}</div>
          <pre
            style={{
              padding: 8,
              background: "var(--bg-alt, #f5f5f5)",
              borderRadius: 4,
              minHeight: 40,
              whiteSpace: "pre-wrap",
              wordBreak: "break-word",
              margin: 0,
              fontFamily: "inherit",
              fontSize: 13,
            }}
          >
            {renderedBodyText}
          </pre>
        </div>
      )}
    </div>
  );
};

export default TemplateEditor;
