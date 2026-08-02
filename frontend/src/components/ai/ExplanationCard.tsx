import { Card, Collapse, List, Tag, Alert, Button, Space, Tooltip, Empty, Descriptions } from "antd";
import { QuestionCircleOutlined, ExperimentOutlined, WarningOutlined, FileTextOutlined } from "@ant-design/icons";
import { enumLabel, t } from "../../i18n";
import type { AIResponse } from "../../types";

interface ExplanationCardProps {
  response: AIResponse;
  /** 建议动作回调 */
  onActionClick?: (actionType: string, draftId?: string) => void;
}

function confidenceTag(confidence: number | null | undefined): { color: string; label: string } {
  if (confidence == null) return { color: "default", label: "-" };
  if (confidence >= 0.7) return { color: "green", label: t("aiAssistant.confidenceHigh") };
  if (confidence >= 0.4) return { color: "gold", label: t("aiAssistant.confidenceMedium") };
  return { color: "red", label: t("aiAssistant.confidenceLow") };
}

export default function ExplanationCard({ response, onActionClick }: ExplanationCardProps) {
  const hasEvidence = response.evidence && response.evidence.length > 0;
  const hasWarnings = response.warnings && response.warnings.length > 0;
  const hasActions = response.suggested_actions && response.suggested_actions.length > 0;
  const hasDraft = response.draft != null;
  const meta = response.metadata || {};

  const collapseItems: Array<{ key: string; label: React.ReactNode; children: React.ReactNode }> = [];

  if (hasEvidence) {
    collapseItems.push({
      key: "evidence",
      label: (
        <Space>
          <FileTextOutlined />
          <span>{t("aiAssistant.evidence")}</span>
          <Tag>{response.evidence.length}</Tag>
        </Space>
      ),
      children: (
        <List
          size="small"
          dataSource={response.evidence}
          renderItem={(item, idx) => {
            const tag = confidenceTag(item.confidence);
            return (
              <List.Item key={idx}>
                <Space direction="vertical" size={2} style={{ width: "100%" }}>
                  <Space>
                    <Tag color="blue">{enumLabel("aiEvidenceType", item.type)}</Tag>
                    <Tag color={tag.color}>{tag.label}</Tag>
                    <span style={{ fontSize: 12, color: "#94a3b8" }}>{enumLabel("aiEvidenceSource", item.source)}</span>
                  </Space>
                  <span style={{ fontSize: 13 }}>{item.content}</span>
                </Space>
              </List.Item>
            );
          }}
        />
      ),
    });
  }

  if (hasWarnings) {
    collapseItems.push({
      key: "warnings",
      label: (
        <Space>
          <WarningOutlined style={{ color: "#faad14" }} />
          <span>{t("aiAssistant.warnings")}</span>
          <Tag color="warning">{response.warnings.length}</Tag>
        </Space>
      ),
      children: (
        <ul style={{ margin: 0, paddingLeft: 20 }}>
          {response.warnings.map((w, idx) => (
            <li key={idx} style={{ fontSize: 13, color: "#874d00", marginBottom: 4 }}>{w}</li>
          ))}
        </ul>
      ),
    });
  }

  if (hasDraft) {
    const draftObj = response.draft as Record<string, unknown> | null;
    const draftType = String(draftObj?.draft_type ?? draftObj?.action_type ?? "");
    const isFactorDraft = draftType === "draft_factor";
    collapseItems.push({
      key: "draft",
      label: (
        <Space>
          <ExperimentOutlined style={{ color: "#722ed1" }} />
          <span>{t("aiAssistant.draft")}</span>
          {isFactorDraft && <Tag color="purple">factor</Tag>}
        </Space>
      ),
      children: (
        <>
          <Alert
            type="warning"
            message={t("aiAssistant.draftNeedsConfirmation")}
            style={{ marginBottom: 8 }}
            description={
              <pre style={{ fontSize: 12, maxHeight: 200, overflow: "auto", margin: 0 }}>
                {JSON.stringify(response.draft, null, 2)}
              </pre>
            }
          />
          {isFactorDraft && (
            <Button
              size="small"
              type="primary"
              ghost
              icon={<ExperimentOutlined />}
              onClick={() => {
                // 派发自定义事件，由 FactorCenter 监听并打开确认 Modal
                const payload = (draftObj?.suggested_payload ?? draftObj) as Record<string, unknown>;
                window.dispatchEvent(
                  new CustomEvent("open-factor-draft", { detail: { payload } }),
                );
              }}
            >
              {t("aiDraftApplyToEditor")}
            </Button>
          )}
        </>
      ),
    });
  }

  const metadataItems: Array<{ label: string; value: string }> = [];
  if (meta.data_as_of) metadataItems.push({ label: t("aiAssistant.dataAsOf"), value: String(meta.data_as_of) });
  if (meta.model_version) metadataItems.push({ label: t("aiAssistant.modelVersion"), value: String(meta.model_version) });
  if (meta.rule_version) metadataItems.push({ label: t("aiAssistant.ruleVersion"), value: String(meta.rule_version) });
  if (meta.provider_used) metadataItems.push({ label: t("aiAssistant.provider"), value: String(meta.provider_used) });
  if (meta.latency_ms != null) metadataItems.push({ label: t("aiAssistant.latency"), value: `${meta.latency_ms}ms` });
  if (meta.tokens != null) metadataItems.push({ label: t("aiAssistant.tokens"), value: String(meta.tokens) });

  return (
    <Card
      size="small"
      data-testid="ai-explanation-card"
      style={{ marginBottom: 12 }}
      bodyStyle={{ padding: "8px 12px" }}
    >
      {/* 主回答 */}
      <div style={{ fontSize: 14, lineHeight: 1.6, whiteSpace: "pre-wrap", marginBottom: 8 }}>
        {response.answer}
      </div>

      {/* 建议动作 */}
      {hasActions && (
        <Space wrap size="small" style={{ marginBottom: 8 }}>
          {response.suggested_actions.map((action, idx) => (
            <Button
              key={idx}
              size="small"
              type="link"
              onClick={() => onActionClick?.(action.action_type, action.draft_id)}
            >
              {action.description}
            </Button>
          ))}
        </Space>
      )}

      {/* 折叠区：证据 / 警告 / 草稿 */}
      {collapseItems.length > 0 && (
        <Collapse
          size="small"
          items={collapseItems}
          defaultActiveKey={[]}
          style={{ marginBottom: 8 }}
        />
      )}

      {/* 元数据 */}
      {metadataItems.length > 0 && (
        <Descriptions
          size="small"
          column={2}
          colon={false}
          labelStyle={{ fontSize: 11, color: "#94a3b8" }}
          contentStyle={{ fontSize: 11 }}
        >
          {metadataItems.map((item, idx) => (
            <Descriptions.Item key={idx} label={item.label}>{item.value}</Descriptions.Item>
          ))}
        </Descriptions>
      )}

      {/* 免责声明 */}
      <div style={{ fontSize: 11, color: "#94a3b8", marginTop: 4 }}>
        <Tooltip title={t("aiAssistant.usageNote")}>
          <QuestionCircleOutlined style={{ marginRight: 4 }} />
        </Tooltip>
        {t("aiAssistant.usageNote")}
      </div>
    </Card>
  );
}

/** 空状态解释卡（AI 无响应时）。 */
export function ExplanationCardEmpty() {
  return (
    <Card size="small" style={{ marginBottom: 12 }}>
      <Empty description={t("aiAssistant.formatError")} image={Empty.PRESENTED_IMAGE_SIMPLE} />
    </Card>
  );
}
