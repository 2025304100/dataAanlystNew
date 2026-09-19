import { t } from "../../../../../i18n";
import type { PoolSnapshot } from "./poolApi";

/**
 * 候选池分析看板弹窗（向导 §3.7.4）——**只读，不可编辑**。
 *
 * 布局：概览卡片（股票数量/平均市值/时间范围/数据完整度）→
 * 市值分布 / 行业分布 / 风格暴露 / 市场环境 → 数据质量（字段覆盖率）；
 * 底部 [重新选择] [下一步]。右上角显示数据截止日与分析时间。
 *
 * 只读约束：弹窗内**不渲染任何 input/select/textarea**（有测试断言）。
 */
export interface PoolAnalysisModalProps {
  snapshot: PoolSnapshot | null;
  onClose: () => void;
  onReselect: () => void;
  onNext: () => void;
}

function Card({ label, value }: { label: string; value: string }) {
  return (
    <div className="mining-pool-board-card">
      <span className="mining-pool-board-card-label">{label}</span>
      <span className="mining-pool-board-card-value">{value}</span>
    </div>
  );
}

export default function PoolAnalysisModal({
  snapshot, onClose, onReselect, onNext,
}: PoolAnalysisModalProps) {
  const analysis = (snapshot?.analysis_json ?? {}) as Record<string, any>;
  return (
    <div className="mining-pool-modal-mask" role="dialog" aria-modal="true">
      <div className="mining-pool-modal" data-pool-analysis-modal>
        <header className="mining-pool-modal-head">
          <h3>{t("miningPoolBoardTitle")}</h3>
          <div className="mining-pool-modal-meta">
            <span>
              {t("miningPoolBoardAsOf")}: {snapshot?.as_of_date ?? "-"}
            </span>
            <span>
              {t("miningPoolBoardAnalyzedAt")}: {snapshot?.analyzed_at ?? "-"}
            </span>
          </div>
          <button type="button" className="mining-pool-modal-close" onClick={onClose}>
            ×
          </button>
        </header>

        <section className="mining-pool-board-cards">
          <Card label={t("miningPoolBoardCount")} value={String(analysis.symbol_count ?? "-")} />
          <Card label={t("miningPoolBoardAvgCap")} value={String(analysis.avg_market_cap ?? "-")} />
          <Card label={t("miningPoolBoardRange")} value={String(analysis.range ?? "-")} />
          <Card label={t("miningPoolBoardIntegrity")} value={String(analysis.integrity ?? "-")} />
        </section>

        <section className="mining-pool-board-grid">
          <div>
            <h4>{t("miningPoolBoardCapDist")}</h4>
            <pre>{JSON.stringify(analysis.market_cap_distribution ?? [], null, 2)}</pre>
          </div>
          <div>
            <h4>{t("miningPoolBoardIndustryDist")}</h4>
            <pre>{JSON.stringify(analysis.industry_distribution ?? [], null, 2)}</pre>
          </div>
          <div>
            <h4>{t("miningPoolBoardStyle")}</h4>
            <pre>{JSON.stringify(analysis.style_exposure ?? [], null, 2)}</pre>
          </div>
          <div>
            <h4>{t("miningPoolBoardMarket")}</h4>
            <pre>{JSON.stringify(analysis.market_environment ?? [], null, 2)}</pre>
          </div>
        </section>

        <section className="mining-pool-board-quality">
          <h4>{t("miningPoolBoardQuality")}</h4>
          <pre>{JSON.stringify(analysis.field_coverage ?? [], null, 2)}</pre>
          {Object.keys(analysis).length === 0 && <p>{t("miningPoolBoardEmpty")}</p>}
        </section>

        <footer className="mining-pool-modal-foot">
          <button type="button" className="mining-pool-btn" data-pool-reselect onClick={onReselect}>
            {t("miningPoolReselect")}
          </button>
          <button type="button" className="mining-pool-btn primary" data-pool-next onClick={onNext}>
            {t("miningPoolNext")}
          </button>
        </footer>
      </div>
    </div>
  );
}
