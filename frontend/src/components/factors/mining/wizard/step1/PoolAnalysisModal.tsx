import { t } from "../../../../../i18n";
import MiningChart, {
  MINING_CHART_COLORS,
  miningBaseOption,
} from "../../charts/MiningChart";
import type { PoolAnalysis, PoolSnapshot } from "./poolApi";
import type { EChartsOption } from "echarts";

/**
 * 候选池分析看板弹窗（向导 §3.7.4）——**只读，不可编辑**。
 *
 * 布局：概览卡片（股票数量/平均市值/时间范围/数据完整度）→
 * 市值分布 / 行业分布（横向柱状） · 风格暴露（雷达） / 市场环境（阶段/波动率/趋势 + 因子建议星级）
 * → 数据质量（字段覆盖率进度条，<80% 标红）→ 分析提示；底部 [重新选择] [下一步]。
 *
 * **字段绑定后端真实契约**（`analysis.build_analysis` 的返回结构）：
 * `overview` / `market_cap_distribution[]` / `industry_distribution[]` /
 * `industry_concentration` / `style_exposure.dimensions` /
 * `market_environment.{market_regime,volatility,trend_strength,factor_type_suggestions}` /
 * `data_quality.fields[]` / `warnings[]`。缺失项按空态展示，不臆造。
 *
 * 只读约束：弹窗内**不渲染任何 input/select/textarea**（有测试断言）。
 */
export interface PoolAnalysisModalProps {
  snapshot: PoolSnapshot | null;
  onClose: () => void;
  onReselect: () => void;
  onNext: () => void;
}

/** 后端因子类型枚举 → 展示名（未知值原样展示） */
const FACTOR_TYPE_LABEL: Record<string, string> = {
  trend: "趋势",
  reversal: "反转",
  volatility: "波动率",
  valuation: "估值",
  quality: "质量",
  volume_price: "量价",
  momentum: "动量",
  vol_contraction: "波动率收缩",
  breakout: "突破",
};

const STYLE_ORDER = ["growth", "value", "quality", "momentum", "volatility"];

function typeLabel(key: string): string {
  return FACTOR_TYPE_LABEL[key] ?? key;
}

/** 百分比数值（后端 ratio/coverage 均为 0~1） */
function pct(value: number | null | undefined, digits = 1): string {
  if (value == null || !Number.isFinite(value)) return "-";
  const v = value <= 1.0000001 ? value * 100 : value;
  return `${v.toFixed(digits)}%`;
}

/** 横向柱状图（市值/行业分布共用，值取 ratio*100） */
function horizontalBarOption(
  rows: Array<{ label: string; ratio: number }>,
): EChartsOption {
  const reversed = rows.slice().reverse();
  return {
    ...miningBaseOption(),
    grid: { left: 8, right: 46, top: 8, bottom: 8, containLabel: true },
    xAxis: { type: "value", show: false, max: 100 },
    yAxis: {
      type: "category",
      data: reversed.map((r) => r.label),
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { color: MINING_CHART_COLORS.axisLabel, fontSize: 12 },
    },
    series: [
      {
        type: "bar",
        data: reversed.map((r) => r.ratio * 100),
        barMaxWidth: 14,
        itemStyle: { color: MINING_CHART_COLORS.brand, borderRadius: [0, 6, 6, 0] },
        label: {
          show: true,
          position: "right",
          formatter: (p: { value?: unknown }) => `${Number(p.value ?? 0).toFixed(1)}%`,
          color: MINING_CHART_COLORS.axisLabel,
          fontSize: 11,
        },
      },
    ],
  };
}

