import React, { useState } from "react";
import { Button, Tooltip } from "antd";
import { WarningOutlined, StopOutlined } from "@ant-design/icons";
import { useApp } from "../../context/AppContext";
import { CapabilityBlockModal } from "./CapabilityBlockModal";
import { getReasonMessage } from "./reasonCodeMessages";
import { t } from "../../i18n";

interface CapabilityGateButtonProps {
  capabilityKey: string;
  children: React.ReactNode;
  onClick?: () => void;
  type?: "default" | "primary" | "text" | "dashed" | "link";
  icon?: React.ReactNode;
  disabled?: boolean;
  size?: "small" | "middle" | "large";
  className?: string;
  loading?: boolean;
  danger?: boolean;
  id?: string;
}

export function CapabilityGateButton({
  capabilityKey,
  children,
  onClick,
  type = "default",
  icon,
  disabled,
  size,
  className,
  loading,
  danger,
  id,
}: CapabilityGateButtonProps) {
  const ctx = useApp();
  const [modalOpen, setModalOpen] = useState(false);
  const capability = ctx.getCapability(capabilityKey);
  // capabilities 未加载时按 ready 处理
  const status = capability?.status ?? "ready";
  const userMessage = capability ? getReasonMessage(capability.reason_code, capability.user_message) : "";
  const dataCutoff = capability?.data_cutoff_at;

  const tooltipMessage = dataCutoff
    ? `${userMessage}（${t("capability.dataCutoff")}: ${dataCutoff}）`
    : userMessage;

  if (status === "blocked") {
    // 不使用 antd disabled（disabled 的 button 不响应点击），
    // 改为用样式模拟 disabled 外观，以便点击后能打开 CapabilityBlockModal。
    return (
      <>
        <Tooltip title={tooltipMessage || t("capability.blocked")}>
          <Button
            id={id}
            type={type}
            size={size}
            className={className}
            style={{ opacity: 0.5, cursor: "not-allowed" }}
            icon={icon ?? <StopOutlined style={{ color: "#ff4d4f" }} />}
            onClick={(e) => {
              e.preventDefault();
              setModalOpen(true);
            }}
          >
            {children}
          </Button>
        </Tooltip>
        <CapabilityBlockModal
          open={modalOpen}
          onClose={() => setModalOpen(false)}
          capability={capability ?? null}
        />
      </>
    );
  }

  if (status === "degraded") {
    return (
      <Tooltip title={tooltipMessage}>
        <Button
          id={id}
          type={type}
          size={size}
          className={className}
          disabled={disabled}
          loading={loading}
          danger={danger}
          icon={icon ?? <WarningOutlined style={{ color: "#faad14" }} />}
          onClick={onClick}
        >
          {children}
        </Button>
      </Tooltip>
    );
  }

  // ready 或 capabilities 未加载
  return (
    <Button
      id={id}
      type={type}
      size={size}
      className={className}
      disabled={disabled}
      loading={loading}
      danger={danger}
      icon={icon}
      onClick={onClick}
    >
      {children}
    </Button>
  );
}
