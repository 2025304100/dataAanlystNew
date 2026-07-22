// WP5.1：因子解释面板
//
// 设计说明：
// - 任务清单 #4：因子解释 + 模型 ID + 因子贡献 + 数据截止时间
// - 从 InvestmentCenter.tsx 抽出 renderFactorExplanation JSX 块
// - 因子数据获取逻辑（loadFactorExplanation）保留在 Shell，通过 props 传入
// - 不重写因子计算、权重模式、宏观因子的展示逻辑
import { Button, Tag } from "antd";
import { ExperimentOutlined, ReloadOutlined } from "@ant-design/icons";
import { t } from "../../i18n";
import { pnlClass, score } from "../../utils/format";
import type { FactorExplanationPanelProps } from "./types";

/**
 * 因子解释面板：展示模型 ID、权重模式、因子贡献、数据截止时间。
 * - 数据获取由 Shell 通过 useFactorExplanation 完成
 * - 本组件只负责展示
 */
export default function FactorExplanationPanel({
  symbolId,
  factorExplanation,
  loading,
  error,
  onRefresh,
}: FactorExplanationPanelProps) {
  if (!symbolId) return null;

  const factors = [
    ...Object.entries(factorExplanation?.explanation.factors ?? {}),
    ...Object.entries(factorExplanation?.explanation.event_factors ?? {}),
  ].sort(
    ([, left], [, right]) =>
      Math.abs(Number(right.contribution ?? 0)) -
      Math.abs(Number(left.contribution ?? 0)),
  );
  const macroRegime =
    factorExplanation?.macro_regime ??
    factorExplanation?.explanation.macro?.regime ??
    "-";
  const multiplier =
    factorExplanation?.macro_position_multiplier ??
    factorExplanation?.explanation.macro?.position_multiplier;
  const macroDetail = factorExplanation?.explanation.macro;

  return (
    <section className="ic__section ic__factor-section symbol-research-factor">
      <div className="panel">
        <div className="detail-card-head">
          <h3>
            <ExperimentOutlined /> {t("icFactorTitle")}
          </h3>
          <div className="detail-actions">
            {factorExplanation && (
              <>
                <Tag color={factorExplanation.weight_mode === "ridge" ? "green" : "blue"}>
                  {factorExplanation.weight_mode}
                </Tag>
                <Tag>{factorExplanation.trade_date}</Tag>
              </>
            )}
            <Button
              size="small"
              aria-label={t("icFactorRefresh")}
              title={t("icFactorRefresh")}
              icon={<ReloadOutlined />}
              loading={loading}
              onClick={onRefresh}
            />
          </div>
        </div>
        {loading && !factorExplanation ? (
          <div className="empty">{t("icFactorRefresh")}...</div>
        ) : !factorExplanation ? (
          <div className="empty">{error || t("icFactorUnavailable")}</div>
        ) : (
          <>
            <div className="ic__factor-summary">
              <div>
                <span>{t("icFactorMode")}</span>
                <strong>{factorExplanation.weight_mode}</strong>
              </div>
              <div title={factorExplanation.model_run_id}>
                <span>{t("icFactorModel")}</span>
                <strong>{factorExplanation.model_run_id.slice(0, 18)}</strong>
              </div>
              <div>
                <span>{t("icFactorTradeDate")}</span>
                <strong>{factorExplanation.trade_date}</strong>
              </div>
              <div>
                <span>{t("icFactorCutoff")}</span>
                <strong>
                  {factorExplanation.factor_data_cutoff_at?.replace("T", " ").slice(0, 19) ?? "-"}
                </strong>
              </div>
              <div>
                <span>{t("icFactorQuality")}</span>
                <strong>{score(factorExplanation.factor_quality_score, 1)}</strong>
              </div>
              <div>
                <span>{t("icFactorTiming")}</span>
                <strong>{score(factorExplanation.factor_timing_score, 1)}</strong>
              </div>
              <div>
                <span>{t("icFactorAlpha")}</span>
                <strong>{score(factorExplanation.model_alpha_score, 1)}</strong>
              </div>
              <div>
                <span>{t("icFactorMacro")}</span>
                <strong>{macroRegime}</strong>
              </div>
              <div>
                <span>{t("icFactorMultiplier")}</span>
                <strong>{multiplier == null ? "-" : `${score(multiplier, 2)}x`}</strong>
              </div>
              <div>
                <span>{t("icFactorMarketLiquidity")}</span>
                <strong>{score(macroDetail?.liquidity_score, 1)}</strong>
              </div>
              <div>
                <span>{t("icFactorMarketAmountChange")}</span>
                <strong>
                  {macroDetail?.market_amount_change_ratio == null
                    ? "-"
                    : `${score(macroDetail.market_amount_change_ratio * 100, 2)}%`}
                </strong>
              </div>
              <div>
                <span>{t("icFactorMarketAmountZ")}</span>
                <strong>{score(macroDetail?.market_amount_z20, 2)}</strong>
              </div>
              <div>
                <span>{t("icFactorAdvancingRatio")}</span>
                <strong>
                  {macroDetail?.advancing_ratio == null
                    ? "-"
                    : `${score(macroDetail.advancing_ratio * 100, 1)}%`}
                </strong>
              </div>
              <div>
                <span>{t("icFactorLeverageDivergence")}</span>
                <strong>
                  {macroDetail?.margin_amount_divergence == null
                    ? "-"
                    : `${score(macroDetail.margin_amount_divergence * 100, 2)}%`}
                </strong>
              </div>
            </div>
            <div className="ic__factor-table" role="table">
              <div className="ic__factor-row ic__factor-row--head" role="row">
                <span>{t("icFactorFactor")}</span>
                <span>{t("icFactorRaw")}</span>
                <span>{t("icFactorNormalized")}</span>
                <span>{t("icFactorCoefficient")}</span>
                <span>{t("icFactorContribution")}</span>
              </div>
              {factors.map(([code, item]) => (
                <div key={code} className="ic__factor-row" role="row">
                  <span>
                    <strong>{t("icFactorName_" + code)}</strong>
                    <small>
                      {code}
                      {item.is_imputed ? ` · ${t("icFactorImputed")}` : ""}
                    </small>
                  </span>
                  <span>{score(item.raw_value, 4)}</span>
                  <span>{score(item.normalized_value, 3)}</span>
                  <span className={pnlClass(item.coefficient)}>{score(item.coefficient, 4)}</span>
                  <span className={pnlClass(item.contribution)}>{score(item.contribution, 4)}</span>
                </div>
              ))}
            </div>
          </>
        )}
      </div>
    </section>
  );
}