/** 风格暴露雷达（0~1 分位；不可用维度不画） */
function radarOption(dims: Array<{ label: string; score: number }>): EChartsOption {
  return {
    ...miningBaseOption(),
    tooltip: { ...miningBaseOption().tooltip, trigger: "item" },
    radar: {
      indicator: dims.map((d) => ({ name: d.label, max: 1 })),
      radius: "66%",
      splitNumber: 4,
      axisName: { color: MINING_CHART_COLORS.axisLabel, fontSize: 11 },
      axisLine: { lineStyle: { color: MINING_CHART_COLORS.axis } },
      splitLine: { lineStyle: { color: MINING_CHART_COLORS.axis } },
      splitArea: { areaStyle: { color: ["rgba(15,118,110,0.03)", "rgba(15,118,110,0.07)"] } },
    },
    series: [
      {
        type: "radar",
        data: [
          {
            value: dims.map((d) => d.score),
            name: t("miningPoolBoardStyle"),
            areaStyle: { color: MINING_CHART_COLORS.brandSoft },
            lineStyle: { color: MINING_CHART_COLORS.brand, width: 2 },
            itemStyle: { color: MINING_CHART_COLORS.brand },
          },
        ],
      },
    ],
  };
}

export default function PoolAnalysisModal({
  snapshot, onClose, onReselect, onNext,
}: PoolAnalysisModalProps) {
  const analysis = (snapshot?.analysis_json ?? null) as PoolAnalysis | null;
  const overview = analysis?.overview ?? {};
  const capTiers = analysis?.market_cap_distribution ?? [];
  const industries = analysis?.industry_distribution ?? [];
  const concentration = analysis?.industry_concentration ?? null;
  const styleDims = analysis?.style_exposure?.dimensions ?? {};
  const env = analysis?.market_environment ?? {};
  const qualityFields = analysis?.data_quality?.fields ?? [];
  const warnings = analysis?.warnings ?? [];
  const hasAnalysis = analysis != null && Object.keys(analysis).length > 0;

  const capRows = capTiers.map((c) => ({ label: c.label_zh, ratio: c.ratio }));
  const industryRows = industries.map((i) => ({ label: i.industry, ratio: i.ratio }));

  const styleRows = STYLE_ORDER.map((key) => {
    const dim = styleDims[key];
    return {
      key,
      label: dim?.label_zh ?? key,
      score: dim?.available && dim.score != null ? dim.score : null,
      reason: dim?.reason_zh ?? null,
      coverage: dim?.coverage ?? null,
    };
  });
  const availableStyles = styleRows.filter(
    (s): s is typeof s & { score: number } => s.score != null,
  );
  const dominantKey = analysis?.style_exposure?.dominant_style ?? null;
  const dominantDim = dominantKey ? styleDims[dominantKey] : undefined;

  const suggestions = env.factor_type_suggestions ?? [];

  const Card = ({ label, value, note }: { label: string; value: string; note?: string | null }) => (
    <div className="mining-pool-board-card">
      <span className="mining-pool-board-card-label">{label}</span>
      <span className="mining-pool-board-card-value">{value}</span>
      {note ? <span className="mining-pool-board-card-label">{note}</span> : null}
    </div>
  );

  return (
    <div className="mining-pool-modal-mask" role="dialog" aria-modal="true">
      <div className="mining-pool-modal" data-pool-analysis-modal>
        <header className="mining-pool-modal-head">
          <h3>{t("miningPoolBoardTitle")}</h3>
          <div className="mining-pool-modal-meta">
            <span>
              {t("miningPoolBoardAsOf")}:{" "}
              {analysis?.as_of_date ?? snapshot?.as_of_date ?? "-"}
            </span>
            <span>
              {t("miningPoolBoardAnalyzedAt")}:{" "}
              {analysis?.generated_at ?? snapshot?.analyzed_at ?? "-"}
            </span>
          </div>
          <button
            type="button"
            className="mining-pool-modal-close"
            aria-label={t("miningResClose")}
            onClick={onClose}
          >
            ×
          </button>
        </header>

        <section className="mining-pool-board-cards">
          <Card
            label={t("miningPoolBoardCount")}
            value={overview.stock_count != null ? String(overview.stock_count) : "-"}
            note={overview.below_min_pool_size ? t("miningPoolBoardBelowFloor") : null}
          />
          <Card
            label={t("miningPoolBoardAvgCap")}
            value={
              overview.avg_market_cap_yi != null
                ? `${overview.avg_market_cap_yi} ${t("miningPoolBoardYi")}`
                : "-"
            }
            note={overview.market_cap_band_zh}
          />
          <Card
            label={t("miningPoolBoardRange")}
            value={String(overview.time_range ?? "-")}
          />
          <Card
            label={t("miningPoolBoardIntegrity")}
            value={pct(overview.data_completeness)}
          />
        </section>

        <section className="mining-pool-board-grid">
          <div className="mining-pool-board-chart" data-pool-board-chart="cap">
            <h4>{t("miningPoolBoardCapDist")}</h4>
            <MiningChart
              testId="cap"
              height={200}
              emptyText={t("miningPoolBoardNoData")}
              option={horizontalBarOption(capRows)}
            />
          </div>

          <div className="mining-pool-board-chart" data-pool-board-chart="industry">
            <h4>
              {t("miningPoolBoardIndustryDist")}
              {concentration?.alert ? (
                <span className="mining-chip mining-chip--warn">
                  {concentration.alert_zh ?? t("miningPoolBoardConcentration")}
                </span>
              ) : null}
            </h4>
            <MiningChart
              testId="industry"
              height={200}
              emptyText={t("miningPoolBoardNoData")}
              option={horizontalBarOption(industryRows)}
            />
          </div>

          <div className="mining-pool-board-chart" data-pool-board-chart="style">
            <h4>{t("miningPoolBoardStyle")}</h4>
            <MiningChart
              testId="style"
              height={210}
              emptyText={t("miningPoolBoardNoData")}
              option={radarOption(availableStyles.map((s) => ({ label: s.label, score: s.score })))}
            />
            {(dominantDim?.label_zh ||
              analysis?.style_exposure?.complement_suggestion_zh) && (
              <div className="mining-pool-board-suggest">
                {dominantDim?.label_zh && (
                  <span className="mining-pool-board-suggest-item">
                    {t("miningPoolBoardDominantStyle")}：{dominantDim.label_zh}
                    {analysis?.style_exposure?.dominant_score != null
                      ? ` ${pct(analysis.style_exposure.dominant_score, 0)}`
                      : ""}
                  </span>
                )}
                {analysis?.style_exposure?.complement_suggestion_zh && (
                  <span className="mining-pool-board-suggest-item">
                    {t("miningPoolBoardComplement")}：
                    {analysis.style_exposure.complement_suggestion_zh}
                  </span>
                )}
              </div>
            )}
            {/* 不可用维度如实标注（后端不造数，前端也不隐藏） */}
            {styleRows.some((s) => s.score == null) && (
              <p className="mining-hint">
                {styleRows
                  .filter((s) => s.score == null)
                  .map((s) => `${s.label}：${s.reason ?? "-"}`)
                  .join(" · ")}
              </p>
            )}
          </div>

          <div className="mining-pool-board-chart" data-pool-board-chart="market">
            <h4>{t("miningPoolBoardMarket")}</h4>
            {(env.market_regime || env.volatility || env.trend_strength) ? (
              <div className="mining-pool-board-env">
                <div className="mining-pool-board-env-row">
                  <span>{t("miningPoolBoardEnvStage")}</span>
                  <span>{env.market_regime?.label_zh ?? "-"}</span>
                </div>
                <div className="mining-pool-board-env-row">
                  <span>{t("miningPoolBoardEnvVol")}</span>
                  <span>
                    {env.volatility?.label_zh ?? "-"}
                    {env.volatility?.annualized != null
                      ? `（年化 ${pct(env.volatility.annualized)}）`
                      : ""}
                  </span>
                </div>
                <div className="mining-pool-board-env-row">
                  <span>{t("miningPoolBoardEnvTrend")}</span>
                  <span>{env.trend_strength?.label_zh ?? "-"}</span>
                </div>
                {env.market_regime?.cumulative_return != null && (
                  <div className="mining-pool-board-env-row">
                    <span>区间累计收益</span>
                    <span>{pct(env.market_regime.cumulative_return, 2)}</span>
                  </div>
                )}
                {env.market_regime?.max_drawdown != null && (
                  <div className="mining-pool-board-env-row">
                    <span>最大回撤</span>
                    <span>{pct(env.market_regime.max_drawdown, 2)}</span>
                  </div>
                )}
              </div>
            ) : (
              <p className="mining-hint">{t("miningPoolBoardNoData")}</p>
            )}
            {suggestions.length > 0 && (
              <div className="mining-pool-board-suggest" data-pool-board-suggest>
                <span className="mining-pool-board-suggest-item">
                  {t("miningPoolBoardFactorSuggest")}
                </span>
                {suggestions.map((s) => (
                  <span className="mining-pool-board-suggest-item" key={s.factor_type}>
                    {typeLabel(s.factor_type)}
                    <span className="mining-pool-board-stars">
                      {s.stars_label_zh ?? "★".repeat(s.stars)}
                    </span>
                  </span>
                ))}
              </div>
            )}
          </div>
        </section>

        <section className="mining-pool-board-quality" data-pool-board-quality>
          <h4>{t("miningPoolBoardQuality")}</h4>
          {qualityFields.length > 0 ? (
            <div className="mining-pool-board-cov">
              {qualityFields.map((f) => {
                const low = f.below_threshold || f.coverage < 0.8;
                return (
                  <div className="mining-pool-board-cov-row" key={f.field}>
                    <span className="mining-pool-board-cov-code" title={f.source ?? f.field}>
                      {f.field}
                    </span>
                    <span className="mining-meter mining-meter--sm">
                      <span
                        className={`mining-meter-fill ${low ? "mining-meter-fill--danger" : ""}`}
                        style={{ width: `${Math.max(0, Math.min(100, f.coverage * 100))}%` }}
                      />
                    </span>
                    <span className={`mining-pool-board-cov-pct ${low ? "is-low" : ""}`}>
                      {pct(f.coverage)}
                    </span>
                  </div>
                );
              })}
            </div>
          ) : (
            <p className="mining-hint">{t("miningPoolBoardNoData")}</p>
          )}

          {(analysis?.trade_days_actual != null ||
            analysis?.avg_daily_symbols != null ||
            analysis?.lookback_days != null) && (
            <div className="mining-pool-board-env">
              {analysis?.trade_days_actual != null && (
                <div className="mining-pool-board-env-row">
                  <span>{t("miningPoolBoardValidDays")}</span>
                  <span>{analysis.trade_days_actual}</span>
                </div>
              )}
              {analysis?.avg_daily_symbols != null && (
                <div className="mining-pool-board-env-row">
                  <span>{t("miningPoolBoardAvgDaily")}</span>
                  <span>{analysis.avg_daily_symbols}</span>
                </div>
              )}
              {analysis?.lookback_days != null && (
                <div className="mining-pool-board-env-row">
                  <span>{t("miningPoolBoardLookback")}</span>
                  <span>{analysis.lookback_days}</span>
                </div>
              )}
            </div>
          )}

          {qualityFields.some((f) => f.below_threshold || f.coverage < 0.8) && (
            <span className="mining-chip mining-chip--danger">
              {t("miningPoolBoardCoverageLow")}
            </span>
          )}
          {!hasAnalysis && <p className="mining-hint">{t("miningPoolBoardEmpty")}</p>}
        </section>

        {warnings.length > 0 && (
          <section className="mining-pool-board-quality" data-pool-board-warnings>
            <h4>{t("miningPoolBoardWarnings")}</h4>
            <div className="mining-banner mining-banner--muted">
              <div className="mining-banner-body">
                {warnings.map((w, i) => (
                  <span key={i}>{w}</span>
                ))}
              </div>
            </div>
          </section>
        )}

        {/* 完全无法识别的结构：原样展示，不臆造图形也不静默丢弃 */}
        {hasAnalysis && capRows.length === 0 && industryRows.length === 0 && (
          <section className="mining-pool-board-quality">
            <details className="mining-mirror-error-detail">
              <summary>{t("miningPoolBoardRaw")}</summary>
              <pre className="mining-pool-board-raw">{JSON.stringify(analysis, null, 2)}</pre>
            </details>
          </section>
        )}

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
