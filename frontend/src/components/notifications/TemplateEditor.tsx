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
import { useCallback, useEffect, useState } from "react";
import {
  Alert,
  Button,
  Empty,
  Form,
  Input,
  Modal,
  Popconfirm,
  Space,
  Spin,
  Switch,
  Table,
  Tag,
  message,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import {
  PlusOutlined,
  ReloadOutlined,
  EditOutlined,
  EyeOutlined,
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
}

// 解析变量 JSON
function parseVariables(json: string | null): TemplateVariable[] {
  if (!json) return [];
  try {
    const parsed = JSON.parse(json);
    if (Array.isArray(parsed)) return parsed as TemplateVariable[];
  } catch {
    // 解析失败返回空数组
  }
  return [];
}

// 简易变量替换：将 {{name}} 替换为输入值
function renderTemplate(template: string, vars: Record<string, string>): string {
  return template.replace(/\{\{\s*(\w+)\s*\}\}/g, (_, name) => vars[name] ?? `{{${name}}}`);
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
          variables_json: values.variables_json || null,
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
          variables_json: values.variables_json || null,
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
        variables_json: template.variables_json || "",
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
      initVars[v.name] = "";
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
                {`{{${v.name}}}`}
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
        width={640}
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
        width={640}
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
  return (
    <Form
      form={form}
      layout="vertical"
      preserve={false}
      initialValues={{ is_active: true }}
    >
      <Form.Item
        name="name"
        label={t("templateColumnName")}
        rules={[{ required: true }]}
      >
        <Input placeholder={t("templateColumnNamePlaceholder")} autoComplete="off" />
      </Form.Item>
      <Form.Item
        name="title_template"
        label={t("templateTitle")}
        rules={[{ required: true }]}
        tooltip={t("templateTitleTooltip")}
      >
        <Input placeholder={t("templateTitlePlaceholder")} autoComplete="off" />
      </Form.Item>
      <Form.Item
        name="body_template"
        label={t("templateBody")}
        rules={[{ required: true }]}
        tooltip={t("templateBodyTooltip")}
      >
        <Input.TextArea rows={6} placeholder={t("templateBodyPlaceholder")} />
      </Form.Item>
      <Form.Item
        name="body_text_template"
        label={t("templateBodyText")}
        tooltip={t("templateBodyTextTooltip")}
      >
        <Input.TextArea rows={4} placeholder={t("templateBodyTextPlaceholder")} />
      </Form.Item>
      <Form.Item
        name="variables_json"
        label={t("templateVariables")}
        tooltip={t("templateVariablesTooltip")}
      >
        <Input.TextArea
          rows={3}
          placeholder={t("templateVariablesPlaceholder")}
        />
      </Form.Item>
      <Form.Item name="is_active" label={t("templateActive")} valuePropName="checked">
        <Switch />
      </Form.Item>
    </Form>
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
                  {`{{${v.name}}}`}
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
        <div style={{ fontWeight: 500, marginBottom: 4 }}>{t("templateBody")}</div>
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
