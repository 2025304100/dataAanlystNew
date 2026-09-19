/** Task 3：链式步骤导航 ChainStepsBar — 8 Steps + 阻断直达 + 下一步主按钮。 */
import React from "react";
import { Button, Tooltip } from "antd";
import {
  CheckOutlined,
  ExclamationCircleOutlined,
  ArrowRightOutlined,
} from "@ant-design/icons";
import { t } from "../../i18n";
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
  { idx: 1, shortName: "因子库", longName: "因子库就绪", blockReason: "请先初始化因子库（仓库健康状态未通过，无法加载因子/集合）。", goAction: null },
  { idx: 2, shortName: "集合", longName: "新建因子集合", blockReason: "暂无因子集合：需要至少创建 1 个集合才能继续。", goAction: { label: "新建集合", testId: "chain-btn-create-factorset" } },
  { idx: 3, shortName: "成员", longName: "添加集合成员（含 feature）", blockReason: "集合为空或不含 feature 角色：请在某个集合中添加至少 1 个 feature 因子。", goAction: { label: "去添加成员", testId: "chain-btn-add-members" } },
  { idx: 4, shortName: "冻结", longName: "冻结集合", blockReason: "没有已冻结集合：先完成预检并冻结一个合格的因子集合才能训练。", goAction: { label: "去冻结", testId: "chain-btn-freeze" } },
  { idx: 5, shortName: "训练", longName: "训练模型", blockReason: "没有可用冻结集合或尚未训练：请在一个已冻结集合上点「训练模型」。", goAction: { label: "去训练", testId: "chain-btn-train" } },
  { idx: 6, shortName: "验证", longName: "模型验证", blockReason: "当前没有状态为「已验证」的模型：训练完成后等待验证阶段通过。", goAction: null },
  { idx: 7, shortName: "激活", longName: "激活生产模型", blockReason: "决策模式非 formal 或活动模型 ID 为空：请先激活一个已验证模型为正式启用。", goAction: { label: "去激活", testId: "chain-btn-activate-model" } },
  { idx: 8, shortName: "评分", longName: "评分/流水线", blockReason: "流水线配置尚未就绪（仓库/集合/模型至少一方未通过健康检查）。", goAction: { label: "前往流水线设置", testId: "chain-btn-goto-pipeline" } },
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
}

const ChainStepsBar: React.FC<ChainStepsBarProps> = ({ state, onGoStep, className }) => {
  const { current, blocked, nextEnabled } = inferChainStep(state);
  const nextStepName = STEP_META.find((m) => m.idx === current)?.longName ?? "";

  const handleNextPrimary = () => {
    if (current < 8) {
      const meta = STEP_META.find((m) => m.idx === current);
      if (meta?.goAction) onGoStep?.(current);
    } else onGoStep?.(8);
  };

  return (
    <div
      className={`chain-steps-bar ${className ?? ""}`}
      data-testid="chain-steps-bar"
    >
      <div className="chain-steps-wrapper">
        {STEP_META.map((meta, i) => {
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
          const stateClass = isReady ? "chain-ready"
            : isBlocked ? "chain-blocked" : "chain-todo";
          const stepBaseClass = `chain-step chain-step-${meta.idx} ${stateClass} ${isCurrent ? "chain-current" : ""}`;
          const iconNode = isReady ? <CheckOutlined className="text-white" />
            : isBlocked ? <ExclamationCircleOutlined className="text-red-600" />
            : <span className="font-semibold text-slate-400">{meta.idx}</span>;
          const dotBgClass = isReady ? "bg-green-500 border-green-600 text-white"
            : isBlocked ? "bg-slate-200 border-red-400 text-red-600"
            : "bg-slate-100 border-slate-300 text-slate-500";
          const labelStrongCls = isReady ? "text-green-700"
            : isBlocked ? "text-slate-500" : "text-slate-600";
          const labelMutedCls = isReady ? "text-green-600"
            : isBlocked ? "text-slate-400" : "text-slate-500";
          const stepContent = (
            <div
              className={`${stepBaseClass} flex items-center gap-2 rounded-md px-2 py-1.5 transition-colors`}
              data-testid={`chain-step-${meta.idx}`}
              data-step-state={isReady ? "ready" : isBlocked ? "blocked" : "todo"}
            >
              <div className={`chain-dot flex h-7 w-7 shrink-0 items-center justify-center rounded-full border text-xs shadow-sm ${dotBgClass}`}>
                {iconNode}
              </div>
              <div className="chain-step-label">
                <span className={`text-[12px] font-semibold ${labelStrongCls}`}>步骤 {meta.idx}</span>
                <span className={`text-[11px] ${labelMutedCls}`}>{meta.shortName}</span>
              </div>
              {isBlocked && meta.goAction && (
                <Button
                  size="small"
                  type="primary"
                  ghost
                  className="ml-1 !h-6 !px-2 !text-[11px]"
                  data-testid={meta.goAction.testId}
                  onClick={(e) => { e.stopPropagation(); onGoStep?.(meta.idx); }}
                >
                  {meta.goAction.label}
                </Button>
              )}
            </div>
          );
          const connectorCls = `chain-connector mx-0.5 h-px w-5 self-center ${isReady ? "bg-green-300" : "bg-slate-200"}`;
          const wrapped = isBlocked ? (
            <Tooltip key={meta.idx} title={meta.blockReason} placement="top">
              {stepContent}
            </Tooltip>
          ) : (
            <React.Fragment key={meta.idx}>{stepContent}</React.Fragment>
          );
          return (
            <React.Fragment key={`w-${meta.idx}`}>
              {wrapped}
              {i < STEP_META.length - 1 && <div className={connectorCls} aria-hidden />}
            </React.Fragment>
          );
        })}
      </div>
      <div className="chain-next-action shrink-0 pl-3">
        <Button
          size="small"
          onClick={() => onGoStep?.(1)}
          data-testid="chain-btn-restart-step1"
          className="chain-restart-step1"
        >
          从步骤1重新开始
        </Button>
        <Button
          type="primary"
          size="large"
          disabled={!nextEnabled}
          onClick={handleNextPrimary}
          icon={current === 8 ? <ArrowRightOutlined /> : null}
          className="!font-semibold"
          data-testid={current === 8 ? "chain-btn-start-scoring" : "chain-btn-next"}
        >
          {current === 8 ? "开始评分消费→" : `下一步：${nextStepName}`}
        </Button>
      </div>
    </div>
  );
};

export default ChainStepsBar;
