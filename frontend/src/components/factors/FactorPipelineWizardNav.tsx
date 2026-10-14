import React from "react";
import { CheckOutlined, ExclamationCircleOutlined } from "@ant-design/icons";
import "./FactorPipelineWizardNav.css";

export type FactorPipelineWizardStep = "prepare" | "train" | "review" | "score";
export type FactorPipelineWizardStepState = "complete" | "current" | "waiting" | "blocked";

export interface FactorPipelineWizardNavProps {
  step: FactorPipelineWizardStep;
  steps: Array<{
    id: FactorPipelineWizardStep;
    title: string;
    state: FactorPipelineWizardStepState;
  }>;
  onSelect: (step: FactorPipelineWizardStep) => void;
  busy?: boolean;
}

const STATE_LABELS: Record<FactorPipelineWizardStepState, string> = {
  complete: "已完成",
  current: "待完成",
  waiting: "待开始",
  blocked: "需处理",
};

/** Navigation only: the displayed page is independent of task readiness. */
const FactorPipelineWizardNav: React.FC<FactorPipelineWizardNavProps> = ({
  step,
  steps,
  onSelect,
  busy = false,
}) => {
  const displayedIndex = steps.findIndex((item) => item.id === step);

  return (
    <nav
      className="factor-pipeline-wizard-nav"
      aria-label="因子流水线步骤"
      aria-busy={busy}
      data-testid="factor-pipeline-wizard-nav"
    >
      <p className="factor-pipeline-wizard-position" aria-live="polite">
        第 {displayedIndex + 1} 步 <span>/ 共 {steps.length} 步</span>
      </p>
      <ol className="factor-pipeline-wizard-steps">
        {steps.map((item, index) => {
          const selected = item.id === step;
          return (
            <li key={item.id}>
              <button
                type="button"
                className={`factor-pipeline-wizard-step${selected ? " is-selected" : ""}`}
                aria-label={`第 ${index + 1} 步：${item.title}`}
                aria-current={selected ? "step" : undefined}
                data-step={item.id}
                data-step-state={item.state}
                onClick={() => onSelect(item.id)}
              >
                <span className="factor-pipeline-wizard-number" aria-hidden="true">
                  {item.state === "complete" ? (
                    <CheckOutlined />
                  ) : item.state === "blocked" ? (
                    <ExclamationCircleOutlined />
                  ) : (
                    index + 1
                  )}
                </span>
                <span className="factor-pipeline-wizard-copy">
                  <span className="factor-pipeline-wizard-title">{item.title}</span>
                  <span className="factor-pipeline-wizard-state">{STATE_LABELS[item.state]}</span>
                </span>
              </button>
            </li>
          );
        })}
      </ol>
    </nav>
  );
};

export default FactorPipelineWizardNav;
