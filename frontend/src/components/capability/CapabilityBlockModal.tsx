import React from "react";
import { Modal, List, Button, Typography, Tag, Space } from "antd";
import { CheckCircleOutlined, CloseCircleOutlined } from "@ant-design/icons";
import { useApp } from "../../context/AppContext";
import { CapabilityItem, CapabilityAction } from "../../types";
import { t } from "../../i18n";

const { Text, Paragraph } = Typography;

interface CapabilityBlockModalProps {
  open: boolean;
  onClose: () => void;
  capability: CapabilityItem | null;
}

export function CapabilityBlockModal({ open, onClose, capability }: CapabilityBlockModalProps) {
  const ctx = useApp();
  if (!capability) return null;

  const handleAction = (action: CapabilityAction) => {
    switch (action.action_type) {
      case "redirect":
        if (action.target) {
          ctx.setActiveTab(action.target);
        }
        onClose();
        break;
      case "sync":
        ctx.runSync();
        onClose();
        break;
      case "retry":
        ctx.loadCapabilities();
        onClose();
        break;
      case "configure":
      case "dismiss":
      default:
        onClose();
        break;
    }
  };

  return (
    <Modal
      open={open}
      title={capability.label}
      onCancel={onClose}
      footer={null}
    >
      <Paragraph>{capability.user_message}</Paragraph>

      {capability.data_cutoff_at && (
        <div style={{ marginBottom: 12 }}>
          <Tag color="orange">{t("capability.dataCutoff")}: {capability.data_cutoff_at}</Tag>
        </div>
      )}

      {capability.prerequisites.length > 0 && (
        <div style={{ marginBottom: 16 }}>
          <Text strong>{t("capability.prerequisites")}</Text>
          <List
            size="small"
            dataSource={capability.prerequisites}
            renderItem={(p) => (
              <List.Item>
                <Space>
                {p.satisfied
                  ? <CheckCircleOutlined style={{ color: "#52c41a" }} />
                  : <CloseCircleOutlined style={{ color: "#ff4d4f" }} />}
                <span>{p.label}</span>
                {p.detail && <Text type="secondary">— {p.detail}</Text>}
                </Space>
              </List.Item>
            )}
          />
        </div>
      )}

      {capability.recommended_actions.length > 0 && (
        <div>
          <Text strong>{t("capability.recommendedActions")}</Text>
          <Space wrap style={{ marginTop: 8 }}>
            {capability.recommended_actions.map((action, idx) => (
              <Button
                key={idx}
                type={action.action_type === "redirect" || action.action_type === "sync" ? "primary" : "default"}
                onClick={() => handleAction(action)}
              >
                {action.label}
              </Button>
            ))}
          </Space>
        </div>
      )}
    </Modal>
  );
}
