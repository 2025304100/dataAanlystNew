/** Task 3：链式步骤导航 ChainStepsBar — 8 Steps + 阻断直达 + 下一步主按钮。 */
import React from "react";
import { Button, Tooltip } from "antd";
import {
  CheckOutlined,
  ExclamationCircleOutlined,
  ArrowRightOutlined,
} from "@ant-design/icons";
import "./ChainStepsBar.css";

export interface ChainState {
  factorWarehouseReady: boolean;
  anyFactorSetCreated: boolean;
  anyFactorSetMembersReady: boolean;
  anyFactorSetFrozen: boolean;
  anyModelTrained: boolean;
  anyModelValidated: boolean;
  decisionModeEqualsFormalActive: boolean;
  pipelineReady: boolean;
}

export interface ChainStepInference {
  current: number;
  blocked: Set<number>;
  nextEnabled: boolean;
}

interface StepMeta {
  idx: 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8;
  shortName: string;
  longName: string;
  blockReason: string;
  goAction?: { label: string; testId: string } | null;
}

const STEP_META: StepMeta[] = [
  { idx: 1, shortName: "因子库", longName: "检查因子库", blockReason: "请先初始化因子库（仓库健康状态未通过，无法加载因子/集合）。", goAction: { label: "检查因子库", testId: "chain-btn-check-warehouse" } },
  { idx: 2, shortName: "集合", longName: "新建因子集合", blockReason: "暂无因子集合：需要至少创建 1 个集合才能继续。", goAction: { label: "新建集合", testId: "chain-btn-create-factorset" } },
  { idx: 3, shortName: "成员", longName: "添加集合成员（含 feature）", blockReason: "集合为空或不含 feature 角色：请在某个集合中添加至少 1 个 feature 因子。", goAction: { label: "去添加成员", testId: "chain-btn-add-members" } },
  { idx: 4, shortName: "冻结", longName: "冻结集合", blockReason: "没有已冻结集合：先完成预检并冻结一个合格的因子集合才能训练。", goAction: { label: "去冻结", testId: "chain-btn-freeze" } },
  { idx: 5, shortName: "训练", longName: "训练模型", blockReason: "集合已冻结，下一步训练新模型。选择训练和验证窗口，完成后查看验证结果。", goAction: { label: "去训练", testId: "chain-btn-train" } },
  { idx: 6, shortName: "验证", longName: "查看模型验证", blockReason: "当前没有通过正式运行资格检查的已验证模型：请查看验证详情；历史模型仍需通过训练来源和因子版本检查。", goAction: { label: "查看验证", testId: "chain-btn-review-model" } },
  { idx: 7, shortName: "激活", longName: "激活生产模型", blockReason: "当前尚未激活正式使用的模型。请先查看通过验证的模型，由你本人确认后激活。", goAction: { label: "去激活", testId: "chain-btn-activate-model" } },
  { idx: 8, shortName: "评分就绪", longName: "前往生成评分", blockReason: "模型已激活，还需在流水线检查评分条件。条件就绪后运行评分任务，完成后再查看结果。", goAction: { label: "前往流水线设置", testId: "chain-btn-goto-pipeline" } },
];

/** 纯函数：根据 8 项链路状态推断当前步骤 + 阻断集合 + 下一步按钮 enabled。 */
export function inferChainStep(state: ChainState): ChainStepInference {
  const arr = [
    state.factorWarehouseReady,
    state.anyFactorSetCreated,
    state.anyFactorSetMembersReady,
    state.anyFactorSetFrozen,
    state.anyModelTrained,
    state.anyModelValidated,
    state.decisionModeEqualsFormalActive,
    state.pipelineReady,
  ];
  let current = 8;
  for (let i = 0; i < arr.length; i++) if (!arr[i]) { current = i + 1; break; }
  const blocked = new Set<number>();
  for (let i = 0; i < arr.length; i++) {
    const s = i + 1;
    if ((s >= current && !arr[i]) || s > current) blocked.add(s);
  }
  const nextEnabled = current !== 8 || arr[7];
  return { current, blocked, nextEnabled };
}

export interface ChainStepsBarProps {
  state: ChainState;
  onGoStep?: (stepIdx: number) => void;
  className?: string;
  disabledSteps?: Partial<Record<number, string>>;
}

