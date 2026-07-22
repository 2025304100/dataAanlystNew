import React, { useState, useMemo, useEffect } from "react";
import { Modal, Steps, Button, Typography, Alert } from "antd";
import { CheckCircleOutlined, ArrowRightOutlined } from "@ant-design/icons";
import { useApp } from "../../context/AppContext";
import { t } from "../../i18n";

const { Paragraph } = Typography;

interface FirstScanWizardProps {
  // 由父组件控制是否打开（通常当 overall blocked 且在 Discovery/Opportunity 页时打开）
  open: boolean;
  onClose: () => void;
}

export function FirstScanWizard({ open, onClose }: FirstScanWizardProps) {
  const ctx = useApp();
  const [currentStep, setCurrentStep] = useState(0);

  const overallBlocked = ctx.capabilities?.overall_status === "blocked";
  const marketDataBlocked = ctx.isCapabilityBlocked("market_data");
  const scoringBlocked = ctx.isCapabilityBlocked("scoring");
  const discoveryBlocked = ctx.isCapabilityBlocked("discovery");

  // 动态生成步骤
  const steps = useMemo(() => {
    const list: Array<{ title: string; description: string; action: () => void }> = [];
    if (marketDataBlocked) {
      list.push({
        title: t("capability.firstScanStep1"),
        description: ctx.getCapability("market_data")?.user_message ?? "",
        action: () => ctx.setActiveTab("macro"),
      });
    }
    if (scoringBlocked) {
      list.push({
        title: t("capability.firstScanStep2"),
        description: ctx.getCapability("scoring")?.user_message ?? "",
        action: () => ctx.setActiveTab("settings"),
      });
    }
    if (discoveryBlocked && !marketDataBlocked && !scoringBlocked) {
      list.push({
        title: t("capability.firstScanStep3"),
        description: ctx.getCapability("discovery")?.user_message ?? "",
        action: () => {
          ctx.runScan();
        },
      });
    }
    return list;
  }, [marketDataBlocked, scoringBlocked, discoveryBlocked, ctx]);

  // 全部 ready 时自动关闭并触发扫描
  useEffect(() => {
    if (open && !overallBlocked && ctx.capabilities) {
      // 数据已就绪，自动触发扫描
      onClose();
    }
  }, [open, overallBlocked, ctx.capabilities, onClose]);

  if (!overallBlocked) return null;
  if (steps.length === 0) return null;

  const handleStepAction = () => {
    const step = steps[currentStep];
    if (step) {
      step.action();
      // 刷新 capabilities 状态
      ctx.loadCapabilities();
    }
  };

  return (
    <Modal
      open={open}
      title={t("capability.firstScanTitle")}
      onCancel={onClose}
      footer={[
        <Button key="close" onClick={onClose}>
          {t("cancel")}
        </Button>,
        currentStep < steps.length - 1 ? (
          <Button
            key="next"
            type="primary"
            onClick={() => setCurrentStep(currentStep + 1)}
          >
            {"下一步"}
          </Button>
        ) : null,
        <Button key="action" type="primary" onClick={handleStepAction}>
          {steps[currentStep]?.title} <ArrowRightOutlined />
        </Button>,
      ]}
    >
      <Alert
        message={t("capability.overallBlocked")}
        type="warning"
        showIcon
        style={{ marginBottom: 16 }}
      />
      <Steps
        current={currentStep}
        direction="vertical"
        items={steps.map((s, idx) => ({
          title: s.title,
          description: s.description,
          status: idx < currentStep ? "finish" : idx === currentStep ? "process" : "wait",
        }))}
      />
      {currentStep === steps.length - 1 && !marketDataBlocked && !scoringBlocked && (
        <Paragraph style={{ marginTop: 16 }}>
          <CheckCircleOutlined style={{ color: "#52c41a", marginRight: 8 }} />
          {t("capability.firstScanReady")}
        </Paragraph>
      )}
    </Modal>
  );
}
