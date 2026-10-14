import { useState } from "react";
import { Alert, Button, Tag } from "antd";
import { ArrowLeftOutlined, DatabaseOutlined, ReloadOutlined } from "@ant-design/icons";
import ExternalDataSync from "../ExternalDataSync";
import "./FactorPipelinePrepareStep.css";

const INPUT_DRAFT_STORAGE_KEY = "factor-pipeline:inputs:v1";

export type FactorPipelinePrepareStepProps = {
  loading: boolean;
  featureEnabled: boolean;
  warehouseAvailable: boolean;
  coverage: number | null;
  latestDate: string | null;
  error?: string | null;
  busy?: boolean;
  onEnable: () => void;
  onInitialize: () => void;
  onRefresh: () => void;
  onCoverage?: () => void;
  onViewChange?: (view: "check" | "inputs") => void;
};

export default function FactorPipelinePrepareStep({
  loading,
  featureEnabled,
  warehouseAvailable,
  coverage,
  latestDate,
  error,
  busy = false,
  onEnable,
  onInitialize,
  onRefresh,
  onCoverage,
  onViewChange,
}: FactorPipelinePrepareStepProps) {
  const [inputPage, setInputPage] = useState(false);
  const known = !loading && !error;
  const pendingLabel = loading ? "确认中" : "待确认";
  const coverageText = known
    ? coverage !== null && Number.isFinite(coverage)
      ? `${(Math.min(1, Math.max(0, coverage)) * 100).toFixed(1)}%`
      : "尚无覆盖统计"
    : pendingLabel;

  if (inputPage) {
    return (
      <section className="factor-prepare-step factor-prepare-input-page" aria-label="因子输入操作页">
        <div className="factor-prepare-input-head">
          <div>
            <h2>准备因子输入</h2>
            <p>选择标的和日期，按数据集同步所需输入。完成后返回检查，再继续下一步。</p>
          </div>
          <Button aria-label="返回输入检查" icon={<ArrowLeftOutlined />} onClick={() => {
            setInputPage(false);
            onViewChange?.("check");
            onRefresh();
          }}>返回输入检查</Button>
        </div>
        <ExternalDataSync showScoringRefresh={false} draftStorageKey={INPUT_DRAFT_STORAGE_KEY} />
      </section>
    );
  }

  return (
    <section className="factor-prepare-step" aria-label="因子输入检查">
      <div className="factor-prepare-head">
        <div>
          <h2>检查因子输入</h2>
          <p>先确认因子功能和本地仓库可用，再准备训练所需的行情与外部数据。</p>
        </div>
        <Button aria-label="重新检查" icon={<ReloadOutlined />} loading={loading} disabled={busy} onClick={onRefresh}>重新检查</Button>
      </div>

      {error && <Alert
        type="error"
        showIcon
        message="输入状态暂时无法确认"
        description={error}
        action={<Button size="small" loading={loading} disabled={busy} onClick={onRefresh}>重试检查</Button>}
      />}

      <dl className="factor-prepare-status-list" aria-busy={loading}>
        <div className="factor-prepare-status-item">
          <dt>因子功能</dt>
          <dd><Tag color={known && featureEnabled ? "success" : known ? "warning" : "default"}>
            {known ? featureEnabled ? "已开启" : "需要开启" : pendingLabel}
          </Tag></dd>
          {known && !featureEnabled && <Button size="small" loading={busy} onClick={onEnable}>开启因子功能</Button>}
        </div>
        <div className="factor-prepare-status-item">
          <dt>本地因子仓库</dt>
          <dd><Tag color={known && warehouseAvailable ? "success" : known ? "warning" : "default"}>
            {known ? warehouseAvailable ? "可用" : "需要初始化" : pendingLabel}
          </Tag></dd>
          {known && !warehouseAvailable && <Button size="small" loading={busy} onClick={onInitialize}>初始化因子仓库</Button>}
        </div>
        <div className="factor-prepare-status-item">
          <dt>平均输入覆盖率</dt>
          <dd className="factor-prepare-metric">{coverageText}</dd>
          {onCoverage && <Button size="small" type="link" disabled={!known || busy} onClick={onCoverage}>查看覆盖诊断</Button>}
        </div>
        <div className="factor-prepare-status-item">
          <dt>最新交易日</dt>
          <dd className="factor-prepare-metric">{known ? latestDate || "尚无交易日记录" : pendingLabel}</dd>
        </div>
      </dl>

      <div className="factor-prepare-action">
        <DatabaseOutlined className="factor-prepare-action-icon" />
        <div>
          <h3>补齐训练所需的输入</h3>
          <p>进入输入操作页查看数据缺口，并同步对应数据集。已有数据也可以直接继续，下一步会检查所选集合的训练资格。</p>
        </div>
        <Button type="primary" size="large" disabled={loading || busy} onClick={() => {
          setInputPage(true);
          onViewChange?.("inputs");
        }}>准备因子输入</Button>
      </div>
      <p className="factor-prepare-note">覆盖率是仓库统计；实际训练还会检查所选因子、训练日期范围和样本质量。</p>
    </section>
  );
}