const ChainStepsBar: React.FC<ChainStepsBarProps> = ({ state, onGoStep, className, disabledSteps = {} }) => {
  const { current, blocked, nextEnabled } = inferChainStep(state);
  const currentMeta = STEP_META.find((m) => m.idx === current)!;
  const nextStepName = currentMeta.longName;
  const allReady = current === 8 && state.pipelineReady;
  const completedCount = allReady ? 8 : current - 1;

  const handleNextPrimary = () => {
    if (!disabledSteps[current]) onGoStep?.(current);
  };

  return (
    <div
      className={`chain-steps-bar ${className ?? ""}`}
      data-testid="chain-steps-bar"
    >
      <div className="chain-focus">
        <div className="chain-focus-copy" role="status" aria-live="polite">
          <div className="chain-focus-eyebrow">
            <span>完整研发流程</span>
            <span>{completedCount} / 8 步已就绪</span>
          </div>
          <strong className="chain-focus-title">
            {allReady ? "评分条件已就绪" : `当前待处理：${currentMeta.longName}`}
          </strong>
          <p className="chain-focus-description">
            {allReady
              ? "研发与激活条件已通过。前往流水线生成评分，任务完成后再查看评分结果。"
              : currentMeta.blockReason}
          </p>
        </div>
        <Button
          type="primary"
          size="large"
          disabled={!nextEnabled || !!disabledSteps[current]}
          title={disabledSteps[current]}
          onClick={handleNextPrimary}
          icon={<ArrowRightOutlined />}
          className="chain-primary-action"
          data-testid={current === 8 ? "chain-btn-start-scoring" : "chain-btn-next"}
        >
          {current === 8 ? "前往生成评分" : `下一步：${nextStepName}`}
        </Button>
      </div>
      <div className="chain-steps-wrapper">
        {STEP_META.map((meta) => {
          const isBlocked = blocked.has(meta.idx);
          const readyByState = (() => {
            switch (meta.idx) {
              case 1: return state.factorWarehouseReady;
              case 2: return state.anyFactorSetCreated;
              case 3: return state.anyFactorSetMembersReady;
              case 4: return state.anyFactorSetFrozen;
              case 5: return state.anyModelTrained;
              case 6: return state.anyModelValidated;
              case 7: return state.decisionModeEqualsFormalActive;
              case 8: return state.pipelineReady;
              default: return false;
            }
          })();
          const isCurrent = meta.idx === current;
          const isReady = readyByState && !isBlocked;
          const isWaiting = meta.idx > current;
          const stateClass = isReady ? "chain-ready"
            : isBlocked ? "chain-blocked" : "chain-todo";
          const stepBaseClass = `chain-step chain-step-${meta.idx} ${stateClass} ${isCurrent ? "chain-current" : ""} ${isWaiting ? "chain-waiting" : ""}`;
          const iconNode = isReady ? <CheckOutlined />
            : isCurrent ? <ExclamationCircleOutlined />
            : <span>{meta.idx}</span>;
          const stepContent = (
            <div
              className={stepBaseClass}
              data-testid={`chain-step-${meta.idx}`}
              data-step-state={isReady ? "ready" : isBlocked ? "blocked" : "todo"}
              data-visual-state={isReady ? "ready" : isWaiting ? "waiting" : "current"}
              aria-current={isCurrent ? "step" : undefined}
            >
              <div className="chain-dot" aria-hidden>
                {iconNode}
              </div>
              <div className="chain-step-label">
                <span className="chain-step-name">{meta.idx}. {meta.shortName}</span>
                <span className="chain-step-status">
                  {isReady ? "已就绪" : isWaiting ? "等待前序步骤" : "待处理"}
                </span>
              </div>
            </div>
          );
          const wrapped = isBlocked ? (
            <Tooltip key={meta.idx} title={isWaiting ? `先完成步骤 ${current}「${currentMeta.shortName}」，再继续「${meta.shortName}」。` : meta.blockReason} placement="top">
              {stepContent}
            </Tooltip>
          ) : (
            <React.Fragment key={meta.idx}>{stepContent}</React.Fragment>
          );
          return (
            <React.Fragment key={`w-${meta.idx}`}>
              {wrapped}
            </React.Fragment>
          );
        })}
      </div>
      <div className="chain-secondary-actions">
        <details className="chain-step-shortcuts">
          <summary>其它步骤入口</summary>
          <div className="chain-shortcuts-grid">
            {STEP_META.filter((meta) => blocked.has(meta.idx) && meta.goAction).map((meta) => (
              <Button
                key={meta.idx}
                size="small"
                data-testid={meta.goAction!.testId}
                disabled={!!disabledSteps[meta.idx]}
                title={disabledSteps[meta.idx]}
                onClick={() => onGoStep?.(meta.idx)}
              >
                {meta.idx}. {meta.goAction!.label}
              </Button>
            ))}
            {!blocked.size && <span className="chain-shortcuts-empty">各研发步骤已就绪，可直接前往生成评分。</span>}
          </div>
        </details>
        <Button
          size="small"
          onClick={() => onGoStep?.(1)}
          data-testid="chain-btn-restart-step1"
          className="chain-restart-step1"
        >
          从步骤1重新开始
        </Button>
      </div>
    </div>
  );
};

export default ChainStepsBar;
