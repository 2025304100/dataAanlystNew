import React, { useId } from "react";
import { Button } from "antd";
import {
  ArrowRightOutlined,
  CheckOutlined,
  ExclamationCircleOutlined,
} from "@ant-design/icons";
import "./FactorWorkflowGuide.css";

export type WorkflowStep = {
  id: "prepare" | "train" | "review" | "score";
  title: string;
  description: string;
  state: "complete" | "current" | "waiting" | "blocked";
  current?: boolean;
  onClick?: () => void;
};

export interface FactorWorkflowGuideProps {
  title: string;
  description: string;
  tone: "neutral" | "info" | "warning" | "success";
  badge: string;
  steps: WorkflowStep[];
  actionLabel: string;
  onAction: () => void;
  actionDisabled?: boolean;
  secondaryLabel?: string;
  onSecondary?: () => void;
  summary?: Array<{ label: string; value: string }>;
  children?: React.ReactNode;
}

const STEP_STATE_LABEL: Record<WorkflowStep["state"], string> = {
  complete: "已完成",
  current: "当前步骤",
  waiting: "待完成",
  blocked: "待处理",
};

/** Displays the next useful action; readiness and all actions belong to the caller. */
const FactorWorkflowGuide: React.FC<FactorWorkflowGuideProps> = ({
  title,
  description,
  tone,
  badge,
  steps,
  actionLabel,
  onAction,
  actionDisabled = false,
  secondaryLabel,
  onSecondary,
  summary = [],
  children,
}) => {
  const titleId = useId();
  const descriptionId = useId();

  return (
    <section
      className={`factor-workflow-guide factor-workflow-guide--${tone}`}
      aria-labelledby={titleId}
      aria-describedby={descriptionId}
      data-testid="factor-workflow-guide"
    >
      <div className="factor-workflow-guide__header">
        <div className="factor-workflow-guide__intro">
          <div className="factor-workflow-guide__eyebrow">
            <span className="factor-workflow-guide__eyebrow-label">因子工作流</span>
            <span className="factor-workflow-guide__badge">
              <span className="factor-workflow-guide__badge-dot" aria-hidden="true" />
              {badge}
            </span>
          </div>
          <h2 id={titleId} className="factor-workflow-guide__title">{title}</h2>
          <p id={descriptionId} className="factor-workflow-guide__description">{description}</p>
        </div>
        <div className="factor-workflow-guide__actions">
          <Button
            type="primary"
            size="large"
            className="factor-workflow-guide__primary"
            onClick={onAction}
            disabled={actionDisabled}
          >
            <span>{actionLabel}</span>
            <ArrowRightOutlined aria-hidden="true" />
          </Button>
          {secondaryLabel && onSecondary && (
            <Button
              type="text"
              className="factor-workflow-guide__secondary"
              onClick={onSecondary}
            >
              {secondaryLabel}
            </Button>
          )}
        </div>
      </div>

      {summary.length > 0 && (
        <dl className="factor-workflow-guide__summary">
          {summary.map(({ label, value }) => (
            <div className="factor-workflow-guide__fact" key={label}>
              <dt>{label}</dt>
              <dd>{value}</dd>
            </div>
          ))}
        </dl>
      )}

      <nav className="factor-workflow-guide__navigation" aria-label="因子工作流阶段">
        <ol className="factor-workflow-guide__steps">
          {steps.map((step, index) => {
            const icon = step.state === "complete"
              ? <CheckOutlined aria-hidden="true" />
              : step.state === "blocked"
                ? <ExclamationCircleOutlined aria-hidden="true" />
                : index + 1;

            return (
              <li
                key={step.id}
                className={`factor-workflow-guide__step factor-workflow-guide__step--${step.state}`}
              >
                <button
                  type="button"
                  className="factor-workflow-guide__step-button"
                  onClick={step.onClick}
                  disabled={!step.onClick}
                  aria-current={step.current || step.state === "current" ? "step" : undefined}
                  aria-label={`步骤 ${index + 1}，${step.title}，${STEP_STATE_LABEL[step.state]}。${step.description}`}
                >
                  <span className="factor-workflow-guide__step-icon" aria-hidden="true">{icon}</span>
                  <span className="factor-workflow-guide__step-content">
                    <span className="factor-workflow-guide__step-heading">
                      <span className="factor-workflow-guide__step-title">{step.title}</span>
                      <span className="factor-workflow-guide__step-state">{STEP_STATE_LABEL[step.state]}</span>
                    </span>
                    <span className="factor-workflow-guide__step-description">{step.description}</span>
                  </span>
                </button>
              </li>
            );
          })}
        </ol>
      </nav>

      {children && <div className="factor-workflow-guide__detail">{children}</div>}
    </section>
  );
};

export default FactorWorkflowGuide;
